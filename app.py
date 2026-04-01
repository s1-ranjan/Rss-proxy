"""
Flask RSS Cleaning Proxy for Nitter/Twitter feeds
Optimized for Telegram RSS bots — supports both RSStT and simple bots.

Endpoints:
  GET /clean?url=<rss_url>[&format=html|text]
      → cleaned single feed
      → format=html  (DEFAULT) keeps HTML for RSStT — rich text, images, blockquotes
      → format=text  strips HTML to plain text for simple bots

  GET /clean-all?urls=url1,url2,…[&format=html|text]
      → merged + sorted + deduped feed

  GET /health   → service health check
  GET /         → usage info

Deploy with:
  gunicorn -c gunicorn.conf.py app:app
"""

import re
import time
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response, jsonify

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

MAX_ITEMS     = 20      # items returned per feed
CACHE_TTL     = 300     # cache lifetime in seconds (5 minutes)
MAX_TEXT_LEN  = 1200    # max plain-text description length (format=text only)
FETCH_TIMEOUT = 10      # upstream HTTP request timeout in seconds

# ── URL Whitelist ──────────────────────────────────────────────────────────────
# Exact feed URLs — add specific ones here if needed.
ALLOWED_FEED_URLS: set[str] = {
    "https://nitter.net/elonmusk/rss",
    "https://nitter.net/sama/rss",
    "https://nitter.privacydev.net/elonmusk/rss",
    "https://nitter.privacydev.net/sama/rss",
    "https://nitter.net/IPRDBihar/rss",
    # Add more as needed…
}
# Entire domains — any feed URL on these hosts is allowed.
ALLOWED_DOMAINS: set[str] = {
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "xcancel.com",
    "nitter.cz",
    "nitter.1d4.us",
    "nitter.kavin.rocks",
    "nitter.unixfox.eu",
    "nitter.42l.fr",
    "twitt.re",
    "nitter.pek.li",
}

# Image URL patterns that identify avatars / icons — always skip these
IGNORED_IMAGE_RE = re.compile(
    r"(avatar|profile_images|icon|logo|emoji|favicon|badge|default_profile)",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache   { cache_key: (timestamp, rss_payload) }
# ─────────────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, str]] = {}


def cache_get(key: str) -> str | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    _cache.pop(key, None)   # evict stale entry
    return None


def cache_set(key: str, value: str) -> None:
    _cache[key] = (time.time(), value)


# ─────────────────────────────────────────────────────────────────────────────
# Security — SSRF prevention
# ─────────────────────────────────────────────────────────────────────────────
_PRIVATE_PREFIXES = ("localhost", "127.", "10.", "192.168.", "172.", "0.0.0.0")


def is_url_allowed(url: str) -> bool:
    """
    Return True only when ALL of these pass:
    1. Scheme is http or https
    2. Host is not a private / loopback address
    3. URL or hostname is in the whitelist
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").lower()
    if not host or any(host.startswith(p) for p in _PRIVATE_PREFIXES):
        return False

    if url in ALLOWED_FEED_URLS:
        return True
    if host in ALLOWED_DOMAINS:
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Feed fetching
# ─────────────────────────────────────────────────────────────────────────────

def fetch_feed(url: str) -> feedparser.FeedParserDict | None:
    """Fetch and parse a remote RSS feed. Returns None on any failure."""
    try:
        resp = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RSSProxy/1.0)"},
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            log.warning("feedparser bozo for %s: %s", url, feed.bozo_exception)
            return None
        return feed
    except requests.Timeout:
        log.error("Timeout fetching %s", url)
    except requests.RequestException as exc:
        log.error("HTTP error for %s: %s", url, exc)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTML MODE — clean_html()
# Keeps the description as HTML for RSStT (which renders HTML itself).
# Removes junk (footer/cite URLs, avatars, hashtag links) while preserving
# all rich-text structure: <p>, <br>, <blockquote>, <b>, <img>, <a>, etc.
# ─────────────────────────────────────────────────────────────────────────────

def clean_html(html: str, base_url: str = "") -> str:
    """
    Lightly clean Nitter HTML for RSStT consumption.

    Keeps intact:
      - Paragraph / line-break tags  (<p>, <br>)
      - Quoted tweets                (<blockquote>, <b>)
      - Images                       (<img src="…">) — URLs resolved to absolute
      - Video thumbnail anchors      (<a href="…"><br>Video<br><img/></a>)
      - Post / profile links         (<a href="https://…">)

    Removes:
      - <footer> and <cite>          (raw Nitter citation back-links)
      - <hr/>                        (RSStT doesn't render it cleanly)
      - Hashtag search links         (<a href="/search?…">#tag</a>) → bare text
      - @mention links               (<a href="/User">@User</a>)    → bare text
      - Avatar / icon <img> tags
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 1. Remove <footer> and <cite> — Nitter puts citation back-links here
    for tag in soup.find_all(["footer", "cite"]):
        tag.decompose()

    # 2. Remove <hr/> — poorly supported in RSStT message rendering
    for hr in soup.find_all("hr"):
        hr.decompose()

    # 3. Fix <img> URLs: resolve relative/protocol-relative → absolute,
    #    strip inline styles, remove avatar/icon images entirely.
    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if not src:
            img.decompose()
            continue
        # Resolve protocol-relative
        if src.startswith("//"):
            src = "https:" + src
        # Resolve relative path
        elif not src.startswith("http") and base_url:
            src = urljoin(base_url, src)
        # Skip avatars / icons
        if IGNORED_IMAGE_RE.search(src):
            img.decompose()
            continue
        # Keep the image — clean attributes down to just src
        img.attrs = {"src": src}

    # 4. Handle <a> links:
    #    a) Hashtag search links  → decompose (remove the whole tag + text)
    #       because hashtags are noise in government/official feeds
    #    b) @mention links        → unwrap (keep display text, drop href)
    #    c) Everything else       → leave untouched (video links, post links …)
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        # Hashtag links (Nitter uses /search?…%23tag or href="#")
        if "%23" in href or (href.startswith("/search") and text.startswith("#")):
            a.decompose()
            continue
        # @mention links
        if text.startswith("@"):
            a.unwrap()
            continue
        # All other links — keep as-is

    # 5. Return cleaned HTML as a string (BeautifulSoup serialises it back)
    return str(soup)


# ─────────────────────────────────────────────────────────────────────────────
# TEXT MODE — html_to_text() + clean_text()
# Converts HTML to readable plain text for simple RSS bots that cannot
# render HTML (e.g. @RSSBot, basic feed readers).
# ─────────────────────────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """
    Convert Nitter HTML to clean plain text, preserving structure:
    - <p> / <br> / <div>  → paragraph breaks (newlines)
    - <blockquote>         → 💬 Author Name\\nquoted text
    - <hr/>                → ────────────────
    - <footer> / <cite>    → removed
    - Video <a> anchors    → ▶ Video
    - All other <a>        → unwrapped (text kept, href dropped)
    - <img>                → removed (handled via enclosures)
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 1. Remove <footer> and <cite>
    for tag in soup.find_all(["footer", "cite"]):
        tag.decompose()

    # 2. Remove all <img> tags — images go into <enclosure> tags instead
    for img in soup.find_all("img"):
        img.decompose()

    # 3. Handle <a> tags
    for a_tag in soup.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        if "video" in link_text.lower():
            a_tag.replace_with("\n▶ Video\n")
        else:
            a_tag.unwrap()

    # 4. Replace <hr/> with a visual divider
    for hr in soup.find_all("hr"):
        hr.replace_with("\n" + "─" * 16 + "\n")

    # 5. Format <blockquote> as a labelled quoted section.
    #    Author name extracted HERE so @handles are never stripped by clean_text.
    for bq in soup.find_all("blockquote"):
        author_tag = bq.find("b")
        if author_tag:
            author_name = author_tag.get_text(strip=True)
            author_tag.decompose()
        else:
            author_name = ""

        for inner in bq.find_all(["p", "br", "div"]):
            inner.insert_before("\n")
            inner.insert_after("\n")

        bq_text = bq.get_text(separator="", strip=False)
        bq_text = re.sub(r"\n{3,}", "\n\n", bq_text).strip()

        if author_name:
            quoted_block = f"\n\n💬 {author_name}\n{bq_text}\n"
        else:
            quoted_block = f"\n\n💬\n{bq_text}\n" if bq_text else ""

        bq.replace_with(quoted_block)

    # 6. Insert newlines at remaining block-level tags
    for tag in soup.find_all(["p", "br", "div", "li"]):
        tag.insert_before("\n")
        tag.insert_after("\n")

    text = soup.get_text(separator="", strip=False)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Lines starting with 💬 are author attribution — never strip @handles
_AUTHOR_LINE_RE  = re.compile(r"^💬")
# Pure divider lines — never touch
_DIVIDER_LINE_RE = re.compile(r"^─+$")


def _clean_line(line: str) -> str:
    """Strip URLs, @mentions, #hashtags from a single line of plain text.
    Skips author lines, divider lines, and video markers unchanged."""
    if _AUTHOR_LINE_RE.match(line):
        return line
    if _DIVIDER_LINE_RE.match(line):
        return line
    if line.strip() == "▶ Video":
        return line
    line = re.sub(r"https?://\S+", "", line)
    line = re.sub(r"@\w+", "", line)
    line = re.sub(r"#\w+", "", line)
    line = re.sub(r"[ \t]+", " ", line).strip()
    return line


def clean_text(raw: str) -> str:
    """
    Full plain-text cleaner:
    - Removes RT by / R to prefixes (both Nitter styles)
    - Cleans each line individually (preserves author/divider/video lines)
    - Collapses excessive blank lines (max 1 between paragraphs)
    - Soft-truncates to MAX_TEXT_LEN at a word boundary
    """
    if not raw:
        return ""

    # Handle both Nitter RT/reply prefix styles:
    #   "RT by @User: …"   (retweet)
    #   "R to @User: …"    (reply)
    text = re.sub(
        r"^R(?:T\s+by|(?:\s+to))\s+@\w+:\s*",
        "",
        raw.strip(),
        flags=re.IGNORECASE,
    )

    lines   = text.split("\n")
    cleaned = [_clean_line(line) for line in lines]

    # Collapse runs of blank lines to at most one
    result_lines: list[str] = []
    blank_count = 0
    for line in cleaned:
        if line == "":
            blank_count += 1
            if blank_count <= 1:
                result_lines.append("")
        else:
            blank_count = 0
            result_lines.append(line)

    text = "\n".join(result_lines).strip()

    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN].rsplit(" ", 1)[0] + "…"

    return text


def clean_title(raw: str) -> str:
    """
    Clean a tweet title for use in RSS <title>.
    Collapses multiline titles, strips RT/R-to prefixes, mentions, hashtags, URLs.
    Truncates to 200 chars.
    """
    if not raw:
        return ""
    text = " ".join(raw.split())   # collapse all whitespace including newlines
    text = re.sub(
        r"^R(?:T\s+by|(?:\s+to))\s+@\w+:\s*",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 200:
        text = text[:200].rsplit(" ", 1)[0] + "…"
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Image extraction  (used by both modes for <enclosure> tags)
# ─────────────────────────────────────────────────────────────────────────────

def extract_images(html: str, base_url: str = "") -> list[str]:
    """
    Extract ordered, deduplicated image URLs from HTML.
    - Prefers data-src over src (lazy-loaded images)
    - Resolves relative and protocol-relative URLs to absolute
    - Skips avatars / icons
    - Preserves carousel / thread order
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str]   = set()
    images: list[str] = []

    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http") and base_url:
            src = urljoin(base_url, src)
        if not src.startswith("http"):
            continue
        if IGNORED_IMAGE_RE.search(src):
            continue
        if src not in seen:
            seen.add(src)
            images.append(src)

    return images


# ─────────────────────────────────────────────────────────────────────────────
# Entry processor
# ─────────────────────────────────────────────────────────────────────────────

def process_entry(entry: feedparser.FeedParserDict, feed_url: str) -> dict:
    """
    Extract and clean all fields from a feedparser entry.
    Returns a normalised dict with BOTH html and text descriptions,
    plus images list and standard metadata.
    """
    # Raw HTML from feedparser (content[] takes priority over summary)
    raw_html = ""
    if getattr(entry, "content", None):
        raw_html = entry.content[0].get("value", "")
    if not raw_html:
        raw_html = getattr(entry, "summary", "")

    # Base URL for resolving relative image paths
    parsed_url = urlparse(feed_url)
    base_url   = f"{parsed_url.scheme}://{parsed_url.netloc}"

    # ── HTML description (for RSStT / format=html) ────────────────────────
    html_desc = clean_html(raw_html, base_url=base_url)

    # ── Plain-text description (for simple bots / format=text) ───────────
    text_desc = clean_text(html_to_text(raw_html))

    # ── Images (for <enclosure> tags — used in both modes) ────────────────
    images: list[str] = extract_images(raw_html, base_url=base_url)

    # Also collect from feedparser media_content / enclosures
    # (some Nitter instances put images here rather than in HTML)
    seen = set(images)
    for mc in getattr(entry, "media_content", []):
        url = mc.get("url", "").strip()
        if url and url not in seen and not IGNORED_IMAGE_RE.search(url):
            seen.add(url); images.append(url)
    for enc in getattr(entry, "enclosures", []):
        url = enc.get("url", "").strip()
        if url and url not in seen and not IGNORED_IMAGE_RE.search(url):
            seen.add(url); images.append(url)

    # ── Metadata ──────────────────────────────────────────────────────────
    title    = clean_title(getattr(entry, "title", "") or "")
    link     = getattr(entry, "link",  "") or ""
    guid     = getattr(entry, "id",   link) or link   # must stay stable
    pub_date = ""
    pp = getattr(entry, "published_parsed", None)
    if pp:
        dt = datetime(*pp[:6], tzinfo=timezone.utc)
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    return {
        "title":    title or "(no title)",
        "link":     link,
        "guid":     guid,
        "pub_date": pub_date,
        "html":     html_desc,    # RSStT / format=html
        "text":     text_desc,    # simple bots / format=text
        "images":   images,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RSS 2.0 builder
# ─────────────────────────────────────────────────────────────────────────────

def _xe(s: str) -> str:
    """Escape a string for use inside an XML attribute value."""
    return escape(s, {'"': "&quot;"})


def build_rss(
    feed_title: str,
    feed_link:  str,
    feed_desc:  str,
    items:      list[dict],
    mode:       str = "html",   # "html" → RSStT  |  "text" → simple bots
) -> str:
    """
    Produce a valid RSS 2.0 document.

    mode="html"  → <description> contains cleaned HTML (best for RSStT)
    mode="text"  → <description> contains plain text   (best for simple bots)

    Both modes include <enclosure> tags for images.
    """
    item_blocks: list[str] = []

    for item in items:
        images = item["images"]

        # Choose description content based on requested mode
        desc_content = item["html"] if mode == "html" else item["text"]

        # Build enclosure block — only when images exist
        if images:
            enc_block = "\n    " + "\n    ".join(
                f'<enclosure url="{_xe(img)}" type="image/jpeg" length="0"/>'
                for img in images
            )
        else:
            enc_block = ""

        item_blocks.append(
            f"""  <item>
    <title><![CDATA[{item['title']}]]></title>
    <link>{_xe(item['link'])}</link>
    <guid isPermaLink="false">{_xe(item['guid'])}</guid>
    <pubDate>{item['pub_date']}</pubDate>
    <description><![CDATA[{desc_content}]]></description>{enc_block}
  </item>"""
        )

    now       = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items_xml = "\n".join(item_blocks)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title><![CDATA[{feed_title}]]></title>
  <link>{_xe(feed_link)}</link>
  <description><![CDATA[{feed_desc}]]></description>
  <lastBuildDate>{now}</lastBuildDate>
{items_xml}
</channel>
</rss>"""


# ─────────────────────────────────────────────────────────────────────────────
# Flask application
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/clean")
def clean_feed():
    """
    GET /clean?url=<whitelisted_rss_url>[&format=html|text]

    format=html (DEFAULT) — description keeps HTML → best for RSStT
    format=text           — description is plain text → best for simple bots

    Responses are cached for CACHE_TTL seconds per (url, format) pair.
    """
    url = request.args.get("url",    "").strip()
    fmt = request.args.get("format", "html").strip().lower()

    if not url:
        return jsonify(error="Missing 'url' query parameter."), 400
    if fmt not in ("html", "text"):
        return jsonify(error="'format' must be 'html' or 'text'."), 400
    if not is_url_allowed(url):
        log.warning("Blocked request for URL: %s", url)
        return jsonify(error="URL not in whitelist."), 403

    # Cache key includes format so html and text are cached independently
    cache_key = hashlib.md5(f"{url}|{fmt}".encode()).hexdigest()
    if cached := cache_get(cache_key):
        log.info("Cache HIT: %s [%s]", url, fmt)
        return Response(cached, mimetype="application/rss+xml")

    log.info("Fetching [%s]: %s", fmt, url)
    feed = fetch_feed(url)
    if feed is None:
        return jsonify(error="Failed to fetch or parse the feed."), 502

    entries   = feed.entries[:MAX_ITEMS]
    processed = [process_entry(e, url) for e in entries]
    processed.sort(key=lambda x: x["pub_date"] or "", reverse=True)

    rss_out = build_rss(
        feed_title=getattr(feed.feed, "title",       "Cleaned Feed"),
        feed_link =getattr(feed.feed, "link",        url),
        feed_desc =getattr(feed.feed, "description", "Cleaned RSS feed"),
        items=processed,
        mode=fmt,
    )

    cache_set(cache_key, rss_out)
    return Response(rss_out, mimetype="application/rss+xml")


@app.route("/clean-all")
def clean_all_feeds():
    """
    GET /clean-all?urls=url1,url2,…[&format=html|text]
    Fetches multiple feeds, merges, sorts by pubDate, deduplicates by guid.
    """
    raw_urls = request.args.get("urls",   "").strip()
    fmt      = request.args.get("format", "html").strip().lower()

    if not raw_urls:
        return jsonify(error="Missing 'urls' query parameter."), 400
    if fmt not in ("html", "text"):
        return jsonify(error="'format' must be 'html' or 'text'."), 400

    urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    if not urls:
        return jsonify(error="No valid URLs provided."), 400

    blocked = [u for u in urls if not is_url_allowed(u)]
    if blocked:
        return jsonify(error="Some URLs are not in the whitelist.", blocked=blocked), 403

    combo_key = hashlib.md5(("|".join(sorted(urls)) + "|" + fmt).encode()).hexdigest()
    if cached := cache_get(combo_key):
        return Response(cached, mimetype="application/rss+xml")

    all_items: list[dict] = []
    seen_guids: set[str]  = set()

    for url in urls:
        feed = fetch_feed(url)
        if feed is None:
            log.warning("Skipping unreachable feed: %s", url)
            continue
        for entry in feed.entries[:MAX_ITEMS]:
            item = process_entry(entry, url)
            if item["guid"] in seen_guids:
                continue
            seen_guids.add(item["guid"])
            all_items.append(item)

    all_items.sort(key=lambda x: x["pub_date"] or "", reverse=True)

    rss_out = build_rss(
        feed_title="Combined Cleaned Feed",
        feed_link ="",
        feed_desc =f"Merged from {len(urls)} Nitter feed(s)",
        items=all_items,
        mode=fmt,
    )

    cache_set(combo_key, rss_out)
    return Response(rss_out, mimetype="application/rss+xml")


@app.route("/health")
def health():
    """Health-check — use for Render keep-alive / UptimeRobot ping."""
    return jsonify(status="ok", cached_feeds=len(_cache)), 200


@app.route("/")
def index():
    """Usage reference."""
    return jsonify(
        endpoints={
            "GET /clean":     "?url=<rss_url>[&format=html|text]",
            "GET /clean-all": "?urls=<url1>,<url2>,…[&format=html|text]",
            "GET /health":    "service health check",
        },
        format_modes={
            "html": "DEFAULT — keeps HTML description, best for RSStT bot",
            "text": "Strips HTML to plain text, best for simple RSS bots",
        },
        note="Only whitelisted Nitter RSS feed domains are accepted.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# WSGI entry-point
#   Production:  gunicorn -c gunicorn.conf.py app:app
#   Development: python app.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)

"""
Flask RSS Cleaning Proxy for Nitter/Twitter feeds
Optimized for Telegram RSS bots.

Endpoints:
  GET /clean?url=<rss_url>          → cleaned single feed
  GET /clean-all?urls=url1,url2,…   → merged + sorted + deduped feed
  GET /health                        → service health check
  GET /                              → usage info

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

MAX_ITEMS     = 20       # items returned per feed
CACHE_TTL     = 300      # seconds (5 minutes)
MAX_TEXT_LEN  = 9999     # characters — increased to fit quoted tweets cleanly
FETCH_TIMEOUT = 10       # seconds for upstream HTTP requests

# ── URL Whitelist ─────────────────────────────────────────────────────────────
# Add exact feed URLs OR just add the domain to ALLOWED_DOMAINS below.
ALLOWED_FEED_URLS: set[str] = {
    "https://nitter.net/IPRDBihar/rss",
    # Add specific feed URLs here if needed
}

# Any feed URL whose hostname matches is allowed.
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

# Image patterns that identify avatars / icons — skip these
IGNORED_IMAGE_RE = re.compile(
    r"(avatar|profile_images|icon|logo|emoji|favicon|badge|default_profile)",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache   { key: (timestamp, payload) }
# ─────────────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, str]] = {}


def cache_get(key: str) -> str | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    _cache.pop(key, None)
    return None


def cache_set(key: str, value: str) -> None:
    _cache[key] = (time.time(), value)


# ─────────────────────────────────────────────────────────────────────────────
# Security
# ─────────────────────────────────────────────────────────────────────────────
_PRIVATE_PREFIXES = ("localhost", "127.", "10.", "192.168.", "172.", "0.0.0.0")


def is_url_allowed(url: str) -> bool:
    """
    SSRF-safe URL check:
    1. Must be http/https
    2. Must not point to private/loopback addresses
    3. Must match exact whitelist OR whitelisted domain
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
    """Fetch and parse an RSS feed. Returns None on any error."""
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
# HTML → Plain text  (BeautifulSoup only, no regex on HTML)
# ─────────────────────────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """
    Convert Nitter HTML description to clean plain text.

    Handles:
    - <p> / <br> / <div>  → paragraph breaks
    - <blockquote>         → quoted tweet block with 💬 author header
    - <hr/>                → visual divider ────────────────
    - <footer> / <cite>    → removed (raw citation URLs)
    - <a> wrapping Video   → replaced with ▶ Video marker
    - all other <a>        → unwrapped (text kept, href dropped)
    - <img>                → removed from text (handled separately)
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 1. Remove <footer> and <cite> — Nitter puts raw citation URLs here
    for tag in soup.find_all(["footer", "cite"]):
        tag.decompose()

    # 2. Remove all <img> tags from text — images are extracted separately
    for img in soup.find_all("img"):
        img.decompose()

    # 3. Handle <a> tags:
    #    - Video anchors → ▶ Video marker
    #    - All others    → unwrap (keep text, discard href)
    for a_tag in soup.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        if "video" in link_text.lower():
            a_tag.replace_with("\n▶ Video\n")
        else:
            a_tag.unwrap()

    # 4. Replace <hr/> with a visual divider line
    for hr in soup.find_all("hr"):
        hr.replace_with("\n" + "─" * 16 + "\n")

    # 5. Format <blockquote> as a clean quoted section.
    #    Author name is extracted HERE before clean_text can strip @mentions.
    for bq in soup.find_all("blockquote"):
        # Extract author name from <b> tag (e.g. "Nitish Kumar (@NitishKumar)")
        author_tag = bq.find("b")
        if author_tag:
            author_name = author_tag.get_text(strip=True)
            author_tag.decompose()
        else:
            author_name = ""

        # Insert newlines at block-level tags inside the blockquote
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

    # 6. Insert newlines at remaining block-level elements
    for tag in soup.find_all(["p", "br", "div", "li"]):
        tag.insert_before("\n")
        tag.insert_after("\n")

    text = soup.get_text(separator="", strip=False)

    # Collapse 3+ newlines → max 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Text cleaning  (runs on plain text AFTER html_to_text)
# ─────────────────────────────────────────────────────────────────────────────

# Lines starting with 💬 are author attribution — never strip @handles from them
_AUTHOR_LINE_RE = re.compile(r"^💬")
# Lines that are pure dividers — never touch
_DIVIDER_LINE_RE = re.compile(r"^─+$")


def _clean_line(line: str) -> str:
    """
    Clean a single plain-text line:
    - Skip author lines (💬 ...) — preserve @handles in them
    - Skip divider lines (────)
    - Skip video markers (▶ Video)
    - Otherwise: remove URLs, @mentions, #hashtags, normalise spaces
    """
    if _AUTHOR_LINE_RE.match(line):
        return line
    if _DIVIDER_LINE_RE.match(line):
        return line
    if line.strip() == "▶ Video":
        return line

    line = re.sub(r"https?://\S+", "", line)   # remove URLs
    line = re.sub(r"@\w+", "", line)            # remove @mentions
    line = re.sub(r"#\w+", "", line)            # remove #hashtags
    line = re.sub(r"[ \t]+", " ", line).strip() # normalise spaces
    return line


def clean_text(raw: str) -> str:
    """
    Full description cleaner:
    - Removes RT/R-to/Reply prefixes (both Nitter styles)
    - Cleans line by line (preserving author/divider/video lines)
    - Removes orphan blank lines left after stripping mentions
    - Collapses excessive blank lines (max 1 between paragraphs)
    - Soft-truncates to MAX_TEXT_LEN at a word boundary
    """
    if not raw:
        return ""

    # FIX 1: Handle BOTH Nitter RT/reply prefix styles:
    #   "RT by @IPRDBihar: …"  (retweet)
    #   "R to @IPRDBihar: …"   (reply — Nitter's actual label)
    text = re.sub(
        r"^R(?:T\s+by|(?:\s+to))\s+@\w+:\s*",
        "",
        raw.strip(),
        flags=re.IGNORECASE,
    )

    # Clean line by line
    lines   = text.split("\n")
    cleaned = [_clean_line(line) for line in lines]

    # FIX 2: Remove lines that became empty ONLY because they were a lone
    # @mention (e.g. "@NitishKumar" on its own line → stripped to "").
    # We collapse all consecutive blank lines to max 1.
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

    # Soft-truncate at word boundary
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN].rsplit(" ", 1)[0] + "…"

    return text


def clean_title(raw: str) -> str:
    """
    Clean a tweet title (single-line — strips everything, no paragraph logic).
    Handles both 'RT by @user:' and 'R to @user:' prefixes.
    Truncates to 200 chars.
    """
    if not raw:
        return ""

    # FIX 3: Nitter titles can be multiline — collapse to single line first
    text = " ".join(raw.split())

    # Remove RT/reply prefixes
    text = re.sub(
        r"^R(?:T\s+by|(?:\s+to))\s+@\w+:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > 200:
        text = text[:200].rsplit(" ", 1)[0] + "…"

    return text


# ─────────────────────────────────────────────────────────────────────────────
# Image / media extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_images(html: str, base_url: str = "") -> list[str]:
    """
    Extract ordered, deduplicated image URLs from HTML.
    - Prefers data-src over src (lazy-loaded images)
    - Resolves relative and protocol-relative URLs to absolute
    - Skips avatars / icons matched by IGNORED_IMAGE_RE
    - Preserves carousel order
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
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
    Returns a normalised dict ready for RSS output.
    """
    # Raw HTML content — feedparser stores it in content[] or summary
    raw_html = ""
    if getattr(entry, "content", None):
        raw_html = entry.content[0].get("value", "")
    if not raw_html:
        raw_html = getattr(entry, "summary", "")

    # Description: HTML → plain text → clean
    clean = clean_text(html_to_text(raw_html))

    # Images from HTML body
    parsed_url = urlparse(feed_url)
    base_url   = f"{parsed_url.scheme}://{parsed_url.netloc}"
    images: list[str] = extract_images(raw_html, base_url=base_url)

    # FIX 4: Also pull images from feedparser media_content / enclosures
    # (some Nitter instances put images here instead of in HTML)
    seen = set(images)
    for mc in getattr(entry, "media_content", []):
        url = mc.get("url", "").strip()
        if url and url not in seen and not IGNORED_IMAGE_RE.search(url):
            seen.add(url)
            images.append(url)
    for enc in getattr(entry, "enclosures", []):
        url = enc.get("url", "").strip()
        if url and url not in seen and not IGNORED_IMAGE_RE.search(url):
            seen.add(url)
            images.append(url)

    # Title — use dedicated single-line cleaner
    title = clean_title(getattr(entry, "title", "") or "")

    link     = getattr(entry, "link", "") or ""
    guid     = getattr(entry, "id", link) or link   # must stay stable

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
        "text":     clean,
        "images":   images,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RSS 2.0 builder
# ─────────────────────────────────────────────────────────────────────────────

def _xe(s: str) -> str:
    """Escape a string for use inside an XML attribute value."""
    return escape(s, {'"': "&quot;"})


def build_rss(feed_title: str, feed_link: str, feed_desc: str, items: list[dict]) -> str:
    """
    Produce a valid RSS 2.0 document.
    - Description in CDATA (clean text only, no image URLs)
    - One <enclosure> per image for Telegram media display
    """
    item_blocks: list[str] = []

    for item in items:
        images = item["images"]

        # FIX 5: Only generate enclosure block when there are images.
        # Previously an empty enc_lines string left a trailing blank line
        # inside <item> which some RSS parsers flag as malformed.
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
    <description><![CDATA[{item['text']}]]></description>{enc_block}
  </item>"""
        )

    now      = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
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
    GET /clean?url=<whitelisted_rss_url>
    Returns a cleaned, Telegram-friendly RSS 2.0 feed.
    Responses are cached for CACHE_TTL seconds.
    """
    url = request.args.get("url", "").strip()

    if not url:
        return jsonify(error="Missing 'url' query parameter."), 400

    if not is_url_allowed(url):
        log.warning("Blocked request for URL: %s", url)
        return jsonify(error="URL not in whitelist."), 403

    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cached := cache_get(cache_key):
        log.info("Cache HIT: %s", url)
        return Response(cached, mimetype="application/rss+xml")

    log.info("Fetching: %s", url)
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
    )

    cache_set(cache_key, rss_out)
    return Response(rss_out, mimetype="application/rss+xml")


@app.route("/clean-all")
def clean_all_feeds():
    """
    GET /clean-all?urls=url1,url2,…
    Fetches multiple feeds, merges, sorts by pubDate, deduplicates by guid.
    """
    raw_urls = request.args.get("urls", "").strip()
    if not raw_urls:
        return jsonify(error="Missing 'urls' query parameter."), 400

    urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    if not urls:
        return jsonify(error="No valid URLs provided."), 400

    blocked = [u for u in urls if not is_url_allowed(u)]
    if blocked:
        return jsonify(error="Some URLs are not in the whitelist.", blocked=blocked), 403

    combo_key = hashlib.md5("|".join(sorted(urls)).encode()).hexdigest()
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
    )

    cache_set(combo_key, rss_out)
    return Response(rss_out, mimetype="application/rss+xml")


@app.route("/health")
def health():
    """Health-check — use for Render keep-alive / UptimeRobot."""
    return jsonify(status="ok", cached_feeds=len(_cache)), 200


@app.route("/")
def index():
    """Usage reference."""
    return jsonify(
        endpoints={
            "GET /clean":     "?url=<whitelisted_rss_url>",
            "GET /clean-all": "?urls=<url1>,<url2>,…",
            "GET /health":    "service health check",
        },
        note="Only whitelisted Nitter RSS feeds are accepted.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# WSGI entry-point
#   Production:  gunicorn -c gunicorn.conf.py app:app
#   Development: python app.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)

"""
Flask RSS Cleaning Proxy for Nitter/Twitter feeds
Optimized for Telegram RSS bots.

Endpoints:
  GET /clean?url=<rss_url>          → cleaned single feed
  GET /clean-all?urls=url1,url2,…   → merged + sorted + deduped feed
  GET /health                        → service health check
  GET /                              → usage info

Deploy with:
  gunicorn -w 2 -b 0.0.0.0:8000 app:app
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
# Configuration  (edit these to suit your deployment)
# ─────────────────────────────────────────────────────────────────────────────

MAX_ITEMS    = 10        # items returned per feed
CACHE_TTL    = 300       # seconds (5 minutes)
MAX_TEXT_LEN = 400       # characters for cleaned description
FETCH_TIMEOUT = 10       # seconds for upstream HTTP requests

# ── URL Whitelist ─────────────────────────────────────────────────────────────
# Add exact feed URLs you want to allow here.
ALLOWED_FEED_URLS: set[str] = {
    "https://feeds.bbcnews.com/news/rss.xml",
    # Add more as needed…
}

# Any feed URL whose hostname is in this set is also allowed.

ALLOWED_DOMAINS: set[str] = {
    "feeds.bbcnews.com",
    "rss.cnn.com",
    "feeds.reuters.com",
}

# Image src patterns that identify avatars / icons — skip these
IGNORED_IMAGE_RE = re.compile(
    r"(avatar|profile_images|icon|logo|emoji|favicon|badge|default_profile)",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Simple in-memory cache   { key: (timestamp, payload) }
# ─────────────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, str]] = {}


def cache_get(key: str) -> str | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    _cache.pop(key, None)          # evict stale entry
    return None


def cache_set(key: str, value: str) -> None:
    _cache[key] = (time.time(), value)


# ─────────────────────────────────────────────────────────────────────────────
# Security
# ─────────────────────────────────────────────────────────────────────────────
_PRIVATE_PREFIXES = ("localhost", "127.", "10.", "192.168.", "172.", "0.0.0.0")


def is_url_allowed(url: str) -> bool:
    """
    Return True only if the URL passes all security checks:
    1. Valid http/https scheme
    2. Not pointing at a private/loopback address (SSRF prevention)
    3. Matches the exact whitelist OR a whitelisted domain
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

    # Exact URL match
    if url in ALLOWED_FEED_URLS:
        return True

    # Domain match  (allows any path on that Nitter instance)
    if host in ALLOWED_DOMAINS:
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Feed fetching
# ─────────────────────────────────────────────────────────────────────────────

def fetch_feed(url: str) -> feedparser.FeedParserDict | None:
    """
    Fetch RSS via requests (so we control timeout & headers),
    then parse with feedparser. Returns None on any error.
    """
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
# Content cleaning
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(raw: str) -> str:
    """
    Strip RT prefixes, mentions, hashtags, and URLs.
    Normalise whitespace. Truncate to MAX_TEXT_LEN characters.
    """
    # 1. Remove  "RT by @user:" prefixes
    text = re.sub(r"^RT\s+by\s+@\w+:\s*", "", raw.strip(), flags=re.IGNORECASE)

    # 2. Remove bare URLs
    text = re.sub(r"https?://\S+", "", text)

    # 3. Remove @mentions
    text = re.sub(r"@\w+", "", text)

    # 4. Remove #hashtags
    text = re.sub(r"#\w+", "", text)

    # 5. Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # 6. Soft-truncate at a word boundary
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN].rsplit(" ", 1)[0] + "…"

    return text


# ─────────────────────────────────────────────────────────────────────────────
# HTML / Media extraction  (BeautifulSoup only — no regex on HTML)
# ─────────────────────────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """Plain-text extraction via BeautifulSoup."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def extract_images(html: str, base_url: str = "") -> list[str]:
    """
    Extract ordered, deduplicated image URLs from HTML.

    - Checks both ``src`` and ``data-src`` (lazy-loaded images).
    - Resolves relative paths to absolute URLs.
    - Skips avatar / icon images matched by IGNORED_IMAGE_RE.
    - Preserves carousel/thread order.
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

        # Resolve protocol-relative URLs
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
    Returns a normalised dict for RSS output.
    """
    # ── Raw HTML content ───────────────────────────────────────────────────
    raw_html = ""
    if getattr(entry, "content", None):
        raw_html = entry.content[0].get("value", "")
    if not raw_html:
        raw_html = getattr(entry, "summary", "")

    # ── Text ───────────────────────────────────────────────────────────────
    clean = clean_text(html_to_text(raw_html))

    # ── Images ────────────────────────────────────────────────────────────
    # Parse the base origin so relative paths can be resolved
    parsed = urlparse(feed_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    images: list[str] = extract_images(raw_html, base_url=base_url)

    # Also pull media_content and enclosures from feedparser itself
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

    # ── Metadata ───────────────────────────────────────────────────────────
    title = clean_text(getattr(entry, "title", "") or "")
    link  = getattr(entry, "link", "") or ""
    guid  = getattr(entry, "id", link) or link   # must remain stable

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

    - Descriptions wrapped in CDATA
    - One <enclosure> per image (Telegram reads the first one)
    - Fallback  🖼 [N/T] URL  lines in the description body
    """
    item_blocks: list[str] = []

    for item in items:
        images = item["images"]
        total  = len(images)

        # Description body: clean text + image link list
        desc_parts = [item["text"]]
        if images:
            desc_parts.append("")
            for idx, img_url in enumerate(images, 1):
                desc_parts.append(f"🖼 [{idx}/{total}] {img_url}")
        cdata_body = "\n".join(desc_parts)

        # One <enclosure> per image
        enc_lines = "\n    ".join(
            f'<enclosure url="{_xe(img)}" type="image/jpeg" length="0"/>'
            for img in images
        )

        item_blocks.append(
            f"""  <item>
    <title><![CDATA[{item['title']}]]></title>
    <link>{_xe(item['link'])}</link>
    <guid isPermaLink="false">{_xe(item['guid'])}</guid>
    <pubDate>{item['pub_date']}</pubDate>
    <description><![CDATA[{cdata_body}]]></description>
    {enc_lines}
  </item>"""
        )

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
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

    # ── Cache hit ─────────────────────────────────────────────────────────
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cached := cache_get(cache_key):
        log.info("Cache HIT: %s", url)
        return Response(cached, mimetype="application/rss+xml")

    # ── Fetch & parse ─────────────────────────────────────────────────────
    log.info("Fetching: %s", url)
    feed = fetch_feed(url)
    if feed is None:
        return jsonify(error="Failed to fetch or parse the feed."), 502

    # ── Process + sort ────────────────────────────────────────────────────
    entries   = feed.entries[:MAX_ITEMS]
    processed = [process_entry(e, url) for e in entries]
    processed.sort(key=lambda x: x["pub_date"] or "", reverse=True)

    rss_out = build_rss(
        feed_title=getattr(feed.feed, "title", "Cleaned Feed"),
        feed_link =getattr(feed.feed, "link",  url),
        feed_desc =getattr(feed.feed, "description", "Cleaned RSS feed"),
        items=processed,
    )

    cache_set(cache_key, rss_out)
    return Response(rss_out, mimetype="application/rss+xml")


@app.route("/clean-all")
def clean_all_feeds():
    """
    GET /clean-all?urls=url1,url2,…

    Fetches multiple whitelisted feeds, merges all items,
    sorts by pubDate (newest first), and deduplicates by guid.
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

    # ── Whole-response cache ──────────────────────────────────────────────
    combo_key = hashlib.md5("|".join(sorted(urls)).encode()).hexdigest()
    if cached := cache_get(combo_key):
        return Response(cached, mimetype="application/rss+xml")

    # ── Merge feeds ───────────────────────────────────────────────────────
    all_items: list[dict] = []
    seen_guids: set[str] = set()

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
    """Health-check endpoint — useful for Render / UptimeRobot keep-alive."""
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
#   Production:  gunicorn -w 2 -b 0.0.0.0:8000 app:app
#   Development: python app.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)

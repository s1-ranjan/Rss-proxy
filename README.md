# RSS Cleaning Proxy 🧹

A Flask-based RSS proxy that cleans Nitter (Twitter-alternative) feeds for
Telegram RSS bots. Strips hashtags, mentions, URLs, and RT prefixes; extracts
images from carousels; rebuilds clean RSS 2.0 with proper `<enclosure>` tags.

---

## Features

| Feature | Detail |
|---|---|
| Content cleaning | Removes hashtags, @mentions, RT prefix, URLs |
| Media extraction | All images (incl. `data-src`), carousel order preserved |
| RSS 2.0 output | CDATA descriptions, multiple `<enclosure>` tags |
| In-memory cache | 5-minute TTL, keyed by feed URL |
| Security | URL whitelist + domain whitelist, SSRF prevention |
| Bonus endpoint | `/clean-all` merges, sorts, and deduplicates multiple feeds |

---

## Quick Start

```bash
# 1. Clone / download the project
git clone <your-repo>
cd rss_proxy

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run (development)
python app.py

# 5. Run (production)
gunicorn -c gunicorn.conf.py app:app
```

---

## Configuration

Edit the top of **`app.py`**:

```python
MAX_ITEMS    = 10        # items per feed
CACHE_TTL    = 300       # cache lifetime in seconds
MAX_TEXT_LEN = 400       # max cleaned text length

ALLOWED_FEED_URLS = {    # exact URLs
    "https://nitter.net/elonmusk/rss",
    …
}

ALLOWED_DOMAINS = {      # entire domains
    "nitter.net",
    "nitter.privacydev.net",
    …
}
```

---

## API Reference

### `GET /clean?url=<rss_url>`

Returns a cleaned RSS 2.0 feed for a single Nitter RSS URL.

```
curl "http://localhost:5000/clean?url=https://nitter.net/elonmusk/rss"
```

### `GET /clean-all?urls=<url1>,<url2>,…`

Merges, sorts (newest first), and deduplicates items from multiple feeds.

```
curl "http://localhost:5000/clean-all?urls=https://nitter.net/elonmusk/rss,https://nitter.net/sama/rss"
```

### `GET /health`

Returns `{"status": "ok", "cached_feeds": N}`.  
Use for Render health checks or UptimeRobot keep-alive pings.

---

## Running Tests

```bash
pip install pytest
pytest test_app.py -v
```

---

## Telegram Bot Setup

In your Telegram RSS bot (e.g. @TheFeedReaderBot or similar), set the feed URL
to your proxy endpoint:

```
https://<your-service>.onrender.com/clean?url=https://nitter.net/elonmusk/rss
```

The bot will receive:
- Clean readable text (no hashtags / mentions)
- Images embedded via `<enclosure>` tags
- Stable `<guid>` so no post is shown twice

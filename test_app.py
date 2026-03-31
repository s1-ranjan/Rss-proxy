"""
Unit tests for the RSS Cleaning Proxy.

Run with:  python -m pytest test_app.py -v
"""

import pytest
from app import (
    app,
    clean_text,
    extract_images,
    html_to_text,
    is_url_allowed,
    build_rss,
    process_entry,
)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ─────────────────────────────────────────────
# clean_text
# ─────────────────────────────────────────────

class TestCleanText:
    def test_removes_rt_prefix(self):
        assert "Hello" in clean_text("RT by @user: Hello")

    def test_removes_mention(self):
        result = clean_text("Hey @alice check this out")
        assert "@alice" not in result
        assert "check this out" in result

    def test_removes_hashtag(self):
        result = clean_text("Love this #Python tip")
        assert "#Python" not in result
        assert "Love this" in result

    def test_removes_url(self):
        result = clean_text("Read more at https://example.com/article")
        assert "https://" not in result

    def test_normalises_whitespace(self):
        result = clean_text("Hello   world\n\nfoo")
        assert "  " not in result

    def test_truncates_long_text(self):
        long = "word " * 200
        result = clean_text(long)
        assert len(result) <= 402   # MAX_TEXT_LEN + ellipsis

    def test_empty_string(self):
        assert clean_text("") == ""


# ─────────────────────────────────────────────
# html_to_text
# ─────────────────────────────────────────────

class TestHtmlToText:
    def test_strips_tags(self):
        assert html_to_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_empty(self):
        assert html_to_text("") == ""


# ─────────────────────────────────────────────
# extract_images
# ─────────────────────────────────────────────

class TestExtractImages:
    def test_basic_img(self):
        html = '<img src="https://example.com/photo.jpg">'
        assert extract_images(html) == ["https://example.com/photo.jpg"]

    def test_data_src_preferred(self):
        html = '<img src="https://example.com/low.jpg" data-src="https://example.com/hi.jpg">'
        imgs = extract_images(html)
        assert imgs[0] == "https://example.com/hi.jpg"

    def test_skips_avatar(self):
        html = '<img src="https://pbs.twimg.com/profile_images/avatar.jpg">'
        assert extract_images(html) == []

    def test_deduplicates(self):
        html = (
            '<img src="https://example.com/a.jpg">'
            '<img src="https://example.com/a.jpg">'
        )
        assert len(extract_images(html)) == 1

    def test_relative_to_absolute(self):
        html = '<img src="/media/photo.jpg">'
        imgs = extract_images(html, base_url="https://nitter.net")
        assert imgs[0].startswith("https://nitter.net")

    def test_multiple_images_ordered(self):
        html = (
            '<img src="https://example.com/1.jpg">'
            '<img src="https://example.com/2.jpg">'
            '<img src="https://example.com/3.jpg">'
        )
        imgs = extract_images(html)
        assert imgs == [
            "https://example.com/1.jpg",
            "https://example.com/2.jpg",
            "https://example.com/3.jpg",
        ]

    def test_empty_html(self):
        assert extract_images("") == []


# ─────────────────────────────────────────────
# is_url_allowed
# ─────────────────────────────────────────────

class TestIsUrlAllowed:
    def test_exact_whitelist(self):
        assert is_url_allowed("https://nitter.net/elonmusk/rss")

    def test_allowed_domain(self):
        assert is_url_allowed("https://nitter.cz/someuser/rss")

    def test_blocks_localhost(self):
        assert not is_url_allowed("http://localhost/rss")

    def test_blocks_private_ip(self):
        assert not is_url_allowed("http://192.168.1.1/rss")

    def test_blocks_unknown_domain(self):
        assert not is_url_allowed("https://evil.com/rss")

    def test_blocks_ftp(self):
        assert not is_url_allowed("ftp://nitter.net/rss")

    def test_blocks_empty(self):
        assert not is_url_allowed("")


# ─────────────────────────────────────────────
# build_rss
# ─────────────────────────────────────────────

class TestBuildRss:
    def _sample_item(self):
        return {
            "title":    "Test tweet",
            "link":     "https://nitter.net/user/status/1",
            "guid":     "https://nitter.net/user/status/1",
            "pub_date": "Mon, 01 Jan 2024 12:00:00 +0000",
            "text":     "This is a clean tweet.",
            "images":   [
                "https://pbs.twimg.com/media/photo1.jpg",
                "https://pbs.twimg.com/media/photo2.jpg",
            ],
        }

    def test_valid_xml_structure(self):
        rss = build_rss("Title", "https://example.com", "Desc", [self._sample_item()])
        assert '<?xml version="1.0"' in rss
        assert "<rss version=" in rss
        assert "<channel>" in rss
        assert "</channel>" in rss
        assert "</rss>" in rss

    def test_item_present(self):
        rss = build_rss("T", "L", "D", [self._sample_item()])
        assert "Test tweet" in rss
        assert "clean tweet" in rss

    def test_enclosures_generated(self):
        rss = build_rss("T", "L", "D", [self._sample_item()])
        assert 'type="image/jpeg"' in rss
        assert "photo1.jpg" in rss
        assert "photo2.jpg" in rss

    def test_image_fallback_in_description(self):
        rss = build_rss("T", "L", "D", [self._sample_item()])
        assert "🖼 [1/2]" in rss
        assert "🖼 [2/2]" in rss

    def test_cdata_wrapping(self):
        rss = build_rss("T", "L", "D", [self._sample_item()])
        assert "<![CDATA[" in rss

    def test_empty_items(self):
        rss = build_rss("T", "L", "D", [])
        assert "<item>" not in rss


# ─────────────────────────────────────────────
# Flask endpoints
# ─────────────────────────────────────────────

class TestEndpoints:
    def test_index(self, client):
        rv = client.get("/")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "endpoints" in data

    def test_health(self, client):
        rv = client.get("/health")
        assert rv.status_code == 200
        assert rv.get_json()["status"] == "ok"

    def test_clean_missing_url(self, client):
        rv = client.get("/clean")
        assert rv.status_code == 400

    def test_clean_blocked_url(self, client):
        rv = client.get("/clean?url=https://evil.com/rss")
        assert rv.status_code == 403

    def test_clean_all_missing_urls(self, client):
        rv = client.get("/clean-all")
        assert rv.status_code == 400

    def test_clean_all_blocked(self, client):
        rv = client.get("/clean-all?urls=https://evil.com/rss")
        assert rv.status_code == 403

#!/usr/bin/env python3
"""
agentic_fetcher.py — RSS Newspaper Agentic Fetcher
===================================================

A Claude Agent SDK agent that:
  1. Parses all data/feed*.opml files for RSS/Atom subscriptions
  2. Fetches each feed and filters to articles from the last 3 days
  3. Deduplicates against already-saved article JSON files
  4. Enriches missing metadata (hero image, canonical URL, content) via page scraping
  5. Saves fully-populated Article JSON files under data/articles/<feed-slug>/

File naming convention:
    data/articles/<feed-slug>/<yyyy-mm-dd_HHmmss>-<feed-slug>-<article-title-slug>.json

Re-runs are idempotent: existing articles (matched by GUID) are never duplicated.

Environment (loaded from .env):
    ANTHROPIC_BASE_URL  - LLM endpoint (e.g. http://localhost:1234 for LM Studio)
    ANTHROPIC_AUTH_TOKEN - API auth token
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    create_sdk_mcp_server,
    tool,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Environment & Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

load_dotenv()

# The Claude Agent SDK (and the underlying Claude Code CLI) reads
# ANTHROPIC_API_KEY. The user's .env may set ANTHROPIC_AUTH_TOKEN instead.
if not os.environ.get("ANTHROPIC_API_KEY"):
    _token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if _token:
        os.environ["ANTHROPIC_API_KEY"] = _token

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
ARTICLES_DIR = DATA_DIR / "articles"
ARCHIVE_DIR = DATA_DIR / "archive"
RETIRED_FEEDS_OPML = ARCHIVE_DIR / "retired-feeds.opml"
CUTOFF_DAYS = 3

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RSSNewspaper/1.0; "
        "+https://github.com/rssnewspaper)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Timeout for HTTP requests (seconds)
HTTP_TIMEOUT = 20
PERMANENT_REDIRECT_CODES = {301, 308}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FeedSubscription(BaseModel):
    """A single RSS/Atom feed subscription extracted from an OPML file."""

    name: str = Field(description="Human-readable display name of the feed")
    xml_url: str = Field(description="Full URL of the RSS or Atom feed")
    category: str = Field(
        default="Uncategorized",
        description="Category or folder the feed is grouped under in the OPML",
    )
    source_file: str = Field(
        description="Filename of the OPML file this subscription was parsed from"
    )


class Article(BaseModel):
    """
    Fully-populated article suitable for rendering as a web 'card'.

    Every field has a description so LLM agents understand its purpose.
    All tool parameters that produce an Article MUST populate every field.
    """

    title: str = Field(description="The headline / title of the article")
    canonical_url: str = Field(
        description="The canonical permalink URL of the article"
    )
    feed_name: str = Field(
        description="Human-readable name of the RSS/Atom feed this article belongs to"
    )
    feed_url: str = Field(description="URL of the RSS/Atom feed itself")
    category: str = Field(
        default="Uncategorized",
        description="Category the feed is grouped under (from OPML)",
    )
    author: str = Field(
        default="Unknown", description="Author or byline of the article"
    )
    published_date: str = Field(
        description="ISO 8601 date-time when the article was published"
    )
    summary: str = Field(
        default="", description="A short plain-text summary or excerpt of the article"
    )
    content_html: str = Field(
        default="",
        description="Full or partial article body as HTML (for card rendering)",
    )
    image_url: str = Field(
        default="",
        description="URL of the hero / title image to display on the card",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Keywords, tags, or categories associated with the article",
    )
    language: str = Field(
        default="en", description="ISO 639-1 language code of the article"
    )

    # ── Metadata ──
    fetched_at: str = Field(
        description="ISO 8601 timestamp of when this article data was fetched/saved"
    )
    source_opml: str = Field(
        description="Filename of the OPML file that contained this feed subscription"
    )
    guid: str = Field(
        description=(
            "Globally unique identifier for the article — "
            "the feed's own GUID, or a SHA-256 hash derived from the link+title"
        )
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helper Functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def slugify(text: str, max_length: int = 80) -> str:
    """Convert arbitrary text to a filesystem-safe, lowercase slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_length] if text else "unnamed"


def _to_str(value: Any) -> str:
    """Convert a loosely-typed value to a safe string for typed storage."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    return str(value)


def article_filepath(
    feed_name: str, published_date: str, article_title: str
) -> Path:
    """Compute the canonical file path for an article JSON file.

    Pattern: data/articles/<feed-slug>/<yyyy-mm-dd_HHmmss>-<feed-slug>-<title-slug>.json
    """
    feed_slug = slugify(feed_name)
    title_slug = slugify(article_title, max_length=60)
    try:
        dt = datetime.fromisoformat(published_date)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    date_prefix = dt.strftime("%Y-%m-%d_%H%M%S")
    filename = f"{date_prefix}-{feed_slug}-{title_slug}.json"
    return ARTICLES_DIR / feed_slug / filename


def compute_guid(entry: dict[str, Any], feed_url: str) -> str:
    """Derive a stable GUID for a feed entry.

    Prefers the feed's own <id>/<guid>, falls back to <link>,
    and finally hashes feed_url + title + date.
    """
    if entry.get("id"):
        return str(entry["id"])
    if entry.get("link"):
        return str(entry["link"])
    raw = f"{feed_url}|{entry.get('title', '')}|{entry.get('published', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _parse_entry_date(entry: Any) -> datetime | None:
    """Try to extract a timezone-aware datetime from a feedparser entry."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    # Try ISO 8601 string fields
    for field in ("published", "updated", "created"):
        raw = getattr(entry, field, None)
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                pass
    return None


def _extract_entry_image(entry: Any) -> str:
    """Extract the best image URL from a feedparser entry."""
    # media:content
    if hasattr(entry, "media_content") and entry.media_content:
        for mc in entry.media_content:
            if mc.get("medium") == "image" or mc.get("type", "").startswith(
                "image"
            ):
                url = mc.get("url", "")
                if url:
                    return url
    # media:thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url", "")
        if url:
            return url
    # enclosures
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image"):
                url = enc.get("href", enc.get("url", ""))
                if url:
                    return url
    # image embedded in content/summary
    for attr in ("summary", "description"):
        html = getattr(entry, attr, "")
        if html and "<img" in str(html):
            soup = BeautifulSoup(str(html), "html.parser")
            img = soup.find("img", src=True)
            if img:
                return _to_str(img.get("src", ""))
    return ""


def _extract_entry_content(entry: Any) -> str:
    """Extract article body HTML from a feedparser entry."""
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].get("value", "")
    if hasattr(entry, "summary_detail"):
        return getattr(entry.summary_detail, "value", "")
    return getattr(entry, "summary", "")


def _scrape_page_metadata(url: str) -> dict[str, str]:
    """Fetch a URL and extract OG/meta tags, canonical URL, and hero image.

    Returns a dict with keys: image_url, canonical_url, description, author,
    site_name, body_html (truncated to 5000 chars).
    """
    result: dict[str, str] = {}
    try:
        resp = requests.get(
            url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True
        )
        resp.raise_for_status()
    except Exception:
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Open Graph & meta tags
    for meta in soup.find_all("meta"):
        prop = _to_str(meta.get("property", "") or meta.get("name", ""))
        content = _to_str(meta.get("content", ""))
        if not content:
            continue
        if prop == "og:image":
            result.setdefault("image_url", content)
        elif prop == "og:description":
            result.setdefault("description", content)
        elif prop in ("og:author", "author", "article:author"):
            result.setdefault("author", content)
        elif prop == "og:site_name":
            result.setdefault("site_name", content)
        elif prop == "twitter:image":
            result.setdefault("image_url", content)
        elif prop == "description":
            result.setdefault("description", content)

    # Canonical URL
    link_tag = soup.find("link", rel="canonical")
    if link_tag and link_tag.get("href"):
        result["canonical_url"] = _to_str(link_tag.get("href", ""))

    # Article body (best-effort extraction)
    for selector in (
        "article",
        '[role="main"]',
        ".post-content",
        ".entry-content",
        ".article-body",
        ".article-content",
        "main",
    ):
        el = soup.select_one(selector)
        if el:
            result["body_html"] = str(el)[:5000]
            break

    return result


def _load_existing_guids(feed_slug: str) -> set[str]:
    """Load all known GUIDs from existing article files for a feed directory."""
    feed_dir = ARTICLES_DIR / feed_slug
    guids: set[str] = set()
    if not feed_dir.exists():
        return guids
    for json_path in feed_dir.glob("*.json"):
        try:
            with open(json_path) as f:
                data = json.load(f)
            g = data.get("guid", "")
            if g:
                guids.add(g)
        except Exception:
            continue
    return guids


def _get_opml_path(source_opml: str) -> Path:
    """Resolve an OPML filename to an absolute path under data/."""
    return DATA_DIR / source_opml


def _update_feed_url_in_opml(
    source_opml: str,
    feed_name: str,
    old_feed_url: str,
    new_feed_url: str,
) -> bool:
    """Update a feed xmlUrl in the source OPML file.

    Matches primarily by xmlUrl, and prefers a matching feed title when
    duplicates exist.
    """
    opml_path = _get_opml_path(source_opml)
    if not opml_path.exists():
        return False

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
    except Exception:
        return False

    exact_match: ET.Element | None = None
    fallback_match: ET.Element | None = None
    for outline in root.iter("outline"):
        xml_url = _to_str(outline.get("xmlUrl", ""))
        if xml_url != old_feed_url:
            continue
        if fallback_match is None:
            fallback_match = outline
        if _to_str(outline.get("text", "")) == feed_name:
            exact_match = outline
            break

    target = exact_match if exact_match is not None else fallback_match
    if target is None:
        return False

    target.set("xmlUrl", new_feed_url)
    tree.write(opml_path, encoding="utf-8", xml_declaration=True)
    return True


def _remove_feed_from_opml(source_opml: str, feed_name: str, feed_url: str) -> bool:
    """Remove a feed outline from its source OPML file."""
    opml_path = _get_opml_path(source_opml)
    if not opml_path.exists():
        return False

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
    except Exception:
        return False

    removed = False
    for parent in root.iter():
        for child in list(parent):
            if child.tag != "outline":
                continue
            child_url = _to_str(child.get("xmlUrl", ""))
            child_name = _to_str(child.get("text", ""))
            if child_url == feed_url and (child_name == feed_name or not feed_name):
                parent.remove(child)
                removed = True
                break
        if removed:
            break

    if removed:
        tree.write(opml_path, encoding="utf-8", xml_declaration=True)
    return removed


def _update_feed_category_in_opml(
    source_opml: str,
    feed_name: str,
    feed_url: str,
    new_category: str,
) -> bool:
    """Update a feed category in its source OPML file."""
    opml_path = _get_opml_path(source_opml)
    if not opml_path.exists():
        return False

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
    except Exception:
        return False

    updated = False
    fallback: ET.Element | None = None
    for outline in root.iter("outline"):
        xml_url = _to_str(outline.get("xmlUrl", ""))
        if xml_url != feed_url:
            continue
        if fallback is None:
            fallback = outline
        if _to_str(outline.get("text", "")) == feed_name:
            outline.set("category", new_category)
            updated = True
            break

    if not updated and fallback is not None:
        fallback.set("category", new_category)
        updated = True

    if updated:
        tree.write(opml_path, encoding="utf-8", xml_declaration=True)
    return updated


def _collect_known_categories() -> list[str]:
    """Collect existing categories from feed*.opml files."""
    categories: set[str] = set()
    for opml_path in sorted(glob.glob(str(DATA_DIR / "feed*.opml"))):
        try:
            tree = ET.parse(opml_path)
        except Exception:
            continue
        for outline in tree.getroot().iter("outline"):
            cat = _to_str(outline.get("category", "")).strip()
            if cat and cat.lower() != "uncategorized":
                categories.add(cat)
    return sorted(categories)


def _infer_category_for_feed(
    feed_name: str,
    feed_url: str,
    feed_title: str,
    feed_description: str,
    known_categories: list[str],
) -> str:
    """Infer a best-fit category for a feed when category is missing."""
    haystack = " ".join(
        [
            _to_str(feed_name).lower(),
            _to_str(feed_url).lower(),
            _to_str(feed_title).lower(),
            _to_str(feed_description).lower(),
        ]
    )

    keyword_priority: list[tuple[str, list[str]]] = [
        ("Security", ["security", "infosec", "cyber", "owasp", "malware"]),
        ("Podcasts", ["podcast", "episode", "audio", "libsyn", "megaphone"]),
        ("Science", ["science", "research", "journal", "nature", "space"]),
        ("News", ["news", "breaking", "headlines", "world"]),
        ("Programming", ["python", "developer", "coding", "programming", "software"]),
        ("Tech", ["tech", "technology", "ai", "cloud", "startup", "openai"]),
        ("Weather", ["weather", "forecast", "solar storm"]),
        ("Entertainment", ["entertainment", "movie", "film", "music", "drama"]),
        ("Comedy", ["comedy", "humor", "satire"]),
        ("Intelligence", ["intel", "intelligence", "geopolitics", "espionage"]),
        ("Gaming", ["gaming", "game", "esports"]),
    ]

    known_lookup = {c.lower(): c for c in known_categories}
    for target, keywords in keyword_priority:
        if target.lower() not in known_lookup:
            continue
        if any(keyword in haystack for keyword in keywords):
            return known_lookup[target.lower()]

    if "Tech" in known_categories:
        return "Tech"
    if known_categories:
        return known_categories[0]
    return "Uncategorized"


def _append_retired_feed_to_archive(
    feed_name: str,
    feed_url: str,
    category: str,
    source_opml: str,
    reason: str,
) -> bool:
    """Append a retired feed to data/archive/retired-feeds.opml.

    Skips insertion if a matching xmlUrl already exists.
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    if RETIRED_FEEDS_OPML.exists():
        try:
            tree = ET.parse(RETIRED_FEEDS_OPML)
            root = tree.getroot()
        except Exception:
            return False
    else:
        root = ET.Element("opml", {"version": "2.0"})
        head = ET.SubElement(root, "head")
        ET.SubElement(head, "title").text = "Retired Feed Subscriptions"
        ET.SubElement(root, "body")
        tree = ET.ElementTree(root)

    body = root.find("body")
    if body is None:
        body = ET.SubElement(root, "body")

    for outline in root.iter("outline"):
        if _to_str(outline.get("xmlUrl", "")) == feed_url:
            return True

    retired_at = datetime.now(timezone.utc).isoformat()
    ET.SubElement(
        body,
        "outline",
        {
            "type": "rss",
            "text": feed_name,
            "xmlUrl": feed_url,
            "category": category or "Uncategorized",
            "sourceOpml": source_opml,
            "retiredReason": reason,
            "retiredAt": retired_at,
        },
    )
    tree.write(RETIRED_FEEDS_OPML, encoding="utf-8", xml_declaration=True)
    return True


def _probe_feed_url(feed_url: str) -> tuple[int | None, str]:
    """Probe a feed URL without following redirects.

    Returns (status_code, redirect_location). redirect_location is an absolute
    URL when present.
    """
    try:
        resp = requests.get(
            feed_url,
            headers=HTTP_HEADERS,
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
    except Exception:
        return None, ""

    location = _to_str(resp.headers.get("Location", ""))
    absolute_location = urljoin(feed_url, location) if location else ""
    return resp.status_code, absolute_location


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP Tool Definitions
#  ─ Each tool has:
#      • JSON Schema input_schema with 'description' on every property
#      • All parameters are required (no defaults in tool schemas)
#      • A docstring explaining purpose and return shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool(
    "parse_opml_files",
    (
        "Parse all data/feed*.opml files and return a JSON array of feed "
        "subscriptions. Each subscription includes name, xml_url, category, "
        "and source_file. Use this as the first step to discover feeds."
    ),
    {
        "type": "object",
        "properties": {
            "glob_pattern": {
                "type": "string",
                "description": (
                    "Glob pattern relative to the data/ directory to match "
                    "OPML files, e.g. 'feed*.opml' or '*.opml'"
                ),
            }
        },
        "required": ["glob_pattern"],
    },
)
async def parse_opml_files(args: dict[str, Any]) -> dict[str, Any]:
    """Parse OPML files matching the glob pattern and extract all RSS/Atom
    feed subscriptions.

    Returns JSON with keys:
        opml_files_found  - number of OPML files matched
        total_subscriptions - total valid subscriptions extracted
        subscriptions - list of {name, xml_url, category, source_file}
    """
    pattern = args["glob_pattern"]
    opml_paths = sorted(glob.glob(str(DATA_DIR / pattern)))

    if not opml_paths:
        return _text_result(
            {
                "error": f"No OPML files matched pattern: {pattern}",
                "subscriptions": [],
            }
        )

    subscriptions: list[dict[str, Any]] = []
    for fpath in opml_paths:
        try:
            tree = ET.parse(fpath)
            for outline in tree.getroot().iter("outline"):
                xml_url = outline.get("xmlUrl")
                if xml_url:
                    sub = FeedSubscription(
                        name=outline.get("text", "Unnamed Feed"),
                        xml_url=xml_url,
                        category=outline.get("category", "Uncategorized")
                        or "Uncategorized",
                        source_file=Path(fpath).name,
                    )
                    subscriptions.append(sub.model_dump())
        except Exception as exc:
            subscriptions.append({"error": f"Failed to parse {fpath}: {exc}"})

    return _text_result(
        {
            "opml_files_found": len(opml_paths),
            "total_subscriptions": len(
                [s for s in subscriptions if "xml_url" in s]
            ),
            "subscriptions": subscriptions,
        }
    )


@tool(
    "process_single_feed",
    (
        "Full pipeline for ONE feed: fetch the RSS/Atom feed, filter to the "
        "last 3 days, deduplicate against already-saved articles, auto-enrich "
        "missing image/content by scraping the article page, and save all new "
        "articles as JSON. Returns a summary of what was saved and skipped."
    ),
    {
        "type": "object",
        "properties": {
            "feed_name": {
                "type": "string",
                "description": "Human-readable name of the feed",
            },
            "feed_url": {
                "type": "string",
                "description": "Full URL of the RSS or Atom feed to fetch",
            },
            "category": {
                "type": "string",
                "description": "Category of the feed from the OPML file",
            },
            "source_opml": {
                "type": "string",
                "description": "Filename of the OPML file this feed came from",
            },
        },
        "required": ["feed_name", "feed_url", "category", "source_opml"],
    },
)
async def process_single_feed(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch one RSS/Atom feed, filter to last 3 days, deduplicate, enrich
    missing fields by scraping, and save new articles as JSON.

    Returns JSON with keys:
        feed_name, feed_url, status,
        total_entries, recent_entries, new_saved, duplicates_skipped,
        enriched_count, errors, saved_articles[]
    """
    feed_name: str = args["feed_name"]
    feed_url: str = args["feed_url"]
    category: str = args["category"]
    source_opml: str = args["source_opml"]

    cutoff = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)
    feed_slug = slugify(feed_name)
    now_iso = datetime.now(timezone.utc).isoformat()
    effective_feed_url = feed_url
    opml_updated = False
    category_assigned = False
    category_updated_in_opml = False

    status_code, redirect_url = _probe_feed_url(feed_url)

    if status_code in PERMANENT_REDIRECT_CODES and redirect_url:
        effective_feed_url = redirect_url
        opml_updated = _update_feed_url_in_opml(
            source_opml=source_opml,
            feed_name=feed_name,
            old_feed_url=feed_url,
            new_feed_url=redirect_url,
        )

    if status_code == 404:
        archived = _append_retired_feed_to_archive(
            feed_name=feed_name,
            feed_url=feed_url,
            category=category,
            source_opml=source_opml,
            reason="404 Not Found",
        )
        removed = _remove_feed_from_opml(
            source_opml=source_opml,
            feed_name=feed_name,
            feed_url=feed_url,
        )
        return _text_result(
            {
                "feed_name": feed_name,
                "feed_url": feed_url,
                "status": "retired",
                "reason": "404 Not Found",
                "removed_from_opml": removed,
                "archived_to": str(RETIRED_FEEDS_OPML),
                "archived": archived,
                "new_saved": 0,
            }
        )

    # ── Fetch feed ──────────────────────────────────────────────────────────
    try:
        feed = feedparser.parse(effective_feed_url)
        if feed.bozo and not feed.entries:
            return _text_result(
                {
                    "feed_name": feed_name,
                    "feed_url": effective_feed_url,
                    "status": "error",
                    "error": f"Feed parse error: {feed.bozo_exception}",
                    "feed_url_updated_in_opml": opml_updated,
                    "new_saved": 0,
                }
            )
    except Exception as exc:
        return _text_result(
            {
                "feed_name": feed_name,
                "feed_url": effective_feed_url,
                "status": "error",
                "error": str(exc),
                "feed_url_updated_in_opml": opml_updated,
                "new_saved": 0,
            }
        )

    if (not category.strip()) or (category.lower() == "uncategorized"):
        feed_meta = getattr(feed, "feed", None)
        feed_title = ""
        feed_description = ""
        if isinstance(feed_meta, dict):
            feed_title = _to_str(feed_meta.get("title", ""))
            feed_description = _to_str(
                feed_meta.get("subtitle", feed_meta.get("description", ""))
            )
        elif feed_meta is not None:
            feed_title = _to_str(getattr(feed_meta, "title", ""))
            feed_description = _to_str(
                getattr(feed_meta, "subtitle", getattr(feed_meta, "description", ""))
            )

        known_categories = _collect_known_categories()
        inferred_category = _infer_category_for_feed(
            feed_name=feed_name,
            feed_url=effective_feed_url,
            feed_title=feed_title,
            feed_description=feed_description,
            known_categories=known_categories,
        )
        if inferred_category and inferred_category.lower() != "uncategorized":
            category = inferred_category
            category_assigned = True
            category_updated_in_opml = _update_feed_category_in_opml(
                source_opml=source_opml,
                feed_name=feed_name,
                feed_url=feed_url,
                new_category=category,
            )

    # ── Load existing GUIDs for dedup ───────────────────────────────────────
    existing_guids = _load_existing_guids(feed_slug)

    feed_lang = ""
    feed_meta = getattr(feed, "feed", None)
    if feed_meta is not None:
        if isinstance(feed_meta, dict):
            feed_lang = _to_str(feed_meta.get("language", ""))
        else:
            feed_lang = _to_str(getattr(feed_meta, "language", ""))
    feed_lang = feed_lang or "en"

    saved_articles: list[dict[str, str]] = []
    skipped_dupes = 0
    skipped_old = 0
    enriched_count = 0
    errors: list[str] = []

    for entry in feed.entries:
        # ── Date filter ─────────────────────────────────────────────────────
        pub_date = _parse_entry_date(entry)
        if pub_date and pub_date < cutoff:
            skipped_old += 1
            continue

        # ── GUID & dedup ────────────────────────────────────────────────────
        guid = compute_guid(entry, effective_feed_url)
        if guid in existing_guids:
            skipped_dupes += 1
            continue

        # ── Extract data from feed entry ────────────────────────────────────
        title = getattr(entry, "title", "Untitled") or "Untitled"
        link = getattr(entry, "link", "") or ""
        author = getattr(entry, "author", "Unknown") or "Unknown"
        summary = getattr(entry, "summary", "") or ""
        if len(summary) > 500:
            summary = summary[:497] + "..."
        content_html = _extract_entry_content(entry)
        image_url = _extract_entry_image(entry)
        tags: list[str] = []
        if hasattr(entry, "tags"):
            for tag in getattr(entry, "tags", []):
                if isinstance(tag, dict):
                    term = _to_str(tag.get("term", ""))
                    if term:
                        tags.append(term)

        pub_iso = pub_date.isoformat() if pub_date else now_iso

        # ── Enrich missing fields by scraping the article page ──────────────
        if link and (not image_url or not content_html):
            try:
                page_meta = _scrape_page_metadata(link)
                if not image_url and page_meta.get("image_url"):
                    image_url = page_meta["image_url"]
                    enriched_count += 1
                if not content_html and page_meta.get("body_html"):
                    content_html = page_meta["body_html"]
                    enriched_count += 1
                if author == "Unknown" and page_meta.get("author"):
                    author = page_meta["author"]
                if not summary and page_meta.get("description"):
                    summary = page_meta["description"]
            except Exception:
                pass  # enrichment is best-effort

        # ── Build & validate Article ────────────────────────────────────────
        try:
            article = Article(
                title=title,
                canonical_url=link,
                feed_name=feed_name,
                feed_url=effective_feed_url,
                category=category,
                author=author,
                published_date=pub_iso,
                summary=summary,
                content_html=content_html,
                image_url=image_url,
                tags=tags,
                language=feed_lang,
                fetched_at=now_iso,
                source_opml=source_opml,
                guid=guid,
            )
        except Exception as exc:
            errors.append(f"Validation error for '{title}': {exc}")
            continue

        # ── Save ────────────────────────────────────────────────────────────
        fpath = article_filepath(feed_name, pub_iso, title)
        try:
            fpath.parent.mkdir(parents=True, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(article.model_dump(), f, indent=2, ensure_ascii=False)
            saved_articles.append({"title": title, "path": str(fpath)})
            existing_guids.add(guid)  # prevent intra-batch dupes
        except Exception as exc:
            errors.append(f"Save error for '{title}': {exc}")

    return _text_result(
        {
            "feed_name": feed_name,
            "feed_url": effective_feed_url,
            "feed_url_original": feed_url,
            "feed_url_updated_in_opml": opml_updated,
            "category": category,
            "category_assigned": category_assigned,
            "category_updated_in_opml": category_updated_in_opml,
            "status": "ok",
            "total_entries": len(feed.entries),
            "recent_entries": len(feed.entries) - skipped_old,
            "new_saved": len(saved_articles),
            "duplicates_skipped": skipped_dupes,
            "old_skipped": skipped_old,
            "enriched_fields": enriched_count,
            "error_count": len(errors),
            "errors": errors[:5],  # cap for brevity
            "saved_articles": saved_articles,
        }
    )


@tool(
    "extract_page_metadata",
    (
        "Fetch a web page by URL and extract Open Graph metadata, canonical "
        "URL, hero image, title, description, and article body HTML. Use this "
        "to manually enrich an article that is still missing fields after the "
        "initial feed processing."
    ),
    {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL of the web page to fetch and scrape",
            }
        },
        "required": ["url"],
    },
)
async def extract_page_metadata(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch a web page and extract OG metadata, canonical URL, hero image,
    and article body content.

    Returns JSON with keys:
        url, canonical_url, title, image_url, description, author,
        site_name, body_html_preview, error (if any)
    """
    url = args["url"]
    try:
        resp = requests.get(
            url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True
        )
        resp.raise_for_status()
    except Exception as exc:
        return _text_result({"error": f"Failed to fetch {url}: {exc}"})

    soup = BeautifulSoup(resp.text, "html.parser")

    og: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        prop = _to_str(meta.get("property", "") or meta.get("name", ""))
        content = _to_str(meta.get("content", ""))
        if prop.startswith("og:") and content:
            og[prop[3:]] = content
        elif prop == "twitter:image" and content:
            og.setdefault("image", content)
        elif prop == "description" and content:
            og.setdefault("description", content)
        elif prop in ("author", "article:author") and content:
            og.setdefault("author", content)

    canonical = ""
    link_tag = soup.find("link", rel="canonical")
    if link_tag and link_tag.get("href"):
        canonical = _to_str(link_tag.get("href", ""))

    title = og.get("title", "")
    if not title:
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)

    body_html = ""
    for sel in (
        "article",
        '[role="main"]',
        ".post-content",
        ".entry-content",
        ".article-body",
        "main",
    ):
        el = soup.select_one(sel)
        if el:
            body_html = str(el)[:3000]
            break

    return _text_result(
        {
            "url": url,
            "final_url": resp.url,
            "canonical_url": canonical or resp.url,
            "title": title,
            "image_url": og.get("image", ""),
            "description": og.get("description", ""),
            "author": og.get("author", ""),
            "site_name": og.get("site_name", ""),
            "body_html_preview": body_html[:2000],
        }
    )


@tool(
    "save_article",
    (
        "Validate article data against the Article schema and save it as a "
        "JSON file at the canonical path under data/articles/<feed-slug>/. "
        "Use this to save or overwrite an article after manual enrichment."
    ),
    {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Article headline / title",
            },
            "canonical_url": {
                "type": "string",
                "description": "Canonical permalink URL of the article",
            },
            "feed_name": {
                "type": "string",
                "description": "Human-readable name of the RSS/Atom feed",
            },
            "feed_url": {
                "type": "string",
                "description": "URL of the RSS/Atom feed",
            },
            "category": {
                "type": "string",
                "description": "Feed category from the OPML",
            },
            "author": {
                "type": "string",
                "description": "Author or byline of the article",
            },
            "published_date": {
                "type": "string",
                "description": "ISO 8601 publication date-time",
            },
            "summary": {
                "type": "string",
                "description": "Short plain-text summary or excerpt",
            },
            "content_html": {
                "type": "string",
                "description": "Full or partial article body as HTML",
            },
            "image_url": {
                "type": "string",
                "description": "Hero/title image URL for card display",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keywords or tags for the article",
            },
            "language": {
                "type": "string",
                "description": "ISO 639-1 language code (e.g. 'en')",
            },
            "fetched_at": {
                "type": "string",
                "description": "ISO 8601 timestamp when article data was fetched",
            },
            "source_opml": {
                "type": "string",
                "description": "OPML filename this feed came from",
            },
            "guid": {
                "type": "string",
                "description": "Globally unique identifier for the article",
            },
        },
        "required": [
            "title",
            "canonical_url",
            "feed_name",
            "feed_url",
            "category",
            "author",
            "published_date",
            "summary",
            "content_html",
            "image_url",
            "tags",
            "language",
            "fetched_at",
            "source_opml",
            "guid",
        ],
    },
)
async def save_article(args: dict[str, Any]) -> dict[str, Any]:
    """Validate the supplied article data against the Article Pydantic model
    and persist it to the correct JSON file path.

    Returns JSON with keys: saved (bool), path, feed_name, title, error (if any)
    """
    try:
        article = Article(**args)
    except Exception as exc:
        return _text_result(
            {"error": f"Validation failed: {exc}", "saved": False}
        )

    fpath = article_filepath(
        article.feed_name, article.published_date, article.title
    )
    fpath.parent.mkdir(parents=True, exist_ok=True)

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(article.model_dump(), f, indent=2, ensure_ascii=False)

    return _text_result(
        {
            "saved": True,
            "path": str(fpath),
            "feed_name": article.feed_name,
            "title": article.title,
        }
    )


@tool(
    "list_feed_articles",
    (
        "List all existing article JSON files for a given feed name. "
        "Returns filenames, titles, GUIDs, and published dates. Use this "
        "to inspect what has already been saved for a feed."
    ),
    {
        "type": "object",
        "properties": {
            "feed_name": {
                "type": "string",
                "description": "Human-readable name of the feed to list articles for",
            }
        },
        "required": ["feed_name"],
    },
)
async def list_feed_articles(args: dict[str, Any]) -> dict[str, Any]:
    """List saved article JSON files for a given feed.

    Returns JSON with keys:
        feed_name, feed_dir, exists, article_count, articles[]
    """
    feed_name = args["feed_name"]
    feed_dir = ARTICLES_DIR / slugify(feed_name)

    if not feed_dir.exists():
        return _text_result(
            {
                "feed_name": feed_name,
                "exists": False,
                "article_count": 0,
                "articles": [],
            }
        )

    articles: list[dict[str, Any]] = []
    for json_file in sorted(feed_dir.glob("*.json")):
        try:
            with open(json_file) as f:
                data = json.load(f)
            articles.append(
                {
                    "filename": json_file.name,
                    "title": data.get("title", ""),
                    "guid": data.get("guid", ""),
                    "published_date": data.get("published_date", ""),
                    "has_image": bool(data.get("image_url")),
                    "has_content": bool(data.get("content_html")),
                }
            )
        except Exception:
            articles.append({"filename": json_file.name, "error": "unreadable"})

    return _text_result(
        {
            "feed_name": feed_name,
            "exists": True,
            "article_count": len(articles),
            "articles": articles,
        }
    )


@tool(
    "get_run_statistics",
    (
        "Get aggregate statistics about the current article archive: total "
        "feeds with saved articles, total article count, breakdown by feed, "
        "and articles missing key fields (image, content). Use at the end "
        "of a run for a summary report."
    ),
    {
        "type": "object",
        "properties": {
            "include_per_feed_detail": {
                "type": "boolean",
                "description": (
                    "If true, include per-feed article counts. "
                    "If false, only return aggregate totals."
                ),
            }
        },
        "required": ["include_per_feed_detail"],
    },
)
async def get_run_statistics(args: dict[str, Any]) -> dict[str, Any]:
    """Compute and return statistics about the article archive.

    Returns JSON with keys:
        total_feeds, total_articles, missing_image_count,
        missing_content_count, per_feed (optional)
    """
    include_detail = args["include_per_feed_detail"]

    if not ARTICLES_DIR.exists():
        return _text_result(
            {"total_feeds": 0, "total_articles": 0, "per_feed": []}
        )

    per_feed: list[dict[str, Any]] = []
    total_articles = 0
    missing_image = 0
    missing_content = 0

    for feed_dir in sorted(ARTICLES_DIR.iterdir()):
        if not feed_dir.is_dir():
            continue
        count = 0
        for json_file in feed_dir.glob("*.json"):
            count += 1
            try:
                with open(json_file) as f:
                    data = json.load(f)
                if not data.get("image_url"):
                    missing_image += 1
                if not data.get("content_html"):
                    missing_content += 1
            except Exception:
                pass
        total_articles += count
        if include_detail:
            per_feed.append({"feed_slug": feed_dir.name, "article_count": count})

    result: dict[str, Any] = {
        "total_feeds": len(
            [d for d in ARTICLES_DIR.iterdir() if d.is_dir()]
        ),
        "total_articles": total_articles,
        "missing_image_count": missing_image,
        "missing_content_count": missing_content,
    }
    if include_detail:
        result["per_feed"] = per_feed
    return _text_result(result)


# ── Tool response helper ────────────────────────────────────────────────────


def _text_result(data: Any) -> dict[str, Any]:
    """Wrap a JSON-serialisable value in the MCP tool response format."""
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2)}]
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP Server Registration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

rss_server = create_sdk_mcp_server(
    name="rss_tools",
    version="1.0.0",
    tools=[
        parse_opml_files,
        process_single_feed,
        extract_page_metadata,
        save_article,
        list_feed_articles,
        get_run_statistics,
    ],
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent System Prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SYSTEM_PROMPT = """\
You are a meticulous RSS/Atom feed article fetcher agent.  Your mission is to
build a complete, deduplicated archive of recent articles as JSON files so they
can be rendered as "cards" on a newspaper-style web page.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKFLOW — Follow these steps in strict, numbered order:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### Step 1: DISCOVERY
Call `mcp__rss_tools__parse_opml_files` with glob_pattern="feed*.opml" to get
every feed subscription.  Note the total count.

### Step 2: PROCESS EACH FEED
For **every** subscription returned in Step 1, call
`mcp__rss_tools__process_single_feed` with the feed's name, url, category,
and source_opml.  This single call will:
  • fetch the feed
  • filter to the last 3 days
  • skip duplicates already on disk
  • auto-enrich missing image/content by scraping the article page
  • save new articles as JSON

Process feeds one by one.  If a feed errors, note it and continue to the next.

### Step 3: SUMMARY
After ALL feeds are processed, call `mcp__rss_tools__get_run_statistics` with
include_per_feed_detail=true and present a clear summary:
  • Total feeds attempted
  • Total new articles saved
  • Total duplicates skipped
  • Feeds that failed (with reason)
  • Articles still missing hero image or body content

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Do NOT skip any feed.  Process every single one.
• If a feed returns 0 recent articles, that is fine — just move on.
• Be efficient: each feed needs only ONE tool call (process_single_feed).
• Keep going until every feed in the OPML has been processed.
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main Entry Point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def main() -> None:
    """Run the agentic RSS feed fetcher workflow."""

    print("=" * 70)
    print("  RSS Newspaper — Agentic Fetcher")
    print("=" * 70)
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Data dir:     {DATA_DIR}")
    print(f"  Articles dir: {ARTICLES_DIR}")
    print(f"  Cutoff:       last {CUTOFF_DAYS} days")
    print(f"  LLM endpoint: {os.environ.get('ANTHROPIC_BASE_URL', '(default)')}")
    print("=" * 70)
    print()

    # Ensure output directory exists
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    # ── Build options ───────────────────────────────────────────────────────
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"rss_tools": rss_server},
        allowed_tools=[
            # Custom MCP tools
            "mcp__rss_tools__parse_opml_files",
            "mcp__rss_tools__process_single_feed",
            "mcp__rss_tools__extract_page_metadata",
            "mcp__rss_tools__save_article",
            "mcp__rss_tools__list_feed_articles",
            "mcp__rss_tools__get_run_statistics",
            # Built-in tools for ad-hoc enrichment / searching
            "WebSearch",
            "WebFetch",
        ],
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        max_turns=500,  # ~120 feeds × ~2 turns each + overhead
    )

    # ── Prompt ──────────────────────────────────────────────────────────────
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = (
        f"Today is {today}.  Begin the RSS article fetching workflow now.  "
        f"Process ALL feeds found in the OPML files under data/.  "
        f"Fetch articles from the last {CUTOFF_DAYS} days, deduplicate, "
        f"enrich where possible, and save them all as JSON files."
    )

    # ── Run agent ───────────────────────────────────────────────────────────
    feeds_processed = 0

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async for message in client.receive_messages():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"\n[Agent] {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        tool_name_short = block.name.split("__")[-1]
                        input_preview = json.dumps(block.input)
                        if len(input_preview) > 120:
                            input_preview = input_preview[:117] + "..."
                        print(f"  → {tool_name_short}({input_preview})")
                        if tool_name_short == "process_single_feed":
                            feeds_processed += 1
                            print(
                                f"    [feed #{feeds_processed}: "
                                f"{block.input.get('feed_name', '?')}]"
                            )
                    elif isinstance(block, ToolResultBlock):
                        # Summarise tool results concisely
                        if block.content and isinstance(block.content, str):
                            try:
                                data = json.loads(block.content)
                                if "new_saved" in data:
                                    print(
                                        f"    ✓ saved={data['new_saved']}  "
                                        f"dupes={data.get('duplicates_skipped', 0)}  "
                                        f"status={data.get('status', '?')}"
                                    )
                            except (json.JSONDecodeError, TypeError):
                                pass

            elif isinstance(message, ResultMessage):
                print()
                print("=" * 70)
                print(f"  Workflow finished: {message.subtype}")
                print(f"  Duration:  {message.duration_ms / 1000:.1f}s")
                print(f"  Turns:     {message.num_turns}")
                if message.total_cost_usd is not None:
                    print(f"  Cost:      ${message.total_cost_usd:.4f}")
                if message.result:
                    # Print the final summary (truncate if very long)
                    result_text = message.result
                    if len(result_text) > 2000:
                        result_text = result_text[:2000] + "\n... (truncated)"
                    print(f"\n{result_text}")
                print("=" * 70)
                break

    print(f"\nTotal feeds dispatched: {feeds_processed}")
    print(f"Article files at: {ARTICLES_DIR}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())

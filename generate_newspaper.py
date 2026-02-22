#!/usr/bin/env python3
"""
generate_newspaper.py — RSS Newspaper HTML Generator

Reads all article JSON files from data/articles/**, renders them into a
single self-contained HTML newspaper using Jinja2 templates, and writes
the output to:

    data/newspapers/yyyy-mm-dd_HHmm/newspaper-yyyy-mm-dd_HHmm.html

Usage:
    python generate_newspaper.py                     # all articles
    python generate_newspaper.py --days 3            # last N days only
    python generate_newspaper.py --output-dir /tmp   # custom output root
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PROJECT_ROOT = Path(__file__).resolve().parent
ARTICLES_DIR = PROJECT_ROOT / "data" / "articles"
NEWSPAPERS_DIR = PROJECT_ROOT / "data" / "newspapers"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
TEMPLATE_NAME = "newspaper.html.j2"

# Category → SVG icon-id mapping
CATEGORY_ICONS: dict[str, str] = {
    "Tech": "globe",
    "Security": "shield",
    "Science": "beaker",
    "Space": "star",
    "Comedy": "smile",
    "Uncategorized": "newspaper",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("newspaper")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Article Loading & Processing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def load_articles(articles_dir: Path, max_age_days: int | None = None) -> list[dict[str, Any]]:
    """Recursively load all article JSON files from the articles directory.

    Args:
        articles_dir: Root directory containing feed subdirectories with JSON files.
        max_age_days: If set, only include articles published within this many days.

    Returns:
        List of article dicts, sorted by published_date descending (newest first).
    """
    articles: list[dict[str, Any]] = []
    cutoff: datetime | None = None

    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    if not articles_dir.is_dir():
        log.warning("Articles directory does not exist: %s", articles_dir)
        return articles

    json_files = sorted(articles_dir.rglob("*.json"))
    log.info("Found %d JSON files in %s", len(json_files), articles_dir)

    for json_path in json_files:
        try:
            raw = json_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Skipping %s: %s", json_path.name, exc)
            continue

        # Parse published date
        pub_dt = _parse_datetime(data.get("published_date", ""))
        if pub_dt and cutoff and pub_dt < cutoff:
            continue

        # Enrich for template
        data["_pub_dt"] = pub_dt
        data["published_display"] = _format_date(pub_dt) if pub_dt else "Unknown date"
        data["summary_plain"] = _html_to_plain(data.get("summary", ""))
        data["content_html_safe"] = _sanitize_for_modal(data.get("content_html", ""))

        # Ensure defaults
        data.setdefault("category", "Uncategorized")
        data.setdefault("author", "Unknown")
        data.setdefault("content_kind", "article")
        data.setdefault("image_url", "")
        data.setdefault("tags", [])
        data.setdefault("media_assets", [])
        data.setdefault("canonical_url", "")
        data.setdefault("feed_name", "Unknown Feed")

        # Derive media flags from media_assets for filtering
        asset_kinds = {a.get("kind", "") for a in data.get("media_assets", [])}
        data["_has_audio"] = "audio" in asset_kinds
        data["_has_video"] = "video" in asset_kinds
        data["_has_youtube"] = "youtube" in asset_kinds
        data["_media_flags"] = ",".join(
            f for f in ("audio", "video", "youtube") if f in asset_kinds
        )

        articles.append(data)

    # Sort newest-first
    articles.sort(key=lambda a: a.get("_pub_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # Assign sequential index for JS article lookup
    for idx, article in enumerate(articles):
        article["_idx"] = idx

    log.info("Loaded %d articles (after date filter)", len(articles))
    return articles


def _parse_datetime(date_str: str) -> datetime | None:
    """Parse an ISO 8601 date string into a timezone-aware datetime."""
    if not date_str:
        return None

    # Try standard ISO parse first
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass

    # Fallback: strip and try common patterns
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    return None


def _format_date(dt: datetime) -> str:
    """Format a datetime for newspaper display."""
    return dt.strftime("%B %d, %Y at %H:%M")


def _html_to_plain(raw_html: str) -> str:
    """Strip HTML tags to produce plain text for summaries."""
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True)


def _sanitize_for_modal(raw_html: str) -> str:
    """Light sanitization of content HTML for display in the article modal.

    Removes script/style/form tags but preserves semantic markup, links, and images.
    """
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    # Remove dangerous tags
    for tag_name in ("script", "style", "form", "iframe", "embed", "object", "applet"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remove event handler attributes
    for tag in soup.find_all(True):
        attrs_to_remove = [attr for attr in tag.attrs if attr.startswith("on")]
        for attr in attrs_to_remove:
            del tag[attr]

    return str(soup)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data Organization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def group_by_category(articles: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group articles by their category, maintaining sort order within each group.

    Categories are sorted with a priority order, then alphabetically.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        cat = article.get("category", "Uncategorized")
        groups.setdefault(cat, []).append(article)

    # Sort categories: prioritize known ones, then alphabetic
    priority = ["Tech", "Security", "Science", "Space", "Comedy"]
    sorted_groups: dict[str, list[dict[str, Any]]] = {}

    for cat in priority:
        if cat in groups:
            sorted_groups[cat] = groups.pop(cat)

    for cat in sorted(groups.keys()):
        sorted_groups[cat] = groups[cat]

    return sorted_groups


def extract_unique_feeds(articles: list[dict[str, Any]]) -> list[str]:
    """Return sorted list of unique feed names."""
    return sorted({a.get("feed_name", "Unknown") for a in articles})


def extract_unique_categories(articles: list[dict[str, Any]]) -> list[str]:
    """Return sorted list of unique category names."""
    priority = ["Tech", "Security", "Science", "Space", "Comedy"]
    cats = {a.get("category", "Uncategorized") for a in articles}
    result = [c for c in priority if c in cats]
    result.extend(sorted(c for c in cats if c not in priority))
    return result


def extract_top_tags(articles: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    """Return the most-used tags with their counts, sorted by frequency."""
    from collections import Counter
    tag_counter: Counter[str] = Counter()
    for a in articles:
        for tag in a.get("tags", []):
            tag_counter[tag.strip()] += 1
    # Remove empty strings
    tag_counter.pop("", None)
    return [
        {"name": tag, "count": count}
        for tag, count in tag_counter.most_common(limit)
    ]


def compute_content_counts(articles: list[dict[str, Any]]) -> dict[str, int]:
    """Count articles by content_kind and media type."""
    counts: dict[str, int] = {
        "article": 0, "podcast": 0, "video": 0,
        "has_audio": 0, "has_video": 0, "has_youtube": 0,
    }
    for a in articles:
        kind = a.get("content_kind", "article")
        if kind in counts:
            counts[kind] += 1
        counts["has_audio"] += int(a.get("_has_audio", False))
        counts["has_video"] += int(a.get("_has_video", False))
        counts["has_youtube"] += int(a.get("_has_youtube", False))
    return counts


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JSON Serialization for Inline Script
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_articles_json(articles: list[dict[str, Any]]) -> str:
    """Build a JS-safe JSON blob for the inline ARTICLES array.

    Only includes fields needed by the modal JavaScript.
    Indexed by article _idx for O(1) lookup.
    """
    js_articles: dict[int, dict] = {}

    for article in articles:
        idx = article["_idx"]
        js_articles[idx] = {
            "title": article.get("title", ""),
            "canonical_url": article.get("canonical_url", ""),
            "feed_name": article.get("feed_name", ""),
            "category": article.get("category", ""),
            "author": article.get("author", ""),
            "published_display": article.get("published_display", ""),
            "summary": article.get("summary_plain", ""),
            "content_html": article.get("content_html_safe", ""),
            "image_url": article.get("image_url", ""),
            "tags": article.get("tags", []),
            "content_kind": article.get("content_kind", "article"),
            "media_assets": [
                {
                    "kind": ma.get("kind", ""),
                    "url": ma.get("url", ""),
                    "title": ma.get("title", ""),
                    "duration_seconds": ma.get("duration_seconds"),
                }
                for ma in article.get("media_assets", [])
            ],
        }

    # Serialize — ensure no </script> injection
    raw = json.dumps(js_articles, ensure_ascii=False, separators=(",", ":"))
    # Escape </script> to prevent premature tag closing
    raw = raw.replace("</script>", "<\\/script>")
    return raw


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTML Generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_newspaper(articles: list[dict[str, Any]], template_dir: Path) -> str:
    """Render the full newspaper HTML from articles and the Jinja2 template.

    Args:
        articles: Enriched article dicts (with _idx, published_display, etc.).
        template_dir: Path to the templates/ directory.

    Returns:
        Complete HTML string.
    """
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    template = env.get_template(TEMPLATE_NAME)

    now = datetime.now(timezone.utc)
    categories_articles = group_by_category(articles)
    categories = extract_unique_categories(articles)
    feeds = extract_unique_feeds(articles)

    top_tags = extract_top_tags(articles, limit=25)
    content_counts = compute_content_counts(articles)

    context = {
        "generation_date_display": now.strftime("%A, %B %d, %Y"),
        "generation_timestamp": now.strftime("%Y-%m-%d %H:%M UTC"),
        "total_articles": len(articles),
        "total_feeds": len(feeds),
        "total_categories": len(categories),
        "categories": categories,
        "feeds": feeds,
        "categories_articles": categories_articles,
        "category_icons": CATEGORY_ICONS,
        "top_tags": top_tags,
        "content_counts": content_counts,
        "articles_json": build_articles_json(articles),
    }

    log.info(
        "Rendering newspaper: %d articles, %d feeds, %d categories",
        len(articles),
        len(feeds),
        len(categories),
    )

    return template.render(**context)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Output
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def write_newspaper(html_content: str, output_root: Path | None = None) -> Path:
    """Write the rendered HTML to the newspaper output directory.

    Creates:
        <output_root>/yyyy-mm-dd_HHmm/newspaper-yyyy-mm-dd_HHmm.html

    Args:
        html_content: The rendered HTML string.
        output_root: Root directory for newspapers (default: data/newspapers/).

    Returns:
        Path to the written HTML file.
    """
    if output_root is None:
        output_root = NEWSPAPERS_DIR

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    out_dir = output_root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    # Create images sub-directory for potential local image caching
    (out_dir / "images").mkdir(exist_ok=True)

    filename = f"newspaper-{stamp}.html"
    out_path = out_dir / filename

    out_path.write_text(html_content, encoding="utf-8")
    file_size_mb = out_path.stat().st_size / (1024 * 1024)

    log.info("Newspaper written: %s (%.2f MB)", out_path, file_size_mb)
    return out_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an HTML newspaper from RSS article JSON data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Only include articles published within the last N days (default: all)",
    )
    parser.add_argument(
        "--articles-dir",
        type=Path,
        default=ARTICLES_DIR,
        metavar="DIR",
        help=f"Path to articles directory (default: {ARTICLES_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=f"Root output directory for newspapers (default: {NEWSPAPERS_DIR})",
    )
    parser.add_argument(
        "--template-dir",
        type=Path,
        default=TEMPLATE_DIR,
        metavar="DIR",
        help=f"Path to templates directory (default: {TEMPLATE_DIR})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    log.info("=" * 60)
    log.info("  RSS Newspaper Generator")
    log.info("=" * 60)

    # Load articles
    articles = load_articles(args.articles_dir, max_age_days=args.days)

    if not articles:
        log.error("No articles found. Nothing to generate.")
        sys.exit(1)

    # Render
    html_content = render_newspaper(articles, args.template_dir)

    # Write
    out_path = write_newspaper(html_content, args.output_dir)

    log.info("-" * 60)
    log.info("  Done! Open in your browser:")
    log.info("  file://%s", out_path)
    log.info("-" * 60)


if __name__ == "__main__":
    main()

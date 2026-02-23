# RSS Newspaper Instructions

## Project Overview

RSS Newspaper is a two-stage pipeline:

1. **`agentic_fetcher.py`** — A per-feed agent pipeline: parses OPML in pure Python, spawns a fresh Claude Agent SDK agent for each feed (keeping context small for consistent token speed), enriches missing metadata via page scraping, sanitizes HTML, detects media assets (MP3/MP4/YouTube), computes statistics in pure Python, and saves fully-populated Article JSON files under `data/articles/<feed-slug>/`.

2. **`generate_newspaper.py`** — A Jinja2-based HTML generator that reads all article JSON files and renders a single self-contained HTML newspaper with an old-time newspaper aesthetic (desaturated palette, serif typography, inline SVG icons). Output goes to `data/newspapers/yyyy-mm-dd_HHmm/`.

## Key Files

| File | Purpose |
|---|---|
| `agentic_fetcher.py` | Per-feed agentic RSS fetcher (~3000 lines). Uses Claude Agent SDK + in-process MCP tools. |
| `generate_newspaper.py` | Newspaper HTML generator (~520 lines). Loads JSON, enriches for display, renders Jinja2. |
| `templates/newspaper.html.j2` | Self-contained HTML template (~1900 lines). All CSS, JS, SVG icons inline. |
| `data/feed*.opml` | OPML subscription files (input for the fetcher). |
| `data/articles/` | Article JSON output directory (one subdirectory per feed). |
| `data/newspapers/` | Generated newspaper output directory (timestamped subdirectories). |
| `example.env` | Environment variable reference (copy to `.env`). |
| `requirements.txt` | Python dependencies. |

## Architecture

### Article JSON Schema (Pydantic `Article` model)

18 fields: `title`, `canonical_url`, `feed_name`, `feed_url`, `category`, `author`, `published_date`, `summary`, `content_html`, `image_url`, `tags`, `language`, `fetched_at`, `source_opml`, `guid`, `content_kind` (article/podcast/video), `media_assets[]`.

`media_assets` entries: `kind` (audio/video/youtube), `url`, `mime_type`, `title`, `duration_seconds`.

### Newspaper Template

- **CSS**: CSS custom properties throughout (`--paper`, `--ink`, `--accent`, etc.). Base font size is `16px` (rem-based scaling). Responsive grid layout.
- **HTML structure**: Masthead → sticky toolbar (search, filter toggle, view toggle, count) → collapsible filter panel (categories, content types, media filters, tag cloud) → category sections with card grids → article modal → back-to-top.
- **JS**: Self-contained IIFE. Multi-dimensional filtering (category × content kind × media type × tag × text search). Filter panel is collapsible with active-filter hints. Modal renders full article content including media asset links.
- **All inline**: No external CSS/JS/font dependencies. SVG icon sprite defined in `<defs>` with 34 icon symbols. Only external resources are article images.

### Generator Enrichment

`generate_newspaper.py` computes per-article display fields before passing to the template:
- `_idx`: sequential index for JS article lookup
- `published_display`: human-readable date string
- `summary_plain`: HTML-stripped summary for safe `data-searchable` attributes
- `_has_audio`, `_has_video`, `_has_youtube`: boolean media flags
- `_media_flags`: space-separated media type string for `data-media` attribute

Template context also includes: `top_tags` (top 25 tags with counts), `content_counts` (article/podcast/video/media totals), `category_icons` mapping (22 categories → SVG icon-id).

## Development Instructions

- Always activate the venv: `source .venv/bin/activate`
- Jinja2 environment uses `autoescape=True` — mark trusted HTML with `|safe`
- The template is self-contained; test by opening the output `.html` file directly in a browser
- Regenerate the newspaper: `python generate_newspaper.py`
- Run the fetcher: `python agentic_fetcher.py`

## Category Handling

### OPML Categories

The OPML file defines categories via `category` attributes on feed outlines. The fetcher preserves OPML categories as-is. When a feed has no category (empty or `"Uncategorized"`), `_infer_category_for_feed()` uses keyword matching against feed name, URL, title, and description.

### Category Inference (`_infer_category_for_feed`)

- **Specificity-first ordering**: Niche categories (Cryptography, Security) are matched before broad ones (News, Tech) to prevent false positives.
- **OPML-gated**: Only categories that exist in the current OPML are considered as inference targets.
- **Conservative fallback**: Unmatched feeds default to "News" (if present) → "Tech" → "Uncategorized", rather than guessing a niche category.
- When adding new keyword rules, place them above broader categories in `keyword_priority`.

### Generator Category Support

`CATEGORY_ICONS` maps 22 category names to SVG icon IDs. Unknown categories fall back to `"newspaper"` icon. Category sections and filter buttons are sorted dynamically by article count (descending), with "Uncategorized" always last. No hardcoded priority lists — any new OPML category is handled automatically.

### Adding a New Category

1. Add it to your OPML file as a `category` attribute.
2. Add a `("CategoryName", ["keyword1", "keyword2"])` tuple to `keyword_priority` in `_infer_category_for_feed()` at the appropriate specificity level.
3. Add an SVG `<symbol id="icon-youricon">` to the template `<defs>` block.
4. Add `"CategoryName": "youricon"` to `CATEGORY_ICONS` in `generate_newspaper.py`.

## Conventions

- Tool function parameters in `agentic_fetcher.py` must be non-optional (no defaults). Use `Field()` descriptions and docstrings to guide the agent.
- All Pydantic models use `Field()` with descriptions.
- `content_html` is sanitized before saving (no scripts, styles, iframes, forms; HTTPS-only links/images).
- Articles are deduplicated by GUID. Re-runs never create duplicate files.
- The fetcher supports chunked processing via `chunk_page`/`chunk_size` to stay within output token limits.
- The fetcher uses per-feed agent spawning: OPML parsing and statistics are pure Python; only feed processing uses the LLM. Each feed gets a fresh agent conversation to avoid context growth and progressive slowdown.

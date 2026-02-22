# RSS Newspaper

A two-stage pipeline that turns RSS/Atom subscriptions into a self-contained HTML newspaper with an old-time broadsheet aesthetic.

1. **Agentic Fetcher** (`agentic_fetcher.py`) — A [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) agent that parses OPML subscription lists, fetches recent articles, enriches missing metadata via page scraping, sanitizes HTML, detects media assets (MP3/MP4/YouTube), and saves JSON files ready for article/podcast/video cards.

2. **Newspaper Generator** (`generate_newspaper.py`) — Reads all article JSON files and renders a single self-contained HTML newspaper using Jinja2 — complete with search, multi-dimensional filtering, tag cloud, grid/headline views, and a full-article modal.

## How It Works

### Stage 1 — Agentic Fetcher

The agent follows a disciplined three-step workflow:

1. **Discovery** — Parses all `data/feed*.opml` files to extract every RSS/Atom subscription.
2. **Process Each Feed** — For each subscription, fetches the feed, filters to the last 3 days, deduplicates against already-saved articles (by GUID), auto-enriches missing images and content by scraping the article page, detects media assets (MP3/MP4/YouTube), sanitizes HTML, and saves new items as JSON.
3. **Summary** — Reports totals: feeds attempted, articles saved, duplicates skipped, failures, and articles still missing key fields.

### Article JSON Schema

Every item is validated against a Pydantic `Article` model with 18 fields:

| Field | Description |
|---|---|
| `title` | Headline of the article |
| `canonical_url` | Permalink URL |
| `feed_name` | Name of the source feed |
| `feed_url` | URL of the RSS/Atom feed |
| `category` | OPML category |
| `author` | Author or byline |
| `published_date` | ISO 8601 publication timestamp |
| `summary` | Short plain-text excerpt |
| `content_html` | Article body as HTML |
| `image_url` | Hero/title image URL |
| `tags` | Keywords and tags |
| `language` | ISO 639-1 language code |
| `fetched_at` | When the data was fetched |
| `source_opml` | Which OPML file contained the feed |
| `guid` | Globally unique identifier |
| `content_kind` | Primary type: `article`, `podcast`, or `video` |
| `media_assets` | Structured media list (mp3/mp4/youtube and metadata) |

`media_assets` entries use this shape:

| Field | Description |
|---|---|
| `kind` | `audio`, `video`, or `youtube` |
| `url` | Absolute `https://` media URL |
| `mime_type` | MIME type when present (`audio/mpeg`, `video/mp4`, etc.) |
| `title` | Optional media label (usually episode title) |
| `duration_seconds` | Optional duration in seconds |

### Example JSON (podcast item)

```json
{
  "title": "Episode 42: Threat modeling for LLM systems",
  "canonical_url": "https://example.com/episodes/42",
  "feed_name": "Example Security Podcast",
  "feed_url": "https://example.com/feed.xml",
  "category": "Security",
  "author": "Example Research Team",
  "published_date": "2026-02-20T16:00:00+00:00",
  "summary": "A practical security walkthrough for auditing LLM systems.",
  "content_html": "<p>Episode notes...</p><h2>Key takeaways</h2><ul><li>Threat modeling</li></ul>",
  "image_url": "https://example.com/images/episode-42.jpg",
  "tags": ["security", "llm", "prompt-injection"],
  "language": "en",
  "fetched_at": "2026-02-22T12:34:56+00:00",
  "source_opml": "feeds-2026-02-22.opml",
  "guid": "example-guid-42",
  "content_kind": "podcast",
  "media_assets": [
    {
      "kind": "audio",
      "url": "https://cdn.example.com/audio/episode-42.mp3",
      "mime_type": "audio/mpeg",
      "title": "Episode 42",
      "duration_seconds": 3720
    },
    {
      "kind": "youtube",
      "url": "https://example.com/video-platform/watch/abc123xyz",
      "mime_type": "",
      "title": "Episode 42 (Video)",
      "duration_seconds": null
    }
  ]
}
```

### Example JSON (video-only item)

```json
{
  "title": "Live: AI Security Roundtable",
  "canonical_url": "https://example.com/videos/ai-security-roundtable",
  "content_kind": "video",
  "media_assets": [
    {
      "kind": "video",
      "url": "https://cdn.example.com/video/ai-security-roundtable.mp4",
      "mime_type": "video/mp4",
      "title": "AI Security Roundtable",
      "duration_seconds": 5410
    }
  ]
}
```

### HTML Sanitization

`content_html` is sanitized before writing JSON:

- Keeps semantic markup (headings, paragraphs, lists, emphasis, links, images).
- Removes active/unsafe content (`script`, `style`, `iframe`, forms, embeds, inline styles).
- Keeps only absolute `https://` links and image sources.
- Converts non-HTTPS hyperlinks to plain text.
- Removes non-HTTPS images.

### Output Structure

```
data/articles/
  feed-slug/
    2026-02-22_103000-feed-slug-article-title.json
    2026-02-22_140000-feed-slug-another-article.json
  another-feed/
    ...
```

### Deduplication

Articles are matched by GUID (the feed's own `<id>`/`<guid>`, falling back to `<link>`, then a SHA-256 hash of feed URL + title + date). Re-runs never create duplicate files.

## Custom MCP Tools

The agent exposes six tools via an in-process MCP server:

| Tool | Purpose |
|---|---|
| `parse_opml_files` | Parse OPML glob → subscription list |
| `process_single_feed` | Chunked fetch/filter/dedup/enrich/save pipeline for one feed (`chunk_page`/`chunk_size`) |
| `extract_page_metadata` | Scrape OG tags, canonical URL, hero image from a page |
| `save_article` | Validate & save a single Article JSON |
| `list_feed_articles` | List saved articles for a feed |
| `get_run_statistics` | Aggregate archive statistics |

The agent also has access to the built-in `WebSearch` and `WebFetch` tools for ad-hoc enrichment.

## Stage 2 — Newspaper Generator

Once articles are fetched, generate the newspaper:

```bash
python generate_newspaper.py
```

This will:

1. **Load** all article JSON files from `data/articles/`.
2. **Enrich** each article with display fields (formatted dates, plain-text summaries, media flags, sequential indices).
3. **Extract** tag frequencies and content-type counts across the corpus.
4. **Render** a single self-contained HTML file via the Jinja2 template at `templates/newspaper.html.j2`.
5. **Write** the output to `data/newspapers/yyyy-mm-dd_HHmm/newspaper-yyyy-mm-dd_HHmm.html`.

Open the generated `.html` file directly in any browser — no server required.

### CLI Options

```
python generate_newspaper.py --help

  --days N              Only include articles from the last N days
  --articles-dir DIR    Custom articles directory
  --output-dir DIR      Custom output root directory
  --template-dir DIR    Custom templates directory
  -v, --verbose         Enable debug logging
```

### Newspaper Features

- **Old-time broadsheet aesthetic** — desaturated palette, serif typography, decorative flourishes, all via CSS custom properties.
- **Sticky search bar** — full-text search across titles, authors, feeds, summaries, and tags. Press `/` to focus.
- **Collapsible filter panel** — multi-dimensional filtering by category, content type (article/podcast/video), media type (MP3/YouTube), and tags. All filters compose together (AND logic). Active filters shown as badge hints when the panel is collapsed.
- **Tag cloud** — top 25 tags with article counts, click to filter.
- **Grid / headline views** — toggle between card grid and compact headline list.
- **Article modal** — full content display with media asset links (MP3 play, YouTube watch), tags, and link to original.
- **Fully self-contained** — all CSS, JS, and SVG icons are inline. No external dependencies. Only article images are external resources.
- **Responsive** — scales from mobile to desktop with a 32px rem base for readability.

### Template Architecture

The Jinja2 template (`templates/newspaper.html.j2`, ~1800 lines) contains:

- **CSS block** — custom properties, component styles, responsive breakpoints.
- **SVG `<defs>`** — 19 icon symbols (newspaper, search, shield, podcast, video, etc.).
- **HTML** — masthead, toolbar, filter panel, category sections with card grids, modal overlay, back-to-top button.
- **Inline JSON** — `ARTICLES` array for JS article lookup (title, content, media assets, etc.).
- **JS IIFE** — search, multi-dimensional filtering, view toggle, modal, keyboard shortcuts.

The generator computes these enrichment fields per article before passing to the template:

| Field | Description |
|---|---|
| `_idx` | Sequential index for JS lookup |
| `published_display` | Human-readable date string |
| `summary_plain` | HTML-stripped summary for `data-searchable` |
| `_has_audio` | Boolean — has audio media asset |
| `_has_video` | Boolean — has video media asset |
| `_has_youtube` | Boolean — has YouTube media asset |
| `_media_flags` | Space-separated media types for `data-media` attribute |

Template context also receives: `top_tags` (top 25 tags with counts), `content_counts` (article/podcast/video/media totals), `category_icons` mapping.

## Prerequisites

- Python 3.12+
- Node.js 18+ (for the Claude Code CLI)
- [LM Studio](https://lmstudio.ai/) running locally (or any OpenAI-compatible endpoint)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/timelessprototype/rssnewspaper.git
cd rssnewspaper

# Run the setup script
./dev-prepare.sh

# Activate the virtual environment
source .venv/bin/activate

# Run the fetcher
python agentic_fetcher.py

# Generate the newspaper
python generate_newspaper.py
```

### Manual Setup

```bash
# Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install the Claude Code CLI (required by the Agent SDK)
npm install -g @anthropic-ai/claude-code

# Copy and configure environment variables
cp example.env .env
# Edit .env if needed

# Fetch articles
python agentic_fetcher.py

# Generate the newspaper
python generate_newspaper.py
```

## Configuration

Environment variables are loaded from `.env` (see `example.env`):

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_BASE_URL` | LLM API endpoint | `http://localhost:1234` |
| `ANTHROPIC_AUTH_TOKEN` | API authentication token | `lmstudio` |
| `MODEL` | Model name used by the SDK client (`USE_VLLM=1` overrides this to `VLLM_SERVED_MODEL_NAME`) | `mistral-7b-instruct-v0_3` |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Max output tokens per assistant response | `20000` |
| `AGENT_MAX_TURNS` | Max agentic turns per run (`0` or blank = unlimited). Docs describe no default limit and do not publish a hard numeric max | `500` |
| `ANTHROPIC_USE_MESSAGES_ENDPOINT` | Enables LM Studio Anthropic `/v1/messages` compatibility mode | `true` |
| `LMSTUDIO_USER_ONLY_PROMPT` | When `1`, always avoid Anthropic `system` role by embedding system instructions in the first user message (template-compat mode) | `0` |
| `LMSTUDIO_AUTO_USER_ONLY_RETRY` | When `1`, auto-retries once in template-compat mode if backend returns a Jinja role-mismatch error | `1` |
| `USE_VLLM` | When `1`, auto-start local vLLM server from the script | `1` |
| `VLLM_MODEL` | Hugging Face model id passed to `vllm serve` | `mistralai/Mistral-7B-Instruct-v0.3` |
| `VLLM_SERVED_MODEL_NAME` | Alias exposed by vLLM (used by client model mapping, must not contain `/`) | `mistral-7b-instruct-v0_3` |
| `VLLM_HOST` | vLLM bind host | `127.0.0.1` |
| `VLLM_PORT` | vLLM bind port | `8234` |
| `VLLM_PORT_SCAN_LIMIT` | How many higher ports to probe if `VLLM_PORT` is in use | `20` |
| `VLLM_MASTER_PORT` | Internal vLLM distributed/rendezvous port (separate from API port) | `8334` |
| `VLLM_MASTER_PORT_SCAN_LIMIT` | How many higher ports to probe if `VLLM_MASTER_PORT` is in use | `100` |
| `VLLM_API_KEY` | API key expected by local vLLM server | `dummy` |
| `VLLM_HF_TOKEN` | Optional Hugging Face token for private/gated models (`--hf-token`) | `` |
| `VLLM_MAX_MODEL_LEN` | Requested vLLM max context length | `20000` |
| `VLLM_AUTO_ADJUST_MAX_MODEL_LEN` | If `1`, auto-retry startup using vLLM's estimated safe max length after KV-cache failure | `1` |
| `VLLM_GPU_MEMORY_UTILIZATION` | Fraction of total VRAM vLLM may reserve (`0.1`–`1.0`) | `0.9` |
| `VLLM_FIT_PROFILE` | If `1`, applies fit-first defaults when unset (`max-num-seqs=1`, `max-num-batched-tokens=512`, `enforce-eager=1`) | `1` |
| `VLLM_MAX_NUM_SEQS` | vLLM scheduler limit (`--max-num-seqs`) | `` |
| `VLLM_MAX_NUM_BATCHED_TOKENS` | vLLM scheduler token cap (`--max-num-batched-tokens`) | `` |
| `VLLM_ENFORCE_EAGER` | Disable CUDA graph capture (`--enforce-eager`) to reduce memory overhead | `1` |
| `VLLM_KV_CACHE_DTYPE` | KV cache dtype override (e.g. `fp8`, `bfloat16`) | `` |
| `VLLM_CPU_OFFLOAD_GB` | vLLM CPU offload budget in GiB (`--cpu-offload-gb`) | `` |
| `VLLM_SWAP_SPACE_GB` | vLLM CPU swap space per GPU in GiB (`--swap-space`) | `` |
| `VLLM_LOAD_FORMAT` | vLLM load format override (`auto`, `gguf`, `bitsandbytes`, etc.) | `` |
| `VLLM_QUANTIZATION` | Explicit quantization method (`--quantization`) | `` |
| `VLLM_DTYPE` | Weight/activation dtype override (`--dtype`) | `` |
| `VLLM_EXTRA_ARGS` | Raw extra args appended to `vllm serve` (advanced escape hatch) | `` |
| `VLLM_TOOL_CALL_PARSER` | vLLM tool-call parser for the selected model | `mistral` |
| `VLLM_STARTUP_TIMEOUT` | Seconds to wait for vLLM health readiness (`0` = no timeout) | `1800` |
| `AGENT_MIN_CONTEXT_TOKENS` | Advisory threshold for effective context capacity (`0` disables advisory; does not block startup) | `16000` |

`AGENT_MAX_TURNS` sizing heuristic: `(feed_count × expected_turns_per_feed) + retry_overhead`.
Example: ~120 feeds × ~2 turns + overhead ≈ `300-500`.

### Runtime Modes

- `USE_VLLM=0`: Uses `ANTHROPIC_BASE_URL` directly (LM Studio/OpenAI-compatible endpoint).
- `USE_VLLM=1`: Starts `vllm serve` automatically and rewires `ANTHROPIC_*` vars to the local vLLM server.
- If the configured `VLLM_PORT` is already occupied, startup auto-selects the next free port (up to `VLLM_PORT + VLLM_PORT_SCAN_LIMIT`) and prints the requested/effective ports.

### vLLM Auth Notes

- If your model is private or gated, set `VLLM_HF_TOKEN` (or `HF_TOKEN`) before running.
- You can also authenticate once with `hf auth login`.
- If startup fails, check the printed `vLLM logs` path for the exact server error.

## Project Structure

```
rssnewspaper/
├── agentic_fetcher.py       # Stage 1: Agentic RSS fetcher (entry point)
├── generate_newspaper.py    # Stage 2: HTML newspaper generator
├── templates/
│   └── newspaper.html.j2    # Self-contained Jinja2 newspaper template
├── dev-prepare.sh           # One-shot dev environment setup
├── requirements.txt         # Python dependencies
├── example.env              # Example environment config
├── LICENSE                  # MIT License
├── README.md
└── data/
    ├── feeds-*.opml         # OPML subscription files (fetcher input)
    ├── articles/            # Article JSON output (one subdir per feed)
    │   └── <feed-slug>/
    │       └── <date>-<feed>-<title>.json
    └── newspapers/          # Generated newspapers (timestamped subdirs)
        └── yyyy-mm-dd_HHmm/
            └── newspaper-yyyy-mm-dd_HHmm.html
```

## License

[MIT](LICENSE) — Copyright (c) 2026 Timeless Prototype

# RSS Newspaper

An agentic RSS/Atom feed fetcher powered by the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview). It autonomously works through OPML subscription lists, fetches recent articles, enriches missing metadata, sanitizes HTML for safe card rendering, and saves JSON files ready for article, podcast, or video cards.

## How It Works

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
| `process_single_feed` | Full fetch/filter/dedup/enrich/save pipeline for one feed |
| `extract_page_metadata` | Scrape OG tags, canonical URL, hero image from a page |
| `save_article` | Validate & save a single Article JSON |
| `list_feed_articles` | List saved articles for a feed |
| `get_run_statistics` | Aggregate archive statistics |

The agent also has access to the built-in `WebSearch` and `WebFetch` tools for ad-hoc enrichment.

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

# Run
python agentic_fetcher.py
```

## Configuration

Environment variables are loaded from `.env` (see `example.env`):

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_BASE_URL` | LLM API endpoint | `http://localhost:1234` |
| `ANTHROPIC_AUTH_TOKEN` | API authentication token | `lmstudio` |
| `MODEL` | Model name used by the SDK client (`USE_VLLM=1` overrides this to `VLLM_SERVED_MODEL_NAME`) | `mistral-7b-instruct-v0_3` |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Max output tokens per assistant response | `20000` |
| `ANTHROPIC_USE_MESSAGES_ENDPOINT` | Enables LM Studio Anthropic `/v1/messages` compatibility mode | `true` |
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
| `VLLM_MAX_MODEL_LEN` | vLLM max context length (lower this if KV cache errors occur) | `2000` |
| `VLLM_GPU_MEMORY_UTILIZATION` | Fraction of total VRAM vLLM may reserve (`0.1`–`1.0`) | `0.9` |
| `VLLM_TOOL_CALL_PARSER` | vLLM tool-call parser for the selected model | `mistral` |
| `VLLM_STARTUP_TIMEOUT` | Seconds to wait for vLLM health readiness (`0` = no timeout) | `1800` |

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
├── agentic_fetcher.py   # The agentic fetcher (entry point)
├── dev-prepare.sh       # One-shot dev environment setup
├── requirements.txt     # Python dependencies
├── example.env          # Example environment config
├── LICENSE              # MIT License
├── README.md
└── data/
    ├── feeds-*.opml     # OPML subscription files
    └── articles/        # Output directory (created automatically)
        └── <feed-slug>/
            └── <date>-<feed>-<title>.json
```

## License

[MIT](LICENSE) — Copyright (c) 2026 Timeless Prototype

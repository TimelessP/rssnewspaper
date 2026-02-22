# RSS Newspaper

An agentic RSS/Atom feed fetcher powered by the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview). It autonomously works through OPML subscription lists, fetches recent articles, enriches missing metadata, and saves fully-populated JSON files ready for rendering as newspaper-style article cards.

## How It Works

The agent follows a disciplined three-step workflow:

1. **Discovery** — Parses all `data/feed*.opml` files to extract every RSS/Atom subscription.
2. **Process Each Feed** — For each subscription, fetches the feed, filters to the last 3 days, deduplicates against already-saved articles (by GUID), auto-enriches missing images and content by scraping the article page, and saves new articles as JSON.
3. **Summary** — Reports totals: feeds attempted, articles saved, duplicates skipped, failures, and articles still missing key fields.

### Article JSON Schema

Every article is validated against a Pydantic `Article` model with 16 fields:

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

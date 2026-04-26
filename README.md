# Competitive Intelligence Scraper

A local-first tool that crawls a website and generates a structured AI analyst report — covering what the product does, who it targets, how it's priced, and how it's positioned competitively.

![Demo](Assets/scraper_demo.gif)

## Features

- Headless Playwright crawler with smart priority queuing (homepage → docs → product pages → blog)
- Respects `robots.txt`, skips login-gated pages and legal/TOS pages
- Guarantees minimum doc coverage when a `/docs` path or subdomain is detected
- Live progress stream in the UI via SSE
- AI analysis via **Claude** (Anthropic API) or any local **Ollama** model — no cloud required
- Vanilla JS/CSS frontend, no build step

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser (first time only)
playwright install chromium

# Start the server
python3 app.py
# → http://localhost:5000
```

**To use Claude models:** add your Anthropic API key to a `.env` file in the project root:
```
ANTHROPIC_API_KEY=sk-ant-...
```

**To use Ollama models:** install [Ollama](https://ollama.com) and pull a model (e.g. `ollama pull llama3`). No API key needed.

## Crawler — standalone usage

```bash
python3 crawl.py https://example.com
python3 crawl.py https://example.com --max-pages 25 --delay 1.5
```

Output is written to `Crawl history/scraped_<domain>_<timestamp>.md`.

## Architecture

Two independent Python scripts connected by a thin Flask API:

**`crawl.py`** — headless Playwright crawler
- Reads `sitemap.xml` to assess site structure and page count
- Priority queue: homepage → docs subdomains → product/features/pricing → blog/news/about → everything else
- Extracts title, meta description, h1–h3 headings, and up to 60 paragraphs/list items per page
- Outputs a structured Markdown file with pages separated by `---`

**`app.py`** — Flask server + LLM analysis layer
- `POST /api/crawl` — spawns crawler subprocess, returns `job_id`
- `GET /api/crawl/<job_id>/stream` — SSE stream with live progress
- `POST /api/analyze` — compresses scraped content (~75% token reduction) and calls Claude or Ollama
- `GET /api/ollama/models` — probes `localhost:11434` for installed models
- `GET /api/reports` — lists previous crawl files sorted by date

**`static/`** — vanilla JS/CSS single-page UI, no build step required

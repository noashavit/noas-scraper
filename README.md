# Competitive intelligence scraper

A local-first tool that crawls a website and generates a structured AI analyst report, covering what the product does, who it targets, how it's priced, and how it's positioned competitively.

![Demo](Assets/scraper_demo.gif)

## Features

- Headless Playwright crawler with smart priority queuing (homepage → docs → product pages → blog)
- Respects `robots.txt`, skips login-gated pages and legal/TOS pages
- Guarantees minimum doc coverage when a `/docs` path or subdomain is detected
- Live progress stream in the UI via SSE
- AI analysis via **Claude** (Anthropic API) or any local **Ollama** model — no cloud required
- Vanilla JS/CSS frontend, no build step

## Requirements

- Python 3.9+
- One of:
  - **Anthropic API key** — for Claude models (sign up at [console.anthropic.com](https://console.anthropic.com))
  - **[Ollama](https://ollama.com)** — to run models locally, no API key needed

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/noashavit/noas-scraper.git
cd noas-scraper

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install the Playwright browser (first time only)
playwright install chromium
```

## Configuration

**Option A — Use Claude (Anthropic API):**

Create a `.env` file in the project root:
```
ANTHROPIC_API_KEY=sk-ant-...
```

**Option B — Use Ollama (fully local, no API key):**

Install Ollama from [ollama.com](https://ollama.com), then pull a model:
```bash
ollama pull llama3
# or any other model: mistral, gemma3, phi4, etc.
```

## Running the app

```bash
python3 app.py
# → http://localhost:5000
```

Open your browser to `http://localhost:5000`, enter a URL, and hit **Crawl**. Once the crawl finishes, select a model and click **Analyze** to generate the report.

## Crawler — standalone usage

You can also run the crawler on its own without the web UI:

```bash
python3 crawl.py https://example.com
python3 crawl.py https://example.com --max-pages 25 --delay 1.5
```

Output is written to `Crawl history/scraped_<domain>_<timestamp>.md`.

## File structure

```
noas-scraper/
├── app.py              # Flask server — API routes and LLM analysis
├── crawl.py            # Headless Playwright crawler (runs standalone or via API)
├── requirements.txt    # Python dependencies
├── .env                # Your API key — create this locally, never commit it
│
├── static/
│   ├── index.html      # Single-page UI
│   ├── app.js          # All UI state and API calls
│   └── style.css       # Styles
│
├── Assets/
│   └── scraper_demo.gif  # Demo animation used in this README
│
└── Crawl history/      # Crawler output files (gitignored)
    └── scraped_<domain>_<timestamp>.md
```

## Architecture

Two independent Python scripts connected by a thin Flask API:

**`crawl.py`** — headless Playwright crawler
- Reads `sitemap.xml` to assess site structure and page count
- Priority queue: homepage → docs subdomains → product/features/pricing → blog/news/about → everything else
- Skips login-gated pages, legal/TOS pages, and binary file extensions
- Guarantees minimum doc coverage when a `/docs` path or subdomain is detected
- Extracts title, meta description, h1–h3 headings, and up to 60 paragraphs/list items per page
- Outputs a structured Markdown file with pages separated by `---`

**`app.py`** — Flask server + LLM analysis layer
- `POST /api/crawl` — spawns crawler subprocess, returns `job_id`
- `GET /api/crawl/<job_id>/stream` — SSE stream with live crawl progress
- `POST /api/analyze` — compresses scraped content (~75% token reduction) and calls Claude or Ollama
- `GET /api/ollama/models` — probes `localhost:11434` for installed models
- `GET /api/reports` — lists previous crawl files sorted by date

**`static/`** — vanilla JS/CSS single-page UI, no build step required

---

## Built by

[Noa Shavit](https://www.linkedin.com/in/noashavit), product marketer and AI builder based in San Francisco.

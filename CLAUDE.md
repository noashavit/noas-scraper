# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Install dependencies (requires Playwright browsers on first run)
pip install -r requirements.txt
playwright install chromium

# Start the Flask server
python3 app.py
# → http://localhost:5001
```

Set `ANTHROPIC_API_KEY` in a `.env` file in the project root (loaded automatically at startup).

## Running the crawler standalone

```bash
python3 crawl.py https://example.com
python3 crawl.py https://example.com --max-pages 50 --delay 1.5
```

Defaults: `--max-pages 50`, `--delay 1.5`. Output written to `Crawl history/scraped_<domain>_<timestamp>.md`. The bare file path is printed to stdout; progress lines go to stderr.

## Architecture

Two independent Python scripts connected by a thin Flask API:

**`crawl.py`** — headless Playwright crawler.
- Priority queue: homepage (0) → docs subdomains (1) → product/features/pricing pages (2) → blog/news/about (3) → everything else (4). Skips paths containing `/privacy`, `/terms`, `/legal`, `/user-agreement`, `/tos`.
- Respects `robots.txt`, skips login-gated pages (password input or `/login`-style paths), avoids binary file extensions.
- Guarantees a minimum of 10 docs pages (`MIN_DOCS_PAGES`) if a docs subdomain or `/docs` path is detected — will continue past `max_pages` for docs-only URLs to hit this floor.
- Extracts title, meta description, h1–h3 headings, and up to 60 paragraphs/list items per page.
- Writes a structured Markdown file; each page block separated by `---`. Format per page: `## Title — URL`, `**Meta:** ...`, `### Headings`, `### Content`.

**`app.py`** — Flask server + LLM analysis layer.
- `POST /api/crawl` — spawns `crawl.py` as a subprocess in a daemon thread, returns a `job_id`.
- `GET /api/crawl/<job_id>/stream` — SSE stream; proxies stderr lines as `{"type":"progress"}` events, then emits `{"type":"done","file":"..."}` or `{"type":"error"}`.
- `POST /api/analyze` — reads a scraped `.md` file, compresses it via `_compress_scraped()` (strips paragraph text, keeps headings/meta only, ~75% token reduction), then calls either Claude Haiku (`ANTHROPIC_API_KEY`) or a local Ollama model.
- `POST /api/export/pdf` — accepts the report JSON, builds a self-contained HTML page via `_build_report_html()`, renders it with Playwright (`page.pdf()`), and returns a binary PDF. Links are preserved natively by Chrome's PDF engine — no coordinate math.
- `GET /api/ollama/models` — probes `http://localhost:11434` for installed models.
- `GET /api/reports` — lists `scraped_*.md` files in `Crawl history/` sorted by mtime.

**`static/`** — vanilla JS/CSS single-page UI (no build step). `app.js` drives all UI state and API calls.

**`crawl.md`** — prompt template used for the `/crawl` Claude Code skill (standalone analyst report workflow, separate from the Flask app).

## Key design details

- The `pages_reviewed` field in the analysis JSON is always overridden with URLs parsed directly from the markdown file (regex on `## Title — URL` lines), not from LLM output, to avoid hallucination.
- Ollama path caps input at 24 000 chars and retries once with an explicit correction prompt if the model returns invalid JSON. Ollama uses `num_ctx: 16384` and `temperature: 0.1`.
- Claude path uses `claude-haiku-4-5-20251001` with `max_tokens: 4096`.
- Jobs are held in in-process dicts (`jobs`, `job_queues`) — no persistence across server restarts.
- `PORT` env var overrides the default port (5001).

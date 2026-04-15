#!/usr/bin/env python3
"""
Flask server for the competitive intelligence crawler.
Usage: python3 app.py
Requires: ANTHROPIC_API_KEY in .env file or environment variable
"""

import json
import os
import queue
import re
import subprocess
import threading
import urllib.request
import urllib.error
import uuid
from pathlib import Path

import anthropic
from flask import Flask, Response, jsonify, request, send_from_directory

OLLAMA_BASE = "http://localhost:11434"

# ---------------------------------------------------------------------------
# Load .env file (simple parser, no extra dependencies)
# ---------------------------------------------------------------------------
def _load_dotenv():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

_load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR))

# job_id -> {"status": "running"|"done"|"error", "file": str|None, "error": str|None}
jobs: dict[str, dict] = {}
job_queues: dict[str, queue.Queue] = {}


# ---------------------------------------------------------------------------
# Crawl management
# ---------------------------------------------------------------------------
def _run_crawl(job_id: str, url: str):
    q = job_queues[job_id]
    try:
        proc = subprocess.Popen(
            ["python3", str(BASE_DIR / "crawl.py"), url],
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Stream stderr (progress lines) into the queue
        output_filename = None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                q.put({"type": "progress", "message": line})

        proc.wait()

        # stdout contains the output filename
        stdout = proc.stdout.read().strip()
        if proc.returncode == 0 and stdout:
            output_filename = stdout
            jobs[job_id]["status"] = "done"
            jobs[job_id]["file"] = output_filename
            q.put({"type": "done", "file": output_filename})
        else:
            err = f"Crawl exited with code {proc.returncode}"
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = err
            q.put({"type": "error", "message": err})

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        q.put({"type": "error", "message": str(e)})
    finally:
        q.put(None)  # sentinel


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(STATIC_DIR), filename)


@app.route("/api/crawl", methods=["POST"])
def start_crawl():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    if not url.startswith("http"):
        url = "https://" + url

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "file": None, "error": None}
    job_queues[job_id] = queue.Queue()

    t = threading.Thread(target=_run_crawl, args=(job_id, url), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/crawl/<job_id>/stream")
def stream_progress(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "job not found"}), 404

    def event_stream():
        q = job_queues[job_id]
        while True:
            msg = q.get()
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


def _compress_scraped(raw: str, max_chars=None) -> str:
    """
    Extract only URL, title, meta description, and headings from each page block.
    Skips the full paragraph content — reduces tokens by ~75% while preserving
    all the signal the model needs for structured extraction.

    max_chars: if set, truncate the result to this many characters (whole pages only,
    so the last page is never cut mid-way).
    """
    pages = re.split(r"\n---\n", raw)
    compressed = []
    total_chars = 0
    for page in pages:
        lines = page.strip().splitlines()
        kept = []
        in_headings = False
        in_content = False
        for line in lines:
            # Always keep: section header (## Title — URL), meta line, heading bullets
            if line.startswith("## ") or line.startswith("# "):
                kept.append(line)
                in_content = False
                in_headings = False
            elif line.startswith("**Meta:**"):
                kept.append(line)
            elif line.strip() == "### Headings":
                kept.append(line)
                in_headings = True
                in_content = False
            elif line.strip() == "### Content":
                in_headings = False
                in_content = True  # skip content lines
            elif in_headings and line.startswith("- "):
                kept.append(line)
            elif in_content:
                pass  # skip full paragraph text
            elif line.strip():
                kept.append(line)
        if kept:
            page_text = "\n".join(kept)
            if max_chars and total_chars + len(page_text) > max_chars:
                break
            compressed.append(page_text)
            total_chars += len(page_text)
    return "\n\n---\n\n".join(compressed)


ANALYSIS_PROMPT_TEMPLATE = """You are a competitive intelligence analyst for a product marketing team.

Below is structured metadata (URLs, titles, headings) scraped from a website. Analyze it and return ONLY a raw JSON object — no markdown fences, no extra text.

{{
  "company_name": "company or product name",
  "domain": "the domain",
  "overview": {{
    "what_they_do": "2-3 sentences: product/service and problem it solves",
    "target_audience": "primary buyers and users — persona, industry, company size",
    "business_model": "how they charge and any pricing signals found",
    "competitive_positioning": "stated differentiators and who they position against"
  }},
  "features": [
    {{
      "name": "feature category name",
      "description": "3-5 sentences for a product marketer: what the feature does, the problem it solves, who uses it, and any standout technical detail or differentiator",
      "source_url": "most relevant page URL from the scraped content, or empty string"
    }}
  ],
  "pages_reviewed": [
    {{"url": "string", "title": "string"}}
  ]
}}

Rules:
- features: 5-10 items covering the most important capability areas; each description must be 3-5 sentences
- pages_reviewed: every URL from the scraped content
- Only include facts from the scraped text — no hallucination
- Return raw JSON only, no markdown or extra text

SCRAPED CONTENT:
{scraped_text}"""


def _parse_llm_json(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        raise ValueError("Model returned an empty response.")

    # Strip markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()

    # Try parsing as-is first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Fall back: extract the outermost {...} block from the response
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Nothing worked — include the raw output so the user can see what the model said
    preview = raw[:500] + ("…" if len(raw) > 500 else "")
    raise ValueError(f"Could not parse model response as JSON. Raw output:\n\n{preview}")


def _analyze_anthropic(scraped_text: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set — add it to your .env file")
    client = anthropic.Anthropic(api_key=api_key)
    prompt = ANALYSIS_PROMPT_TEMPLATE.format(scraped_text=scraped_text)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_llm_json(message.content[0].text)


def _ollama_request(model: str, messages: list) -> str:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 16384},
        "format": "json",  # ask Ollama to constrain output to valid JSON
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_BASE}. "
            "Make sure Ollama is running: https://ollama.com"
        ) from e
    return body["message"]["content"]


def _analyze_ollama(scraped_text: str, model: str) -> dict:
    # Ollama local models have limited context — cap input at ~6k tokens worth of chars
    if len(scraped_text) > 24000:
        scraped_text = scraped_text[:24000] + "\n\n[... truncated for model context limit ...]"
    prompt = ANALYSIS_PROMPT_TEMPLATE.format(scraped_text=scraped_text)
    messages = [{"role": "user", "content": prompt}]
    text = _ollama_request(model, messages)
    print(f"[ollama raw response] {text[:1000]}")
    try:
        return _parse_llm_json(text)
    except ValueError:
        # Retry: explicitly ask the model to fix its output
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Your response was not valid JSON. Return ONLY the raw JSON object, nothing else."},
        ]
        text2 = _ollama_request(model, messages)
        return _parse_llm_json(text2)


@app.route("/api/ollama/models")
def ollama_models():
    """Return list of locally installed Ollama models."""
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        models = [m["name"] for m in body.get("models", [])]
        return jsonify({"models": models, "available": True})
    except Exception:
        return jsonify({"models": [], "available": False})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True)
    filename = (data.get("file") or "").strip()
    provider = (data.get("provider") or "anthropic").strip()
    ollama_model = (data.get("ollama_model") or "llama3.2").strip()

    if not filename:
        return jsonify({"error": "file is required"}), 400

    file_path = BASE_DIR / "Crawl history" / Path(filename).name
    if not file_path.exists():
        return jsonify({"error": f"file not found: {filename}"}), 404

    raw_text = file_path.read_text(encoding="utf-8")
    scraped_text = _compress_scraped(raw_text)

    # Extract all crawled pages directly from the markdown (not from LLM output)
    all_pages = []
    for line in raw_text.splitlines():
        m = re.match(r"^## (.+?) — (https?://\S+)", line)
        if m:
            all_pages.append({"title": m.group(1).strip(), "url": m.group(2).strip()})

    try:
        if provider == "ollama":
            report = _analyze_ollama(scraped_text, ollama_model)
        else:
            report = _analyze_anthropic(scraped_text)
        # Override pages_reviewed with the full authoritative list from the file
        report["pages_reviewed"] = all_pages
        return jsonify(report)
    except (json.JSONDecodeError, ValueError) as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports")
def list_reports():
    crawl_history_dir = BASE_DIR / "Crawl history"
    files = sorted(crawl_history_dir.glob("scraped_*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    return jsonify([f.name for f in files])


if __name__ == "__main__":
    STATIC_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", 5001))
    print(f"Starting server at http://localhost:{port}")
    app.run(debug=False, port=port, threaded=True)

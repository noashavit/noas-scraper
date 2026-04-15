#!/usr/bin/env python3
"""
Web crawler for competitive intelligence.
Usage: python crawl.py https://example.com [--max-pages 25]
Output: scraped_<domain>_<timestamp>.md in the current directory
"""

import asyncio
import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------
# Priority 0 = homepage, 1 = docs, 2 = product/features/pricing, 3 = blog/about, 4 = everything else
DOCS_SUBDOMAINS = ("docs.", "developer.", "developers.", "help.", "support.")
DOCS_PATH_PATTERNS = ["/docs", "/documentation", "/doc"]
PRIORITY_2_PATTERNS = [
    "/features", "/feature", "/solutions", "/solution",
    "/platform", "/product", "/products", "/pricing", "/price",
]
PRIORITY_3_PATTERNS = [
    "/blog", "/news", "/resources", "/resource",
    "/about", "/team", "/why-us", "/why",
]
MIN_DOCS_PAGES = 10


def is_docs_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return any(host.startswith(s) for s in DOCS_SUBDOMAINS) or \
           any(path == p or path.startswith(p + "/") for p in DOCS_PATH_PATTERNS)


def page_priority(url: str) -> int:
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    if path in ("", "/"):
        return 0  # homepage first
    if is_docs_url(url):
        return 1  # docs subdomain or /docs path — highest priority after homepage
    for pat in PRIORITY_2_PATTERNS:
        if path == pat or path.startswith(pat + "/") or path.startswith(pat + "-"):
            return 2
    for pat in PRIORITY_3_PATTERNS:
        if path == pat or path.startswith(pat + "/") or path.startswith(pat + "-"):
            return 3
    return 4


# ---------------------------------------------------------------------------
# robots.txt helper
# ---------------------------------------------------------------------------
def build_robot_parser(base_url: str) -> RobotFileParser:
    robots_url = urljoin(base_url, "/robots.txt")
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception:
        pass  # if robots.txt is unavailable, treat as allowed
    return rp


# ---------------------------------------------------------------------------
# Login-gate detection
# ---------------------------------------------------------------------------
def looks_like_login_page(current_url: str, html: str) -> bool:
    parsed = urlparse(current_url)
    path = parsed.path.lower()
    if any(seg in path for seg in ("/login", "/signin", "/sign-in", "/auth", "/sso")):
        return True
    if '<input type="password"' in html.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------
async def extract_page_data(page) -> dict:
    url = page.url

    title = await page.title()
    meta_desc = await page.evaluate(
        "() => (document.querySelector('meta[name=\"description\"]') || {}).content || ''"
    )

    headings = await page.evaluate("""
        () => {
            const tags = ['h1', 'h2', 'h3'];
            const results = [];
            tags.forEach(tag => {
                document.querySelectorAll(tag).forEach(el => {
                    const text = el.innerText.trim();
                    if (text) results.push({ tag, text });
                });
            });
            return results;
        }
    """)

    paragraphs = await page.evaluate("""
        () => {
            const seen = new Set();
            const results = [];
            document.querySelectorAll('p, li').forEach(el => {
                const text = el.innerText.trim();
                if (text.length > 40 && !seen.has(text)) {
                    seen.add(text);
                    results.push(text);
                }
            });
            return results.slice(0, 60);
        }
    """)

    links = await page.evaluate("""
        (baseUrl) => {
            const results = [];
            document.querySelectorAll('a[href]').forEach(el => {
                try {
                    const abs = new URL(el.getAttribute('href'), baseUrl).href;
                    results.push(abs);
                } catch(e) {}
            });
            return results;
        }
    """, url)

    html_snippet = await page.content()

    return {
        "url": url,
        "title": title,
        "meta": meta_desc,
        "headings": headings,
        "paragraphs": paragraphs,
        "links": links,
        "html": html_snippet,
    }


# ---------------------------------------------------------------------------
# Markdown serialisation
# ---------------------------------------------------------------------------
def page_to_markdown(data: dict) -> str:
    lines = []
    lines.append(f"## {data['title'] or '(no title)'} — {data['url']}")
    if data["meta"]:
        lines.append(f"**Meta:** {data['meta']}")
    lines.append("")
    if data["headings"]:
        lines.append("### Headings")
        for h in data["headings"]:
            lines.append(f"- {h['tag'].upper()}: {h['text']}")
        lines.append("")
    if data["paragraphs"]:
        lines.append("### Content")
        for p in data["paragraphs"][:30]:
            lines.append(p)
            lines.append("")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main crawler
# ---------------------------------------------------------------------------
async def crawl(start_url: str, max_pages: int = 25, delay: float = 1.5):
    parsed_start = urlparse(start_url)
    base_origin = f"{parsed_start.scheme}://{parsed_start.netloc}"
    domain = parsed_start.netloc.replace("www.", "")

    robot_parser = build_robot_parser(base_origin)

    def is_allowed(url: str) -> bool:
        return robot_parser.can_fetch("Mozilla/5.0", url)

    def is_internal(url: str) -> bool:
        p = urlparse(url)
        if p.netloc == parsed_start.netloc or p.netloc == "":
            return True
        # also allow docs.rootdomain.com, developer.rootdomain.com, etc.
        root_domain = parsed_start.netloc.replace("www.", "")
        return p.netloc.endswith("." + root_domain)

    def normalise(url: str) -> str:
        p = urlparse(url)
        # strip fragments and trailing slashes
        normalised = p._replace(fragment="", query="").geturl().rstrip("/")
        return normalised

    visited: set[str] = set()
    # queue: list of (priority, url)
    queue: list[tuple[int, str]] = [(page_priority(start_url), start_url)]
    pages_data: list[dict] = []
    docs_pages_crawled = 0
    docs_detected = is_docs_url(start_url)  # true if starting on a docs subdomain

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        while queue:
            # Stop when max pages reached, unless docs were detected but undercrawled
            if len(pages_data) >= max_pages:
                if not docs_detected or docs_pages_crawled >= MIN_DOCS_PAGES:
                    break
                # Filter queue down to docs-only URLs to satisfy the minimum
                queue = [(p, u) for p, u in queue if is_docs_url(u)]
                if not queue:
                    break
            # sort by priority (stable)
            queue.sort(key=lambda x: x[0])
            priority, url = queue.pop(0)

            norm = normalise(url)
            if norm in visited:
                continue
            visited.add(norm)

            if not is_allowed(url):
                print(f"  [robots] skipping {url}", file=sys.stderr)
                continue

            docs_tag = f" docs:{docs_pages_crawled}" if docs_detected else ""
            print(f"  [{len(pages_data)+1}/{max_pages}{docs_tag}] p{priority} → {url}", file=sys.stderr)

            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)

                # skip non-HTML responses
                content_type = response.headers.get("content-type", "") if response else ""
                if "text/html" not in content_type and content_type:
                    continue

                # wait for JS to settle
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass  # proceed with whatever loaded

                html = await page.content()

                # skip login-gated pages
                if looks_like_login_page(page.url, html):
                    print(f"  [auth] skipping login page {page.url}", file=sys.stderr)
                    continue

                data = await extract_page_data(page)
                pages_data.append(data)

                if is_docs_url(page.url):
                    docs_pages_crawled += 1

                # discover new links
                for link in data["links"]:
                    try:
                        parsed_link = urlparse(link)
                    except Exception:
                        continue
                    # skip non-http, anchors, files
                    if parsed_link.scheme not in ("http", "https"):
                        continue
                    if any(link.lower().endswith(ext) for ext in (
                        ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                        ".zip", ".mp4", ".mp3", ".css", ".js"
                    )):
                        continue
                    if not is_internal(link):
                        continue
                    if is_docs_url(link):
                        docs_detected = True
                    norm_link = normalise(link)
                    if norm_link not in visited:
                        queue.append((page_priority(link), link))

            except PlaywrightTimeoutError:
                print(f"  [timeout] {url}", file=sys.stderr)
            except Exception as e:
                print(f"  [error] {url}: {e}", file=sys.stderr)

            await asyncio.sleep(delay)

        await browser.close()

    return pages_data, domain


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Competitive intelligence web crawler")
    parser.add_argument("url", help="Starting URL (e.g. https://example.com)")
    parser.add_argument("--max-pages", type=int, default=50, help="Max pages to crawl (default: 50)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between requests (default: 1.5)")
    args = parser.parse_args()

    start_url = args.url
    if not start_url.startswith("http"):
        start_url = "https://" + start_url

    print(f"Crawling {start_url} (max {args.max_pages} pages)...", file=sys.stderr)
    start_ts = datetime.now()

    pages_data, domain = asyncio.run(crawl(start_url, args.max_pages, args.delay))

    # Build output filename
    safe_domain = re.sub(r"[^\w.-]", "_", domain)
    timestamp = start_ts.strftime("%Y%m%d_%H%M%S")
    crawl_history_dir = Path(__file__).parent / "Crawl history"
    crawl_history_dir.mkdir(exist_ok=True)
    out_file = crawl_history_dir / f"scraped_{safe_domain}_{timestamp}.md"

    # Write markdown
    lines = [
        f"# Scraped: {domain}",
        f"Crawled: {start_ts.isoformat(timespec='seconds')} | Pages: {len(pages_data)}",
        "",
        "---",
        "",
    ]
    for data in pages_data:
        lines.append(page_to_markdown(data))
        lines.append("")

    out_file.write_text("\n".join(lines), encoding="utf-8")
    # Print the filename as the last line of stdout so the skill can capture it
    print(str(out_file))


if __name__ == "__main__":
    main()

"""Discover pages on the target site.

Two strategies, tried in order:
  1. Parse `sitemap.xml` (and any nested sitemapindex). Best for sites that
     publish one — it's authoritative and includes every page the site wants
     indexed.
  2. **Fallback: BFS crawl from the homepage** with Playwright. Many modern
     static sites (Netlify, Vercel, Webflow) don't publish a sitemap. We
     load the homepage in a real browser (so JS-rendered links count), extract
     every same-origin anchor, and recurse until either we run out of new
     pages or hit MAX_BFS_PAGES.

We use Playwright/patchright rather than plain httpx so:
  - JS-only SPAs are crawl-able
  - sites with light bot protection (not BT-grade Akamai) just work
  - the same code path can be reused for the other checks
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

from patchright.async_api import async_playwright, Browser, BrowserContext
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import BASE_URL, SITEMAP_URL, USER_AGENT, CRAWL_TIMEOUT_MS, DATA_DIR
from helpers.cookies import dismiss_cookies

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Cap the BFS so we don't accidentally try to crawl 10 000 pages.
MAX_BFS_PAGES = int(os.environ.get("BT_MONITOR_MAX_BFS_PAGES", "30"))
MAX_BFS_DEPTH = int(os.environ.get("BT_MONITOR_MAX_BFS_DEPTH", "3"))


@dataclass
class CrawlResult:
    discovery_method: str = "unknown"  # "sitemap" | "bfs" | "homepage_only"
    sitemap_urls_fetched: list[str] = field(default_factory=list)
    page_urls: set[str] = field(default_factory=set)
    errors: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "base_url": BASE_URL,
            "discovery_method": self.discovery_method,
            "sitemap_urls_fetched": self.sitemap_urls_fetched,
            "page_count": len(self.page_urls),
            "page_urls": sorted(self.page_urls),
            "errors": self.errors,
        }


# ----------------------------------------------------------------------
# strategy 1 — sitemap.xml
# ----------------------------------------------------------------------

async def fetch_xml(context: BrowserContext, url: str) -> tuple[str | None, dict]:
    page = await context.new_page()
    try:
        response = await page.goto(url, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
        if response is None:
            return None, {"status": None, "reason": "no_response"}
        status = response.status
        body = await response.text()
        if status >= 400:
            return None, {"status": status, "snippet": body[:300]}
        return body, {"status": status, "bytes": len(body)}
    except Exception as exc:
        return None, {"error": repr(exc)}
    finally:
        await page.close()


def parse_sitemap(xml_text: str, source_url: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(xml_text, "xml")
    sub_sitemaps: list[str] = []
    pages: list[str] = []
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if loc and loc.text:
            sub_sitemaps.append(urljoin(source_url, loc.text.strip()))
    for url_node in soup.find_all("url"):
        loc = url_node.find("loc")
        if loc and loc.text:
            pages.append(urljoin(source_url, loc.text.strip()))
    return sub_sitemaps, pages


async def try_sitemap(context: BrowserContext, result: CrawlResult) -> None:
    """Walk sitemap.xml and any nested sitemapindex documents."""
    queue: list[str] = [SITEMAP_URL]
    visited: set[str] = set()
    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        print(f"[sitemap] {url}", flush=True)
        xml, dbg = await fetch_xml(context, url)
        if xml is None:
            print(f"   [skip] {dbg}", flush=True)
            result.errors.append({"url": url, "stage": "sitemap_fetch", "debug": dbg})
            continue
        result.sitemap_urls_fetched.append(url)
        try:
            subs, pages = parse_sitemap(xml, url)
        except Exception as exc:
            result.errors.append({"url": url, "stage": "sitemap_parse", "error": repr(exc)})
            continue
        for s in subs:
            if s not in visited:
                queue.append(s)
        for p in pages:
            result.page_urls.add(p)
        print(f"   -> {len(subs)} sub-sitemap(s), {len(pages)} URL(s)", flush=True)


# ----------------------------------------------------------------------
# strategy 2 — BFS from homepage
# ----------------------------------------------------------------------

def normalize_link(href: str, page_url: str) -> str | None:
    """Resolve and clean a href; return None if it should be skipped."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:",
                                    "sms:", "whatsapp:", "data:")):
        return None
    absolute = urljoin(page_url, href)
    # Drop fragment (#section) — same page from a crawl perspective.
    clean, _ = urldefrag(absolute)
    # Skip non-http(s) (file://, ftp://, etc.).
    parsed = urlparse(clean)
    if parsed.scheme not in ("http", "https"):
        return None
    return clean


def is_same_origin(url: str, base: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return False


async def extract_links(context: BrowserContext, page_url: str) -> tuple[list[str], int | None]:
    """Load a page and return every same-origin anchor href + the HTTP status."""
    page = await context.new_page()
    try:
        response = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
        if response is None:
            return [], None
        status = response.status
        if status >= 400:
            return [], status
        # Let JS-injected nav links appear.
        await page.wait_for_timeout(1500)
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
        urls: list[str] = []
        seen: set[str] = set()
        for h in hrefs:
            u = normalize_link(h, page_url)
            if u and is_same_origin(u, BASE_URL) and u not in seen:
                seen.add(u)
                urls.append(u)
        return urls, status
    except Exception:
        return [], None
    finally:
        await page.close()


async def try_bfs(context: BrowserContext, result: CrawlResult) -> None:
    """Crawl from the homepage, BFS, capped by MAX_BFS_PAGES / MAX_BFS_DEPTH."""
    start = BASE_URL.rstrip("/") + "/"
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    visited: set[str] = set()

    while queue and len(visited) < MAX_BFS_PAGES:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        print(f"[bfs depth={depth}] {url}", flush=True)
        children, status = await extract_links(context, url)
        if status is None or status >= 400:
            print(f"   [skip] status={status}", flush=True)
            result.errors.append({"url": url, "stage": "bfs_load", "status": status})
            continue
        # This page is real → add it to results.
        result.page_urls.add(url)
        print(f"   -> {len(children)} same-origin link(s) on page", flush=True)
        if depth < MAX_BFS_DEPTH:
            for c in children:
                if c not in visited:
                    queue.append((c, depth + 1))


# ----------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------

async def discover() -> CrawlResult:
    result = CrawlResult()
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ro-RO",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )

        # Warm-up — primes Chromium DNS cache + any first-hit cookie quirks.
        warm = await context.new_page()
        try:
            r = await warm.goto(BASE_URL, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
            await dismiss_cookies(warm)
            print(f"[warm-up] {BASE_URL} -> {r.status if r else 'no_response'}", flush=True)
        except Exception as exc:
            print(f"[warm-up] failed: {exc!r}", flush=True)
            result.errors.append({"stage": "warmup", "error": repr(exc)})
        await warm.close()

        # Strategy 1: sitemap.xml.
        await try_sitemap(context, result)
        if result.page_urls:
            result.discovery_method = "sitemap"
            print(f"\n[discovery] sitemap.xml yielded {len(result.page_urls)} page(s).", flush=True)
        else:
            # Strategy 2: BFS from homepage.
            print(f"\n[discovery] no sitemap pages — falling back to BFS crawl "
                  f"(max {MAX_BFS_PAGES} pages, depth {MAX_BFS_DEPTH}).", flush=True)
            await try_bfs(context, result)
            if result.page_urls:
                result.discovery_method = "bfs"
            else:
                # Last resort: just record the homepage if it responded at all.
                start = BASE_URL.rstrip("/") + "/"
                result.page_urls.add(start)
                result.discovery_method = "homepage_only"
                print("[discovery] BFS returned nothing — recording homepage only.", flush=True)

        await context.close()
        await browser.close()

    return result


async def main() -> None:
    print(f"Discovering pages on {BASE_URL}")
    result = await discover()

    out_dir = ROOT / DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "sitemap_pages.json"
    out_path.write_text(json.dumps(result.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print(f"Method:           {result.discovery_method}")
    print(f"Sitemaps fetched: {len(result.sitemap_urls_fetched)}")
    print(f"Unique page URLs: {len(result.page_urls)}")
    print(f"Errors:           {len(result.errors)}")
    print(f"Saved to:         {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

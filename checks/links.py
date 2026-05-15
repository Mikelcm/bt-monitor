"""Broken-links check.

For each page discovered by the crawler, load the page in Playwright, extract
every linkable URL (anchors, images, scripts, stylesheets, iframes), then
issue a HEAD (falling back to GET) for each unique URL and record:
  - HTTP status
  - whether it was a redirect (and the final URL)
  - whether it is internal or external to BASE_URL

We use Playwright to FETCH the page (so JS-rendered links are captured), but
plain httpx for the link probes themselves (10x faster than spinning up pages).
External links sometimes have WAFs too — we send a real browser User-Agent.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from patchright.async_api import async_playwright, BrowserContext

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import BASE_URL, USER_AGENT, CRAWL_TIMEOUT_MS, DATA_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Schemes we don't probe (not real HTTP URLs).
SKIP_SCHEMES = {"mailto", "tel", "javascript", "data", "sms", "whatsapp"}

# Max parallel HTTP probes — be polite, don't DDoS.
PROBE_CONCURRENCY = 10
PROBE_TIMEOUT_S = 15


@dataclass
class LinkProbe:
    url: str
    status: int | None = None
    final_url: str | None = None
    redirected: bool = False
    error: str | None = None
    internal: bool = False

    def is_broken(self) -> bool:
        if self.error is not None:
            return True
        if self.status is None:
            return True
        return self.status >= 400


@dataclass
class PageLinks:
    page_url: str
    links_found: int = 0
    links: list[str] = field(default_factory=list)
    fetch_error: str | None = None


def is_same_origin(url: str, base: str = BASE_URL) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return False


def should_skip(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() in SKIP_SCHEMES:
            return True
        # Skip fragment-only URLs (handled by the page itself).
        if not parsed.netloc and not parsed.path:
            return True
        return False
    except Exception:
        return True


async def extract_links_from_page(context: BrowserContext, page_url: str) -> PageLinks:
    """Load the page and extract every href/src URL on it (after JS render)."""
    out = PageLinks(page_url=page_url)
    page = await context.new_page()
    try:
        response = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
        if response is None or response.status >= 400:
            out.fetch_error = f"status={response.status if response else 'no_response'}"
            return out

        # Give late-loading content a moment (single-page apps, lazy widgets).
        await page.wait_for_timeout(1500)

        urls: set[str] = set()
        # Anchors
        for href in await page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))"):
            if href:
                urls.add(urljoin(page_url, href))
        # Images, scripts, stylesheets, iframes — anything that fetches a URL.
        for sel, attr in [("img[src]", "src"), ("script[src]", "src"),
                          ("link[href]", "href"), ("iframe[src]", "src"),
                          ("source[src]", "src")]:
            for u in await page.eval_on_selector_all(
                sel, f"els => els.map(e => e.getAttribute('{attr}'))"
            ):
                if u:
                    urls.add(urljoin(page_url, u))

        out.links = sorted(u for u in urls if not should_skip(u))
        out.links_found = len(out.links)
        return out
    except Exception as exc:
        out.fetch_error = repr(exc)
        return out
    finally:
        await page.close()


async def probe_url(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> LinkProbe:
    """HEAD-probe a URL; fall back to GET if HEAD is not allowed (405/403)."""
    probe = LinkProbe(url=url, internal=is_same_origin(url))
    async with sem:
        try:
            # HEAD first.
            r = await client.head(url, follow_redirects=True, timeout=PROBE_TIMEOUT_S)
            probe.status = r.status_code
            probe.final_url = str(r.url)
            probe.redirected = len(r.history) > 0
            # Some servers reject HEAD; retry with GET.
            if probe.status in (403, 405, 501):
                r = await client.get(url, follow_redirects=True, timeout=PROBE_TIMEOUT_S)
                probe.status = r.status_code
                probe.final_url = str(r.url)
                probe.redirected = len(r.history) > 0
            return probe
        except httpx.RequestError as exc:
            probe.error = f"{type(exc).__name__}: {exc}"
            return probe
        except Exception as exc:
            probe.error = repr(exc)
            return probe


async def check_broken_links(page_urls: list[str]) -> dict:
    """Top-level: fetch each page, extract links, probe every unique link."""
    per_page: list[PageLinks] = []
    all_links: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ro-RO",
            viewport={"width": 1366, "height": 768},
        )
        for url in page_urls:
            print(f"[extract] {url}", flush=True)
            pl = await extract_links_from_page(context, url)
            print(f"   → {pl.links_found} links" + (f"  (FETCH ERROR: {pl.fetch_error})" if pl.fetch_error else ""), flush=True)
            per_page.append(pl)
            all_links.update(pl.links)
        await context.close()
        await browser.close()

    print(f"\n[probe] {len(all_links)} unique links to check")
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8"},
    ) as client:
        probes = await asyncio.gather(*(probe_url(client, u, sem) for u in sorted(all_links)))

    broken = [p for p in probes if p.is_broken()]
    redirects = [p for p in probes if p.redirected and not p.is_broken()]

    print(f"[probe] done: {len(probes)} checked, {len(broken)} broken, {len(redirects)} redirects")

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "pages_checked": len(per_page),
        "unique_links": len(all_links),
        "broken_count": len(broken),
        "redirect_count": len(redirects),
        "per_page": [
            {"page_url": p.page_url, "links_found": p.links_found, "fetch_error": p.fetch_error}
            for p in per_page
        ],
        "broken": [p.__dict__ for p in broken],
        "redirects": [p.__dict__ for p in redirects],
        "all_probes": [p.__dict__ for p in probes],
    }


async def main() -> None:
    sitemap_path = ROOT / DATA_DIR / "sitemap_pages.json"
    if not sitemap_path.exists():
        print(f"ERROR: {sitemap_path} not found. Run crawler/sitemap.py first.")
        return
    sitemap = json.loads(sitemap_path.read_text(encoding="utf-8"))
    page_urls = sitemap["page_urls"]

    result = await check_broken_links(page_urls)

    out_path = ROOT / DATA_DIR / "links_report.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"BROKEN LINKS REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:    {result['pages_checked']}")
    print(f"Unique links:     {result['unique_links']}")
    print(f"Broken:           {result['broken_count']}")
    print(f"Redirects:        {result['redirect_count']}")
    print(f"Saved to:         {out_path}")

    if result["broken"]:
        print("\nBROKEN LINKS:")
        for p in result["broken"][:20]:
            print(f"  [{p.get('status') or 'ERR'}] {p['url']}")
            if p.get("error"):
                print(f"        error: {p['error']}")


if __name__ == "__main__":
    asyncio.run(main())

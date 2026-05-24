"""Performance check: measure how slow each page is and flag those over the threshold.

We use Playwright's Performance API + navigation timing to get real metrics:
  - TTFB (time to first byte)
  - DOMContentLoaded
  - load event
  - LCP (Largest Contentful Paint) — best proxy for "feels slow"
  - total transfer size
  - request count
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import BASE_URL, USER_AGENT, CRAWL_TIMEOUT_MS, SLOW_PAGE_THRESHOLD_MS, DATA_DIR
from helpers.cookies import dismiss_cookies

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# JS snippet that pulls navigation timing + LCP from the page.
PERF_SCRIPT = """
() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const paints = Object.fromEntries(
    performance.getEntriesByType('paint').map(p => [p.name, p.startTime])
  );
  const resources = performance.getEntriesByType('resource');
  const totalBytes = resources.reduce((s, r) => s + (r.transferSize || 0), 0);

  // LCP from the buffered performance entries (Chromium retains these).
  let lcp = null;
  try {
    const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
    if (lcpEntries.length) {
      lcp = Math.round(lcpEntries[lcpEntries.length - 1].startTime);
    }
  } catch (e) {}

  return {
    ttfb_ms: nav.responseStart ? Math.round(nav.responseStart - nav.requestStart) : null,
    dom_content_loaded_ms: nav.domContentLoadedEventEnd ? Math.round(nav.domContentLoadedEventEnd - nav.startTime) : null,
    load_ms: nav.loadEventEnd ? Math.round(nav.loadEventEnd - nav.startTime) : null,
    first_paint_ms: paints['first-paint'] ? Math.round(paints['first-paint']) : null,
    first_contentful_paint_ms: paints['first-contentful-paint'] ? Math.round(paints['first-contentful-paint']) : null,
    lcp_ms: lcp,
    transfer_bytes: totalBytes,
    resource_count: resources.length,
  };
}
"""

@dataclass
class PagePerf:
    page_url: str
    wall_time_ms: int | None = None
    metrics: dict = field(default_factory=dict)
    status: int | None = None
    error: str | None = None

    @property
    def slow(self) -> bool:
        # Prefer LCP, then load, then DCL, then wall clock.
        candidate = (
            self.metrics.get("lcp_ms")
            or self.metrics.get("load_ms")
            or self.metrics.get("dom_content_loaded_ms")
            or self.wall_time_ms
        )
        return candidate is not None and candidate > SLOW_PAGE_THRESHOLD_MS


async def measure_page(context, page_url: str) -> PagePerf:
    perf = PagePerf(page_url=page_url)
    page = await context.new_page()
    try:
        import time
        t0 = time.perf_counter()
        response = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="load")
        # Give LCP observer a beat to register.
        await page.wait_for_timeout(800)
        perf.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        perf.status = response.status if response else None
        perf.metrics = await page.evaluate(PERF_SCRIPT)
        return perf
    except Exception as exc:
        perf.error = repr(exc)
        return perf
    finally:
        await page.close()


async def check_performance(page_urls: list[str]) -> dict:
    results: list[PagePerf] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ro-RO",
            viewport={"width": 1366, "height": 768},
        )
        # (LCP observer installed per-page after navigation; see measure_page.)

        # Warm-up: prime Chromium DNS cache + any first-request quirks
        # by hitting the base URL once before timed measurements.
        warm = await context.new_page()
        try:
            await warm.goto(BASE_URL, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
            await dismiss_cookies(warm)
            print(f"[warm-up] {BASE_URL} -> ok", flush=True)
        except Exception as exc:
            print(f"[warm-up] failed: {exc!r}", flush=True)
        await warm.close()

        for url in page_urls:
            print(f"[perf] {url}", flush=True)
            # Two attempts — first request may hit transient DNS hiccups in headless Chromium.
            p = await measure_page(context, url)
            if p.error and ("ERR_NAME_NOT_RESOLVED" in p.error or "ERR_CONNECTION" in p.error):
                print(f"   retrying after network error...", flush=True)
                await asyncio.sleep(1.5)
                p = await measure_page(context, url)
            if p.error:
                print(f"   [ERROR] {p.error[:140]}", flush=True)
            else:
                badge = "SLOW" if p.slow else "ok"
                lcp = p.metrics.get("lcp_ms")
                load = p.metrics.get("load_ms")
                print(f"   [{badge}] wall={p.wall_time_ms}ms  load={load}ms  lcp={lcp}ms", flush=True)
            results.append(p)
        await context.close()
        await browser.close()

    slow = [r for r in results if r.slow]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "threshold_ms": SLOW_PAGE_THRESHOLD_MS,
        "pages_checked": len(results),
        "slow_count": len(slow),
        "pages": [r.__dict__ for r in results],
        "slow_pages": [r.__dict__ for r in slow],
    }


async def main() -> None:
    sitemap_path = ROOT / DATA_DIR / "sitemap_pages.json"
    if not sitemap_path.exists():
        print(f"ERROR: {sitemap_path} not found. Run crawler/sitemap.py first.")
        return
    page_urls = json.loads(sitemap_path.read_text(encoding="utf-8"))["page_urls"]

    report = await check_performance(page_urls)
    out_path = ROOT / DATA_DIR / "performance_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"PERFORMANCE REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:    {report['pages_checked']}")
    print(f"Threshold:        {report['threshold_ms']} ms")
    print(f"Slow pages:       {report['slow_count']}")
    print(f"Saved to:         {out_path}")

    if report["slow_pages"]:
        print("\nSLOW PAGES:")
        for p in report["slow_pages"]:
            m = p["metrics"]
            print(f"  {p['page_url']}")
            print(f"    LCP={m.get('lcp_ms')}ms  Load={m.get('load_ms')}ms  TTFB={m.get('ttfb_ms')}ms")


if __name__ == "__main__":
    asyncio.run(main())

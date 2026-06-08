"""Asset check: detect images, fonts, scripts, stylesheets that fail to load.

While the page is loading we attach a listener to every network response.
Anything with a 4xx/5xx status or that errors out is logged. We classify by
resource type so the report can say "X images broken, Y fonts broken" — which
is exactly the brief's 'imagini nu se afișează' / 'font stricat' requirement.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import BASE_URL, USER_AGENT, CRAWL_TIMEOUT_MS, DATA_DIR
from helpers.cookies import dismiss_cookies

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# Resource types we care about per the brief. Playwright's response.request.resource_type
# returns one of: document, stylesheet, image, media, font, script, texttrack, xhr,
# fetch, eventsource, websocket, manifest, other.
INTERESTING_TYPES = {"image", "font", "media", "stylesheet", "script"}


@dataclass
class FailedAsset:
    url: str
    resource_type: str
    status: int | None
    page_url: str
    failure_text: str | None = None  # set when the request didn't even get a response

    def __dict__repr__(self):
        return self.__dict__


@dataclass
class PageAssetReport:
    page_url: str
    total_requests: int = 0
    requests_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    failed: list[FailedAsset] = field(default_factory=list)
    fetch_error: str | None = None

    def to_json(self):
        return {
            "page_url": self.page_url,
            "total_requests": self.total_requests,
            "requests_by_type": dict(self.requests_by_type),
            "failed_count": len(self.failed),
            "failed": [f.__dict__ for f in self.failed],
            "fetch_error": self.fetch_error,
        }


async def check_assets_on_page(context, page_url: str) -> PageAssetReport:
    report = PageAssetReport(page_url=page_url)
    page = await context.new_page()

    def on_response(response):
        try:
            req = response.request
            rtype = req.resource_type
            report.total_requests += 1
            report.requests_by_type[rtype] += 1
            if response.status >= 400 and rtype in INTERESTING_TYPES:
                report.failed.append(FailedAsset(
                    url=response.url,
                    resource_type=rtype,
                    status=response.status,
                    page_url=page_url,
                ))
        except Exception:
            pass

    def on_request_failed(request):
        # Network-level failure (DNS, connection refused, blocked, timeout).
        try:
            rtype = request.resource_type
            if rtype in INTERESTING_TYPES:
                report.failed.append(FailedAsset(
                    url=request.url,
                    resource_type=rtype,
                    status=None,
                    page_url=page_url,
                    failure_text=request.failure or "request_failed",
                ))
        except Exception:
            pass

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)

    try:
        response = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="load")
        # Wait a little for lazy-loaded images / fonts that fire after `load`.
        await page.wait_for_timeout(2000)
        if response is None or response.status >= 400:
            report.fetch_error = f"status={response.status if response else 'no_response'}"
    except Exception as exc:
        report.fetch_error = repr(exc)
    finally:
        await page.close()
    return report


async def check_assets(page_urls: list[str]) -> dict:
    results: list[PageAssetReport] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ro-RO",
            viewport={"width": 1366, "height": 768},
        )
        # DNS warm-up — patchright headless can flake on first hit.
        warm = await context.new_page()
        try:
            await warm.goto(BASE_URL, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
            await dismiss_cookies(warm)
        except Exception:
            pass
        await warm.close()

        for url in page_urls:
            print(f"[assets] {url}", flush=True)
            r = await check_assets_on_page(context, url)
            tally = ", ".join(f"{k}={v}" for k, v in sorted(r.requests_by_type.items()))
            print(
                f"   requests: {r.total_requests} ({tally})  failed: {len(r.failed)}",
                flush=True,
            )
            results.append(r)
        await context.close()
        await browser.close()

    all_failed: list[FailedAsset] = [f for r in results for f in r.failed]
    failed_by_type: dict[str, list[dict]] = defaultdict(list)
    for f in all_failed:
        failed_by_type[f.resource_type].append(f.__dict__)

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "pages_checked": len(results),
        "total_failed_assets": len(all_failed),
        "failed_by_type": {k: len(v) for k, v in failed_by_type.items()},
        "failed_assets_by_type": dict(failed_by_type),
        "per_page": [r.to_json() for r in results],
    }


async def main() -> None:
    sitemap_path = ROOT / DATA_DIR / "sitemap_pages.json"
    if not sitemap_path.exists():
        print(f"ERROR: {sitemap_path} not found. Run crawler/sitemap.py first.")
        return
    page_urls = json.loads(sitemap_path.read_text(encoding="utf-8"))["page_urls"]

    report = await check_assets(page_urls)
    out_path = ROOT / DATA_DIR / "assets_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"ASSETS REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:        {report['pages_checked']}")
    print(f"Total failed assets:  {report['total_failed_assets']}")
    for t, n in report["failed_by_type"].items():
        print(f"   {t}: {n}")
    print(f"Saved to:             {out_path}")

    if report["total_failed_assets"]:
        print("\nFAILED ASSETS:")
        for t, items in report["failed_assets_by_type"].items():
            print(f"  [{t}]")
            for it in items[:8]:
                code = it.get("status") or it.get("failure_text") or "?"
                print(f"    [{code}] {it['url']}")
                print(f"         on page: {it['page_url']}")


if __name__ == "__main__":
    asyncio.run(main())

"""Capture JavaScript runtime errors and 4xx/5xx console messages per page.

Ported from flaviuzh/bt-monitor (TypeScript). The original captured only
`pageerror` events. We add two more useful signals:
  - browser `console.error(...)` messages (visible JS warnings the dev team
    almost always wants to know about)
  - failed network requests that the page tried to make (404s on fetch,
    blocked CORS, etc.) — these often surface real broken integrations

For each page, we open it in Playwright, listen for the duration of the
load + a short tail, then report.
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
from config import BASE_URL, USER_AGENT, CRAWL_TIMEOUT_MS, DATA_DIR
from helpers.cookies import dismiss_cookies

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


@dataclass
class PageConsole:
    page_url: str
    js_errors: list[dict] = field(default_factory=list)     # uncaught exceptions
    console_errors: list[dict] = field(default_factory=list) # console.error(...)
    console_warnings: list[dict] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    fetch_error: str | None = None

    @property
    def total_issues(self) -> int:
        return len(self.js_errors) + len(self.console_errors) + len(self.failed_requests)


async def check_one(context, page_url: str) -> PageConsole:
    report = PageConsole(page_url=page_url)
    page = await context.new_page()

    def on_pageerror(exc):
        # Uncaught JS exception on the page — what the user's browser DevTools
        # would show as red "Uncaught …".
        try:
            report.js_errors.append({
                "name": getattr(exc, "name", "Error"),
                "message": str(exc),
            })
        except Exception:
            report.js_errors.append({"message": repr(exc)})

    def on_console(msg):
        try:
            mtype = msg.type
            text = msg.text
        except Exception:
            return
        # Skip noisy known-benign categories.
        if not text:
            return
        if mtype == "error":
            # Skip dev-tools chatter we can't act on.
            low = text.lower()
            if any(s in low for s in ("favicon.ico", "manifest.json")):
                return
            report.console_errors.append({"text": text[:500]})
        elif mtype == "warning":
            report.console_warnings.append({"text": text[:300]})

    def on_request_failed(req):
        # Resource the page tried to fetch but failed (network, CORS, etc.).
        try:
            report.failed_requests.append({
                "url": req.url,
                "method": req.method,
                "resource_type": req.resource_type,
                "failure": req.failure or "request_failed",
            })
        except Exception:
            pass

    page.on("pageerror", on_pageerror)
    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)

    try:
        resp = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="load")
        # Tail listening for late-firing errors (typical for SPAs).
        await page.wait_for_timeout(2500)
        if resp is None or resp.status >= 400:
            report.fetch_error = f"status={resp.status if resp else 'no_response'}"
    except Exception as exc:
        report.fetch_error = repr(exc)
    finally:
        await page.close()
    return report


async def check_console(page_urls: list[str]) -> dict:
    results: list[PageConsole] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT, locale="ro-RO",
            viewport={"width": 1366, "height": 768},
        )
        warm = await context.new_page()
        try:
            await warm.goto(BASE_URL, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
            await dismiss_cookies(warm)
        except Exception:
            pass
        await warm.close()

        for url in page_urls:
            print(f"[console] {url}", flush=True)
            r = await check_one(context, url)
            print(
                f"   js_err={len(r.js_errors)} con_err={len(r.console_errors)} "
                f"failed_req={len(r.failed_requests)}",
                flush=True,
            )
            results.append(r)
        await context.close()
        await browser.close()

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "pages_checked": len(results),
        "total_js_errors": sum(len(r.js_errors) for r in results),
        "total_console_errors": sum(len(r.console_errors) for r in results),
        "total_failed_requests": sum(len(r.failed_requests) for r in results),
        "pages_with_issues": sum(1 for r in results if r.total_issues > 0),
        "pages": [
            {
                "page_url": r.page_url,
                "js_errors": r.js_errors,
                "console_errors": r.console_errors,
                "failed_requests": r.failed_requests,
                "fetch_error": r.fetch_error,
            }
            for r in results
        ],
    }


async def main() -> None:
    sitemap_path = ROOT / DATA_DIR / "sitemap_pages.json"
    if not sitemap_path.exists():
        print(f"ERROR: {sitemap_path} not found. Run crawler/sitemap.py first.")
        return
    page_urls = json.loads(sitemap_path.read_text(encoding="utf-8"))["page_urls"]

    report = await check_console(page_urls)
    out_path = ROOT / DATA_DIR / "console_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"CONSOLE / JS ERRORS REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:           {report['pages_checked']}")
    print(f"JS exceptions:           {report['total_js_errors']}")
    print(f"console.error messages:  {report['total_console_errors']}")
    print(f"Failed network requests: {report['total_failed_requests']}")
    print(f"Saved to:                {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

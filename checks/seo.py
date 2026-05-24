"""Basic SEO health check.

Ported from flaviuzh/bt-monitor (TypeScript) and expanded. The original
checked two things — meta description and <title>. We add a few more
that matter for bank sites (mobile readiness + crawler hints):

  - <title>                           present and non-empty
  - <meta name="description">         present and non-empty
  - <h1>                              exactly one (best practice)
  - <meta name="viewport">            present (mobile responsiveness)
  - <link rel="canonical">            present (avoids duplicate-content SEO penalty)
  - <html lang="…">                   present (accessibility + i18n)
  - Open Graph (og:title, og:image)   for nice link previews on social/Teams
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


# JS that extracts everything we care about in one round-trip.
SEO_SCRIPT = """
() => {
  const meta = (name) => {
    const el = document.querySelector(`meta[name="${name}"]`)
            || document.querySelector(`meta[property="${name}"]`);
    return el ? (el.getAttribute('content') || '').trim() : null;
  };
  return {
    title: (document.title || '').trim(),
    meta_description: meta('description'),
    meta_viewport: meta('viewport'),
    canonical: (() => {
      const l = document.querySelector('link[rel="canonical"]');
      return l ? l.getAttribute('href') : null;
    })(),
    html_lang: document.documentElement.getAttribute('lang') || null,
    h1_count: document.querySelectorAll('h1').length,
    h1_texts: Array.from(document.querySelectorAll('h1')).slice(0, 5)
                .map(h => (h.innerText || '').trim().slice(0, 80)),
    og_title:       meta('og:title'),
    og_description: meta('og:description'),
    og_image:       meta('og:image'),
  };
}
"""


@dataclass
class PageSEO:
    page_url: str
    findings: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    fetch_error: str | None = None


def evaluate_seo(f: dict) -> list[str]:
    issues: list[str] = []
    if not f.get("title"):
        issues.append("missing_title")
    elif len(f["title"]) > 70:
        issues.append("title_too_long")
    elif len(f["title"]) < 10:
        issues.append("title_too_short")

    if not f.get("meta_description"):
        issues.append("missing_meta_description")
    elif f["meta_description"] and len(f["meta_description"]) > 320:
        issues.append("meta_description_too_long")

    if not f.get("meta_viewport"):
        issues.append("missing_viewport")     # broken on mobile
    if not f.get("canonical"):
        issues.append("missing_canonical")
    if not f.get("html_lang"):
        issues.append("missing_html_lang")

    h1 = f.get("h1_count", 0)
    if h1 == 0:
        issues.append("no_h1")
    elif h1 > 1:
        issues.append(f"multiple_h1_({h1})")

    if not f.get("og_title") and not f.get("og_description"):
        issues.append("no_open_graph")
    return issues


async def check_one(context, page_url: str) -> PageSEO:
    out = PageSEO(page_url=page_url)
    page = await context.new_page()
    try:
        resp = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        if resp is None or resp.status >= 400:
            out.fetch_error = f"status={resp.status if resp else 'no_response'}"
            return out
        out.findings = await page.evaluate(SEO_SCRIPT)
        out.issues = evaluate_seo(out.findings)
        return out
    except Exception as exc:
        out.fetch_error = repr(exc)
        return out
    finally:
        await page.close()


async def check_seo(page_urls: list[str]) -> dict:
    results: list[PageSEO] = []
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
            print(f"[seo] {url}", flush=True)
            r = await check_one(context, url)
            if r.fetch_error:
                print(f"   [ERROR] {r.fetch_error[:120]}", flush=True)
            else:
                print(f"   issues: {len(r.issues)} ({', '.join(r.issues) or 'none'})",
                      flush=True)
            results.append(r)
        await context.close()
        await browser.close()

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "pages_checked": len(results),
        "pages_with_issues": sum(1 for r in results if r.issues),
        "total_issues": sum(len(r.issues) for r in results),
        "pages": [
            {
                "page_url": r.page_url,
                "findings": r.findings,
                "issues": r.issues,
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

    report = await check_seo(page_urls)
    out_path = ROOT / DATA_DIR / "seo_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"SEO REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:     {report['pages_checked']}")
    print(f"Pages with issues: {report['pages_with_issues']}")
    print(f"Total SEO issues:  {report['total_issues']}")
    print(f"Saved to:          {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

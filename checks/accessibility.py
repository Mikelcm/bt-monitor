"""Accessibility check — runs axe-core on every discovered page.

Ported from flaviuzh/bt-monitor (TypeScript, @axe-core/playwright).
The original used the Node binding; here we inject axe-core directly
into each page from the CDN and call `axe.run()` via page.evaluate().

We keep the same noise filter as the original — five rules that produce
high false-positive rates on enterprise / SPA sites are silenced. We
also keep the same "critical + serious only" severity gate, because
moderate/minor findings are best-practice suggestions, not bugs.

For a bank, WCAG 2 AA compliance is a real legal obligation in most
EU jurisdictions, so this check is one of the highest-value additions.
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


# axe-core script — pinned to a known-good version, served from cdnjs.
AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"

# Rules excluded (verbatim from flaviuzh/bt-monitor) because they produce
# high false positives on enterprise/SPA sites.
EXCLUDED_RULES = [
    "scrollable-region-focusable",
    "region",
    "duplicate-id-aria",
    "duplicate-id",
    "bypass",
]

# JS that runs inside the page after axe is loaded. Returns a JSON-able dict.
AXE_RUN_SCRIPT = """
async (excluded) => {
  if (typeof axe === 'undefined') return { error: 'axe_not_loaded' };
  try {
    const results = await axe.run(document, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
      rules: Object.fromEntries(excluded.map(r => [r, { enabled: false }])),
    });
    return {
      violations: results.violations.map(v => ({
        id: v.id,
        impact: v.impact,
        description: v.description,
        help: v.help,
        helpUrl: v.helpUrl,
        nodeCount: v.nodes.length,
        tags: v.tags,
      })),
      passes_count: results.passes.length,
      incomplete_count: results.incomplete.length,
    };
  } catch (e) {
    return { error: String(e && e.message || e) };
  }
}
"""


@dataclass
class PageA11y:
    page_url: str
    violations: list[dict] = field(default_factory=list)
    passes_count: int = 0
    incomplete_count: int = 0
    error: str | None = None

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.violations if v.get("impact") == "critical")

    @property
    def serious_count(self) -> int:
        return sum(1 for v in self.violations if v.get("impact") == "serious")

    def filtered(self) -> list[dict]:
        """Only critical + serious — matches original behavior."""
        return [v for v in self.violations if v.get("impact") in ("critical", "serious")]


async def check_one(context, page_url: str) -> PageA11y:
    result = PageA11y(page_url=page_url)
    page = await context.new_page()
    try:
        resp = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="load")
        if resp is None or resp.status >= 400:
            result.error = f"status={resp.status if resp else 'no_response'}"
            return result
        # Let lazy components settle so axe sees the final DOM.
        await page.wait_for_timeout(1500)
        # Inject axe-core from CDN.
        try:
            await page.add_script_tag(url=AXE_CDN)
        except Exception as exc:
            result.error = f"axe_inject_failed: {exc!r}"
            return result
        data = await page.evaluate(AXE_RUN_SCRIPT, EXCLUDED_RULES)
        if "error" in data:
            result.error = data["error"]
            return result
        result.violations = data.get("violations", [])
        result.passes_count = data.get("passes_count", 0)
        result.incomplete_count = data.get("incomplete_count", 0)
        return result
    except Exception as exc:
        result.error = repr(exc)
        return result
    finally:
        await page.close()


async def check_accessibility(page_urls: list[str]) -> dict:
    results: list[PageA11y] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT, locale="ro-RO",
            viewport={"width": 1366, "height": 768},
        )
        # DNS warm-up.
        warm = await context.new_page()
        try:
            await warm.goto(BASE_URL, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
            await dismiss_cookies(warm)
        except Exception:
            pass
        await warm.close()

        for url in page_urls:
            print(f"[a11y] {url}", flush=True)
            r = await check_one(context, url)
            if r.error:
                print(f"   [ERROR] {r.error[:120]}", flush=True)
            else:
                print(f"   crit={r.critical_count} serious={r.serious_count} "
                      f"passes={r.passes_count} incomplete={r.incomplete_count}",
                      flush=True)
            results.append(r)
        await context.close()
        await browser.close()

    total_critical = sum(r.critical_count for r in results)
    total_serious  = sum(r.serious_count for r in results)
    pages_with_issues = [r for r in results if r.filtered()]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "axe_version_url": AXE_CDN,
        "excluded_rules": EXCLUDED_RULES,
        "pages_checked": len(results),
        "pages_with_issues": len(pages_with_issues),
        "total_critical": total_critical,
        "total_serious": total_serious,
        "pages": [
            {
                "page_url": r.page_url,
                "critical": r.critical_count,
                "serious": r.serious_count,
                "passes": r.passes_count,
                "incomplete": r.incomplete_count,
                "violations": r.filtered(),
                "error": r.error,
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

    report = await check_accessibility(page_urls)
    out_path = ROOT / DATA_DIR / "accessibility_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"ACCESSIBILITY REPORT (axe-core WCAG 2 AA) — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:      {report['pages_checked']}")
    print(f"Pages with issues:  {report['pages_with_issues']}")
    print(f"Critical issues:    {report['total_critical']}")
    print(f"Serious issues:     {report['total_serious']}")
    print(f"Saved to:           {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

"""Placeholder content scanner — finds Lorem Ipsum, "[coming soon]", "TODO"
and other forgotten dev/staging text on production pages.

Ported from flaviuzh/bt-monitor and expanded with Romanian patterns
("de completat", "în lucru", "text de introdus", etc.) — for a Romanian
bank site these matter more than the English lorem ipsum.

A single match is enough to flag the page — once a reviewer sees the URL,
they can open it and decide.
"""
from __future__ import annotations

import asyncio
import json
import re
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


# Each entry: (pattern, severity, description)
PLACEHOLDER_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # Classic Lorem Ipsum
    (re.compile(r"lorem ipsum", re.IGNORECASE),         "high",   "Lorem Ipsum text on page"),
    (re.compile(r"dolor sit amet", re.IGNORECASE),       "high",   "Lorem Ipsum continuation"),
    (re.compile(r"consectetur adipiscing", re.IGNORECASE),"high",   "Lorem Ipsum continuation"),
    # Square-bracket placeholders
    (re.compile(r"\[placeholder\]", re.IGNORECASE),      "high",   "[placeholder] marker"),
    (re.compile(r"\[coming soon\]", re.IGNORECASE),      "medium", "[coming soon] marker"),
    (re.compile(r"\[insert (text|here|name)\]", re.IGNORECASE), "high", "[insert …] marker"),
    (re.compile(r"\[TBD\]", re.IGNORECASE),              "medium", "[TBD] marker"),
    # Dev markers leaked from source
    (re.compile(r"\bTODO\b\s*[:\-]", re.IGNORECASE),     "medium", "TODO marker"),
    (re.compile(r"\bFIXME\b", re.IGNORECASE),            "high",   "FIXME marker"),
    (re.compile(r"\bXXX\b\s*[:\-]"),                     "medium", "XXX marker"),
    # Romanian — these matter MORE for a Romanian bank
    (re.compile(r"de\s+completat", re.IGNORECASE),       "medium", "“de completat” marker (RO)"),
    (re.compile(r"de\s+adaugat", re.IGNORECASE),         "medium", "“de adaugat” marker (RO)"),
    (re.compile(r"î?n\s+lucru", re.IGNORECASE),          "medium", "“în lucru” marker (RO)"),
    (re.compile(r"text\s+de\s+introdus", re.IGNORECASE), "high",   "“text de introdus” placeholder (RO)"),
    (re.compile(r"continut\s+placeholder", re.IGNORECASE),"high",  "“continut placeholder” (RO)"),
    (re.compile(r"aici\s+vine\s+text", re.IGNORECASE),   "high",   "“aici vine text” placeholder (RO)"),
    # JS rendering bugs leaking into UI
    (re.compile(r"\[object Object\]"),                   "high",   "[object Object] — JS render bug"),
    (re.compile(r"\bundefined\b", re.IGNORECASE),        "low",    "Literal 'undefined' in visible text"),
    (re.compile(r"\bNaN\b"),                             "low",    "Literal 'NaN' in visible text"),
]


@dataclass
class PageContent:
    page_url: str
    matches: list[dict] = field(default_factory=list)
    body_chars: int = 0
    fetch_error: str | None = None


def find_placeholders(text: str) -> list[dict]:
    """Match each pattern at most once per page; return the bigger context."""
    out: list[dict] = []
    for pat, severity, desc in PLACEHOLDER_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        ctx = text[start:end].replace("\n", " ").strip()
        out.append({
            "marker": m.group(0),
            "description": desc,
            "severity": severity,
            "context": ctx,
        })
    return out


async def check_one(context, page_url: str) -> PageContent:
    out = PageContent(page_url=page_url)
    page = await context.new_page()
    try:
        resp = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
        await dismiss_cookies(page)
        # Let JS-rendered content (SPAs) finish so we see what the user actually sees.
        await page.wait_for_timeout(1500)
        if resp is None or resp.status >= 400:
            out.fetch_error = f"status={resp.status if resp else 'no_response'}"
            return out
        body = await page.eval_on_selector("body", "el => el.innerText") if await page.query_selector("body") else ""
        body = body or ""
        out.body_chars = len(body)
        out.matches = find_placeholders(body)
        return out
    except Exception as exc:
        out.fetch_error = repr(exc)
        return out
    finally:
        await page.close()


async def check_content(page_urls: list[str]) -> dict:
    results: list[PageContent] = []
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
            print(f"[content] {url}", flush=True)
            r = await check_one(context, url)
            if r.fetch_error:
                print(f"   [ERROR] {r.fetch_error[:120]}", flush=True)
            else:
                print(f"   body={r.body_chars} chars, matches={len(r.matches)}"
                      + (f"  -> {', '.join(m['marker'] for m in r.matches[:3])}" if r.matches else ""),
                      flush=True)
            results.append(r)
        await context.close()
        await browser.close()

    pages_with_issues = [r for r in results if r.matches]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "pages_checked": len(results),
        "pages_with_issues": len(pages_with_issues),
        "total_matches": sum(len(r.matches) for r in results),
        "patterns_count": len(PLACEHOLDER_PATTERNS),
        "pages": [
            {
                "page_url": r.page_url,
                "body_chars": r.body_chars,
                "matches": r.matches,
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

    report = await check_content(page_urls)
    out_path = ROOT / DATA_DIR / "content_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"CONTENT PLACEHOLDER REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:     {report['pages_checked']}")
    print(f"Pages with issues: {report['pages_with_issues']}")
    print(f"Total matches:     {report['total_matches']}")
    print(f"Saved to:          {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

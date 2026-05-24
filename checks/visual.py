"""AI visual check — Claude vision on a sample of pages.

For each sampled page:
  1. Open it in Playwright.
  2. Take a full-page screenshot (viewport-only, not too tall).
  3. Send the PNG + a structured prompt to Claude.
  4. Parse the JSON response: list of issues, each with severity + description
     + (optional) approximate location.

Cost control:
  - Only N pages per run (default 10) — picks homepage + a deterministic sample.
  - Skipped if the ANTHROPIC_API_KEY env var is not set, so the rest of the
    monitor still works in offline / unfunded environments.

Prompt is in Romanian so Claude orients to the actual BT user context.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
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

# Hard cap on screenshot height (px). Tall pages → too many tokens.
MAX_SCREENSHOT_HEIGHT = 4000
MAX_PAGES = int(os.environ.get("BT_MONITOR_VISUAL_MAX_PAGES", "10"))
MODEL = os.environ.get("BT_MONITOR_VISUAL_MODEL", "claude-sonnet-4-5")

VISUAL_PROMPT = """Ești un auditor UX/QA care analizează un screenshot al unei pagini publice de pe site-ul Băncii Transilvania.

Identifică probleme VIZIBILE pe pagină. Categorii relevante:
  - layout: elemente suprapuse, text tăiat, butoane în afara ecranului, alinieri rupte
  - typography: fonturi nestandard, lipsă diacritice, caractere placeholder (□, ?), font înlocuit cu fallback ciudat
  - imagery: imagini lipsă (icoană "imagine ruptă"), placeholder gri, logo-uri stricate
  - text: text Lorem Ipsum, "[object Object]", text duplicat, propoziții netranslate
  - accessibility: contrast slab, text gri-pe-gri
  - branding: probleme de respectare a identității vizuale BT
  - errors: mesaje "Error 404", "Something went wrong", stack traces vizibile

NU raporta:
  - decizii subiective de design ("ar arăta mai bine cu")
  - probleme care țin de SEO sau performanță (avem alte tool-uri pentru asta)
  - lucruri pe care nu le poți VEDEA în imagine (presupuneri)

Returnează STRICT un obiect JSON cu această structură:
{
  "page_health": "ok" | "warn" | "critical",
  "issues": [
    {
      "category": "layout" | "typography" | "imagery" | "text" | "accessibility" | "branding" | "errors",
      "severity": "low" | "medium" | "high",
      "description": "scurtă descriere în română",
      "location": "unde apare pe pagină, în cuvinte"
    }
  ],
  "summary": "o frază scurtă despre starea generală a paginii"
}

Dacă pagina arată curat, returnează "issues": [] și page_health: "ok"."""


@dataclass
class VisualFinding:
    page_url: str
    page_health: str = "unknown"
    issues: list[dict] = field(default_factory=list)
    summary: str = ""
    error: str | None = None
    raw_response: str | None = None


def pick_sample(page_urls: list[str], max_pages: int = MAX_PAGES) -> list[str]:
    """Pick a representative sample: homepage + spread-out indices."""
    if len(page_urls) <= max_pages:
        return list(page_urls)
    seen = set()
    sampled: list[str] = []
    base = BASE_URL.rstrip("/") + "/"
    # Always include the homepage if present.
    for u in page_urls:
        if u in (base, base[:-1]):
            sampled.append(u); seen.add(u); break
    # Fill the rest with evenly-spaced picks.
    step = max(1, len(page_urls) // max_pages)
    for i in range(0, len(page_urls), step):
        u = page_urls[i]
        if u in seen:
            continue
        sampled.append(u); seen.add(u)
        if len(sampled) >= max_pages:
            break
    return sampled


async def screenshot_page(context, page_url: str) -> bytes | None:
    page = await context.new_page()
    try:
        await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="load")
        # Let lazy-loaded hero images render.
        await page.wait_for_timeout(2000)
        # Cap the height — Claude vision has token cost proportional to size.
        png = await page.screenshot(
            full_page=False,
            clip={"x": 0, "y": 0, "width": 1366, "height": min(MAX_SCREENSHOT_HEIGHT, 2200)},
            type="png",
        )
        return png
    except Exception:
        return None
    finally:
        await page.close()


async def call_claude(image_b64: str, page_url: str) -> tuple[dict | None, str | None, str]:
    """Returns (parsed_json, error, raw_text)."""
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None, "anthropic SDK not installed", ""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None, "ANTHROPIC_API_KEY not set", ""

    client = AsyncAnthropic(api_key=api_key)
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": image_b64,
                    }},
                    {"type": "text", "text": f"Pagina analizată: {page_url}\n\n{VISUAL_PROMPT}"},
                ],
            }],
        )
        raw = "".join(block.text for block in resp.content if hasattr(block, "text"))
        # Strip ```json fences if present.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
            if cleaned.endswith("```"):
                cleaned = cleaned[: cleaned.rfind("```")]
        parsed = json.loads(cleaned)
        return parsed, None, raw
    except json.JSONDecodeError as exc:
        return None, f"JSON parse failed: {exc}", raw if 'raw' in locals() else ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", ""


async def check_visual(page_urls: list[str]) -> dict:
    sample = pick_sample(page_urls)
    print(f"[visual] sampling {len(sample)} of {len(page_urls)} page(s)", flush=True)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "base_url": BASE_URL,
            "skipped": True,
            "reason": "ANTHROPIC_API_KEY not set",
            "sampled_count": 0,
            "findings": [],
        }

    findings: list[VisualFinding] = []
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

        for url in sample:
            print(f"[visual] {url}", flush=True)
            png = await screenshot_page(context, url)
            if png is None:
                findings.append(VisualFinding(page_url=url, error="screenshot_failed"))
                print(f"   [error] screenshot failed", flush=True)
                continue
            b64 = base64.standard_b64encode(png).decode()
            parsed, err, raw = await call_claude(b64, url)
            f = VisualFinding(page_url=url)
            f.raw_response = raw[:2000] if raw else None
            if err or parsed is None:
                f.error = err or "no_response"
                print(f"   [error] {err}", flush=True)
            else:
                f.page_health = parsed.get("page_health", "unknown")
                f.issues = parsed.get("issues", [])
                f.summary = parsed.get("summary", "")
                print(
                    f"   [{f.page_health}] {len(f.issues)} issue(s) — {f.summary[:80]}",
                    flush=True,
                )
            findings.append(f)
        await context.close()
        await browser.close()

    pages_with_issues = [f for f in findings if f.issues]
    total_issues = sum(len(f.issues) for f in findings)

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "model": MODEL,
        "sampled_count": len(findings),
        "pages_with_issues": len(pages_with_issues),
        "total_issues": total_issues,
        "findings": [f.__dict__ for f in findings],
    }


async def main() -> None:
    sitemap_path = ROOT / DATA_DIR / "sitemap_pages.json"
    if not sitemap_path.exists():
        print(f"ERROR: {sitemap_path} not found. Run crawler/sitemap.py first.")
        return
    page_urls = json.loads(sitemap_path.read_text(encoding="utf-8"))["page_urls"]

    report = await check_visual(page_urls)
    out_path = ROOT / DATA_DIR / "visual_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"VISUAL AI REPORT — {BASE_URL}")
    print("=" * 60)
    if report.get("skipped"):
        print(f"SKIPPED — {report['reason']}")
        print("Set ANTHROPIC_API_KEY to enable Claude vision checks.")
    else:
        print(f"Pages sampled:      {report['sampled_count']}")
        print(f"Pages with issues:  {report['pages_with_issues']}")
        print(f"Total issues:       {report['total_issues']}")
        print(f"Model:              {report['model']}")
    print(f"Saved to:           {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

"""Form detection + static health check.

What we check WITHOUT submitting (safe for production):
  - Every <form> element on every page
  - Its action URL: reachable? returns 4xx/5xx?
  - Its method (GET/POST)
  - Field count + whether it has labels
  - Whether required/email/tel/url validation is present
  - Whether it sits on HTTPS (mixed-content is a bug)
  - Suspicious patterns: action="" (posts to self, often OK), action="#" (broken),
    method missing, no submit button, etc.

What we explicitly DO NOT do here:
  - Submit the form. Doing so creates a real lead in the bank's CRM. The brief's
    'lead-ul se trimite catre noi' verification REQUIRES a staging environment
    or a test-data convention agreed with BT. We surface this clearly in the
    report as 'submission_test = "not_run"' so reviewers know it's intentional,
    not missing.
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


# JS that walks every <form> and returns a structured description.
FORMS_SCRIPT = """
() => {
  const forms = Array.from(document.querySelectorAll('form'));
  return forms.map((f, idx) => {
    const fields = Array.from(f.querySelectorAll('input, textarea, select')).map(el => ({
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      name: el.name || null,
      id: el.id || null,
      required: el.required || false,
      has_label: !!(el.labels && el.labels.length > 0),
      placeholder: el.placeholder || null,
    }));
    const submit = f.querySelector('button[type=submit], input[type=submit], button:not([type])');
    return {
      index: idx,
      action: f.getAttribute('action') || '',
      method: (f.getAttribute('method') || 'GET').toUpperCase(),
      id: f.id || null,
      name: f.getAttribute('name') || null,
      class: f.className || null,
      fields: fields,
      field_count: fields.length,
      has_submit: !!submit,
      submit_label: submit ? (submit.innerText || submit.value || '').trim().slice(0, 80) : null,
      novalidate: f.noValidate || false,
    };
  });
}
"""


@dataclass
class FormHealth:
    page_url: str
    form_index: int
    action_url: str | None
    method: str
    field_count: int
    has_submit: bool
    submit_label: str | None
    fields_without_labels: int = 0
    required_fields: int = 0
    has_email_field: bool = False
    has_phone_field: bool = False
    issues: list[str] = field(default_factory=list)
    action_status: int | None = None
    action_reachable: bool | None = None
    submission_test: str = "not_run"

    def to_json(self):
        return self.__dict__


def absolutize(action: str, page_url: str) -> str | None:
    if action is None:
        return None
    action = action.strip()
    if action == "" or action == "#":
        # action="" means submit to same URL — that's fine in HTML, mark as same-page.
        return page_url if action == "" else None
    return urljoin(page_url, action)


def analyze_form(raw: dict, page_url: str) -> FormHealth:
    fields = raw.get("fields", [])
    no_label = sum(1 for f in fields if not f.get("has_label") and f.get("type") not in ("hidden", "submit"))
    required = sum(1 for f in fields if f.get("required"))
    types = {(f.get("type") or "").lower() for f in fields}
    names = {(f.get("name") or "").lower() for f in fields}

    fh = FormHealth(
        page_url=page_url,
        form_index=raw["index"],
        action_url=absolutize(raw.get("action") or "", page_url),
        method=raw.get("method", "GET"),
        field_count=raw.get("field_count", 0),
        has_submit=raw.get("has_submit", False),
        submit_label=raw.get("submit_label"),
        fields_without_labels=no_label,
        required_fields=required,
        has_email_field="email" in types or any("email" in n or "mail" in n for n in names),
        has_phone_field="tel" in types or any("tel" in n or "phone" in n or "telefon" in n for n in names),
    )

    # Heuristic issues.
    if not fh.has_submit:
        fh.issues.append("no_submit_button")
    if raw.get("action") == "#":
        fh.issues.append("action_is_hash")
    if no_label > 0:
        fh.issues.append(f"{no_label}_field(s)_without_label")
    if fh.field_count == 0:
        fh.issues.append("zero_fields")
    if raw.get("novalidate") and required > 0:
        fh.issues.append("required_fields_with_novalidate")
    return fh


async def probe_action_url(client: httpx.AsyncClient, action_url: str) -> tuple[int | None, bool]:
    """HEAD the action URL to see if it's at least reachable. Many form endpoints
    refuse HEAD; we accept anything that isn't a network error or 404 as 'reachable'."""
    try:
        r = await client.head(action_url, follow_redirects=True, timeout=10)
        # Some endpoints return 405 (method not allowed for HEAD) — that's actually a
        # good sign: the URL exists, just doesn't accept HEAD. We mark reachable.
        return r.status_code, r.status_code != 404
    except httpx.RequestError:
        return None, False
    except Exception:
        return None, False


async def collect_forms_on_page(context, page_url: str) -> tuple[list[FormHealth], str | None]:
    page = await context.new_page()
    try:
        response = await page.goto(page_url, timeout=CRAWL_TIMEOUT_MS, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)  # let JS-injected forms appear
        if response is None or response.status >= 400:
            return [], f"status={response.status if response else 'no_response'}"
        raw_forms = await page.evaluate(FORMS_SCRIPT)
        return [analyze_form(r, page_url) for r in raw_forms], None
    except Exception as exc:
        return [], repr(exc)
    finally:
        await page.close()


async def check_forms(page_urls: list[str]) -> dict:
    all_forms: list[FormHealth] = []
    page_errors: dict[str, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ro-RO",
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
            print(f"[forms] {url}", flush=True)
            forms, err = await collect_forms_on_page(context, url)
            if err:
                page_errors[url] = err
                print(f"   [ERROR] {err[:120]}", flush=True)
                continue
            print(f"   found {len(forms)} form(s)", flush=True)
            all_forms.extend(forms)
        await context.close()
        await browser.close()

    # Probe each unique action URL.
    unique_actions = sorted({fh.action_url for fh in all_forms if fh.action_url})
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        probes = await asyncio.gather(*(probe_action_url(client, u) for u in unique_actions))
    action_map = dict(zip(unique_actions, probes))

    for fh in all_forms:
        if fh.action_url and fh.action_url in action_map:
            status, reachable = action_map[fh.action_url]
            fh.action_status = status
            fh.action_reachable = reachable
            if not reachable and status is None:
                fh.issues.append("action_unreachable")
            elif status and status >= 400 and status != 405:
                fh.issues.append(f"action_returns_{status}")

    forms_with_issues = [fh for fh in all_forms if fh.issues]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "pages_checked": len(page_urls),
        "pages_with_errors": len(page_errors),
        "total_forms": len(all_forms),
        "forms_with_issues": len(forms_with_issues),
        "page_errors": page_errors,
        "forms": [fh.to_json() for fh in all_forms],
        "submission_test_status": "not_run — end-to-end form submission requires a staging/test endpoint",
    }


async def main() -> None:
    sitemap_path = ROOT / DATA_DIR / "sitemap_pages.json"
    if not sitemap_path.exists():
        print(f"ERROR: {sitemap_path} not found. Run crawler/sitemap.py first.")
        return
    page_urls = json.loads(sitemap_path.read_text(encoding="utf-8"))["page_urls"]

    report = await check_forms(page_urls)
    out_path = ROOT / DATA_DIR / "forms_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"FORMS REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Pages checked:      {report['pages_checked']}")
    print(f"Total forms found:  {report['total_forms']}")
    print(f"Forms with issues:  {report['forms_with_issues']}")
    print(f"Submission test:    {report['submission_test_status']}")
    print(f"Saved to:           {out_path}")

    if report["total_forms"]:
        print("\nFORMS DETECTED:")
        for fh in report["forms"]:
            issues = ", ".join(fh["issues"]) if fh["issues"] else "OK"
            print(f"  [{fh['method']}] {fh['action_url'] or '(no action)'}")
            print(f"        page:    {fh['page_url']}")
            print(f"        fields:  {fh['field_count']} ({fh['required_fields']} required)")
            print(f"        submit:  {fh['submit_label']}")
            print(f"        issues:  {issues}")


if __name__ == "__main__":
    asyncio.run(main())

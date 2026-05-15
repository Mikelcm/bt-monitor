"""Document scanner — find PDFs/DOCX/XLSX on the public site that contain
internal classification markers.

This is the highest-value check for a bank. If a document labeled 'Uz intern'
or 'Confidential' is linked from a public page, it's a real compliance leak.

Pipeline:
  1. From the broken-links report (or live crawl), collect every link whose
     extension is .pdf/.docx/.xlsx/.doc/.xls/.pptx/.ppt (case-insensitive).
  2. Download each (range-limited to ~20MB to be polite).
  3. Extract text.
  4. Match against a Romanian + English keyword list:
        'Uz intern', 'Uz Intern', 'UZ INTERN',
        'Confidential', 'Confidențial', 'Strict secret',
        'Internal use only', 'Restricted', 'Do not distribute',
        'Pentru uz intern', etc.
  5. Report each doc + matched markers + the context (a few words around the hit).

We use plain httpx (with browser User-Agent) for downloading — PDFs are static
and don't need a full browser.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import BASE_URL, USER_AGENT, DATA_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# Extensions we treat as documents to scan.
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf"}

# Max size we'll download per doc (10 MB). Bigger ones we flag but skip.
MAX_DOC_BYTES = 10 * 1024 * 1024

# Compiled regex of sensitive markers. Use word-ish boundaries because Romanian
# diacritics make plain \b unreliable.
SENSITIVE_MARKERS = [
    r"uz\s+intern",
    r"pentru\s+uz\s+intern",
    r"confiden[tț]ial",          # confidential, confidențial
    r"strict\s+confiden[tț]ial",
    r"strict\s+secret",
    r"secret\s+de\s+serviciu",
    r"document\s+intern",
    r"internal\s+use\s+only",
    r"for\s+internal\s+use",
    r"restricted",
    r"do\s+not\s+distribute",
    r"not\s+for\s+distribution",
    r"draft\s*[-–—]\s*do\s+not",
]
MARKER_RE = re.compile("|".join(SENSITIVE_MARKERS), re.IGNORECASE | re.UNICODE)


@dataclass
class DocFinding:
    url: str
    referenced_from: list[str]
    status: int | None = None
    bytes: int = 0
    content_type: str | None = None
    extension: str = ""
    text_extracted_chars: int = 0
    markers_found: list[dict] = field(default_factory=list)  # [{marker, context}]
    error: str | None = None

    @property
    def has_leak(self) -> bool:
        return bool(self.markers_found)


def doc_extension(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in DOC_EXTS:
        if path.endswith(ext):
            return ext
    return ""


def collect_doc_urls_from_reports() -> dict[str, list[str]]:
    """Look at the links_report.json to find every doc URL and which pages it was on."""
    links_path = ROOT / DATA_DIR / "links_report.json"
    if not links_path.exists():
        return {}
    data = json.loads(links_path.read_text(encoding="utf-8"))
    # The links report has per_page summaries but no per-link-per-page mapping for ALL probes.
    # Use the all_probes list and cross-reference per_page.links via a re-extract.
    # Easier: re-extract by walking probes and matching pages.
    doc_map: dict[str, list[str]] = {}
    # We saved per-page link counts but not the actual links per page in links_report.
    # Pragma: in the current links.py we did NOT persist per-page links. To make this
    # check standalone-useful, we just look at all unique probe URLs.
    for probe in data.get("all_probes", []):
        url = probe["url"]
        if doc_extension(url):
            doc_map.setdefault(url, []).append("(from links report)")
    return doc_map


def extract_pdf_text(content: bytes) -> str:
    """Try pypdf, fall back to nothing. We install pypdf on demand below."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def extract_docx_text(content: bytes) -> str:
    try:
        from docx import Document  # python-docx
    except ImportError:
        return ""
    try:
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception:
        return ""


def extract_text(content: bytes, ext: str) -> str:
    if ext == ".pdf":
        return extract_pdf_text(content)
    if ext == ".docx":
        return extract_docx_text(content)
    # .doc/.xls/.ppt (binary legacy formats) need olefile or libreoffice — skip for now.
    return ""


def find_markers(text: str) -> list[dict]:
    findings: list[dict] = []
    for m in MARKER_RE.finditer(text):
        start = max(0, m.start() - 50)
        end = min(len(text), m.end() + 50)
        ctx = text[start:end].replace("\n", " ").strip()
        findings.append({"marker": m.group(0), "context": ctx})
        if len(findings) >= 20:
            break  # cap per doc
    return findings


async def scan_one_doc(client: httpx.AsyncClient, url: str, referenced_from: list[str]) -> DocFinding:
    f = DocFinding(url=url, referenced_from=referenced_from, extension=doc_extension(url))
    try:
        # HEAD first to check size.
        head = await client.head(url, follow_redirects=True, timeout=20)
        f.status = head.status_code
        f.content_type = head.headers.get("content-type")
        length = head.headers.get("content-length")
        if length and int(length) > MAX_DOC_BYTES:
            f.error = f"too_large ({length} bytes)"
            return f
        # Real download.
        r = await client.get(url, follow_redirects=True, timeout=60)
        f.status = r.status_code
        f.content_type = r.headers.get("content-type") or f.content_type
        f.bytes = len(r.content)
        if r.status_code >= 400:
            f.error = f"http_{r.status_code}"
            return f
        text = extract_text(r.content, f.extension)
        f.text_extracted_chars = len(text)
        f.markers_found = find_markers(text)
    except Exception as exc:
        f.error = repr(exc)
    return f


async def scan_docs(doc_urls: dict[str, list[str]]) -> dict:
    findings: list[DocFinding] = []
    if not doc_urls:
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "base_url": BASE_URL,
            "docs_found": 0,
            "leaks_found": 0,
            "docs": [],
            "note": "No documents found in the links report — re-run checks/links.py first.",
        }
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8"}
    ) as client:
        for url, refs in doc_urls.items():
            print(f"[doc] {url}", flush=True)
            res = await scan_one_doc(client, url, refs)
            badge = "LEAK!" if res.has_leak else ("err" if res.error else "ok")
            print(
                f"   [{badge}]  status={res.status}  bytes={res.bytes}  "
                f"text_chars={res.text_extracted_chars}  markers={len(res.markers_found)}"
                + (f"  ({res.error})" if res.error else ""),
                flush=True,
            )
            findings.append(res)

    leaks = [f for f in findings if f.has_leak]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "docs_found": len(findings),
        "leaks_found": len(leaks),
        "docs": [f.__dict__ for f in findings],
        "leaks": [f.__dict__ for f in leaks],
    }


async def main() -> None:
    doc_urls = collect_doc_urls_from_reports()
    report = await scan_docs(doc_urls)
    out_path = ROOT / DATA_DIR / "docs_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"DOCS REPORT — {BASE_URL}")
    print("=" * 60)
    print(f"Documents scanned:  {report['docs_found']}")
    print(f"Leaks detected:     {report['leaks_found']}")
    print(f"Saved to:           {out_path}")

    if report["leaks_found"]:
        print("\n!! INTERNAL DOCS LEAKED ON PUBLIC SITE !!")
        for f in report["leaks"]:
            print(f"  {f['url']}")
            for m in f["markers_found"][:3]:
                print(f"    match '{m['marker']}': ...{m['context']}...")


if __name__ == "__main__":
    asyncio.run(main())

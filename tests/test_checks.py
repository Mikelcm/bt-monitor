"""Deep-scan check tests: link doc-detection + HEAD->GET fallback (#17/#20),
doc-reference mapping consumed by the doc scanner (#17)."""
import json

from checks import links, docs


def test_is_doc_url():
    assert links._is_doc_url("https://x/report.PDF") is True
    assert links._is_doc_url("https://x/a.docx") is True
    assert links._is_doc_url("https://x/page.html") is False


def test_head_get_fallback_includes_400():
    # #20 — the fallback set must include 400 (some servers 400 on HEAD).
    import inspect
    src = inspect.getsource(links.probe_url)
    assert "400" in src and "405" in src


def test_doc_references_consumed(tmp_path, monkeypatch):
    # Write a links_report.json with the new doc_references mapping and confirm
    # the doc scanner reads the real referencing pages (#17).
    report = {
        "doc_references": {
            "https://x/secret.pdf": ["https://x/page-a", "https://x/page-b"],
        },
        "all_probes": [],
    }
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "links_report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(docs, "ROOT", tmp_path)
    monkeypatch.setattr(docs, "DATA_DIR", "data")
    out = docs.collect_doc_urls_from_reports()
    assert out["https://x/secret.pdf"] == ["https://x/page-a", "https://x/page-b"]

"""Local test fixture: a tiny Flask site with INTENTIONAL issues.

Every issue type from the brief is reproduced here so we can prove the
monitor catches it:

  /                       — homepage (clean, links to everything below)
  /clean-page             — a normal healthy page
  /broken-links           — page with several 404 anchors + a bad image
  /missing-assets         — page with broken image src + bad font URL
  /slow                   — page that takes 5 seconds to render (over threshold)
  /broken-form            — page with a form that posts to a 404 action URL
  /good-form              — a healthy form, posts to /submit (which 200s)
  /docs                   — page that links to two PDFs
  /static/uz-intern.pdf   — a PDF containing 'Uz intern' marker (THE LEAK)
  /static/public.pdf      — a clean PDF
  /sitemap.xml            — sitemap listing the public pages
  /robots.txt             — allow all

Run: python test_fixture/server.py
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

from flask import Flask, Response, render_template_string, send_file

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)

BASE = "http://127.0.0.1:8765"

# ---------- shared HTML helpers ----------
HEADER = """
<!doctype html>
<html lang="ro">
<head>
  <meta charset="utf-8">
  <title>{{ title }}</title>
  <link rel="stylesheet" href="{{ font }}">
  <style>body{font-family:system-ui;max-width:760px;margin:2rem auto;padding:0 1rem}
  nav a{margin-right:1rem}</style>
</head>
<body>
  <nav>
    <a href="/">home</a>
    <a href="/clean-page">clean</a>
    <a href="/broken-links">broken-links</a>
    <a href="/missing-assets">missing-assets</a>
    <a href="/slow">slow</a>
    <a href="/broken-form">broken-form</a>
    <a href="/good-form">good-form</a>
    <a href="/docs">docs</a>
  </nav>
  <hr>
"""
FOOTER = "</body></html>"


@app.route("/")
def home():
    body = """
    <h1>Test fixture — bt-monitor demo</h1>
    <p>Every section below is intentionally broken in one specific way.
       The monitor should catch all of them.</p>
    <ul>
      <li><a href="/clean-page">/clean-page</a> — should be 100% OK</li>
      <li><a href="/broken-links">/broken-links</a> — contains 404 anchors</li>
      <li><a href="/missing-assets">/missing-assets</a> — broken image + bad font</li>
      <li><a href="/slow">/slow</a> — 5-second server delay</li>
      <li><a href="/broken-form">/broken-form</a> — form posts to a 404</li>
      <li><a href="/good-form">/good-form</a> — healthy form</li>
      <li><a href="/docs">/docs</a> — links to a leaked internal PDF</li>
    </ul>
    """
    return render_template_string(HEADER + body + FOOTER, title="Home", font="/static/font-real.css")


@app.route("/clean-page")
def clean_page():
    body = """
    <h1>Clean page</h1>
    <p>This page has no intentional issues.</p>
    <img src="/static/image-real.svg" alt="real">
    """
    return render_template_string(HEADER + body + FOOTER, title="Clean", font="/static/font-real.css")


@app.route("/broken-links")
def broken_links():
    body = """
    <h1>Broken links</h1>
    <p>The links below intentionally lead to 404s:</p>
    <ul>
      <li><a href="/this-page-does-not-exist">/this-page-does-not-exist</a> (404)</li>
      <li><a href="/also-missing">/also-missing</a> (404)</li>
      <li><a href="/redirect-loop">/redirect-loop</a> (will redirect to itself a few times)</li>
      <li><a href="https://invalid-domain-just-for-test.bt-monitor.example/">invalid host</a> (DNS fail)</li>
    </ul>
    """
    return render_template_string(HEADER + body + FOOTER, title="Broken links", font="/static/font-real.css")


@app.route("/redirect-loop")
def redirect_loop():
    return Response(status=302, headers={"Location": "/redirect-loop"})


@app.route("/missing-assets")
def missing_assets():
    body = """
    <h1>Missing assets</h1>
    <p>This page references a missing image and a missing font:</p>
    <img src="/static/this-image-is-missing.png" alt="missing">
    <img src="/static/image-real.svg" alt="real">
    <p style="font-family: 'BrokenFont'">This paragraph requests a font that 404s.</p>
    """
    # NB: this page's stylesheet intentionally points at a 404 font file.
    return render_template_string(HEADER + body + FOOTER, title="Missing assets", font="/static/font-broken.css")


@app.route("/slow")
def slow():
    time.sleep(5)  # 5-second server delay — well over 3s threshold
    body = "<h1>Slow page</h1><p>This page took 5 seconds to respond.</p>"
    return render_template_string(HEADER + body + FOOTER, title="Slow page", font="/static/font-real.css")


@app.route("/broken-form")
def broken_form():
    body = """
    <h1>Broken form</h1>
    <p>This form posts to /this-endpoint-does-not-exist which 404s.</p>
    <form action="/this-endpoint-does-not-exist" method="POST">
      <input type="text" name="nume" placeholder="Nume">  <!-- no label, no required -->
      <input type="email" name="email" placeholder="Email">
      <input type="tel" name="telefon" placeholder="Telefon">
      <!-- no submit button on purpose -->
    </form>
    """
    return render_template_string(HEADER + body + FOOTER, title="Broken form", font="/static/font-real.css")


@app.route("/good-form")
def good_form():
    body = """
    <h1>Healthy form</h1>
    <p>This form is well-formed: labels, required fields, real submit, action is live.</p>
    <form action="/submit" method="POST">
      <p><label>Nume <input type="text" name="nume" required></label></p>
      <p><label>Email <input type="email" name="email" required></label></p>
      <p><label>Telefon <input type="tel" name="telefon"></label></p>
      <button type="submit">Trimite</button>
    </form>
    """
    return render_template_string(HEADER + body + FOOTER, title="Good form", font="/static/font-real.css")


@app.route("/submit", methods=["POST", "GET", "HEAD"])
def submit():
    return "<p>Thank-you page. Lead recorded.</p>"


@app.route("/docs")
def docs():
    body = """
    <h1>Documente</h1>
    <p>Două documente linkate de pe site:</p>
    <ul>
      <li><a href="/static/uz-intern.pdf">/static/uz-intern.pdf</a> (this one has 'Uz intern' inside!)</li>
      <li><a href="/static/public.pdf">/static/public.pdf</a> (this one is fine)</li>
    </ul>
    """
    return render_template_string(HEADER + body + FOOTER, title="Documente", font="/static/font-real.css")


@app.route("/sitemap.xml")
def sitemap():
    pages = ["/", "/clean-page", "/broken-links", "/missing-assets", "/slow",
             "/broken-form", "/good-form", "/docs"]
    urls = "".join(
        f"<url><loc>{BASE}{p}</loc><priority>0.7</priority></url>" for p in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}"
        "</urlset>"
    )
    return Response(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    return Response(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n", mimetype="text/plain")


# ---------- static files generated on the fly ----------

@app.route("/static/image-real.svg")
def image_real():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60" viewBox="0 0 120 60">'
        '<rect width="120" height="60" fill="#0066cc"/>'
        '<text x="60" y="36" font-size="20" text-anchor="middle" fill="white">OK</text>'
        '</svg>'
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/static/font-real.css")
def font_real():
    # Real stylesheet, points at a font that exists.
    return Response("/* clean stylesheet, no external font request */\nbody{line-height:1.5}",
                    mimetype="text/css")


@app.route("/static/font-broken.css")
def font_broken():
    # Stylesheet that requests a font file that returns 404.
    css = """
@font-face {
  font-family: 'BrokenFont';
  src: url('/static/this-font-is-missing.woff2') format('woff2');
}
body{line-height:1.5}
"""
    return Response(css, mimetype="text/css")


@app.route("/static/uz-intern.pdf")
def uz_intern_pdf():
    return send_file(io.BytesIO(_make_pdf(
        "Document confidential — Uz intern\n"
        "Banca Transilvania - politica internă - NU se distribuie public.\n"
        "Acest document este pentru uz intern al angajatilor."
    )), mimetype="application/pdf", download_name="uz-intern.pdf")


@app.route("/static/public.pdf")
def public_pdf():
    return send_file(io.BytesIO(_make_pdf(
        "Brosura publica - oferta clienti retail\n"
        "Acest document este destinat publicului larg.\n"
        "Vizitati www.bancatransilvania.ro pentru mai multe informatii."
    )), mimetype="application/pdf", download_name="public.pdf")


def _make_pdf(text: str) -> bytes:
    """Produce a tiny one-page PDF containing the given text, no extra deps required.

    This builds a valid minimal PDF by hand. Using only stdlib so we don't have to
    pull reportlab into requirements for the fixture.
    """
    # Escape the PDF text string (parens are special).
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    # Split into lines for the PDF content stream.
    lines = safe.split("\n")
    stream_parts = ["BT", "/F1 12 Tf", "50 750 Td"]
    for line in lines:
        stream_parts.append(f"({line}) Tj")
        stream_parts.append("0 -18 Td")
    stream_parts.append("ET")
    stream = "\n".join(stream_parts).encode("latin-1", errors="replace")

    objects = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
    )
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode())
        out.write(obj)
        out.write(b"\nendobj\n")
    xref_offset = out.tell()
    out.write(b"xref\n")
    out.write(f"0 {len(objects)+1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n")
    out.write(f"<< /Size {len(objects)+1} /Root 1 0 R >>\n".encode())
    out.write(b"startxref\n")
    out.write(f"{xref_offset}\n".encode())
    out.write(b"%%EOF")
    return out.getvalue()


if __name__ == "__main__":
    print(f"Test fixture running at {BASE}")
    print("Stop with Ctrl+C.")
    app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=False)

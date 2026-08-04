"""
HTML → PDF for the daily deals report.

Chromium (via Playwright) rather than WeasyPrint or PyMuPDF's Story API,
because the report's layout is email HTML: nested `<table role="presentation">`
scaffolding, inline styles, an inline SVG chart, and `border-collapse` tables.
WeasyPrint mangles the table scaffolding and needs GTK on Windows (so previews
would not render on the desk's own machine); PyMuPDF's Story supports too small
a CSS subset to be trusted with the masthead and the chart. Chromium renders
byte-for-byte what the desk sees in a browser preview.

The renderer is deliberately best-effort: a PDF failure must never cost the
morning email. `render_pdf` returns None on any failure and the caller sends
without the attachment.

Requires:
  pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger("nse.report.pdf")

# A4 with room for the 760px-wide email body. The body is fixed-width, so we
# scale rather than reflow — print_background keeps the parchment palette.
_PDF_OPTS = {
    "format": "A4",
    "print_background": True,
    "margin": {"top": "12mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
    "scale": 0.86,
    "prefer_css_page_size": False,
}

# Print-only overrides, targeting the class hooks emitted by reports/design.py.
#
# The screen layout centres a 640px card on a grey page. On paper that grey is
# wasted ink and the card is a narrow column down the middle of an A4 sheet, so
# print drops the frame and lets the card use the full measure.
#
# Break rules are deliberately narrow. An earlier version applied
# `page-break-inside: avoid` to every td, which in this markup means every
# *section wrapper* — so a section that did not fit in the remaining space moved
# to the next page whole and left half a page blank. Only data rows are
# protected; the layout cells must be free to break.
_PRINT_CSS = """
@page { size: A4; }
html, body {
  background: #ffffff !important;
  padding: 0 !important;
  margin: 0 !important;
}
/* Drop the screen page frame. */
.bac-page { background: #ffffff !important; }
.bac-page > tbody > tr > td { padding: 0 !important; }
.bac-card {
  width: 100% !important;
  max-width: 100% !important;
  border: none !important;
}
/* Layout cells break freely; only data rows stay intact. */
td, th { page-break-inside: auto; break-inside: auto; }
table.bac-data tr { page-break-inside: avoid; break-inside: avoid; }
/* Repeat the header of any table that spans a page boundary. */
table.bac-data thead { display: table-header-group; }
table.bac-data tbody { display: table-row-group; }
img, svg { max-width: 100% !important; }
a { text-decoration: none !important; }
"""


def render_pdf(html: str, *, timeout_ms: int = 60_000) -> bytes | None:
    """Renders report HTML to PDF bytes. Returns None if rendering is unavailable.

    Loads via a temp file:// document rather than set_content so relative
    anchors (`href="#symbols"`) and the inline SVG resolve exactly as they do
    in a browser.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning(
            "playwright not installed — skipping PDF attachment. "
            "Install with: pip install playwright && playwright install chromium"
        )
        return None

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".html", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(html)
            tmp_path = Path(fh.name)

        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.goto(tmp_path.as_uri(), wait_until="load", timeout=timeout_ms)
                page.add_style_tag(content=_PRINT_CSS)
                # The inline SVG and webfont-free Times stack need no network,
                # but give layout a beat to settle before measuring pages.
                page.wait_for_timeout(400)
                pdf = page.pdf(**_PDF_OPTS)
            finally:
                browser.close()

        logger.info("PDF rendered: %.1f KB", len(pdf) / 1024)
        return pdf

    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF rendering failed (%s) — email will send without it", exc)
        return None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

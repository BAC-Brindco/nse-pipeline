"""
BAC house design system for emailed reports.

Transcribed from the morning brief, which is the reference implementation:
  D:/bac_morning_brief/config/design.yaml         — palette, type scale
  D:/bac_morning_brief/templates/email_dashboard.html.j2 — component markup

Every BAC report imports this module so the house style is shared rather than
copied. A colour or size literal in a report's own source is a bug: table amber
and chart amber must be the same hex by construction, not by discipline.

Three rules inherited from the reference implementation, each of which exists
because Outlook's Word rendering engine broke the obvious approach:

  1. Spacing lives on <td> padding, never on a <div> or <p>. Word drops padding
     on both, so a layout spaced with divs renders correctly in every browser
     and arrives in Outlook with every gap collapsed to nothing.
  2. Every line-height is paired with mso-line-height-rule:exactly. Without it
     Word substitutes its own leading and the vertical rhythm goes.
  3. The document declares `color-scheme: light only`. Outlook and Apple Mail
     otherwise invert text colours in dark mode while honouring explicit
     backgrounds, which leaves dark text on a light panel — invisible.

Semantic colour is reserved: GOOD, BAD and WARN are the only colours that carry
meaning. Everything else is structure. Amber is invisible in greyscale and to a
red-green colour-blind reader, so it never carries meaning alone.

Two class hooks — `bac-page`/`bac-card` on the shell and `bac-data` on tables —
exist solely so the PDF renderer can target the layout in print CSS. Every style
that matters is still inline, because a mail client may drop the classes and must
not depend on them.
"""

from __future__ import annotations

from html import escape as _e

# ── Palette ──────────────────────────────────────────────────────────────────
# Structure
INK         = "#14181D"
INK_SOFT    = "#48515C"
INK_FAINT   = "#7C858F"
NAVY        = "#12304F"   # primary: mastheads, table heads, structural rules
NAVY_SOFT   = "#2C5480"   # secondary: meters, informational callout edge
GOLD        = "#9A7B2F"   # accent: kickers, section captions, callout edge
GOLD_PALE   = "#F3EEE2"   # accent callout fill
GOLD_RULE   = "#E2D9C2"   # divider inside an accent callout
RULE        = "#DBE0E6"
RULE_STRONG = "#A9B2BC"
ROW_RULE    = "#E8ECEF"   # between table body rows — lighter than RULE
BAND        = "#F6F8FA"   # panel fill, table head fill
BAND_SOFT   = "#FAFBFC"   # zebra banding, ≤3% ink
PAPER       = "#FFFFFF"
PAGE        = "#EEF1F4"   # the surface the card sits on
CARD_BORDER = "#D3D9DF"

# Semantic — the only colours that mean anything
GOOD = "#14664A"
BAD  = "#A32B22"
WARN = "#8A5A00"

# Chart series, in order. Brand-neutral and separable in greyscale by order, so
# a forwarded black-and-white print still reads. Same values the morning brief's
# matplotlib theme is generated from.
SERIES = ["#1F3A5F", "#B4762B", "#3E7C6A", "#7A4A6E", "#5C6670"]

# ── Typography ───────────────────────────────────────────────────────────────
# One family. The print brief embeds Tinos (metrically identical, OFL licensed);
# email cannot rely on an embedded face, so it names the metric it matches.
FONT = "'Times New Roman', Times, serif"

# ── Glyphs ───────────────────────────────────────────────────────────────────
EM_DASH   = "&mdash;"     # the ONLY thing a missing value ever prints
DIAMOND   = "&#9670;"
UP        = "&#9650;"
DOWN      = "&#9660;"
FLAT      = "="
MIDDOT    = "&middot;"

# ── Geometry ─────────────────────────────────────────────────────────────────
CARD_WIDTH = 640
PAD_X      = 24           # card side padding
SECTION_PAD = f"34px {PAD_X}px 20px {PAD_X}px"
BLOCK_PAD   = f"13px {PAD_X}px 0 {PAD_X}px"


def lh(px: float) -> str:
    """A line-height paired with the Word directive that makes it stick."""
    return f"line-height:{px}px;mso-line-height-rule:exactly;"


def font(size: float, *, color: str = INK, weight: str | None = None,
         ls: float | None = None, upper: bool = False,
         italic: bool = False, leading: float | None = None) -> str:
    """One type style. Always emits the family so no cell inherits Word's default."""
    css = f"font-family:{FONT};font-size:{size}px;color:{color};"
    if weight:
        css += f"font-weight:{weight};"
    if ls is not None:
        css += f"letter-spacing:{ls}px;"
    if upper:
        css += "text-transform:uppercase;"
    if italic:
        css += "font-style:italic;"
    if leading is not None:
        css += lh(leading)
    return css


# ── Document shell ───────────────────────────────────────────────────────────

def doc_open(title: str, preheader: str = "") -> str:
    """Doctype through the opening of the card table.

    XHTML 1.0 Transitional, not HTML5: Word's engine is most predictable
    against it, and it is what the reference implementation ships.
    """
    pre = ""
    if preheader:
        # Zero-width non-joiners stop the client padding the preview with body text.
        pre = (
            f'<div style="display:none;font-size:1px;color:{PAGE};line-height:1px;'
            f'max-height:0;max-width:0;opacity:0;overflow:hidden;">{preheader}'
            + "&nbsp;&zwnj;" * 12
            + "</div>\n"
        )
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="x-apple-disable-message-reformatting" />
<meta name="color-scheme" content="light only" />
<meta name="supported-color-schemes" content="light only" />
<title>{title}</title>
<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:{PAGE};-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">
{pre}
<table role="presentation" class="bac-page" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{PAGE};">
<tr><td align="center" style="padding:18px 8px 26px 8px;">

<table role="presentation" class="bac-card" width="{CARD_WIDTH}" cellpadding="0" cellspacing="0" border="0" style="width:{CARD_WIDTH}px;max-width:{CARD_WIDTH}px;background-color:{PAPER};border:1px solid {CARD_BORDER};">
"""


DOC_CLOSE = """
</table>
</td></tr>
</table>
</body>
</html>"""


def row(content: str, pad: str = SECTION_PAD) -> str:
    """A card row. Padding on the cell — see rule 1 in the module docstring."""
    return f'  <tr><td style="padding:{pad};">\n{content}\n  </td></tr>\n'


# ── Components ───────────────────────────────────────────────────────────────

def masthead(*, kicker: str, title: str, dateline: str,
             subline: str = "", scope: str = "") -> str:
    """Gold kicker, navy title, dateline, optional justified scope paragraph."""
    out = (
        f'  <tr><td style="padding:22px {PAD_X}px 15px {PAD_X}px;'
        f'border-top:4px solid {NAVY};">\n'
        f'    <div style="{font(10.5, color=GOLD, weight="bold", ls=2, upper=True)}">{kicker}</div>\n'
        f'    <div style="{font(32.5, color=NAVY, weight="bold", leading=35.5)}'
        f'padding-top:3px;">{title}</div>\n'
        f'    <div style="{font(14)}padding-top:6px;">{dateline}</div>\n'
    )
    if subline:
        out += f'    <div style="{font(11.5, color=INK_SOFT)}padding-top:2px;">{subline}</div>\n'
    if scope:
        # A nested table, not a div: the padding above it has to survive Word.
        out += (
            f'    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">\n'
            f'      <tr><td style="{font(11.5, color=INK_FAINT, leading=17.5)}'
            f'text-align:justify;padding:16px 0 0 0;">{scope}</td></tr>\n'
            f'    </table>\n'
        )
    return out + "  </td></tr>\n"


def caption_title(text: str) -> str:
    """The gold uppercase label that heads a table, chart or panel."""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="{font(10.5, color=GOLD, weight="bold", ls=1.6, upper=True)}'
        f'padding:0 0 14px 0;">{text}</td></tr></table>'
    )


def kpi_grid(cards: list[dict], per_row: int = 3) -> str:
    """Bordered value cards. Each dict: label, value, sub, optional flag.

    flag in {good, bad} colours the value; anything else stays navy. A card is
    structure, so its border and label never take a semantic colour.
    """
    if not cards:
        return ""
    rows = ""
    for i in range(0, len(cards), per_row):
        chunk = cards[i:i + per_row]
        cells = ""
        for k in chunk:
            color = {"good": GOOD, "bad": BAD}.get(k.get("flag"), NAVY)
            cells += (
                f'<td width="{100 // per_row}%" style="width:{100 / per_row:.2f}%;'
                f'padding:9px 10px;border:1px solid {RULE};vertical-align:top;">'
                f'<div style="{font(9.5, color=INK_FAINT, weight="bold", ls=1.1, upper=True)}">'
                f'{k["label"]}</div>'
                f'<div style="{font(23, color=color, weight="bold", leading=26.5)}'
                f'padding-top:3px;">{k["value"]}</div>'
                f'<div style="{font(10.5, color=INK_SOFT)}padding-top:2px;">'
                f'{k.get("sub", "")}</div>'
                f'</td>'
            )
        # Pad the last row so the borders line up with the rows above it.
        for _ in range(per_row - len(chunk)):
            cells += f'<td width="{100 // per_row}%" style="border:1px solid {RULE};"></td>'
        rows += f"<tr>{cells}</tr>"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="border-collapse:collapse;">{rows}</table>'
    )


def callout(body: str, *, accent: str = "gold", title: str = "") -> str:
    """A filled panel with a 4px left bar. gold = editorial, navy = operational."""
    fill, edge = (GOLD_PALE, GOLD) if accent == "gold" else (BAND, NAVY_SOFT)
    inner = caption_title(title) if title else ""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:{fill};border-left:4px solid {edge};">'
        f'<tr><td style="padding:12px 14px;">{inner}'
        f'<div style="{font(12.5, leading=18.5)}text-align:justify;">{body}</div>'
        f'</td></tr></table>'
    )


def numbered_list(items: list[tuple[str, str]]) -> str:
    """Gold-numbered editorial items: [(headline, body), ...]."""
    rows = ""
    for i, (head, body) in enumerate(items):
        top = "15px" if i else "0"
        border = f"border-top:1px solid {GOLD_RULE};" if i else ""
        rows += (
            f'<tr>'
            f'<td width="20" style="width:20px;{font(16, color=GOLD, weight="bold")}'
            f'vertical-align:top;padding:{top} 0 0 0;">{i + 1}</td>'
            f'<td style="{font(13, leading=19.5)}text-align:justify;'
            f'padding:{top} 0 {"0" if i == len(items) - 1 else "6px"} 0;{border}">'
            f'<strong>{head}</strong> {body}</td>'
            f'</tr>'
        )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{rows}</table>'
    )


def strip(pairs: list[tuple[str, str]]) -> str:
    """Label/value rows separated by hairlines — the Levels/Stance pattern."""
    rows = ""
    for label, val in pairs:
        if not val:
            continue
        rows += (
            f'<tr>'
            f'<td width="64" style="width:64px;{font(9.5, color=INK_FAINT, weight="bold", ls=1.2, upper=True)}'
            f'vertical-align:top;padding:6px 8px 0 0;border-top:1px solid {RULE};">{label}</td>'
            f'<td style="{font(11.5, leading=17.5)}padding:6px 0 0 0;'
            f'border-top:1px solid {RULE};">{val}</td>'
            f'</tr>'
        )
    if not rows:
        return ""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{rows}</table>'
    )


MAX_COLUMNS = 9   # R4 hard cap, inherited from the reference implementation


def datatable(
    columns: list[str],
    rows: list[list[str]],
    *,
    align: list[str] | None = None,
    title: str = "",
    source: str = "",
    caption: str = "",
    empty: str = "No rows.",
) -> str:
    """The house table: navy head on band, zebra body, mandatory source line.

    `align` takes one of l/c/r per column; c and r both centre, matching the
    reference implementation's numeric_align. Cell content is inserted as-is so
    callers can pass badges — callers escape their own text.
    """
    if len(columns) > MAX_COLUMNS:
        raise ValueError(
            f"{len(columns)} columns exceeds the house cap of {MAX_COLUMNS}"
        )

    head = caption_title(title) if title else ""
    if not rows:
        body = (
            f'<div style="{font(12, color=INK_FAINT, italic=True)}">{empty}</div>'
        )
        return f'{head}{body}'

    def _al(i: int) -> str:
        if align and i < len(align) and align[i] in ("r", "c"):
            return "center"
        return "left"

    ths = "".join(
        f'<th style="{font(9.5, color=NAVY, weight="bold", ls=0.8, upper=True)}'
        f'background-color:{BAND};border-top:2px solid {NAVY};'
        f'border-bottom:1px solid {RULE_STRONG};padding:7px 6px;'
        f'text-align:{_al(i)};">{c}</th>'
        for i, c in enumerate(columns)
    )

    trs = ""
    for r_i, r in enumerate(rows):
        band = PAPER if r_i % 2 == 0 else BAND_SOFT
        tds = "".join(
            f'<td style="{font(12, leading=16)}background-color:{band};'
            f'border-bottom:1px solid {ROW_RULE};padding:6px 6px;'
            f'text-align:{_al(i)};">{c}</td>'
            for i, c in enumerate(r)
        )
        trs += f"<tr>{tds}</tr>"

    # <thead> so the PDF renderer can repeat the header on every page a long
    # table spans; email clients treat it as an ordinary row group.
    out = (
        f'{head}'
        f'<table role="presentation" class="bac-data" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" style="border-collapse:collapse;">'
        f'<thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>'
    )
    if source:
        out += (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'<tr><td style="{font(10.5, color=INK_FAINT)}padding:18px 0 0 0;">'
            f'{source}</td></tr></table>'
        )
    if caption:
        out += (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'<tr><td style="{font(10, color=INK_SOFT, italic=True, leading=16)}'
            f'text-align:justify;padding:12px 0 0 0;">{caption}</td></tr></table>'
        )
    return out


def meter(pct: float, *, width: int = 132, full_is_good: bool = True) -> str:
    """A horizontal fill bar built from table cells — no images, no CSS bars."""
    pct = max(0.0, min(100.0, float(pct)))
    fill = GOOD if (full_is_good and pct >= 100) else NAVY_SOFT
    cells = ""
    if pct > 0:
        cells += (
            f'<td width="{pct:.0f}%" bgcolor="{fill}" '
            f'style="height:7px;font-size:0;line-height:0;">&nbsp;</td>'
        )
    if pct < 100:
        cells += (
            f'<td width="{100 - pct:.0f}%" bgcolor="{RULE}" '
            f'style="height:7px;font-size:0;line-height:0;">&nbsp;</td>'
        )
    return (
        f'<table role="presentation" width="{width}" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:{width}px;border-collapse:collapse;">'
        f'<tr>{cells}</tr></table>'
    )


def badge(text: str, color: str = NAVY) -> str:
    """A small outlined chip. Leading nbsp so it never welds to a ticker.

    vertical-align is deliberately omitted: Word honours only keyword values
    there, and a length silently invalidates the whole declaration.
    """
    return (
        f'&nbsp;<span style="{font(8.5, color=color, weight="bold", ls=0.6)}'
        f'border:1px solid {color};padding:0 3px;white-space:nowrap;">'
        f'{_e(text)}</span>'
    )


def value(text: str, flag: str | None = None) -> str:
    """A table value with optional semantic colour. None renders the em-dash."""
    if text is None or text == "":
        return f'<span style="color:{INK_FAINT};">{EM_DASH}</span>'
    color = {"good": GOOD, "bad": BAD, "warn": WARN, "stale": WARN}.get(flag)
    if not color:
        return text
    return f'<span style="color:{color};font-weight:bold;">{text}</span>'


def colophon(provenance: str, disclaimer: str) -> str:
    """Navy rule, provenance line, italic disclaimer."""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="border-top:2px solid {NAVY};{font(10.5, color=INK_FAINT, leading=15)}'
        f'padding:12px 0 0 0;">{provenance}</td></tr>'
        f'<tr><td style="{font(11, color=INK_SOFT, italic=True, leading=16)}'
        f'text-align:justify;padding:14px 0 0 0;">{disclaimer}</td></tr>'
        f'</table>'
    )

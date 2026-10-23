"""House style for every PDF the agents make: the same look as the HR forms (onboarding/forms, Oct 2026).

Logo top left (both logos when a document covers both companies), navy title, small uppercase subtitle, a 3 pt gold
rule under the header, navy uppercase section headings with a thin grey rule, navy table headers, and a footer line.
Colors: Snow White navy #003746 / gold #f9af10; The Laundry Group navy #003f50 / gold #ffb807. Font: Helvetica
(the forms use Liberation Sans/Arial, which looks the same).

Use:
    import brand
    doc = brand.document("file.pdf", "Routes and drivers", footer="Weekly routes report", landscape=True)
    story = brand.header("Routes and drivers", "Week of Mon 10/12 to Sun 10/18/2026", companies=("sw", "tlg"))
    story += brand.section("Snow White routes") + [brand.table(rows, widths)]
    doc.build(story)
Logos: logo_sw.png and logo_tlg.png, from the GitHub repo woodtang2000/repository, branch claude/admiring-planck-rdk120,
folder brand/ (git clone copies them exactly; text tools can't carry images safely). Put them beside this file or in
the current folder, or set BRAND_DIR. Each file is checked against LOGO_SHA256; a missing or damaged logo is replaced
by the company name in navy, so a PDF always builds. `python3 brand.py check` reports which logos were found.
"""
import hashlib
import os
import sys

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape as _landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import HRFlowable

COMPANIES = {
    "sw": {"name": "Snow White Inc", "brand": "Snow White Linen Supply", "navy": "#003746", "tint": "#eaf0f1",
           "gold": "#f9af10", "addr": "700 'I' Street · Anchorage, AK 99501", "ratio": 420 / 159},
    "tlg": {"name": "The Laundry Group LLC", "brand": "The Laundry Group", "navy": "#003f50", "tint": "#eaf0f2",
            "gold": "#ffb807", "addr": "300 E 5th Ave · Anchorage, AK 99501", "ratio": 520 / 177},
}
INK, MUTED, RULE = colors.HexColor("#1c2226"), colors.HexColor("#5a646b"), colors.HexColor("#c9d0d5")
NAVY, GOLD = colors.HexColor("#003746"), colors.HexColor("#f9af10")
FONT, BOLD = "Helvetica", "Helvetica-Bold"
rl_config.useA85 = 0  # binary streams: about 25% smaller PDFs (they are emailed as attachments)

BODY = ParagraphStyle("body", fontName=FONT, fontSize=10, leading=14, textColor=INK)
SMALL = ParagraphStyle("small", fontName=FONT, fontSize=8, leading=10, textColor=INK)
NOTE = ParagraphStyle("note", fontName=FONT, fontSize=8.5, leading=11, textColor=MUTED)
TITLE = ParagraphStyle("title", fontName=BOLD, fontSize=19, leading=22, textColor=NAVY)
SUB = ParagraphStyle("sub", fontName=FONT, fontSize=9, leading=12, textColor=MUTED)
H2 = ParagraphStyle("h2", fontName=BOLD, fontSize=10, leading=13, textColor=NAVY, spaceBefore=10)
H3 = ParagraphStyle("h3", fontName=BOLD, fontSize=9.5, leading=12, textColor=NAVY, spaceBefore=6)
TH = ParagraphStyle("th", fontName=BOLD, fontSize=7.5, leading=9, textColor=colors.white)


LOGO_SHA256 = {"sw": "252aa41fb8bdeb17fb1e68a52cdcbc7511f72ad37e990e1a6f0dd9860415c5f5",
               "tlg": "0e18d3443a6793f80dbfabbd9203e5080c5f5e38a697a5ba3b46d288bd2834af"}
LOGO_NAME = ParagraphStyle("logoname", fontName=BOLD, fontSize=13, leading=15, textColor=NAVY)


def logo_path(key):
    """Path of a verified logo file, or None."""
    for d in (os.environ.get("BRAND_DIR"), os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        p = os.path.join(d, f"logo_{key}.png") if d else None
        if p and os.path.exists(p) and hashlib.sha256(open(p, "rb").read()).hexdigest() == LOGO_SHA256[key]:
            return p
    return None


def logo(key, height=0.75 * inch):
    c = COMPANIES[key]
    p = logo_path(key)
    if p:
        # flatten the transparent background onto white: no soft mask, so the PDF stays small enough to email
        import io
        from PIL import Image as PILImage
        im = PILImage.open(p).convert("RGBA")
        flat = PILImage.new("RGB", im.size, "white")
        flat.paste(im, mask=im.split()[3])
        px = int(height / inch * 170)  # about 170 dpi: sharp in print, small in email
        if flat.size[1] > px:
            flat = flat.resize((round(flat.size[0] * px / flat.size[1]), px), PILImage.LANCZOS)
        flat = flat.quantize(colors=32).convert("RGB")  # few colors compress well inside the PDF
        buf = io.BytesIO()
        flat.save(buf, "PNG")
        buf.seek(0)
        return Image(buf, width=height * c["ratio"], height=height)
    t = Table([[Paragraph(c["brand"], LOGO_NAME)]], colWidths=[1.9 * inch], rowHeights=[height])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    t.drawWidth = 1.9 * inch
    return t


def P(text, style=SMALL):
    return Paragraph(text, style)


def header(title, subtitle="", companies=("sw",)):
    """Logo(s) + title block, then the gold rule. Returns a list of flowables."""
    logos = [logo(k, 0.62 * inch if len(companies) > 1 else 0.75 * inch) for k in companies]
    text = [Paragraph(title, TITLE)] + ([Paragraph(subtitle.upper(), SUB)] if subtitle else [])
    t = Table([logos + [text]], colWidths=[l.drawWidth + 14 for l in logos] + [None])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    t.hAlign = "LEFT"
    gold = colors.HexColor(COMPANIES[companies[0]]["gold"])
    return [t, Spacer(1, 8), HRFlowable(width="100%", thickness=3, color=gold, spaceBefore=0, spaceAfter=8)]


def section(text):
    """Navy uppercase heading with a thin grey rule (h2 on the HR forms)."""
    return [Paragraph(text.upper(), H2), HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=1, spaceAfter=5)]


def table(data, widths, total_row=False, navy=NAVY):
    """Navy header row (wraps), light banding, thin grid. Header cells may be plain strings."""
    data = [[c if not isinstance(c, str) else Paragraph(c, TH) for c in data[0]]] + list(data[1:])
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [("BACKGROUND", (0, 0), (-1, 0), navy), ("FONTNAME", (0, 1), (-1, -1), FONT), ("FONTSIZE", (0, 1), (-1, -1), 8),
          ("TEXTCOLOR", (0, 1), (-1, -1), INK), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6f7")])]
    if total_row:
        st += [("FONTNAME", (0, -1), (-1, -1), BOLD), ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK),
               ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaf0f1"))]
    t.setStyle(TableStyle(st))
    return t


def document(fname, title, footer="", landscape=False, companies=("sw", "tlg")):
    """SimpleDocTemplate with the HR-form margins and a footer line on every page."""
    size = _landscape(letter) if landscape else letter
    names = " · ".join(COMPANIES[k]["name"] for k in companies)

    def _foot(canvas, doc):
        canvas.saveState()
        w, _ = size
        canvas.setStrokeColor(RULE); canvas.setLineWidth(0.5)
        canvas.line(0.6 * inch, 0.42 * inch, w - 0.6 * inch, 0.42 * inch)
        canvas.setFont(FONT, 7.5); canvas.setFillColor(MUTED)
        canvas.drawString(0.6 * inch, 0.3 * inch, " · ".join(x for x in (names, footer) if x))
        canvas.drawRightString(w - 0.6 * inch, 0.3 * inch, f"Page {doc.page}")
        canvas.restoreState()

    d = SimpleDocTemplate(fname, pagesize=size, leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.55 * inch,
                          bottomMargin=0.6 * inch, title=title, author=names)
    d.build_ = d.build
    d.build = lambda story: d.build_(story, onFirstPage=_foot, onLaterPages=_foot)
    return d


if __name__ == "__main__" and sys.argv[1:] == ["check"]:
    for k in COMPANIES:
        print(k, logo_path(k) or "MISSING (company name will be printed instead)")

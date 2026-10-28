"""Donna field guide for Snow White drivers, as a printable PDF in the house style (brand/brand.py).

    python3 flyer/donna_field_guide.py      -> flyer/Donna-Field-Guide-Snow-White-Drivers.pdf

The living version (the link drivers get) is the Claude Doc "Donna Field Guide: Snow White Drivers"; keep the two in step.
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "brand"))
sys.path.insert(0, HERE)
import brand  # noqa: E402
from PIL import Image as PILImage, ImageDraw  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import CondPageBreak, Image, KeepTogether, Paragraph, Spacer, Table, TableStyle  # noqa: E402

OUT = os.path.join(HERE, "Donna-Field-Guide-Snow-White-Drivers.pdf")
pdfmetrics.registerFont(TTFont("Sym", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
CHECK = '<font name="Sym" color="#2e9e5b">✔</font>'
MIC = '<font name="Sym">●</font>'
NAVY, GOLD, INK = brand.NAVY, brand.GOLD, brand.INK
TEXT = ParagraphStyle("t", parent=brand.BODY, fontSize=10, leading=13.5, spaceAfter=4)
CELL = ParagraphStyle("c", parent=brand.BODY, fontSize=9, leading=11.5)
EX = ParagraphStyle("e", parent=CELL, fontName="Helvetica-Oblique", textColor=colors.HexColor("#24343b"))
LEAD = ParagraphStyle("l", parent=brand.BODY, fontSize=11.5, leading=15.5, textColor=NAVY, spaceAfter=6)
BUL = ParagraphStyle("b", parent=TEXT, leftIndent=12, bulletIndent=2, spaceAfter=2)
CARD = ParagraphStyle("k", parent=brand.BODY, fontSize=9.5, leading=12.5, spaceAfter=2.5)


def p(text, style=TEXT):
    return Paragraph(text, style)


def bullets(items):
    return [Paragraph(t, BUL, bulletText="•") for t in items]


def two_col(head, rows, widths=(2.35 * inch, 4.95 * inch)):
    data = [head] + [[p(a, CELL), p(b, EX)] for a, b in rows]
    return brand.table(data, list(widths))


def donna_badge(size=1.05 * inch):
    """Donna's picture, round, for the intro."""
    im = PILImage.open(os.path.join(HERE, "..", "brand", "donna_google_profile.png")).convert("RGB").resize((420, 420))
    mask = PILImage.new("L", im.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 419, 419), fill=255)
    out = PILImage.new("RGB", im.size, "white")
    out.paste(im, mask=mask)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    buf.seek(0)
    return Image(buf, width=size, height=size)


from driver_guide import CHEAT, CHEAT_FOOT, GUIDE, INTRO  # noqa: E402  (the content, shared with the web page)


def build():
    fill = lambda t: t.replace("{CHECK}", CHECK)
    doc = brand.document(OUT, "Donna Field Guide", footer="Donna field guide for drivers", companies=("sw",))
    story = brand.header("Donna Field Guide", "For Snow White drivers \u00b7 October 2026", companies=("sw",))
    t = Table([[donna_badge(), [p(INTRO[0], LEAD), p(INTRO[1])]]], colWidths=[1.25 * inch, None])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [t, Spacer(1, 4)]
    for block in GUIDE:
        kind = block[0]
        if kind == "section":
            story += [CondPageBreak(1.6 * inch)] + brand.section(block[1])
        elif kind in ("p", "lead", "note"):
            story.append(p(fill(block[1]), {"p": TEXT, "lead": LEAD, "note": brand.NOTE}[kind]))
        elif kind == "bullets":
            story += bullets([fill(b) for b in block[1]])
        elif kind == "table":
            story += [two_col(block[1], [(fill(a), fill(b)) for a, b in block[2]]), Spacer(1, 5)]
    card = [Paragraph("<b>DONNA CHEAT SHEET</b> \u00b7 keep this in the truck", ParagraphStyle(
        "kh", parent=CARD, fontSize=11, textColor=NAVY, spaceAfter=6))]
    card += [Paragraph(f"<b>{i}. {what}:</b> {fill(how)}", CARD) for i, (what, how) in enumerate(CHEAT, 1)]
    card += [Spacer(1, 4), Paragraph(fill(CHEAT_FOOT), CARD)]
    box = Table([[card]], colWidths=[7.3 * inch])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 2.2, GOLD), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff9ea")),
                             ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                             ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    story += [CondPageBreak(4.2 * inch), Spacer(1, 10), KeepTogether([box])]
    doc.build(story)
    return OUT


if __name__ == "__main__":
    print(build())

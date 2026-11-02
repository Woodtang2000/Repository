"""The daily scorecard for Scott and Bill: how many requests came in, through which channel and from whom, who
finished them (the ✅: Donna or a person), what's still open, and every change Donna made in Alliant. Read from the
tickets in #service-desk, whose metadata already says where each came from (src_channel), who asked (driver) and who
entered it (entered_by, done_at). Sent each morning for the day before (desk.scorecard_pass, SCORECARD_HOUR, to
SCORECARD_TO); "Donna scorecard" in a private message gives today so far.
"""
import re
from collections import Counter
from datetime import datetime, timedelta

from .context import ALASKA

OWN = ("@snowwhitelinen.com", "@thelaundrygroup.com")


def channel_of(p: dict, text: str, routes: set[str], desk_id: str) -> str:
    """Where a request came in, in plain words."""
    src = str(p.get("src_channel") or "")
    if p.get("kind") == "web_request":
        return "Website chat"
    if p.get("kind") == "phone_request":
        return "Phone call (not on file)"
    if src == "text":
        return "Customer text"
    if src == "email":
        return "Email (staff forward)" if str(p.get("email_to") or "").lower().endswith(OWN) else "Customer email"
    if src == "phone":
        return "Customer phone call"
    if src in routes:
        return "Employee line (driver call)" if "called it in" in text else "Driver in Slack"
    if src.startswith("D") or src.startswith("U"):
        if "by voice" in text:
            return "Donna app (voice)"
        if "by text" in text:
            return "Owner text"
        return "Private message to Donna"
    if src == desk_id or not src:
        return "Office in #service-desk"
    return "Slack channel"


def _first(name: str) -> str:
    return (name or "").split()[0] if name else "someone"


def stats(messages: list[dict], meta, body, routes: set[str], desk_id: str, start: datetime, end: datetime) -> dict:
    """The day's numbers: requests posted, and work finished, between start and end."""
    lo, hi = start.timestamp(), end.timestamp()
    came, done_by, open_, needs, donna = [], Counter(), 0, 0, []
    for m in messages:
        p = meta(m) or {}
        kind = p.get("kind")
        if kind not in ("ticket", "done", "web_request", "phone_request"):
            continue
        text = body(m)
        posted = float(m.get("ts") or 0)
        if lo <= posted < hi:
            came.append((channel_of(p, text, routes, desk_id), p.get("driver") or ""))
            if kind != "done":
                open_ += 1
                if p.get("donna") == "needs_person":
                    needs += 1
        if kind == "done" and lo <= float(p.get("done_at") or 0) < hi:
            who = _first(p.get("entered_by") or "")
            done_by[who] += 1
            if who.lower() == "donna":
                rb = re.sub(r"^✅\s*", "", p.get("readback") or "").strip() or text.splitlines()[0][:160]
                donna.append(rb)
    return {"came": len(came), "by_channel": Counter(c for c, _ in came).most_common(),
            "by_who": Counter(_first(w) for _, w in came if w).most_common(8), "done_by": done_by.most_common(),
            "finished": sum(done_by.values()), "open": open_, "needs": needs, "donna": donna}


def build(messages: list[dict], meta, body, routes: set[str], desk_id: str, start: datetime, end: datetime,
          title: str) -> str:
    """The scorecard as a Slack message."""
    s = stats(messages, meta, body, routes, desk_id, start, end)
    lines = [f"📊 *Daily scorecard · {title}*", f"*Requests in:* {s['came']}"]
    if s["came"]:
        lines.append("*By channel:* " + " · ".join(f"{c} {n}" for c, n in s["by_channel"]))
        if s["by_who"]:
            lines.append("*Asked by:* " + " · ".join(f"{w} {n}" for w, n in s["by_who"]))
    lines.append(f"*Finished (✅):* {s['finished']}" + (" — " + " · ".join(f"{w} {n}" for w, n in s["done_by"])
                                                       if s["finished"] else ""))
    lines.append(f"*Still open from that day:* {s['open']}" + (f" ({s['needs']} that Donna handed to a person)"
                                                              if s["needs"] else ""))
    if s["donna"]:
        lines.append(f"*Donna's changes in Alliant ({len(s['donna'])}):*")
        lines += [f"• {d}" for d in s["donna"][:25]] + ([f"…and {len(s['donna']) - 25} more"] if len(s["donna"]) > 25 else [])
    return "\n".join(lines)


def pdf(s: dict, title: str, path: str) -> str:
    """The scorecard as a PDF in the house style (brand/brand.py, like the driver reports). Returns the path."""
    import os
    import sys
    from xml.sax.saxutils import escape
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brand"))
    import brand
    from reportlab.lib.units import inch
    from reportlab.platypus import Spacer, Table, TableStyle

    doc = brand.document(path, "Daily scorecard", footer="Daily scorecard (Donna)")
    story = brand.header("Daily scorecard", title, companies=("sw", "tlg"))
    tiles = [["Requests in", "Finished", "Still open", "Donna entered"],
             [str(s["came"]), str(s["finished"]), str(s["open"]), str(len(s["donna"]))]]
    t = Table(tiles, colWidths=[1.75 * inch] * 4)
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), brand.FONT), ("FONTSIZE", (0, 0), (-1, 0), 8),
                           ("TEXTCOLOR", (0, 0), (-1, 0), brand.MUTED), ("FONTNAME", (0, 1), (-1, 1), brand.BOLD),
                           ("FONTSIZE", (0, 1), (-1, 1), 22), ("LEADING", (0, 1), (-1, 1), 26),
                           ("TEXTCOLOR", (0, 1), (-1, 1), brand.NAVY), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                           ("BOX", (0, 0), (0, -1), 0.6, brand.RULE), ("BOX", (1, 0), (1, -1), 0.6, brand.RULE),
                           ("BOX", (2, 0), (2, -1), 0.6, brand.RULE), ("BOX", (3, 0), (3, -1), 0.6, brand.RULE),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 1), (-1, 1), 10)]))
    story += [t, Spacer(1, 10)]
    if s["needs"]:
        story.append(brand.P(f"{s['needs']} of the open requests were handed back to a person by Donna."))
    half = 3.45 * inch
    story += brand.section("Where requests came in")
    story.append(brand.table([["Channel", "Requests"]] + [[escape(c), str(n)] for c, n in s["by_channel"]]
                             + [["Total", str(s["came"])]], [half + 1.5 * inch, 1.0 * inch], total_row=True)
                 if s["by_channel"] else brand.P("No requests came in."))
    story += brand.section("Who asked")
    story.append(brand.table([["Person", "Requests"]] + [[escape(w), str(n)] for w, n in s["by_who"]],
                             [half + 1.5 * inch, 1.0 * inch]) if s["by_who"] else brand.P("—"))
    story += brand.section("Who finished the work (gave it the check)")
    story.append(brand.table([["Person", "Finished"]] + [[escape(w), str(n)] for w, n in s["done_by"]]
                             + [["Total", str(s["finished"])]], [half + 1.5 * inch, 1.0 * inch], total_row=True)
                 if s["done_by"] else brand.P("Nothing was finished."))
    story += brand.section(f"Donna's changes in Alliant ({len(s['donna'])})")
    story.append(brand.table([["#", "Change"]] + [[str(i), brand.P(escape(d))] for i, d in enumerate(s["donna"], 1)],
                             [0.4 * inch, 6.6 * inch]) if s["donna"] else brand.P("None."))
    doc.build(story)
    return path


def day_window(now: datetime, days_back: int = 1) -> tuple[datetime, datetime, str]:
    """Yesterday (or today so far with days_back=0), Alaska time."""
    local = now.astimezone(ALASKA)
    start = (local - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) if days_back else local
    return start, end, f"{start:%A, %B} {start.day}" + ("" if days_back else " (so far)")

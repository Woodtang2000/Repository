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


def build(messages: list[dict], meta, body, routes: set[str], desk_id: str, start: datetime, end: datetime,
          title: str) -> str:
    """The scorecard text for requests posted (and work finished) between start and end."""
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
    lines = [f"📊 *Daily scorecard · {title}*", f"*Requests in:* {len(came)}"]
    if came:
        by_ch = Counter(c for c, _ in came)
        lines.append("*By channel:* " + " · ".join(f"{c} {n}" for c, n in by_ch.most_common()))
        by_who = Counter(_first(w) for _, w in came if w)
        if by_who:
            lines.append("*Asked by:* " + " · ".join(f"{w} {n}" for w, n in by_who.most_common(8)))
    finished = sum(done_by.values())
    lines.append(f"*Finished (✅):* {finished}" + (" — " + " · ".join(f"{w} {n}" for w, n in done_by.most_common())
                                                  if finished else ""))
    lines.append(f"*Still open from that day:* {open_}" + (f" ({needs} that Donna handed to a person)" if needs else ""))
    if donna:
        lines.append(f"*Donna's changes in Alliant ({len(donna)}):*")
        lines += [f"• {d}" for d in donna[:25]] + ([f"…and {len(donna) - 25} more"] if len(donna) > 25 else [])
    return "\n".join(lines)


def day_window(now: datetime, days_back: int = 1) -> tuple[datetime, datetime, str]:
    """Yesterday (or today so far with days_back=0), Alaska time."""
    local = now.astimezone(ALASKA)
    start = (local - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) if days_back else local
    return start, end, f"{start:%A, %B} {start.day}" + ("" if days_back else " (so far)")

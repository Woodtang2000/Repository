"""What people ask Donna that she can't do yet (Scott, 11/18: "I want to keep adding features but I don't know what
people are asking", and "the worst thing is she says something is done and it didn't go anywhere").

Every time Donna has to say "I can't do that yet", the request goes here (<data>/wishlist.jsonl), so nothing someone
asked for disappears silently and Scott can see what to build next: "Donna wishlist" in his private messages lists the
last two weeks, and Monday mornings she sends him the past week's.
"""
import json
import os
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

ALASKA = ZoneInfo("America/Anchorage")

FILE = "wishlist.jsonl"


def add(data_dir: str, who: str, where: str, text: str, why: str = "") -> None:
    """One request Donna couldn't handle: who asked, where (a channel, private message, the app), what they said."""
    row = {"at": time.time(), "who": who or "someone", "where": where, "text": " ".join(str(text).split())[:500], "why": why}
    try:
        with open(os.path.join(data_dir, FILE), "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        print(time.strftime("%H:%M"), "wishlist not saved:", e, flush=True)


def recent(data_dir: str, days: float = 14, now: float | None = None) -> list[dict]:
    since = (now or time.time()) - days * 86400
    out = []
    try:
        with open(os.path.join(data_dir, FILE)) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("at", 0) >= since:
                    out.append(row)
    except OSError:
        pass
    return out


def summary(data_dir: str, days: float = 14, now: float | None = None) -> str:
    """For Scott: everything asked in the period, newest first, with who asked how often."""
    rows = recent(data_dir, days, now)
    if not rows:
        return f"📝 Nobody asked me for anything I can't do in the last {int(days)} days."
    who = Counter(r["who"] for r in rows)
    lines = [f"📝 *What people asked me that I can't do yet* (last {int(days)} days: {len(rows)}, "
             + ", ".join(f"{w} {n}" for w, n in who.most_common()) + ")"]
    for r in sorted(rows, key=lambda r: -r["at"])[:40]:
        d = datetime.fromtimestamp(r["at"], ALASKA)
        when = f"{d:%a} {d.month}/{d.day}"
        lines.append(f"• {when} · {r['who']} ({r['where']}): _{r['text'][:200]}_" + (f" · {r['why']}" if r.get("why") else ""))
    return "\n".join(lines)

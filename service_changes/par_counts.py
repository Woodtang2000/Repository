"""Par counting for the hospitals, replacing Linen Helper (Scott, 11/23-11/26). Spec and decisions:
deploy/par_counts/README.md.

- Delivery points and their pars live in <data>/par_points.json. They're imported from Linen Helper's export
  (python -m service_changes.par_counts --import file.csv --account 6001-2-00000), not taken from Alliant: Alliant's
  "Standard" is what's delivered, not the shelf par.
- The techs' page: chat.thelaundrygroup.com/par/<PAR_KEY>/ (employees only, the key in the link). Pick the hospital,
  pick a delivery point, type what's on the shelf; the page shows the need (par - on hand) as they go. Each point is
  saved on its own. Phones that lose signal keep the counts and send them later.
- "Done for today" builds the pull list: combined totals per item plus each point's breakdown. It's posted in the
  hospital's TLG channel and printable from the page. **Production runs off this list, never waiting on Alliant**.
- Alliant gets **building totals only** (Scott, 11/26): Donna enters one Request Entry with the combined quantities
  (kind "par_request"). That starts once Donna is back in Alliant: PAR_ALLIANT=on, and she's not paused.
- Never involve TEXO / Alliant support in this project (Scott, 11/26: "They own linen helper").
"""
import csv
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

from .context import ALASKA

POINTS = "par_points.json"
COUNTS = "par_counts.json"


def alliant_on() -> bool:
    """Donna enters the building totals in Alliant (off until she's back in Alliant, Scott 11/26)."""
    return os.environ.get("PAR_ALLIANT", "off").lower() in ("on", "1", "yes", "true")


def delivery_date(now: datetime | None = None) -> str:
    """Counts made today are for tomorrow's delivery."""
    now = now or datetime.now(ALASKA)
    return (now + timedelta(days=1)).strftime("%Y-%m-%d")


def import_csv(path: str, account: str, name: str = "") -> dict:
    """Linen Helper's export -> one hospital's delivery points and pars. Columns are matched loosely: the delivery
    point (point / delivery point / location / unit), the item (item / description / product), an optional SKU,
    and the par (par / par level / quantity)."""
    def pick(row: dict, *names: str) -> str:
        for k, v in row.items():
            key = re.sub(r"[^a-z]", "", (k or "").lower())
            if any(key == re.sub(r"[^a-z]", "", n) for n in names):
                return (v or "").strip()
        return ""
    points: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            point = pick(row, "point", "delivery point", "deliverypoint", "location", "unit", "department")
            item = pick(row, "item", "description", "product", "item description")
            par = pick(row, "par", "par level", "parlevel", "quantity", "qty")
            if not (point and item and re.fullmatch(r"\d+(\.\d+)?", par or "")):
                continue
            pid = re.sub(r"[^a-z0-9]+", "-", point.lower()).strip("-")
            p = points.setdefault(pid, {"id": pid, "name": point, "items": []})
            p["items"].append({"sku": pick(row, "sku", "item number", "itemnumber", "code"), "item": item,
                               "par": int(float(par))})
    return {"account": account, "name": name or account, "points": list(points.values())}


class Pars:
    def __init__(self, desk):
        self.desk = desk
        self.lock = threading.Lock()

    def _p(self, name: str) -> str:
        return os.path.join(self.desk.data_dir, name)

    def _load(self, name: str) -> dict:
        try:
            return json.load(open(self._p(name)))
        except (OSError, ValueError):
            return {}

    def _save(self, name: str, data: dict) -> None:
        with open(self._p(name) + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(self._p(name) + ".tmp", self._p(name))

    def setup(self) -> dict:
        """{account: {name, points: [{id, name, items: [{sku, item, par}]}]}}"""
        return self._load(POINTS)

    def add_hospital(self, h: dict) -> None:
        with self.lock:
            data = self.setup()
            data[h["account"]] = {"name": h["name"], "points": h["points"]}
            self._save(POINTS, data)

    def config(self) -> dict:
        """For the page: the hospitals, their points and pars, and what's been counted for tomorrow already."""
        date = delivery_date()
        counts = self._load(COUNTS).get(date, {})
        return {"date": date, "hospitals": [
            {"account": a, "name": h["name"], "points": h["points"],
             "counted": {pid: c.get("by", "") for pid, c in counts.get(a, {}).get("points", {}).items()},
             "done": bool(counts.get(a, {}).get("done"))}
            for a, h in sorted(self.setup().items())]}

    def save(self, body: dict) -> dict:
        """One delivery point's count: {account, point, counts: {item: on hand}, who}."""
        account, pid = str(body.get("account") or ""), str(body.get("point") or "")
        h = self.setup().get(account)
        point = next((p for p in (h or {}).get("points", []) if p["id"] == pid), None)
        if not point:
            return {"ok": False, "error": "Unknown hospital or delivery point."}
        given = body.get("counts") or {}
        on_hand = {}
        for it in point["items"]:
            v = given.get(it["item"])
            if v is None or v == "":
                continue
            try:
                on_hand[it["item"]] = max(0, int(float(v)))
            except (TypeError, ValueError):
                continue
        date = str(body.get("date") or delivery_date())
        with self.lock:
            data = self._load(COUNTS)
            day = data.setdefault(date, {}).setdefault(account, {"points": {}})
            day["points"][pid] = {"on_hand": on_hand, "by": str(body.get("who") or "")[:60], "at": time.time()}
            data = {k: v for k, v in sorted(data.items())[-120:]}  # four months for the reports
            self._save(COUNTS, data)
        need = {it["item"]: max(0, it["par"] - on_hand[it["item"]]) for it in point["items"] if it["item"] in on_hand}
        return {"ok": True, "date": date, "need": need}

    def pull_list(self, account: str, date: str) -> dict:
        """Combined need per item (what route makeup builds), and each point's part of it."""
        h = self.setup().get(account) or {}
        day = self._load(COUNTS).get(date, {}).get(account, {})
        totals: dict[str, dict] = {}
        points, missing = [], []
        for p in h.get("points", []):
            c = day.get("points", {}).get(p["id"])
            if not c:
                missing.append(p["name"])
                continue
            lines = []
            for it in p["items"]:
                if it["item"] not in c["on_hand"]:
                    continue
                n = max(0, it["par"] - c["on_hand"][it["item"]])
                if n:
                    lines.append({"item": it["item"], "need": n})
                    t = totals.setdefault(it["item"], {"item": it["item"], "sku": it.get("sku", ""), "need": 0})
                    t["need"] += n
            points.append({"name": p["name"], "by": c.get("by", ""), "lines": lines})
        return {"account": account, "name": h.get("name", account), "date": date,
                "totals": sorted(totals.values(), key=lambda t: t["item"]), "points": points, "missing": missing}

    def finish(self, body: dict) -> dict:
        """"Done for today": the pull list to the hospital's TLG channel; Donna's Alliant entry once that's on."""
        account = str(body.get("account") or "")
        if account not in self.setup():
            return {"ok": False, "error": "Unknown hospital."}
        date = str(body.get("date") or delivery_date())
        pl = self.pull_list(account, date)
        with self.lock:
            data = self._load(COUNTS)
            day = data.setdefault(date, {}).setdefault(account, {"points": {}})
            day.update(done=True, done_by=str(body.get("who") or "")[:60], done_at=time.time())
            self._save(COUNTS, data)
        self.post(pl)
        queued = self.queue_alliant(pl)
        return {"ok": True, "pull": pl, "alliant": queued}

    def text(self, pl: dict) -> str:
        d = datetime.strptime(pl["date"], "%Y-%m-%d")
        out = [f"🧺 *Pull list · {pl['name']} · for {d:%A %-m/%-d}*"]
        out += [f"• {t['item']}: *{t['need']}*" for t in pl["totals"]] or ["Nothing needed: every counted point is at par."]
        if pl["missing"]:
            out.append(f"⚠️ Not counted ({len(pl['missing'])}): " + ", ".join(pl["missing"]))
        out.append("_By delivery point:_")
        for p in pl["points"]:
            out.append(f"*{p['name']}*" + (f" ({p['by']})" if p["by"] else "") + ": "
                       + (", ".join(f"{x['item']} {x['need']}" for x in p["lines"]) or "at par"))
        return "\n".join(out)

    def post(self, pl: dict) -> str:
        name = os.environ.get("PAR_CHANNEL", "tlg-alaska-regional")
        ch = getattr(self.desk, "channels", {}).get(name) or os.environ.get("PAR_CHANNEL_ID", "")
        if not ch:
            print(time.strftime("%H:%M"), "par counts: no channel", name, flush=True)
            return ""
        try:
            return self.desk.slack.chat_postMessage(channel=ch, text=self.text(pl)[:3900], unfurl_links=False).get("ts", "")
        except Exception as e:
            print(time.strftime("%H:%M"), "par pull list not posted:", str(e)[:200], flush=True)
            return ""

    def queue_alliant(self, pl: dict) -> str:
        """Building totals into Alliant's Request Entry (Donna's workstation), once she's back in Alliant."""
        if not alliant_on() or not pl["totals"]:
            return ""
        jobs = self.desk.jobs()
        if jobs.load().get("paused"):
            return ""
        d = datetime.strptime(pl["date"], "%Y-%m-%d")
        text = "\n".join([
            f"PAR REQUEST for {pl['name']} ({pl['account']}), delivery {d:%A %-m/%-d/%Y}. Follow your rules section "
            "\"Hospital par requests\".",
            "Healthcare > Request Entry: this customer, its delivery unit for the building totals, the week with that date.",
            "Set the Quantity for that day on each line below (building totals). Leave every other line and day alone.",
        ] + [f"- {t['sku'] + ' ' if t['sku'] else ''}{t['item']}: {t['need']}" for t in pl["totals"]] + [
            "If loading has already started for that date (entry is blocked), stop: needs_person. Report entered with the "
            "lines you set."])
        return jobs.custom_job("par_request", text, {"account": pl["account"], "request_id": f"{pl['account']}/{pl['date']}"})

    def job_done(self, job: dict, question: str = "") -> str:
        name = os.environ.get("PAR_CHANNEL", "tlg-alaska-regional")
        ch = getattr(self.desk, "channels", {}).get(name) or os.environ.get("PAR_CHANNEL_ID", "")
        ok = job["status"] == "entered"
        text = (f"✅ Entered in Alliant (Request Entry) {datetime.now(ALASKA):%-I:%M %p}: {job.get('request_id', '')}"
                if ok else f"⚠️ Couldn't enter the building totals in Alliant ({job.get('request_id', '')}). "
                           "Please enter them by hand.") + (f"\n{question}" if question else "") + \
            (f"\n{job.get('note')}" if job.get("note") else "")
        if ch:
            self.desk.slack.chat_postMessage(channel=ch, text=text[:3900], unfurl_links=False)
        return job["status"]


def main() -> None:
    """python -m service_changes.par_counts --data service_changes/data --import alaska_regional.csv
       --account 6001-2-00000 --name "Alaska Regional Hospital" [--yes]"""
    import argparse
    from types import SimpleNamespace
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    ap.add_argument("--import", dest="src", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--yes", action="store_true", help="save it (otherwise only show what was read)")
    args = ap.parse_args()
    h = import_csv(args.src, args.account, args.name)
    print(f"{h['name']} ({h['account']}): {len(h['points'])} delivery points, "
          f"{sum(len(p['items']) for p in h['points'])} item lines")
    for p in h["points"][:50]:
        print(f"  {p['name']}: " + ", ".join(f"{i['item']} {i['par']}" for i in p["items"][:8])
              + (" ..." if len(p["items"]) > 8 else ""))
    if args.yes:
        Pars(SimpleNamespace(data_dir=args.data)).add_hospital(h)
        print("Saved.")
    else:
        print("Nothing saved. Add --yes to save it.")


if __name__ == "__main__":
    main()

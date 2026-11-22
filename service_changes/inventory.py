"""Garment inventory scans (Scott, 11/22): an employee scans a customer's garments with a phone, and Donna enters each
barcode at Alliant's Count Station with scan type V (INVENTORY). It changes nothing about a garment (no wash count,
no service dates): it only records that the garment was seen. Employees only, no approval (Scott: "simpler than that").

- Employees: chat.snowwhitelinen.com/inventory/<INVENTORY_KEY>/ (TLG: chat.thelaundrygroup.com/...): pick any account,
  scan, send.
- Customers (Scott, 11/22: "I want customers to be able to use it. It really can't screw anything up"): the public page
  chat.snowwhitelinen.com/inventory (on the websites at /inventory). No account search: the garments they scan decide
  the account, and a garment from another account is only "not on your account", nothing about it. They see the
  garment and size (on the tag in their hands), never a wearer name, a code or another customer.
- Donna posts a card in #stockroom: how many were scanned, against how many Alliant has in service on that account,
  and which in-service garments weren't scanned; then her workstation enters them (kind "inventory"; rules
  "Inventory scans") and reports any warnings (out of service, a pull code or a service code) under the card.
- The Count Station needs the account's delivery day: the scan day if it's one of them, else the next one.
- INVENTORY_TEST (on until Scott switches it off): the card says what Donna would enter, and nothing goes to Alliant.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

from .context import ALASKA
from .uniform import BARCODE, describe, nice, pooled

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FULL = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday",
        "Sun": "Sunday"}
MAX_SCANS = 1500
FILE = "inventory.json"


def test_mode() -> bool:
    return os.environ.get("INVENTORY_TEST", "on").lower() not in ("off", "0", "no", "false")


def _bc(b: str) -> str:
    return re.sub(r"\D", "", str(b or ""))


def delivery_days(alliant, account: str) -> list[str]:
    """The account's delivery days, Mon..Sun order, from its route stops (or the customer file)."""
    days = {str(r.get("day", ""))[:3].title() for r in getattr(alliant, "stops", []) if r.get("account") == account}
    if not days:
        c = next((c for c in alliant.customers if c.account == account), None)
        days = {str(d)[:3].title() for d in (c.service_days if c else [])}
    return [d for d in DAYS if d in days]


def count_day(alliant, account: str, when: datetime | None = None) -> str:
    """The day to select at the Count Station: the scan day if it's a delivery day, else the next one (Scott, 11/22)."""
    days = delivery_days(alliant, account)
    when = when or datetime.now(ALASKA)
    if not days:
        return ""
    for i in range(7):
        d = DAYS[(when.weekday() + i) % 7]
        if d in days:
            return d
    return days[0]


def accounts(alliant, q: str, limit: int = 12) -> list[dict]:
    """Accounts whose number or name matches what the employee typed."""
    q = (q or "").strip().lower()
    if len(q) < 2:
        return []
    words = q.split()
    out = []
    for c in alliant.customers:
        hay = f"{c.account} {c.name}".lower()
        if all(w in hay for w in words):
            out.append({"account": c.account, "name": nice(c.name), "days": delivery_days(alliant, c.account)})
    out.sort(key=lambda x: (not x["account"].lower().startswith(q), x["name"]))
    return out[:limit]


def in_service(alliant, account: str) -> list[dict]:
    """Every garment Alliant has in service on the account (the hourly file)."""
    return [row for (acct, _), rows in getattr(alliant, "in_service", {}).items() if acct == account for row in rows]


def lookup(alliant, barcode: str, account: str = "", public: bool = False) -> dict:
    """One scan, for the page: what it is, and whether it's on the account being counted. public (a customer): the
    first scan names their account; after that only their own garments say anything."""
    b = _bc(barcode)
    if not BARCODE.fullmatch(b):
        return {"found": False, "barcode": b, "why": "not a garment barcode"}
    row = alliant.garment(b)
    if not row:
        return {"found": False, "barcode": b, "why": "not in service in Alliant's last file"}
    item, size = describe(row)
    acct = row.get("cust_code", "")
    if public:
        if account and acct != account:
            return {"found": True, "barcode": b, "here": False}  # someone else's garment: nothing about it
        c = next((c for c in alliant.customers if c.account == acct), None)
        return {"found": True, "barcode": row.get("barcode") or b, "garment": item, "size": size, "here": True,
                "account": acct, **({"name": nice(c.name) if c else ""} if not account else {})}
    w = None if pooled(acct) else next((w for w in alliant.wearers.get(acct, []) if w.employee == row.get("wearer")), None)
    return {"found": True, "barcode": row.get("barcode") or b, "garment": item, "size": size,
            "wearer": nice(w.name) if w else "", "account": acct, "here": not account or acct == account,
            "code": row.get("service_code", "")}


class Inventory:
    def __init__(self, desk):
        self.desk = desk
        self.lock = threading.Lock()

    @property
    def path(self) -> str:
        return os.path.join(self.desk.data_dir, FILE)

    def load(self) -> dict:
        try:
            return json.load(open(self.path))
        except (OSError, ValueError):
            return {"seq": 100, "scans": {}}

    def save(self, data: dict) -> None:
        with open(self.path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(self.path + ".tmp", self.path)

    def channel(self) -> str:
        from .new_accounts import STOCKROOM_ID
        return os.environ.get("INVENTORY_CHANNEL_ID", STOCKROOM_ID)

    def name(self, account: str) -> str:
        c = next((c for c in self.desk.alliant.customers if c.account == account), None)
        return f"{account} {nice(c.name)}" if c else account

    def new(self, body: dict, public: bool = False) -> dict:
        """The page's scans: {account, barcodes, who}. Card in #stockroom, then the Alliant job (unless test mode).
        public (a customer): the account is the one most of their garments belong to, whatever the page says, and only
        that account's garments are entered."""
        alliant = self.desk.alliant
        account = str(body.get("account") or "").strip()
        if public:
            from collections import Counter
            owners = Counter((alliant.garment(_bc(b)) or {}).get("cust_code", "") for b in (body.get("barcodes") or [])[:MAX_SCANS])
            owners.pop("", None)
            account = owners.most_common(1)[0][0] if owners else ""
            if not account:
                return {"ok": False, "error": "None of those barcodes are garments we have in service. Check with our office."}
        if not any(c.account == account for c in alliant.customers):
            return {"ok": False, "error": "Pick the account first."}
        scanned = list(dict.fromkeys(b for b in (_bc(x) for x in (body.get("barcodes") or [])[:MAX_SCANS]) if b))
        if not scanned:
            return {"ok": False, "error": "No barcodes scanned."}
        who = (str(body.get("who") or "").strip()[:60] or ("a customer" if public else "an employee")) + \
            (" (customer)" if public else "")
        here, elsewhere, unknown = [], [], []
        for b in scanned:
            row = alliant.garment(b)
            if not row:
                unknown.append(b)  # Alliant may still know it (newer than the file): Donna enters it and notes the warning
            elif row.get("cust_code") == account:
                here.append(row.get("barcode") or b)
            else:
                elsewhere.append((row.get("barcode") or b, row.get("cust_code", "")))
        if public:  # a customer (or a bot): only garments we have in service on their account, never a guess
            unknown = []
            if not here:
                return {"ok": False, "error": "None of those barcodes are garments we have in service. Check with our office."}
        seen = {b.zfill(12) for b in scanned}
        missing = [r for r in in_service(alliant, account) if (r.get("barcode") or "").zfill(12) not in seen]
        day = count_day(alliant, account)
        with self.lock:
            data = self.load()
            data["seq"] = int(data.get("seq", 100)) + 1
            ref = f"INV-{data['seq']}"
            rec = {"ref": ref, "at": time.time(), "account": account, "who": who, "day": day,
                   "enter": here + unknown, "elsewhere": elsewhere, "unknown": unknown,
                   "missing": [r.get("barcode", "") for r in missing], "in_service": len(in_service(alliant, account)),
                   "status": "test" if test_mode() else "queued"}
            data["scans"][ref] = rec
            data["scans"] = dict(list(data["scans"].items())[-500:])
            self.save(data)
        ts = self.post(rec, missing)
        if ts:
            rec = self.update(ref, ts=ts)
        if not test_mode() and rec["enter"] and day:
            jid = self.desk.jobs().custom_job("inventory", self.job_text(rec), {"account": account, "request_id": ref})
            self.update(ref, job=jid)
        return {"ok": True, "ref": ref, "scanned": len(scanned), "on_account": len(here), "elsewhere": len(elsewhere),
                "unknown": len(unknown), "in_service": rec["in_service"], "missing": len(rec["missing"]), "day": FULL.get(day, ""),
                **({} if public else {"account": account})}

    def update(self, ref: str, **kw) -> dict:
        with self.lock:
            data = self.load()
            data["scans"][ref].update(kw)
            self.save(data)
            return data["scans"][ref]

    def post(self, rec: dict, missing: list[dict]) -> str:
        """The card: counts, and the in-service garments nobody scanned (the point of an inventory)."""
        alliant = self.desk.alliant
        test = " · TEST" if test_mode() else ""
        lines = [f"📋 *{rec['ref']}{test} · Inventory scan · {self.name(rec['account'])}* (by {rec['who']})",
                 f"Scanned *{len(rec['enter']) - len(rec['unknown'])}* of the *{rec['in_service']}* garments Alliant has in "
                 f"service on this account."]
        if missing:
            lines.append(f"*Not scanned ({len(missing)}):*")
            for r in missing[:40]:
                item, size = describe(r)
                w = "" if pooled(rec["account"]) else next(
                    (x.name for x in alliant.wearers.get(rec["account"], []) if x.employee == r.get("wearer")), "")
                lines.append(f"• {r.get('barcode', '')} · {' '.join(x for x in (item, size) if x)}" + (f" · {nice(w)}" if w else ""))
            if len(missing) > 40:
                lines.append(f"…and {len(missing) - 40} more.")
        if rec["elsewhere"] and "(customer)" in rec["who"]:
            lines.append(f"Scanned but on another account, not entered ({len(rec['elsewhere'])}): "
                         + ", ".join(f"{b} ({a})" for b, a in rec["elsewhere"][:15]))
        elif rec["elsewhere"]:
            lines.append(f"On another account, not entered ({len(rec['elsewhere'])}): "
                         + ", ".join(f"{b} ({a})" for b, a in rec["elsewhere"][:15]))
        if rec["unknown"]:
            lines.append(f"Not in Alliant's last file ({len(rec['unknown'])}): " + ", ".join(rec["unknown"][:15])
                         + ". I'll enter them and note what Alliant says.")
        day = FULL.get(rec["day"], "")
        if not day:
            lines.append("⚠️ I don't know this account's delivery day, so I can't use the Count Station. Please enter by hand.")
        elif test_mode():
            lines.append(f"TEST: I would enter {len(rec['enter'])} barcodes at the Count Station (Inventory, {day}). "
                         "Nothing was sent to Alliant.")
        elif rec["enter"]:
            lines.append(f"🤖 Entering {len(rec['enter'])} barcodes at the Count Station now (Inventory, {day}).")
        try:
            return self.desk.slack.chat_postMessage(channel=self.channel(), text="\n".join(lines)[:3900],
                                                    unfurl_links=False).get("ts", "")
        except Exception as e:
            print(time.strftime("%H:%M"), "inventory card:", str(e)[:200], flush=True)
            return ""

    def job_text(self, rec: dict) -> str:
        """The workstation job (rules: "Inventory scans")."""
        return "\n".join([
            f"INVENTORY SCAN {rec['ref']} from the inventory page (by {rec['who']}). Follow your rules section \"Inventory scans\".",
            f"Account: {self.name(rec['account'])}",
            f"Count Station: scan type V (INVENTORY), day: {FULL.get(rec['day'], rec['day'])}.",
            f"Barcodes ({len(rec['enter'])}), one at a time, Enter after each:"] + [f"- {b}" for b in rec["enter"]] + [
            "A warning (not in service, a pull code, a service code, or anything else): continue past it and note the barcode "
            "and what it said. Change nothing else. Report entered with how many went in and the list of warnings."])

    def job_done(self, job: dict, question: str = "") -> str:
        ref = job.get("request_id", "")
        rec = self.load()["scans"].get(ref)
        if not rec:
            return "unknown scan"
        ok = job["status"] == "entered"
        rec = self.update(ref, status="entered" if ok else "needs_person", finished=time.time())
        text = (f"✅ Entered at the Count Station {datetime.now(ALASKA):%-I:%M %p}." if ok else
                "⚠️ I couldn't finish it in Alliant. Please enter the rest by hand.") + \
            (f"\n{question.strip()}" if question.strip() else "") + (f"\n{job.get('note')}" if job.get("note") else "")
        if rec.get("ts"):
            self.desk.slack.chat_postMessage(channel=self.channel(), thread_ts=rec["ts"], text=text[:3900], unfurl_links=False)
        return f"{ref} {rec['status']}"

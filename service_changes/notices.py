"""Emails Donna sends customers on her own, from the hourly Alliant feed:

  garments arrived  a uniform order is fully received (purchase_orders.csv): "Dave's new shirts arrived and go out on
                    your next delivery (Thursday)."
  welcome           a new account appears: their delivery days, what they're set up for (no prices) and how to reach
                    Donna.

Each goes to the account's contact on its Alliant card, once, and the office sees an FYI in #service-desk. The first
run only takes note of what's already there (no emails about the past). CUSTOMER_NOTICES: "on" (default) or "off";
at most MAX_PER_PASS emails a pass, so a feed glitch can't flood customers.
"""
import json
import os
import re
import time

FILE = "notices.json"
MAX_PER_PASS = 25


def _num(v) -> int:
    try:
        return int(float(re.sub(r"[^\d.]", "", str(v)) or 0))
    except ValueError:
        return 0


def _po_key(o: dict) -> str:
    return f"{o.get('po', '')}/{o.get('line') or o.get('sku', '') + '/' + o.get('wearer', '')}"


class Notices:
    def __init__(self, desk):
        self.desk = desk

    def _path(self) -> str:
        return os.path.join(self.desk.data_dir, FILE)

    def _load(self) -> dict | None:
        try:
            return json.load(open(self._path()))
        except (OSError, ValueError):
            return None

    def _save(self, data: dict) -> None:
        with open(self._path() + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(self._path() + ".tmp", self._path())

    def emails(self, acct: str) -> list[str]:
        card = self.desk.alliant.cards.get(acct, {})
        return list(dict.fromkeys(e.strip().lower() for e in re.split(r"[;,\s]+", card.get("email") or "")
                                  if re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", e.strip())))

    def pass_(self) -> dict:
        """Look at the feed once; returns what was sent."""
        if os.environ.get("CUSTOMER_NOTICES", "on").lower() == "off":
            return {}
        data = self.desk.alliant
        received = {_po_key(o): o for orders in data.orders.values() for o in orders
                    if _num(o.get("qty_ordered")) and _num(o.get("qty_received")) >= _num(o.get("qty_ordered"))}
        accounts = {c.account for c in data.customers}
        state = self._load()
        if state is None:  # first run: what's there now is history
            self._save({"po": sorted(received), "accounts": sorted(accounts)})
            return {"baseline": len(received) + len(accounts)}
        seen_po, seen_accts = set(state.get("po", [])), set(state.get("accounts", []))
        out = {"garments": 0, "welcome": 0}
        budget = MAX_PER_PASS
        new_po: dict[str, list[dict]] = {}
        for k, o in received.items():
            if k not in seen_po:
                new_po.setdefault(o.get("cust_code", ""), []).append(o)
        for acct, lines in new_po.items():
            to = self.emails(acct)
            if to and budget > 0:
                budget -= len(to)
                self.garments_arrived(acct, lines, to)
                out["garments"] += 1
            seen_po.update(_po_key(o) for o in lines)  # once, sent or not (no email on the card: nothing to send)
        if accounts and len(accounts - seen_accts) <= MAX_PER_PASS:  # dozens at once is a feed change, not new customers
            for acct in sorted(accounts - seen_accts):
                to = self.emails(acct)
                if to and budget > 0:
                    budget -= len(to)
                    self.welcome(acct, to)
                    out["welcome"] += 1
                seen_accts.add(acct)
        else:
            seen_accts |= accounts
        self._save({"po": sorted(seen_po), "accounts": sorted(seen_accts)})
        return out

    def _next_day(self, acct: str) -> str:
        cust = next((c for c in self.desk.alliant.customers if c.account == acct), None)
        names = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday",
                 "Sat": "Saturday", "Sun": "Sunday"}
        days = [names.get(d[:3].title(), d) for d in (cust.service_days if cust else [])]
        return " or ".join(days[:2])

    def _send(self, acct: str, to: list[str], subject: str, body: str, kind: str, note: str) -> None:
        from . import bird
        from .context import company_for
        company = company_for(acct)
        for e in to:
            bird.send(e, f"{company}: {subject}", f"Hello,\n\n{body}\n\nThank you,\n\n" + bird.signature(company),
                      company=company, key=f"{kind}/{acct}/{e}/{time.strftime('%Y%m%d')}")
        name = next((c.name for c in self.desk.alliant.customers if c.account == acct), acct)
        self.desk.post(self.desk.desk_id, f"✉️ *Donna emailed {acct} {name}* ({', '.join(to)}): {note}", {"kind": "notice"})

    def garments_arrived(self, acct: str, lines: list[dict], to: list[str]) -> None:
        wearers = {w.employee: w.name.title() for w in self.desk.alliant.wearers.get(acct, [])}
        parts = []
        for o in lines:
            who = wearers.get(o.get("wearer", "")) or ""
            what = (o.get("description") or o.get("sku") or "garments").lower()
            parts.append(f"{who}'s new {what}" if who else f"new {what}")
        items = ", ".join(dict.fromkeys(parts))
        day = self._next_day(acct)
        body = (f"Good news: {items} {'have' if len(parts) > 1 else 'has'} arrived at our plant and will go out on your next "
                f"delivery{f' ({day})' if day else ''}. If anything doesn't fit right, just reply or text us.")
        self._send(acct, to, "new garments on the way", body, "garments", f"garments arrived ({items})")

    def welcome(self, acct: str, to: list[str]) -> None:
        from .context import company_for
        data = self.desk.alliant
        cust = next((c for c in data.customers if c.account == acct), None)
        items = [f"{data.autocount.get(acct, {}).get(i, q)} {i.title()}" for i, q in data.items.get(acct, {}).items()][:12]
        day = self._next_day(acct)
        company = company_for(acct)
        body = (f"Welcome to {company}! I'm Donna, and I'll be helping take care of your account"
                + (f" ({cust.name.title()})" if cust else "") + "."
                + (f"\n\nYour deliveries are on {day}." if day else "")
                + ("\n\nYou're set up for: " + ", ".join(items) + "." if items else "")
                + "\n\nNeed more of something, less, a new employee's uniforms, or a skipped week? Just text me at "
                  "(866) 827-8398 or reply to this email, any time, and I'll take care of it.")
        self._send(acct, to, "welcome", body, "welcome", "welcome email")

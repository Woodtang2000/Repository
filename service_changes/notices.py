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
# Billing and service lines that aren't something a customer "is set up for" ("0 Nog Lbs Delivered", 11/17)
INTERNAL = re.compile(r"\b(?:nog|lbs?|pounds?|fees?|charges?|minimum|fuel|energy|surcharge|service|delivery|delivered|"
                      r"invoice|tax|credit|adjust\w*|misc\w*)\b", re.I)
MAX_PER_PASS = 25


def _num(v) -> int:
    try:
        return int(float(re.sub(r"[^\d.]", "", str(v)) or 0))
    except ValueError:
        return 0


def _po_key(o: dict) -> str:
    return f"{o.get('po', '')}/{o.get('line') or o.get('sku', '') + '/' + o.get('wearer', '')}"


def held() -> set[str]:
    """WELCOME_HOLD in /etc/service-desk.env: account numbers (the part before the "-"), emails or email domains whose
    welcome waits (Scott, 11/18: Pursuit's lodges were entered on a Snow White route by mistake; hold until Sales says
    the routes are right). Remove the entry and the welcome goes out on the next pass."""
    return {x.strip().lower() for x in os.environ.get("WELCOME_HOLD", "").split(",") if x.strip()}


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
        from .context import customer_emails  # a sub-account (Costco, Safeway): its own linked contacts only
        return customer_emails(self.desk.alliant, acct, getattr(self.desk, "data_dir", None))

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
            # one welcome per person, naming all their new locations (11/17: one lodge manager got 4 welcomes at once)
            from .context import company_for
            by_person: dict[tuple[str, str], list[str]] = {}
            hold = held()
            for acct in sorted(accounts - seen_accts):
                to = self.emails(acct)
                if acct.split("-")[0] in hold or any(e.lower() in hold or e.lower().split("@")[-1] in hold for e in to):
                    continue  # held (WELCOME_HOLD): not marked seen, so it's welcomed once the hold comes off
                for e in to:
                    by_person.setdefault((e, company_for(acct)), []).append(acct)
                seen_accts.add(acct)
            for (e, _), accts in by_person.items():
                if budget > 0:
                    budget -= 1
                    self.welcome(accts, [e])
                    out["welcome"] += 1
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

    def welcome(self, accts: str | list[str], to: list[str]) -> None:
        """One welcome for one person: their location (with what it's set up for), or all their new locations at once."""
        from .context import company_for
        accts = [accts] if isinstance(accts, str) else accts
        data = self.desk.alliant
        name = lambda a: next((c.name.title() for c in data.customers if c.account == a), "")
        days = lambda a: self._next_day(a).replace(" or ", " and ")
        company = company_for(accts[0])
        if len(accts) == 1:
            acct = accts[0]
            items = [f"{n} {i.title()}" for i, q in data.items.get(acct, {}).items()
                     if (n := _num(data.autocount.get(acct, {}).get(i, q))) > 0 and not INTERNAL.search(i)][:12]
            body = (f"Welcome to {company}! I'm Donna, and I'll be helping take care of your account"
                    + (f" ({name(acct)})" if name(acct) else "") + "."
                    + (f"\n\nYour deliveries are on {days(acct)}." if days(acct) else "")
                    + ("\n\nYou're set up for: " + ", ".join(items) + "." if items else ""))
        else:
            body = (f"Welcome to {company}! I'm Donna, and I'll be helping take care of your {len(accts)} locations:\n"
                    + "\n".join(f"- {name(a) or a}" + (f": deliveries on {days(a)}" if days(a) else "") for a in accts))
        body += ("\n\nNeed more of something, less, a new employee's uniforms, or a skipped week? Just text me at "
                 "866-TAP-TEXT (866-827-8398) or reply to this email, any time, and I'll take care of it.")
        self._send(accts[0], to, "welcome", body, "welcome", "welcome email"
                   + (f" ({len(accts)} locations: {', '.join(accts)})" if len(accts) > 1 else ""))

"""Stopped items still out at the customer: getting them back before they bill as a loss.

A stopped item keeps its inventory until someone lowers it. The next two invoices show it at $0.00 as "STI" (still
in inventory), and after two weeks each piece still there is billed as a loss. Drivers are told to pick them up, but
if the office doesn't lower the inventory by what came back, the customer is billed for all of it (Scott,
2026-10-29). Once per Alliant feed:

  usage items  (mats, towels, linen: anything without a barcode) whose per-delivery count went to 0 with inventory
               left. The customer gets one friendly email listing them and the loss date; around day 7 the route's
               driver is reminded in their route channel (on the stop's delivery day); around day 12 the office gets
               one list in #service-desk of what's still there. Each drops off when its inventory reaches 0 or it
               starts again. A driver's "picked up 8 mats at Safeway" is an inventory decrease like any change.
  garments     of a wearer stopped in Alliant (wearers.csv stopped_date), the barcodes still at the customer
               (garments_in_service.csv, out clean or at the customer). Scanning takes the inventory off, so staff
               aren't told: the customer gets the email with each barcode, and one reminder around day 10 if any are
               still out.

The first run only takes note of what's already there. CUSTOMER_NOTICES=off stops the customer emails; at most
MAX_EMAILS a pass.
"""
import json
import os
import re
import time
from datetime import date, datetime, timedelta

FILE = "stops_watch.json"
LOSS_DAYS = 14
DRIVER_DAY, OFFICE_DAY, CUSTOMER_REMINDER_DAY = 7, 12, 10
MAX_EMAILS = 25
AT_CUSTOMER = {"O", "V"}  # out clean, at the customer (inventory): not back at the plant
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_date(v: str) -> date | None:
    v = (v or "").strip().split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    return None


def nice(d: date) -> str:
    return f"{d:%A, %B} {d.day}"


def title(item: str) -> str:
    return re.sub(r"\b(\d+)X(\d+)\b", r"\1x\2", (item or "").title())


class StopsWatch:
    def __init__(self, desk, today: date | None = None):
        self.desk = desk
        from .context import ALASKA
        self.now = datetime.now(ALASKA)
        self.today = today or self.now.date()

    # ---- state -------------------------------------------------------------------------------------------------

    def _path(self) -> str:
        return os.path.join(self.desk.data_dir, FILE)

    def load(self) -> dict | None:
        try:
            return json.load(open(self._path()))
        except (OSError, ValueError):
            return None

    def save(self, st: dict) -> None:
        with open(self._path() + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(self._path() + ".tmp", self._path())

    # ---- what the feed shows -----------------------------------------------------------------------------------

    def barcoded(self, acct: str) -> set[str]:
        """SKUs on this account that are barcoded garments: scanning handles those."""
        a = self.desk.alliant
        return {r.get("sku", "") for (ac, _), rows in a.in_service.items() if ac == acct for r in rows} | \
               {sku for k, sku in a.sku.items() if len(k) == 3 and k[0] == acct}

    def lines(self) -> dict[str, list]:
        """account\\titem -> [per-delivery count, inventory], usage items only."""
        a, out = self.desk.alliant, {}
        for acct, items in a.items.items():
            garments = None
            for item, inv in items.items():
                sku = a.sku.get((acct, item), "")
                if garments is None:
                    garments = self.barcoded(acct)
                if sku and sku in garments or "GARMENT" in item.upper():
                    continue
                out[f"{acct}\t{item}"] = [a.autocount.get(acct, {}).get(item, inv), inv]
        return out

    def out_garments(self, acct: str, wearer: str) -> list[dict]:
        return [r for r in self.desk.alliant.in_service.get((acct, wearer), [])
                if (r.get("status") or "").upper() in AT_CUSTOMER and r.get("barcode")]

    # ---- the pass ----------------------------------------------------------------------------------------------

    def pass_(self) -> dict:
        a = self.desk.alliant
        now_lines = self.lines()
        st = self.load()
        if st is None:  # first run: what's there now is history
            self.save({"lines": now_lines, "items": {}, "wearers": {f"{x}\t{y}": "seen"
                                                                    for x, y in getattr(a, "stopped_wearers", {})}})
            return {"baseline": len(now_lines)}
        out = {"items": 0, "garments": 0, "emails": 0, "driver": 0, "office": 0}
        today = self.today.isoformat()
        # usage items: per-delivery count >0 -> 0 with inventory left is a stop
        for key, (count, inv) in now_lines.items():
            before = st["lines"].get(key)
            if before and before[0] > 0 and count == 0 and inv > 0 and key not in st["items"]:
                acct, item = key.split("\t", 1)
                st["items"][key] = {"account": acct, "item": item, "inventory": inv, "stopped": today}
                out["items"] += 1
        for key in list(st["items"]):
            count, inv = now_lines.get(key, [0, 0])
            if inv <= 0 or count > 0:  # turned in (inventory lowered) or started again
                st["items"].pop(key)
            else:
                st["items"][key]["inventory"] = inv
        st["lines"] = now_lines
        # garments of newly stopped wearers
        for (acct, wearer), w in getattr(a, "stopped_wearers", {}).items():
            key = f"{acct}\t{wearer}"
            if key in st["wearers"]:
                continue
            stopped = parse_date(w.get("stopped", "")) or self.today
            if (self.today - stopped).days > LOSS_DAYS:
                st["wearers"][key] = "seen"  # long past: nothing to chase now
                continue
            st["wearers"][key] = {"account": acct, "wearer": wearer, "name": w.get("name", ""),
                                  "stopped": stopped.isoformat()}
            out["garments"] += 1
        self.notify(st, out)
        self.save(st)
        return out

    # ---- telling people ----------------------------------------------------------------------------------------

    def notify(self, st: dict, out: dict) -> None:
        budget = MAX_EMAILS
        emails_on = os.environ.get("CUSTOMER_NOTICES", "on").lower() != "off"
        by_acct: dict[str, dict] = {}
        for key, it in st["items"].items():
            if not it.get("emailed"):
                by_acct.setdefault(it["account"], {"items": [], "wearers": []})["items"].append(key)
        for key, w in st["wearers"].items():
            if not isinstance(w, dict):
                continue
            age = (self.today - date.fromisoformat(w["stopped"])).days
            due = not w.get("emailed") or (age >= CUSTOMER_REMINDER_DAY and not w.get("reminded"))
            if due and age <= LOSS_DAYS:
                if self.out_garments(w["account"], w["wearer"]):
                    by_acct.setdefault(w["account"], {"items": [], "wearers": []})["wearers"].append(key)
                elif w.get("emailed"):
                    w["reminded"] = "nothing left out"
        for acct, what in by_acct.items():
            if not emails_on or budget <= 0:
                break
            from .notices import Notices
            to = Notices(self.desk).emails(acct)
            if not to:
                for k in what["items"]:
                    st["items"][k]["emailed"] = "no email on file"
                for k in what["wearers"]:
                    st["wearers"][k].setdefault("emailed", "no email on file")
                continue
            budget -= len(to)
            reminder = not what["items"] and all(st["wearers"][k].get("emailed") for k in what["wearers"])
            self.email(acct, to, [st["items"][k] for k in what["items"]], [st["wearers"][k] for k in what["wearers"]],
                       reminder=reminder)
            out["emails"] += 1
            for k in what["items"]:
                st["items"][k]["emailed"] = self.today.isoformat()
            for k in what["wearers"]:
                w = st["wearers"][k]
                if w.get("emailed"):
                    w["reminded"] = self.today.isoformat()
                else:
                    w["emailed"] = self.today.isoformat()
        out["driver"] = self.remind_drivers(st)
        out["office"] = self.remind_office(st)

    def email(self, acct: str, to: list[str], items: list[dict], wearers: list[dict], reminder: bool = False) -> None:
        a = self.desk.alliant
        name = next((c.name for c in a.customers if c.account == acct), acct).title()
        lines, dues = [], []
        for it in items:
            stopped = date.fromisoformat(it["stopped"])
            dues.append(stopped + timedelta(days=LOSS_DAYS))
            lines.append(f"- {it['inventory']} {title(it['item'])} (stopped {stopped:%B} {stopped.day})")
        for w in wearers:
            stopped = date.fromisoformat(w["stopped"])
            dues.append(stopped + timedelta(days=LOSS_DAYS))
            gs = self.out_garments(acct, w["wearer"])
            lines.append(f"- {w['name'].title() or 'Wearer #' + w['wearer']}'s garments (stopped {stopped:%B} {stopped.day}):")
            lines += [f"    {title(g.get('description') or g.get('sku', ''))} {g.get('size_a') or g.get('sizes') or ''}"
                      .rstrip() + f", barcode {g['barcode']}" for g in gs]
        due = min(dues)
        intro = ("A friendly reminder: we're still looking for" if reminder else
                 "Thank you for being our customer! Our records show these were recently stopped on your account, and "
                 "we'd like to get them back:")
        exact = ("\n\nEach garment has its own barcode on the label inside. We're looking for these exact ones: if a "
                 "different garment comes back in its place, the one listed here still counts as out. A quick check of "
                 "the labels before they go back saves a surprise on your invoice.") if wearers else ""
        body = (f"{intro} {name} ({acct}).\n\n" if reminder else f"{intro}\n\n{name} ({acct})\n") + "\n".join(lines) + exact + (
            f"\n\nPlease hand them to your driver at your next delivery. Anything we haven't gotten back by "
            f"{nice(due)} is billed as a loss, and we'd much rather have them back than charge you for them.\n\n"
            "If they've already gone back to us, thank you, and please ignore this note. Questions? Just reply, "
            "or text me at 866-TAP-TEXT (866-827-8398).")
        from .notices import Notices
        n = Notices(self.desk)
        n._send(acct, to, "items to send back" if not reminder else "a reminder about items to send back", body,
                "stopped" if not reminder else "stopped-reminder",
                f"{'reminder: ' if reminder else ''}stopped items to send back, loss after {due:%b} {due.day}")

    def remind_drivers(self, st: dict) -> int:
        """Around day 7, on the stop's delivery day (day 9 at the latest), in its route channel: what to pick up."""
        a, n = self.desk.alliant, 0
        if self.now.hour < 6:
            return 0
        by_acct: dict[str, list[dict]] = {}
        for it in st["items"].values():
            age = (self.today - date.fromisoformat(it["stopped"])).days
            cust = next((c for c in a.customers if c.account == it["account"]), None)
            on_day = cust and DAYS[self.today.weekday()] in cust.service_days
            if not it.get("driver") and age >= DRIVER_DAY and (on_day or age >= DRIVER_DAY + 2) and age <= LOSS_DAYS:
                by_acct.setdefault(it["account"], []).append(it)
        for acct, its in by_acct.items():
            cust = next((c for c in a.customers if c.account == acct), None)
            due = min(date.fromisoformat(i["stopped"]) for i in its) + timedelta(days=LOSS_DAYS)
            what = ", ".join(f"{i['inventory']} {title(i['item'])}" for i in its)
            route = f"route-{cust.route}" if cust and acct.split("-")[1:2] == ["1"] else ""
            where = self.desk.channels.get(route) or self.desk.desk_id
            self.desk.post(where, f"📦 *{acct} {cust.name if cust else ''}*: please pick up {what} (stopped; billed as "
                                  f"a loss after {due:%b} {due.day}). Post what you picked up here, e.g. "
                                  f"\"picked up {its[0]['inventory']} at {(cust.name.title() if cust else acct)}\".",
                           {"kind": "pickup_reminder", "account": acct})
            for i in its:
                i["driver"] = self.today.isoformat()
            n += 1
        return n

    def remind_office(self, st: dict) -> int:
        """Around day 12: one list in #service-desk of what's still in inventory, before it bills as a loss."""
        due = [i for i in st["items"].values() if not i.get("office")
               and OFFICE_DAY <= (self.today - date.fromisoformat(i["stopped"])).days <= LOSS_DAYS + 7]
        if not due:
            return 0
        a = self.desk.alliant
        lines = []
        for i in sorted(due, key=lambda i: i["stopped"]):
            d = date.fromisoformat(i["stopped"]) + timedelta(days=LOSS_DAYS)
            name = next((c.name for c in a.customers if c.account == i["account"]), "")
            lines.append(f"• *{i['account']}* {name}: {i['inventory']} {i['item']} still in inventory "
                         f"(stopped {i['stopped']}, loss after {d:%b} {d.day})")
            i["office"] = self.today.isoformat()
        self.desk.post(self.desk.desk_id, "📦 *Stopped items still in inventory* — if the driver brought any back, lower "
                                          "the inventory by what came back before the loss bills:\n" + "\n".join(lines),
                       {"kind": "pickup_office"})
        return len(due)

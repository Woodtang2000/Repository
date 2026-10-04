"""Route and service-day context, plus optional Alliant exports.

The channel name gives the route and the message date gives the service day,
which narrows the customer list before the model ever sees the message.

Optional CSV exports (any extra columns are ignored):
  customers.csv      account, name, route, service_days      (service_days like "Mon;Thu")
  current_items.csv  account, item, quantity
"""
import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

ALASKA = ZoneInfo("America/Anchorage")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def route_from_channel(channel_name: str) -> str | None:
    m = re.search(r"route-?(\d+)", channel_name)
    return m.group(1) if m else None


def service_day(ts: str | float) -> str:
    return DAYS[datetime.fromtimestamp(float(ts), ALASKA).weekday()]


def frequency_label(code: str | None) -> str | None:
    """Alliant delivery frequency code in plain words (codes per Snow White's office)."""
    code = (code or "").strip().upper()
    if code in ("1", "2", "3", "4"):
        return f"monthly, week {code}"
    if re.fullmatch(r"A[1-8]", code):
        return f"every 8 weeks, week {code[1]}"
    return {"5": "every other week", "6": "every other week", "7": "weekly", "8": "more than once a week",
            "9": "first delivery only", "0": "no delivery"}.get(code, f"frequency {code}" if code else None)


@dataclass
class Customer:
    account: str
    name: str
    route: str
    service_days: list[str]


@dataclass
class Wearer:
    employee: str  # Alliant wearer number
    first: str
    last: str
    department: str = ""

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}".strip()


@dataclass
class Alliant:
    customers: list[Customer] = field(default_factory=list)
    items: dict[str, dict[str, int]] = field(default_factory=dict)  # account -> item -> qty
    frequency: dict[str, dict[str, str]] = field(default_factory=dict)  # account -> item -> Alliant freq code
    autocount: dict[str, dict[str, int]] = field(default_factory=dict)  # account -> item -> per-delivery autocount, when exported
    wearers: dict[str, list[Wearer]] = field(default_factory=dict)  # account -> wearers
    garments: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)  # (account, wearer#) -> "ITEM SIZE" -> qty
    aliases: dict[str, list[str]] = field(default_factory=dict)  # account -> other names drivers use ("BSI")
    sku: dict[tuple, str] = field(default_factory=dict)  # (account, item) or (account, wearer#, "ITEM SIZE") -> Alliant SKU
    cards: dict[str, dict] = field(default_factory=dict)  # account -> record card header (stop_sequence, special_instructions, ...)
    as_of: str = ""  # date of the export the data came from, e.g. "Oct 3"

    @classmethod
    def from_dir(cls, data_dir: str) -> "Alliant":
        """Load whatever of customers/current_items/garments/wearers.csv exists in `data_dir`."""
        import os
        f = lambda n: os.path.join(data_dir, n) if os.path.exists(os.path.join(data_dir, n)) else None
        data = cls.load(f("customers.csv"), f("current_items.csv"), f("garments.csv"), f("wearers.csv"))
        data.load_aliases(f("aliases.csv") or os.path.join(os.path.dirname(__file__), "aliases.csv"))
        if f("customers.csv"):
            data.as_of = datetime.fromtimestamp(os.path.getmtime(f("customers.csv")), ALASKA).strftime("%b %-d")
        if f("customer_cards.csv"):
            with open(f("customer_cards.csv"), newline="") as fh:
                data.cards = {row["account"]: row for row in csv.DictReader(fh)}
        if f("card_lines.csv") and f("current_items.csv"):
            data._autocount_from_cards(f("current_items.csv"), f("card_lines.csv"))
        return data

    def _autocount_from_cards(self, items_csv: str, card_lines_csv: str) -> None:
        """Take each item's autocount from the record cards (record_cards.py), matched on account, SKU and
        inventory. This replaces any autocount the Item Usage converter guessed by line order."""
        with open(card_lines_csv, newline="") as f:
            cards: dict[tuple[str, str, int], list[int]] = {}
            for row in csv.DictReader(f):
                if not row["wearer"]:
                    cards.setdefault((row["account"], row["sku"], int(row["inventory"])), []).append(int(row["autocount"]))
        with open(items_csv, newline="") as f:
            for row in csv.DictReader(f):
                found = cards.get((row["account"].strip(), row["sku"].strip(), int(row["quantity"])))
                if found:
                    self.autocount.setdefault(row["account"].strip(), {})[row["item"].strip()] = found.pop(0)

    def load_aliases(self, path: str | None) -> None:
        """Nicknames the office keeps in aliases.csv (account, also_called)."""
        import os
        if path and os.path.exists(path):
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    self.aliases.setdefault(row["account"].strip(), []).append(row["also_called"].strip())

    def find_wearer(self, account: str | None, name: str) -> Wearer | None:
        """The wearer on `account` called `name` (first name, last name or both), if exactly one fits."""
        want = re.sub(r"[^a-z ]", "", name.lower()).split()
        if not want:
            return None
        hits = [w for w in self.wearers.get(account or "", [])
                if all(p in re.sub(r"[^a-z ]", "", w.name.lower()).split() for p in want)]
        return hits[0] if len(hits) == 1 else None

    @classmethod
    def load(cls, customers_csv: str | None = None, items_csv: str | None = None,
             garments_csv: str | None = None, wearers_csv: str | None = None) -> "Alliant":
        data = cls()
        if garments_csv:
            with open(garments_csv, newline="") as f:
                for row in csv.DictReader(f):
                    label = f'{row["item"].strip()} {row["size"].strip()}'.strip()
                    data.garments.setdefault((row["account"], row["employee"]), {})[label] = int(row["quantity"])
                    if row.get("sku"):
                        data.sku[(row["account"], row["employee"], label)] = row["sku"].strip()
        if wearers_csv:
            with open(wearers_csv, newline="") as f:
                for row in csv.DictReader(f):
                    data.wearers.setdefault(row["account"], []).append(
                        Wearer(row["employee"], row["first"].strip(), row["last"].strip(), row.get("department", "").strip()))
        if customers_csv:
            with open(customers_csv, newline="") as f:
                for row in csv.DictReader(f):
                    days = [d.strip()[:3].title() for d in re.split(r"[;,/ ]+", row.get("service_days", "")) if d.strip()]
                    data.customers.append(Customer(row["account"].strip(), row["name"].strip(), row["route"].strip(), days))
        if items_csv:
            with open(items_csv, newline="") as f:
                for row in csv.DictReader(f):
                    acct, item = row["account"].strip(), row["item"].strip()
                    data.items.setdefault(acct, {})[item] = int(row["quantity"])
                    if row.get("sku"):
                        data.sku[(acct, item)] = row["sku"].strip()
                    if row.get("frequency"):
                        data.frequency.setdefault(acct, {})[item] = row["frequency"].strip()
                    if (row.get("autocount") or "").strip():
                        data.autocount.setdefault(acct, {})[item] = int(row["autocount"])
        return data

    def match_account(self, name: str | None, route: str | None, day: str) -> str | None:
        """Account number when `name` matches exactly one candidate, ignoring case and punctuation."""
        if not name:
            return None
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        key = norm(name)
        hits = [c.account for c in self.candidates(route, day)
                if norm(c.name) == key or any(norm(a) == key for a in self.aliases.get(c.account, []))]
        return hits[0] if len(hits) == 1 else None

    def candidates(self, route: str | None, day: str) -> list[Customer]:
        """Customers on this route, today's stops first; everyone if the route is unknown.

        The whole route stays in: drivers sometimes post a day after the stop."""
        on_route = [c for c in self.customers if route is None or c.route == route] or self.customers
        return sorted(on_route, key=lambda c: day not in c.service_days)


def account_facts(alliant: "Alliant", account: str) -> str:
    """What a driver may be told about an account: stop, contact, notes, items and wearers. No prices or contract terms."""
    cust = next((c for c in alliant.customers if c.account == account), None)
    card = alliant.cards.get(account, {})
    out = [f"{account} {cust.name if cust else card.get('name', '')}"]
    if card.get("stop_sequence") or cust:
        out.append(f"Route {cust.route if cust else card.get('route', '')} · stops: "
                   + (card.get("stop_sequence") or ", ".join(cust.service_days if cust else [])))
    if card.get("contact") or card.get("phone"):
        out.append(f"Contact: {' '.join(x for x in [card.get('contact'), card.get('phone')] if x)}")
    if (card.get("special_instructions") or "").strip() and "Remittance" not in card["special_instructions"]:
        out.append(f"Card note: {card['special_instructions'].strip()}")
    items = alliant.items.get(account, {})
    if items:
        out.append("Items (per-delivery autocount; inventory; how often):")
        for item, inv in items.items():
            auto = alliant.autocount.get(account, {}).get(item)
            freq = frequency_label(alliant.frequency.get(account, {}).get(item))
            out.append(f"- {item}: {auto if auto is not None else inv} per delivery; inventory {inv}" + (f"; {freq}" if freq else ""))
    wearers = alliant.wearers.get(account, [])
    if wearers:
        out.append("Wearers (number, name: garments size x qty):")
        for w in wearers:
            g = alliant.garments.get((account, w.employee), {})
            out.append(f"- #{w.employee} {w.name}: " + (", ".join(f"{k} x{v}" for k, v in g.items()) or "no garments"))
    return "\n".join(out)

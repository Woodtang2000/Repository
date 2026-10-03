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
    wearers: dict[str, list[Wearer]] = field(default_factory=dict)  # account -> wearers
    garments: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)  # (account, wearer#) -> "ITEM SIZE" -> qty

    @classmethod
    def from_dir(cls, data_dir: str) -> "Alliant":
        """Load whatever of customers/current_items/garments/wearers.csv exists in `data_dir`."""
        import os
        f = lambda n: os.path.join(data_dir, n) if os.path.exists(os.path.join(data_dir, n)) else None
        return cls.load(f("customers.csv"), f("current_items.csv"), f("garments.csv"), f("wearers.csv"))

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
                    if row.get("frequency"):
                        data.frequency.setdefault(acct, {})[item] = row["frequency"].strip()
        return data

    def match_account(self, name: str | None, route: str | None, day: str) -> str | None:
        """Account number when `name` matches exactly one candidate, ignoring case and punctuation."""
        if not name:
            return None
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        hits = [c.account for c in self.candidates(route, day) if re.sub(r"[^a-z0-9]", "", c.name.lower()) == key]
        return hits[0] if len(hits) == 1 else None

    def candidates(self, route: str | None, day: str) -> list[Customer]:
        """Customers on this route, today's stops first; everyone if the route is unknown.

        The whole route stays in: drivers sometimes post a day after the stop."""
        on_route = [c for c in self.customers if route is None or c.route == route] or self.customers
        return sorted(on_route, key=lambda c: day not in c.service_days)

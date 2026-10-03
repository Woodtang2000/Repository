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


@dataclass
class Customer:
    account: str
    name: str
    route: str
    service_days: list[str]


@dataclass
class Alliant:
    customers: list[Customer] = field(default_factory=list)
    items: dict[str, dict[str, int]] = field(default_factory=dict)  # account -> item -> qty

    @classmethod
    def load(cls, customers_csv: str | None = None, items_csv: str | None = None) -> "Alliant":
        data = cls()
        if customers_csv:
            with open(customers_csv, newline="") as f:
                for row in csv.DictReader(f):
                    days = [d.strip()[:3].title() for d in re.split(r"[;,/ ]+", row.get("service_days", "")) if d.strip()]
                    data.customers.append(Customer(row["account"].strip(), row["name"].strip(), row["route"].strip(), days))
        if items_csv:
            with open(items_csv, newline="") as f:
                for row in csv.DictReader(f):
                    data.items.setdefault(row["account"].strip(), {})[row["item"].strip()] = int(row["quantity"])
        return data

    def match_account(self, name: str | None, route: str | None, day: str) -> str | None:
        """Account number when `name` matches exactly one candidate, ignoring case and punctuation."""
        if not name:
            return None
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        hits = [c.account for c in self.candidates(route, day) if re.sub(r"[^a-z0-9]", "", c.name.lower()) == key]
        return hits[0] if len(hits) == 1 else None

    def candidates(self, route: str | None, day: str) -> list[Customer]:
        """Customers on this route serviced today; falls back to the whole route, then everyone."""
        on_route = [c for c in self.customers if route is None or c.route == route]
        today = [c for c in on_route if day in c.service_days]
        return today or on_route or self.customers

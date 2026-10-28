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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ALASKA = ZoneInfo("America/Anchorage")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def route_from_channel(channel_name: str) -> str | None:
    m = re.search(r"route-?(\d+)", channel_name)
    return m.group(1) if m else None


def service_day(ts: str | float) -> str:
    return DAYS[datetime.fromtimestamp(float(ts), ALASKA).weekday()]


def company_for(account: str | None) -> str:
    """Which company signs a message to this account: branch 2 is The Laundry Group, everything else Snow White."""
    parts = (account or "").split("-")
    return "The Laundry Group" if len(parts) > 1 and parts[1] == "2" else "Snow White Linen"


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
    skus: dict[str, dict] = field(default_factory=dict)  # SKU -> its details from skus.csv (no prices)
    ar: dict[str, dict] = field(default_factory=dict)  # account -> balance and last payment (ar.csv): Scott and Bill only
    sku_counts: dict[tuple, int] = field(default_factory=dict)  # (sku, item name) -> accounts using it, company-wide
    cards: dict[str, dict] = field(default_factory=dict)  # account -> record card header (stop_sequence, special_instructions, ...)
    as_of: str = ""  # date of the export the data came from, e.g. "Oct 3"
    exported_at: datetime | None = None  # when the data was exported from Alliant (feed_done.txt, else file time)
    holds: dict[str, list[dict]] = field(default_factory=dict)  # account -> item-level holds (holds.csv)
    # Garment tracking, for office lookups (garment_facts): barcoded garments by (account, wearer#) from
    # garments_in_service.csv, and customer purchase-order lines by account from purchase_orders.csv.
    in_service: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    orders: dict[str, list[dict]] = field(default_factory=dict)
    by_barcode: dict[str, dict] = field(default_factory=dict)  # barcode -> its garments_in_service row
    price_type: dict[str, dict[str, str]] = field(default_factory=dict)  # account -> item -> Alliant price type code
    scans: dict[str, list[tuple]] = field(default_factory=dict)  # barcode -> (datetime, status, text, scan type), 60 days

    def on_hold(self, account: str | None, item: str | None = None, sku: str | None = None) -> list[dict]:
        """Holds on this account, or on this item / SKU on it."""
        hs = self.holds.get(account or "", [])
        if item or sku:
            hs = [h for h in hs if (item and h.get("item", "").upper() == item.upper()) or (sku and h.get("sku") == sku)]
        return hs

    @classmethod
    def from_dir(cls, data_dir: str) -> "Alliant":
        """Load whatever of customers/current_items/garments/wearers.csv exists in `data_dir`."""
        import os
        f = lambda n: os.path.join(data_dir, n) if os.path.exists(os.path.join(data_dir, n)) else None
        data = cls.load(f("customers.csv"), f("current_items.csv"), f("garments.csv"), f("wearers.csv"))
        data.load_aliases(f("aliases.csv") or os.path.join(os.path.dirname(__file__), "aliases.csv"))
        data.load_skus(f("skus.csv"))
        data.load_ar(f("ar.csv"))
        if f("feed_done.txt"):  # the nightly Alliant SQL feed: its export time, written last
            try:
                data.exported_at = datetime.strptime(open(f("feed_done.txt")).read().split()[0], "%Y-%m-%dT%H:%M:%S%z")
            except (ValueError, IndexError):
                pass
        if f("customers.csv") and data.exported_at is None:
            data.exported_at = datetime.fromtimestamp(os.path.getmtime(f("customers.csv")), ALASKA)
        if data.exported_at:
            data.as_of = data.exported_at.astimezone(ALASKA).strftime("%b %-d")
        if f("holds.csv"):
            with open(f("holds.csv"), newline="") as fh:
                for row in csv.DictReader(fh):
                    data.holds.setdefault(row["account"].strip(), []).append({k: (v or "").strip() for k, v in row.items()})
        if f("customer_cards.csv"):
            with open(f("customer_cards.csv"), newline="") as fh:
                data.cards = {row["account"]: row for row in csv.DictReader(fh)}
        if f("garments_in_service.csv"):
            with open(f("garments_in_service.csv"), newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    row = {k: (v or "").strip() for k, v in row.items() if k}
                    data.in_service.setdefault((row.get("cust_code", ""), row.get("wearer", "")), []).append(row)
                    if row.get("barcode"):
                        data.by_barcode[row["barcode"]] = row
        if f("garment_scans.csv"):
            with open(f("garment_scans.csv"), newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    b = (row.get("barcode") or "").strip()
                    if b:
                        data.scans.setdefault(b, []).append(tuple((row.get(k) or "").strip() for k in
                                                                  ("scan_datetime", "status", "scan_text", "scan_type")))
        if f("purchase_orders.csv"):
            # Open lines going back to 2006 are dead orders: keep the last year only (agreed with the Mac session).
            since = ((data.exported_at or datetime.now(ALASKA)).date() - timedelta(days=PO_DAYS)).isoformat()
            with open(f("purchase_orders.csv"), newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    row = {k: (v or "").strip() for k, v in row.items() if k}
                    if row.get("po_date", "") >= since:
                        data.orders.setdefault(row.get("cust_code", ""), []).append(row)
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
                    g = data.garments.setdefault((row["account"], row["employee"]), {})
                    g[label] = g.get(label, 0) + int(row["quantity"])
                    if row.get("sku"):
                        data.sku[(row["account"], row["employee"], label)] = row["sku"].strip()
        if wearers_csv:
            with open(wearers_csv, newline="") as f:
                for row in csv.DictReader(f):
                    if (row.get("stopped_date") or "").strip():  # the feed lists stopped wearers too
                        continue
                    data.wearers.setdefault(row["account"], []).append(
                        Wearer(row["employee"], row["first"].strip(), row["last"].strip(), row.get("department", "").strip()))
        if customers_csv:
            with open(customers_csv, newline="") as f:
                for row in csv.DictReader(f):
                    if re.match(r"99\d{4}-", row["account"].strip()):  # route placeholder accounts ("RTE 1 (DOWNTOWN)")
                        continue
                    days = [d.strip()[:3].title() for d in re.split(r"[;,/ ]+", row.get("service_days", "")) if d.strip()]
                    data.customers.append(Customer(row["account"].strip(), row["name"].strip(), row["route"].strip(), days))
        if items_csv:
            with open(items_csv, newline="") as f:
                for row in csv.DictReader(f):
                    acct, item = row["account"].strip(), row["item"].strip()
                    # The same item can sit on several lines of one account (240 + 160 bath towels): add them up.
                    have = data.items.setdefault(acct, {})
                    have[item] = have.get(item, 0) + int(row["quantity"])
                    if row.get("sku"):
                        data.sku[(acct, item)] = row["sku"].strip()
                    if row.get("frequency"):
                        data.frequency.setdefault(acct, {})[item] = row["frequency"].strip()
                    if (row.get("price_type") or "").strip():  # the code only (8, 9, 14, 21...), never a price
                        data.price_type.setdefault(acct, {})[item] = row["price_type"].strip()
                    if (row.get("autocount") or "").strip():
                        auto = data.autocount.setdefault(acct, {})
                        auto[item] = auto.get(item, 0) + int(row["autocount"])
        return data

    def load_ar(self, path: str | None) -> None:
        """ar.csv from the Alliant feed: account, balance, last_payment_date, last_payment_amount. Only Scott and Bill
        may ever see it (desk.ar_answer), in a private message or by voice: never drivers, customers or channels."""
        self.ar = {}
        if not path:
            return
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                acct = (row.get("account") or "").strip()
                if acct:
                    self.ar[acct] = {k.strip(): (v or "").strip() for k, v in row.items() if k and k.strip() != "account"}

    def load_skus(self, path: str | None) -> None:
        """skus.csv from the Alliant feed: one row per SKU with whatever details it has (description, category, size,
        color, weight, material, pack size...). Price and cost columns are dropped here, so they never reach an answer."""
        self.skus = {}
        if not path:
            return
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                sku = (row.get("sku") or "").strip()
                if sku:
                    self.skus[sku] = {k.strip(): (v or "").strip() for k, v in row.items() if k and (v or "").strip()
                                      and not re.search(r"price|cost|rate|charge|amount", k, re.I)}

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


PO_DAYS = 365
# Alliant's garment count types / statuses (Alliant University file 12), in plain words.
GARMENT_STATUS = {"I": "in soil (back at the plant)", "O": "out clean (going out to the customer)", "M": "in mending",
                  "A": "replaced", "C": "cancelled (removed from service)", "E": "flagged for inspection",
                  "R": "rewash", "U": "custom scan", "V": "at the customer (inventory)", "X": "decreased",
                  "Y": "on-line mend", "Z": "size change"}
MAX_GARMENT_LINES = 150


def _num(v: str) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def garment_line(r: dict) -> str:
    """One barcoded garment: what it is, its last scan, washes and last delivery."""
    st = GARMENT_STATUS.get(r.get("status", ""), f"status {r.get('status', '')}")
    txt = f' "{r["scan_text"]}"' if r.get("scan_text") else ""
    when = r.get("scan_date") or r.get("status_date", "")
    size = " ".join(x for x in (r.get("size_a"), r.get("size_b")) if x)
    extra = [f"{r['washes']} washes" if r.get("washes") else "", f"{r['repairs']} repairs" if _num(r.get("repairs")) else "",
             f"in service since {r['in_service_date']}" if r.get("in_service_date") else "",
             f"last delivered {r['last_delivered']}" if r.get("last_delivered") else ""]
    return (f"{r.get('barcode', '')} {r.get('description') or r.get('sku', '')} {size}: {st}{txt} {when}".rstrip()
            + "".join(f"; {x}" for x in extra if x))


def barcode_facts(alliant: "Alliant", barcodes: list[str]) -> str:
    """Garments looked up by barcode, with whose they are and their recent scans (garment_scans.csv)."""
    out = []
    for b in barcodes:
        r = alliant.by_barcode.get(b)
        if not r:
            continue
        acct, wr = r.get("cust_code", ""), r.get("wearer", "")
        cust = next((c.name for c in alliant.customers if c.account == acct), "")
        w = next((w.name for w in alliant.wearers.get(acct, []) if w.employee == wr), "")
        out.append(f"Garment {garment_line(r)}\n  Belongs to: {acct} {cust}, wearer #{wr} {w}".rstrip())
        scans = alliant.scans.get(b, [])
        if scans:
            out.append("  Recent scans (newest first; status, free text, where scanned):")
            for when, st, txt, kind in sorted(scans, reverse=True)[:8]:
                out.append(f"  - {when}: {GARMENT_STATUS.get(st, 'status ' + st)}" + (f' "{txt}"' if txt else "")
                           + (f" ({kind})" if kind else ""))
    return "\n".join(out)


def garment_facts(alliant: "Alliant", account: str, question: str = "") -> str:
    """Barcoded garments and recent garment orders for the office: each garment's last scan, and PO lines with what's
    been received. Detail for the wearers the question names (or everyone on a small account), else a summary."""
    wearers = {w.employee: w for w in alliant.wearers.get(account, [])}
    words = set(re.findall(r"[a-z]+", question.lower()))
    named = [e for e, w in wearers.items() if words & {p for p in re.findall(r"[a-z]+", w.name.lower()) if len(p) > 2}]
    pieces = {wr: rows for (acct, wr), rows in alliant.in_service.items() if acct == account}
    orders = alliant.orders.get(account, [])
    if not pieces and not orders:
        return ""
    detail = named or (list(wearers) if len(wearers) <= 15 else [])
    who = lambda e: f"#{e} {wearers[e].name}" if e in wearers else f"#{e}"
    out = []
    if pieces:
        out.append("Garments in service (barcode, garment, size: last scan; custom scan text is free text the driver "
                   "or plant typed, up to 10 characters; if it names a different customer, say it may be misdelivered):")
        lines = 0
        for e in sorted(pieces, key=lambda e: (e not in detail, _num(e))):
            rows = pieces[e]
            if e in detail and lines < MAX_GARMENT_LINES:
                out.append(f"- Wearer {who(e)}:")
                for r in rows:
                    out.append("  - " + garment_line(r))
                    lines += 1
            else:
                counts: dict[str, int] = {}
                for r in rows:
                    counts[r.get("status", "")] = counts.get(r.get("status", ""), 0) + 1
                out.append(f"- Wearer {who(e)}: {len(rows)} garments (" + ", ".join(
                    f"{n} {GARMENT_STATUS.get(k, 'status ' + k)}" for k, n in sorted(counts.items())) + ")")
    recent = [o for o in orders if not detail or o.get("wearer") in detail]
    if recent:
        out.append(f"Garment orders (purchase orders from the last {PO_DAYS} days; open = not fully received):")
        for o in sorted(recent, key=lambda o: o.get("po_date", ""), reverse=True)[:60]:
            got, want = _num(o.get("qty_received")), _num(o.get("qty_ordered"))
            state = (f"received {got} of {want}" + (f" on {o['received_date']}" if o.get("received_date") else "")
                     if got else f"none received of {want}")
            if got < want:
                state += " (OPEN" + (f", expected {o['expected_date']}" if o.get("expected_date") else "") + ")"
            size = " ".join(x for x in (o.get("size_a"), o.get("size_b")) if x)
            out.append(f"- PO {o.get('po', '')} {o.get('po_date', '')}, wearer {who(o.get('wearer', ''))}: "
                       f"{o.get('description') or o.get('sku', '')} {size}: {state}"
                       + (f", from {o['vendor_name']}" if o.get("vendor_name") else ""))
    return "\n".join(out)


def route_facts(alliant: "Alliant", route: str, today: datetime | None = None) -> str:
    """One route number, for each company that has it (Snow White's route 1 and The Laundry Group's route 1 are
    different trucks): every customer with delivery days and stop numbers (from the record cards' stop sequence, e.g.
    "Mon 5;Thu 22"), the stop order and count for each day worked out here, today's first, and items delivered less
    often than weekly."""
    today = (today or datetime.now(ALASKA)).astimezone(ALASKA)
    day = DAYS[today.weekday()]
    custs = [c for c in alliant.customers if c.route == route]
    if not custs:
        return ""
    out = [f"Today is {today.strftime('%A %B')} {today.day}, {today.year} ({day}). Account numbers ending -1-xxxxx are "
           "Snow White Linen customers; -2-xxxxx are The Laundry Group. Each company has its own route numbers, so route "
           f"{route} is listed per company. Stop counts, first and last stops are worked out already: use them as given."]
    for branch, company in (("1", "Snow White Linen"), ("2", "The Laundry Group")):
        mine = [c for c in custs if ((c.account.split("-") + ["", ""])[1] == "2") == (branch == "2")]
        if not mine:
            continue
        stops: dict[str, list[tuple[int, str]]] = {}
        lines = []
        for c in mine:
            seq = alliant.cards.get(c.account, {}).get("stop_sequence") or ""
            for d, num in re.findall(r"([A-Za-z]{3})[a-z]*\s*[:\s]\s*(\d+)", seq):
                stops.setdefault(d.title(), []).append((int(num), f"{c.account} {c.name}"))
            odd = [f"{item} {frequency_label(code)}" for item, code in alliant.frequency.get(c.account, {}).items()
                   if frequency_label(code) and frequency_label(code) != "weekly"]
            lines.append(f"- {c.account} {c.name}: {'/'.join(c.service_days) or '?'}; stops {seq or 'not on the card'}"
                         + (f"; {', '.join(odd)}" if odd else ""))
        out.append(f"\n{company} route {route}: {len(mine)} customers")
        for d in [day] + [x for x in DAYS if x != day]:
            if d not in stops:
                if d == day:
                    out.append(f"TODAY ({d}): no stops on {company} route {route}.")
                continue
            order = sorted(stops[d])
            label = f"TODAY ({d})" if d == day else d
            out.append(f"{label}: {len(order)} stops; first {order[0][1]} (stop {order[0][0]}), last {order[-1][1]} "
                       f"(stop {order[-1][0]}). Order: " + ", ".join(f"{n}. {name}" for n, name in order))
        out.append("Customers (account name: delivery days; stop numbers; items not weekly):")
        out += lines
    return "\n".join(out)

def group_totals(alliant: "Alliant", accounts: list[str], question: str = "") -> list[str]:
    """Totals across a group of accounts, added up in code (never by the model): each item (by SKU) with its
    per-delivery count, inventory and how many locations have it, then a subtotal for each kind of item the question
    names ("mats" -> every item with MAT in its name)."""
    per: dict[tuple, list] = {}
    for acct in accounts:
        for item, inv in alliant.items.get(acct, {}).items():
            key = (alliant.sku.get((acct, item), ""), item)
            auto = alliant.autocount.get(acct, {}).get(item)
            t = per.setdefault(key, [0, 0, 0])
            t[0] += auto if auto is not None else inv
            t[1] += inv
            t[2] += 1
    words = {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z]{3,}", question.lower())}
    words = {w for w in words if any(w.upper() in re.findall(r"[A-Z]+", item) for _, item in per)}
    out = []
    for w in sorted(words):
        hits = [(k, v) for k, v in per.items() if w.upper() in re.findall(r"[A-Z]+", k[1])]
        if hits:
            locs = len({a for a in accounts for item in alliant.items.get(a, {}) if w.upper() in re.findall(r"[A-Z]+", item)})
            out.append(f"ALL {w.upper()} ITEMS combined: {sum(v[0] for _, v in hits)} per delivery, inventory "
                       f"{sum(v[1] for _, v in hits)}, {len(hits)} different items, at {locs} of {len(accounts)} locations")
    for (sku, item), (auto, inv, n) in sorted(per.items(), key=lambda kv: (-kv[1][1], kv[0][1])):
        out.append(f"{item}" + (f" ({sku})" if sku else "") + f": {auto} per delivery, inventory {inv}, at {n} locations")
    return out


def group_facts(alliant: "Alliant", accounts: list[str], question: str = "") -> str:
    """Several accounts at once (every Safeway under 4000): the totals, computed here, then one line per account."""
    out = []
    for acct in accounts:
        cust = next((c for c in alliant.customers if c.account == acct), None)
        items = alliant.items.get(acct, {})
        parts = []
        for item, inv in items.items():
            auto = alliant.autocount.get(acct, {}).get(item)
            parts.append(f"{item} {auto if auto is not None else inv}/delivery inv {inv}")
        holds = alliant.on_hold(acct)
        line = f"- {acct} {cust.name if cust else ''} (route {cust.route if cust else '?'}): " + ("; ".join(parts) or "no items")
        if holds:
            line += " | on hold: " + ", ".join(h.get("item", "") for h in holds)
        out.append(line)
    totals = group_totals(alliant, accounts, question)
    return (f"TOTALS across all {len(accounts)} accounts (already added up; use these numbers, don't add them yourself):\n"
            + "\n".join(totals) + f"\n\nPer account (item per-delivery autocount, inventory):\n" + "\n".join(out))


def ar_facts(alliant: "Alliant", accounts: list[str]) -> str:
    """What these accounts owe and when they last paid, with the total worked out here for several. Scott and Bill only."""
    names = {c.account: c.name for c in alliant.customers}
    out, total = [], 0.0
    for a in accounts:
        r = alliant.ar.get(a)
        if r is None:
            out.append(f"- {a} {names.get(a, '')}: nothing in the AR file (probably owes nothing)")
            continue
        try:
            total += float(re.sub(r"[^\d.-]", "", r.get("balance", "")) or 0)
        except ValueError:
            pass
        out.append(f"- {a} {names.get(a, '')} ({company_for(a)}): " + "; ".join(f"{k.replace('_', ' ')} {v}" for k, v in r.items()))
    if len(accounts) > 1:
        out.insert(0, f"TOTAL owed across these {len(accounts)} accounts (already added up): ${total:,.2f}")
    return "\n".join(out)


def sku_facts(alliant: "Alliant", question: str, limit: int = 8) -> str:
    """The SKUs a question is about ("how much does a bath blanket weigh?", "what's 6-01-55?"), with every detail the
    feed has on them. Matched by SKU code, else by the most words shared with the description."""
    skus = getattr(alliant, "skus", {}) or {}
    if not skus:
        return ""
    codes = [s for s in skus if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", question, re.I)]
    if not codes:
        words = {w.rstrip("s") for w in re.findall(r"[a-z0-9]{3,}", question.lower())} - {
            "how", "much", "many", "does", "the", "what", "weigh", "weight", "size", "sku", "item", "our", "are", "for", "and"}
        scored = []
        for s, d in skus.items():
            desc = {w.rstrip("s") for w in re.findall(r"[a-z0-9]{3,}", " ".join(d.values()).lower())}
            hit = len(words & desc)
            if hit:
                scored.append((-hit, s))
        codes = [s for _, s in sorted(scored)[:limit]]
    def detail(k: str, v: str) -> str:
        """Alliant keeps SKU weights in ounces: say so, with pounds worked out here."""
        if re.search(r"weight|wt\b", k, re.I) and not re.search(r"lb|pound", k, re.I):
            try:
                oz = float(re.sub(r"[^\d.]", "", v))
                return f"weight {oz:g} oz (= {oz / 16:.2f} lb)"
            except ValueError:
                pass
        return f"{k} {v}"
    return "\n".join("SKU " + s + ": " + "; ".join(detail(k, v) for k, v in skus[s].items() if k != "sku") for s in codes)


def account_facts(alliant: "Alliant", account: str) -> str:
    """What a driver may be told about an account: stop, contact, notes, items and wearers. No prices or contract terms."""
    cust = next((c for c in alliant.customers if c.account == account), None)
    card = alliant.cards.get(account, {})
    out = [f"{account} {cust.name if cust else card.get('name', '')} ({company_for(account)} customer)"]
    if card.get("stop_sequence") or cust:
        out.append(f"Route {cust.route if cust else card.get('route', '')} · stops: "
                   + (card.get("stop_sequence") or ", ".join(cust.service_days if cust else [])))
    if card.get("address"):
        out.append(f"Address: {card['address']}")
    on_site = " ".join(x for x in [card.get("location_contact"), card.get("phone"), card.get("mobile")] if x)
    if on_site:
        out.append(f"On-site contact: {on_site}")
    if card.get("contact") or card.get("email"):  # the feed's contact/email = who gets the invoices
        out.append(f"Invoice contact: {' '.join(x for x in [card.get('contact'), card.get('email')] if x)}")
    if (card.get("special_instructions") or "").strip() and "Remittance" not in card["special_instructions"]:
        out.append(f"Card note: {card['special_instructions'].strip()}")
    items = alliant.items.get(account, {})
    if items:
        out.append("Items (per-delivery autocount; inventory; how often):")
        for item, inv in items.items():
            auto = alliant.autocount.get(account, {}).get(item)
            freq = frequency_label(alliant.frequency.get(account, {}).get(item))
            out.append(f"- {item}: {auto if auto is not None else inv} per delivery; inventory {inv}" + (f"; {freq}" if freq else ""))
    holds = alliant.on_hold(account)
    if holds:
        out.append("On hold in Alliant: " + ", ".join(
            h.get("item", "") + (f" (wearer #{h['employee']})" if h.get("employee") not in ("", "0") else "") for h in holds))
    wearers = alliant.wearers.get(account, [])
    if wearers:
        out.append("Wearers (number, name: garments size x qty):")
        for w in wearers:
            g = alliant.garments.get((account, w.employee), {})
            out.append(f"- #{w.employee} {w.name}: " + (", ".join(f"{k} x{v}" for k, v in g.items()) or "no garments"))
    return "\n".join(out)

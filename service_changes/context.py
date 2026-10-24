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
    exported_at: datetime | None = None  # when the data was exported from Alliant (feed_done.txt, else file time)
    holds: dict[str, list[dict]] = field(default_factory=dict)  # account -> item-level holds (holds.csv)
    # Garment tracking, for office lookups (garment_facts): barcoded garments by (account, wearer#) from
    # garments_in_service.csv, and customer purchase-order lines by account from purchase_orders.csv.
    in_service: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    orders: dict[str, list[dict]] = field(default_factory=dict)
    by_barcode: dict[str, dict] = field(default_factory=dict)  # barcode -> its garments_in_service row
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
                    if (row.get("autocount") or "").strip():
                        auto = data.autocount.setdefault(acct, {})
                        auto[item] = auto.get(item, 0) + int(row["autocount"])
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


def route_facts(alliant: "Alliant", route: str) -> str:
    """One route: every customer on it with delivery days, stop number on each day (from the record cards' stop
    sequence, e.g. "Mon:5;Thu:22") and any item delivered less often than weekly."""
    custs = [c for c in alliant.customers if c.route == route]
    if not custs:
        return ""
    stops: dict[str, list[tuple[int, str]]] = {}
    out = [f"Route {route}: {len(custs)} customers (account name: delivery days; stop number by day; items not delivered "
           "weekly with how often)"]
    for c in custs:
        seq = alliant.cards.get(c.account, {}).get("stop_sequence") or ""
        for day, num in re.findall(r"([A-Za-z]{3})[a-z]*\s*[:\s]\s*(\d+)", seq):
            stops.setdefault(day.title(), []).append((int(num), f"{c.account} {c.name}"))
        odd = []
        for item, code in alliant.frequency.get(c.account, {}).items():
            label = frequency_label(code)
            if label and label != "weekly":
                odd.append(f"{item} {label}")
        out.append(f"- {c.account} {c.name}: {'/'.join(c.service_days) or '?'}; stops {seq or 'not on the card'}"
                   + (f"; {', '.join(odd)}" if odd else ""))
    for day in DAYS:
        if day in stops:
            out.append(f"Stop order {day}: " + ", ".join(f"{n}. {name}" for n, name in sorted(stops[day])))
    return "\n".join(out)


def group_facts(alliant: "Alliant", accounts: list[str]) -> str:
    """Several accounts at once (every Safeway under 4000), items and holds only: one line per account."""
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
    return f"{len(accounts)} accounts (item per-delivery autocount, inventory):\n" + "\n".join(out)


def account_facts(alliant: "Alliant", account: str) -> str:
    """What a driver may be told about an account: stop, contact, notes, items and wearers. No prices or contract terms."""
    cust = next((c for c in alliant.customers if c.account == account), None)
    card = alliant.cards.get(account, {})
    out = [f"{account} {cust.name if cust else card.get('name', '')}"]
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

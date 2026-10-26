"""Wearer garments whose inventory doesn't match the auto count (equal, or double by the ratio rule) with nothing on order.

Inventory above the auto count is normal for a while: a new or replacement order is waiting to be filled and the old
garments stay in inventory until the new ones are assigned. Otherwise every item's inventory should equal its auto
count or be double it (the ratio rule, Scott 2026-10-24); anything else is a data mismatch for the office to fix,
not a sign the customer did anything wrong (many customers simply don't turn in every garment, Scott 2026-10-26).
Left out: customer-owned NOG garments, COG customers (cog_accounts.txt) and TLG's hospital scrub pools.

  python -m service_changes.garment_check --data service_changes/data > garment_fixes.csv

Needs the auto count in garments.csv (the nightly feed). Open orders come from purchase_orders.csv (last 12 months,
not fully received), matched by account, wearer and SKU; open_work_orders.csv is used too once the feed has it."""
import argparse
import re
import csv
import os
import sys
from collections import defaultdict

from .context import Alliant


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


COG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cog_accounts.txt")


def cog_accounts() -> set[str]:
    """4-digit account numbers of COG customers (cog_accounts.txt): they own their garments, so nothing to check."""
    try:
        with open(COG_FILE) as f:
            return {ln.split()[0] for ln in f if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def is_nog(sku: str, item: str) -> bool:
    """NOG ("not our goods"): the customer's own garments we launder, e.g. 4-01-01 NOG GARMENTS."""
    return sku.startswith("4-01-") or bool(re.search(r"\bNOG\b", item or "", re.I))


def by_account(rows: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """The findings grouped by account, the accounts with the most extra garments first."""
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["account"], r["customer"])].append(r)
    return sorted(((a, c, rs) for (a, c), rs in groups.items()), key=lambda g: -sum(r["extra"] - r["on_order"] for r in g[2]))


def findings(data_dir: str, alliant: Alliant | None = None) -> list[dict]:
    alliant = alliant or Alliant.from_dir(data_dir)
    path = os.path.join(data_dir, "garments.csv")
    if not os.path.exists(path):
        return []
    open_qty: dict[tuple, int] = defaultdict(int)  # (account, wearer, sku) -> pieces still on order
    for acct, lines in alliant.orders.items():
        for o in lines:
            left = (_int(o.get("qty_ordered")) or 0) - (_int(o.get("qty_received")) or 0)
            if left > 0:
                open_qty[(acct, o.get("wearer", ""), o.get("sku", ""))] += left
    wo = os.path.join(data_dir, "open_work_orders.csv")
    if os.path.exists(wo):
        with open(wo, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                left = (_int(r.get("qty_ordered")) or 0) - (_int(r.get("qty_assigned")) or 0)
                if left > 0:
                    open_qty[(r.get("account", ""), r.get("wearer", ""), r.get("sku", ""))] += left
    in_service: dict[tuple, int] = defaultdict(int)
    for (acct, wr), rows in alliant.in_service.items():
        for r in rows:
            if r.get("status") != "C":
                in_service[(acct, wr, r.get("sku", ""))] += 1
    names = {c.account: c.name for c in alliant.customers}
    who = {(a, w.employee): w.name for a, ws in alliant.wearers.items() for w in ws}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    auto_total: dict[str, int] = defaultdict(int)  # account -> its garments' total auto count
    for r in rows:
        if not is_nog(r.get("sku", ""), r.get("item", "")):
            auto_total[r["account"]] += _int(r.get("autocount")) or 0
    out, cog = [], cog_accounts()
    for r in rows:
        if r["account"].split("-")[1:2] == ["2"]:
            continue  # The Laundry Group: hospital scrub pools by size, counted as the carts are built; auto count always 0
        if r["account"].split("-")[0] in cog:
            continue  # COG: the customer bought these garments and pays per wash
        if is_nog(r.get("sku", ""), r.get("item", "")):
            continue  # customer-owned goods we only launder: never delivered as rental, so nothing to stop
        inv, auto = _int(r.get("quantity")), _int(r.get("autocount"))
        if inv is None or auto is None or inv <= auto or (auto and inv == 2 * auto):
            continue  # equal or double the auto count is a correct setup (the ratio rule), not old garments
        key = (r["account"], r["employee"], r.get("sku", ""))
        extra, ordered = inv - auto, open_qty.get(key, 0)
        if ordered >= extra:
            continue  # waiting on an order: normal until it's filled
        out.append({"account": r["account"], "customer": names.get(r["account"], ""), "wearer": r["employee"],
                    "name": who.get((r["account"], r["employee"]), ""), "item": r.get("item", ""), "sku": key[2],
                    "size": r.get("size", ""), "inventory": inv, "auto_count": auto, "extra": extra,
                    "on_order": ordered, "barcodes_in_service": in_service.get(key, 0),
                    "auto_count_unset": auto_total[r["account"]] == 0,
                    "issue": f"inventory {inv} doesn't match the auto count {auto} (not equal or double), nothing on "
                             "order: set the inventory to match, or fix the auto count"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    rows = findings(ap.parse_args().data)
    fields = ["account", "customer", "wearer", "name", "item", "sku", "size", "inventory", "auto_count", "extra",
              "on_order", "barcodes_in_service", "auto_count_unset", "issue"]
    w = csv.DictWriter(sys.stdout, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    print(f"{len(rows)} garment lines to look at", file=sys.stderr)


if __name__ == "__main__":
    main()

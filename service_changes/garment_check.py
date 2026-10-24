"""Wearer garments whose inventory is above the auto count with nothing on order to explain it.

Inventory above the auto count is normal for a while: a new or replacement order is waiting to be filled and the old
garments stay in inventory until the new ones are assigned. Price type 9 bills the auto count, so the customer isn't
charged rent on garments they don't have yet. With **nothing on order**, the extra inventory means the old garments
were never stopped/deassigned (Scott, 2026-10-24). The data isn't clean, the customer may have garments they aren't
billed for, and garments that never come back never get a loss or abuse charge.

  python -m service_changes.garment_check --data service_changes/data > garment_fixes.csv

Needs the auto count in garments.csv (the nightly feed). Open orders come from purchase_orders.csv (last 12 months,
not fully received), matched by account, wearer and SKU; open_work_orders.csv is used too once the feed has it."""
import argparse
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
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            inv, auto = _int(r.get("quantity")), _int(r.get("autocount"))
            if inv is None or auto is None or inv <= auto:
                continue
            key = (r["account"], r["employee"], r.get("sku", ""))
            extra, ordered = inv - auto, open_qty.get(key, 0)
            if ordered >= extra:
                continue  # waiting on an order: normal until it's filled
            out.append({"account": r["account"], "customer": names.get(r["account"], ""), "wearer": r["employee"],
                        "name": who.get((r["account"], r["employee"]), ""), "item": r.get("item", ""), "sku": key[2],
                        "size": r.get("size", ""), "inventory": inv, "auto_count": auto, "extra": extra,
                        "on_order": ordered, "barcodes_in_service": in_service.get(key, 0),
                        "issue": f"{extra - ordered} more in inventory than delivered, nothing on order: stop/deassign "
                                 "the old garments (charge loss if they don't come back) or fix the auto count"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    rows = findings(ap.parse_args().data)
    fields = ["account", "customer", "wearer", "name", "item", "sku", "size", "inventory", "auto_count", "extra",
              "on_order", "barcodes_in_service", "issue"]
    w = csv.DictWriter(sys.stdout, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    print(f"{len(rows)} garment lines to look at", file=sys.stderr)


if __name__ == "__main__":
    main()

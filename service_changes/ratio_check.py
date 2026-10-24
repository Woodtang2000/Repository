"""Items whose inventory is neither equal to nor double the auto count: old entry mistakes to clean up in Alliant
(Scott, 2026-10-24: "on every item the autocount should either match the inventory or be half").

  python -m service_changes.ratio_check --data service_changes/data > ratio_fixes.csv

For each line it suggests the fix that keeps the auto count (what Kirk and Maria pull and the driver delivers) and
sets the inventory to the ratio most of that account's items use. Items with no auto count are left out: those are
the separate zero-autocount list."""
import argparse
import csv
import sys

from .context import Alliant


def mismatches(alliant: Alliant) -> list[dict]:
    names = {c.account: c.name for c in alliant.customers}
    out = []
    for acct, items in sorted(alliant.items.items()):
        autos = alliant.autocount.get(acct, {})
        ratios = [inv / autos[i] for i, inv in items.items() if autos.get(i) and inv in (autos[i], 2 * autos[i])]
        usual = 2 if ratios.count(2.0) > ratios.count(1.0) else 1
        for item, inv in items.items():
            auto = autos.get(item)
            if not auto or inv in (auto, 2 * auto):
                continue
            out.append({"account": acct, "customer": names.get(acct, ""), "item": item,
                        "price_type": alliant.price_type.get(acct, {}).get(item, ""), "auto_count": auto,
                        "inventory": inv, "suggested_inventory": auto * usual,
                        "why": f"most items on this account have inventory {'double' if usual == 2 else 'equal to'} the auto count"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    rows = mismatches(Alliant.from_dir(ap.parse_args().data))
    w = csv.DictWriter(sys.stdout, fieldnames=["account", "customer", "item", "price_type", "auto_count", "inventory",
                                               "suggested_inventory", "why"])
    w.writeheader()
    w.writerows(rows)
    print(f"{len(rows)} items to fix", file=sys.stderr)


if __name__ == "__main__":
    main()

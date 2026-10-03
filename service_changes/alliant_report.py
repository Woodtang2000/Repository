"""Convert Alliant's "Item Usage" report (Excel) into the CSVs the parser reads.

  python -m service_changes.alliant_report Alliant_Item_Usage_Report.xlsx --out data/

Writes:
  customers.csv      account, name, route, service_days, frequency
  current_items.csv  account, item, quantity, sku, days, frequency, unit_price, delivery_unit
  garments.csv       account, employee, sku, size, item, quantity, days, frequency

The report is laid out like the printed page: a "Customer" header row, then one row per
item, then "Total Inventory". Rows with an employee number are wearer garments.
"""
import argparse
import csv
import os
from collections import Counter

DAY_LETTERS = "MTWHFSU"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Column positions in the Excel export.
C_CUST_LABEL, C_ACCOUNT, C_NAME, C_ROUTE, C_CUST_FREQ, C_CUST_DAYS = 0, 5, 16, 46, 64, 78
C_EMPL, C_SKU, C_SIZE, C_DESC, C_DAYS, C_FREQ, C_QTY, C_UNIT_PRICE = 0, 3, 8, 12, 22, 28, 32, 55
C_UNIT_LABEL, C_UNIT_NAME = 3, 9


def decode_days(pattern) -> list[str]:
    """'M  H   ' -> ['Mon', 'Thu']. Each of the 7 positions is one weekday."""
    s = str(pattern or "")
    return [DAY_NAMES[i] for i, ch in enumerate(s[:7]) if ch.strip() and ch == DAY_LETTERS[i]]


def _rows(path):
    import openpyxl
    import openpyxl.reader.excel as excel

    # PDF-to-Excel converters write font settings openpyxl rejects; cell values are fine.
    excel.apply_stylesheet = lambda archive, wb: wb
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    keep_spacing = {C_DAYS, C_CUST_DAYS}  # day letters are positional: "   H   " is Thursday
    for row in wb.worksheets[0].iter_rows(values_only=True):
        yield {i: (v.strip() if isinstance(v, str) and i not in keep_spacing else v)
               for i, v in enumerate(row) if v not in (None, "") and str(v).strip()}


def parse(path):
    customers, items, garments = [], [], []
    cur, unit = None, ""
    for r in _rows(path):
        if r.get(C_CUST_LABEL) == "Customer" and C_ACCOUNT in r:
            cur, unit = str(r[C_ACCOUNT]), ""
            customers.append({"account": cur, "name": r.get(C_NAME, ""), "route": str(r.get(C_ROUTE, "")),
                              "service_days": ";".join(decode_days(r.get(C_CUST_DAYS))),
                              "frequency": str(r.get(C_CUST_FREQ, ""))})
        elif r.get(C_UNIT_LABEL) == "Delivery Unit:":
            unit = str(r.get(C_UNIT_NAME, ""))
        elif cur and C_DESC in r and C_QTY in r and isinstance(r[C_QTY], (int, float)):
            line = {"account": cur, "sku": str(r.get(C_SKU, "")), "item": r[C_DESC], "quantity": int(r[C_QTY]),
                    "days": ";".join(decode_days(r.get(C_DAYS))), "frequency": str(r.get(C_FREQ, ""))}
            if C_EMPL in r:
                garments.append({**line, "employee": str(r[C_EMPL]), "size": str(r.get(C_SIZE, ""))})
            else:
                items.append({**line, "unit_price": r.get(C_UNIT_PRICE, ""), "delivery_unit": unit})
    return customers, items, garments


def write(customers, items, garments, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    # A customer's header repeats when its items run past a page break; keep one row per account.
    customers = list({c["account"]: c for c in customers}.values())

    # The parser looks items up by name per account, so make repeated names unique
    # ("BAR MOP" on two delivery units becomes "BAR MOP [Kitchen]").
    seen = Counter((i["account"], i["item"]) for i in items)
    for i in items:
        if seen[(i["account"], i["item"])] > 1 and i["delivery_unit"]:
            i["item"] = f'{i["item"]} [{i["delivery_unit"]}]'
    seen = Counter((i["account"], i["item"]) for i in items)
    for i in items:  # same item on different delivery days: "BAR MOP (Mon)"
        if seen[(i["account"], i["item"])] > 1:
            i["item"] = f'{i["item"]} ({i["days"] or "freq " + i["frequency"]})'

    for name, rows, cols in [
        ("customers.csv", customers, ["account", "name", "route", "service_days", "frequency"]),
        ("current_items.csv", items, ["account", "item", "quantity", "sku", "days", "frequency", "unit_price", "delivery_unit"]),
        ("garments.csv", garments, ["account", "employee", "sku", "size", "item", "quantity", "days", "frequency"]),
    ]:
        with open(os.path.join(out_dir, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    customers, items, garments = parse(args.report)
    write(customers, items, garments, args.out)
    print(f"{len(customers)} customers, {len(items)} item lines, {len(garments)} garment lines -> {args.out}/")


if __name__ == "__main__":
    main()

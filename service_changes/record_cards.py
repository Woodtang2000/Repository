"""Parse Alliant's Customer Record Cards from the text of the PDF report.

The text is what Dropbox's text extraction returns for the PDF (the `text` field of a fetch). Each
card page starts with "Customer Record Card"; a card that runs onto a second page repeats the header,
so pages are merged by account.

  python -m service_changes.record_cards cards.txt --out service_changes/data

Writes customer_cards.csv (one row per account: contacts, stop sequence, special instructions, contract
dates, stop minimum) and card_lines.csv (one row per item or garment line, with the autocount).
"""
import argparse
import csv
import os
import re

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# "  3-23-03 MAT LOGO SAFETY  3X5 C  2  0  0 21  2 $10.7625 $0.00 N"
# "    45  2-CT10NV 42 R COVERALL NAVY B  2  2  0 9  2 $1.5800 N"   (garment: wearer number first, no flat rate)
LINE = re.compile(
    r"^\s*(?:(?P<wearer>\d+)\s+)?(?P<sku>\d+-[0-9A-Z-]+)\s+(?P<middle>.+?)\s+"
    r"(?P<reg>\d+)\s+(?P<asgn>\d+)\s+(?P<spec>\d+)\s+(?P<ptype>\d+)\s+(?P<auto>\d+)\s+"
    r"\$(?P<price>[\d.]+)(?:\s+\$(?P<flat>[\d.]+))?\s+(?P<tax>[YN])\s*$")


def _first(pattern, text, group=1):
    m = re.search(pattern, text, re.M)
    return m.group(group).strip() if m else ""


def parse_page(page: str) -> dict | None:
    account = _first(r"^Account (\d{4}-\d-\d{5})", page)
    if not account:
        return None
    lines = [ln for ln in page.splitlines() if ln.strip()]

    # Name: the line after the two seven-number rows (stop sequence, bag count).
    name = ""
    for i, ln in enumerate(lines):
        if ln.strip() == "Bag Count" and i + 3 < len(lines):
            name = lines[i + 3].strip()
            break

    # "Route 0 0 0 6 0 0 0": the route on each weekday. The stop-sequence row above it sometimes loses a
    # separator in extraction ("0 0 22 0 0 00"), so pair its non-zero numbers with the delivery days in order.
    by_day = re.search(r"^Route ((?:\d+ ){6}\d+)\s*$", page, re.M)
    route_days = [DAYS[i] for i, v in enumerate(by_day.group(1).split()) if v != "0"] if by_day else []
    stop_row = ""
    for i, ln in enumerate(lines):
        if ln.strip() == "Bag Count" and i + 1 < len(lines):
            stop_row = lines[i + 1]
    stops = [s for s in re.findall(r"\d+", stop_row) if int(s)]
    stop_seq = ";".join(f"{d} {s}" for d, s in zip(route_days, stops))

    special = _first(r"^(.*?)Special Instructions", page)
    contact = _first(r"^Contact (.+?)(?:\s+Contact Phone.*)?$", page)
    # "Email (907)276-1972accounting@bobsservices.com": the contact fax runs into the address.
    email_line = re.sub(r"\(\d{3}\)\d{3}-\d{4}", " ", _first(r"^Email (.*)$", page))
    email = _first(r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", email_line)
    phone = _first(r"Contact Phone (\(\d{3}\)\d{3}-\d{4})", page)
    m = re.search(r"Account Type(\S*)\s+(\S+)\s+(\d{1,2}/\d{1,2}/\d{4})\s+Stop Minimum \$([\d.]+)", page)
    rep, acct_type, installed, stop_min = m.groups() if m else ("", "", "", "")

    items = []
    for i, ln in enumerate(lines):
        mm = LINE.match(ln)
        if not mm:
            continue
        g = mm.groupdict()
        middle = g["middle"].split()
        make_up = middle.pop() if middle and re.fullmatch(r"[A-Z]", middle[-1]) else ""
        # Below each line: "Price Changed / Delivery Days" (with the day letters), then for a garment the
        # locker and the wearer's name, then the delivery frequency as a bare code.
        freq, days, names = "", "", []
        for nxt in lines[i + 1:i + 7]:
            if LINE.match(nxt):
                break
            if "Delivery Days" in nxt:
                dm = re.search(r"Delivery Days (.{7})", nxt)
                days = ";".join(DAYS[k] for k, ch in enumerate(dm.group(1)) if ch == "MTWHFSU"[k]) if dm else ""
            elif re.fullmatch(r"\s*(?:\d|[A-Z]\d)\s*", nxt):  # "7", "A2"; a bare "M" is a wearer label
                freq = nxt.strip()
                break
            else:
                names.append(nxt.strip())
        size, desc = [], list(middle)
        if g["wearer"]:  # garment: "32 32 PANT WORK BLACK", "XL SHIRT ...", "CUS TOM SHIRT ..."
            while len(desc) > 1 and (len(desc[0]) <= 3 or re.search(r"\d", desc[0])):
                size.append(desc.pop(0))
        items.append({"account": account, "wearer": g["wearer"] or "", "wearer_name": names[-1] if names else "",
                      "sku": g["sku"], "size": " ".join(size), "description": " ".join(desc),
                      "make_up": make_up, "inventory": int(g["reg"]), "assigned": int(g["asgn"]),
                      "special": int(g["spec"]), "autocount": int(g["auto"]), "unit_price": g["price"],
                      "frequency": freq, "days": days})
    return {"account": account, "name": name, "route": _first(r"^Route (\d+|[A-Z]\d?)\s*$", page),
            "service_days": ";".join(route_days),
            "stop_sequence": stop_seq, "contact": contact, "phone": phone, "email": email,
            "special_instructions": special, "sales_rep": rep, "account_type": acct_type,
            "install_date": installed, "contract_expires": _first(r"Contract ExpState Tax \d+ (\d{1,2}/\d{1,2}/\d{4})", page),
            "stop_minimum": stop_min, "items": items}


def parse_text(text: str) -> dict[str, dict]:
    cards: dict[str, dict] = {}
    for page in text.split("Customer Record Card")[1:]:
        card = parse_page(page)
        if not card:
            continue
        if card["account"] in cards:  # continuation page: keep the header, add the lines
            cards[card["account"]]["items"] += card["items"]
        else:
            cards[card["account"]] = card
    return cards


def write(cards: dict[str, dict], out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    head = ["account", "name", "route", "service_days", "stop_sequence", "contact", "phone", "email", "special_instructions",
            "sales_rep", "account_type", "install_date", "contract_expires", "stop_minimum"]
    with open(os.path.join(out_dir, "customer_cards.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=head, extrasaction="ignore")
        w.writeheader()
        w.writerows(cards.values())
    cols = ["account", "wearer", "wearer_name", "sku", "size", "description", "make_up", "inventory", "assigned",
            "special", "autocount", "unit_price", "frequency", "days"]
    with open(os.path.join(out_dir, "card_lines.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in cards.values():
            w.writerows(c["items"])


def write_bot_data(cards: dict[str, dict], out_dir: str):
    """The four files the parser loads (customers, current_items, garments, wearers), built from the cards
    alone, so the Record Cards PDF is the only export the bot needs."""
    from collections import Counter

    os.makedirs(out_dir, exist_ok=True)
    customers = [{"account": c["account"], "name": c["name"], "route": c["route"], "service_days": c["service_days"]}
                 for c in cards.values()]
    items, garments, wearers = [], [], {}
    for c in cards.values():
        for ln in c["items"]:
            if ln["wearer"]:
                garments.append({"account": ln["account"], "employee": ln["wearer"], "sku": ln["sku"], "size": ln["size"],
                                 "item": ln["description"], "quantity": ln["assigned"] or ln["inventory"],
                                 "days": ln["days"], "frequency": ln["frequency"]})
                parts = ln["wearer_name"].split()
                wearers.setdefault((ln["account"], ln["wearer"]), {
                    "account": ln["account"], "employee": ln["wearer"], "first": parts[0] if parts else "",
                    "last": " ".join(parts[1:]), "department": ""})
            else:
                items.append({"account": ln["account"], "item": ln["description"], "quantity": ln["inventory"],
                              "autocount": ln["autocount"], "sku": ln["sku"], "days": ln["days"],
                              "frequency": ln["frequency"], "unit_price": ln["unit_price"]})
    seen = Counter((i["account"], i["item"]) for i in items)
    for i in items:  # same item on two delivery schedules: "BAR MOP (Mon)"
        if seen[(i["account"], i["item"])] > 1:
            i["item"] = f'{i["item"]} ({i["days"] or "freq " + i["frequency"]})'
    for name, rows in [("customers.csv", customers), ("current_items.csv", items),
                       ("garments.csv", garments), ("wearers.csv", list(wearers.values()))]:
        with open(os.path.join(out_dir, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="text of the Customer Record Cards PDF (or the JSON a Dropbox fetch saved)")
    ap.add_argument("--out", default="data")
    ap.add_argument("--bot-data", action="store_true",
                    help="also write customers/current_items/garments/wearers.csv from the cards (no other exports needed)")
    args = ap.parse_args()
    raw = open(args.text).read()
    if raw.lstrip().startswith("{"):
        import json
        raw = json.loads(raw)["text"]
    cards = parse_text(raw)
    write(cards, args.out)
    if args.bot_data:
        write_bot_data(cards, args.out)
    print(f"{len(cards)} accounts, {sum(len(c['items']) for c in cards.values())} lines -> {args.out}/")


if __name__ == "__main__":
    main()

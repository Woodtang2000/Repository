"""Drivers asking the plant to pull garments (a service code in Alliant: repair, inspection/audit, relabel) or for an
inventory at an account. Turns the parsed request into the office ticket, the driver's readback and any questions,
checked against last night's garment data (garments_in_service.csv).

Snow White's own two-letter codes go in service_codes.csv (code,kind,meaning); until then the ticket says which kind
of code to pick."""
import csv
import os

from .context import Alliant, garment_line

KINDS = {"repair": ("🧵", "repair"), "inspection": ("🔍", "inspection / audit"), "relabel": ("🏷️", "relabel"),
         "inventory": ("📋", "inventory count"), "other": ("🛠️", "service")}
HOW = {"garment": 'In Alliant: "Inquiry" > "Garment" tab > scan or type the barcode > "Add Service Code"',
       "wearer": 'In Alliant: "Stockroom" > "Add/Remove Service Code" > scope "Specific Wearers" > "Append" > "Begin"',
       "customer": 'In Alliant: "Stockroom" > "Add/Remove Service Code" > scope "Specific Customer" (pick the SKU if it's one garment type) > "Append" > "Begin"'}


def codes(path: str = os.path.join(os.path.dirname(__file__), "service_codes.csv")) -> dict[str, tuple[str, str]]:
    """kind -> (code, meaning) for the first code of each kind in service_codes.csv."""
    out: dict[str, tuple[str, str]] = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                out.setdefault((row.get("kind") or "").strip().lower(), ((row.get("code") or "").strip(),
                                                                          (row.get("meaning") or "").strip()))
    return out


def review(parsed, alliant: Alliant) -> tuple[list[str], str, list[str]]:
    """(ticket lines, readback for the driver, questions). May fill parsed.account_number from a barcode."""
    f = parsed.service
    if f is None:
        return [f"🛠️ *Service request*", parsed.summary], f"✅ Office has it: {parsed.summary}", []
    kind = f.kind if f.kind in KINDS else "other"
    icon, label = KINDS[kind]
    lines, questions, target = [], [], ""
    found = [(b, alliant.by_barcode.get(b)) for b in f.barcodes]
    for _, row in found:  # the barcode tells us whose garment it is
        if row and not parsed.account_number:
            parsed.account_number = row.get("cust_code")
    acct = parsed.account_number
    cust = next((c for c in alliant.customers if c.account == acct), None)
    lines.append(f"{icon} *{label.capitalize()}{' requested' if kind == 'inventory' else ' (service code)'}*")
    lines.append(f"*{acct}*  {cust.name}" if cust else f"⚠️ *Account not matched*  {parsed.customer_as_written or 'customer?'}")
    pieces = lambda wr=None: [r for (a, w), rows in alliant.in_service.items() if a == acct and (wr is None or w == wr)
                              for r in rows]
    scope = f.scope if f.scope in ("garment", "wearer", "customer") else "garment"
    if kind == "inventory":
        scope = "customer"
    if scope == "garment":
        if not f.barcodes:
            if f.wearer:
                scope = "wearer"
            else:
                questions.append("Which garment? Send the barcode number (or the wearer's name if it's all their garments).")
        for b, row in found:
            if not row:
                lines.append(f"⚠️ Barcode *{b}* isn't in last night's Alliant data: check the number")
                continue
            whose = next((w.name for w in alliant.wearers.get(row.get("cust_code", ""), []) if w.employee == row.get("wearer")), "")
            other = row.get("cust_code") != acct
            lines.append(f"• {garment_line(row)}" + (f" · wearer #{row.get('wearer')} {whose}" if row.get("wearer") else "")
                         + (f" · ⚠️ belongs to {row.get('cust_code')}" if other else ""))
        target = ("garment " + ", ".join(f.barcodes)) if f.barcodes else ""
    if scope == "wearer":
        w = alliant.find_wearer(acct, f.wearer or "") if acct else None
        if w:
            n = len(pieces(w.employee))
            lines.append(f"• Wearer *#{w.employee} {w.name}*: all {n} garment{'s' if n != 1 else ''} in service")
            target = f"{w.name}'s garments"
        elif acct:
            questions.append(f"Which employee? I couldn't find {f.wearer or 'that wearer'} on this account.")
    if scope == "customer":
        n = len(pieces())
        lines.append(f"• Whole account: {n} garment{'s' if n != 1 else ''} in service" if n else "• Whole account")
        target = "the whole account" if kind != "inventory" else (cust.name if cust else "the account")
    if not acct:
        questions.append("Which customer?")
    lines.append(f"Reason: {f.reason}")
    if kind == "inventory":
        lines.append("Schedule the count with the route supervisor; enter the counts in Alliant when they come back.")
        rb = f"✅ Inventory count scheduled for {target}" + (f" ({f.reason})" if f.reason else "") + "."
    else:
        code = codes().get(kind)
        lines.append(f"Code: `{code[0]}` {code[1]}".rstrip() if code else f"Code: pick the {label} service code")
        lines.append(HOW[scope])
        rb = f"✅ Service code on {target or 'it'} for {label} ({f.reason}): the plant will pull it at the next scan."
    return lines, rb, questions

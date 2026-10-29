"""The money files for Scott and Bill only: data/private/ (ar, open_invoices, pricing, account_details), mirrored from
Dropbox's "private - Scott and Bill only" folder by deploy/dropbox_sync.py.

Only desk.private_answer reads this, after checking it's Scott or Bill (AR_SLACK_IDS) in a private message or by
voice. Nothing here goes into the facts drivers, office staff or customers are answered from, and unlike skus.csv no
price columns are dropped: prices are the point. Files are read fresh when they change (by modification time).
"""
import csv
import os
import re

FILES = ("ar.csv", "open_invoices.csv", "pricing.csv", "account_details.csv")
_cache: dict[str, tuple[float, dict[str, list[dict]]]] = {}


def _by_account(path: str) -> dict[str, list[dict]]:
    """Rows keyed by account; the account column may be account, cust_code or customer."""
    stamp = os.path.getmtime(path)
    if path in _cache and _cache[path][0] == stamp:
        return _cache[path][1]
    out: dict[str, list[dict]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            acct = row.get("account") or row.get("cust_code") or row.get("customer") or ""
            if acct:
                out.setdefault(acct, []).append(row)
    _cache[path] = (stamp, out)
    return out


def load(data_dir: str) -> dict[str, dict[str, list[dict]]]:
    """file stem -> account -> rows, for whichever private files exist (ar.csv also read from the old data/ar.csv)."""
    out = {}
    for name in FILES:
        path = os.path.join(data_dir, "private", name)
        if name == "ar.csv" and not os.path.exists(path):
            path = os.path.join(data_dir, "ar.csv")
        if os.path.exists(path):
            out[name[:-4]] = _by_account(path)
    return out


def _money(v: str) -> float:
    try:
        return float(re.sub(r"[^\d.-]", "", v) or 0)
    except ValueError:
        return 0.0


def _line(row: dict, skip=("account", "cust_code")) -> str:
    return "; ".join(f"{k.replace('_', ' ')} {v}" for k, v in row.items() if v and k not in skip)


def facts(data_dir: str, accounts: list[str], question: str, names: dict[str, str] | None = None) -> str:
    """Everything private on these accounts that the question could need: balance and aging, open invoices, price lines
    (all, or the ones whose item the question names) and the account's details. Totals are added up here."""
    data = load(data_dir)
    if not data:
        return ""
    names = names or {}
    words = {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", question.lower())}
    wants = lambda *keys: any(re.search(k, question, re.I) for k in keys)
    show_all = not wants(r"owe|balance|past due|aging|paid|payment|invoice|pric|charge|rate|cost|contract|expir|terms|"
                         r"rep\b|bill.?to|contact|minimum|insurance|replacement|statement")
    out, total = [], 0.0
    for a in accounts[:60]:
        head = f"{a} {names.get(a, '')}".strip()
        part = []
        ar = data.get("ar", {}).get(a)
        if show_all or wants(r"owe|balance|past due|aging|paid|payment|credit|collect"):
            if ar:
                total += _money(ar[0].get("balance", ""))
                part.append("Balance and aging: " + _line(ar[0]))
            elif "ar" in data:
                part.append("Balance: nothing owed (not in the AR file)")
        inv = data.get("open_invoices", {}).get(a, [])
        if inv and (show_all or wants(r"invoice|owe|past due|balance|open")):
            inv = sorted(inv, key=lambda r: r.get("inv_date", ""))
            part.append(f"Open invoices ({len(inv)}, oldest first, total open "
                        f"${sum(_money(r.get('balance', '')) for r in inv):,.2f}):")
            part += ["  - " + _line(r, skip=("account", "cust_code", "name")) for r in inv[:40]]
        prices = data.get("pricing", {}).get(a, [])
        if prices and (show_all or wants(r"pric|charge|rate|cost|minimum|insurance|replacement|lost|abuse|flat")):
            named = [r for r in prices if words & {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", r.get("description", "").lower())}]
            rows = named or prices
            part.append(f"Price lines ({len(rows)}{' matching the question' if named else ''}):")
            part += ["  - " + _line(r, skip=("account", "cust_code", "name")) for r in rows[:80]]
        det = data.get("account_details", {}).get(a)
        if det and (show_all or wants(r"contract|expir|terms|rep\b|sales|bill.?to|contact|statement|national|group|"
                                      r"aging|start")):
            part.append("Account details: " + _line(det[0], skip=("account", "cust_code")))
        out.append(f"{head}:\n" + ("\n".join(part) if part else "nothing in the private files"))
    if len(accounts) > 1 and "ar" in data:
        out.insert(0, f"TOTAL owed across these {len(accounts)} accounts (already added up): ${total:,.2f}")
    return "\n\n".join(out)

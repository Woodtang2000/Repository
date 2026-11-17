"""Who is this? A best guess from Salesforce (Scott, 11/16): the phone numbers and emails on Salesforce Contacts and
Accounts of our Alliant customers, so a text, email or call from someone not linked yet shows the office which account
it's probably from.

A guess for the office only. It never links a number, never gives a caller account details, and Donna never says it
to the person: the office still taps Add (or types `add … to …`) after making sure it's really them.

The copy is refreshed once a day (desk.new_accounts_pass) into data/sf_directory.json:
  {"at": time, "phones": {"9075551234": [[account number, contact or account name], ...]}, "emails": {...}}
Account number = Salesforce's AccountNumber, Alliant's first part (1205 for 1205-1-00004 and 1205-2-00001).
"""
import json
import os
import re
import time
import urllib.parse

FILE = "sf_directory.json"
MAX_AGE = 24 * 3600
TOO_COMMON = 3  # a number or address on more than this many accounts (a franchise office, a bookkeeper): no guess
OURS = {"9072227738", "9075190761", "9075190762", "9078022449", "9075190752", "8668278398", "8885197604",
        "9078022418", "9078022447", "9078022396", "9072584200", "9072790251"}


def _digits(s) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    return d[-10:] if len(d) >= 10 else ""


def build(data_dir: str) -> dict:
    """Read Salesforce (Contacts and Accounts of live Alliant customers) and save the directory. Read-only there."""
    from . import salesforce
    if not salesforce.configured():
        return {}
    token, instance = salesforce._token()
    base = f"{instance}/services/data/{salesforce.API}"

    def rows(soql: str) -> list[dict]:
        out, url = [], f"{base}/query?" + urllib.parse.urlencode({"q": soql})
        while url:
            r = salesforce._call("GET", url, token)
            out += r.get("records") or []
            url = (instance + r["nextRecordsUrl"]) if r.get("nextRecordsUrl") else ""
        return out

    phones: dict[str, list] = {}
    emails: dict[str, list] = {}
    names: dict[str, list] = {}  # account number -> contact names, lowercase (the Alliant comparison)

    def add(table, key, acct, label):
        if key and acct and [acct, label] not in table.setdefault(key, []):
            table[key].append([acct, label])

    for c in rows("SELECT Name, Phone, MobilePhone, Email, Account.AccountNumber, Account.Name FROM Contact "
                  "WHERE Account.Alliant_Account__c = true"):
        acct = str((c.get("Account") or {}).get("AccountNumber") or "").strip()
        label = f"{c.get('Name') or ''} at {(c.get('Account') or {}).get('Name') or ''}".strip()
        for p in (c.get("Phone"), c.get("MobilePhone")):
            add(phones, _digits(p), acct, label)
        add(emails, (c.get("Email") or "").strip().lower(), acct, label)
        n = " ".join((c.get("Name") or "").lower().split())
        if acct and n and n not in names.setdefault(acct, []):
            names[acct].append(n)
    for a in rows("SELECT Name, Phone, AccountNumber FROM Account WHERE Alliant_Account__c = true AND Phone != null"):
        add(phones, _digits(a.get("Phone")), str(a.get("AccountNumber") or "").strip(), f"{a.get('Name') or ''} (main number)")
    accounts = sorted({str(a.get("AccountNumber") or "").strip() for a in
                       rows("SELECT AccountNumber FROM Account WHERE Alliant_Account__c = true")} - {""})
    data = {"at": time.time(), "phones": phones, "emails": emails, "names": names, "accounts": accounts}
    path = os.path.join(data_dir, FILE)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)
    return data


def load(data_dir: str) -> dict:
    try:
        with open(os.path.join(data_dir, FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def refresh(data_dir: str, now: float | None = None) -> bool:
    """Rebuild when the copy is a day old (or missing). True if it was rebuilt."""
    now = now or time.time()
    if now - (load(data_dir).get("at") or 0) < MAX_AGE:
        return False
    build(data_dir)
    return True


def guess(data_dir: str, alliant, addr: str) -> tuple[list[str], str]:
    """(Alliant accounts it's probably from, a line for the office card) for a phone number or email; ([], "") if
    Salesforce doesn't know it, or knows it on too many accounts to guess."""
    d = load(data_dir)
    if not d:
        return [], ""
    a = (addr or "").strip().lower()
    key = a if "@" in a else _digits(a)
    if not key or key in OURS:
        return [], ""
    hits = (d.get("emails" if "@" in key else "phones") or {}).get(key) or []
    bases = list(dict.fromkeys(h[0] for h in hits))
    if not bases or len(bases) > TOO_COMMON:
        return [], ""
    accounts = [c.account for c in getattr(alliant, "customers", []) if c.account.split("-")[0] in bases][:20]
    if not accounts:
        return [], ""
    who = "; ".join(f"{label} ({acct})" for acct, label in hits[:3])
    return accounts, f"📇 Salesforce has this {'email' if '@' in key else 'number'} for: {who}"


def compare(data_dir: str, alliant) -> list[dict]:
    """Every person on an Alliant customer card (the invoice contact and the on-site contact) that isn't on the
    matching Salesforce Account: no Contact there with the same phone, email or name. Read-only; for the Sales agent
    to import after Scott's OK. Sub-accounts billed by another company (Costco, Safeway) are left out."""
    from .context import sub_account
    d = load(data_dir)
    in_sf = set(d.get("accounts") or [])
    have = {}
    for table in ("phones", "emails"):
        for key, hits in (d.get(table) or {}).items():
            for acct, _ in hits:
                have.setdefault(acct, set()).add(key)
    out = []
    names = {c.account: c.name for c in getattr(alliant, "customers", [])}
    for account, card in sorted(getattr(alliant, "cards", {}).items()):
        if account not in names or sub_account(account):
            continue
        base = account.split("-")[0]
        known, known_names = have.get(base, set()), set(d.get("names", {}).get(base, []))
        people = [("invoice contact", card.get("contact"), "", "", card.get("email")),
                  ("on-site contact", card.get("location_contact"), card.get("phone"), card.get("mobile"),
                   card.get("location_email"))]
        for role, who, phone, mobile, email in people:
            who = " ".join(str(who or "").split())
            keys = {k for k in (_digits(phone), _digits(mobile), str(email or "").strip().lower()) if k and k not in OURS}
            if not who and not keys:
                continue
            if keys & known or (who and who.lower() in known_names):
                continue
            out.append({"account": account, "location": names[account], "salesforce_account": base,
                        "account_in_salesforce": "yes" if base in in_sf else "NO", "role": role, "name": who,
                        "phone": str(phone or "").strip(), "mobile": str(mobile or "").strip(),
                        "email": str(email or "").strip()})
    return out


def main() -> None:
    """python -m service_changes.sf_directory --compare : refresh the Salesforce copy, then write
    data/alliant_contacts_not_in_salesforce.csv and print the counts. Reads Salesforce; writes nothing there."""
    import argparse
    import csv
    from .context import Alliant
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--data", default="service_changes/data")
    a = ap.parse_args()
    if not a.compare:
        ap.print_help()
        return
    build(a.data)
    rows = compare(a.data, Alliant.from_dir(a.data))
    path = os.path.join(a.data, "alliant_contacts_not_in_salesforce.csv")
    fields = ["account", "location", "salesforce_account", "account_in_salesforce", "role", "name", "phone", "mobile", "email"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    locs = {r["account"] for r in rows}
    no_acct = {r["salesforce_account"] for r in rows if r["account_in_salesforce"] == "NO"}
    print(f"{len(rows)} Alliant card contacts aren't in Salesforce, across {len(locs)} locations "
          f"({sum(r['role'] == 'invoice contact' for r in rows)} invoice, {sum(r['role'] == 'on-site contact' for r in rows)} "
          f"on-site). {len(no_acct)} account numbers have no Salesforce Account at all. List: {path}")


if __name__ == "__main__":
    main()

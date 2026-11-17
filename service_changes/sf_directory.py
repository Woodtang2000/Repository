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
    for a in rows("SELECT Name, Phone, AccountNumber FROM Account WHERE Alliant_Account__c = true AND Phone != null"):
        add(phones, _digits(a.get("Phone")), str(a.get("AccountNumber") or "").strip(), f"{a.get('Name') or ''} (main number)")
    data = {"at": time.time(), "phones": phones, "emails": emails}
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

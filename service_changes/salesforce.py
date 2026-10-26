"""Salesforce Accounts and Opportunities for website chat leads, standard library only. The team works in Accounts and
Opportunities (New Account (Guided) / New Opportunity (Guided)) and has never used Leads, so a "Give to" click makes
the same records: the Account (reused when one already has that name), a Contact for the person, and a Prospecting
Opportunity owned by whoever the button names. Salesforce's own flows then fill the account number and copy the
address and phone onto the Opportunity.

Uses the OAuth 2.0 client credentials flow: an external client app in Salesforce with "Enable Client Credentials
Flow" and a run-as user that may create records. Needs SF_DOMAIN (e.g. snowwhite.my.salesforce.com), SF_CLIENT_ID,
SF_CLIENT_SECRET.
"""
import json
import os
import re
from datetime import date, timedelta
import urllib.error
import urllib.parse
import urllib.request

API = "v62.0"
# Who Bill can give a lead to: key -> (name, Salesforce user id)
OWNERS = {"bill": ("Bill Dubinsky", "0051W000004Fa1pQAC"), "jesse": ("Jesse James Tingley", "0051W000005qDzpQAE"),
          "scott": ("Scott Woodland", "005400000016E1ZAAU")}
VENDORS = ["Alsco", "Cintas", "UniFirst", "Quantum Laundry", "Hills Commercial"]  # the Account Current Vendor picklist
INDUSTRIES = [("Restaurant", r"restaurant|pizza|cafe|caf\u00e9|coffee|bar\b|grill|diner|bakery|brew|kitchen|catering"),
              ("Lodging", r"hotel|motel|lodge|inn\b|b&b|bed and breakfast|resort|hostel"),
              ("Healthcare", r"clinic|medical|dental|dentist|health|hospital|vet\b|veterinar|pharmac"),
              ("Auto", r"auto|garage|mechanic|tire|body shop|car wash|dealer"), ("Construction", r"construct|contractor|builder"),
              ("Government", r"city of|borough|municipal|state of|school|district"), ("Office", r"office"),
              ("Manufacturing", r"manufactur|plant|factory|fish|seafood|processing"), ("Retail", r"retail|store|shop|market")]
STAGE = "Prospecting"  # the first stage of the pipeline
PRODUCTS = [("Uniforms", r"uniform|shirt|pant|coverall|coat|scrub|jacket"), ("Linens", r"linen|sheet|napkin|table|gown|blanket|pillow"),
            ("Towels", r"towel|bar mop|rag|microfiber"), ("Aprons", r"apron"), ("Mats", r"\bmats?\b"), ("Paper", r"paper|toilet|tissue")]
COMPANY = {"sw": "Snow White Linen Supply", "tlg": "The Laundry Group"}


def configured() -> bool:
    return all(os.environ.get(v) for v in ("SF_DOMAIN", "SF_CLIENT_ID", "SF_CLIENT_SECRET"))


class SalesforceError(RuntimeError):
    def __init__(self, message: str, errors):
        super().__init__(message)
        self.errors = errors if isinstance(errors, list) else []  # [{"message", "errorCode", "duplicateResult"...}]


def _call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # Salesforce says why in the body, e.g. [{"message": ..., "errorCode": ...}]
        text = e.read().decode()
        try:
            errors = json.loads(text)
        except ValueError:
            errors = []
        raise SalesforceError(f"{method} {url.split('/services/')[-1][:80]}: {e.code} {text[:400]}", errors) from None


def _token() -> tuple[str, str]:
    req = urllib.request.Request(f"https://{os.environ['SF_DOMAIN']}/services/oauth2/token", method="POST",
                                 data=urllib.parse.urlencode({"grant_type": "client_credentials",
                                                              "client_id": os.environ["SF_CLIENT_ID"],
                                                              "client_secret": os.environ["SF_CLIENT_SECRET"]}).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = json.loads(r.read())
    return t["access_token"], t.get("instance_url") or f"https://{os.environ['SF_DOMAIN']}"


def _q(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def description(lead: dict) -> str:
    text = " ".join(str(lead.get(k) or "") for k in ("needs", "notes", "transcript"))
    quote = lead.get("source") == "quote"
    desc = [f"[{COMPANY.get(lead.get('company'), '')}] Website {'quote request' if quote else 'chat lead'}"]
    if lead.get("handed_from"):
        desc.append(f"Came in on the {COMPANY.get(lead['handed_from'], '')} site and was handed over")
    products = [p for p, rx in PRODUCTS if re.search(rx, text, re.I)]
    if products:
        desc.append("Products: " + ", ".join(products))
    for label, key in (("Contact", "name"), ("Type", "business_type"), ("Size", "size"), ("Needs", "needs" if not quote else ""),
                       ("Uses now", "current_provider"), ("Contract ends", "contract_end"), ("Best time", "best_time"),
                       ("Notes", "notes")):
        if key and lead.get(key):
            desc.append(f"{label}: {lead[key]}")
    if quote:
        from .catalog import line_text
        desc += ["", "Items requested (no prices given; Alliant SKU in brackets):"] + \
            [f"- {line_text(x)}" for x in lead.get("items") or []] + [f"- Interested in: {i}" for i in lead.get("interests") or []]
        if lead.get("other"):
            desc.append(f"- Something else: {lead['other']}")
    else:
        desc += ["", "Chat:", lead.get("transcript") or ""]
    return "\n".join(desc)[:31000]


def account_body(lead: dict, owner_id: str) -> dict:
    """A new prospect Account: the chat's details, plus what the web lookup found (lead["found"])."""
    found = lead.get("found") or {}
    vendor = next((v for v in VENDORS if v.lower() in (lead.get("current_provider") or "").lower()), None)
    kind = f"{lead.get('business_type') or ''} {lead.get('business') or ''}"
    industry = next((i for i, rx in INDUSTRIES if re.search(rx, kind, re.I)), None)
    body = {"Name": lead.get("business") or lead.get("name") or "(unknown)", "Type": "Potential Customer",
            "OwnerId": owner_id, "Phone": found.get("phone") or lead.get("phone"), "Website": found.get("website"),
            "BillingStreet": lead.get("street") or found.get("street"), "BillingCity": found.get("city") or lead.get("town"),
            "BillingState": found.get("state") or ("AK" if lead.get("town") else None),
            "BillingPostalCode": lead.get("zip") or found.get("zip"), "ShippingStreet": lead.get("street") or found.get("street"),
            "ShippingCity": found.get("city") or lead.get("town"), "ShippingState": found.get("state") or ("AK" if lead.get("town") else None),
            "ShippingPostalCode": lead.get("zip") or found.get("zip"), "Industry": industry,
            "Current_Vendor__c": vendor or ("Other competitor" if lead.get("current_provider") else None),
            "Description": description(lead)}
    return {k: v for k, v in body.items() if v}


def contact_body(lead: dict, account_id: str, owner_id: str, primary: bool = True) -> dict | None:
    parts = (lead.get("name") or "").split()
    if not parts:
        return None
    body = {"FirstName": " ".join(parts[:-1]), "LastName": parts[-1], "Phone": lead.get("phone"),
            "Email": lead.get("email"), "AccountId": account_id, "OwnerId": owner_id, "Primary_Contact__c": primary}
    return {k: v for k, v in body.items() if v}


def opportunity_body(lead: dict, account_id: str, owner_id: str, contact_id: str | None = None,
                     today: date | None = None) -> dict:
    today, found = today or date.today(), lead.get("found") or {}
    body = {"Name": lead.get("business") or lead.get("name") or "Website lead", "AccountId": account_id,
            "StageName": STAGE, "CloseDate": (today + timedelta(days=30)).isoformat(), "Type": "New Business",
            "LeadSource": "Website", "OwnerId": owner_id, "Description": description(lead),
            "opportunityContact__c": contact_id, "Address_1__c": lead.get("street") or found.get("street"),
            "City__c": found.get("city") or lead.get("town"), "State__c": found.get("state") or ("AK" if lead.get("town") else None),
            "Zip__c": lead.get("zip") or found.get("zip"), "Phone__c": found.get("phone") or lead.get("phone")}
    return {k: v for k, v in body.items() if v}


def _digits(phone) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def _street(street) -> str:
    return re.sub(r"[^a-z0-9]", "", (street or "").lower())


def pick_account(candidates: list[dict], phones: list[str], street: str) -> tuple[str | None, str]:
    """Which same-name Account to reuse: one whose phone or street matches the lead's; else the only one with that
    name. Several same-name Accounts and none matching: None (make a new Account) and a note for the Slack card."""
    phones = {_digits(p) for p in phones if len(_digits(p)) == 10}
    street = _street(street)
    strong = [c for c in candidates if _digits(c.get("Phone")) in phones
              or (street and street in (_street(c.get("BillingStreet")), _street(c.get("ShippingStreet"))))]
    if strong:
        return strong[0]["Id"], (f"{len(strong)} accounts match this name and phone/address; used the first" if len(strong) > 1 else "")
    if len(candidates) == 1:
        return candidates[0]["Id"], ""
    if candidates:
        return None, (f"{len(candidates)} accounts are named {candidates[0].get('Name')} and none has this phone or "
                      "address, so this is a new account: merge it if it's the same business")
    return None, ""


def duplicate_ids(errors: list) -> list[str]:
    """The existing records Salesforce's duplicate rule matched, from a DUPLICATES_DETECTED error."""
    return [m["record"]["Id"] for e in errors if e.get("errorCode") == "DUPLICATES_DETECTED"
            for r in (e.get("duplicateResult") or {}).get("matchResults") or []
            for m in r.get("matchRecords") or [] if (m.get("record") or {}).get("Id")]


def create_opportunity(lead: dict, owner_id: str) -> tuple[str, str, str]:
    """Account (an existing one with that name, preferring a phone or street match; or the one Salesforce's duplicate
    rule points to), Contact (reused when one with that email or name is on it; primary on a new Account) and
    Opportunity with that Contact. Returns (opportunity id, link, a note for the Slack card or "")."""
    token, instance = _token()
    base = f"{instance}/services/data/{API}"
    new = account_body(lead, owner_id)
    found = lead.get("found") or {}
    same_name = _call("GET", f"{base}/query?" + urllib.parse.urlencode(
        {"q": f"SELECT Id, Name, Phone, BillingStreet, ShippingStreet FROM Account WHERE Name = {_q(new['Name'])} "
              "ORDER BY CreatedDate LIMIT 20"}), token).get("records") or []
    account_id, note = pick_account(same_name, [found.get("phone"), lead.get("phone")], lead.get("street") or found.get("street"))
    created = False
    if not account_id:
        try:
            account_id, created = _call("POST", f"{base}/sobjects/Account", token, new)["id"], True
        except SalesforceError as e:
            dupes = duplicate_ids(e.errors)
            if not dupes:
                raise
            account_id, note = dupes[0], "Salesforce's duplicate rule matched an existing account; used it"
    contact, contact_id = contact_body(lead, account_id, owner_id, primary=created), None
    if contact:
        match = " OR ".join(f"{k} = {_q(contact[k])}" for k in ("Email",) if contact.get(k)) or \
            f"(FirstName = {_q(contact.get('FirstName', ''))} AND LastName = {_q(contact['LastName'])})"
        same = _call("GET", f"{base}/query?" + urllib.parse.urlencode(
            {"q": f"SELECT Id FROM Contact WHERE AccountId = {_q(account_id)} AND ({match}) LIMIT 1"}), token)
        contact_id = (same.get("records") or [{}])[0].get("Id") or _call("POST", f"{base}/sobjects/Contact", token, contact)["id"]
    opp = _call("POST", f"{base}/sobjects/Opportunity", token, opportunity_body(lead, account_id, owner_id, contact_id))
    return opp["id"], f"{instance}/lightning/r/Opportunity/{opp['id']}/view", note

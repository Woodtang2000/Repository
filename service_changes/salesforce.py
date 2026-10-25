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


def _call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


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
    desc = [f"[{COMPANY.get(lead.get('company'), '')}] Website chat lead"]
    if lead.get("handed_from"):
        desc.append(f"Came in on the {COMPANY.get(lead['handed_from'], '')} site and was handed over")
    products = [p for p, rx in PRODUCTS if re.search(rx, text, re.I)]
    if products:
        desc.append("Products: " + ", ".join(products))
    for label, key in (("Contact", "name"), ("Type", "business_type"), ("Size", "size"), ("Needs", "needs"),
                       ("Uses now", "current_provider"), ("Best time", "best_time"), ("Notes", "notes")):
        if lead.get(key):
            desc.append(f"{label}: {lead[key]}")
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
            "BillingStreet": found.get("street"), "BillingCity": found.get("city") or lead.get("town"),
            "BillingState": found.get("state") or ("AK" if lead.get("town") else None),
            "BillingPostalCode": found.get("zip"), "Industry": industry,
            "Current_Vendor__c": vendor or ("Other competitor" if lead.get("current_provider") else None),
            "Description": description(lead)}
    return {k: v for k, v in body.items() if v}


def contact_body(lead: dict, account_id: str, owner_id: str) -> dict | None:
    parts = (lead.get("name") or "").split()
    if not parts:
        return None
    body = {"FirstName": " ".join(parts[:-1]), "LastName": parts[-1], "Phone": lead.get("phone"),
            "Email": lead.get("email"), "AccountId": account_id, "OwnerId": owner_id, "LeadSource": "Website"}
    return {k: v for k, v in body.items() if v}


def opportunity_body(lead: dict, account_id: str, owner_id: str, today: date | None = None) -> dict:
    today = today or date.today()
    return {"Name": lead.get("business") or lead.get("name") or "Website lead", "AccountId": account_id,
            "StageName": STAGE, "CloseDate": (today + timedelta(days=30)).isoformat(), "Type": "New Business",
            "LeadSource": "Website", "OwnerId": owner_id, "Description": description(lead)}


def create_opportunity(lead: dict, owner_id: str) -> tuple[str, str]:
    """Account (reused by exact name), Contact (unless one with that email or name is on it) and Opportunity.
    Returns (opportunity id, link)."""
    token, instance = _token()
    base = f"{instance}/services/data/{API}"
    name = account_body(lead, owner_id)["Name"]
    hit = _call("GET", f"{base}/query?" + urllib.parse.urlencode(
        {"q": f"SELECT Id FROM Account WHERE Name = {_q(name)} LIMIT 1"}), token).get("records") or []
    account_id = hit[0]["Id"] if hit else _call("POST", f"{base}/sobjects/Account", token, account_body(lead, owner_id))["id"]
    contact = contact_body(lead, account_id, owner_id)
    if contact:
        match = " OR ".join(f"{k} = {_q(contact[k])}" for k in ("Email",) if contact.get(k)) or \
            f"(FirstName = {_q(contact.get('FirstName', ''))} AND LastName = {_q(contact['LastName'])})"
        same = _call("GET", f"{base}/query?" + urllib.parse.urlencode(
            {"q": f"SELECT Id FROM Contact WHERE AccountId = {_q(account_id)} AND ({match}) LIMIT 1"}), token)
        if not same.get("records"):
            _call("POST", f"{base}/sobjects/Contact", token, contact)
    opp = _call("POST", f"{base}/sobjects/Opportunity", token, opportunity_body(lead, account_id, owner_id))
    return opp["id"], f"{instance}/lightning/r/Opportunity/{opp['id']}/view"

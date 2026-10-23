"""Salesforce Leads for website chat leads, standard library only.

Uses the OAuth 2.0 client credentials flow: a connected app in Salesforce with "Enable Client Credentials Flow" and a
run-as user that may create Leads. Needs SF_DOMAIN (e.g. snowwhite.my.salesforce.com), SF_CLIENT_ID, SF_CLIENT_SECRET.
"""
import json
import os
import re
import urllib.parse
import urllib.request

API = "v62.0"
# Who Bill can give a lead to: key -> (name, Salesforce user id)
OWNERS = {"bill": ("Bill Dubinsky", "0051W000004Fa1pQAC"), "jesse": ("Jesse James Tingley", "0051W000005qDzpQAE"),
          "scott": ("Scott Woodland", "005400000016E1ZAAU")}
VENDORS = ["Alsco", "Alaska Cleaners", "Co-Op", "Mayfields", "Fireweed Cleaners"]  # the Current Vendor picklist
PRODUCTS = [("Uniforms", r"uniform|shirt|pant|coverall|coat|scrub|jacket"), ("Linens", r"linen|sheet|napkin|table|gown|blanket|pillow"),
            ("Towels", r"towel|bar mop|rag|microfiber"), ("Aprons", r"apron"), ("Mats", r"\bmats?\b"), ("Paper", r"paper|toilet|tissue")]
COMPANY = {"sw": "Snow White Linen Supply", "tlg": "The Laundry Group"}


def configured() -> bool:
    return all(os.environ.get(v) for v in ("SF_DOMAIN", "SF_CLIENT_ID", "SF_CLIENT_SECRET"))


def _post(url: str, data: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def _token() -> tuple[str, str]:
    t = _post(f"https://{os.environ['SF_DOMAIN']}/services/oauth2/token",
              urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": os.environ["SF_CLIENT_ID"],
                                      "client_secret": os.environ["SF_CLIENT_SECRET"]}).encode(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    return t["access_token"], t.get("instance_url") or f"https://{os.environ['SF_DOMAIN']}"


def lead_body(lead: dict, owner_id: str) -> dict:
    """A website chat lead as Salesforce Lead fields."""
    parts = (lead.get("name") or "").split()
    last = parts.pop() if len(parts) > 1 else (parts[0] if parts else "(unknown)")
    text = " ".join(str(lead.get(k) or "") for k in ("needs", "notes", "transcript"))
    vendor = next((v for v in VENDORS if v.lower() in (lead.get("current_provider") or "").lower()), None)
    products = [p for p, rx in PRODUCTS if re.search(rx, text, re.I)]
    desc = [f"[{COMPANY.get(lead.get('company'), '')}] Website chat lead"]
    if lead.get("handed_from"):
        desc.append(f"Came in on the {COMPANY.get(lead['handed_from'], '')} site and was handed over")
    for label, key in (("Type", "business_type"), ("Size", "size"), ("Needs", "needs"), ("Uses now", "current_provider"),
                       ("Best time", "best_time"), ("Notes", "notes")):
        if lead.get(key):
            desc.append(f"{label}: {lead[key]}")
    desc += ["", "Chat:", lead.get("transcript") or ""]
    body = {"FirstName": " ".join(parts), "LastName": last, "Company": lead.get("business") or "(unknown)",
            "City": lead.get("town"), "Phone": lead.get("phone"), "Email": lead.get("email"), "LeadSource": "Website",
            "Status": "Open", "OwnerId": owner_id, "Description": "\n".join(desc)[:31000]}
    if vendor:
        body["Current_Vendor__c"] = vendor
    if products:
        body["Products__c"] = ";".join(products)
    return {k: v for k, v in body.items() if v}


def create_lead(lead: dict, owner_id: str) -> tuple[str, str]:
    """Create the Lead; returns (id, link)."""
    token, instance = _token()
    r = _post(f"{instance}/services/data/{API}/sobjects/Lead", json.dumps(lead_body(lead, owner_id)).encode(),
              {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return r["id"], f"{instance}/lightning/r/Lead/{r['id']}/view"

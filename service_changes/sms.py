"""Customers text in changes for their own account(s); the office works the ticket in Slack as usual.

  python -m service_changes.sms --data service_changes/data --desk service-desk-test

Each phone number is pinned to its account(s) in `customer_phones.csv` in the data folder
(phone,accounts,name: "+19075551234,1205-1-00004;1205-1-00001,Jane at Midas"). The office keeps that list.

Isolation is enforced here, not left to the prompt: for a text from a number, Claude is only ever given that
number's accounts (a cut-down copy of the Alliant data), answers come only from those accounts' facts, and a ticket
for any other account is refused. So "show me Costco's mats" has nothing to show, whatever the message says.
Answers leave out prices, card notes, contacts and route details.

Texts are fetched by polling Twilio (no open port on the server). Unclear requests get up to three questions by
text, at most twice; then a ticket goes to the office channel marked as a customer text. When the office reacts ✅,
desk.py texts the readback back (see send_readback). Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER.
"""
import argparse
import base64
import csv
import dataclasses
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import demo
from .checks import check, readback, ticket
from .todo import GREEN, attachment, shrink, stamp
from .verify import expectations
from .context import ALASKA, Alliant, frequency_label
from .schema import Category

KIND = "service_desk"
ACTIONABLE = {Category.item_change, Category.wearer_change, Category.hold_or_closure, Category.special_order}
MAX_ASKS = 2
MAX_QUESTIONS = 3
CONVO_MINUTES = 60  # a text within this long of our last question is read as the answer
MAX_TEXTS_PER_DAY = 30  # per number; past this the office is told and the bot stops answering that number today
# The first reply to a number we don't know names neither company: the same line serves Snow White and The Laundry
# Group customers, and it reads more like a person (Scott, 2026-10-27). Replies after set-up sign with the customer's
# own company (context.company_for). HELP and STOP are answered by Twilio (Advanced Opt-Out), whose HELP reply names both.
COMPLIANCE = " Msg & data rates may apply. Reply STOP to opt out, HELP for help."
ASKS = re.compile(r"\?|\b(can|could|would|will|need|needs|please|add|more|less|stop|change|want|wants)\b", re.I)


def who_are_you(body: str = "") -> str:
    """The first reply to a number we don't know, in Donna's voice: "Absolutely!" when they asked for something."""
    opener = "Absolutely! " if ASKS.search(body or "") else "Hi there! "
    return (opener + "This is Donna. Who do I have the pleasure of working with today? Can I get your name and the "
            "company you're with?" + COMPLIANCE)


WHO_ARE_YOU = who_are_you()  # the greeting without a request

# Donna's voice in every customer text and email (Scott: "I want people to really like Donna"; she's his Donna, the
# confident, one-step-ahead assistant who makes things feel handled): warm, short, plain, a little wit.
# She never claims to be a person; if asked, she's the company's automated assistant and the office follows up.
WAITING = "Thank you! I'm getting your account confirmed with our office, and then I'll take care of your request."
SET_UP = "You're all set! Text me here any time you need anything for {names}."
GOT_IT = "Consider it done! I've passed it to our office, and I'll {how} you the moment it's entered."
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
PHONE = r"(\+?1?[\s.-]*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4})"
# "add 907-555-1234 to Midas Fairbanks", "add (907) 555-1234 for 1205-1-00004", "remove 907 555 1234"
ADD_CMD = re.compile(rf"^\s*(?:add|approve)\s+{PHONE}\s+(?:to|for)\s+(.+?)\s*$", re.I | re.S)
REMOVE_CMD = re.compile(rf"^\s*(?:remove|delete)\s+{PHONE}\s*$", re.I)


UNBLOCK_CMD = re.compile(r"^\s*unblock\s+(\S+(?:\s*\S+){0,3})\s*$", re.I)
BLOCKED_FILE = "blocked_contacts.csv"  # numbers and emails the office blocked (an imposter, a competitor fishing)


def is_phone_command(text: str) -> bool:
    return bool(ADD_CMD.match(text or "") or REMOVE_CMD.match(text or "") or UNBLOCK_CMD.match(text or ""))


def contact_key(addr: str) -> str:
    """A phone number's 10 digits, or an email lowercased: the same person however it's written."""
    addr = (addr or "").strip()
    return addr.lower() if "@" in addr else phone_key(addr)


def blocked(data_dir: str) -> set[str]:
    path = os.path.join(data_dir, BLOCKED_FILE)
    if not os.path.exists(path):
        return set()
    with open(path, newline="") as f:
        return {contact_key(r.get("contact", "")) for r in csv.DictReader(f) if r.get("contact")}


def block(data_dir: str, addr: str, by: str, why: str = "") -> None:
    """Never answer this number or email again (texts, email, calls), and take it off every account."""
    path = os.path.join(data_dir, BLOCKED_FILE)
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f)]
    if contact_key(addr) not in {contact_key(r.get("contact", "")) for r in rows}:
        rows.append({"contact": contact_key(addr), "by": by, "at": time.strftime("%Y-%m-%d %H:%M"), "why": why[:200]})
    with open(path + ".tmp", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["contact", "by", "at", "why"])
        w.writeheader()
        w.writerows(rows)
    os.replace(path + ".tmp", path)
    if "@" in addr:
        from .email_desk import remove_email_everywhere
        remove_email_everywhere(data_dir, addr)
    else:
        remove_phone(os.path.join(data_dir, "customer_phones.csv"), addr)


ASKS_FILE = "contact_requests.json"  # every sign-up request per number or email: who they said they were, when


def other_accounts(data_dir: str, addr: str, accounts: list[str], current: list[str]) -> list[str]:
    """Remember this request; return the accounts this number or email already asked for (last 90 days) or is on,
    other than the ones it's asking for now. Non-empty: someone may be fishing across customers."""
    path = os.path.join(data_dir, ASKS_FILE)
    try:
        asks = json.load(open(path))
    except (OSError, ValueError):
        asks = {}
    key, now = contact_key(addr), time.time()
    mine = [a for a in asks.get(key, []) if now - a["at"] < 90 * 86400]
    before = [x for a in mine for x in a["accounts"]] + list(current)
    if accounts:
        mine.append({"at": now, "accounts": accounts[:1]})
    asks[key] = mine[-20:]
    with open(path + ".tmp", "w") as f:
        json.dump(asks, f)
    os.replace(path + ".tmp", path)
    base = lambda a: a.split("-")[0]  # the same customer's other locations (1205-1-00004 and 1205-1-00008) are fine
    return list(dict.fromkeys(x for x in before if accounts and base(x) not in {base(a) for a in accounts[:1]}))


def unblock(data_dir: str, addr: str) -> bool:
    path = os.path.join(data_dir, BLOCKED_FILE)
    if not os.path.exists(path):
        return False
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f)]
    keep = [r for r in rows if contact_key(r.get("contact", "")) != contact_key(addr)]
    with open(path + ".tmp", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["contact", "by", "at", "why"])
        w.writeheader()
        w.writerows(keep)
    os.replace(path + ".tmp", path)
    return len(keep) < len(rows)


# ---- Twilio (standard library only) ---------------------------------------------------------------------------

def _twilio(method: str, path: str, params: dict | None = None) -> dict:
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/{path}"
    data = None
    if method == "GET" and params:
        url += "?" + urllib.parse.urlencode(params)
    elif params:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


LOOKUPS_FILE = "caller_lookups.json"  # caller ID names already looked up (each about a penny), kept 30 days
RIVALS = re.compile(r"\b(linen|uniform|laundry|cintas|aramark|vestis|unifirst|alsco|g ?& ?k|textile)\b", re.I)


def caller_id(data_dir: str, phone: str) -> dict:
    """What the phone network says about a number we don't know (Twilio Lookup): {"name", "kind", "carrier"}; {} when
    it isn't set up or doesn't answer. A hint for the office only: caller ID can be faked, so it never grants access."""
    key = phone_key(phone)
    if len(key) != 10 or not os.environ.get("TWILIO_ACCOUNT_SID"):
        return {}
    path = os.path.join(data_dir, LOOKUPS_FILE)
    try:
        seen = json.load(open(path))
    except (OSError, ValueError):
        seen = {}
    if key in seen and time.time() - seen[key].get("at", 0) < 30 * 86400:
        return seen[key]
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    req = urllib.request.Request(f"https://lookups.twilio.com/v2/PhoneNumbers/%2B1{key}?Fields=caller_name,line_type_intelligence",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
    except Exception as e:
        print(time.strftime("%H:%M"), "caller ID lookup failed:", str(e)[:150], flush=True)
        return {}
    cn, lt = d.get("caller_name") or {}, d.get("line_type_intelligence") or {}
    out = {"name": (cn.get("caller_name") or "").strip(), "who": (cn.get("caller_type") or "").lower(),
           "kind": lt.get("type") or "", "carrier": lt.get("carrier_name") or "", "at": time.time()}
    seen[key] = out
    with open(path + ".tmp", "w") as f:
        json.dump(seen, f)
    os.replace(path + ".tmp", path)
    return out


def caller_id_lines(cid: dict, match_name: str = "") -> list[str]:
    """The card's caller ID line, and a warning when it looks wrong: a throwaway internet number, another linen or
    uniform company, or a business name that has nothing to do with the customer they say they are."""
    if not cid:
        return []
    kinds = {"mobile": "cell phone", "landline": "landline", "fixedVoip": "internet phone (business)",
             "nonFixedVoip": "internet phone (app or throwaway)", "tollFree": "toll-free", "voip": "internet phone"}
    out = ["Caller ID: " + ", ".join(x for x in (cid.get("name") or "no name listed", kinds.get(cid.get("kind"), cid.get("kind")),
                                                 cid.get("carrier")) if x)]
    name = cid.get("name") or ""
    words = {w for w in re.findall(r"[a-z]{3,}", name.lower())} - {"inc", "llc", "the", "and", "company", "wireless"}
    if RIVALS.search(name) and not re.search(r"snow ?white|laundry group", name, re.I):
        out.append(f"⚠️ The caller ID name ({name}) looks like another linen or uniform company.")
    elif cid.get("kind") == "nonFixedVoip":
        out.append("⚠️ An app or throwaway internet number: be extra sure it's really them.")
    elif cid.get("who") == "business" and match_name and words and not words & set(re.findall(r"[a-z]{3,}", match_name.lower())):
        out.append(f"⚠️ The caller ID name ({name}) doesn't match {match_name.title()}.")
    return out


def plain(text: str) -> str:
    """Slack markup to plain text for a phone."""
    text = re.sub(r"<@\w+>\s*", "", text)
    text = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", text)
    return text.replace("*", "").replace("_", "").replace("`", "").strip()


def our_numbers() -> list[str]:
    """TWILIO_NUMBER, or several comma-separated (866-TAP-TEXT and 888-519-7604): the first is the default sender."""
    return [n.strip() for n in os.environ.get("TWILIO_NUMBER", "").split(",") if n.strip()]


LAST_NUMBER = os.path.join(os.environ.get("SERVICE_DESK_DATA", "service_changes/data"), "sms_last_number.json")


def _last_numbers() -> dict:
    try:
        with open(LAST_NUMBER) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def send_sms(to: str, body: str) -> None:
    """Reply from the number the customer last texted (the readback is sent by the desk, a separate process, so
    that is kept in a small file), else from the first number."""
    mine = our_numbers()
    last = phone_key(_last_numbers().get(phone_key(to), ""))
    frm = next((n for n in mine if phone_key(n) == last), mine[0])  # digits only: "+18668278398" = "(866) 827-8398"
    _twilio("POST", "Messages.json", {"From": "+1" + phone_key(frm), "To": to, "Body": plain(body)[:1500]})


def inbound(since: datetime) -> list[dict]:
    """Texts to any of our numbers since `since`, oldest first."""
    msgs = []
    for number in our_numbers():
        r = _twilio("GET", "Messages.json", {"To": "+1" + phone_key(number), "DateSent>": since.strftime("%Y-%m-%d"),
                                             "PageSize": 100})
        msgs += [m for m in r.get("messages", []) if m.get("direction") == "inbound"]
    msgs.sort(key=lambda m: m.get("date_created") or "")
    if msgs:
        last = _last_numbers()
        last.update({phone_key(m.get("from", "")): m.get("to", "") for m in msgs})
        try:
            with open(LAST_NUMBER + ".tmp", "w") as f:
                json.dump(last, f)
            os.replace(LAST_NUMBER + ".tmp", LAST_NUMBER)
        except OSError:
            pass
    return msgs


# ---- who may see what -----------------------------------------------------------------------------------------

def phone_key(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:]


def load_phones(path: str) -> dict[str, dict]:
    """customer_phones.csv -> {last 10 digits: {"accounts": [...], "name": ...}}."""
    out = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                accts = [a.strip() for a in re.split(r"[;,\s]+", row.get("accounts", "")) if a.strip()]
                if phone_key(row.get("phone", "")) and accts:
                    out[phone_key(row["phone"])] = {"accounts": accts, "name": (row.get("name") or "").strip()}
    return out


def save_phone(path: str, phone: str, accounts: list[str], name: str) -> list[str]:
    """Add (or extend) a number's accounts in customer_phones.csv; returns its accounts now."""
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f)]
    key = phone_key(phone)
    row = next((r for r in rows if phone_key(r.get("phone", "")) == key), None)
    if row is None:
        row = {"phone": "+1" + key, "accounts": "", "name": name}
        rows.append(row)
    have = [a for a in re.split(r"[;,\s]+", row.get("accounts", "")) if a]
    row["accounts"] = ";".join(have + [a for a in accounts if a not in have])
    row["name"] = row.get("name") or name
    _write_phones(path, rows)
    return row["accounts"].split(";")


def remove_phone(path: str, phone: str) -> bool:
    if not os.path.exists(path):
        return False
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f)]
    keep = [r for r in rows if phone_key(r.get("phone", "")) != phone_key(phone)]
    _write_phones(path, keep)
    return len(keep) < len(rows)


def _write_phones(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["phone", "accounts", "name"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def person(slack, user: str) -> str:
    """A Slack user's name for a status line, or "someone"."""
    try:
        u = slack.users_info(user=user)["user"]
        return u.get("real_name") or u.get("name") or "someone"
    except Exception:
        return "someone"


def link_buttons(accounts: list[str], current: list[str], name) -> dict | None:
    """The card's buttons: "Add to <best match>" (and the next two), or, when the number is already on other accounts,
    "Move to <best>" (taken off the others: people change jobs) and "Add <best> too" (owners with several accounts)."""
    els = []
    for i, a in enumerate(accounts[:3]):
        label = (name(a) or a).title()[:40]
        if current and a not in current and i == 0:
            els.append({"type": "button", "action_id": "link_move", "style": "primary", "value": json.dumps({"a": a, "m": 1}),
                        "text": {"type": "plain_text", "text": f"Move to {label}"}})
            els.append({"type": "button", "action_id": "link_add0", "value": json.dumps({"a": a, "m": 0}),
                        "text": {"type": "plain_text", "text": f"Add {label} too"}})
        elif a not in current:
            els.append({"type": "button", "action_id": f"link_add{i + 1}", "value": json.dumps({"a": a, "m": 0}),
                        **({"style": "primary"} if i == 0 else {}),
                        "text": {"type": "plain_text", "text": f"Add to {label}"}})
    els.append({"type": "button", "action_id": "link_block", "style": "danger", "value": json.dumps({"b": 1}),
                "text": {"type": "plain_text", "text": "Block"},
                "confirm": {"title": {"type": "plain_text", "text": "Block this caller?"},
                            "text": {"type": "plain_text", "text": "Donna will never answer this number or email again "
                                     "(texts, email or calls) and it comes off every account. Type \"unblock\" and the "
                                     "number in #service-desk to undo."},
                            "confirm": {"type": "plain_text", "text": "Block"}, "deny": {"type": "plain_text", "text": "Cancel"}}})
    return {"type": "actions", "block_id": "link", "elements": els}


def signup_card(slack, desk_id: str, alliant: Alliant, phone: str, who: str, said: str, accounts: list[str],
                by: str = "", request: str = "", current: list[str] | None = None, kind: str = "sms_signup",
                head: str = "", data_dir: str = "", cid: dict | None = None) -> None:
    """Ask the office to approve a number for an account: a button (or ✅) approves. `request`: what they asked for
    (a call, a text), handled as soon as the number is approved. `current`: accounts the number is already on."""
    name = lambda a: next((c.name for c in alliant.customers if c.account == a), "")
    pretty = f"({phone_key(phone)[:3]}) {phone_key(phone)[3:6]}-{phone_key(phone)[6:]}"
    head = head or (f"📱 *{by}* asks to set up {pretty} for texting" if by else f"📱 *New texter* {pretty}")
    current = [a for a in (current or []) if a]
    lines = [head + (f": {who}" if who else ""), f"_{said[:200]}_"]
    lines += caller_id_lines(cid or {}, name(accounts[0]) if accounts else "")
    if current:
        lines.append("This number is now on: " + ", ".join(f"{a} {name(a)}" for a in current))
    others = other_accounts(data_dir, phone, accounts, current) if data_dir else []
    if others:  # the same number naming different customers: could be someone fishing, so the owner hears about it
        warn = (f"⚠️ This number has also asked about or is on: " + ", ".join(f"{a} {name(a)}" for a in others[:5])
                + ". Make sure it's really them before adding it, or Block it.")
        lines.append(warn)
        owner = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
        try:
            slack.chat_postMessage(channel=owner, unfurl_links=False,
                                   text=f"⚠️ *{pretty}* is asking to be set up for *{accounts[0]} {name(accounts[0])}*, "
                                        f"but has also asked about or is on " + ", ".join(f"{a} {name(a)}" for a in others[:5])
                                        + ". The card is in #service-desk, with a Block button if it's not legit.")
        except Exception:
            pass
    if accounts:
        lines.append(f"Best match: *{accounts[0]}* {name(accounts[0])}")
        if accounts[1:]:
            lines.append("Could also be: " + ", ".join(f"{a} {name(a)}" for a in accounts[1:]))
        lines.append(f"Tap a button, or type `add {phone_key(phone)} to <account number>` for another account."
                     + (" Their request is handled as soon as it's added." if request else ""))
    else:
        lines.append(f"I couldn't tell which customer. Type `add {phone_key(phone)} to <account number>` to approve.")
    buttons = link_buttons(accounts, current, name)
    slack.chat_postMessage(channel=desk_id, text="", unfurl_links=False,
                           attachments=attachment("\n".join(lines), button=False, extra=[buttons] if buttons else None),
                           metadata={"event_type": KIND, "event_payload": {
                               "kind": kind, "phone": "+1" + phone_key(phone), "accounts": accounts[:1], "name": who,
                               "request": request[:1500]}})


def only(alliant: Alliant, accounts: list[str]) -> Alliant:
    """A copy of the Alliant data holding nothing but these accounts."""
    keep = set(accounts)
    return dataclasses.replace(
        alliant,
        customers=[c for c in alliant.customers if c.account in keep],
        items={a: v for a, v in alliant.items.items() if a in keep},
        frequency={a: v for a, v in alliant.frequency.items() if a in keep},
        autocount={a: v for a, v in alliant.autocount.items() if a in keep},
        wearers={a: v for a, v in alliant.wearers.items() if a in keep},
        garments={k: v for k, v in alliant.garments.items() if k[0] in keep},
        aliases={a: v for a, v in alliant.aliases.items() if a in keep},
        sku={k: v for k, v in alliant.sku.items() if k[0] in keep},
        cards={a: v for a, v in alliant.cards.items() if a in keep},
        holds={a: v for a, v in alliant.holds.items() if a in keep},
        ar={},  # balances never travel with a customer's data
        sku_counts=sku_counts(alliant),  # how many accounts use each SKU (no account in it): the ticket's "usual" SKU
        # the feed's garment, order and route data: their own accounts only; the SKU catalog (skus) stays, no prices
        in_service={k: v for k, v in alliant.in_service.items() if k[0] in keep},
        by_barcode={b: r for b, r in alliant.by_barcode.items() if r.get("cust_code") in keep},
        scans={b: v for b, v in alliant.scans.items() if (alliant.by_barcode.get(b) or {}).get("cust_code") in keep},
        orders={a: v for a, v in alliant.orders.items() if a in keep},
        price_type={a: v for a, v in alliant.price_type.items() if a in keep},
        retired={},
        stops=[r for r in alliant.stops if r.get("account") in keep],
    )


def sku_counts(alliant: Alliant) -> dict[tuple, int]:
    if alliant.sku_counts:
        return alliant.sku_counts
    out: dict[tuple, int] = {}
    for key, sku in alliant.sku.items():
        if len(key) == 2 and sku:
            out[(sku, key[1])] = out.get((sku, key[1]), 0) + 1
    return out


def customer_facts(alliant: Alliant, account: str) -> str:
    """What a customer may be told about their own account: delivery days, items per delivery, wearers.
    No prices, inventory, card notes, contacts or route details."""
    cust = next((c for c in alliant.customers if c.account == account), None)
    out = [f"{account} {cust.name if cust else ''}".strip()]
    if cust and cust.service_days:
        out.append("Delivery days: " + ", ".join(cust.service_days))
    items = alliant.items.get(account, {})
    if items:
        out.append("Items (per delivery; how often):")
        for item, inv in items.items():
            auto = alliant.autocount.get(account, {}).get(item)
            freq = frequency_label(alliant.frequency.get(account, {}).get(item))
            out.append(f"- {item}: {auto if auto is not None else inv} per delivery" + (f"; {freq}" if freq else ""))
    wearers = alliant.wearers.get(account, [])
    if wearers:
        out.append("Wearers (name: garments size x qty):")
        for w in wearers:
            g = alliant.garments.get((account, w.employee), {})
            out.append(f"- {w.name}: " + (", ".join(f"{k} x{v}" for k, v in g.items()) or "no garments"))
    return "\n".join(out)


# ---- the loop -------------------------------------------------------------------------------------------------

class TextDesk:
    """Customer texts. email_desk.EmailDesk reuses all of this for customer emails, through the hooks below."""
    word, icon, to_key, signup_kind, list_file = "text", "📱", "sms_to", "sms_signup", "customer_phones.csv"
    add_cmd, remove_cmd, set_up = ADD_CMD, REMOVE_CMD, SET_UP

    def key(self, addr: str) -> str:
        return phone_key(addr)

    def addr(self, key: str) -> str:
        return "+1" + key

    def masked(self, addr: str) -> str:
        return f"(…{phone_key(addr)[-4:]})"

    def contacts(self) -> dict[str, dict]:
        return load_phones(os.path.join(self.data_dir, self.list_file))

    def save_contact(self, addr: str, accounts: list[str], name: str) -> list[str]:
        return save_phone(os.path.join(self.data_dir, self.list_file), addr, accounts, name)

    def remove_contact(self, addr: str) -> bool:
        return remove_phone(os.path.join(self.data_dir, self.list_file), addr)

    def card(self, addr: str, who: str, said: str, accounts: list[str], by: str = "") -> None:
        current = self.contacts().get(self.key(addr), {}).get("accounts", [])
        signup_card(self.slack, self.desk_id, self.alliant, addr, who, said, accounts, by=by, current=current,
                    data_dir=self.data_dir, cid=caller_id(self.data_dir, addr) if not by else None)

    def inbound(self, since: datetime) -> list[dict]:
        """New messages as {"id", "from", "body"}, oldest first."""
        return [{"id": m["sid"], "from": m.get("from", ""), "body": (m.get("body") or "").strip()} for m in inbound(since)]

    def ticket_extra(self, addr: str) -> dict:
        return {}

    def ticket_notes(self, addr: str) -> list[str]:
        return []

    def __init__(self, slack, claude, alliant: Alliant, desk: str, data_dir: str, send=send_sms):
        self.slack, self.claude, self.alliant, self.data_dir, self.send = slack, claude, alliant, data_dir, send
        self.desk_id = next(c["id"] for p in slack.conversations_list(types="public_channel,private_channel",
                                                                      exclude_archived=True, limit=1000)
                            for c in p["channels"] if c["name"] == desk)
        self.state_path = os.path.join(data_dir, "sms_state.json")
        try:
            with open(self.state_path) as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("seen", [])
        self.state.setdefault("convos", {})
        self.state.setdefault("counts", {})

    def take_asks(self) -> None:
        """Questions Donna asked from her Alliant workstation (desk.ask_requester): the answer is read together with
        the request, like the answer to one of our own questions."""
        path = os.path.join(self.data_dir, f"{self.word}_asks.json")
        try:
            waiting = json.load(open(path))
            os.remove(path)
        except (OSError, ValueError):
            return
        for w in waiting:
            self.state["convos"][self.key(w["to"])] = {"lines": w["lines"], "asks": 1, "at": w["at"]}
        self.save()

    def log(self, addr: str, what: str) -> None:
        print(time.strftime("%H:%M"), f"{self.word} from {self.masked(addr)}: {what}", flush=True)

    def save(self) -> None:
        self.state["seen"] = self.state["seen"][-2000:]
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def office(self, text: str, payload: dict, todo: str | None = None) -> None:
        """A message in the office channel. todo: "own" (a ✅ ticket) or "button" puts it in a gold to-do bar (todo.py)."""
        bar = {"text": "", "attachments": attachment(text, button=todo == "button")} if todo else {"text": text}
        self.slack.chat_postMessage(channel=self.desk_id, unfurl_links=False, **bar,
                                    metadata={"event_type": KIND, "event_payload": payload})

    def run_once(self, first: bool = False) -> int:
        """Handle new texts. On the very first run, existing texts are only marked as seen, never answered."""
        since = datetime.now(timezone.utc) - timedelta(days=1)
        new = [m for m in self.inbound(since) if m["id"] not in self.state["seen"]]
        handled = self.office_pass() if not first else 0
        self.demo_pass()
        self.take_asks()
        for m in new:
            self.state["seen"].append(m["id"])
            if not first:
                self.handle(m["from"], m["body"], m["id"])
                handled += 1
            self.save()
        return handled

    def handle(self, phone: str, body: str, sid: str) -> None:
        key = self.key(phone)
        if contact_key(phone) in blocked(self.data_dir):  # the office blocked them: no answer, nothing about any account
            self.log(phone, "blocked: ignored")
            return
        if self.word == "text":  # Scott's or Bill's own phone: any account, after their code phrase
            from .phone_agent import trusted
            person = trusted(phone)
            if person:
                return self.owner_text(phone, body, person)
        cmd = demo.COMMAND.match(body)
        if cmd and demo.allowed(key, self.key):
            return self.demo_command(phone, cmd.group(1))
        if key in self.state.get("demo", {}):
            self.log(phone, "in demo mode")
            return self.demo_text(phone, body)
        who = self.contacts().get(self.key(phone))
        today = datetime.now(ALASKA).strftime("%Y-%m-%d")
        count_key = f"{today} {self.key(phone)}"
        n = self.state["counts"][count_key] = self.state["counts"].get(count_key, 0) + 1
        self.state["counts"] = {k: v for k, v in self.state["counts"].items() if k.startswith(today)}
        masked = self.masked(phone)
        if not who:
            p = self.state.get("pending", {}).get(self.key(phone))
            self.log(phone, "unknown number: " + ("asked who they are" if p is None else
                     f"sign-up {p.get('stage')}, held for the office to approve"))
            return self.sign_up(phone, body, sid, n)
        if n > MAX_TEXTS_PER_DAY:
            self.log(phone, f"over {MAX_TEXTS_PER_DAY} today, not answered")
            if n == MAX_TEXTS_PER_DAY + 1:
                self.office(f"⚠️ {who['name'] or 'A customer'} {masked} has sent {MAX_TEXTS_PER_DAY} {self.word}s today; "
                            "I've stopped answering them until tomorrow.", {"kind": "sms_limit"})
            return

        accounts = who["accounts"]
        mine = only(self.alliant, accounts)
        convo = self.state["convos"].get(self.key(phone))
        if convo and time.time() - convo["at"] > CONVO_MINUTES * 60:
            convo = None
        lines = (convo["lines"] if convo else []) + [f"Customer: {body}"]
        text = body if len(lines) == 1 else "\n".join(lines)

        from .parser import answer_lookup, parse_message
        from .run import fill_item_matches
        parsed = parse_message(self.claude, text, f"customer-{self.word}", f"{time.time():.6f}", mine,
                               author=f"{who['name'] or 'Customer'} (the customer, by {self.word})")
        # Isolation: only this number's accounts, whatever the message or the model says.
        if parsed.account_number not in accounts:
            parsed.account_number = accounts[0] if len(accounts) == 1 else None
        for c in parsed.changes:  # a text is one request for one of this number's accounts; never a per-change one
            c.account_number = None
        acct = parsed.account_number

        self.log(phone, f"{parsed.category.value} for {acct or 'no account yet'}")
        if parsed.category == Category.not_a_request:
            self.state["convos"].pop(self.key(phone), None)
            return
        if acct is None:
            names = [f"{c.name}" for c in mine.customers] or accounts
            q = "Which location is this for? " + " / ".join(names)
            return self.ask(phone, lines, [q], convo)
        if parsed.category == Category.lookup:
            from .checks import OPTIONS_Q, options_facts
            facts = customer_facts(mine, acct)
            if OPTIONS_Q.search(body):  # "what colors do you have in tablecloths?": the catalog, never prices
                facts += "\n" + options_facts(mine, body) + ("\nSuggest a color that goes with what they already have, "
                                                             "and ask what they'd like; they may just be asking.")
            ans = answer_lookup(self.claude, parsed.summary or body, facts, asker="customer")
            self.send(phone, ans.answer)
            self.state["convos"].pop(self.key(phone), None)
            if not ans.found:
                self.office(f"{self.icon} *{acct}* {self.name(mine, acct)}: customer question I couldn't answer {masked}\n"
                            f"_{body[:300]}_\nPlease follow up with them.", {"kind": "sms_question", "account": acct},
                            todo="button")
            return
        if parsed.category in ACTIONABLE:
            fill_item_matches(self.claude, parsed, mine)
        result = check(parsed, mine)
        asks = convo["asks"] if convo else 0
        if parsed.category in ACTIONABLE and result.questions and asks < MAX_ASKS:
            return self.ask(phone, lines, result.questions[:MAX_QUESTIONS], convo)

        # Ticket for the office; the customer hears back when it's ✅.
        if parsed.category in ACTIONABLE:
            out = ticket(result, mine, with_readback=False).splitlines()
            rb = readback(result) if not result.questions else ""
            if result.questions:
                out.append("⚠️ _Still unclear after asking. Please call the customer._")
        else:
            out = [f"*{acct}*  {self.name(mine, acct)}", f"📣 *Customer message: {parsed.category.value.replace('_', ' ')}*",
                   parsed.summary]
            rb = f"✅ Thanks, we've got it: {parsed.summary}"
        out += self.ticket_notes(phone)
        quote = "\n".join("> " + ln for ln in lines[-6:])
        self.office("\n".join(out + ["", quote, f"{self.icon} Customer {self.word} from {who['name'] or 'customer'} "
                                                 f"{masked} · React ✅ or type done when it's in Alliant"]),
                    {"kind": "ticket", "src_channel": self.word, "src_ts": sid, "readback": rb, "driver": who["name"],
                     "driver_id": "", "account": acct, "msgs": [], self.to_key: phone, **self.ticket_extra(phone),
                     "expect": expectations(result) if parsed.category in ACTIONABLE and not result.questions else []},
                    todo="own")
        self.send(phone, GOT_IT.format(how="text" if self.word == "text" else "email"))
        self.state["convos"].pop(self.key(phone), None)

    # ---- Scott texting from his own phone -----------------------------------------------------------------------

    def owner_text(self, phone: str, body: str, person: dict) -> None:
        """Scott's own number (OWNER_PHONES) or Bill's (TRUSTED_PHONES) texting: the same Donna as their Donna app
        (desk.voice: Harvey mode for Scott, business only for Bill; any account, their yes before anything changes),
        once they've texted their code phrase on its own; a text's caller ID can be faked. Good for OWNER_TEXT_HOURS
        (12); 3 wrong tries locks it until tomorrow and tells them in Slack. A request sent before the phrase is held
        and answered right after it. The phrase itself is never logged."""
        from .phone_agent import secret, secret_matches
        key, real, now = self.key(phone), secret(person["user"]), time.time()
        st = self.state.setdefault("owner", {}).setdefault(key, {"until": 0, "tries": 0, "day": "", "held": ""})
        today = datetime.now(ALASKA).strftime("%Y-%m-%d")
        if st["day"] != today:
            st.update(day=today, tries=0)
        if not real:
            self.log(phone, "owner number, but no code phrase is set")
            return self.send(phone, "Donna: texting changes isn't set up yet (no code phrase on file).")
        if st["until"] < now:
            if st["tries"] >= 3:
                self.log(phone, "owner number: locked for today")
                return
            if not secret_matches(body, real):
                if st.get("asked"):  # we already asked for the phrase: this was a wrong try
                    st["tries"] += 1
                    self.log(phone, f"owner number: wrong code phrase ({st['tries']} of 3)")
                    if st["tries"] >= 3:
                        st["held"] = ""
                        self.save()
                        self.slack.chat_postMessage(channel=person["user"], text=f"⚠️ Someone texting from your number {self.masked(phone)} got "
                                                                        "the code phrase wrong 3 times. Texting changes is locked until tomorrow.")
                        return
                    self.save()
                    return self.send(phone, "Donna: that's not it. Text just your code phrase.")
                st.update(asked=True, held=body[:1000])  # answered right after the phrase
                self.log(phone, "owner number: asked for the code phrase")
                self.save()
                return self.send(phone, f"Donna: hi {person['name']}! Text me your code phrase first, then I'll take care of that.")
            hours = float(os.environ.get("OWNER_TEXT_HOURS", "12") or 12)
            held = st["held"]
            st.update(until=now + hours * 3600, tries=0, held="", asked=False)
            self.save()
            self.log(phone, "owner number: verified")
            if not held:
                return self.send(phone, f"Donna: got it, {person['name']}. What can I do?")
            body = held
        self.log(phone, "owner text")
        self.send(phone, "Donna: " + self.ask_desk(body, key, person["user"]))

    def ask_desk(self, text: str, key: str, owner: str) -> str:
        """desk.voice on 127.0.0.1 (desk.serve_voice), the way the phone agent's ask_donna does."""
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{os.environ.get('DESK_VOICE_PORT', '8091')}/voice",
                                     data=json.dumps({"text": text, "convo": "sms-" + key, "user": owner}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=110) as r:
                return r.read().decode("utf-8", "replace").strip() or "Got it."
        except Exception as e:
            print(time.strftime("%H:%M"), "owner text: desk not reached:", str(e)[:200], flush=True)
            return "I can't reach the office data right now. Text me again in a minute."

    # ---- demo mode: a made-up business for showing Donna on a sales call (demo.py) ---------------------------

    def demo_command(self, phone: str, word: str) -> None:
        key, shows = self.key(phone), self.state.setdefault("demo", {})
        if word.lower() in ("off", "stop", "end", "done"):
            shows.pop(key, None)
            self.state["convos"].pop(key, None)
            return self.send(phone, "🎬 Demo off. Nice work, Harvey.")
        businesses = demo.load()
        bid = demo.pick(word, businesses)
        if bid is None:
            return self.send(phone, "🎬 Which one? " + ", ".join(f"demo {b}" for b in businesses))
        shows[key] = {"biz": bid, "changed": {}}
        self.state["convos"].pop(key, None)
        self.send(phone, demo.started(businesses[bid]))

    def demo_text(self, phone: str, body: str) -> None:
        """A text during a demo: the real Donna on the made-up business, with nothing sent to the office."""
        key = self.key(phone)
        show = self.state["demo"][key]
        b = demo.load()[show["biz"]]
        try:
            self.demo_answer(phone, body, b, show)
        except Exception as e:  # never fumble in front of a customer: the scripted answer instead
            print(time.strftime("%H:%M"), "demo fallback:", e, flush=True)
            reply = demo.fallback(b, body)
            self.send(phone, reply)
            if reply.startswith("Consider it done"):
                self.state.setdefault("demo_due", []).append(
                    {"to": phone, "at": time.time() + demo.ENTER_SECONDS, "changed": {},
                     "text": f"All done! {demo.ENTERED_BY} entered it for you.\nThanks so much!"})

    def demo_answer(self, phone: str, body: str, b: dict, show: dict) -> None:
        from .desk import customer_readback
        from .parser import answer_lookup, parse_message
        from .run import fill_item_matches
        key, acct, mine = self.key(phone), b["account"], demo.alliant(b, show["changed"])
        convo = self.state["convos"].get(key)
        if convo and time.time() - convo["at"] > CONVO_MINUTES * 60:
            convo = None
        lines = (convo["lines"] if convo else []) + [f"Customer: {body}"]
        text = body if len(lines) == 1 else "\n".join(lines)
        parsed = parse_message(self.claude, text, f"customer-{self.word}", f"{time.time():.6f}", mine,
                               author=f"{b['contact']} (the customer, by {self.word})")
        parsed.account_number = acct
        for c in parsed.changes:
            c.account_number = None
        if parsed.category == Category.not_a_request:
            self.state["convos"].pop(key, None)
            return self.send(phone, "Anytime! I'm right here whenever you need anything.")
        if parsed.category == Category.lookup:
            self.state["convos"].pop(key, None)
            return self.send(phone, answer_lookup(self.claude, parsed.summary or body, customer_facts(mine, acct),
                                                  asker="customer").answer)
        if parsed.category in ACTIONABLE:
            fill_item_matches(self.claude, parsed, mine)
        result = check(parsed, mine)
        if parsed.category in ACTIONABLE and result.questions and (convo["asks"] if convo else 0) < MAX_ASKS:
            return self.ask(phone, lines, result.questions[:MAX_QUESTIONS], convo)
        rb = readback(result) if parsed.category in ACTIONABLE else f"✅ {parsed.summary}"
        changed = {cc.alliant_item: cc.new_total for cc in result.changes
                   if cc.alliant_item and cc.new_total is not None and not cc.change.wearer}
        self.send(phone, GOT_IT.format(how="text" if self.word == "text" else "email"))
        self.state["convos"].pop(key, None)
        self.state.setdefault("demo_due", []).append({"to": phone, "at": time.time() + demo.ENTER_SECONDS,
                                                      "changed": changed,
                                                      "text": customer_readback(rb, demo.ENTERED_BY, [])})

    def demo_pass(self) -> None:
        """Send the "All done!" texts whose time has come, and count the change in for the rest of the demo."""
        due = self.state.get("demo_due") or []
        now = time.time()
        for d in [d for d in due if d["at"] <= now]:
            due.remove(d)
            show = self.state.get("demo", {}).get(self.key(d["to"]))
            if show is None:
                continue  # the demo was turned off
            show["changed"].update(d["changed"])
            self.send(d["to"], d["text"])
        if due or "demo_due" in self.state:
            self.state["demo_due"] = due
            self.save()

    # ---- sign-up: an unknown number tells us who they are, the office approves --------------------------------

    def sign_up(self, phone: str, body: str, sid: str, n: int) -> None:
        key = self.key(phone)
        p = self.state.setdefault("pending", {}).get(key)
        if n > MAX_TEXTS_PER_DAY:
            return
        if p is None:  # first text: hold it and ask who they are
            self.state["pending"][key] = {"phone": phone, "stage": "asked", "held": [[body, sid]], "at": time.time()}
            self.send(phone, who_are_you(body))
            return
        if p["stage"] == "asked":  # their answer: suggest an account to the office
            from .parser import identify_customer
            found = identify_customer(self.claude, body, self.alliant.customers)
            p.update(stage="waiting", intro=body, name=found.person_name or "")
            self.card(phone, found.person_name or "", body, found.accounts)
            self.send(phone, WAITING)
            return
        p["held"].append([body, sid])  # waiting for the office: keep it for after approval

    def office_pass(self) -> int:
        """Office side: ✅ on a sign-up card approves it; `add <phone> to <account or name>` and `remove <phone>`
        typed in the office channel (or a driver's add, forwarded by desk.py as a card) change the phone list."""
        done = set(self.state.setdefault("cmds", []))
        r = self.slack.conversations_history(channel=self.desk_id, oldest=f"{time.time() - 14 * 86400:.6f}", limit=200,
                                             include_all_metadata=True)
        changed = 0
        for m in reversed(r["messages"]):
            pl = ((m.get("metadata") or {}).get("event_payload") or {})
            if m["ts"] in done:
                continue
            if pl.get("kind") == self.signup_kind and pl.get("accounts"):
                who = pl.get("approved_by") or next((u for x in m.get("reactions", []) if x.get("name") in DONE_REACTIONS
                                                     for u in x.get("users", []) if not m.get("user") or u != m.get("user")), None)
                if who:
                    self.approve(pl["phone"], pl["accounts"], pl.get("name", ""), move=bool(pl.get("move")),
                                 request=pl.get("request", ""), sid=m["ts"])
                    did = ("Moved to " if pl.get("move") else "Added to ") + ", ".join(
                        f"{a} {self.name(self.alliant, a)}" for a in pl["accounts"]) + " · approved"
                    self.slack.chat_update(channel=self.desk_id, ts=m["ts"], text="",
                                           attachments=shrink(self.slack, self.desk_id, m, GREEN,
                                                              stamp(person(self.slack, who), did), lines=1),
                                           metadata={"event_type": KIND, "event_payload": {**pl, "kind": "sms_approved"}})
                    done.add(m["ts"])
                    changed += 1
                continue
            if m.get("bot_id") or m.get("subtype"):
                continue
            text = m.get("text", "")
            # Slack links phone numbers: "<tel:9075551234|(907) 555-1234>" reads as what was typed
            text = re.sub(r"<tel:[^|>]*\|([^>]*)>", r"\1", text)
            text = re.sub(r"<tel:([^|>]*)>", r"\1", text)
            unb = UNBLOCK_CMD.match(text)
            if unb and m["ts"] not in done:
                ok = unblock(self.data_dir, unb.group(1))
                self.office(f"{self.icon} {'Unblocked' if ok else 'Nothing blocked for'} {unb.group(1).strip()}.",
                            {"kind": "sms_admin"})
                done.add(m["ts"])
                continue
            add, rem = self.add_cmd.match(text), self.remove_cmd.match(text)
            if re.match(r"^\s*(add|approve|remove|delete)\b", text, re.I) and m["ts"] not in self.__dict__.setdefault("logged", set()):
                self.logged.add(m["ts"])
                print(time.strftime("%H:%M"), f"{self.word} office command: " + (
                    "add" if add else "remove" if rem else f"not understood: {text[:80]!r}"), flush=True)
            if rem:
                ok = self.remove_contact(rem.group(1))
                self.state.get("pending", {}).pop(self.key(rem.group(1)), None)
                self.office(f"{self.icon} {'Removed' if ok else f'No {self.word}s set up for'} {rem.group(1).strip()}.",
                            {"kind": "sms_admin"})
                changed += 1
            elif add:
                ids = {c.account for c in self.alliant.customers}
                known = [t for t in re.split(r"[;,\s]+", add.group(2)) if t in ids]
                if known:
                    self.approve(add.group(1), known, "")
                    self.office(f"{self.icon} {add.group(1).strip()} can now {self.word} us for " +
                                ", ".join(f"{a} {self.name(self.alliant, a)}" for a in known) + ".", {"kind": "sms_admin"})
                else:  # a name, not an account number: suggest, and let a ✅ confirm which
                    from .parser import identify_customer
                    found = identify_customer(self.claude, add.group(2), self.alliant.customers)
                    self.card(add.group(1), "", add.group(2), found.accounts, by="Office")
                changed += 1
            else:
                continue
            done.add(m["ts"])
        self.state["cmds"] = list(done)[-500:]
        return changed

    def approve(self, phone: str, accounts: list[str], name: str, move: bool = False, request: str = "",
                sid: str = "") -> None:
        """Link a number (or email) to accounts. `move`: off its other accounts first (they changed jobs). `request`:
        what they asked for before being set up (a phone call's), handled now like their held texts."""
        key = self.key(phone)
        p = self.state.get("pending", {}).pop(key, None)
        name = name or (p or {}).get("name", "")
        if move:
            self.remove_contact(self.addr(key))
        if request:
            p = {**(p or {}), "held": (p or {}).get("held", []) + [[request, sid or f"req-{time.time():.0f}"]]}
        now = self.save_contact(self.addr(key), accounts, name)
        to = (p or {}).get("phone") or self.addr(key)
        names = " and ".join(self.name(self.alliant, a) for a in now)
        said, send = [self.set_up.format(names=names)], self.send
        self.send = lambda _to, msg: said.append(msg)  # the welcome and the answer to what they sent go out together
        try:
            for body, sid in (p or {}).get("held", [])[-3:]:  # what they sent before being set up (the latest few)
                self.handle(to, body, sid)
        finally:
            self.send = send
        self.send(to, "\n\n".join(dict.fromkeys(said)))  # the same acknowledgement twice reads as once

    def ask(self, phone: str, lines: list[str], questions: list[str], convo: dict | None) -> None:
        msg = "Happy to! Just one quick question: " + questions[0] if len(questions) == 1 else \
            "Happy to! Just a few quick questions:\n" + "\n".join(f"- {q}" for q in questions)
        self.send(phone, msg)
        self.state["convos"][self.key(phone)] = {"lines": lines + [f"Snow White asked: {plain(msg)}"],
                                                  "asks": (convo["asks"] if convo else 0) + 1, "at": time.time()}

    @staticmethod
    def name(alliant: Alliant, acct: str) -> str:
        return next((c.name for c in alliant.customers if c.account == acct), acct)


def main():
    import anthropic
    from slack_sdk import WebClient

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    ap.add_argument("--desk", default="service-desk", help="office channel for tickets")
    ap.add_argument("--every", type=int, default=5, help="seconds between checks for new texts")
    args = ap.parse_args()
    missing = [v for v in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER") if not os.environ.get(v)]
    if missing:
        sys.exit("Needs " + ", ".join(missing) + " (from the Twilio console).")
    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SERVICE_DESK_API_KEY"))
    td = TextDesk(slack, claude, Alliant.from_dir(args.data), args.desk.lstrip("#"), args.data)
    first, loaded = not td.state["seen"], time.time()
    print(time.strftime("%H:%M"), "watching texts", flush=True)
    while True:
        try:
            from .context import feed_stamp
            if time.time() - loaded > 3600 or feed_stamp(args.data) > loaded:  # the hourly feed, without a restart
                td.alliant, loaded = Alliant.from_dir(args.data), time.time()
            n = td.run_once(first)
            first = False
            if n:
                print(time.strftime("%H:%M"), f"{n} text(s)", flush=True)
        except Exception as e:  # keep going through a Twilio, Slack or API hiccup
            print(time.strftime("%H:%M"), "error:", e, flush=True)
        time.sleep(args.every)


if __name__ == "__main__":
    main()

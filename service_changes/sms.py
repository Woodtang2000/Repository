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

from .checks import check, readback, ticket
from .context import ALASKA, Alliant, frequency_label
from .schema import Category

KIND = "service_desk"
ACTIONABLE = {Category.item_change, Category.wearer_change, Category.hold_or_closure, Category.special_order}
MAX_ASKS = 2
MAX_QUESTIONS = 3
CONVO_MINUTES = 60  # a text within this long of our last question is read as the answer
MAX_TEXTS_PER_DAY = 30  # per number; past this the office is told and the bot stops answering that number today
NOT_SET_UP = ("Hi, this is Snow White Linen. This number isn't set up for service requests yet. "
              "Please ask your route driver or call the office.")


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


def plain(text: str) -> str:
    """Slack markup to plain text for a phone."""
    text = re.sub(r"<@\w+>\s*", "", text)
    text = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", text)
    return text.replace("*", "").replace("_", "").replace("`", "").strip()


def send_sms(to: str, body: str) -> None:
    _twilio("POST", "Messages.json", {"From": os.environ["TWILIO_NUMBER"], "To": to, "Body": plain(body)[:1500]})


def inbound(since: datetime) -> list[dict]:
    """Texts to our number since `since`, oldest first."""
    r = _twilio("GET", "Messages.json", {"To": os.environ["TWILIO_NUMBER"], "DateSent>": since.strftime("%Y-%m-%d"),
                                          "PageSize": 100})
    msgs = [m for m in r.get("messages", []) if m.get("direction") == "inbound"]
    return sorted(msgs, key=lambda m: m.get("date_created") or "")


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
    )


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

    def save(self) -> None:
        self.state["seen"] = self.state["seen"][-2000:]
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def office(self, text: str, payload: dict) -> None:
        self.slack.chat_postMessage(channel=self.desk_id, text=text, unfurl_links=False,
                                    metadata={"event_type": KIND, "event_payload": payload})

    def run_once(self, first: bool = False) -> int:
        """Handle new texts. On the very first run, existing texts are only marked as seen, never answered."""
        since = datetime.now(timezone.utc) - timedelta(days=1)
        new = [m for m in inbound(since) if m["sid"] not in self.state["seen"]]
        handled = 0
        for m in new:
            self.state["seen"].append(m["sid"])
            if not first:
                self.handle(m.get("from", ""), (m.get("body") or "").strip(), m["sid"])
                handled += 1
            self.save()
        return handled

    def handle(self, phone: str, body: str, sid: str) -> None:
        phones = load_phones(os.path.join(self.data_dir, "customer_phones.csv"))
        who = phones.get(phone_key(phone))
        today = datetime.now(ALASKA).strftime("%Y-%m-%d")
        count_key = f"{today} {phone_key(phone)}"
        n = self.state["counts"][count_key] = self.state["counts"].get(count_key, 0) + 1
        self.state["counts"] = {k: v for k, v in self.state["counts"].items() if k.startswith(today)}
        masked = f"(…{phone_key(phone)[-4:]})"
        if not who:
            if n == 1:  # once a day per unknown number
                self.send(phone, NOT_SET_UP)
                self.office(f"📱 Text from a number that isn't set up {masked}: _{body[:200]}_\n"
                            "Add it to customer_phones.csv if it's a customer.", {"kind": "sms_unknown"})
            return
        if n > MAX_TEXTS_PER_DAY:
            if n == MAX_TEXTS_PER_DAY + 1:
                self.office(f"⚠️ {who['name'] or 'A customer'} {masked} has texted {MAX_TEXTS_PER_DAY} times today; "
                            "I've stopped answering them until tomorrow.", {"kind": "sms_limit"})
            return

        accounts = who["accounts"]
        mine = only(self.alliant, accounts)
        convo = self.state["convos"].get(phone_key(phone))
        if convo and time.time() - convo["at"] > CONVO_MINUTES * 60:
            convo = None
        lines = (convo["lines"] if convo else []) + [f"Customer: {body}"]
        text = body if len(lines) == 1 else "\n".join(lines)

        from .parser import answer_lookup, parse_message
        from .run import fill_item_matches
        parsed = parse_message(self.claude, text, "customer-text", f"{time.time():.6f}", mine,
                               author=f"{who['name'] or 'Customer'} (the customer, by text)")
        # Isolation: only this number's accounts, whatever the message or the model says.
        if parsed.account_number not in accounts:
            parsed.account_number = accounts[0] if len(accounts) == 1 else None
        acct = parsed.account_number

        if parsed.category == Category.not_a_request:
            self.state["convos"].pop(phone_key(phone), None)
            return
        if acct is None:
            names = [f"{c.name}" for c in mine.customers] or accounts
            q = "Which location is this for? " + " / ".join(names)
            return self.ask(phone, lines, [q], convo)
        if parsed.category == Category.lookup:
            ans = answer_lookup(self.claude, parsed.summary or body, customer_facts(mine, acct), asker="customer")
            self.send(phone, ans.answer)
            self.state["convos"].pop(phone_key(phone), None)
            if not ans.found:
                self.office(f"📱 *{acct}* {self.name(mine, acct)}: customer question I couldn't answer {masked}\n"
                            f"_{body[:300]}_\nPlease follow up with them.", {"kind": "sms_question", "account": acct})
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
        quote = "\n".join("> " + ln for ln in lines[-6:])
        self.office("\n".join(out + ["", quote, f"📱 Customer text from {who['name'] or 'customer'} {masked} · "
                                                 "React ✅ or type done when it's in Alliant"]),
                    {"kind": "ticket", "src_channel": "sms", "src_ts": sid, "readback": rb, "driver": who["name"],
                     "driver_id": "", "account": acct, "msgs": [], "sms_to": phone})
        self.send(phone, "Got it, thanks. We'll text you when it's done.")
        self.state["convos"].pop(phone_key(phone), None)

    def ask(self, phone: str, lines: list[str], questions: list[str], convo: dict | None) -> None:
        msg = "Quick question: " + questions[0] if len(questions) == 1 else \
            "A few quick questions:\n" + "\n".join(f"- {q}" for q in questions)
        self.send(phone, msg)
        self.state["convos"][phone_key(phone)] = {"lines": lines + [f"Snow White asked: {plain(msg)}"],
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
            if time.time() - loaded > 3600:
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

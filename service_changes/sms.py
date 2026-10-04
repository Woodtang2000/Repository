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
WHO_ARE_YOU = ("Hi, this is Snow White Linen. So we can set you up, what's your name and which business are you with? "
               "Msg & data rates may apply. Reply STOP to opt out, HELP for help.")
WAITING = "Thanks! The office will confirm your account shortly, then we'll take care of your request."
SET_UP = "You're all set. Text this number any time with changes for {names}."
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
PHONE = r"(\+?1?[\s.-]*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4})"
# "add 907-555-1234 to Midas Fairbanks", "add (907) 555-1234 for 1205-1-00004", "remove 907 555 1234"
ADD_CMD = re.compile(rf"^\s*(?:add|approve)\s+{PHONE}\s+(?:to|for)\s+(.+?)\s*$", re.I | re.S)
REMOVE_CMD = re.compile(rf"^\s*(?:remove|delete)\s+{PHONE}\s*$", re.I)


def is_phone_command(text: str) -> bool:
    return bool(ADD_CMD.match(text or "") or REMOVE_CMD.match(text or ""))


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


def signup_card(slack, desk_id: str, alliant: Alliant, phone: str, who: str, said: str, accounts: list[str],
                by: str = "") -> None:
    """Ask the office to approve a number for an account: ✅ approves the best match."""
    name = lambda a: next((c.name for c in alliant.customers if c.account == a), "")
    pretty = f"({phone_key(phone)[:3]}) {phone_key(phone)[3:6]}-{phone_key(phone)[6:]}"
    head = (f"📱 *{by}* asks to set up {pretty} for texting" if by else f"📱 *New texter* {pretty}")
    lines = [head + (f": {who}" if who else ""), f"_{said[:200]}_"]
    if accounts:
        lines.append(f"Best match: *{accounts[0]}* {name(accounts[0])}")
        if accounts[1:]:
            lines.append("Could also be: " + ", ".join(f"{a} {name(a)}" for a in accounts[1:]))
        lines.append(f"React ✅ to approve the best match, or type `add {phone_key(phone)} to <account number>`.")
    else:
        lines.append(f"I couldn't tell which customer. Type `add {phone_key(phone)} to <account number>` to approve.")
    slack.chat_postMessage(channel=desk_id, text="\n".join(lines), unfurl_links=False, metadata={
        "event_type": KIND, "event_payload": {"kind": "sms_signup", "phone": "+1" + phone_key(phone),
                                              "accounts": accounts[:1], "name": who}})


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
        handled = self.office_pass() if not first else 0
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
            return self.sign_up(phone, body, sid, n)
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

    # ---- sign-up: an unknown number tells us who they are, the office approves --------------------------------

    def sign_up(self, phone: str, body: str, sid: str, n: int) -> None:
        key = phone_key(phone)
        p = self.state.setdefault("pending", {}).get(key)
        if n > MAX_TEXTS_PER_DAY:
            return
        if p is None:  # first text: hold it and ask who they are
            self.state["pending"][key] = {"phone": phone, "stage": "asked", "held": [[body, sid]], "at": time.time()}
            self.send(phone, WHO_ARE_YOU)
            return
        if p["stage"] == "asked":  # their answer: suggest an account to the office
            from .parser import identify_customer
            found = identify_customer(self.claude, body, self.alliant.customers)
            p.update(stage="waiting", intro=body, name=found.person_name or "")
            signup_card(self.slack, self.desk_id, self.alliant, phone, found.person_name or "", body, found.accounts)
            self.send(phone, WAITING)
            return
        p["held"].append([body, sid])  # waiting for the office: keep it for after approval

    def office_pass(self) -> int:
        """Office side: ✅ on a sign-up card approves it; `add <phone> to <account or name>` and `remove <phone>`
        typed in the office channel (or a driver's add, forwarded by desk.py as a card) change the phone list."""
        path = os.path.join(self.data_dir, "customer_phones.csv")
        done = set(self.state.setdefault("cmds", []))
        r = self.slack.conversations_history(channel=self.desk_id, oldest=str(time.time() - 14 * 86400), limit=200,
                                             include_all_metadata=True)
        changed = 0
        for m in reversed(r["messages"]):
            pl = ((m.get("metadata") or {}).get("event_payload") or {})
            if m["ts"] in done:
                continue
            if pl.get("kind") == "sms_signup" and pl.get("accounts"):
                who = next((u for x in m.get("reactions", []) if x.get("name") in DONE_REACTIONS
                            for u in x.get("users", []) if not m.get("user") or u != m.get("user")), None)
                if who:
                    self.approve(pl["phone"], pl["accounts"], pl.get("name", ""), path)
                    self.slack.chat_update(channel=self.desk_id, ts=m["ts"], text=m["text"] + "\n✅ *Approved*",
                                           metadata={"event_type": KIND, "event_payload": {**pl, "kind": "sms_approved"}})
                    done.add(m["ts"])
                    changed += 1
                continue
            if m.get("bot_id") or m.get("subtype"):
                continue
            text = m.get("text", "")
            add, rem = ADD_CMD.match(text), REMOVE_CMD.match(text)
            if rem:
                ok = remove_phone(path, rem.group(1))
                self.state.get("pending", {}).pop(phone_key(rem.group(1)), None)
                self.office(f"📱 {'Removed' if ok else 'No texting set up for'} {rem.group(1).strip()}.", {"kind": "sms_admin"})
                changed += 1
            elif add:
                ids = {c.account for c in self.alliant.customers}
                known = [t for t in re.split(r"[;,\s]+", add.group(2)) if t in ids]
                if known:
                    self.approve(add.group(1), known, "", path)
                    self.office(f"📱 {add.group(1).strip()} can now text for " +
                                ", ".join(f"{a} {self.name(self.alliant, a)}" for a in known) + ".", {"kind": "sms_admin"})
                else:  # a name, not an account number: suggest, and let a ✅ confirm which
                    from .parser import identify_customer
                    found = identify_customer(self.claude, add.group(2), self.alliant.customers)
                    signup_card(self.slack, self.desk_id, self.alliant, add.group(1), "", add.group(2), found.accounts,
                                by="Office")
                changed += 1
            else:
                continue
            done.add(m["ts"])
        self.state["cmds"] = list(done)[-500:]
        return changed

    def approve(self, phone: str, accounts: list[str], name: str, path: str) -> None:
        key = phone_key(phone)
        p = self.state.get("pending", {}).pop(key, None)
        name = name or (p or {}).get("name", "")
        now = save_phone(path, "+1" + key, accounts, name)
        to = (p or {}).get("phone") or "+1" + key
        names = " and ".join(self.name(self.alliant, a) for a in now)
        self.send(to, SET_UP.format(names=names))
        for body, sid in (p or {}).get("held", []):  # what they texted before being set up
            self.handle(to, body, sid)

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

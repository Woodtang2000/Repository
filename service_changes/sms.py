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
    slack.chat_postMessage(channel=desk_id, text="", attachments=attachment("\n".join(lines), button=False),
                           unfurl_links=False, metadata={
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
        signup_card(self.slack, self.desk_id, self.alliant, addr, who, said, accounts, by=by)

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
                who = next((u for x in m.get("reactions", []) if x.get("name") in DONE_REACTIONS
                            for u in x.get("users", []) if not m.get("user") or u != m.get("user")), None)
                if who:
                    self.approve(pl["phone"], pl["accounts"], pl.get("name", ""))
                    self.slack.chat_update(channel=self.desk_id, ts=m["ts"], text="",
                                           attachments=shrink(self.slack, self.desk_id, m, GREEN,
                                                              stamp(person(self.slack, who), "Approved"), lines=1),
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

    def approve(self, phone: str, accounts: list[str], name: str) -> None:
        key = self.key(phone)
        p = self.state.get("pending", {}).pop(key, None)
        name = name or (p or {}).get("name", "")
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

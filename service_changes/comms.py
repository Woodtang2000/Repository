"""Everything Donna says to customers, and they to her, in one place (Scott, 11/5: "we don't know what or who she is
communicating with"): #customer-messages (MESSAGES_CHANNEL), private to the office.

One line per person per day: "📱 Toni · 5040-1-00000 CHILIS · 3 messages · last 10:47 AM". Each text, email or call,
in and out, goes in that line's thread, so the channel stays short and nothing Donna sends is out of sight.
Scott's and Bill's own texts and our own staff's emails are never logged here (their money answers stay private).
Off until the channel exists and Donna is in it. Never holds up a text or email: posting runs on its own thread.
"""
import csv
import fcntl
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

from .context import ALASKA

SYNC = False  # tests post inline
_client = None
_channel: dict = {}
ICON = {"text": "📱", "email": "📧", "call": "📞"}


def _data() -> str:
    return os.environ.get("SERVICE_DESK_DATA", os.path.join(os.path.dirname(__file__), "data"))


def _slack():
    global _client
    if _client is None:
        from slack_sdk import WebClient
        _client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
    return _client


def channel_id(slack) -> str:
    name = os.environ.get("MESSAGES_CHANNEL", "customer-messages")
    if name not in _channel or (not _channel[name] and time.time() - _channel.get(name + "@", 0) > 600):
        found = ""
        try:
            for p in slack.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=1000):
                found = next((c["id"] for c in p["channels"] if c.get("name") == name and c.get("is_member", True)), found)
        except Exception:
            pass
        _channel[name], _channel[name + "@"] = found, time.time()
    return _channel[name]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def private(addr: str) -> bool:
    """Scott's and Bill's numbers, and our own staff's email: their conversations aren't customer messages."""
    a = (addr or "").strip().lower()
    if "@" in a:
        own = tuple("@" + d.strip() for d in os.environ.get("EMAIL_OWN_DOMAIN", "snowwhitelinen.com,thelaundrygroup.com").split(","))
        trusted = {x.strip().lower() for x in os.environ.get("EMAIL_TRUSTED", "").split(",") if "@" in x}
        return a.endswith(own) or a in trusted
    d = _digits(a)
    mine = {_digits(x) for x in os.environ.get("OWNER_PHONES", "").split(",")}
    mine |= {_digits(x.split(":")[0]) for x in os.environ.get("TRUSTED_PHONES", "").split(",")}
    return bool(d) and d in mine


def who(addr: str) -> tuple[str, str]:
    """(their name, "account NAME") from the linked numbers / emails and the Alliant customer list, if known."""
    data = _data()
    a = (addr or "").strip().lower()
    rows, col = ("customer_emails.csv", "email") if "@" in a else ("customer_phones.csv", "phone")
    name, acct = "", ""
    try:
        for r in csv.DictReader(open(os.path.join(data, rows), newline="", encoding="utf-8-sig")):
            key = (r.get(col) or "").strip().lower()
            if (key == a) if "@" in a else (_digits(key) == _digits(a)):
                name, acct = r.get("name", ""), (r.get("accounts") or "").split(";")[0]
                break
    except OSError:
        pass
    if acct:
        try:
            for r in csv.DictReader(open(os.path.join(data, "customers.csv"), newline="", encoding="utf-8-sig")):
                if r.get("account") == acct:
                    acct = f"{acct} {r.get('name', '')}".strip()
                    break
        except OSError:
            pass
    return name, acct


_ALLIANT: dict = {}


def _alliant():
    """Alliant's customer list and cards, reloaded when the nightly files change (suggestions only)."""
    from .context import Alliant
    data = _data()
    try:
        stamp = os.path.getmtime(os.path.join(data, "customers.csv"))
    except OSError:
        stamp = 0
    if _ALLIANT.get("at") != stamp:
        _ALLIANT.update(at=stamp, a=Alliant.from_dir(data))
    return _ALLIANT["a"]


FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "msn.com", "live.com",
             "comcast.net", "gci.net", "acsalaska.net", "me.com", "mtaonline.net", "alaska.net"}


def contact_key(addr: str) -> str:
    """The key a #new-contacts card uses for this person: the email, or +1 and ten digits."""
    a = (addr or "").strip().lower()
    return a if "@" in a else "+1" + _digits(a)


def suggest(slack, addr: str) -> list[str]:
    """Best accounts for someone not linked yet (Scott, 11/12: "a suggestion and a button"): the guess on their waiting
    #new-contacts card, then the Alliant cards that list this email or phone, then (email) customers whose cards use
    the same company domain."""
    out: list[str] = []
    try:
        from .sms import cards_channel, open_card
        card = open_card(slack, cards_channel(slack, ""), contact_key(addr))
        if card:
            out += ((card.get("metadata") or {}).get("event_payload") or {}).get("accounts") or []
    except Exception:
        pass
    try:
        al = _alliant()
        a = (addr or "").strip().lower()
        dom = a.split("@")[-1] if "@" in a else ""
        same_domain = []
        for acct, card in al.cards.items():
            text = " ".join(str(card.get(f) or "") for f in ("email", "location_email", "phone")).lower()
            if ("@" in a and a in text) or ("@" not in a and _digits(a) and _digits(a) in re.sub(r"\D", "", text)):
                out.append(acct)
            elif dom and dom not in FREE_MAIL and "@" + dom in text:
                same_domain.append(acct)
        out += same_domain
    except Exception as e:
        print(time.strftime("%H:%M"), "customer-messages suggestion:", str(e)[:150], flush=True)
    return list(dict.fromkeys(x for x in out if x))[:20]


def link_block(slack, addr: str, accounts: list[str]) -> list[dict]:
    """The #new-contacts card's buttons, for a #customer-messages line: Add to the best match, all its locations, or
    the ones picked. Finished through that person's #new-contacts card (desk.msg_link), never here."""
    key = contact_key(addr)
    if not accounts:
        return [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"No match found. Type `add {key} to <account "
                                                  "number>` in #new-contacts to link them."}]}]
    from .sms import link_buttons, locations
    al = _alliant()
    name = lambda a: next((c.name for c in al.customers if c.account == a), "")
    blk = link_buttons(accounts, [], name, locations(al, accounts))
    els = []
    for e in (blk or {}).get("elements", []):
        if e.get("action_id") == "link_block":
            continue  # blocking stays on the #new-contacts card
        e = dict(e, action_id="msg" + e["action_id"])
        if e.get("value"):
            e["value"] = json.dumps({**json.loads(e["value"]), "k": key})
        els.append(e)
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"Best match: *{accounts[0]}* {name(accounts[0])}"}]},
            {"type": "actions", "block_id": "msglink", "elements": els[:25]}]


def mark_linked(addr: str) -> None:
    """A #customer-messages line's buttons were used: today's line for this person stops offering them."""
    path = os.path.join(_data(), "comms_threads.json")
    key = f"{datetime.now(ALASKA):%Y-%m-%d}|{addr.strip().lower() if '@' in addr else _digits(addr)}"
    try:
        with open(path + ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.load(open(path))
            except (OSError, ValueError):
                state = {}
            if key in state:
                state[key]["linked"] = True
                with open(path + ".tmp", "w") as f:
                    json.dump(state, f)
                os.replace(path + ".tmp", path)
    except OSError:
        pass


def log(direction: str, kind: str, addr: str, text: str, name: str = "") -> None:
    """direction "in" (from them) or "out" (Donna); kind "text", "email" or "call". Never raises."""
    if not addr or private(addr) or not (text or "").strip():
        return
    if _client is None and (not os.environ.get("SLACK_BOT_TOKEN") or "pytest" in sys.modules
                            or os.environ.get("COMMS_LOG", "on").lower() == "off"):
        return  # never from tests or one-off scripts (11/6: a test run posted examples to the real channel)
    if SYNC:
        _post(direction, kind, addr, text, name)
    else:
        threading.Thread(target=_post, args=(direction, kind, addr, text, name), daemon=True).start()


def _post(direction: str, kind: str, addr: str, text: str, name: str) -> None:
    try:
        slack = _slack()
        ch = channel_id(slack)
        if not ch:
            return
        now = datetime.now(ALASKA)
        known, acct = who(addr)
        person = name or known or (addr if "@" in addr else f"(…{_digits(addr)[-4:]})")
        key = f"{now:%Y-%m-%d}|{addr.strip().lower() if '@' in addr else _digits(addr)}"
        path = os.path.join(_data(), "comms_threads.json")
        with open(path + ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.load(open(path))
            except (OSError, ValueError):
                state = {}
            from datetime import timedelta
            keep = f"{now - timedelta(days=2):%Y-%m-%d}"
            state = {k: v for k, v in state.items() if k[:10] >= keep}  # a few days of threads, no more
            t = state.get(key) or {}
            t["n"] = t.get("n", 0) + 1
            head = (f"{ICON.get(kind, '💬')} *{person}*" + (f" · {acct}" if acct else " · not linked to an account yet")
                    + f" · {t['n']} message{'s' if t['n'] != 1 else ''} today · last {now:%-I:%M %p}")
            blocks = None
            if not acct and kind in ("email", "text") and not t.get("linked") and (direction == "in" or t.get("sugg") is not None):
                if t.get("sugg") is None:  # worked out once a day per person
                    t["sugg"] = suggest(slack, addr)
                blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}] + link_block(slack, addr, t["sugg"])
            elif t.get("sugg") is not None:  # it had buttons and is linked now: plain again
                blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
            kw = {"blocks": blocks} if blocks else {}
            if t.get("ts"):
                slack.chat_update(channel=ch, ts=t["ts"], text=head, **kw)
            else:
                t["ts"] = slack.chat_postMessage(channel=ch, text=head, unfurl_links=False, **kw)["ts"]
            state[key] = t
            with open(path + ".tmp", "w") as f:
                json.dump(state, f)
            os.replace(path + ".tmp", path)
        line = (f"⬅️ *{person}* by {kind}" if direction == "in" else f"➡️ *Donna* by {kind}") + f" · {now:%-I:%M %p}\n"
        body = re.sub(r"\n{3,}", "\n\n", text.strip())
        slack.chat_postMessage(channel=ch, thread_ts=t["ts"], text=line + (body[:1200] + (" …" if len(body) > 1200 else "")),
                               unfurl_links=False)
    except Exception as e:
        print(time.strftime("%H:%M"), "customer-messages log:", str(e)[:150], flush=True)

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


def log(direction: str, kind: str, addr: str, text: str, name: str = "") -> None:
    """direction "in" (from them) or "out" (Donna); kind "text", "email" or "call". Never raises."""
    if not addr or private(addr) or not (text or "").strip():
        return
    if _client is None and not os.environ.get("SLACK_BOT_TOKEN"):  # no Slack here (tests, one-off scripts)
        return
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
            if t.get("ts"):
                slack.chat_update(channel=ch, ts=t["ts"], text=head)
            else:
                t["ts"] = slack.chat_postMessage(channel=ch, text=head, unfurl_links=False)["ts"]
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

"""Donna tells customers before they have to ask: the office posts in #service-desk ("Donna, let route 4 know we're
running 2 hours late", "Announce: Thanksgiving, Thursday deliveries move to Wednesday") and she replies with exactly
who would get what. Nothing is sent until someone in the office taps Send.

Who: the stops on that route and/or service day (route_stops.csv, matched on branch + route, since Snow White and The
Laundry Group both have a route 1). How: a text to each number the office approved for texting on those accounts
(customer_phones.csv) and an email to each account's contact on its Alliant card, each counted once. Never prices.
"""
import json
import os
import re
import threading
import time
from typing import Literal

from pydantic import BaseModel, Field

FILE = "announcements.json"
SEND, CANCEL = "announce_send", "announce_cancel"
STOP_LINE = " Reply STOP to opt out."  # on every text we start ourselves (toll-free review, Scott 11/16); not on email


def with_stop(msg: str) -> str:
    """A text we start, with the opt-out: "Questions? Just reply, or reply STOP to opt out." reads better than two."""
    for ask in ("Questions? Just reply.", "Reply here if anything else needs fixing."):
        if msg.rstrip().endswith(ask):
            return msg.rstrip()[:-1] + ", or reply STOP to opt out."
    return msg.rstrip() + STOP_LINE
ASK = re.compile(r"^\s*(?:donna,?\s+)?(?:announce|let|tell|text|email|notify|message|warn)\b.{0,80}\b(?:customers?|"
                 r"route\s*\d+|everyone|all (?:of )?(?:our|the)|(?:mon|tues|wednes|thurs|fri|satur|sun)day)", re.I)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MODEL = os.environ.get("ANNOUNCE_MODEL", "claude-sonnet-5-5")


class Announcement(BaseModel):
    is_announcement: bool = Field(description="True if this asks Donna to send a message to customers.")
    branch: Literal["1", "2", "both"] = Field("1", description="1 = Snow White Linen, 2 = The Laundry Group, both if said "
                                                              "or unclear and no route is named.")
    route: str = Field("", description="Route number if a route is named, else empty.")
    day: str = Field("", description="Mon..Sun if a service day is meant ('today' = today's day, 'Thursday deliveries' = "
                                     "Thu), else empty.")
    message: str = Field(description="What customers read: friendly, short, plain words, in Donna's voice, from the "
                                     "company. No prices. Don't sign it; no greeting line.")
    subject: str = Field(description="Short email subject.")


def parse(client, text: str, today: str) -> Announcement:
    r = client.beta.messages.parse(
        model=MODEL, max_tokens=1500, output_format=Announcement,
        messages=[{"role": "user", "content": f"Today is {today}. Someone in the office of Snow White Linen (and its sister "
                   f"company The Laundry Group) wrote to Donna, the company's assistant:\n\n{text}\n\nIs this a request to "
                   "send customers a message? If so, who, and write the message."}])
    return r.parsed_output


class Announcer:
    def __init__(self, desk):
        self.desk = desk
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.load(open(os.path.join(self.desk.data_dir, FILE)))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        path = os.path.join(self.desk.data_dir, FILE)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)

    def audience(self, a: Announcement) -> list[str]:
        """The accounts: stops on the route and/or day (route_stops.csv), branch + route matched."""
        stops = getattr(self.desk.alliant, "stops", [])
        branches = {"1", "2"} if a.branch == "both" else {a.branch}
        accts = []
        for r in stops:
            if r.get("branch", "1").lstrip("0") not in branches:
                continue
            if a.route and r.get("route", "").lstrip("0") != a.route.lstrip("0"):
                continue
            if a.day and r.get("day", "")[:3].title() != a.day[:3].title():
                continue
            accts.append(r.get("account", ""))
        return list(dict.fromkeys(x for x in accts if x))

    def contacts(self, accounts: list[str]) -> tuple[dict[str, str], dict[str, str]]:
        """{phone: account} for approved texting numbers, {email: account} from the Alliant cards; each once."""
        from .sms import load_phones
        mine = set(accounts)
        phones = {}
        for num, who in load_phones(os.path.join(self.desk.data_dir, "customer_phones.csv")).items():
            hit = next((x for x in who["accounts"] if x in mine), None)
            if hit:
                phones["+1" + num] = hit
        emails = {}
        from .context import customer_emails  # a sub-account (Costco, Safeway): its own linked contacts only
        for acct in accounts:
            for e in customer_emails(self.desk.alliant, acct, self.desk.data_dir):
                if e not in emails:
                    emails[e] = acct
        return phones, emails

    def propose(self, h: dict) -> bool:
        """The office's request: a preview with Send / Cancel buttons, or a question if it's unclear who."""
        from datetime import datetime
        from .context import ALASKA
        user = h.get("user")
        if not (self.desk.is_office(user) or self.desk.is_owner(user)):
            return False
        now = datetime.now(ALASKA)
        a = parse(self.desk.claude, h.get("text", ""), f"{now:%A %B} {now.day} ({DAYS[now.weekday()]})")
        if not a or not a.is_announcement:
            return False
        if not a.route and not a.day:
            self.desk.post(self.desk.desk_id, f"<@{user}> Which customers should get it? Name a route, a delivery day, or "
                                              "both (\"route 4 customers\", \"everyone delivered Thursday\").",
                           {"kind": "answer"}, thread_ts=h["ts"])
            return True
        accounts = self.audience(a)
        phones, emails = self.contacts(accounts)
        who = " · ".join(x for x in (
            {"1": "Snow White", "2": "The Laundry Group", "both": "Snow White and The Laundry Group"}[a.branch],
            f"route {a.route}" if a.route else "", f"{a.day} deliveries" if a.day else "") if x)
        if not accounts or not (phones or emails):
            self.desk.post(self.desk.desk_id, f"<@{user}> I don't have anyone to send that to ({who}: {len(accounts)} "
                                              f"customers, {len(phones)} texting numbers, {len(emails)} emails).",
                           {"kind": "answer"}, thread_ts=h["ts"])
            return True
        aid = h["ts"]
        with self.lock:
            data = self._load()
            data[aid] = {"by": user, "who": who, "message": a.message, "subject": a.subject, "phones": phones,
                         "emails": emails, "accounts": len(accounts), "status": "waiting", "at": time.time()}
            self._save(data)
        text = (f"📣 *Ready to send* to {len(accounts)} customers ({who}): {len(phones)} texts, {len(emails)} emails.\n"
                f"> {a.message}\nNothing goes out until you tap Send.")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                  {"type": "actions", "block_id": "announce", "elements": [
                      {"type": "button", "action_id": SEND, "value": aid, "style": "primary",
                       "text": {"type": "plain_text", "text": f"Send to {len(phones) + len(emails)}"}},
                      {"type": "button", "action_id": CANCEL, "value": aid, "text": {"type": "plain_text", "text": "Cancel"}}]}]
        self.desk.slack.chat_postMessage(channel=self.desk.desk_id, thread_ts=h["ts"], text=text, blocks=blocks,
                                         unfurl_links=False)
        return True

    def propose_holiday(self, key: str, name: str, holiday, per_account: dict, still_open: list | None = None) -> bool:
        """A holiday notice (holidays.py): each customer whose delivery moves is told their own day. Same Send / Cancel
        card as any announcement, posted in #service-desk by Donna herself, never sent without a tap."""
        from collections import Counter
        from .holidays import NOTICE_DAYS, notice, open_notice
        from datetime import timedelta
        still_open = [a for a in (still_open or []) if a not in per_account]
        messages = {acct: notice(name, holiday, m) for acct, m in per_account.items()}
        messages.update({acct: open_notice(name, holiday) for acct in still_open})
        phones, emails = self.contacts(list(messages))
        moved = Counter(f"{a:%a} → {b:%a %-m/%-d}" for m in per_account.values() for a, b in m)
        send_by = holiday - timedelta(days=NOTICE_DAYS)
        examples = [messages[a] for a in (next(iter(per_account), None), next(iter(still_open), None)) if a]
        sample = examples[0]
        with self.lock:
            data = self._load()
            if key in data:
                return False
            data[key] = {"by": "donna", "who": f"{name} notice", "message": sample, "messages": messages,
                         "subject": f"{name}: your deliveries", "phones": phones, "emails": emails,
                         "accounts": len(messages), "status": "waiting" if (phones or emails) else "nobody",
                         "at": time.time()}
            self._save(data)
        text = f"🗓️ *{name}, {holiday:%A %B} {holiday.day}*: " + "; ".join(x for x in (
            (f"{len(per_account)} customers' deliveries move (" + ", ".join(f"{k}: {v}" for k, v in sorted(moved.items()))
             + ")") if per_account else "",
            f"{len(still_open)} Laundry Group customers hear we're open as usual" if still_open else "") if x) + ".\n"
        if not (phones or emails):
            self.desk.slack.chat_postMessage(channel=self.desk.desk_id, unfurl_links=False,
                                             text=text + "None of them has a texting number or email on file, so I have "
                                                         "no one to tell. Please let them know another way.")
            return True
        text += (f"Send them each their own day by *{send_by:%A %B} {send_by.day}* (a week ahead): {len(phones)} texts, "
                 f"{len(emails)} emails. For example:\n" + "\n".join(f"> {m}" for m in examples)
                 + "\nNothing goes out until you tap Send.")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                  {"type": "actions", "block_id": "announce", "elements": [
                      {"type": "button", "action_id": SEND, "value": key, "style": "primary",
                       "text": {"type": "plain_text", "text": f"Send to {len(phones) + len(emails)}"}},
                      {"type": "button", "action_id": CANCEL, "value": key, "text": {"type": "plain_text", "text": "Cancel"}}]}]
        self.desk.slack.chat_postMessage(channel=self.desk.desk_id, text=text, blocks=blocks, unfurl_links=False)
        return True

    def act(self, payload: dict) -> str:
        """Send or Cancel, office or owner only, once."""
        user = payload.get("user", {}).get("id")
        act = next((x for x in payload.get("actions", []) if x.get("action_id") in (SEND, CANCEL)), {})
        if not (self.desk.is_office(user) or self.desk.is_owner(user)):
            return "ignored: not office"
        with self.lock:
            data = self._load()
            a = data.get(act.get("value", ""))
            if not a or a["status"] != "waiting":
                return "already handled"
            a["status"] = "sending" if act.get("action_id") == SEND else "cancelled"
            a["decided_by"] = user
            self._save(data)
        msg = payload.get("container", {}).get("message_ts") or payload.get("message", {}).get("ts")
        name = self.desk.name(user).split()[0] if self.desk.name(user) else "office"
        if a["status"] == "cancelled":
            self.desk.slack.chat_update(channel=self.desk.desk_id, ts=msg, text=f"🚫 Not sent (cancelled by {name}).",
                                        blocks=[])
            return "cancelled"
        sent = self.send(a)
        with self.lock:
            data = self._load()
            data[act["value"]].update(status="sent", sent=sent)
            self._save(data)
        self.desk.slack.chat_update(channel=self.desk.desk_id, ts=msg, blocks=[],
                                    text=f"✅ Sent by {name} to {a['who']}: {sent['texts']} texts, {sent['emails']} emails"
                                         + (f" ({sent['failed']} didn't go through)" if sent["failed"] else "")
                                         + f".\n> {a['message']}")
        return f"sent {sent}"

    def send(self, a: dict) -> dict:
        from . import bird
        from .context import company_for
        from .sms import send_sms
        out = {"texts": 0, "emails": 0, "failed": 0}
        msg = lambda acct: (a.get("messages") or {}).get(acct, a["message"])  # a holiday notice: each customer's own day
        for phone, acct in a["phones"].items():
            try:
                send_sms(phone, with_stop(f"{company_for(acct)}: {msg(acct)}"))  # texts we start: the opt-out on each
                out["texts"] += 1
            except Exception as e:
                out["failed"] += 1
                print(time.strftime("%H:%M"), "announce text failed:", str(e)[:120], flush=True)
        for email, acct in a["emails"].items():
            company = company_for(acct)
            try:
                bird.send(email, f"{company}: {a['subject']}", f"Hello,\n\n{msg(acct)}\n\nThank you,\n\n"
                          + bird.signature(company), company=company, key=f"announce/{a['at']}/{email}")
                out["emails"] += 1
            except Exception as e:
                out["failed"] += 1
                print(time.strftime("%H:%M"), "announce email failed:", str(e)[:120], flush=True)
        return out

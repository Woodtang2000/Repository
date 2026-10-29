"""Customers email in changes for their own account(s), the same way they text (sms.py): the office works the ticket
in Slack and the readback goes back by email.

  python -m service_changes.email_desk --data service_changes/data --desk service-desk

A mailbox (e.g. service@snowwhitelinen.com) forwards to a Bird inbound address; this polls Bird for what arrived,
so the server needs no open port. Replies go out from the Bird sending domain (EMAIL_FROM) with Reply-To set to the
real mailbox (EMAIL_REPLY_TO), so a customer's answer comes back the same way.

Each address is pinned to its account(s) in `customer_emails.csv` (email,accounts,name), kept by the office: an
unknown sender gets a sign-up card in the office channel (best match from the Alliant invoice and location emails
first), ✅ approves; or type `add jane@acme.com to <account or name>` / `remove jane@acme.com`. As with texts, Claude
only ever sees the sender's own accounts, and every reply goes to the address on file, never anywhere else.

Email senders can be forged, so a ticket from a message whose SPF and DKIM both failed to pass is marked for the
office to confirm by phone. Staff (our own domain) forwarding a customer's email get a ticket read against every
account, with the readback emailed to them. Needs BIRD_API_KEY and BIRD_INBOUND_ADDRESS_ID.
"""
import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import bird
from .context import Alliant
from .sms import KIND, MAX_TEXTS_PER_DAY, TextDesk, plain
from .todo import attachment

EMAIL = r"([A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
ADD_EMAIL = re.compile(rf"^\s*(?:add|approve)\s+<?(?:mailto:)?{EMAIL}(?:\|[^>]*)?>?\s+(?:to|for)\s+(.+?)\s*$", re.I | re.S)
REMOVE_EMAIL = re.compile(rf"^\s*(?:remove|delete)\s+<?(?:mailto:)?{EMAIL}(?:\|[^>]*)?>?\s*$", re.I)
OWN_DOMAIN = os.environ.get("EMAIL_OWN_DOMAIN", "snowwhitelinen.com")
AUTO = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounce)", re.I)
AUTO_SUBJECT = re.compile(r"^(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|undeliverable|delivery status|"
                          r"returned mail|read:|accepted:|declined:)", re.I)
FILTERED = os.environ.get("EMAIL_FILTERED_CHANNEL", "email-filtered")  # where junk mail is listed, for Scott to watch
BULK = re.compile(r"^(list-unsubscribe|list-id|precedence)$", re.I)
QUOTE = re.compile(r"^(On .{5,200} wrote:|-{2,}\s*Original Message|From: .+|Sent from my |_{5,})", re.I | re.M)


def is_email_command(text: str) -> bool:
    return bool(ADD_EMAIL.match(text or "") or REMOVE_EMAIL.match(text or ""))


def email_key(addr: str) -> str:
    m = re.search(EMAIL, addr or "")
    return m.group(1).lower() if m else (addr or "").strip().lower()


def clean_body(text: str) -> str:
    """The new part of an email: drop the quoted thread under "On … wrote:" and the like."""
    m = QUOTE.search(text or "")
    body = (text or "")[:m.start()] if m else (text or "")
    body = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", body).strip()[:4000]


def alliant_emails(alliant: Alliant) -> dict[str, list[str]]:
    """Invoice and location emails on the Alliant cards -> the accounts they appear on (sign-up suggestions only)."""
    out: dict[str, list[str]] = {}
    for acct, card in alliant.cards.items():
        for field in ("email", "location_email"):
            for e in re.findall(EMAIL, card.get(field) or ""):
                out.setdefault(e.lower(), []).append(acct)
    return out


def load_emails(path: str) -> dict[str, dict]:
    out = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                accts = [a.strip() for a in re.split(r"[;,\s]+", row.get("accounts", "")) if a.strip()]
                if email_key(row.get("email", "")) and accts:
                    out[email_key(row["email"])] = {"accounts": accts, "name": (row.get("name") or "").strip()}
    return out


def _write(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["email", "accounts", "name"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _addresses(v) -> list[str]:
    """Email addresses from a Bird field that may be a dict, a list of dicts, or a header string."""
    if isinstance(v, dict):
        v = [v]
    if isinstance(v, list):
        return [x.get("email", "") if isinstance(x, dict) else str(x) for x in v]
    return re.findall(r"[\w.+'-]+@[\w.-]+\.\w+", v or "")


def real_sender(m: dict) -> str:
    """Who really wrote it. service@ is a Google Group: when the customer's own domain is strict about DMARC (gmail,
    yahoo…) the group rewrites From to the group address ("Jo via service") and puts the customer in Reply-To."""
    frm = (_addresses(m.get("from")) or [""])[0]
    group = email_key(os.environ.get("EMAIL_REPLY_TO", "service@snowwhitelinen.com"))
    if email_key(frm) != group:
        return frm
    headers = m.get("headers") or {}
    if isinstance(headers, list):
        headers = {h.get("name", ""): h.get("value", "") for h in headers if isinstance(h, dict)}
    for v in (m.get("reply_to"), headers.get("Reply-To"), headers.get("X-Original-From"), headers.get("X-Original-Sender")):
        found = [a for a in _addresses(v) if email_key(a) != group]
        if found:
            return found[0]
    return frm


class EmailDesk(TextDesk):
    word, icon, to_key, signup_kind, list_file = "email", "📧", "email_to", "email_signup", "customer_emails.csv"
    add_cmd, remove_cmd = ADD_EMAIL, REMOVE_EMAIL
    set_up = ("You're all set! Email me here any time you need anything for {names}, and I'll confirm each change as soon "
              "as it's entered.")

    def __init__(self, slack, claude, alliant: Alliant, desk: str, data_dir: str, send=None, fetch=None):
        super().__init__(slack, claude, alliant, desk, data_dir, send=send or self._send)
        self.fetch = fetch or (lambda since: bird.received(since, os.environ.get("BIRD_INBOUND_ADDRESS_ID", "")))
        self.state_path = os.path.join(data_dir, "email_state.json")
        self.state = {}
        try:
            import json
            with open(self.state_path) as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            pass
        for k in ("seen", "cmds"):
            self.state.setdefault(k, [])
        for k in ("convos", "counts", "threads"):
            self.state.setdefault(k, {})

    # ---- hooks for TextDesk ---------------------------------------------------------------------------------

    def key(self, addr: str) -> str:
        return email_key(addr)

    def addr(self, key: str) -> str:
        return key

    def masked(self, addr: str) -> str:
        return email_key(addr)

    def contacts(self) -> dict[str, dict]:
        return load_emails(os.path.join(self.data_dir, self.list_file))

    def save_contact(self, addr: str, accounts: list[str], name: str) -> list[str]:
        path = os.path.join(self.data_dir, self.list_file)
        rows = _rows(path)
        row = next((r for r in rows if email_key(r.get("email", "")) == email_key(addr)), None)
        if row is None:
            row = {"email": email_key(addr), "accounts": "", "name": name}
            rows.append(row)
        have = [a for a in re.split(r"[;,\s]+", row.get("accounts", "")) if a]
        row["accounts"] = ";".join(have + [a for a in accounts if a not in have])
        row["name"] = row.get("name") or name
        _write(path, rows)
        return row["accounts"].split(";")

    def remove_contact(self, addr: str) -> bool:
        path = os.path.join(self.data_dir, self.list_file)
        rows = _rows(path)
        keep = [r for r in rows if email_key(r.get("email", "")) != email_key(addr)]
        _write(path, keep)
        return len(keep) < len(rows)

    def card(self, addr: str, who: str, said: str, accounts: list[str], by: str = "") -> None:
        name = lambda a: next((c.name for c in self.alliant.customers if c.account == a), "")
        on_file = alliant_emails(self.alliant).get(email_key(addr), [])
        accounts = list(dict.fromkeys(on_file + accounts))  # on an Alliant card first, then Claude's guess
        head = f"📧 *{by}* asks to set up {email_key(addr)} for email" if by else f"📧 *New emailer* {email_key(addr)}"
        lines = [head + (f": {who}" if who else ""), f"_{said[:200]}_"]
        if on_file:
            lines.append(f"On the Alliant card for: " + ", ".join(f"{a} {name(a)}" for a in on_file[:6])
                         + (f" and {len(on_file) - 6} more" if len(on_file) > 6 else ""))
        current = self.contacts().get(self.key(addr), {}).get("accounts", [])
        if current:
            lines.append("This email is now on: " + ", ".join(f"{a} {name(a)}" for a in current))
        if accounts:
            lines.append(f"Best match: *{accounts[0]}* {name(accounts[0])}")
            lines.append(f"Tap a button, or type `add {email_key(addr)} to <account number(s)>` for another account.")
        else:
            lines.append(f"I couldn't tell which customer. Type `add {email_key(addr)} to <account number>` to approve.")
        from .sms import link_buttons
        buttons = link_buttons(accounts, current, name)
        self.slack.chat_postMessage(channel=self.desk_id, text="", unfurl_links=False,
                                    attachments=attachment("\n".join(lines), button=False, extra=[buttons] if buttons else None),
                                    metadata={
            "event_type": KIND, "event_payload": {"kind": self.signup_kind, "phone": email_key(addr),
                                                  "accounts": accounts[:1], "name": who}})

    def inbound(self, since: datetime) -> list[dict]:
        out = []
        for m in self.fetch(since.strftime("%Y-%m-%dT%H:%M:%SZ")):
            sender = email_key(real_sender(m))
            subject = m.get("subject") or ""
            if AUTO.match(sender) or AUTO_SUBJECT.match(subject):
                continue  # out-of-office, bounces, no-reply senders: never answered (no mail loops)
            body = clean_body(m.get("text") or "")
            headers = m.get("headers") or {}
            names = [h.get("name", "") for h in headers if isinstance(h, dict)] if isinstance(headers, list) else list(headers)
            self.state["threads"][sender] = {
                "subject": subject, "msgid": m.get("message_id") or "", "name": (m.get("from") or {}).get("name") or "",
                "verified": bool(m.get("spf_pass") or m.get("dkim_pass")),
                "bulk": any(BULK.match(n) for n in names) or (m.get("precedence") or "").lower() in ("bulk", "list")}
            out.append({"id": m["id"], "from": sender,
                        "body": (f"Subject: {subject}\n" if subject and not subject.lower().startswith("re:") else "")
                        + body})
        return out

    def ticket_extra(self, addr: str) -> dict:
        t = self.state["threads"].get(email_key(addr), {})
        return {"email_subject": t.get("subject", ""), "email_msgid": t.get("msgid", "")}

    def ticket_notes(self, addr: str) -> list[str]:
        if self.state["threads"].get(email_key(addr), {}).get("verified", True):
            return []
        return ["⚠️ _The sender's email couldn't be verified (SPF/DKIM). Confirm by phone before entering._"]

    def _send(self, to: str, body: str) -> None:
        t = self.state["threads"].get(email_key(to), {})
        subject = t.get("subject") or "Your Snow White Linen request"
        bird.send(email_key(to), subject if subject.lower().startswith("re:") else "Re: " + subject,
                  plain(body) + "\n\n" + bird.signature(), in_reply_to=t.get("msgid", ""))

    def sign_up(self, addr: str, body: str, sid: str, n: int) -> None:
        """An unknown sender: the email itself usually says who they are, so the office gets a card straight away and
        the message is held until it's approved."""
        key = email_key(addr)
        p = self.state.setdefault("pending", {}).get(key)
        if n > MAX_TEXTS_PER_DAY:
            return
        if p is not None:
            p["held"].append([body, sid])
            return
        from .parser import identify_customer, is_junk_email
        thread = self.state["threads"].get(key, {})
        who = thread.get("name", "")
        try:
            junk = is_junk_email(self.claude, f"{who} <{key}>", thread.get("subject", ""), body, thread.get("bulk", False))
        except Exception as e:  # can't check: treat it as a customer
            print(time.strftime("%H:%M"), "junk check failed:", e, flush=True)
            junk = None
        if junk is not None and junk.junk:
            return self.filtered(key, who, thread.get("subject", ""), body, sid, junk.kind)
        found = identify_customer(self.claude, f"From: {who} <{key}>\n{body}", self.alliant.customers)
        self.state["pending"][key] = {"phone": key, "stage": "waiting", "held": [[body, sid]], "at": time.time(),
                                      "name": found.person_name or who}
        self.card(key, found.person_name or who, body, found.accounts)
        self.send(key, "Thanks so much for your email! This is Donna. I'm getting your account confirmed with our "
                       "office, and then I'll take care of your request.")

    # ---- junk: listed for Scott, never answered ------------------------------------------------------------------

    def filtered(self, key: str, who: str, subject: str, body: str, sid: str, kind: str) -> None:
        """Marketing, spam and the like from an unknown sender: no reply (that would confirm the address to spammers)
        and no sign-up card; one line in #email-filtered. ✅ on it puts the email through as a customer's."""
        self.state.setdefault("filtered", {})[sid] = {"from": key, "body": body, "at": time.time()}
        old = sorted(self.state["filtered"].items(), key=lambda kv: kv[1]["at"])[:-300]
        for k, _ in old:
            self.state["filtered"].pop(k)
        where = self.filtered_channel()
        if where is None:
            print(time.strftime("%H:%M"), f"filtered ({kind}) from {key}: {subject[:80]}", flush=True)
            return
        self.slack.chat_postMessage(channel=where, unfurl_links=False, unfurl_media=False,
                                    text=f"🗑️ *{kind}* · {who + ' ' if who else ''}<mailto:{key}|{key}>\n*{subject[:150] or '(no subject)'}*"
                                         f"\n_{plain(body)[:200]}_\nNot junk? React ✅ and it goes to the office as a customer email.",
                                    metadata={"event_type": KIND, "event_payload": {"kind": "email_filtered", "sid": sid}})

    def filtered_channel(self) -> str | None:
        if "filtered_id" not in self.__dict__:
            self.filtered_id = next((c["id"] for p in self.slack.conversations_list(
                types="public_channel,private_channel", exclude_archived=True, limit=1000) for c in p["channels"]
                if c["name"] == FILTERED and c.get("is_member")), None)
        return self.filtered_id

    def rescue_pass(self) -> int:
        """✅ on a filtered email: it wasn't junk, so it goes through the normal sign-up as a customer's email."""
        where = self.filtered_channel()
        if where is None or not self.state.get("filtered"):
            return 0
        r = self.slack.conversations_history(channel=where, oldest=f"{time.time() - 14 * 86400:.6f}", limit=200,
                                             include_all_metadata=True)
        n = 0
        for m in r.get("messages", []):
            pl = (m.get("metadata") or {}).get("event_payload") or {}
            item = self.state["filtered"].get(pl.get("sid", "")) if pl.get("kind") == "email_filtered" else None
            if item and any(x.get("name") in ("white_check_mark", "heavy_check_mark") for x in m.get("reactions", [])):
                self.state["filtered"].pop(pl["sid"])
                self.slack.reactions_add(channel=where, timestamp=m["ts"], name="outbox_tray")
                from .parser import identify_customer
                key = item["from"]
                who = self.state["threads"].get(key, {}).get("name", "")
                found = identify_customer(self.claude, f"From: {who} <{key}>\n{item['body']}", self.alliant.customers)
                self.state.setdefault("pending", {})[key] = {"phone": key, "stage": "waiting", "held": [[item["body"], pl["sid"]]],
                                                             "at": time.time(), "name": found.person_name or who}
                self.card(key, found.person_name or who, item["body"], found.accounts)
                n += 1
        if n:
            self.save()
        return n

    def run_once(self, first: bool = False) -> int:
        n = super().run_once(first)
        if not first:
            try:
                n += self.rescue_pass()
            except Exception as e:
                print(time.strftime("%H:%M"), "rescue check failed:", e, flush=True)
        return n

    # ---- staff forwarding a customer's email ----------------------------------------------------------------

    def handle(self, addr: str, body: str, sid: str) -> None:
        if email_key(addr).endswith("@" + OWN_DOMAIN):
            return self.from_staff(addr, body, sid)
        super().handle(addr, body, sid)

    def from_staff(self, addr: str, body: str, sid: str) -> None:
        """Someone at Snow White forwarded or wrote in: read it against every account, ticket to the office."""
        from .checks import check, readback, ticket
        from .parser import parse_message
        from .run import fill_item_matches
        from .sms import ACTIONABLE
        name = self.state["threads"].get(email_key(addr), {}).get("name") or email_key(addr)
        parsed = parse_message(self.claude, body, "office-email", f"{time.time():.6f}", self.alliant,
                               author=f"{name} (office staff, forwarding a customer's email)")
        if parsed.category in ACTIONABLE and parsed.account_number:
            fill_item_matches(self.claude, parsed, self.alliant)
        result = check(parsed, self.alliant)
        if parsed.category in ACTIONABLE and parsed.account_number:
            out = ticket(result, self.alliant, with_readback=False).splitlines()
            rb = readback(result) if not result.questions else ""
            out += [f"❓ {q}" for q in result.questions]
        else:
            out = [f"📣 *Emailed in: {parsed.category.value.replace('_', ' ')}*", parsed.summary]
            rb = f"✅ {parsed.summary}"
        quote = "\n".join("> " + ln for ln in body.splitlines()[:8])
        self.office("\n".join(out + ["", quote, f"📧 Emailed in by {name} · React ✅ or type done when it's in Alliant"]),
                    {"kind": "ticket", "src_channel": "email", "src_ts": sid, "readback": rb, "driver": name,
                     "driver_id": "", "account": parsed.account_number or "", "msgs": [], "email_to": email_key(addr),
                     **self.ticket_extra(addr)}, todo="own")


def main():
    import anthropic
    from slack_sdk import WebClient

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    ap.add_argument("--desk", default="service-desk", help="office channel for tickets")
    ap.add_argument("--every", type=int, default=20, help="seconds between checks for new email")
    args = ap.parse_args()
    missing = [v for v in ("BIRD_API_KEY", "BIRD_INBOUND_ADDRESS_ID") if not os.environ.get(v)]
    if missing:
        sys.exit("Needs " + ", ".join(missing) + " (from the Bird dashboard).")
    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SERVICE_DESK_API_KEY"))
    ed = EmailDesk(slack, claude, Alliant.from_dir(args.data), args.desk.lstrip("#"), args.data)
    first, loaded = not ed.state["seen"], time.time()
    print(time.strftime("%H:%M"), "watching email", flush=True)
    while True:
        try:
            from .context import feed_stamp
            if time.time() - loaded > 3600 or feed_stamp(args.data) > loaded:  # the hourly feed, without a restart
                ed.alliant, loaded = Alliant.from_dir(args.data), time.time()
            n = ed.run_once(first)
            first = False
            if n:
                print(time.strftime("%H:%M"), f"{n} email(s)", flush=True)
        except Exception as e:  # keep going through a Bird, Slack or API hiccup
            print(time.strftime("%H:%M"), "error:", e, flush=True)
        time.sleep(args.every)


if __name__ == "__main__":
    main()

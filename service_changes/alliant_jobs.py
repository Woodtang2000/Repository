"""Donna enters changes in Alliant herself, from her workstation (the Mac mini, Alliant in Chrome through Azure
Virtual Desktop, run by Claude Code with the Claude in Chrome extension: see deploy/donna_workstation.md).

Each open ticket in the office channel gets a "🤖 Donna, enter it" button. Office staff tap it; the ticket joins her
queue (alliant_jobs.json). The workstation asks for the next job (webchat /api/donna/jobs/next, with
DONNA_WORKER_TOKEN), enters it in Alliant, and reports back (/api/donna/jobs/<id>/result):
  entered       the ticket turns green, "Entered · Donna", and the driver / customer gets the usual readback
  needs_person  the ticket goes back to gold with her reason, for the office to do by hand
The next morning's verify pass checks her entries against the new Alliant export like anyone else's.

AUTO (DONNA_AUTO=on, later): simple quantity changes on items the customer already has are queued without a tap.
Owner can pause her from his private messages with the bot: "Donna pause" / "Donna resume".
"""
import json
import os
import re
import threading
import time
from datetime import datetime

from .context import ALASKA
from .todo import GOLD, GREY, body, restyle, stamp

FILE = "alliant_jobs.json"
ACTION = "donna_enter"
# Her rules give her one phrase for it ("Alliant needs signing in"); a note that merely mentions a session isn't one.
SIGNED_OUT = re.compile(r"alliant needs signing in|alliant isn't open|alliant isn't available|no available resources|needs? (?:to )?sign(?:ing)? in|sign-?in (?:page|screen|prompt)|"
                        r"password (?:page|screen|prompt)|pick an account|signed (?:me |her )?out", re.I)
STALE_MINUTES = 30  # a job "working" longer than this goes back in the queue (the workstation lost it)


EMOJI = {"🧾": "", "➕": "Add:", "➖": "Decrease:", "🔢": "Set:", "⛔": "Stop:", "🔄": "Restart:", "📏": "Size:", "✏️": "Change:",
         "⚠️": "WARNING:", "❓": "QUESTION:", "⏸️": "ON HOLD:", "☑️": "", "📝": "Note:", "•": "-", "·": "|", "→": "->"}


def plain_ticket(text: str) -> str:
    """A ticket as plain text: no Slack bold/italic/code marks, links or emoji; quotes kept."""
    for k, v in EMOJI.items():
        text = text.replace(k, v)
    text = re.sub(r"<(?:https?|mailto)[^|>]*\|([^>]*)>", r"\1", text)
    text = re.sub(r":[a-z0-9_+-]+:", "", text)
    text = re.sub(r"[*_`]", "", text).replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    text = re.sub(r"[^\x09\x0a\x20-\x7e]", "", text)
    return "\n".join(re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.splitlines())


def button(ts: str) -> dict:
    return {"type": "actions", "block_id": "donna", "elements": [
        {"type": "button", "action_id": ACTION, "value": ts, "text": {"type": "plain_text", "text": "🤖 Donna, enter it"}}]}


class Jobs:
    def __init__(self, desk):
        self.desk = desk
        self.lock = threading.Lock()
        self.path = os.path.join(desk.data_dir, FILE)

    # ---- the queue file ------------------------------------------------------------------------------------------

    def load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"jobs": {}, "paused": False}

    def save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    # ---- the office side -----------------------------------------------------------------------------------------

    def offer(self, tickets: list[dict], done: set[str]) -> int:
        """Put the button on open tickets that don't have it yet (once each)."""
        from .desk import KIND, meta
        n = 0
        for t in tickets:
            p = meta(t) or {}
            if t["ts"] in done or p.get("donna") or not t.get("attachments") or p.get("kind") != "ticket":
                continue
            a = dict(t["attachments"][0])
            a["blocks"] = [b for b in a.get("blocks") or [] if b.get("block_id") != "donna"] + [button(t["ts"])]
            self.desk.slack.chat_update(channel=self.desk.desk_id, ts=t["ts"], text="", attachments=[a],
                                        metadata={"event_type": KIND, "event_payload": {**p, "donna": "offered"}})
            n += 1
        return n

    def queue(self, payload: dict) -> str:
        """The button: office staff only. The ticket goes in her queue and says so."""
        from .desk import KIND, meta
        user = payload.get("user", {}).get("id")
        ts = next((a.get("value") for a in payload.get("actions", []) if a.get("action_id") == ACTION), None)
        allowed = self.desk.is_office(user) or getattr(self.desk, "is_owner", lambda u: False)(user)
        if not ts or not allowed:
            if ts and user:  # say why, so a tap never just vanishes
                self.desk.slack.chat_postEphemeral(channel=self.desk.desk_id, user=user,
                                                   text="Only the office can send tickets to Donna for Alliant.")
            return f"ignored: {self.desk.name(user)} isn't on the office list"
        out = self.add(ts, user)
        c = payload.get("container") or {}
        if out.startswith("queued") and c.get("message_ts") and c["message_ts"] != ts:  # tapped on the ⚠️ alert
            who = self.desk.name(user)
            try:  # the alert's button goes away, so it's clear the tap took
                text = (payload.get("message") or {}).get("text") or "⚠️ Alliant signed Donna out"
                self.desk.slack.chat_update(channel=c.get("channel_id"), ts=c["message_ts"], blocks=[], text=text +
                                            f"\n🤖 Re-sent to Donna by {who.split()[0] if who else 'office'}")
            except Exception as e:
                print(time.strftime("%H:%M"), "alert not updated:", e, flush=True)
        return out

    def add(self, ts: str, user: str) -> str:
        """Put the ticket at `ts` in her queue for `user` (the button, or Scott telling her a change himself)."""
        from .desk import KIND, meta
        r = self.desk.slack.conversations_history(channel=self.desk.desk_id, latest=ts, inclusive=True, limit=1,
                                                   include_all_metadata=True)
        t = next((m for m in r.get("messages", []) if m["ts"] == ts), None)
        p = meta(t) if t else None
        if not p or p.get("kind") != "ticket":
            return "not an open ticket"
        who = self.desk.name(user)
        with self.lock:
            data = self.load()
            data["jobs"][ts] = {"id": ts, "account": p.get("account", ""), "ticket": body(t), "expect": p.get("expect", []),
                                "asked_by": who, "status": "queued", "at": time.time()}
            self.save(data)
        status = f"🤖 Queued for Donna · asked by {who.split()[0] if who else 'office'}" + (
            " · ⏸ she's paused" if data.get("paused") else "")
        self.desk.slack.chat_update(channel=self.desk.desk_id, ts=ts, text="", attachments=restyle(t, GOLD, status=status),
                                    metadata={"event_type": KIND, "event_payload": {**p, "donna": "queued"}})
        return f"queued {ts}"

    def custom_job(self, kind: str, text: str, extra: dict) -> str:
        """A job another part of Donna follows up herself (new_accounts.py: "new_account", "new_wearers"): queued like
        a ticket, and its result goes back to that part, not to #service-desk."""
        jid = f"{time.time():.6f}"
        with self.lock:
            data = self.load()
            data["jobs"][jid] = {"id": jid, "kind": kind, "account": extra.get("account", ""), "ticket": text[:12000],
                                 "expect": [], "asked_by": "Sales Handoff", "status": "queued", "at": time.time(), **extra}
            self.save(data)
        return jid

    def screen_task(self, text: str, user: str, reply_to: str) -> str:
        """Scott or Bill's own instructions for work on Alliant's screens that isn't a customer ticket (the SKU center,
        setup screens). Queued as written; her report goes back only to the private conversation it came from, so it
        may carry the values they asked for."""
        jid = f"{time.time():.6f}"
        who = self.desk.name(user) or "Scott"
        with self.lock:
            data = self.load()
            data["jobs"][jid] = {"id": jid, "kind": "screen_task", "account": "", "ticket": text.strip()[:6000],
                                 "expect": [], "asked_by": who, "asked_by_id": user, "reply_to": reply_to,
                                 "status": "queued", "at": time.time()}
            self.save(data)
        return jid

    def retry_waiting(self, user: str, days: int = 3) -> str:
        """"Donna retry": every ticket she sent back because Alliant wasn't signed in or available (the last few
        days), back in her queue in one go, instead of a tap on each."""
        with self.lock:
            data = self.load()
            ids = [k for k, j in data["jobs"].items() if j["status"] == "needs_person" and SIGNED_OUT.search(j.get("note", ""))
                   and time.time() - j.get("finished", 0) < days * 86400]
        n = sum(1 for ts in ids if self.add(ts, user).startswith("queued"))
        if not n:
            return "Nothing is waiting on Alliant."
        return f"Sent {n} ticket{'s' if n != 1 else ''} back to my queue." + (
            " (I'm paused: say \"Donna resume\" when you're ready.)" if self.load().get("paused") else "")

    def pause(self, on: bool) -> str:
        with self.lock:
            data = self.load()
            data["paused"] = on
            self.save(data)
        return "Donna is paused: she won't take new Alliant jobs until you say \"Donna resume\"." if on else \
            "Donna is back on Alliant jobs."

    # ---- the workstation side ------------------------------------------------------------------------------------

    def next(self) -> dict | None:
        """The oldest queued job, marked working; None if there's nothing (or she's paused)."""
        with self.lock:
            data = self.load()
            now = time.time()
            data["seen_at"] = now  # her workstation checked in (watchdog), paused or not
            if data.get("paused"):
                self.save(data)
                return None
            for j in data["jobs"].values():
                if j["status"] == "working" and now - j.get("started", now) > STALE_MINUTES * 60:
                    j["status"] = "queued"
            job = min((j for j in data["jobs"].values() if j["status"] == "queued"), key=lambda j: j["at"], default=None)
            if job:
                job.update(status="working", started=now)
            self.save(data)
            return job

    def result(self, job_id: str, status: str, note: str, readback: str = "", question: str = "") -> str:
        """She's finished a job: entered (readback goes out, as if the office ✅'d it) or needs_person (back to the
        office with her reason). Her note goes in the ticket's thread, never to the customer."""
        from .desk import KIND, meta
        with self.lock:
            data = self.load()
            job = data["jobs"].get(job_id)
            if not job or job["status"] != "working":
                return "unknown job"
            job.update(status=status if status in ("entered", "needs_person", "ask") else "needs_person",
                       note=note[:3800 if job.get("kind") in ("screen_task", "new_account", "new_wearers") else 1500], finished=time.time())
            if job["status"] == "ask" and not question.strip():
                job["status"] = "needs_person"
            data["jobs"] = {k: v for k, v in data["jobs"].items() if v["status"] in ("queued", "working")
                            or time.time() - v.get("finished", 0) < 14 * 86400}
            self.save(data)
        if job.get("kind") == "service_code":  # #uniform-requests (uniform.py)
            return self.desk.uniform().job_done(job, question)
        if job.get("kind") in ("new_account", "new_wearers", "garment_fix"):  # #new-accounts / #stockroom (new_accounts.py)
            return self.desk.new_accounts().job_done(job, question)
        if job.get("kind") == "screen_task":  # her report, privately, to whoever asked (never #service-desk)
            head = {"entered": "✅ Done in Alliant", "ask": "❓ A question before I go on",
                    "needs_person": "🛑 Stopped, nothing more changed"}[job["status"]]
            text = f"🤖 *{head}* (your Alliant task from {datetime.fromtimestamp(job['at'], ALASKA):%-m/%-d %-I:%M %p})\n" + \
                (question.strip() + "\n" if job["status"] == "ask" and question.strip() else "") + (note or "")
            self.desk.slack.chat_postMessage(channel=job["reply_to"], text=text[:3900], unfurl_links=False)
            return f"screen task {job['status']}"
        r = self.desk.slack.conversations_history(channel=self.desk.desk_id, latest=job_id, inclusive=True, limit=1,
                                                   include_all_metadata=True)
        t = next((m for m in r.get("messages", []) if m["ts"] == job_id), None)
        if t is None or (meta(t) or {}).get("kind") != "ticket":
            return "ticket gone or already done"
        if note:
            self.desk.slack.chat_postMessage(channel=self.desk.desk_id, thread_ts=job_id, text=f"🤖 Donna: {note}"[:3000],
                                             unfurl_links=False)
        if job["status"] == "entered" and "Special invoice (S)" in body(t) and not job.get("pdf") and re.search(
                r"couldn'?t|could not|not there|won'?t send|didn'?t send|wasn'?t sent|not sent|no pdf|no one.?time", note, re.I):
            # the PDF normally comes by Alliant's one-time email (email_desk.special_pdf); only when she says it didn't
            to = os.environ.get("SPECIALS_TO", "U5WUV01JA")
            try:  # the special is in Alliant but its PDF didn't come through: Kirk still needs to know
                self.desk.slack.chat_postMessage(channel=to, text=f"🧾 Donna made a special invoice for *{meta(t).get('account', '')}*"
                                                 f" but couldn't send its PDF. Her note: {note[:400]}\nPlease print it from "
                                                 "Alliant (Delivery → Invoices / Delivery Tickets).")
            except Exception as e:
                print(time.strftime("%H:%M"), "specials: Kirk not told:", e, flush=True)
        if job["status"] == "entered" and "Special invoice (S)" in body(t):
            number = re.search(r"\b(S\d{4,})\b", note)
            if number:  # the customer gets the delivery ticket PDF with Donna's note (specials.py), once both are in
                try:
                    from . import specials
                    specials.save_job(self.desk.data_dir, number.group(1), (meta(t) or {}).get("account", ""),
                                      readback or (meta(t) or {}).get("readback", ""))
                    print(time.strftime("%H:%M"), "specials: customer email:",
                          specials.try_send(self.desk.data_dir, self.desk.alliant, number.group(1)) or "waiting for the PDF",
                          flush=True)
                except Exception as e:
                    print(time.strftime("%H:%M"), "specials: customer email not sent:", e, flush=True)
            try:
                self.tell_driver(t, job.get("pdf") or note)
            except Exception as e:
                print(time.strftime("%H:%M"), "specials: driver not told:", e, flush=True)
        if job["status"] == "entered":
            if readback.strip():  # what she actually did on Alliant's screen beats the ticket's (last night's data)
                p = dict(meta(t))
                name = next((c.name for c in getattr(self.desk.alliant, "customers", []) if c.account == p.get("account")), "")
                p["readback"] = f"✅ {p.get('account', '')} {name} – {readback.strip()[:300]}"
                t = {**t, "metadata": {"event_type": KIND, "event_payload": p}}
            self.desk.send_readback(t, self.desk.me, [])
            return "entered"
        p = meta(t)
        if job["status"] == "ask":  # unclear what they meant: ask them, the same way she asks a driver
            who = self.desk.ask_requester(t, question)
            if who:
                self.desk.slack.chat_update(channel=self.desk.desk_id, ts=job_id, text="",
                                            attachments=restyle(t, GREY, status=f"❓ Donna asked {who if who not in ('driver', 'customer', 'owner') else 'the ' + who}: {question.strip()[:200]} "
                                                                f"· their answer makes a new ticket"),
                                            metadata={"event_type": KIND, "event_payload": {**p, "kind": "asked", "donna": "asked"}})
                return f"asked the {who}"
        if SIGNED_OUT.search(note):
            # Only Alliant's sign-in stopped her: the button comes back, so one tap re-sends it once she's signed in
            a = restyle(t, GOLD, status="🔒 Waiting for Alliant sign-in · tap below once she's signed back in")[0]
            a["blocks"] = [b for b in a.get("blocks") or [] if b.get("block_id") != "donna"] + [button(job_id)]
            self.desk.slack.chat_update(channel=self.desk.desk_id, ts=job_id, text="", attachments=[a],
                                        metadata={"event_type": KIND, "event_payload": {**p, "donna": "offered"}})
            self.signed_out(job_id)
            return "back to the office (signed out)"
        self.desk.slack.chat_update(channel=self.desk.desk_id, ts=job_id, text="",
                                    attachments=restyle(t, GOLD, status="🙋 Donna needs a person for this one (see thread)"),
                                    metadata={"event_type": KIND, "event_payload": {**p, "donna": "needs_person"}})
        return "back to the office"

    def attach(self, job_id: str, data: bytes, name: str) -> str:
        """The PDF of a special invoice she just made goes to whoever pulls the routes (SPECIALS_TO, Kirk Kosiek), who
        sends it on to the customer; a note in the ticket's thread says so. Only a PDF, only for a job she's on."""
        from .desk import meta
        with self.lock:
            data_ = self.load()
            job = data_["jobs"].get(job_id)
            if not job or job["status"] != "working":
                return "unknown job"
        if not data.startswith(b"%PDF"):
            return "not a PDF"
        r = self.desk.slack.conversations_history(channel=self.desk.desk_id, latest=job_id, inclusive=True, limit=1,
                                                   include_all_metadata=True)
        t = next((m for m in r.get("messages", []) if m["ts"] == job_id), None)
        p = (meta(t) or {}) if t else {}
        acct = p.get("account", "")
        name_ = next((c.name for c in getattr(self.desk.alliant, "customers", []) if c.account == acct), "")
        to = os.environ.get("SPECIALS_TO", "U5WUV01JA")
        text = (f"🧾 *Special invoice {name.rsplit('.', 1)[0]}* for *{acct} {name_}*, made by Donna"
                f" (asked by {job.get('asked_by') or 'the office'}). It's on the route for its delivery date; "
                "here's the PDF to send to the customer.")
        first = self.desk.slack.chat_postMessage(channel=to, text=text)
        self.desk.slack.files_upload_v2(channel=first.get("channel") or to, thread_ts=first.get("ts"), content=data,
                                        filename=name, title=f"Special invoice {acct} {name_}".strip())
        with self.lock:
            data_ = self.load()
            if job_id in data_["jobs"]:
                data_["jobs"][job_id]["pdf"] = name
                self.save(data_)
        self.desk.slack.chat_postMessage(channel=self.desk.desk_id, thread_ts=job_id,
                                         text=f"🧾 The invoice PDF ({name}) went to <@{to}>.")
        return f"pdf sent to {to}"

    def tell_driver(self, t: dict, said: str) -> bool:
        """A special she made: the route's driver hears what's coming and when, in their route channel, so they and
        Kirk (who pulls it) both know. Branch 1 routes have channels (route-N)."""
        from .desk import meta
        acct = (meta(t) or {}).get("account", "")
        cust = next((c for c in getattr(self.desk.alliant, "customers", []) if c.account == acct), None)
        channels = getattr(self.desk, "channels", {}) or {}
        where = channels.get(f"route-{cust.route}") if cust and acct.split("-")[1:2] == ["1"] else None
        if not where:
            return False
        text = body(t)
        day = re.search(r"delivery ([^·\n]+?)\s*(?:·|\n|$)", text)
        lines = [re.sub(r"[*`]", "", m.group(1)).strip() for m in re.finditer(r"^•\s*(.+)$", text, re.M)]
        number = re.search(r"\b(S\d{2,})\b", said or "")
        r = self.desk.slack.chat_postMessage(
            channel=where, unfurl_links=False,
            text=f"🧾 *Special for {cust.name}* ({acct})" + (f", invoice {number.group(1)}" if number else "")
                 + f": going out *{day.group(1).strip() if day else 'on its delivery date'}*.\n"
                 + "\n".join(f"• {ln}" for ln in lines) + "\nKirk is pulling it. The delivery ticket PDF follows here.")
        if number:  # the delivery ticket PDF goes in this thread, now or when Alliant's email brings it (specials.py)
            from . import specials
            specials.set_driver_post(self.desk.data_dir, number.group(1), (r or {}).get("channel") or where,
                                     (r or {}).get("ts", ""))
            specials.share_with_driver(self.desk.data_dir, self.desk.slack, number.group(1))
        return True

    def alert(self, note: str) -> str:
        """Donna, between jobs, found Alliant signed out (or a disconnect Reconnect didn't fix): tell people now."""
        self.signed_out(note=note[:300])
        return "alerted"

    def watchdog(self, now: float | None = None) -> str:
        """Her workstation checks in every few minutes (jobs/next), around the clock. Silent for WATCHDOG_MINUTES (20):
        the Mac is off or asleep, Chrome crashed, or her session died. Tell Scott, at most once an hour."""
        now = now or time.time()
        minutes = int(os.environ.get("WATCHDOG_MINUTES", "20"))
        with self.lock:
            data = self.load()
            seen = data.get("seen_at")
            if not seen or now - seen < minutes * 60 or now - data.get("watch_alerted", 0) < 3600:
                return ""
            data["watch_alerted"] = now
            self.save(data)
        from .context import ALASKA
        when = datetime.fromtimestamp(seen, ALASKA)
        text = f"⚠️ Donna's workstation isn't responding (nothing since {when:%-I:%M %p}) – please check it."
        owner = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
        for where in (owner, self.desk.desk_id):
            try:
                self.desk.slack.chat_postMessage(channel=where, text=text)
            except Exception as e:
                print(time.strftime("%H:%M"), "watchdog: not posted:", e, flush=True)
        return "alerted"

    def signed_out(self, job_id: str = "", note: str = "") -> None:
        """Alliant signed her out: say so where people will see it (the office channel, and Scott directly), at most once
        an hour; a ticket thread alone is easy to miss. She clicks Reconnect and a filled-in Sign in herself; this is
        when that wasn't enough (an empty field, a code or phone approval, another account)."""
        if time.time() - getattr(self, "warned", 0) < 3600:
            return
        self.warned = time.time()
        text = "⚠️ Donna has been logged out of Alliant – please reconnect."
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}] + ([button(job_id)] if job_id else [])
        self.desk.slack.chat_postMessage(channel=self.desk.desk_id, text=text, blocks=blocks)
        owner = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
        try:
            self.desk.slack.chat_postMessage(channel=owner, text=text, blocks=blocks)
        except Exception:
            pass

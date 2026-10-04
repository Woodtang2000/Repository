"""Service Desk workflow: driver -> clarifying questions -> clean office ticket -> office ✅ -> readback to driver.

  python -m service_changes.desk --data service_changes/data --desk service-desk-test --routes route-12-test --watch 30

Everything happens in the main channels; nobody has to use threads (thread replies still work).

1. A driver posts a change in a route channel. The bot reacts 👀 so the driver knows it was picked up.
2. If something needed to enter the change is unclear, the bot asks in the channel, @mentioning the driver so it
   pushes to their phone (at most twice). The driver just answers in the channel. Their next message is checked by
   Claude: an answer is read together with the request; anything else is treated as a new request.
3. Once the request is clear (or still unclear after two tries) a ticket goes to the office channel (--desk):
   account number and name, then one line per change. Unclear tickets say to call the driver.
4. Office staff enter it in Alliant, then react ✅ on the ticket or type "done" in the office channel. With more
   than one ticket open, "done" needs the account number ("4000-1-01606 done") or the bot asks which. Words after
   "done" go to the driver as an office note.
5. The bot posts the readback in the route channel, @mentioning the driver, and marks the ticket
   "✅ Entered by <name> · readback sent".
6. If the driver answers the readback with a correction ("no, I meant 3") it comes back as a 🔁 correction ticket.
   A change before the office finished marks the open ticket "🚫 Replaced".

All state lives in Slack: 👀 marks a message as read, and the bot's own posts carry message metadata (kind =
question / ticket / readback / done / replaced). A pass can be rerun without double-posting, and --watch repeats
it every N seconds.

Posting in the real #route-N channels needs SERVICE_DESK_LIVE=1, the same lock as bot.py --live. Channels with
anything after the number (#route-12-test) are test channels and need no lock.
"""
import argparse
import os
import re
import sys
import time

from .bot import _messages, _name, _office_staff
from .checks import check, readback, ticket
from .context import Alliant, route_from_channel, service_day
from .schema import Category

KIND = "service_desk"
SEEN = "eyes"
WORKING = "hourglass_flowing_sand"  # put on by the listener the moment a message arrives, swapped for 👀 when read
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
DONE_WORDS = re.compile(r"^\s*(done|changed|entered|complete[d]?|made|updated|ok(ay)?|got it|all set|finished)\b[\s.!]*$", re.I)
MAX_ASKS = 2
MAX_LOOKUPS_PER_DAY = 15  # per person: past this the bot stops answering and tells the office
ACTIONABLE = {Category.item_change, Category.wearer_change, Category.hold_or_closure, Category.special_order}
DONE_ANY = re.compile(r"\b(done|entered|changed|complete[d]?|all set|finished)\b", re.I)
ACCOUNT = re.compile(r"\b\d{4}-\d-\d{5}\b")
LIVE_ROUTE = re.compile(r"route-\d+")


def meta(m: dict) -> dict | None:
    md = m.get("metadata") or {}
    return md.get("event_payload") if md.get("event_type") == KIND else None


def _post(slack, channel: str, text: str, payload: dict, thread_ts: str | None = None, **kw) -> dict:
    return slack.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts, unfurl_links=False,
                                  metadata={"event_type": KIND, "event_payload": payload}, **kw)


def _is_bot(m: dict, me: str) -> bool:
    return bool(m.get("bot_id")) or m.get("user") == me


def _seen(m: dict, me: str) -> bool:
    return any(r.get("name") == SEEN and me in r.get("users", []) for r in m.get("reactions", []))


def _short(text: str, n: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _quote(text: str) -> str:
    return "\n".join("> " + ln for ln in text.splitlines()) or ">"


class Desk:
    def __init__(self, slack, claude, alliant: Alliant, desk: str, routes: list[str]):
        self.slack, self.claude, self.alliant = slack, claude, alliant
        self.me = slack.auth_test()["user_id"]
        self.names: dict[str, str] = {}
        self.office = _office_staff()
        self.replies_seen: dict[tuple[str, str], str] = {}  # (channel, ts) -> latest_reply already looked at
        self.links: dict[tuple[str, str], set[str]] = {}  # (channel, request ts) -> main-channel replies read with it
        self.listening = False
        channels = {}
        cursor = None
        while True:
            r = slack.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=500, cursor=cursor)
            channels.update({c["name"]: c["id"] for c in r["channels"] if c.get("is_member")})
            cursor = r.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        missing = [n for n in [desk, *routes] if n not in channels]
        if missing:
            sys.exit("Invite @Service Desk to " + ", ".join("#" + n for n in missing) + " first.")
        self.desk_id = channels[desk]
        self.routes = {n: channels[n] for n in routes}

    def name(self, user: str | None) -> str:
        return _name(self.slack, self.names, user)

    def is_office(self, user: str | None) -> bool:
        return self.name(user).lower() in self.office

    def thread(self, channel: str, m: dict) -> list[dict]:
        if not m.get("reply_count"):
            return [m]
        return self.slack.conversations_replies(channel=channel, ts=m["ts"], include_all_metadata=True, limit=200)["messages"]

    def post(self, channel: str, text: str, payload: dict, posted: list | None = None, **kw) -> dict:
        r = _post(self.slack, channel, text, payload, **kw)
        m = {"ts": r["ts"], "user": self.me, "bot_id": "self", "text": text,
             "metadata": {"event_type": KIND, "event_payload": payload}}
        if posted is not None:
            posted.append(m)
        if channel == self.desk_id and hasattr(self, "desk_msgs"):
            self.desk_msgs[m["ts"]] = m
        return m

    def mark_seen(self, channel: str, msgs: list[dict]) -> None:
        for t in msgs:
            try:
                self.slack.reactions_add(channel=channel, timestamp=t["ts"], name=SEEN)
            except Exception as e:  # already_reacted on a rerun is fine
                if "already_reacted" not in str(e):
                    raise
            if any(r.get("name") == WORKING for r in t.get("reactions", [])) or self.listening:
                try:
                    self.slack.reactions_remove(channel=channel, timestamp=t["ts"], name=WORKING)
                except Exception:  # no_reaction: it was never put on
                    pass

    # ---- driver side ----------------------------------------------------------------------------------------

    def conversation(self, convo: list[dict]) -> str:
        """Request, questions, answers, readback and replies as one message for the parser."""
        lines = []
        for i, m in enumerate(convo):
            text = m.get("text", "")
            if _is_bot(m, self.me):
                kind = (meta(m) or {}).get("kind")
                if kind == "question":
                    lines.append(f"Service Desk asked: {text}")
                elif kind == "readback":
                    lines.append(f"Office entered it and confirmed: {text}")
                elif kind == "answer":
                    lines.append(f"Service Desk answered: {text}")
            elif i == 0:
                lines.append(f"{self.name(m.get('user'))} (original request): {text}")
            else:
                lines.append(f"{self.name(m.get('user'))}{' (office)' if self.is_office(m.get('user')) else ''}: {text}")
        return "\n".join(lines)

    def convo(self, channel: str, src: dict, top: list[dict], bot_posts: list[dict], extra: list[dict]) -> list[dict]:
        """Everything that belongs to one request: its thread, the bot's main-channel questions and readbacks
        about it, and the driver's main-channel answers, oldest first."""
        mine = [b for b in bot_posts if (meta(b) or {}).get("src_ts") == src["ts"]]
        linked = set(self.links.get((channel, src["ts"]), set()))
        for b in mine:
            linked.update(meta(b).get("msgs", []))
        items = {m["ts"]: m for m in self.thread(channel, src) + mine + [m for m in top if m["ts"] in linked] + extra}
        return sorted(items.values(), key=lambda m: float(m["ts"]))

    def addressed_to(self, bot_posts: list[dict], m: dict) -> dict | None:
        """The bot's latest question or readback to this person before their message, if recent."""
        mine = [b for b in bot_posts if (meta(b) or {}).get("driver_id") == m.get("user")
                and meta(b).get("kind") in ("question", "readback", "answer") and float(b["ts"]) < float(m["ts"])]
        b = max(mine, key=lambda b: float(b["ts"]), default=None)
        return b if b and float(m["ts"]) - float(b["ts"]) < 86400 else None

    def handle(self, route_name: str, channel: str, src: dict, convo: list[dict], todo: list[dict],
               open_tickets: dict, bot_posts: list[dict]) -> str:
        """Read one request as it stands and ask, ticket, or just mark it read."""
        last_readback = max((i for i, t in enumerate(convo) if (meta(t) or {}).get("kind") == "readback"), default=-1)
        asks = sum(1 for t in convo[last_readback + 1:] if (meta(t) or {}).get("kind") == "question")
        text = src.get("text", "") if len(convo) == 1 else self.conversation(convo)
        author = self.name(src.get("user"))
        replies = [t["ts"] for t in convo if t is not src and not _is_bot(t, self.me) and t.get("thread_ts") != src["ts"]]
        self.links.setdefault((channel, src["ts"]), set()).update(replies)

        from .parser import parse_message
        from .run import fill_item_matches, fix_department
        parsed = parse_message(self.claude, text, route_name, src["ts"], self.alliant, author=author,
                               office=self.is_office(src.get("user")))
        if parsed.category != Category.not_a_request:
            fix_department(parsed, self.alliant, route_from_channel(route_name), service_day(src["ts"]))
            fill_item_matches(self.claude, parsed, self.alliant)
        result = check(parsed, self.alliant)

        did = "read"
        if parsed.category == Category.not_a_request:
            pass
        elif parsed.category == Category.lookup and parsed.account_number and not parsed.questions_for_driver:
            did = self.answer(route_name, channel, src, author, parsed, replies, bot_posts)
        elif result.questions and asks < MAX_ASKS and (parsed.category in ACTIONABLE or parsed.category == Category.lookup):
            qs = "\n".join(f"• {q}" for q in result.questions)
            # A normal channel message with an @mention, so it pushes to the driver's phone.
            self.post(channel, f"<@{src.get('user')}> Quick check on _{_short(src.get('text', ''), 60)}_\n{qs}",
                      {"kind": "question", "src_ts": src["ts"], "driver_id": src.get("user"), "msgs": replies}, bot_posts)
            did = "asked"
        else:
            key = (channel, src["ts"])
            open_tickets[key] = [self.post_ticket(route_name, channel, src, author, parsed, result, replies,
                                                  correction=last_readback >= 0, replaces=open_tickets.get(key, []))]
            did = "ticket"
        self.mark_seen(channel, todo)
        return did

    def answer(self, route_name, channel, src, author, parsed, replies, bot_posts) -> str:
        """A driver's question about an account, answered from the Alliant export. Every answer is logged in the
        office channel; if the data doesn't say, the office gets a ticket instead; past MAX_LOOKUPS_PER_DAY for one
        person the bot stops answering and flags it."""
        from .context import ALASKA, account_facts
        from .parser import answer_lookup
        from datetime import datetime
        acct = parsed.account_number
        name = next((c.name for c in self.alliant.customers if c.account == acct), parsed.customer_as_written or acct)
        link = self.slack.chat_getPermalink(channel=channel, message_ts=src["ts"])["permalink"]
        midnight = datetime.now(ALASKA).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = sum(1 for b in bot_posts if (meta(b) or {}).get("kind") == "answer"
                    and meta(b).get("driver_id") == src.get("user") and float(b["ts"]) >= midnight)
        cust = next((c for c in self.alliant.customers if c.account == acct), None)
        here = route_from_channel(route_name)
        if cust and here and cust.route != here and not self.is_office(src.get("user")):
            # Only customers on this channel's route are answered; anything else goes to the office.
            self.post(channel, f"<@{src.get('user')}> That customer isn't on route {here}, so I've passed this to the office.",
                      {"kind": "answer", "src_ts": src["ts"], "driver_id": src.get("user"), "msgs": replies, "held": True},
                      bot_posts)
            self.post(self.desk_id, f"⚠️ *{author}* in #{route_name} asked about *{acct}* {name}, which is on route "
                                    f"{cust.route}. I didn't answer. _{_short(src.get('text', ''), 120)}_ "
                                    f"(<{link}|#{route_name}>)", {"kind": "lookup_off_route", "driver_id": src.get("user")})
            return "answered"
        if today >= MAX_LOOKUPS_PER_DAY:
            # Lots of lookups in one day: stop answering and let the office decide.
            self.post(channel, f"<@{src.get('user')}> I've passed this one to the office.",
                      {"kind": "answer", "src_ts": src["ts"], "driver_id": src.get("user"), "msgs": replies, "held": True},
                      bot_posts)
            self.post(self.desk_id, f"⚠️ *{author}* has asked about accounts {today} times today in #{route_name}; "
                                    f"I've stopped answering. Latest: _{_short(src.get('text', ''), 120)}_ "
                                    f"(<{link}|#{route_name}>)", {"kind": "lookup_limit", "driver_id": src.get("user")})
            return "answered"
        ans = answer_lookup(self.claude, parsed.summary or src.get("text", ""), account_facts(self.alliant, acct))
        as_of = f" _(Alliant as of {self.alliant.as_of})_" if self.alliant.as_of else ""
        text = f"<@{src.get('user')}> *{name}*: {ans.answer}{as_of}"
        if ans.found:
            # The office sees every answer the bot gives out: who asked, about which account, and what they got.
            self.post(self.desk_id, f"🔎 *{author}* asked about *{acct}* {name} (<{link}|#{route_name}>): "
                                    f"_{_short(src.get('text', ''), 120)}_\n↳ {_short(ans.answer, 200)}",
                      {"kind": "lookup_log", "driver_id": src.get("user"), "account": acct})
        else:
            text += "\nI've asked the office."
            lines = [f"*{acct}*  {name}", f"❓ *Driver question I couldn't answer*: {parsed.summary}"]
            self.post(self.desk_id, "\n".join(lines + ["", _quote(_short(src.get("text", ""), 300)),
                                                     f"<{link}|#{route_name}> · Answer the driver there, then react ✅"]),
                      {"kind": "ticket", "src_channel": channel, "src_ts": src["ts"], "readback": "",
                       "driver": author, "driver_id": src.get("user"), "account": acct, "msgs": replies})
        self.post(channel, text, {"kind": "answer", "src_ts": src["ts"], "driver_id": src.get("user"), "msgs": replies},
                  bot_posts)
        return "answered"

    def post_ticket(self, route_name, channel, src, author, parsed, result, replies, correction, replaces) -> str:
        link = self.slack.chat_getPermalink(channel=channel, message_ts=src["ts"])["permalink"]
        if parsed.category in ACTIONABLE:
            lines = ticket(result, self.alliant, with_readback=False).splitlines()
            rb = readback(result) if not result.questions else ""
            if result.questions:
                lines.append("⚠️ _Still unclear after asking the driver. Please call them._")
        else:
            lines = [f"📣 *FYI: {parsed.category.value.replace('_', ' ')}*", parsed.summary]
            rb = f"✅ Office has it: {parsed.summary}"
        if correction:
            lines.insert(1, "🔁 *Correction*: the driver says the last entry wasn't right")
        text = "\n".join(lines + ["", _quote(_short(src.get("text", ""), 300)),
                                   f"<{link}|#{route_name}> · React ✅ or type done when it's in Alliant"])
        new = self.post(self.desk_id, text, {"kind": "ticket", "src_channel": channel, "src_ts": src["ts"],
                                             "readback": rb, "driver": author, "driver_id": src.get("user"),
                                             "account": parsed.account_number or "", "msgs": replies})
        for old_ts in replaces:
            old = self.desk_msgs.get(old_ts)
            if old:
                self.slack.chat_update(channel=self.desk_id, ts=old_ts,
                                       text="🚫 *Replaced by a newer ticket below. Don't enter this one.*\n" + old["text"],
                                       metadata={"event_type": KIND, "event_payload": {**meta(old), "kind": "replaced"}})
        return new["ts"]

    def route_pass(self, name: str, cid: str, oldest: float, open_t: dict, counts: dict) -> None:
        top = list(reversed(list(_messages(self.slack, cid, oldest, include_all_metadata=True))))
        bot_posts = [m for m in top if _is_bot(m, self.me) and meta(m)]
        for m in top:
            if m.get("subtype") or _is_bot(m, self.me):
                continue
            key = (cid, m["ts"])
            if not _seen(m, self.me):
                # A main-channel message right after the bot asked or read back to this person may be the answer.
                b = self.addressed_to(bot_posts, m)
                src = next((x for x in top if b and x["ts"] == meta(b)["src_ts"]), None)
                from .parser import is_reply
                # A one- or two-word message right after a question ("1821", "6", "yes weekly") is the answer.
                short = meta(b or {}) and meta(b)["kind"] == "question" and len(m.get("text", "").split()) <= 2
                if src and (short or is_reply(self.claude, b.get("text", ""), src.get("text", ""), m.get("text", ""))):
                    did = self.handle(name, cid, src, self.convo(cid, src, top, bot_posts, [m]), [m], open_t, bot_posts)
                else:
                    thread = self.thread(cid, m)
                    todo = [m] + [t for t in thread[1:] if self._answers(t, m)]
                    did = self.handle(name, cid, m, self.convo(cid, m, top, bot_posts, []), todo, open_t, bot_posts)
                counts[did] += 1
            elif self.replies_seen.get(key) != m.get("latest_reply"):
                todo = [t for t in self.thread(cid, m)[1:] if self._answers(t, m)]
                if todo:  # the driver answered in the thread instead
                    counts[self.handle(name, cid, m, self.convo(cid, m, top, bot_posts, []), todo, open_t, bot_posts)] += 1
            self.replies_seen[key] = m.get("latest_reply")

    def _answers(self, t: dict, src: dict) -> bool:
        """A thread reply the bot should read: from whoever posted, or anyone not in the office, not yet 👀."""
        return (not _is_bot(t, self.me) and t.get("subtype") in (None, "thread_broadcast") and not _seen(t, self.me)
                and (t.get("user") == src.get("user") or not self.is_office(t.get("user"))))

    # ---- office side ----------------------------------------------------------------------------------------

    def desk_pass(self, oldest: float, counts: dict) -> dict[tuple[str, str], list[str]]:
        """Send readbacks for tickets marked done (✅, or "done" in the channel or the ticket's thread) and
        return the tickets still open, keyed by the driver message they came from."""
        top = list(reversed(list(_messages(self.slack, self.desk_id, oldest, include_all_metadata=True))))
        self.desk_msgs = {m["ts"]: m for m in top}
        tickets = [m for m in top if (meta(m) or {}).get("kind") == "ticket"]
        done: dict[str, tuple[str, list[str]]] = {}
        for t in tickets:
            who = next((u for r in t.get("reactions", []) if r.get("name") in DONE_REACTIONS
                        for u in r.get("users", []) if u != self.me), None)
            people = [r for r in self.thread(self.desk_id, t)[1:] if not _is_bot(r, self.me)]
            who = who or next((r.get("user") for r in people if DONE_WORDS.match(r.get("text", ""))), None)
            if who:
                done[t["ts"]] = (who, [r.get("text", "") for r in people if not DONE_WORDS.match(r.get("text", ""))])
        for h in top:  # "done" typed in the channel itself
            if _is_bot(h, self.me) or h.get("subtype") or _seen(h, self.me):
                continue
            text = h.get("text", "")
            if DONE_ANY.search(text):
                accts = ACCOUNT.findall(text)
                cands = [t for t in tickets if t["ts"] not in done and (not accts or meta(t).get("account") in accts)]
                if len(cands) == 1:
                    rest = DONE_ANY.sub("", ACCOUNT.sub("", text)).strip(" .,!-:")
                    done[cands[0]["ts"]] = (h.get("user"), [rest] if len(rest.split()) > 1 else [])
                elif cands:
                    self.post(self.desk_id, f"<@{h.get('user')}> Which one? React ✅ on the ticket, or type done "
                                            "with the account number.", {"kind": "which"})
            self.mark_seen(self.desk_id, [h])
        for t in tickets:
            if t["ts"] in done:
                self.send_readback(t, *done[t["ts"]])
                counts["readback"] += 1
        open_t: dict[tuple[str, str], list[str]] = {}
        for t in tickets:
            if t["ts"] not in done:
                open_t.setdefault((meta(t)["src_channel"], meta(t)["src_ts"]), []).append(t["ts"])
        return open_t

    def send_readback(self, t: dict, user: str, notes: list[str]) -> None:
        p = meta(t)
        who = self.name(user)
        msg = (f"<@{p['driver_id']}> " if p.get("driver_id") else "") + (p.get("readback") or "✅ Done") + f" – entered by {who}"
        msg += "".join(f"\nOffice note: {n}" for n in notes)
        self.post(p["src_channel"], msg, {"kind": "readback", "src_ts": p["src_ts"], "driver_id": p.get("driver_id"),
                                          "msgs": p.get("msgs", [])})
        self.slack.chat_update(channel=self.desk_id, ts=t["ts"], text=t["text"] + f"\n✅ *Entered by {who}* · readback sent",
                               metadata={"event_type": KIND, "event_payload": {**p, "kind": "done"}})

    # ---- one pass -------------------------------------------------------------------------------------------

    def run_once(self, since_minutes: int = 4320, ticket_days: int = 14, only: set[str] | None = None) -> dict:
        """One pass over the office channel and the route channels (or just the ones in `only`)."""
        now = time.time()
        counts = {"asked": 0, "ticket": 0, "read": 0, "readback": 0, "answered": 0}
        open_t = self.desk_pass(now - ticket_days * 86400, counts)
        for name, cid in sorted(self.routes.items()):
            if only is None or cid in only:
                self.route_pass(name, cid, now - since_minutes * 60, open_t, counts)
        return counts

    def listen(self, app_token: str, bot_token: str, since_minutes: int, backup_seconds: int, reload) -> None:
        """Real time: Slack pushes each event over Socket Mode. The listener puts ⏳ on a new message within a
        second and queues its channel; one worker runs passes so nothing is handled twice. A full pass every
        `backup_seconds` catches anything a dropped connection missed."""
        import queue
        import threading

        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse

        self.listening = True
        quick = WebClient(token=bot_token)  # the listener's own client, so it never waits on the worker
        todo: "queue.Queue[str | None]" = queue.Queue()
        watched = set(self.routes.values()) | {self.desk_id}

        def on_event(client, req):
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            if req.type != "events_api":
                return
            ev = req.payload.get("event", {})
            channel = ev.get("channel") or ev.get("item", {}).get("channel")
            if channel not in watched:
                return
            if ev.get("type") == "message" and not ev.get("bot_id") and ev.get("subtype") in (None, "thread_broadcast") \
                    and channel != self.desk_id and ev.get("user") != self.me \
                    and not (ev.get("thread_ts") not in (None, ev.get("ts")) and self.is_office(ev.get("user"))):
                try:
                    quick.reactions_add(channel=channel, timestamp=ev["ts"], name=WORKING)
                except Exception:
                    pass
            if ev.get("type") in ("message", "reaction_added") and ev.get("user") != self.me:
                todo.put(channel)

        sm = SocketModeClient(app_token=app_token, web_client=quick)
        sm.socket_mode_request_listeners.append(on_event)
        sm.connect()
        print(time.strftime("%H:%M"), "listening", flush=True)
        last_full = 0.0
        while True:
            try:
                channel = todo.get(timeout=5)
            except queue.Empty:
                channel = None
            batch = {channel} if channel else set()
            while not todo.empty():  # several events at once: one pass covers them
                batch.add(todo.get_nowait())
            full = time.time() - last_full > backup_seconds
            if not batch and not full:
                continue
            try:
                reload()
                counts = self.run_once(since_minutes, only=None if full else batch - {self.desk_id})
                if full:
                    last_full = time.time()
                if any(counts[k] for k in ("asked", "ticket", "readback", "answered")):
                    print(time.strftime("%H:%M"), ", ".join(f"{v} {k}" for k, v in counts.items()), flush=True)
            except Exception as e:  # keep listening through a Slack or API hiccup
                print(time.strftime("%H:%M"), "error:", e, flush=True)


def main():
    import anthropic
    from slack_sdk import WebClient

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    ap.add_argument("--desk", default="service-desk", help="office channel for tickets")
    ap.add_argument("--routes", required=True, help="comma-separated route channels, e.g. route-12-test or route-1,route-2")
    ap.add_argument("--since-minutes", type=int, default=4320, help="how far back to look for driver posts")
    ap.add_argument("--watch", type=int, default=0, help="repeat every N seconds (0 = one pass)")
    ap.add_argument("--listen", action="store_true",
                    help="real time over Slack Socket Mode (needs SLACK_APP_TOKEN); --watch sets the backup pass, default 300")
    args = ap.parse_args()

    routes = [r.strip().lstrip("#") for r in args.routes.split(",") if r.strip()]
    live = [r for r in routes if LIVE_ROUTE.fullmatch(r)]
    if live and os.environ.get("SERVICE_DESK_LIVE") != "1":
        sys.exit(f"{', '.join('#' + r for r in live)} are real route channels: that needs SERVICE_DESK_LIVE=1 "
                 "in the environment. Use test channels like #route-12-test until then.")
    if args.listen and not os.environ.get("SLACK_APP_TOKEN"):
        sys.exit("--listen needs SLACK_APP_TOKEN (the xapp- token from the Slack app's Basic Information page).")

    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SERVICE_DESK_API_KEY"))
    desk = Desk(slack, claude, Alliant.from_dir(args.data), args.desk, routes)
    loaded = [time.time()]

    def reload():  # pick up a fresh export without restarting
        if time.time() - loaded[0] > 3600:
            desk.alliant, loaded[0] = Alliant.from_dir(args.data), time.time()

    if args.listen:
        desk.listen(os.environ["SLACK_APP_TOKEN"], os.environ["SLACK_BOT_TOKEN"], args.since_minutes,
                    args.watch or 300, reload)
        return
    while True:
        try:
            reload()
            counts = desk.run_once(args.since_minutes)
            if any(counts[k] for k in ("asked", "ticket", "readback", "answered")) or not args.watch:
                print(time.strftime("%H:%M"), ", ".join(f"{v} {k}" for k, v in counts.items()), flush=True)
        except Exception as e:  # keep watching through a Slack or API hiccup
            if not args.watch:
                raise
            print(time.strftime("%H:%M"), "error:", e, flush=True)
        if not args.watch:
            return
        time.sleep(args.watch)


if __name__ == "__main__":
    main()

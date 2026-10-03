"""Service Desk workflow: driver -> clarifying questions -> clean office ticket -> office ✅ -> readback to driver.

  python -m service_changes.desk --data service_changes/data --desk service-desk-test --routes route-12-test --watch 60

1. A driver posts a change in a route channel. The bot reacts 👀 so the driver knows it was picked up.
2. If something needed to enter the change is unclear, the bot asks in the driver's thread (at most twice),
   @mentioning them so it pushes to their phone and also showing it in the channel.
   The driver's answer is read together with the original post.
3. Once the request is clear (or still unclear after two tries) a ticket goes to the office channel (--desk)
   with the account, item, SKU and counts. Unclear tickets say to call the driver.
4. Office staff enter it in Alliant, then react ✅ to the ticket or reply "done". Anything else they type in the
   ticket's thread goes to the driver as an office note.
5. The bot posts the readback in the driver's thread, @mentioning them, ("✅ Wendy's – 2 3x10 mats added – total now 6 – entered by
   Sonja") and notes in the ticket thread that it was sent.
6. If the driver replies after that ("no, I meant 3"), it goes back through steps 2-3 as a correction.

All state lives in Slack: 👀 marks a driver message as read, and the bot's own posts carry message metadata
(kind = question / ticket / readback / sent / replaced). A pass can be run any number of times without
double-posting, and --watch repeats it every N seconds so questions reach drivers while they are on the route.

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
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
DONE_WORDS = re.compile(r"^\s*(done|changed|entered|complete[d]?|made|updated|ok(ay)?|got it|all set|finished)\b[\s.!]*$", re.I)
MAX_ASKS = 2
ACTIONABLE = {Category.item_change, Category.wearer_change, Category.hold_or_closure, Category.special_order}
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

    # ---- driver side ----------------------------------------------------------------------------------------

    def conversation(self, thread: list[dict]) -> str:
        """The thread as one message for the parser: request, questions, answers, readback, reply."""
        lines = []
        for i, m in enumerate(thread):
            text = m.get("text", "")
            if _is_bot(m, self.me):
                kind = (meta(m) or {}).get("kind")
                if kind == "question":
                    lines.append(f"Service Desk asked: {text}")
                elif kind == "readback":
                    lines.append(f"Office entered it and confirmed: {text}")
            elif i == 0:
                lines.append(f"{self.name(m.get('user'))} (original request): {text}")
            else:
                lines.append(f"{self.name(m.get('user'))}{' (office)' if self.is_office(m.get('user')) else ''}: {text}")
        return "\n".join(lines)

    def handle_driver_message(self, route_name: str, channel: str, m: dict, open_tickets: dict) -> str | None:
        """Look at one top-level route message and its thread; act on anything new. Returns what it did."""
        thread = self.thread(channel, m)
        # New input = the top-level post, or a reply from whoever posted it or anyone not in the office, that the
        # bot hasn't marked 👀 yet. Other office replies in the thread are context only.
        todo = [t for i, t in enumerate(thread)
                if not _is_bot(t, self.me) and t.get("subtype") in (None, "thread_broadcast") and not _seen(t, self.me)
                and (i == 0 or t.get("user") == m.get("user") or not self.is_office(t.get("user")))]
        if not todo:
            return None
        trigger = todo[-1]
        last_readback = max((i for i, t in enumerate(thread) if (meta(t) or {}).get("kind") == "readback"), default=-1)
        asks = sum(1 for t in thread[last_readback + 1:] if (meta(t) or {}).get("kind") == "question")
        text = m.get("text", "") if len(thread) == 1 else self.conversation(thread)
        author = self.name(m.get("user"))

        from .parser import parse_message
        from .run import fill_item_matches, fix_department
        parsed = parse_message(self.claude, text, route_name, m["ts"], self.alliant, author=author,
                               office=self.is_office(m.get("user")))
        if parsed.category != Category.not_a_request:
            fix_department(parsed, self.alliant, route_from_channel(route_name), service_day(m["ts"]))
            fill_item_matches(self.claude, parsed, self.alliant)
        result = check(parsed, self.alliant)

        did = "read"
        if parsed.category == Category.not_a_request:
            pass
        elif result.questions and asks < MAX_ASKS and parsed.category in ACTIONABLE:
            qs = "\n".join(f"• {q}" for q in result.questions)
            # @mention so it pushes to the driver's phone; also show it in the channel so it can't be missed.
            _post(self.slack, channel, f"<@{m.get('user')}> Quick check before this goes to the office:\n{qs}",
                  {"kind": "question", "trigger": trigger["ts"]}, thread_ts=m["ts"], reply_broadcast=True)
            did = "asked"
        else:
            correction = last_readback >= 0
            updated = any((meta(t) or {}).get("kind") == "question" for t in thread) or (channel, m["ts"]) in open_tickets
            open_tickets[(channel, m["ts"])] = [self.post_ticket(route_name, channel, m, author, thread, parsed, result,
                             label="🔁 *Correction*" if correction else "✏️ *Driver answered*" if updated and len(thread) > 1 else "",
                             replaces=open_tickets.get((channel, m["ts"]), []),
                             latest=trigger.get("text") if trigger is not m else None)]
            did = "ticket"
        for t in todo:
            try:
                self.slack.reactions_add(channel=channel, timestamp=t["ts"], name=SEEN)
            except Exception as e:  # already_reacted on a rerun is fine
                if "already_reacted" not in str(e):
                    raise
        return did

    def post_ticket(self, route_name, channel, m, author, thread, parsed, result, label, replaces, latest=None):
        link = self.slack.chat_getPermalink(channel=channel, message_ts=m["ts"])["permalink"]
        if parsed.category in ACTIONABLE:
            lines = ticket(result, self.alliant, with_readback=False).splitlines()
            rb = readback(result) if not result.questions else ""
            if result.questions:
                lines.append("⚠️ _Still unclear after asking the driver. Please call them._")
        else:
            lines = [f"📣 *FYI: {parsed.category.value.replace('_', ' ')}*", parsed.summary]
            rb = f"✅ Office has it: {parsed.summary}"
        if label:
            lines.insert(1, f"{label}" + (f": _\u201c{_short(latest)}\u201d_" if latest else ""))
        text = "\n".join(lines + ["", _quote(_short(m.get("text", ""), 300)),
                                   f"<{link}|#{route_name} thread> · React ✅ when it's in Alliant"])
        new = _post(self.slack, self.desk_id, text, {"kind": "ticket", "src_channel": channel, "src_ts": m["ts"],
                                                     "readback": rb, "driver": author, "driver_id": m.get("user")})
        for old_ts in replaces:
            _post(self.slack, self.desk_id, "Replaced by a newer ticket for the same request, below. Don't enter this one.",
                  {"kind": "replaced"}, thread_ts=old_ts)
        return new["ts"]

    # ---- office side ----------------------------------------------------------------------------------------

    def open_tickets(self, oldest: float) -> dict[tuple[str, str], list[str]]:
        """Tickets the office hasn't finished, keyed by the driver message they came from."""
        out: dict[tuple[str, str], list[str]] = {}
        self._tickets = []
        for m in _messages(self.slack, self.desk_id, oldest, include_all_metadata=True):
            p = meta(m)
            if not p or p.get("kind") != "ticket":
                continue
            replies = self.thread(self.desk_id, m)[1:]
            if any((meta(r) or {}).get("kind") in ("sent", "replaced") for r in replies):
                continue
            out.setdefault((p["src_channel"], p["src_ts"]), []).append(m["ts"])
            self._tickets.append((m, p, replies))
        return out

    def finish_tickets(self, open_t: dict) -> int:
        """Send the readback for every open ticket the office has marked done."""
        sent = 0
        for m, p, replies in self._tickets:
            people = [r for r in replies if not _is_bot(r, self.me)]
            done_by = next((u for r in m.get("reactions", []) if r.get("name") in DONE_REACTIONS
                            for u in r.get("users", []) if u != self.me), None)
            done_by = done_by or next((r.get("user") for r in people if DONE_WORDS.match(r.get("text", ""))), None)
            if not done_by:
                continue
            who = self.name(done_by)
            notes = [r.get("text", "") for r in people if not DONE_WORDS.match(r.get("text", ""))]
            msg = (f"<@{p['driver_id']}> " if p.get("driver_id") else "") + (p.get("readback") or "✅ Done") + f" – entered by {who}"
            msg += "".join(f"\nOffice note: {n}" for n in notes)
            msg += "\n_Reply here if that's not what you meant._"
            _post(self.slack, p["src_channel"], msg, {"kind": "readback"}, thread_ts=p["src_ts"])
            _post(self.slack, self.desk_id, f"Readback sent to {p.get('driver', 'the driver')}.", {"kind": "sent"}, thread_ts=m["ts"])
            key = (p["src_channel"], p["src_ts"])
            open_t[key] = [ts for ts in open_t.get(key, []) if ts != m["ts"]]
            sent += 1
        return sent

    # ---- one pass -------------------------------------------------------------------------------------------

    def run_once(self, since_minutes: int = 4320, ticket_days: int = 14) -> dict:
        now = time.time()
        counts = {"asked": 0, "ticket": 0, "read": 0, "readback": 0}
        open_t = self.open_tickets(now - ticket_days * 86400)
        counts["readback"] = self.finish_tickets(open_t)
        for name, cid in sorted(self.routes.items()):
            for m in reversed(list(_messages(self.slack, cid, now - since_minutes * 60, include_all_metadata=True))):
                if m.get("subtype") or _is_bot(m, self.me):
                    continue
                key = (cid, m["ts"])
                if _seen(m, self.me) and self.replies_seen.get(key) == m.get("latest_reply"):
                    continue  # nothing new since the last look
                did = self.handle_driver_message(name, cid, m, open_t)
                self.replies_seen[key] = m.get("latest_reply")
                if did:
                    counts[did] += 1
        return counts


def main():
    import anthropic
    from slack_sdk import WebClient

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    ap.add_argument("--desk", default="service-desk", help="office channel for tickets")
    ap.add_argument("--routes", required=True, help="comma-separated route channels, e.g. route-12-test or route-1,route-2")
    ap.add_argument("--since-minutes", type=int, default=4320, help="how far back to look for driver posts")
    ap.add_argument("--watch", type=int, default=0, help="repeat every N seconds (0 = one pass)")
    args = ap.parse_args()

    routes = [r.strip().lstrip("#") for r in args.routes.split(",") if r.strip()]
    live = [r for r in routes if LIVE_ROUTE.fullmatch(r)]
    if live and os.environ.get("SERVICE_DESK_LIVE") != "1":
        sys.exit(f"{', '.join('#' + r for r in live)} are real route channels: that needs SERVICE_DESK_LIVE=1 "
                 "in the environment. Use test channels like #route-12-test until then.")

    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SERVICE_DESK_API_KEY"))
    alliant, loaded = Alliant.from_dir(args.data), time.time()
    desk = Desk(slack, claude, alliant, args.desk, routes)
    while True:
        try:
            if time.time() - loaded > 3600:  # pick up a fresh export without restarting
                desk.alliant, loaded = Alliant.from_dir(args.data), time.time()
            counts = desk.run_once(args.since_minutes)
            if any(counts[k] for k in ("asked", "ticket", "readback")) or not args.watch:
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

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
import json
import os
import re
import string
import sys
import time
from datetime import datetime

from .bot import _messages, _name, _office_staff
from .checks import check, readback, split_by_account, ticket
from . import todo as todo_bar  # (listen() has its own `todo` queue)
from .todo import GOLD, GREEN, GREY, attachment, restyle, shrink, stamp
from .context import ALASKA, Alliant, route_from_channel, service_day
from .schema import Category

KIND = "service_desk"
SEEN = "eyes"
WORKING = "hourglass_flowing_sand"  # put on by the listener the moment a message arrives, swapped for 👀 when read
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
DONE_WORDS = re.compile(r"^\s*(done|changed|entered|complete[d]?|made|updated|ok(ay)?|got it|all set|finished)\b[\s.!]*$", re.I)
MAX_ASKS = 2
MAX_QUESTIONS = 3  # per message to a driver; the rest come up again once they answer
PULSE = re.compile(r"\b(how('?s| is| are)? (things|everything|it|we|the team|business|operations?)( going| doing)?|vibe|pulse|"
                   r"morale|feel(ing)?|opinion|better|improve|feedback|complain\w*|brief|catch me up|what'?s going on|"
                   r"what am i missing|happening)\b", re.I)
BRIEF_HOUR = int(os.environ.get("OWNER_BRIEF_HOUR", "7"))  # Donna's morning read for Scott, weekdays from this hour
VOICE_PORT = int(os.environ.get("DESK_VOICE_PORT", "8091"))  # Donna's voice (desk.voice), on 127.0.0.1 only
SKU_Q = re.compile(r"\b(weigh\w*|pounds?|lbs?|ounces?|oz|dimensions?|materials?|fabric|made of|blend|skus?|how big|"
                   r"case pack|pack size|packs? of|bundles?|specs?|discontinued|"
                   r"\d-\d\d-\d\d)\b", re.I)  # questions about what an item is, not who has it
AR_Q = re.compile(r"\b(owes?|owing|balance|past due|overdue|paid|last payment|pays?|paying|payments?|costs?|"
                  r"billed|billing|bill (?:them|him|her|for)|how much (?:do|does|did) (?:we|they) (?:get|make|charge)|"
                  r"credit hold|collections?|receivables?|a/?r|pric(?:e|es|ed|ing)|charg(?:e|es|ing)|rates?|contracts?|"
                  r"expir\w*|terms|sales ?rep|bill.?to|invoices?|minimum charge|insurance|replacement cost)\b", re.I)
AR_USERS = {u.strip() for u in os.environ.get("AR_SLACK_IDS", "U4KFSQCDQ,U9SGPJP0C").split(",") if u.strip()}  # Scott, Bill
ALLIANT_Q = re.compile(r"\b(alliant|catalog|customer items|customer entry|new item|stop item|add wearers?|stop wearer|"
                       r"work orders?|pull list|system create|handheld|route mobile|special order|ancillary|"
                       r"how (do|can|would|should) (i|you|we|she|they)\b.*\b(enter|set up|add|change|stop|print|run|find))\b", re.I)
HOW_TO = re.compile(r"\bhow (do|can|would|should) (i|you|we)\b.*\b(alliant|handheld|enter|make|set up|print|run)\b", re.I)  # Alliant how-to notes
VERIFY_HOUR = 7  # entry check posts once the morning's Alliant data is in, from this hour (Alaska)
STOCKROOM = os.environ.get("STOCKROOM_CHANNEL", "stockroom")  # Ana's channel: weekly garment data checks
STOCKROOM_DAY, STOCKROOM_LEAD = 0, "Ana Faamita"  # Mondays, tagging the stockroom manager
STALE_HOURS = 36  # the Alliant feed runs nightly; older than this means it has stopped (e.g. the Mac restarted)
MAX_LOOKUPS_PER_DAY = 15  # per person: past this the bot stops answering and tells the office
ACTIONABLE = {Category.item_change, Category.wearer_change, Category.hold_or_closure, Category.special_order}
# Scott telling Donna a change himself (owner_change): worth parsing as one; "yes" / "no" to her readback.
CHANGE_Q = re.compile(r"\b(change|add|stop|cancel|take (?:off|away)|remove|increase|decrease|raise|lower|cut|bump|drop|"
                      r"swap|switch|replace|set|make (?:it|that|them)|new wearer|put\b.{0,40}\bon|start|more|fewer|less)\b", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|correct|right|do it|go ahead|go for it|please do|put it in|enter it|"
                 r"sounds good|ok(?:ay)?|that's (?:it|right)|perfect)\b", re.I)
NO = re.compile(r"^\W*(no|nope|cancel|never ?mind|don't|wait|hold on|scratch that)\b", re.I)
DONE_ANY = re.compile(r"\b(done|entered|changed|complete[d]?|all set|finished)\b", re.I)
ACCOUNT = re.compile(r"\b\d{4}-\d-\d{5}\b")
LIVE_ROUTE = re.compile(r"route-\d+")


def meta(m: dict) -> dict | None:
    md = m.get("metadata") or {}
    return md.get("event_payload") if md.get("event_type") == KIND else None


def _post(slack, channel: str, text: str, payload: dict, thread_ts: str | None = None, todo: str | None = None, **kw) -> dict:
    """todo: "button" for a gold to-do bar with ✓ Done, "own" for a gold bar on a message with its own ✅ flow (todo.py)."""
    if todo:
        kw["attachments"] = attachment(text, button=todo == "button")
        text = ""
    return slack.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts, unfurl_links=False,
                                  metadata={"event_type": KIND, "event_payload": payload}, **kw)


def customer_readback(rb: str, who: str, notes: list[str]) -> str:
    """The readback as a customer reads it, in Donna's voice: no ticket marks or account number, who entered it, and
    a thank-you. "✅ 5045-1-00000 HOMER GOLF COURSE – 50 bar mops added – new on the account" ->
    "All done! 50 bar mops added – new on the account (HOMER GOLF COURSE). Ana entered it for you. Thanks so much!" """
    rb = re.sub(r"^✅\s*", "", rb or "").strip()
    m = re.match(r"^(\d{4}-\d-\d{5})\s+(.+?)\s+–\s+(.+)$", rb)
    what = f"{m.group(3)} ({m.group(2).title()})" if m else rb
    first = (who or "").split()[0] if who else "our office"
    out = f"All done! {what}. {first} entered it for you." if what else f"All done! {first} took care of it."
    out += "".join(f"\nA note from our office: {n}" for n in notes)
    return out + "\nThanks so much!"


def spoken(text: str) -> str:
    """A Slack answer as it should sound: no mentions, bold, links or the data-age note."""
    text = re.sub(r"<@\w+>\s*", "", text)
    text = re.sub(r"<[^|>]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"\s*_?\(Alliant as of [^)]*\)_?", "", text)
    return re.sub(r"[*_`]", "", text).strip()


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
        self.plant = _office_staff("plant_staff.txt")
        self.replies_seen: dict[tuple[str, str], str] = {}  # (channel, ts) -> latest_reply already looked at
        self.links: dict[tuple[str, str], set[str]] = {}  # (channel, request ts) -> main-channel replies read with it
        self.listening = False
        self.data_dir = "service_changes/data"
        self.fetched: dict[str, set[str]] = {}  # per route channel: the top-level messages the last pass saw
        # Office ✅ reactions straight from Slack's event (ticket ts -> who), for when history doesn't show them yet.
        self.done_events: dict[str, str] = {}
        self.stale_warned: float | None = None  # when the office was last told the Alliant data is stale
        channels = {}
        self.all_channels: dict[str, str] = {}  # every channel the bot can see, member or not
        cursor = None
        while True:
            r = slack.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=500, cursor=cursor)
            channels.update({c["name"]: c["id"] for c in r["channels"] if c.get("is_member")})
            self.all_channels.update({c["name"]: c["id"] for c in r["channels"]})
            cursor = r.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        missing = [n for n in [desk, *routes] if n not in channels]
        if missing:
            sys.exit("Invite the bot (@Donna, formerly @Service Desk) to " + ", ".join("#" + n for n in missing) + " first.")
        self.desk_name, self.desk_id = desk, channels[desk]
        self.routes = {n: channels[n] for n in routes}
        self.channels = channels
        self.helper = None  # #alliant-help answers (alliant_help.py), set up in main when the bot is in that channel

    def name(self, user: str | None) -> str:
        return _name(self.slack, self.names, user)

    def is_office(self, user: str | None) -> bool:
        return self.name(user).lower() in self.office

    def is_owner(self, user: str | None) -> bool:
        """Scott, by his Slack user id (Slack signs every message, so this can't be faked the way a text or an email
        can). OWNER_SLACK_ID in /etc/service-desk.env changes it."""
        return bool(user) and user in {u.strip() for u in os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")}

    def asker(self, user: str | None) -> str:
        return "owner of the company, in the office" if self.is_owner(user) else "office staff member"

    def is_staff(self, user: str | None) -> bool:
        """Office, plant or dispatch: not the driver the bot is talking to."""
        return self.is_office(user) or self.name(user).lower() in self.plant

    def role(self, user: str | None) -> bool | str:
        if self.is_office(user):
            return True
        return "plant, dispatch or route supervisor; doesn't enter changes in Alliant" if self.is_staff(user) else False

    def thread(self, channel: str, m: dict) -> list[dict]:
        if not m.get("reply_count"):
            return [m]
        return self.hear(channel, self.slack.conversations_replies(channel=channel, ts=m["ts"], include_all_metadata=True,
                                                                   limit=200)["messages"])

    def hear(self, channel: str, msgs: list[dict]) -> list[dict]:
        """Voice messages (a driver holds the mic in Slack, gloves on): each clip's words become the message's text, so
        it's read like anything typed. Transcribed once (ElevenLabs, webchat.transcribe), kept in voice_notes.json;
        the first time, Donna posts what she heard so the driver can check it."""
        clips = [(m, f) for m in msgs if not _is_bot(m, self.me) for f in m.get("files") or []
                 if f.get("subtype") == "slack_audio" or str(f.get("mimetype", "")).startswith("audio/")]
        if not clips:
            return msgs
        path = os.path.join(self.data_dir, "voice_notes.json")
        try:
            heard = json.load(open(path))
        except (OSError, ValueError):
            heard = {}
        new = False
        for m, f in clips:
            m.pop("unheard", None)
            words = heard.get(f.get("id", ""))
            if words is None and f.get("id") and not _seen(m, self.me):
                words = self.transcribe_clip(f)
                if words is None:
                    m["unheard"] = True  # not reachable now: left unread, so the next pass tries again
                    continue
                heard[f["id"]], new = words, True
                self.post(channel, f"<@{m.get('user')}> 🎙️ Heard: _{words or '(nothing I could make out: type it, or try again)'}_",
                          {"kind": "heard", "src_ts": m["ts"], "driver_id": m.get("user")},
                          thread_ts=m.get("thread_ts") if m.get("thread_ts") not in (None, m["ts"]) else None)
            if words and words not in m.get("text", ""):
                m["text"] = (m.get("text", "").strip() + " " + words).strip()
        if new:
            with open(path + ".tmp", "w") as fh:
                json.dump(heard, fh)
            os.replace(path + ".tmp", path)
        return msgs

    def transcribe_clip(self, f: dict) -> str | None:
        import urllib.request
        from .webchat import transcribe
        url = f.get("url_private_download") or f.get("url_private")
        token = getattr(self.slack, "token", None) or os.environ.get("SLACK_BOT_TOKEN", "")
        if not url or (f.get("size") or 0) > 25_000_000:
            return ""
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                audio, ctype = r.read(25_000_001), r.headers.get("Content-Type", "")
        except Exception as e:
            print(time.strftime("%H:%M"), "voice message not downloaded:", str(e)[:200], flush=True)
            return None
        if ctype.startswith("text/html"):  # Slack's sign-in page: the app is missing files:read
            print(time.strftime("%H:%M"), "voice message: Slack sent a web page, not audio (add files:read to the app)", flush=True)
            return None
        return transcribe(audio, f.get("name") or "clip.m4a", f.get("mimetype") or "audio/mp4")

    def post(self, channel: str, text: str, payload: dict, posted: list | None = None, **kw) -> dict:
        r = _post(self.slack, channel, text, payload, **kw)
        m = {"ts": r["ts"], "user": self.me, "bot_id": "self", "text": text,
             "metadata": {"event_type": KIND, "event_payload": payload}}
        if kw.get("todo"):
            m.update(text="", attachments=attachment(text, button=kw["todo"] == "button"))
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

    def conversation(self, convo: list[dict], src: dict | None = None) -> str:
        """Request, questions, answers, readback and replies as one message for the parser. When `src` isn't the
        first message (a new request right after an answered lookup), the earlier exchange is marked as context."""
        lines = []
        new = src is not None and convo and src is not convo[0]
        for i, m in enumerate(convo):
            text = m.get("text", "")
            if _is_bot(m, self.me):
                kind = (meta(m) or {}).get("kind")
                if kind == "question":
                    lines.append(f"Donna (Service Desk) asked: {text}")
                elif kind == "readback":
                    lines.append(f"Office entered it and confirmed: {text}")
                elif kind == "answer":
                    lines.append(f"Donna (Service Desk) answered: {text}")
            elif new and m is src:
                lines.append(f"{self.name(m.get('user'))} (NEW message, read this one): {text}")
            elif i == 0:
                lines.append(f"{self.name(m.get('user'))} ({'earlier question, already answered' if new else 'original request'}): {text}")
            else:
                lines.append(f"{self.name(m.get('user'))}{' (office)' if self.is_office(m.get('user')) else ' (plant)' if self.is_staff(m.get('user')) else ''}: {text}")
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
        text = src.get("text", "") if len(convo) == 1 else self.conversation(convo, src)
        author = self.name(src.get("user"))
        replies = [t["ts"] for t in convo if t is not src and not _is_bot(t, self.me) and t.get("thread_ts") != src["ts"]]
        self.links.setdefault((channel, src["ts"]), set()).update(replies)

        from .parser import parse_message
        from .run import fill_item_matches, fix_department, other_route_account
        parsed = parse_message(self.claude, text, route_name, src["ts"], self.alliant, author=author,
                               office=self.role(src.get("user")))
        if parsed.category != Category.not_a_request:
            other_route_account(parsed, self.alliant)
        # A message for several accounts ("bar mops for the bakery, a new wearer in tire") becomes one part per
        # account: questions are asked together, then each part gets its own ticket and readback.
        parts = split_by_account(parsed) if parsed.category in ACTIONABLE else [parsed]
        if parsed.category != Category.not_a_request:
            for part in parts:
                fix_department(part, self.alliant, route_from_channel(route_name), service_day(src["ts"]))
                fill_item_matches(self.claude, part, self.alliant)
        results = [check(part, self.alliant) for part in parts]
        questions = list(dict.fromkeys(q for r in results for q in r.questions))
        if parsed.category == Category.service_code:  # pull garments for repair/inspection, or an inventory count
            from .service_codes import review
            questions = list(dict.fromkeys(questions + review(parsed, self.alliant)[2]))

        did = "read"
        item = self.sku_answer(src.get("text", ""), src.get("user")) if parsed.category in (
            Category.lookup, Category.howto, Category.not_a_request) and not parsed.account_number else ""
        if item and not item.startswith("I don't have the SKU details"):  # "how much does a bath blanket weigh?"
            self.post(channel, f"<@{src.get('user')}> {item}"[:3900],
                      {"kind": "answer", "src_ts": src["ts"], "driver_id": src.get("user"), "msgs": replies}, bot_posts)
            did = "answered"
        elif parsed.category == Category.not_a_request:
            pass
        elif parsed.category == Category.route_lookup:
            did = self.route_answer(route_name, channel, src, parsed, replies, bot_posts)
        elif parsed.category == Category.howto:
            did = self.howto_answer(channel, src, parsed, replies, bot_posts)
        elif parsed.category == Category.lookup and parsed.account_number and not parsed.questions_for_driver:
            did = self.answer(route_name, channel, src, author, parsed, replies, bot_posts)
        elif (parsed.category == Category.lookup and not parsed.account_number and not parsed.questions_for_driver
              and (group := self.customer_group(route_name, parsed.customer_as_written))):
            # "All of them combined": every location of this customer on the driver's route (7 Costco departments).
            did = self.answer(route_name, channel, src, author, parsed, replies, bot_posts, group=group)
        elif questions and asks < MAX_ASKS and (parsed.category in ACTIONABLE
                                                or parsed.category in (Category.lookup, Category.service_code)):
            qs = "\n".join(f"• {q}" for q in questions[:MAX_QUESTIONS])
            # A normal channel message with an @mention, so it pushes to the driver's phone.
            self.post(channel, f"<@{src.get('user')}> Quick check on _{_short(src.get('text', ''), 60)}_\n{qs}",
                      {"kind": "question", "src_ts": src["ts"], "driver_id": src.get("user"), "msgs": replies}, bot_posts)
            did = "asked"
        else:
            key = (channel, src["ts"])
            old = open_tickets.get(key, [])
            open_tickets[key] = [self.post_ticket(route_name, channel, src, author, part, r, replies,
                                                  correction=last_readback >= 0, replaces=old if i == 0 else [])
                                 for i, (part, r) in enumerate(zip(parts, results))]
            did = "ticket"
        self.mark_seen(channel, todo)
        return did

    def customer_group(self, route_name: str, as_written: str | None) -> list[str]:
        """The accounts on this route whose name contains every word the driver used ("Costco 1342" -> its seven
        departments). Empty unless there are 2-12 of them."""
        words = re.findall(r"[a-z0-9]+", (as_written or "").lower())
        if not words:
            return []
        route = route_from_channel(route_name)
        hits = [c.account for c in self.alliant.customers if (not route or c.route == route)
                and all(w in re.findall(r"[a-z0-9]+", c.name.lower()) for w in words)]
        return hits if 2 <= len(hits) <= 12 else []

    def answer(self, route_name, channel, src, author, parsed, replies, bot_posts, group: list[str] | None = None) -> str:
        """A driver's question about an account, answered from the Alliant export. Every answer is logged in the
        office channel; if the data doesn't say, the office gets a ticket instead; past MAX_LOOKUPS_PER_DAY for one
        person the bot stops answering and flags it."""
        from .context import ALASKA, account_facts
        from .parser import answer_lookup
        from datetime import datetime
        acct = parsed.account_number or (group or [""])[0]
        name = next((c.name for c in self.alliant.customers if c.account == acct), parsed.customer_as_written or acct)
        if group:
            acct, name = ", ".join(group), f"{(parsed.customer_as_written or name).upper()} ({len(group)} accounts)"
        link = self.slack.chat_getPermalink(channel=channel, message_ts=src["ts"])["permalink"]
        midnight = datetime.now(ALASKA).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = sum(1 for b in bot_posts if (meta(b) or {}).get("kind") == "answer"
                    and meta(b).get("driver_id") == src.get("user") and float(b["ts"]) >= midnight)
        cust = None if group else next((c for c in self.alliant.customers if c.account == acct), None)
        here = route_from_channel(route_name)
        if cust and here and cust.route != here and not self.is_staff(src.get("user")):
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
        facts = "\n\n".join(account_facts(self.alliant, a) for a in group) if group else account_facts(self.alliant, acct)
        if not group:  # "are Dave's garments on the way?": last scans and garment orders (no prices in either)
            from .context import garment_facts
            facts += "\n" + garment_facts(self.alliant, acct, f"{src.get('text', '')} {parsed.summary or ''}")
        ans = answer_lookup(self.claude, parsed.summary or src.get("text", ""), facts)
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

    def route_answer(self, route_name, channel, src, parsed, replies, bot_posts) -> str:
        """"What are my stops today?": the stops, order and days of this channel's route only."""
        from .context import route_facts
        from .parser import answer_lookup
        route = route_from_channel(route_name)
        facts = route_facts(self.alliant, route) if route else ""
        if not facts:
            return "read"
        day = service_day(src["ts"])
        ans = answer_lookup(self.claude, f"(Today is {day}.) {parsed.summary or src.get('text', '')}", facts)
        as_of = f" _(Alliant as of {self.alliant.as_of})_" if self.alliant.as_of else ""
        self.post(channel, f"<@{src.get('user')}> {ans.answer}{as_of}",
                  {"kind": "answer", "src_ts": src["ts"], "driver_id": src.get("user"), "msgs": replies}, bot_posts)
        return "answered"

    def howto_answer(self, channel, src, parsed, replies, bot_posts) -> str:
        """"How do I ... on the handheld?": answered from the Alliant University notes, in the thread."""
        if self.helper is None:
            return "read"
        reply, missed = self.helper.answer([{"role": "user", "content": src.get("text", "")}], src.get("user"))
        if missed:
            reply += "\n\n_I've asked the office to follow up._"
            self.post(self.desk_id, f"❓ *Alliant how-to the notes don't cover* (from {self.name(src.get('user'))}): "
                                    f"_{_short(src.get('text', ''), 200)}_", {"kind": "howto_missed"})
        self.post(channel, reply[:3900], {"kind": "answer", "src_ts": src["ts"], "driver_id": src.get("user"),
                                          "msgs": replies, "howto": True}, bot_posts, thread_ts=src["ts"])
        return "answered"

    def post_ticket(self, route_name, channel, src, author, parsed, result, replies, correction, replaces) -> str:
        link = self.slack.chat_getPermalink(channel=channel, message_ts=src["ts"])["permalink"]
        if parsed.category in ACTIONABLE:
            lines = ticket(result, self.alliant, with_readback=False).splitlines()
            rb = readback(result) if not result.questions else ""
            if result.questions:
                lines.append("⚠️ _Still unclear after asking the driver. Please call them._")
        elif parsed.category == Category.service_code:
            from .service_codes import review
            lines, rb, still = review(parsed, self.alliant)
            if still:
                lines += [f"❓ {q}" for q in still] + ["⚠️ _Still unclear after asking the driver. Please call them._"]
        else:
            lines = [f"📣 *FYI: {parsed.category.value.replace('_', ' ')}*", parsed.summary]
            rb = f"✅ Office has it: {parsed.summary}"
        if correction:
            lines.insert(1, "🔁 *Correction*: the driver says the last entry wasn't right")
        cust = next((c for c in self.alliant.customers if c.account == parsed.account_number), None)
        here = route_from_channel(route_name)
        if cust and here and cust.route != here:
            lines.insert(1, f"📍 _A route {cust.route} customer, posted in #{route_name}_")
        done = ("Nothing to enter: the driver changed it on the handheld. React ✅ when seen"
                if parsed.category == Category.today_only else "React ✅ or type done when it's in Alliant"
                if parsed.category in ACTIONABLE or parsed.category == Category.service_code else "React ✅ when it's handled")
        where = f"#{route_name}" if route_name in self.channels else route_name  # "Scott, by voice"
        text = "\n".join(lines + ["", _quote(_short(src.get("text", ""), 300)), f"<{link}|{where}> · {done}"])
        from .verify import expectations
        expect = expectations(result) if parsed.category in ACTIONABLE and not result.questions else []
        new = self.post(self.desk_id, text, {"kind": "ticket", "src_channel": channel, "src_ts": src["ts"],
                                             "readback": rb, "driver": author, "driver_id": src.get("user"),
                                             "account": parsed.account_number or "", "msgs": replies, "expect": expect,
                                             **({"owner_sms": src["owner_sms"]} if src.get("owner_sms") else {})},
                        todo="own")
        for old_ts in replaces:
            old = self.desk_msgs.get(old_ts)
            if old:
                self.slack.chat_update(channel=self.desk_id, ts=old_ts, text="", attachments=shrink(
                                           self.slack, self.desk_id, old, GREY,
                                           "🚫 *Replaced by a newer ticket below. Don't enter this one.*"),
                                       metadata={"event_type": KIND, "event_payload": {**meta(old), "kind": "replaced"}})
        return new["ts"]

    def start_from(self, path: str) -> None:
        """Remember when the desk first watched each channel, and never reach back before it: switching on the real
        #route-N channels must not answer the last three days of driver posts."""
        try:
            with open(path) as f:
                first = json.load(f)
        except (OSError, ValueError):
            first = {}
        now = time.time()
        for cid in self.routes.values():
            first.setdefault(cid, now)
        with open(path + ".new", "w") as f:
            json.dump(first, f)
        os.replace(path + ".new", path)
        self.first_watched = first

    def route_pass(self, name: str, cid: str, oldest: float, open_t: dict, counts: dict) -> None:
        oldest = max(oldest, getattr(self, "first_watched", {}).get(cid, 0))
        top = list(reversed(list(_messages(self.slack, cid, oldest, include_all_metadata=True))))
        self.fetched[cid] = {m["ts"] for m in top}
        bot_posts = [m for m in top if _is_bot(m, self.me) and meta(m)]
        self.hear(cid, top)
        for m in top:
            if m.get("subtype") not in (None, "file_share") or _is_bot(m, self.me) or m.get("unheard"):
                continue
            key = (cid, m["ts"])
            if not _seen(m, self.me) and self.phone_add(name, cid, m):
                counts["read"] += 1
            elif not _seen(m, self.me):
                # A main-channel message right after the bot asked or read back to this person may be the answer.
                b = self.addressed_to(bot_posts, m)
                src = next((x for x in top if b and x["ts"] == meta(b)["src_ts"]), None)
                from .parser import is_reply
                # A one- or two-word message right after a question ("1821", "6", "yes weekly") is the answer.
                short = meta(b or {}) and meta(b)["kind"] == "question" and len(m.get("text", "").split()) <= 2
                if src and meta(b)["kind"] == "answer":
                    # After the bot answered a lookup, the next message is a request of its own ("add 2 mops to
                    # Subway Steese"); the lookup and answer are kept only as context for "what about aprons?".
                    did = self.handle(name, cid, m, self.convo(cid, src, top, bot_posts, [m]), [m], open_t, bot_posts)
                elif src and (short or is_reply(self.claude, b.get("text", ""), src.get("text", ""), m.get("text", ""))):
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

    def phone_add(self, route_name: str, cid: str, m: dict) -> bool:
        """A driver's "add 907-555-1234 to Midas Fairbanks": the office gets a card to approve with ✅. Drivers can
        only suggest; the link between a phone and an account is always made by the office."""
        from .sms import ADD_CMD, REMOVE_CMD, signup_card
        text = m.get("text", "")
        add = ADD_CMD.match(text)
        if not add:
            if REMOVE_CMD.match(text):
                self.post(cid, f"<@{m.get('user')}> Only the office can remove a texting number. I've let them know.",
                          {"kind": "answer", "src_ts": m["ts"], "driver_id": m.get("user")})
                self.post(self.desk_id, f"📱 *{self.name(m.get('user'))}* in #{route_name} asks: _{text[:120]}_",
                          {"kind": "sms_admin"}, todo="button")
                self.mark_seen(cid, [m])
                return True
            return False
        from .parser import identify_customer
        route = route_from_channel(route_name)
        on_route = [c for c in self.alliant.customers if not route or c.route == route] or self.alliant.customers
        found = identify_customer(self.claude, add.group(2), on_route)
        signup_card(self.slack, self.desk_id, self.alliant, add.group(1), "", add.group(2), found.accounts,
                    by=f"{self.name(m.get('user'))} in #{route_name}")
        self.post(cid, f"<@{m.get('user')}> Thanks, I've sent that to the office to approve.",
                  {"kind": "answer", "src_ts": m["ts"], "driver_id": m.get("user")})
        self.mark_seen(cid, [m])
        return True

    def _answers(self, t: dict, src: dict) -> bool:
        """A thread reply the bot should read: from whoever posted, or anyone not in the office, not yet 👀."""
        return (not _is_bot(t, self.me) and t.get("subtype") in (None, "thread_broadcast", "file_share") and not _seen(t, self.me)
                and not t.get("unheard")
                and (t.get("user") == src.get("user") or not self.is_staff(t.get("user"))))

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
                        for u in r.get("users", []) if u != self.me), None) or self.done_events.pop(t["ts"], None)
            people = [r for r in self.thread(self.desk_id, t)[1:] if not _is_bot(r, self.me)]
            who = who or next((r.get("user") for r in people if DONE_WORDS.match(r.get("text", ""))), None)
            if who:
                done[t["ts"]] = (who, [r.get("text", "") for r in people if not DONE_WORDS.match(r.get("text", ""))])
        for h in top:  # "done" typed in the channel itself
            if _is_bot(h, self.me) or h.get("subtype") or _seen(h, self.me):
                continue
            text = h.get("text", "")
            from .email_desk import is_email_command
            from .sms import is_phone_command
            if is_phone_command(text) or is_email_command(text):  # "add 907-555-1234 to Midas": the texting service (sms.py) handles it
                self.mark_seen(self.desk_id, [h])
                continue
            if DONE_ANY.search(text):
                accts = ACCOUNT.findall(text)
                cands = [t for t in tickets if t["ts"] not in done and (not accts or meta(t).get("account") in accts)]
                if len(cands) == 1:
                    rest = DONE_ANY.sub("", ACCOUNT.sub("", text)).strip(" .,!-:")
                    done[cands[0]["ts"]] = (h.get("user"), [rest] if len(rest.split()) > 1 else [])
                elif cands:
                    self.post(self.desk_id, f"<@{h.get('user')}> Which one? React ✅ on the ticket, or type done "
                                            "with the account number.", {"kind": "which"})
            elif not h.get("thread_ts"):
                from .announce import ASK
                if ASK.search(text) and self.announcer().propose(h):  # "Donna, let route 4 know we're running late"
                    counts["answered"] += 1
                elif self.office_lookup(h, top):
                    counts["answered"] += 1
            self.mark_seen(self.desk_id, [h])
        for t in tickets:
            if t["ts"] in done:
                self.send_readback(t, *done[t["ts"]])
                counts["readback"] += 1
        only = os.environ.get("DONNA_ALLIANT", "off").strip()  # on, off, or account numbers to try her on ("5045")
        if only.lower() != "off":  # Donna's workstation is set up (alliant_jobs.py)
            try:
                bases = [] if only.lower() == "on" else [a.strip() for a in only.split(",") if a.strip()]
                self.jobs().offer([t for t in tickets if not bases or any(
                    (meta(t) or {}).get("account", "").startswith(b) for b in bases)], set(done))
            except Exception as e:
                print(time.strftime("%H:%M"), "alliant jobs: button not added:", e, flush=True)
        open_t: dict[tuple[str, str], list[str]] = {}
        for t in tickets:
            if t["ts"] not in done:
                open_t.setdefault((meta(t)["src_channel"], meta(t)["src_ts"]), []).append(t["ts"])
        return open_t

    def office_lookup(self, h: dict, top: list[dict], channel: str | None = None, say=None) -> bool:
        """A question typed in the office channel ("how many shop towels does Midas get?"), answered from the Alliant
        export right in the channel, like the route channels. Any customer on any route; no daily limit and no log,
        since it is the office. If the bot just asked this person which customer, this message is read as the answer.
        `channel` is a private message with the bot (dm) instead of the office channel; `say(text, payload)` gets the
        answer instead of Slack (Donna's voice on Scott's phone)."""
        here = channel or self.desk_id
        say = say or (lambda text, payload: self.post(here, text, payload))
        from .context import account_facts, garment_facts
        from .parser import answer_lookup, parse_message
        user, text = h.get("user"), h.get("text", "")
        asked = next((b for b in reversed(top) if float(b["ts"]) < float(h["ts"]) and _is_bot(b, self.me)
                      and (meta(b) or {}).get("kind") == "office_ask" and meta(b).get("user") == user
                      and float(h["ts"]) - float(b["ts"]) < 900), None)
        if asked:
            text = f"{meta(asked)['q']}\nDonna asked: {asked.get('text', '')}\nAnswer: {text}"
        codes = [b for b in re.findall(r"\b\d{5,14}\b", text)
                 if self.alliant.garment(b) or getattr(self.alliant, "retired_line", lambda b: "")(b)]
        if codes:  # "how many washes on 1234567?"
            from .context import barcode_facts
            ans = answer_lookup(self.claude, text, barcode_facts(self.alliant, codes[:10]),
                                asker=self.asker(user))
            as_of = f" _(Alliant as of {self.alliant.as_of})_" if self.alliant.as_of else ""
            say(f"<@{user}> {ans.answer}{as_of}"[:3900], {"kind": "answer", "barcodes": codes[:10]})
            return True
        sku_answer = self.sku_answer(text, user)
        if sku_answer:
            say(f"<@{user}> {sku_answer}"[:3900], {"kind": "answer", "skus": True})
            return True
        parsed = parse_message(self.claude, text, self.desk_name, h["ts"], self.alliant, author=self.name(user),
                               office=True)
        if AR_Q.search(text):  # "how much does Midas owe us, and when did they last pay?"
            return self.ar_answer(user, text, parsed, here, say)
        if parsed.category != Category.lookup:
            return False
        route = re.search(r"\b(?:route|rte|rt)\s*#?\s*(\d{1,2})\b", text, re.I)
        if route and not parsed.account_number:  # "what's the stop order on route 4 Tuesday?"
            from .context import route_facts
            facts = route_facts(self.alliant, route.group(1))
            if facts:
                ans = answer_lookup(self.claude, parsed.summary or text, facts, asker=self.asker(user))
                as_of = f" _(Alliant as of {self.alliant.as_of})_" if self.alliant.as_of else ""
                say(f"<@{user}> *Route {route.group(1)}*: {ans.answer}{as_of}"[:3900],
                          {"kind": "answer", "route": route.group(1)})
                return True
        group = self.office_group(text, parsed)
        if group:
            from .context import group_facts
            ans = answer_lookup(self.claude, (parsed.summary or text) + "\n(Covers every account listed: give the combined "
                                "totals first, by SKU, from the TOTALS lines, then a short per-location list only if asked.)",
                                group_facts(self.alliant, group, text + " " + (parsed.summary or "")),
                                asker=self.asker(user))
            as_of = f" _(Alliant as of {self.alliant.as_of})_" if self.alliant.as_of else ""
            say(f"<@{user}> *{len(group)} accounts*: {ans.answer}{as_of}"[:3900],
                      {"kind": "answer", "accounts": len(group)})
            return True
        if not parsed.account_number:  # "show me Joey Smith's garments": find the wearer on any account
            words = set(re.findall(r"[a-z]+", text.lower()))
            found = [(acct, w) for acct, ws in self.alliant.wearers.items() for w in ws
                     if len(w.first) > 1 and len(w.last) > 1 and w.first.lower() in words and w.last.lower() in words]
            if len(found) == 1:
                parsed.account_number = found[0][0]
            elif 2 <= len(found) <= 6:
                names = {c.account: c.name for c in self.alliant.customers}
                say(f"<@{user}> Which one? " + "; ".join(
                    f"{w.name} (#{w.employee}) at {acct} {names.get(acct, '')}" for acct, w in found),
                    {"kind": "office_ask", "user": user, "q": text})
                return True
        if not parsed.account_number and getattr(self.alliant, "skus", None):
            # No customer in it: "what's a bath blanket made of?" is about the item itself
            from .context import sku_facts
            facts = sku_facts(self.alliant, text)
            if facts:
                ans = answer_lookup(self.claude, text, "SKU details from Alliant (no prices):\n" + facts, asker=self.asker(user))
                if ans.found:
                    say(f"<@{user}> {ans.answer}"[:3900], {"kind": "answer", "skus": True})
                    return True
        if not parsed.account_number:
            qs = parsed.questions_for_driver or ["Which customer? Add the account number or location."]
            say(f"<@{user}> " + " ".join(qs), {"kind": "office_ask", "user": user, "q": text})
            return True
        acct = parsed.account_number
        name = next((c.name for c in self.alliant.customers if c.account == acct), parsed.customer_as_written or acct)
        question = parsed.summary or text
        facts = "\n".join(x for x in (account_facts(self.alliant, acct), garment_facts(self.alliant, acct, text + " " + question)) if x)
        ans = answer_lookup(self.claude, question, facts, asker=self.asker(user))
        as_of = f" _(Alliant as of {self.alliant.as_of})_" if self.alliant.as_of else ""
        say(f"<@{user}> *{acct}* {name}: {ans.answer}{as_of}", {"kind": "answer", "account": acct})
        return True

    def notices_pass(self) -> None:
        """Once per Alliant feed: garment-arrival and welcome emails to customers (notices.py), and stopped items
        still out at the customer (stops_watch.py)."""
        if getattr(self, "_notices_for", None) is self.alliant:
            return
        self._notices_for = self.alliant
        try:
            from .notices import Notices
            out = Notices(self).pass_()
            if any(out.values()):
                print(time.strftime("%H:%M"), "customer notices:", out, flush=True)
        except Exception as e:
            print(time.strftime("%H:%M"), "customer notices error:", str(e)[:200], flush=True)
        try:  # stopped items still out at the customer: their email, the driver's and the office's reminders
            from .stops_watch import StopsWatch
            out = StopsWatch(self).pass_()
            if any(out.values()):
                print(time.strftime("%H:%M"), "stopped items:", out, flush=True)
        except Exception as e:
            print(time.strftime("%H:%M"), "stopped items error:", str(e)[:200], flush=True)

    def link_contact(self, payload: dict) -> str:
        """A sign-up card's button: office or owner only. Marks the card approved for that account (moving the number
        off its other accounts, or adding this one too); the texting / email desk finishes it (sms.office_pass): links
        the number, texts them they're set up and handles what they asked for."""
        user = payload.get("user", {}).get("id")
        act = next((x for x in payload.get("actions", []) if x.get("action_id", "").startswith("link_")), {})
        ts = payload.get("container", {}).get("message_ts")
        if not (self.is_office(user) or self.is_owner(user)):
            self.slack.chat_postEphemeral(channel=self.desk_id, user=user, text="Only the office can link a number to an account.")
            return "ignored: not office"
        r = self.slack.conversations_history(channel=self.desk_id, latest=ts, inclusive=True, limit=1, include_all_metadata=True)
        m = next((x for x in r.get("messages", []) if x["ts"] == ts), None)
        p = meta(m) if m else None
        if not p or not str(p.get("kind", "")).endswith("_signup"):
            return "not a sign-up card"
        choice = json.loads(act.get("value") or "{}")
        who = self.name(user).split()[0] if self.name(user) else "office"
        if choice.get("b"):  # an imposter or a competitor fishing: never answered again, off every account
            from .sms import block
            block(self.data_dir, p.get("phone", ""), by=self.name(user), why=(p.get("request") or "")[:200])
            a = restyle(m, GREY, status=f"🚫 Blocked by {who}: Donna won't answer this number or email again "
                                        f"(type `unblock {p.get('phone', '')}` to undo)")
            for att in a:
                att["blocks"] = [b for b in att.get("blocks") or [] if b.get("block_id") != "link"]
            self.slack.chat_update(channel=self.desk_id, ts=ts, text="", attachments=a,
                                   metadata={"event_type": KIND, "event_payload": {**p, "kind": "blocked"}})
            return f"blocked {p.get('phone')}"
        a = restyle(m, GOLD, status=f"⏳ {'Moving' if choice.get('m') else 'Adding'} to {choice.get('a')} · {who}")
        for att in a:
            att["blocks"] = [b for b in att.get("blocks") or [] if b.get("block_id") != "link"]
        self.slack.chat_update(channel=self.desk_id, ts=ts, text="", attachments=a, metadata={
            "event_type": KIND, "event_payload": {**p, "accounts": [choice.get("a")], "approved_by": user,
                                                  "move": bool(choice.get("m"))}})
        return f"{'move' if choice.get('m') else 'add'} {p.get('phone')} -> {choice.get('a')}"

    def announcer(self):
        """Messages to many customers at once, sent only when the office taps Send (announce.py)."""
        if "_announcer" not in self.__dict__:
            from .announce import Announcer
            self._announcer = Announcer(self)
        return self._announcer

    def jobs(self):
        """Donna's Alliant work queue (alliant_jobs.py)."""
        if "_jobs" not in self.__dict__:
            from .alliant_jobs import Jobs
            self._jobs = Jobs(self)
        return self._jobs

    def ar_answer(self, user: str | None, text: str, parsed, here: str, say) -> bool:
        """Money questions (what an account owes, invoices, pricing, contract and account details), from the private
        files (private_data.py): for Scott and Bill only (AR_SLACK_IDS), and only in a private message with the bot or
        by voice, never in a channel. Everyone else is told it's not something Donna shares."""
        if user not in AR_USERS:
            say(f"<@{user}> Balances, invoices and pricing aren't something I can share. Scott or Bill can help with that.",
                {"kind": "answer", "ar": "refused"})
            return True
        if here == self.desk_id or not (here == "voice" or str(here).startswith("D")):
            say(f"<@{user}> Ask me that in a private message and I'll tell you there.", {"kind": "answer", "ar": "private"})
            return True
        from . import private_data
        if not private_data.load(self.data_dir):
            say(f"<@{user}> I don't have the private money files yet (balances, invoices, pricing). They come from the "
                "Alliant feed's \"private - Scott and Bill only\" folder.", {"kind": "answer", "ar": "missing"})
            return True
        accounts = self.office_group(text, parsed) or ([parsed.account_number] if parsed.account_number else [])
        if not accounts:
            say(f"<@{user}> Which customer? Give me the account number or location.",
                {"kind": "office_ask", "user": user, "q": text})
            return True
        from .parser import answer_lookup
        names = {c.account: c.name for c in self.alliant.customers}
        ans = answer_lookup(self.claude, text, "Private money files (balances, open invoices, pricing, account details): "
                            "for Scott and Bill only, who are asking now; give the numbers they ask for.\n"
                            + private_data.facts(self.data_dir, accounts[:60], text, names), asker=self.asker(user))
        as_of = f" _(Alliant as of {self.alliant.as_of})_" if self.alliant.as_of else ""
        say(f"<@{user}> {ans.answer}{as_of}"[:3900], {"kind": "answer", "ar": "answered"})
        return True

    def dm(self, ev: dict) -> str | None:
        """A private message to the bot. Anyone can ask how to do something in Alliant (answered from the Alliant
        University notes); office staff can also look up accounts, routes and garments like in #service-desk. Nobody
        else sees it."""
        user, channel = ev.get("user"), ev["channel"]
        try:
            self.slack.reactions_add(channel=channel, timestamp=ev["ts"], name=SEEN)
        except Exception:
            pass
        owner = self.is_owner(user)
        cmd = re.match(r"^\s*donna,?\s+(pause|stop|resume|start|go)\b", ev.get("text", ""), re.I)
        if owner and cmd:  # Donna's Alliant work: "Donna pause" / "Donna resume"
            self.slack.chat_postMessage(channel=channel, text=self.jobs().pause(cmd.group(1).lower() in ("pause", "stop")))
            return "alliant pause" if cmd.group(1).lower() in ("pause", "stop") else "alliant resume"
        mine = self.__dict__.get("changes", {}).get(channel, {}).get("user") == user
        if mine or (owner and CHANGE_Q.search(ev.get("text", ""))):
            said = self.owner_change(channel, ev.get("text", ""), channel, typed=ev, user=user)
            if said:  # "change Chili's bar mops to 60": she reads it back, "yes" puts it in her Alliant queue
                self.slack.chat_postMessage(channel=channel, text=said, unfurl_links=False)
                return "change"
        if owner or self.is_office(user):
            top = list(reversed(list(_messages(self.slack, channel, time.time() - 900, include_all_metadata=True))))
            if self.office_lookup(ev, top, channel=channel):
                return "lookup"
        if owner and not (self.helper and (HOW_TO.search(ev.get("text", "")) or ALLIANT_Q.search(ev.get("text", "")))):
            from .parser import owner_chat
            about = self.briefing()
            if PULSE.search(ev.get("text", "")):
                about += "\n\n" + self.pulse()
            reply = owner_chat(self.claude, self.owner_turns(channel, ev), about)
            self.slack.chat_postMessage(channel=channel, text=reply[:3900] or "I'm here. What do you need?",
                                        unfurl_links=False)
            return "donna"
        if self.helper is None:
            self.slack.chat_postMessage(channel=channel, text="I can answer account, route and garment questions here. "
                                        "The Alliant how-to notes aren't set up for me yet.")
            return "no notes"
        reply, missed = self.helper.answer([{"role": "user", "content": ev.get("text", "")}], user)
        if missed:
            reply += "\n\n_I've logged this one for Scott._"
        self.slack.chat_postMessage(channel=channel, text=reply[:3900], unfurl_links=False)
        return "how-to"

    def voice_messages(self, messages: list[dict]) -> str:
        """The same as voice(), for a conversation that brings its own history (ElevenLabs' agent, through webchat's
        /v1/chat/completions): the last user message is what was just said, the rest is the conversation so far."""
        def text_of(m):
            c = m.get("content")
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
        turns = [{"role": m["role"], "content": text_of(m)} for m in messages
                 if m.get("role") in ("user", "assistant") and text_of(m).strip()]
        if not turns or turns[-1]["role"] != "user":
            return "I'm here. What do you need?"
        convo = "agent-" + str(abs(hash(turns[0]["content"])))
        talk = self.__dict__.setdefault("talks", {}).setdefault(convo, {"turns": [], "ask": None, "at": time.time()})
        talk["turns"] = turns[:-1][-16:]
        while talk["turns"] and talk["turns"][0]["role"] == "assistant":
            talk["turns"].pop(0)
        return self.voice(turns[-1]["content"], convo)

    def voice(self, text: str, convo: str = "", user: str = "") -> str:
        """Scott talking to Donna on his iPhone (the Donna app, through webchat): the same Harvey mode as his private
        Slack messages, answered to be read aloud. Remembers the conversation 30 minutes. `user` is someone else Scott
        gave the app to (VOICE_KEYS, e.g. Bill): business only, i.e. lookups, balances (if AR_SLACK_IDS allows), changes
        and a plain chat, but no briefing on the team, no read of Slack and email, no Harvey mode."""
        from .parser import owner_chat
        print(time.strftime("%H:%M"), f"voice: heard {len(text.split())} words", flush=True)
        if not text.strip():  # the shortcut sent nothing: nothing was said, or its words aren't in the address
            return "I didn't catch anything. Tap the microphone on the keyboard, tell me what you need, then tap Done."
        talks = self.__dict__.setdefault("talks", {})
        now = time.time()
        for k in [k for k, t in talks.items() if now - t["at"] > 1800]:
            talks.pop(k)
        boss = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
        owner = user or boss  # who is talking (named `owner` for Scott, who it usually is)
        scott = owner in {u.strip() for u in os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")}
        talk = talks.setdefault((convo or "phone") if scott else f"{owner}:{convo or 'phone'}",
                                {"turns": [], "ask": None, "at": now})
        talk["at"] = now
        heard = []
        ask = talk["ask"]
        h = {"user": owner, "ts": f"{now:.6f}",
             "text": f"{ask['q']}\nDonna asked: {ask['said']}\nAnswer: {text}" if ask else text}
        talk["ask"] = None
        reply, kind = "", "chat"
        if not ask:
            said = self.owner_change(f"voice:{owner}:{convo or 'phone'}", text, None, user=owner)
            if said:
                reply, kind = spoken(said), "change"
                if convo.startswith("agent-tool") and reply.endswith("?"):  # ElevenLabs' agent: only ask_donna can make the change
                    reply += " (Voice agent: pass Scott's answer to ask_donna word for word; nothing changes until it does.)"
        if not reply and not ask:
            item = self.sku_answer(text, owner)
            if item:
                reply, kind = spoken(item), "sku"
        if not reply and not ask and getattr(self, "helper", None) and ALLIANT_Q.search(text):  # how to do it in Alliant
            how, _ = self.helper.answer([{"role": "user", "content": text + "\n\n(Spoken aloud: say it in a few short "
                                          "sentences, steps in order, no lists or formatting, skip the source line.)"}], owner)
            reply, kind = spoken(re.sub(r"\n*_?Source:.*$", "", how, flags=re.S)), "alliant how-to"
        route = None if reply else re.search(r"\b(?:route|rte|rt)\s*#?\s*(\d{1,2})\b", text, re.I)
        if route and not ask and not re.search(r"\b\d{4}-\d-\d{5}\b", text) \
                and re.search(r"\b(stops?|customers?|accounts?|order|deliver\w*|first|last|next|who|which|list)\b", text, re.I):
            # "what stops are on route 1 Thursday": straight to the route, no parse ("should we raise the route 4
            # minimum?" isn't a lookup and goes to Donna)
            from .context import route_facts
            from .parser import answer_lookup
            facts = route_facts(self.alliant, route.group(1))
            if facts:
                reply, kind = spoken(answer_lookup(self.claude, text, facts, asker=self.asker(owner)).answer), "route"
        if not reply and (ask or self.sounds_like_lookup(text)) and not (scott and PULSE.search(text)) \
                and self.office_lookup(h, [], channel="voice", say=lambda t, p: heard.append((t, p))) and heard:
            kind = "lookup"
            said, payload = heard[-1]
            reply = spoken(said)
            if payload.get("kind") == "office_ask":
                talk["ask"] = {"q": payload.get("q", text), "said": reply}
        if not reply:
            about = self.briefing() + ("\n\n" + self.pulse() if scott and PULSE.search(text) else "")
            reply = owner_chat(self.claude, talk["turns"][-16:] + [{"role": "user", "content": text}], about, spoken=True,
                               person="" if scott else self.name(owner))
        talk["turns"] += [{"role": "user", "content": text}, {"role": "assistant", "content": reply or "I'm here."}]
        print(time.strftime("%H:%M"), f"voice: answered ({kind}) in {time.time() - now:.1f}s", flush=True)
        return reply or "I'm here. What do you need?"

    def owner_change(self, key: str, text: str, channel: str | None, typed: dict | None = None,
                     user: str = "") -> str | None:
        """Scott tells Donna a change himself ("change Chili's bar mops to 60 a week"), by voice or in his private
        messages, for any account. She reads it back the way she would to a driver, or asks what's unclear; his "yes"
        puts a ticket in #service-desk (so the office sees it) straight into her Alliant queue, and her readback comes
        to his private messages when it's entered. None when it isn't a change (a question, a chat)."""
        from .parser import parse_message
        from .run import fill_item_matches, fix_department, other_route_account
        pending = self.__dict__.setdefault("changes", {})
        now = time.time()
        p = pending.get(key)
        if p is None and user:  # her workstation asked in his private messages: answered by voice (or the other way)
            other = next((k for k, v in pending.items() if v.get("user") == user and v.get("workstation")
                          and now - v["at"] < 7200), None)
            if other:
                p = pending[key] = pending.pop(other)
        if p and now - p["at"] > (900 if p.get("confirm") else 7200):
            p = pending.pop(key)
            p = None
        if p and not p.get("confirm") and not p.get("workstation") and CHANGE_Q.search(text) and len(text.split()) > 4 and (
                not YES.match(text) and not NO.match(text)):
            # "Can you add a mat to Homer Golf Course?" is a request of its own, not the answer to an old question
            pending.pop(key, None)
            p = None
        if p and p.get("confirm"):
            if YES.match(text):
                pending.pop(key)
                return self.owner_enter(p, channel, typed)
            if NO.match(text) and len(text.split()) <= 4:
                pending.pop(key)
                return "Okay, I won't change anything."
            text = f"{p['text']}\nCorrection: {text}"
        elif p:  # the answer to her question
            text = f"{p['text']}\nDonna asked: {p['asked']}\nThe answer: {text}"
        elif not CHANGE_Q.search(text):
            return None
        owner = (p or {}).get("user") or user or os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
        parsed = parse_message(self.claude, text, self.desk_name, f"{now:.6f}", self.alliant, author=self.name(owner),
                               office=True)
        if parsed.category not in ACTIONABLE:
            pending.pop(key, None)
            return None
        other_route_account(parsed, self.alliant)
        group = self.all_locations(text, parsed)
        if group:  # "all of the Safeway customers": the same change at each of their locations, one ticket each
            name, accounts = group
            parts = [parsed.model_copy(deep=True, update={"account_number": a, "customer_as_written": name})
                     for a in accounts]
        else:
            parts = split_by_account(parsed)
        for part in parts:
            fix_department(part, self.alliant, None, service_day(now))
            if not group:
                fill_item_matches(self.claude, part, self.alliant)
        results = [check(part, self.alliant) for part in parts]
        if group:  # only the locations that have the items
            keep = [i for i, r in enumerate(results) if r.changes and all(c.alliant_item for c in r.changes)]
            if not keep:
                pending.pop(key, None)
                return f"None of the {group[0]} locations have that in Alliant, so there's nothing to change."
            parts, results = [parts[i] for i in keep], [results[i] for i in keep]
        questions = list(dict.fromkeys(q for r in results for q in r.questions))
        if questions:
            asked = " ".join(questions[:2])
            if group and len(parts) > 1:  # one answer for every location
                asked = f"That's {len(parts)} {group[0]} locations. For all of them: " + re.sub(
                    r"^Stopping .*?: what", "what", questions[0]).capitalize()
            pending[key] = {"text": text, "asked": asked, "at": now, "user": owner, "key": key}
            return asked
        said = []
        for r in results:
            rb = re.sub(r"^✅\s*(\d{4}-\d-\d{5}\s*)?", "", readback(r)).strip()
            name, _, what = rb.partition(" – ")
            said.append(f"{string.capwords(name)}: {what}" if what else rb)
        if group and len(said) > 3:  # a read-back of 20 stores is too long to hear: the count and one example
            lines = sum(len(r.changes) for r in results)
            said = [f"{len(said)} {group[0]} locations, {lines} item lines in all. For example, {said[0]}; and the "
                    f"same at the other {len(said) - 1}"]
        pending[key] = {"text": text, "parts": parts, "results": results, "confirm": True, "at": now, "user": owner,
                        "key": key}
        return "; ".join(said) + ". Want me to put that in?"

    def all_locations(self, text: str, parsed) -> tuple[str, list[str]] | None:
        """"All of the Safeway customers / stores / locations": (the name, every account whose Alliant name has it), for
        Scott and Bill only (owner_change). None when the request isn't for all of a customer's locations."""
        said = f"{text}\n{parsed.customer_as_written or ''}"
        m = re.search(r"\b(?:all|every|each)\s+(?:of\s+)?(?:the\s+|our\s+)?((?:(?!\b(?:all|every|each)\b)[a-z0-9'&. -])+?)\s+"
                      r"(?:customers?|locations?|stores?|accounts?|sites?|departments?)\b", said, re.I)
        if not m:
            return None
        words = [w for w in re.findall(r"[a-z0-9']+", m.group(1).lower()) if w not in ("the", "of", "our", "all")]
        if not words:
            return None
        accounts = sorted(c.account for c in self.alliant.customers
                          if all(re.search(rf"\b{re.escape(w)}", c.name.lower()) for w in words))
        if len(accounts) < 2 or len(accounts) > 80:
            return None
        return string.capwords(" ".join(words)), accounts

    def owner_enter(self, p: dict, channel: str | None, typed: dict | None) -> str:
        """Scott (or Bill) said yes: the ticket(s) in #service-desk, queued for her workstation (unless DONNA_ALLIANT=off)."""
        owner = p["user"]
        first = self.name(owner).split()[0] if self.name(owner) else "Scott"
        if typed:
            src = {**typed, "user": owner, "text": p["text"].split("\n")[0]}
            where = f"{first}, in a private message"
        else:  # by voice: his private messages with her keep the record and get her readback. Posting to his user id
            # opens that conversation with the chat:write the app has (conversations.open would need im:write).
            texted = re.search(r":sms-(\d{10})$", p.get("key", ""))  # by text from his own phone (sms.py owner_text)
            r = _post(self.slack, channel or owner, f"{'📱 You texted me' if texted else '🎙️ You asked me'}: "
                      f"_{_short(p['text'].splitlines()[0], 300)}_", {"kind": "voice_change"})
            channel = r.get("channel") or channel or owner
            src = {"ts": r["ts"], "user": owner, "text": p["text"].splitlines()[0]}
            if texted:  # he hears back the way he asked: by text
                src["owner_sms"] = "+1" + texted.group(1)
            where = f"{first}, by {'text' if texted else 'voice'}"
        tickets = [self.post_ticket(where, channel, src, self.name(owner), part, r, [], False, [])
                   for part, r in zip(p["parts"], p["results"])]
        if os.environ.get("DONNA_ALLIANT", "off").strip().lower() == "off":
            return "It's on the desk for the office to enter. You'll get the confirmation here when it's in."
        for ts in tickets:
            self.jobs().add(ts, owner)
        return "Done. It's in my queue for Alliant, and I'll message you in Slack the moment it's entered."

    def sku_answer(self, text: str, user: str | None = None) -> str:
        """A question about an item itself ("how much does a bath blanket weigh?"), answered from skus.csv; empty if
        it isn't one. Without the file yet, says so rather than guessing."""
        from .checks import OPTIONS_Q, options_facts
        if OPTIONS_Q.search(text):  # "what colors do tablecloths come in?": the catalog's colors and sizes, no prices
            facts = options_facts(self.alliant, text)
            if facts:
                from .parser import answer_lookup
                return answer_lookup(self.claude, text, facts, asker=self.asker(user) if user else
                                     "office staff member").answer
        if not SKU_Q.search(text):
            return ""
        from .context import sku_facts
        if not getattr(self.alliant, "skus", None):
            return ("I don't have the SKU details (weights, sizes, materials) yet. They're coming in the Alliant feed "
                    "as skus.csv.")
        facts = sku_facts(self.alliant, text)
        if not facts:
            return ""
        from .parser import answer_lookup
        return answer_lookup(self.claude, text, "SKU details from Alliant (no prices):\n" + facts,
                             asker=self.asker(user) if user else "office staff member").answer

    def sounds_like_lookup(self, text: str) -> bool:
        """Worth the (slower) account lookup: a number, an account or route word, or a customer's name. Everything else
        goes straight to Donna, which is much quicker when you're talking."""
        low = text.lower()
        if re.search(r"\d|\bhow (many|much)\b|\b(account|route|stop|deliver\w*|garments?|wearers?|uniforms?|mats?|towels?|"
                     r"aprons?|mops?|napkins?|shirts?|pants|coats?|sheets?|linens?|items?|barcode)\b", low):
            return True
        words = set(re.findall(r"[a-z']{4,}", low))
        names = getattr(self, "_name_words", None)
        if names is None or names[0] is not self.alliant:
            common = {"the", "inc", "llc", "company", "service", "services", "store", "center", "alaska", "anchorage"}
            names = (self.alliant, {w for c in self.alliant.customers for w in re.findall(r"[a-z']{4,}", c.name.lower())} - common)
            self._name_words = names
        return bool(words & names[1])

    def serve_voice(self, port: int = 0) -> None:
        """Answer /voice on 127.0.0.1 for the website server (webchat.py), which checks Scott's key before passing it on."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        desk = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code: int, data: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # Donna's workstation asking for the next Alliant job
                if self.path != "/jobs/next":
                    return self.reply(404, b"{}")
                try:
                    job = desk.jobs().next()
                except Exception as e:
                    print(time.strftime("%H:%M"), "alliant jobs error:", e, flush=True)
                    return self.reply(500, b"{}")
                self.reply(200, json.dumps({"job": job}).encode())

            def do_POST(self):
                m = re.fullmatch(r"/jobs/([\d.]+)/result", self.path)
                if m:  # Donna's workstation reporting a finished job
                    try:
                        body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20000)) or b"{}")
                        out = desk.jobs().result(m.group(1), str(body.get("status") or ""), str(body.get("note") or ""),
                                                 str(body.get("readback") or ""), str(body.get("question") or ""))
                    except Exception as e:
                        print(time.strftime("%H:%M"), "alliant jobs error:", e, flush=True)
                        return self.reply(500, b"{}")
                    print(time.strftime("%H:%M"), "alliant jobs:", m.group(1), out, flush=True)
                    return self.reply(200, json.dumps({"ok": out}).encode())
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 200000)) or b"{}")
                    if self.path == "/chat":
                        out = desk.voice_messages(body.get("messages") or [])
                    else:
                        out = desk.voice(str(body.get("text") or "").strip()[:2000], str(body.get("convo") or ""),
                                         str(body.get("user") or ""))
                    code = 200
                except Exception as e:
                    print(time.strftime("%H:%M"), "voice error:", e, flush=True)
                    out, code = "Sorry, I lost you for a second. Say that again?", 200
                data = out.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = ThreadingHTTPServer(("127.0.0.1", port or VOICE_PORT), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(time.strftime("%H:%M"), f"voice on 127.0.0.1:{port or VOICE_PORT}", flush=True)

    def owner_turns(self, channel: str, ev: dict) -> list[dict]:
        """The last couple of hours of Scott's private conversation with the bot, so Donna remembers what he just said."""
        turns: list[dict] = []
        try:
            history = self.slack.conversations_history(channel=channel, oldest=str(time.time() - 7200), limit=30)
            msgs = list(reversed(history.get("messages", [])))
        except Exception:
            msgs = []
        for m in msgs[-20:]:
            if m.get("ts") == ev.get("ts") or not m.get("text"):
                continue
            role = "assistant" if m.get("bot_id") or m.get("user") == self.me else "user"
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] += "\n\n" + m["text"]
            else:
                turns.append({"role": role, "content": m["text"]})
        while turns and turns[0]["role"] == "assistant":
            turns.pop(0)
        if turns and turns[-1]["role"] == "user":
            turns[-1]["content"] += "\n\n" + ev.get("text", "")
        else:
            turns.append({"role": "user", "content": ev.get("text", "")})
        return turns

    def briefing(self) -> str:
        """A few lines about the business for Donna's talks with Scott: customers and routes per company, data age."""
        cs = self.alliant.customers
        out = []
        for branch, company in (("1", "Snow White Linen"), ("2", "The Laundry Group")):
            mine = [c for c in cs if c.account.split("-")[1:2] == [branch]]
            if mine:
                out.append(f"{company}: {len(mine)} customer locations on {len({c.route for c in mine})} routes")
        if self.alliant.as_of:
            out.append(f"Alliant data as of {self.alliant.as_of}")
        out.append("You (Donna, the Service Desk bot) handle route-channel changes, customer texts (Twilio), service@ email (Bird), "
                   "the website chat and /quote page, and the morning stockroom garment check.")
        return "\n".join(out)

    def pulse(self, hours: float = 48, limit: int = 60_000) -> str:
        """What's been said in every channel the bot is in, for Donna's read on the operation: the team's own words,
        and customers' texts, emails and website chats as they reached the office channel. Never private messages."""
        since = time.time() - hours * 3600
        out = []
        for name, cid in sorted(self.channels.items(), key=lambda kv: kv[0] != self.desk_name):
            lines = []
            try:
                for m in _messages(self.slack, cid, since):
                    if m.get("subtype") not in (None, "thread_broadcast", "bot_message"):
                        continue
                    text = todo_bar.body(m) if not m.get("text") else m["text"]
                    who = "Donna" if _is_bot(m, self.me) else self.name(m.get("user"))
                    t = datetime.fromtimestamp(float(m["ts"]), ALASKA).strftime("%a %-I:%M %p")
                    lines.append(f"[{t}] {who}: {_short(text, 400)}" + (f" ({m['reply_count']} replies)" if m.get("reply_count") else ""))
            except Exception as e:
                print(time.strftime("%H:%M"), f"pulse: #{name} unread:", e, flush=True)
                continue
            if lines:
                out.append(f"#{name}:\n" + "\n".join(reversed(lines[:150])))
        text = "\n\n".join(out)
        if len(text) > limit:
            text = text[:limit] + "\n[...more not shown]"
        return (f"WHAT'S BEEN SAID in the last {hours:.0f} hours, in every Slack channel you're in (customer texts, "
                "emails and website chats show up as your (Donna's) posts in #" + self.desk_name + "). Give Scott your "
                "honest read: what's going well (credit people by name), what could be better and what you'd do about "
                "it, and anything he should step into. Upbeat, but tell him the truth. Talk about how the operation is "
                "running, not people's private lives; nobody gets singled out over being sick.\n\n"
                + (text or "(nothing posted)"))

    def owner_brief_pass(self, now: float) -> None:
        """Weekdays from OWNER_BRIEF_HOUR, once a day: Donna's morning read of the last day, in Scott's private messages."""
        local = datetime.fromtimestamp(now, ALASKA)
        if local.weekday() > 4 or local.hour < BRIEF_HOUR or os.environ.get("OWNER_BRIEF", "on").lower() == "off":
            return
        mark = os.path.join(self.data_dir, "owner_brief.txt")
        day = local.strftime("%Y-%m-%d")
        try:
            if open(mark).read().strip() == day:
                return
        except OSError:
            pass
        with open(mark, "w") as f:  # first, so a failure below can't send twice
            f.write(day)
        owner = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()

        def send():
            try:
                from .parser import owner_chat
                hours = 72 if local.weekday() == 0 else 24  # Monday covers the weekend
                ask = ("Good morning. Give me my morning brief: the few things that matter today, what went well, what "
                       "could be better, and your honest read on how things are going. Keep it tight.")
                reply = owner_chat(self.claude, [{"role": "user", "content": ask}],
                                   self.briefing() + "\n\n" + self.pulse(hours))
                # A user id as the channel lands in Scott's private messages with the bot (no im:write scope needed)
                self.slack.chat_postMessage(channel=owner, text=f"☕ *Morning brief*\n{reply}"[:3900], unfurl_links=False)
                print(time.strftime("%H:%M"), "owner brief sent", flush=True)
            except Exception as e:
                print(time.strftime("%H:%M"), "owner brief error:", e, flush=True)
        import threading
        threading.Thread(target=send, daemon=True).start()

    def office_group(self, text: str, parsed) -> list[str]:
        """Every location under an account number ("4000", "all of 4000") or every customer whose name has all the
        words given ("all the Safeways"), when the question isn't about one location. Empty otherwise."""
        full = re.search(r"\b\d{4}-\d-\d{5}\b", text)  # a full account number means that one location
        if full and not re.search(r"\b(all|every|each|total|combined)\b", text, re.I):
            return []
        every = re.search(r"\b(all|every|each|total|combined)\b", text, re.I)
        one = (parsed.account_number or "").split("-")[0]
        for base in re.findall(r"(?<![\d-])(\d{4})(?![\d-])", text):  # a bare 4-digit number: every location under it
            if one and base != one and not every:
                continue  # "Safeway 1807": a store number, and the parser already found the one account
            hits = [c.account for c in self.alliant.customers if c.account.split("-")[0] == base]
            if len(hits) >= 2:
                return hits
        words = re.findall(r"[a-z0-9]+", (parsed.customer_as_written or "").lower())
        words = [w.rstrip("s") if len(w) > 3 else w for w in words if w not in ("all", "the", "every", "of")]
        if words and every:
            hits = [c.account for c in self.alliant.customers
                    if all(any(n.startswith(w) for n in re.findall(r"[a-z0-9]+", c.name.lower())) for w in words)]
            if len(hits) >= 2:
                return hits
        return []

    def send_readback(self, t: dict, user: str, notes: list[str]) -> None:
        p = meta(t)
        who = self.name(user)
        msg = (f"<@{p['driver_id']}> " if p.get("driver_id") else "") + (p.get("readback") or "✅ Done") + f" – entered by {who}"
        msg += "".join(f"\nOffice note: {n}" for n in notes)
        customer = customer_readback(p.get("readback") or "", who, notes)  # what a customer reads (Donna's voice)
        if p.get("sms_to"):  # a customer's text (sms.py): the readback goes back by text, not to Slack
            from .sms import send_sms
            from .context import company_for
            send_sms(p["sms_to"], f"{company_for(p.get('account'))}: " + customer)
        elif p.get("email_to"):  # a customer's email (email_desk.py): the readback is a reply in their thread
            from . import bird
            from .sms import plain
            from .context import company_for
            company = company_for(p.get("account"))
            subject = p.get("email_subject") or f"Your {company} request"
            bird.send(p["email_to"], subject if subject.lower().startswith("re:") else "Re: " + subject,
                      plain(customer) + "\n\n" + bird.signature(company), in_reply_to=p.get("email_msgid", ""), company=company,
                      key=f"readback/{t['ts']}")
        elif p.get("owner_sms"):  # Scott texted it in from his own phone: the readback goes back by text
            from .sms import send_sms
            send_sms(p["owner_sms"], "Donna: " + re.sub(r"^<@\w+>\s*", "", msg))
        else:
            self.post(p["src_channel"], msg, {"kind": "readback", "src_ts": p["src_ts"], "driver_id": p.get("driver_id"),
                                              "msgs": p.get("msgs", [])})
        try:
            self.change_email(p, customer)
        except Exception as e:  # the email is extra: never hold up the ticket over it
            print(time.strftime("%H:%M"), "change email not sent:", e, flush=True)
        self.slack.chat_update(channel=self.desk_id, ts=t["ts"], text="",
                               attachments=shrink(self.slack, self.desk_id, t, GREEN, stamp(who, "Entered") + " · readback sent"),
                               metadata={"event_type": KIND, "event_payload": {**p, "kind": "done", "done_at": time.time(),
                                                                               "entered_by": who}})

    def ask_requester(self, t: dict, question: str) -> str | None:
        """Donna (at her Alliant workstation) can't tell what was meant: ask whoever asked, the same way she asks a
        driver. A driver gets a Quick check in the route channel; a customer a text or an email whose answer the text
        or email desk reads together with the request (<word>_asks.json). Their answer makes the updated ticket.
        Returns "driver" or "customer", or None when there's nobody to ask."""
        p = meta(t) or {}
        q = re.sub(r"\s+", " ", question).strip()[:400]
        said = [re.sub(r"^(&gt;|>)\s*", "", ln).strip() for ln in todo_bar.body(t).splitlines()
                if re.match(r"^(&gt;|>)\s*", ln.strip())]
        if p.get("sms_to") or p.get("email_to"):
            from .context import company_for
            word, to = ("text", p["sms_to"]) if p.get("sms_to") else ("email", p["email_to"])
            company = company_for(p.get("account"))
            if word == "text":
                from .sms import send_sms
                send_sms(to, f"{company}: Happy to! Just one quick question on your request: {q}")
            else:
                from . import bird
                subject = p.get("email_subject") or f"Your {company} request"
                bird.send(to, subject if subject.lower().startswith("re:") else "Re: " + subject,
                          f"Happy to! Just one quick question on your request: {q}\n\n" + bird.signature(company), company=company,
                          in_reply_to=p.get("email_msgid", ""))
            path = os.path.join(self.data_dir, f"{word}_asks.json")
            try:
                waiting = json.load(open(path))
            except (OSError, ValueError):
                waiting = []
            lines = [ln if re.match(r"^(Customer|Snow White asked):", ln) else f"Customer: {ln}" for ln in said]
            waiting.append({"to": to, "lines": lines + [f"Snow White asked: {q}"], "at": time.time()})
            with open(path + ".tmp", "w") as f:
                json.dump(waiting, f)
            os.replace(path + ".tmp", path)
            return "customer"
        if str(p.get("src_channel", "")).startswith("D"):  # Scott told her himself (owner_change): ask him there
            self.post(p["src_channel"], f"Quick check from Donna: {q}", {"kind": "question", "src_ts": p.get("src_ts")})
            if p.get("owner_sms"):  # he texted it in: ask by text too (his answer finds this question by his user id)
                from .sms import send_sms
                send_sms(p["owner_sms"], f"Donna: Quick check: {q}")
            # the ticket's own lines too: they name the exact Alliant items ("MAT CONFETTI GREY 3X10"), which the
            # request alone ("stop all the mats") doesn't, so the answer is read against the right items
            lines = [re.sub(r"[*`_]", "", re.sub(r":[a-z_]+:\s*", "", ln)).strip() for ln in todo_bar.body(t).splitlines()
                     if re.match(r"^\s*(?::[a-z_]+:|[➕➖🔢⛔🔄📏✏️])", ln)]
            text = (" ".join(said) or todo_bar.body(t)) + ("\nThe ticket for it: " + "; ".join(lines) if lines else "")
            self.__dict__.setdefault("changes", {})[p["src_channel"]] = {
                "text": text, "asked": q, "at": time.time(), "user": p.get("driver_id"), "workstation": True}
            return self.name(p.get("driver_id")).split()[0] if p.get("driver_id") else "owner"
        if p.get("src_channel") in self.routes.values() and p.get("src_ts"):
            who = f"<@{p['driver_id']}> " if p.get("driver_id") else ""
            self.post(p["src_channel"], f"{who}Quick check from Donna on _{_short(' '.join(said) or 'your request', 60)}_\n• {q}",
                      {"kind": "question", "src_ts": p["src_ts"], "driver_id": p.get("driver_id"), "msgs": p.get("msgs", [])})
            return "driver"
        return None

    def change_email(self, p: dict, customer: str) -> list[str]:
        """The account's invoice contact (who pays the bills; the Alliant card's email) gets an email confirming the
        change. CHANGE_EMAILS: "all" (default: every change, whether the customer, a driver, the office or Scott asked),
        "customer" (only changes the customer asked for by text, email or phone) or "off". Returns who it went to."""
        mode = os.environ.get("CHANGE_EMAILS", "all").lower()
        src, acct = p.get("src_channel", ""), p.get("account", "")
        if mode == "off" or not acct or not customer or (mode != "all" and src not in ("text", "email", "phone")):
            return []
        from .context import company_for
        from .sms import plain
        card = self.alliant.cards.get(acct, {})
        already = (p.get("email_to") or "").lower()  # an emailed request got its readback in its own thread
        to = [e for e in dict.fromkeys(x.strip().lower() for x in re.split(r"[;,\s]+", card.get("email") or "")
                                       if re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", x.strip()))
              if e != already]
        if not to:
            return []
        name = next((c.name for c in self.alliant.customers if c.account == acct), card.get("name", acct)).title()
        company = company_for(acct)
        what = re.sub(r"^All done!\s*", "", plain(customer).split("\nThanks")[0]).strip()
        how = {"text": "by text", "email": "by email", "phone": "by phone"}.get(src, "through our team")
        from . import bird
        for e in to:
            bird.send(e, f"{company}: a change to your service for {name}",
                      f"Hello,\n\nThis confirms a change to your {company} service for {name} ({acct}), requested {how} "
                      f"and entered today:\n\n{what}\n\nIf this doesn't look right, just reply to this email or call us at "
                      f"(907) 222-7738.\n\nThank you,\n\n" + bird.signature(company), key=f"change/{acct}/{p.get('src_ts', '')}",
                      company=company)
        print(time.strftime("%H:%M"), f"change email sent for {acct} to {len(to)} invoice contact(s)", flush=True)
        return to

    def verify_pass(self, now: float) -> None:
        """The morning after tickets are ✅'d, check them against the new Alliant export (verify.py) and tell the
        office about any that don't match. Once per export, from 7 AM; each ticket is checked once."""
        from .verify import problems
        exported = self.alliant.exported_at
        if exported is None or datetime.fromtimestamp(now, ALASKA).hour < VERIFY_HOUR \
                or getattr(self, "verified_feed", None) == exported.timestamp():
            return
        flagged = []
        for t in _messages(self.slack, self.desk_id, now - 8 * 86400, include_all_metadata=True):
            p = meta(t) or {}
            if p.get("kind") != "done" or p.get("verified") or not p.get("expect") or not p.get("account") \
                    or not p.get("done_at") or p["done_at"] > exported.timestamp():
                continue
            found = problems(self.alliant, p["account"], p["expect"])
            looks = {"text": t["text"]} if t.get("text") else {"text": "", "attachments": t.get("attachments")}
            if found:  # back to gold: the entry needs fixing
                looks = {"text": "", "attachments": restyle(t, GOLD, status=f":warning: *Doesn't match Alliant*: "
                                                                             f"{'; '.join(found)}")}
            self.slack.chat_update(channel=self.desk_id, ts=t["ts"], **looks, metadata={
                "event_type": KIND, "event_payload": {**p, "verified": "flagged" if found else "ok"}})
            if found:
                flagged.append((t, p, found))
        if flagged:
            lines = [f"🔎 *Entry check*: {len(flagged)} ticket{'s' if len(flagged) > 1 else ''} from the last few days "
                     "don't match Alliant this morning. Please fix them in Alliant:"]
            for t, p, found in flagged:
                link = self.slack.chat_getPermalink(channel=self.desk_id, message_ts=t["ts"])["permalink"]
                cust = next((c.name for c in self.alliant.customers if c.account == p["account"]), "")
                by = f" · entered by {p['entered_by']}" if p.get("entered_by") else ""
                lines.append(f"• <{link}|{p['account']} {cust}>{by}\n    " + "\n    ".join(found))
            self.post(self.desk_id, "\n".join(lines), {"kind": "entry_check"}, todo="button")
        self.verified_feed = exported.timestamp()

    def stockroom_pass(self, now: float) -> None:
        """Mondays from 7 AM, once a week: post the garment check (garment_check.py) in #stockroom: one short line in
        the channel, tagging the stockroom manager, and the list in its thread, so others in the channel aren't flooded."""
        cid = self.channels.get(STOCKROOM)
        local = datetime.fromtimestamp(now, ALASKA)
        if not cid or local.weekday() != STOCKROOM_DAY or local.hour < VERIFY_HOUR:
            return
        week = local.strftime("%G-W%V")
        mark = os.path.join(self.data_dir, "stockroom_posted.txt")
        try:
            if open(mark).read().strip() == week:
                return
        except OSError:
            pass
        with open(mark, "w") as f:  # first, so a failure below can't post twice
            f.write(week)
        from .alliant_help import find_user
        from .garment_check import findings
        rows = findings(self.data_dir, self.alliant)
        if not hasattr(self, "stockroom_lead"):
            self.stockroom_lead = find_user(self.slack, STOCKROOM_LEAD)
        tag = f"<@{self.stockroom_lead}> " if self.stockroom_lead else ""
        as_of = f" (Alliant as of {self.alliant.as_of})" if self.alliant.as_of else ""
        if not rows:
            self.post(cid, f"🧺 *Weekly garment check*{as_of}: ✅ every wearer's garment inventory matches its auto count "
                           "(or double it), or the difference is on order.", {"kind": "stockroom_check", "count": 0})
            return
        from .garment_check import by_account
        unset = by_account([r for r in rows if r["auto_count_unset"]])  # auto count 0 on the whole account: setup or COG
        rows = [r for r in rows if not r["auto_count_unset"]]
        groups = by_account(rows)
        head = self.post(cid, f"🧺 {tag}*Weekly garment check*{as_of}: *{len(rows)} wearer line{'s' if len(rows) != 1 else ''}* "
                              f"at *{len(groups)} account{'s' if len(groups) != 1 else ''}* where the inventory doesn't match "
                              "the auto count (not equal or double), with nothing on order. Biggest differences first, "
                              "in the thread 👇",
                         {"kind": "stockroom_check", "count": len(rows)}, todo="button")
        msgs, cur = [], []
        for acct, cust, rs in groups:
            block = [f"*{acct}* {cust}"] + [
                f"    #{r['wearer']} {r['name']} · {r['item']} {r['size']}: inventory {r['inventory']}, auto count "
                f"{r['auto_count']}" + (f", on order {r['on_order']}" if r["on_order"] else "") for r in rs[:8]]
            if len(rs) > 8:
                block.append(f"    …and {len(rs) - 8} more wearer lines")
            if cur and len("\n".join(cur + block)) > 3500:
                msgs.append(cur)
                cur = []
            cur += block
        if cur:
            msgs.append(cur)
        shown = msgs[:12]
        for m in shown:
            self.post(cid, "\n".join(m)[:3900], {"kind": "stockroom_list"}, thread_ts=head["ts"])
        left = len(groups) - sum(1 for m in shown for line in m if line.startswith("*"))
        if unset:
            self.post(cid, "*Auto count is 0 on every garment* (not set up yet, or a COG customer that owns its "
                           "garments; tell Scott which, and COG accounts are left out from then on):\n" + "\n".join(
                               f"• *{a}* {c}: {len({r['wearer'] for r in rs})} wearers" for a, c, rs in unset),
                      {"kind": "stockroom_list"}, thread_ts=head["ts"])
        self.post(cid, (f"…and {left} more accounts (full list on the server: `python -m service_changes.garment_check`)\n"
                        if left > 0 else "") + "For each: set the inventory equal to the auto count (or double it), or "
                  "fix the auto count, whichever is wrong. Not included: customer-owned (NOG) garments, "
                  "COG customers and The Laundry Group's hospital scrub pools.",
                  {"kind": "stockroom_list"}, thread_ts=head["ts"])

    # ---- one pass -------------------------------------------------------------------------------------------

    def check_stale(self, now: float) -> None:
        """Tell the office when the Alliant data is over STALE_HOURS old (once, then daily), and when it's back."""
        exported = self.alliant.exported_at
        if exported is None:
            return
        age = (now - exported.timestamp()) / 3600
        if age > STALE_HOURS:
            if self.stale_warned is None:  # after a restart: was the office already told today?
                posts = [m for m in _messages(self.slack, self.desk_id, now - 86400, include_all_metadata=True)
                         if _is_bot(m, self.me) and (meta(m) or {}).get("kind") == "stale_data"]
                back = max((float(m["ts"]) for m in posts if "current again" in m.get("text", "")), default=0.0)
                warned = [float(m["ts"]) for m in posts if "current again" not in m.get("text", "") and float(m["ts"]) > back]
                if warned:
                    self.stale_warned = max(warned)
            if self.stale_warned is None or now - self.stale_warned > 86400:
                when = exported.astimezone(ALASKA).strftime("%a %b %-d %-I:%M %p")
                self.post(self.desk_id, f"⚠️ *Alliant data is {age:.0f} hours old* (last export {when}). The nightly feed "
                                        "from the plant Mac mini may have stopped, e.g. the Mac restarted and is waiting "
                                        "for someone to log in. Counts in answers and tickets are as of that export.",
                          {"kind": "stale_data"})
                self.stale_warned = now
        elif self.stale_warned is not None:
            self.post(self.desk_id, "✅ Alliant data is current again.", {"kind": "stale_data"})
            self.stale_warned = None

    def run_once(self, since_minutes: int = 4320, ticket_days: int = 14, only: set[str] | None = None) -> dict:
        """One pass over the office channel and the route channels (or just the ones in `only`)."""
        now = time.time()
        counts = {"asked": 0, "ticket": 0, "read": 0, "readback": 0, "answered": 0}
        self.check_stale(now)
        self.verify_pass(now)
        self.notices_pass()
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
        # (channel, ts of a new top-level driver message or None, attempt). Slack can announce a message a moment
        # before conversations.history returns it; a pass that didn't see it tries again a second later.
        todo: "queue.Queue[tuple[str, str | None, int]]" = queue.Queue()
        watched = set(self.routes.values()) | {self.desk_id}

        def on_event(client, req):
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            if req.type == "interactive" and req.payload.get("type") == "block_actions" and any(
                    x.get("action_id") in (todo_bar.DONE, todo_bar.UNDO) for x in req.payload.get("actions", [])):
                # ✓ Done / Undo on a to-do message (todo.py): green with who and when, or back to gold
                if req.payload.get("user", {}).get("id") != self.me:
                    try:
                        print(time.strftime("%H:%M"), "todo:", todo_bar.handle_action(quick, req.payload, self.name), flush=True)
                    except Exception as e:
                        print(time.strftime("%H:%M"), "todo error:", e, flush=True)
                return
            if req.type == "interactive" and req.payload.get("type") == "block_actions" and any(
                    x.get("action_id") == "donna_enter" for x in req.payload.get("actions", [])):
                def enter(p=req.payload):  # "🤖 Donna, enter it" on a ticket (alliant_jobs.py)
                    try:
                        print(time.strftime("%H:%M"), "alliant jobs:", self.jobs().queue(p), flush=True)
                    except Exception as e:
                        print(time.strftime("%H:%M"), "alliant jobs error:", e, flush=True)
                threading.Thread(target=enter, daemon=True).start()
                return
            if req.type == "interactive" and req.payload.get("type") == "block_actions" and any(
                    x.get("action_id", "").startswith("link_") for x in req.payload.get("actions", [])):
                def link(p=req.payload):  # "Add to Chili's" / "Move to …" on a number or email sign-up card
                    try:
                        print(time.strftime("%H:%M"), "link:", self.link_contact(p), flush=True)
                    except Exception as e:
                        print(time.strftime("%H:%M"), "link error:", e, flush=True)
                threading.Thread(target=link, daemon=True).start()
                return
            if req.type == "interactive" and req.payload.get("type") == "block_actions" and any(
                    x.get("action_id", "").startswith("announce_") for x in req.payload.get("actions", [])):
                def announce(p=req.payload):  # Send / Cancel on Donna's message to customers (announce.py)
                    try:
                        print(time.strftime("%H:%M"), "announce:", self.announcer().act(p), flush=True)
                    except Exception as e:
                        print(time.strftime("%H:%M"), "announce error:", e, flush=True)
                threading.Thread(target=announce, daemon=True).start()
                return
            if req.type == "interactive" and req.payload.get("type") == "block_actions":
                # "Give to Bill/Jesse" on a website lead in #sales-desk: create the Salesforce Account and Opportunity (sales.py)
                from . import sales
                def give(p=req.payload):
                    try:
                        print(time.strftime("%H:%M"), "sales:", sales.handle_action(quick, p, self.data_dir), flush=True)
                    except Exception as e:
                        print(time.strftime("%H:%M"), "sales error:", e, flush=True)
                threading.Thread(target=give, daemon=True).start()
                return
            if req.type != "events_api":
                return
            ev = req.payload.get("event", {})
            channel = ev.get("channel") or ev.get("item", {}).get("channel")
            if ev.get("type") == "message" and ev.get("channel_type") == "im" and not ev.get("bot_id") \
                    and ev.get("subtype") is None and ev.get("user") not in (None, self.me):
                def private(e=ev):
                    try:
                        print(time.strftime("%H:%M"), "dm:", self.dm(e), flush=True)
                    except Exception as x:
                        print(time.strftime("%H:%M"), "dm error:", x, flush=True)
                        self.api_trouble(x)
                threading.Thread(target=private, daemon=True).start()
                return
            if self.helper and channel == self.helper.channel:
                if self.helper.wants(ev):
                    def ask(e=ev):
                        try:
                            print(time.strftime("%H:%M"), "alliant-help:", self.helper.handle(e), flush=True)
                        except Exception as x:
                            print(time.strftime("%H:%M"), "alliant-help error:", x, flush=True)
                            self.api_trouble(x)
                    threading.Thread(target=ask, daemon=True).start()
                return
            if channel not in watched:
                return
            if ev.get("type") == "message" and not ev.get("bot_id") and ev.get("subtype") in (None, "thread_broadcast", "file_share") \
                    and channel != self.desk_id and ev.get("user") != self.me \
                    and not (ev.get("thread_ts") not in (None, ev.get("ts")) and self.is_staff(ev.get("user"))):
                try:
                    quick.reactions_add(channel=channel, timestamp=ev["ts"], name=WORKING)
                except Exception:
                    pass
            if ev.get("type") == "reaction_added" and channel == self.desk_id and ev.get("user") != self.me \
                    and ev.get("reaction") in DONE_REACTIONS:
                self.done_events[ev.get("item", {}).get("ts")] = ev["user"]
                print(time.strftime("%H:%M"), "✅ from", ev["user"], flush=True)
            if ev.get("type") == "reaction_removed" and channel == self.desk_id:  # tapped ✅ by mistake
                self.done_events.pop(ev.get("item", {}).get("ts"), None)
            own_ticket = (channel == self.desk_id and ev.get("type") == "message" and ev.get("subtype") is None
                          and ev.get("user") == self.me and not ev.get("thread_ts"))  # texts/email/phone posting one
            if ev.get("type") in ("message", "reaction_added") and (ev.get("user") != self.me or own_ticket):
                new_top = (ev.get("type") == "message" and ev.get("subtype") in (None, "file_share") and channel != self.desk_id
                           and ev.get("thread_ts") in (None, ev.get("ts")))
                todo.put((channel, ev.get("ts") if new_top else None, 0))

        sm = SocketModeClient(app_token=app_token, web_client=quick)
        sm.socket_mode_request_listeners.append(on_event)
        sm.connect()
        print(time.strftime("%H:%M"), "listening", flush=True)
        self.serve(todo, since_minutes, backup_seconds, reload)

    def api_trouble(self, e: Exception) -> None:
        """Out of API credit, or a bad key: nothing gets read until someone fixes it, so tell the office (every 3
        hours at most). Posts wait with ⏳ and are handled by the next pass once it works again."""
        import anthropic
        if not isinstance(e, anthropic.APIStatusError) or e.status_code not in (400, 401, 402, 403):
            return
        if time.time() - getattr(self, "api_warned", 0) < 3 * 3600:
            return
        self.api_warned = time.time()
        why = "the Anthropic account is out of credit" if "credit" in str(e).lower() else f"Claude refused the request ({e.status_code})"
        try:
            self.post(self.desk_id, f"⚠️ *Donna can't read driver posts right now*: {why}. Posts are waiting with ⏳ "
                      "and will be handled automatically once it's fixed. Please tell Scotty.", {"kind": "api_trouble"})
        except Exception:
            pass

    def serve(self, todo, since_minutes: int, backup_seconds: int, reload, workers: int = 8, stop=None) -> None:
        """Run passes for the queued channels in parallel, one pass per channel at a time: drivers on different
        routes don't wait for each other. The office pass (✅ readbacks, open tickets) runs under one lock so
        nothing is sent twice. An event for a channel that's mid-pass makes it run once more right after."""
        import queue
        import threading
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=workers)
        desk_lock, state = threading.Lock(), threading.Lock()
        running: set[str] = set()
        waiting: dict[str, list] = {}  # channel -> items that arrived while its pass was running
        names = {cid: name for name, cid in self.routes.items()}
        fresh: dict = {"open": {}, "at": 0.0}  # the latest office pass: open tickets, and when

        def job(ch: str, items: list) -> None:
            try:
                counts = {"asked": 0, "ticket": 0, "read": 0, "readback": 0, "answered": 0}
                now = time.time()
                with desk_lock:
                    self.check_stale(now)
                    self.verify_pass(now)
                    self.stockroom_pass(now)
                    self.owner_brief_pass(now)
                    self.notices_pass()
                    # Route passes share an office pass from the last few seconds: during a burst of drivers they
                    # don't queue up behind each other here. Office events (✅, "done") always get a fresh one.
                    if ch not in names or time.time() - fresh["at"] > 3:
                        fresh["open"], fresh["at"] = self.desk_pass(now - 14 * 86400, counts), time.time()
                    open_t = fresh["open"]
                if ch in names:
                    self.route_pass(names[ch], ch, now - since_minutes * 60, open_t, counts)
                for c, ts, attempt in items:  # announced but not returned by history yet: look again shortly
                    if ts and ts not in self.fetched.get(c, set()) and attempt < 10:
                        threading.Timer(1.0, todo.put, [(c, ts, attempt + 1)]).start()
                    elif not ts and attempt == 0:
                        # A ✅, an office "done" or a thread reply can't be checked the same way: look a few more times.
                        for delay in (2.0, 6.0, 20.0):
                            threading.Timer(delay, todo.put, [(c, None, 1)]).start()
                if any(counts[k] for k in ("asked", "ticket", "readback", "answered")):
                    where = f"#{names[ch]}" if ch in names else "office"
                    print(time.strftime("%H:%M"), where + ":", ", ".join(f"{v} {k}" for k, v in counts.items()), flush=True)
            except Exception as e:  # keep going through a Slack or API hiccup
                print(time.strftime("%H:%M"), "error:", e, flush=True)
                self.api_trouble(e)
            finally:
                with state:
                    again = waiting.pop(ch, None)
                    if again is None:
                        running.discard(ch)
                if again is not None:
                    pool.submit(job, ch, again)

        def submit(ch: str, items: list) -> None:
            with state:
                if ch in running:
                    waiting.setdefault(ch, []).extend(items)
                    return
                running.add(ch)
            pool.submit(job, ch, items)

        last_full = 0.0
        while not (stop and stop.is_set()):
            try:
                items = [todo.get(timeout=1 if stop else 5)]
            except queue.Empty:
                items = []
            while not todo.empty():
                items.append(todo.get_nowait())
            if time.time() - last_full > backup_seconds:  # backup pass over every channel
                last_full = time.time()
                for ch in list(names) + [self.desk_id]:
                    items.append((ch, None, 1))
            if not items:
                continue
            try:
                reload()
            except Exception as e:
                print(time.strftime("%H:%M"), "error:", e, flush=True)
            by_channel: dict[str, list] = {}
            for it in items:
                by_channel.setdefault(it[0], []).append(it)
            for ch, its in by_channel.items():
                submit(ch, its)
        pool.shutdown(wait=True)

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
    desk.start_from(os.path.join(args.data, "first_watched.json"))
    desk.data_dir = args.data
    from . import alliant_help
    # Set up even before the bot is invited: Slack starts sending the channel's messages the moment it joins.
    # The notes answer how-to questions in #alliant-help (when the bot is in it), in private messages and by voice.
    desk.helper = alliant_help.Helper(slack, claude, args.data, desk.all_channels.get(alliant_help.CHANNEL), desk.me,
                                      alliant_help.find_user(slack, alliant_help.ESCALATE))
    from .context import feed_stamp
    loaded = [time.time(), feed_stamp(args.data)]

    def reload():  # pick up a fresh export (hourly) without restarting
        from .context import feed_stamp
        if feed_stamp(args.data) != loaded[1] or time.time() - loaded[0] > 3600:
            loaded[1] = feed_stamp(args.data)
            desk.alliant, loaded[0] = Alliant.from_dir(args.data), time.time()

    if args.listen:
        if os.environ.get("OWNER_VOICE_TOKEN") or os.environ.get("DONNA_WORKER_TOKEN"):
            desk.serve_voice()
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

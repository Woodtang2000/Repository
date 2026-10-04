"""The driver -> question -> ticket -> ✅ -> readback loop against a fake Slack, with the Claude reading faked."""
import time

import pytest

from . import desk
from .context import Alliant, Customer
from .schema import Action, Category, Change, ParsedMessage


class FakeSlack:
    """Just enough of Slack: channels, threads, reactions and message metadata."""

    def __init__(self):
        self.msgs = {"C1": [], "D1": []}
        self.clock = time.time() - 3600
        self.users = {"U1": "Mike Driver", "U2": "Sonja Burke", "UBOT": "Service Desk"}

    def _ts(self):
        self.clock += 1
        return f"{self.clock:.6f}"

    def say(self, channel, user, text, thread_ts=None, **extra):
        m = {"ts": self._ts(), "user": user, "text": text, **extra}
        if thread_ts:
            m["thread_ts"] = thread_ts
            parent = self.find(channel, thread_ts)
            parent["thread_ts"] = thread_ts
            parent["reply_count"] = parent.get("reply_count", 0) + 1
            parent["latest_reply"] = m["ts"]
        self.msgs[channel].append(m)
        return m

    def find(self, channel, ts):
        return next(m for m in self.msgs[channel] if m["ts"] == ts)

    def react(self, channel, ts, user, name):
        m = self.find(channel, ts)
        r = next((r for r in m.setdefault("reactions", []) if r["name"] == name), None)
        if r is None:
            m["reactions"].append({"name": name, "users": [user]})
        elif user in r["users"]:
            raise Exception("already_reacted")
        else:
            r["users"].append(user)

    def thread_of(self, channel, ts):
        return [m for m in self.msgs[channel] if m["ts"] == ts or m.get("thread_ts") == ts and m["ts"] != ts]

    # --- Slack Web API surface used by desk.py ---
    def auth_test(self):
        return {"user_id": "UBOT"}

    def conversations_list(self, **kw):
        return {"channels": [{"name": "route-1-test", "id": "C1", "is_member": True},
                             {"name": "service-desk-test", "id": "D1", "is_member": True}]}

    def conversations_history(self, channel, oldest, **kw):
        top = [m for m in self.msgs[channel] if m.get("thread_ts") in (None, m["ts"]) and float(m["ts"]) >= float(oldest)]
        return {"messages": list(reversed(top))}

    def conversations_replies(self, channel, ts, **kw):
        return {"messages": self.thread_of(channel, ts)}

    def users_info(self, user):
        return {"user": {"real_name": self.users[user]}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack/{channel}/{message_ts}"}

    def chat_postMessage(self, channel, text, thread_ts=None, metadata=None, reply_broadcast=False, **kw):
        return self.say(channel, "UBOT", text, thread_ts=thread_ts, bot_id="B", metadata=metadata, broadcast=reply_broadcast)

    def chat_update(self, channel, ts, text, metadata=None, **kw):
        m = self.find(channel, ts)
        m["text"] = text
        if metadata:
            m["metadata"] = metadata

    def reactions_add(self, channel, timestamp, name):
        self.react(channel, timestamp, "UBOT", name)

    def reactions_remove(self, channel, timestamp, name):
        m = self.find(channel, timestamp)
        r = next((r for r in m.get("reactions", []) if r["name"] == name and "UBOT" in r["users"]), None)
        if r is None:
            raise Exception("no_reaction")
        r["users"].remove("UBOT")
        if not r["users"]:
            m["reactions"].remove(r)

    # --- helpers for asserts ---
    def bot_posts(self, channel, kind):
        return [m for m in self.msgs[channel] if desk.meta(m) and desk.meta(m)["kind"] == kind]


ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ALLIANT = Alliant(customers=[Customer("W1", "WENDY'S #4412", "1", ALL_DAYS),
                             Customer("4000-1-01606", "SAFEWAY 1821 (BAKERY)", "1", ALL_DAYS)],
                  items={"W1": {"MAT CHARCOAL HEATHER 3X10": 4}, "4000-1-01606": {"MAT CHARCOAL HEATHER 3X10": 2}})
CALLS = []


def fake_parse(client, text, channel, ts, alliant, author="", office=False, previous=""):
    """Unclear until the thread contains the driver's answer "6"; "thanks" is chatter; "no, 3" is a correction."""
    CALLS.append(text)
    last = text.splitlines()[-1].lower()
    if "thanks" in last:
        return ParsedMessage(category=Category.not_a_request, summary="thanks")
    if "how many" in last or "and shop towels" in last:
        return ParsedMessage(category=Category.lookup, customer_as_written="Wendy's", account_number="W1",
                             summary=text.splitlines()[-1].split(": ", 1)[-1])
    acct = "4000-1-01606" if "safeway" in text.lower() else "W1"
    mats = lambda n: ParsedMessage(category=Category.item_change, customer_as_written="Wendy's", account_number=acct,
                                   summary=f"mats to {n}", changes=[Change(action=Action.set, item="3x10 mats", quantity=n)])
    if "no, 3" in last:
        return mats(3)
    if ": 6" in text:
        return mats(6)
    p = mats(6)
    p.changes[0].quantity = None
    p.questions_for_driver = ["How many 3x10 mats should Wendy's have in total?"]
    return p


def fake_is_reply(client, bot_said, request, message):
    return not message.lower().startswith("new:")


@pytest.fixture
def setup(monkeypatch):
    import service_changes.parser as parser
    monkeypatch.setattr(parser, "parse_message", fake_parse)
    monkeypatch.setattr(parser, "is_reply", fake_is_reply)
    monkeypatch.setattr(desk, "_office_staff", lambda: {"sonja burke"})
    CALLS.clear()
    slack = FakeSlack()
    return slack, desk.Desk(slack, None, ALLIANT, "service-desk-test", ["route-1-test"])


NOTHING = {"asked": 0, "ticket": 0, "read": 0, "readback": 0, "answered": 0}


def test_main_channel_loop(setup):
    slack, d = setup
    post = slack.say("C1", "U1", "more mats at wendys")

    # 1. Unclear: a normal channel message @mentioning the driver; the post gets 👀. A rerun does nothing more.
    assert d.run_once()["asked"] == 1
    [q] = slack.bot_posts("C1", "question")
    assert q["text"].startswith("<@U1> Quick check") and "thread_ts" not in q
    assert desk._seen(slack.find("C1", post["ts"]), "UBOT")
    assert d.run_once() == NOTHING and len(CALLS) == 1

    # 2. The driver answers in the main channel: one ticket, read together with the request and the question.
    slack.say("C1", "U1", "6")
    assert d.run_once()["ticket"] == 1
    assert "Service Desk asked:" in CALLS[-1] and "Mike Driver: 6" in CALLS[-1]
    [t] = slack.bot_posts("D1", "ticket")
    assert t["text"].startswith("*W1*  WENDY'S #4412") and "4 → *6*" in t["text"] and "Correction" not in t["text"]
    assert d.run_once() == NOTHING

    # 3. Sonja types "done" in the office channel: readback to the driver in the route channel, ticket marked.
    slack.say("D1", "U2", "done, only had 1 in stock today")
    assert d.run_once()["readback"] == 1
    [rb] = slack.bot_posts("C1", "readback")
    assert "thread_ts" not in rb and rb["text"].startswith("<@U1> ") and "set to 6" in rb["text"]
    assert "entered by Sonja Burke" in rb["text"] and "Office note: only had 1 in stock today" in rb["text"]
    t = slack.find("D1", t["ts"])
    assert desk.meta(t)["kind"] == "done" and "Entered by Sonja Burke" in t["text"]
    assert d.run_once() == NOTHING  # never twice

    # 4. "Thanks" after the readback is not a request.
    slack.say("C1", "U1", "thanks")
    assert d.run_once() == {**NOTHING, "read": 1}

    # 5. "No, 3" after the readback is a correction of the same request.
    slack.say("C1", "U1", "no, 3")
    assert d.run_once()["ticket"] == 1
    new = slack.bot_posts("D1", "ticket")[-1]
    assert "Correction" in new["text"] and "4 → *3*" in new["text"]

    # 6. A separate request right after is not tied to the old one.
    slack.say("C1", "U1", "new: wendys mats to 6: 6")
    assert d.run_once()["ticket"] == 1
    assert "Correction" not in slack.bot_posts("D1", "ticket")[-1]["text"]


def test_thread_answers_still_work(setup):
    slack, d = setup
    post = slack.say("C1", "U1", "more mats at wendys")
    d.run_once()
    slack.say("C1", "U2", "I can call him", thread_ts=post["ts"])  # office chatter in the thread is not an answer
    assert d.run_once() == NOTHING
    slack.say("C1", "U1", "6", thread_ts=post["ts"])
    assert d.run_once()["ticket"] == 1


def test_checkmark_and_which_ticket(setup):
    slack, d = setup
    slack.say("C1", "U1", "wendys mats to 6: 6")
    slack.say("C1", "U1", "new: wendys mats 6 again: 6")
    d.run_once()
    t1, t2 = slack.bot_posts("D1", "ticket")
    # Two open tickets and a bare "done": the bot asks which one and sends nothing.
    slack.say("D1", "U2", "done")
    assert d.run_once()["readback"] == 0
    assert slack.bot_posts("D1", "which")
    # ✅ on a ticket is never ambiguous.
    slack.react("D1", t2["ts"], "U2", "white_check_mark")
    assert d.run_once()["readback"] == 1
    # Now only one is open, so a bare "done" means that one.
    slack.say("D1", "U2", "Done.")
    assert d.run_once()["readback"] == 1
    assert len(slack.bot_posts("C1", "readback")) == 2


def test_done_with_account_number(setup):
    slack, d = setup
    slack.say("C1", "U1", "wendys mats to 6: 6")
    slack.say("C1", "U1", "new: safeway bakery mats to 6: 6")
    d.run_once()
    slack.say("D1", "U2", "4000-1-01606 done, only had 4 today")
    assert d.run_once()["readback"] == 1
    [rb] = slack.bot_posts("C1", "readback")
    assert "Office note: only had 4 today" in rb["text"]
    assert [desk.meta(t)["kind"] for t in slack.msgs["D1"] if desk.meta(t) and desk.meta(t).get("account")] == ["ticket", "done"]


def test_gives_up_asking_after_two_questions(setup):
    slack, d = setup
    slack.say("C1", "U1", "more mats at wendys")
    d.run_once()
    slack.say("C1", "U1", "the usual ones")
    d.run_once()
    assert len(slack.bot_posts("C1", "question")) == 2
    slack.say("C1", "U1", "you know")
    assert d.run_once()["ticket"] == 1
    assert "Please call them" in slack.bot_posts("D1", "ticket")[0]["text"]


def test_change_before_office_finishes_replaces_the_open_ticket(setup):
    slack, d = setup
    q = slack.say("C1", "U1", "more mats at wendys")
    d.run_once()
    slack.say("C1", "U1", "6")
    d.run_once()
    [old] = slack.bot_posts("D1", "ticket")
    slack.say("C1", "U1", "no, 3")  # changed their mind before the office got to it
    assert d.run_once()["ticket"] == 1
    old = slack.find("D1", old["ts"])
    assert desk.meta(old)["kind"] == "replaced" and old["text"].startswith("🚫 *Replaced")
    assert desk.meta(slack.bot_posts("D1", "ticket")[-1])["src_ts"] == q["ts"]
    slack.react("D1", old["ts"], "U2", "white_check_mark")  # ✅ on the replaced ticket does nothing
    assert d.run_once()["readback"] == 0


def test_office_person_who_posted_can_answer(setup):
    slack, d = setup
    slack.say("C1", "U2", "more mats at wendys")  # Sonja posts a call-in
    assert d.run_once()["asked"] == 1
    slack.say("C1", "U2", "6")
    assert d.run_once()["ticket"] == 1


def test_real_route_channels_need_the_live_setting(monkeypatch):
    monkeypatch.setattr("sys.argv", ["desk", "--routes", "route-12,route-3-test"])
    monkeypatch.delenv("SERVICE_DESK_LIVE", raising=False)
    with pytest.raises(SystemExit, match="#route-12 are real route channels"):
        desk.main()


def test_hourglass_is_swapped_for_eyes(setup):
    slack, d = setup
    post = slack.say("C1", "U1", "wendys mats to 6: 6")
    slack.react("C1", post["ts"], "UBOT", desk.WORKING)  # what the listener does on arrival
    d.run_once(only={"C1"})
    names = [r["name"] for r in slack.find("C1", post["ts"])["reactions"]]
    assert names == ["eyes"]


def test_short_answer_skips_the_reply_check(setup, monkeypatch):
    import service_changes.parser as parser
    slack, d = setup
    calls = []
    monkeypatch.setattr(parser, "is_reply", lambda *a: calls.append(a) or True)
    slack.say("C1", "U1", "more mats at wendys")
    d.run_once()
    slack.say("C1", "U1", "6")
    assert d.run_once()["ticket"] == 1 and calls == []


def test_driver_question_is_answered_from_alliant(setup, monkeypatch):
    import service_changes.parser as parser
    from .parser import Answer
    slack, d = setup
    asked = []

    def fake_answer(client, question, facts):
        asked.append((question, facts))
        found = "towels" not in question
        return Answer(answer="4 3x10 charcoal mats per delivery" if found else "No shop towels on this account.", found=found)

    monkeypatch.setattr(parser, "answer_lookup", fake_answer)
    slack.say("C1", "U1", "how many mats does wendys get?")
    assert d.run_once()["answered"] == 1
    [a] = slack.bot_posts("C1", "answer")
    assert a["text"].startswith("<@U1> *WENDY'S #4412*: 4 3x10 charcoal mats per delivery")
    assert "MAT CHARCOAL HEATHER 3X10: 4 per delivery" in asked[0][1]
    assert not slack.bot_posts("D1", "ticket")  # nothing for the office to do...
    [log] = slack.bot_posts("D1", "lookup_log")  # ...but they see what was asked and answered
    assert "*Mike Driver* asked about *W1* WENDY'S #4412" in log["text"] and "4 3x10 charcoal mats" in log["text"]

    # A follow-up in the channel is read with the first question; one the data can't answer goes to the office.
    slack.say("C1", "U1", "and shop towels?")
    assert d.run_once()["answered"] == 1
    assert "Service Desk answered:" in CALLS[-1]
    assert "I've asked the office" in slack.bot_posts("C1", "answer")[-1]["text"]
    [t] = slack.bot_posts("D1", "ticket")
    assert "Driver question I couldn't answer" in t["text"]


def test_too_many_lookups_in_a_day_are_held_and_flagged(setup, monkeypatch):
    import service_changes.parser as parser
    from .parser import Answer
    slack, d = setup
    monkeypatch.setattr(desk, "MAX_LOOKUPS_PER_DAY", 2)
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, f: Answer(answer="4 mats", found=True))
    for i in range(3):
        slack.say("C1", "U1", f"new: how many mats does wendys get {i}?")
        d.run_once()
    answers = slack.bot_posts("C1", "answer")
    assert len(answers) == 3 and "passed this one to the office" in answers[-1]["text"]
    assert len(slack.bot_posts("D1", "lookup_log")) == 2
    [flag] = slack.bot_posts("D1", "lookup_limit")
    assert "*Mike Driver* has asked about accounts 2 times today" in flag["text"]

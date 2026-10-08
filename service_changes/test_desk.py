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


def test_lookup_about_another_route_goes_to_the_office(setup, monkeypatch):
    import service_changes.parser as parser
    slack, d = setup
    called = []
    monkeypatch.setattr(parser, "answer_lookup", lambda *a: called.append(a))
    monkeypatch.setattr(d.alliant, "customers", d.alliant.customers + [Customer("R9", "OTHER ROUTE CAFE", "9", ALL_DAYS)])
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: ParsedMessage(
        category=Category.lookup, customer_as_written="Other Route Cafe", account_number="R9", summary="how many mats"))
    slack.say("C1", "U1", "how many mats does other route cafe get?")
    d.run_once()
    assert not called
    assert "isn't on route 1" in slack.bot_posts("C1", "answer")[0]["text"]
    [flag] = slack.bot_posts("D1", "lookup_off_route")
    assert "which is on route 9" in flag["text"]


def test_at_most_three_questions_at_once(setup, monkeypatch):
    import service_changes.parser as parser
    slack, d = setup

    def many(*a, **k):
        p = fake_parse(*a, **k)
        p.questions_for_driver = [f"Question {i}?" for i in range(1, 6)]
        return p
    monkeypatch.setattr(parser, "parse_message", many)
    slack.say("C1", "U1", "more mats at wendys")
    assert d.run_once()["asked"] == 1
    [q] = slack.bot_posts("C1", "question")
    assert q["text"].count("•") == 3 and "Question 3?" in q["text"] and "Question 4?" not in q["text"]


def test_checkmark_from_the_event_before_history_shows_it(setup):
    slack, d = setup
    slack.say("C1", "U1", "wendys mats to 6: 6")
    d.run_once()
    [t] = slack.bot_posts("D1", "ticket")
    d.done_events[t["ts"]] = "U2"  # Slack's reaction_added event arrived; history has no ✅ yet
    assert d.run_once()["readback"] == 1
    assert d.done_events == {} and d.run_once()["readback"] == 0


def test_office_can_ask_in_the_office_channel(setup, monkeypatch):
    import service_changes.parser as parser
    from .parser import Answer
    slack, d = setup
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, f: Answer(answer="4 3x10 mats weekly", found=True))
    q = slack.say("D1", "U2", "how many mats does wendys get?")
    assert d.run_once()["answered"] == 1
    [a] = slack.bot_posts("D1", "answer")
    assert "thread_ts" not in a and a["text"].startswith("<@U2> *W1* WENDY'S #4412: 4 3x10 mats weekly")
    assert not slack.bot_posts("D1", "lookup_log") and desk._seen(slack.find("D1", q["ts"]), "UBOT")
    assert d.run_once()["answered"] == 0  # read once


def test_office_answers_which_customer_in_the_channel(setup, monkeypatch):
    import service_changes.parser as parser
    from .parser import Answer
    slack, d = setup
    seen = []

    def office_parse(client, text, channel, ts, alliant, author="", office=False, previous=""):
        seen.append(text)
        known = "fairbanks" in text.lower()
        return ParsedMessage(category=Category.lookup, customer_as_written="Midas", summary="shop towels at Midas",
                             account_number="W1" if known else None,
                             questions_for_driver=[] if known else ["Which Midas store?"])
    monkeypatch.setattr(parser, "parse_message", office_parse)
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, f: Answer(answer="200 shop towels weekly", found=True))
    slack.say("D1", "U2", "how many shop towels does midas get")
    d.run_once()
    [ask] = slack.bot_posts("D1", "office_ask")
    assert ask["text"] == "<@U2> Which Midas store?" and "thread_ts" not in ask
    slack.say("D1", "U2", "Fairbanks")
    assert d.run_once()["answered"] == 1
    assert "how many shop towels does midas get" in seen[-1] and "Answer: Fairbanks" in seen[-1]
    [a] = slack.bot_posts("D1", "answer")
    assert a["text"].startswith("<@U2> *W1*") and "200 shop towels weekly" in a["text"]


def test_checkmark_on_a_customer_text_ticket_texts_the_readback(setup, monkeypatch):
    import service_changes.sms as sms
    slack, d = setup
    sent = []
    monkeypatch.setattr(sms, "send_sms", lambda to, body: sent.append((to, body)))
    t = d.post("D1", "*W1* WENDY'S #4412\n➕ *Add 200* shop towels", {
        "kind": "ticket", "src_channel": "sms", "src_ts": "SM1", "readback": "✅ W1 WENDY'S #4412 – 200 shop towels added",
        "driver": "Jane", "driver_id": "", "account": "W1", "msgs": [], "sms_to": "+19075551234"})
    slack.react("D1", t["ts"], "U2", "white_check_mark")
    assert d.run_once()["readback"] == 1
    assert sent == [("+19075551234", "Snow White Linen: ✅ W1 WENDY'S #4412 – 200 shop towels added – entered by Sonja Burke")]
    assert "sms" not in slack.msgs  # nothing posted to Slack for it


def test_driver_can_ask_to_add_a_texting_number(setup, monkeypatch):
    import service_changes.parser as parser
    from .parser import Identified
    slack, d = setup
    monkeypatch.setattr(parser, "identify_customer", lambda c, text, customers: Identified(accounts=["W1"]))
    slack.say("C1", "U1", "add 907-555-1234 to wendys")
    slack.say("D1", "U2", "add 907-555-9999 to W1")  # office command: left for sms.py, not read as a question
    d.run_once()
    [card] = slack.bot_posts("D1", "sms_signup")
    assert "*Mike Driver in #route-1-test* asks to set up (907) 555-1234" in card["text"] and "Best match: *W1*" in card["text"]
    assert not slack.bot_posts("D1", "ticket") and not slack.bot_posts("C1", "question")
    assert "sent that to the office" in slack.bot_posts("C1", "answer")[0]["text"]
    assert not slack.bot_posts("D1", "answer") and len(CALLS) == 0


def test_stale_alliant_data_warns_the_office_once_then_daily(setup):
    from datetime import datetime, timedelta, timezone
    slack, d = setup
    now = datetime.now(timezone.utc)
    d.alliant.exported_at = now - timedelta(hours=40)
    try:
        d.run_once()
        [w] = slack.bot_posts("D1", "stale_data")
        assert "Alliant data is 40 hours old" in w["text"] and "Mac" in w["text"]
        d.run_once()
        assert len(slack.bot_posts("D1", "stale_data")) == 1  # not every pass
        d.check_stale(now.timestamp() + 86401)
        assert len(slack.bot_posts("D1", "stale_data")) == 2  # a reminder a day later
        d.alliant.exported_at = now - timedelta(hours=2)  # the feed came back
        d.run_once()
        assert slack.bot_posts("D1", "stale_data")[-1]["text"] == "✅ Alliant data is current again."
        d.run_once()
        assert len(slack.bot_posts("D1", "stale_data")) == 3
        # A restart forgets, but the office channel remembers: no second warning the same day.
        d.alliant.exported_at = now - timedelta(hours=40)
        d.run_once()
        assert len(slack.bot_posts("D1", "stale_data")) == 4
        d2 = desk.Desk(slack, None, ALLIANT, "service-desk-test", ["route-1-test"])
        d2.alliant = d.alliant
        d2.run_once()
        assert len(slack.bot_posts("D1", "stale_data")) == 4
    finally:
        d.alliant.exported_at = None


def test_routes_are_handled_in_parallel_and_each_message_once(monkeypatch):
    import queue
    import threading
    import service_changes.parser as parser

    class TwoRoutes(FakeSlack):
        def __init__(self):
            super().__init__()
            self.msgs["C2"] = []
            self.lock = threading.RLock()

        def say(self, *a, **k):
            with self.lock:
                return super().say(*a, **k)

        def conversations_list(self, **kw):
            r = super().conversations_list()
            r["channels"].append({"name": "route-2-test", "id": "C2", "is_member": True})
            return r

    def slow_parse(*a, **k):  # Claude takes a while to read each message
        time.sleep(0.6)
        return fake_parse(*a, **k)
    monkeypatch.setattr(parser, "parse_message", slow_parse)
    monkeypatch.setattr(parser, "is_reply", fake_is_reply)
    monkeypatch.setattr(desk, "_office_staff", lambda: {"sonja burke"})
    slack = TwoRoutes()
    d = desk.Desk(slack, None, ALLIANT, "service-desk-test", ["route-1-test", "route-2-test"])
    d.listening = True
    slack.say("C1", "U1", "more mats at wendys")
    slack.say("C2", "U1", "more mats at wendys")
    todo, stop = queue.Queue(), threading.Event()
    for item in [("C1", None, 1), ("C2", None, 1), ("C1", None, 1)]:  # C1 announced twice
        todo.put(item)
    t0 = time.time()
    th = threading.Thread(target=d.serve, args=(todo, 4320, 10**9, lambda: None), kwargs={"stop": stop})
    th.start()
    while time.time() - t0 < 5 and not (slack.bot_posts("C1", "question") and slack.bot_posts("C2", "question")):
        time.sleep(0.05)
    took = time.time() - t0
    time.sleep(1)  # let the queued second C1 pass finish
    stop.set()
    th.join(5)
    assert len(slack.bot_posts("C1", "question")) == 1 and len(slack.bot_posts("C2", "question")) == 1
    assert took < 1.1  # both routes read at the same time, not one after the other (2 x 0.6 s)


def test_a_new_request_after_an_answer_is_its_own_request(setup, monkeypatch):
    import service_changes.parser as parser
    from .parser import Answer
    slack, d = setup
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, f: Answer(answer="4 3x10 mats", found=True))
    monkeypatch.setattr(parser, "is_reply", lambda *a: pytest.fail("no reply check needed after an answer"))
    slack.say("C1", "U1", "how many mats does wendys get?")
    d.run_once()
    slack.say("C1", "U1", "wendys mats to 6: 6")
    assert d.run_once()["ticket"] == 1
    [t] = slack.bot_posts("D1", "ticket")
    assert "wendys mats to 6" in t["text"] and "how many mats" not in t["text"]  # quotes the new message
    assert "(NEW message, read this one): wendys mats to 6" in CALLS[-1]


def test_combined_lookup_across_a_customers_locations(setup, monkeypatch):
    import service_changes.parser as parser
    from .parser import Answer
    slack, d = setup
    d.alliant = Alliant(customers=[Customer("7192-1-00005", "COSTCO 1342 DELI 63", "1", ALL_DAYS),
                                   Customer("7192-1-00006", "COSTCO 1342 BAKERY 62", "1", ALL_DAYS),
                                   Customer("W1", "WENDY'S #4412", "1", ALL_DAYS)],
                        items={"7192-1-00005": {"TOWEL BAR MOP": 800}, "7192-1-00006": {"TOWEL BAR MOP": 760}})
    seen = {}
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: ParsedMessage(
        category=Category.lookup, customer_as_written="costco", summary="bar mops at Costco, all departments"))
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, facts: (seen.update(facts=facts), Answer(answer="1,560 bar mops", found=True))[1])
    slack.say("C1", "U1", "how many bar mops does costco get, all of them combined")
    assert d.run_once()["answered"] == 1
    assert "COSTCO 1342 DELI 63" in seen["facts"] and "COSTCO 1342 BAKERY 62" in seen["facts"] and "WENDY" not in seen["facts"]
    [a] = slack.bot_posts("C1", "answer")
    assert "*COSTCO (2 accounts)*: 1,560 bar mops" in a["text"] and not slack.bot_posts("D1", "ticket")


def test_one_message_for_two_accounts_makes_two_tickets(setup, monkeypatch):
    import service_changes.parser as parser
    slack, d = setup
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: ParsedMessage(
        category=Category.item_change, customer_as_written="wendys and safeway", summary="mats at two stores",
        changes=[Change(action=Action.set, item="3x10 mats", quantity=6, account_number="W1"),
                 Change(action=Action.set, item="3x10 mats", quantity=3, account_number="4000-1-01606")]))
    slack.say("C1", "U1", "wendys mats to 6 and safeway bakery mats to 3")
    assert d.run_once()["ticket"] == 1
    t1, t2 = slack.bot_posts("D1", "ticket")
    assert t1["text"].startswith("*W1*") and "4 → *6*" in t1["text"]
    assert t2["text"].startswith("*4000-1-01606*") and "2 → *3*" in t2["text"]
    # Each ✅ sends its own readback.
    slack.react("D1", t2["ts"], "U2", "white_check_mark")
    d.run_once()
    [rb] = slack.bot_posts("C1", "readback")
    assert "4000-1-01606 SAFEWAY 1821 (BAKERY)" in rb["text"] and "W1" not in rb["text"]
    assert d.run_once()["ticket"] == 0  # the request isn't read again


def test_split_by_account_keeps_single_account_messages_whole():
    from .checks import split_by_account
    one = ParsedMessage(category=Category.item_change, account_number="W1", summary="",
                        changes=[Change(action=Action.add, item="mats", quantity=1)])
    assert split_by_account(one) == [one]
    named = ParsedMessage(category=Category.item_change, summary="",
                          changes=[Change(action=Action.add, item="mats", quantity=1, account_number="W1")])
    assert [p.account_number for p in split_by_account(named)] == ["W1"]

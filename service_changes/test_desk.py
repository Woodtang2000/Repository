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

    def chat_postMessage(self, channel, text, thread_ts=None, metadata=None, **kw):
        return self.say(channel, "UBOT", text, thread_ts=thread_ts, bot_id="B", metadata=metadata)

    def reactions_add(self, channel, timestamp, name):
        self.react(channel, timestamp, "UBOT", name)

    # --- helpers for asserts ---
    def bot_posts(self, channel, kind):
        return [m for m in self.msgs[channel] if desk.meta(m) and desk.meta(m)["kind"] == kind]


ALLIANT = Alliant(customers=[Customer("W1", "WENDY'S #4412", "1", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])],
                  items={"W1": {"MAT CHARCOAL HEATHER 3X10": 4}})
CALLS = []


def fake_parse(client, text, channel, ts, alliant, author="", office=False, previous=""):
    """Unclear until the thread contains the driver's answer "6"; "thanks" is chatter; "no, 3" is a correction."""
    CALLS.append(text)
    last = text.splitlines()[-1].lower()
    if "thanks" in last:
        return ParsedMessage(category=Category.not_a_request, summary="thanks")
    mats = lambda n: ParsedMessage(category=Category.item_change, customer_as_written="Wendy's", account_number="W1",
                                   summary=f"mats to {n}", changes=[Change(action=Action.set, item="3x10 mats", quantity=n)])
    if "no, 3" in last:
        return mats(3)
    if ": 6" in text:
        return mats(6)
    p = mats(6)
    p.changes[0].quantity = None
    p.questions_for_driver = ["How many 3x10 mats should Wendy's have in total?"]
    return p


@pytest.fixture
def setup(monkeypatch):
    import service_changes.parser as parser
    monkeypatch.setattr(parser, "parse_message", fake_parse)
    monkeypatch.setattr(desk, "_office_staff", lambda: {"sonja burke"})
    CALLS.clear()
    slack = FakeSlack()
    return slack, desk.Desk(slack, None, ALLIANT, "service-desk-test", ["route-1-test"])


def test_full_loop(setup):
    slack, d = setup
    post = slack.say("C1", "U1", "more mats at wendys")

    # 1. Unclear: the bot asks in the driver's thread and marks the post 👀. A rerun does nothing more.
    assert d.run_once()["asked"] == 1
    assert len(slack.bot_posts("C1", "question")) == 1
    assert desk._seen(slack.find("C1", post["ts"]), "UBOT")
    assert d.run_once() == {"asked": 0, "ticket": 0, "read": 0, "readback": 0}
    assert len(CALLS) == 1

    # 2. Office chatter in the thread is not an answer.
    slack.say("C1", "U2", "I can call him", thread_ts=post["ts"])
    assert d.run_once()["ticket"] == 0

    # 3. The driver answers: one ticket to the office channel, read with the whole thread.
    slack.say("C1", "U1", "6", thread_ts=post["ts"])
    assert d.run_once()["ticket"] == 1
    assert "Service Desk asked:" in CALLS[-1] and "Mike Driver: 6" in CALLS[-1]
    [t] = slack.bot_posts("D1", "ticket")
    assert "WENDY'S #4412" in t["text"] and "4 → 6" in t["text"] and "Updated request" in t["text"]
    assert desk.meta(t)["src_ts"] == post["ts"]
    assert d.run_once()["ticket"] == 0

    # 4. Sonja enters it and reacts ✅ with a note: the driver gets the readback, the ticket says it was sent.
    slack.say("D1", "U2", "only had 1 in stock today, rest Friday", thread_ts=t["ts"])
    slack.react("D1", t["ts"], "U2", "white_check_mark")
    assert d.run_once()["readback"] == 1
    [rb] = slack.bot_posts("C1", "readback")
    assert rb["thread_ts"] == post["ts"]
    assert "set to 6" in rb["text"] and "entered by Sonja Burke" in rb["text"] and "Office note: only had 1" in rb["text"]
    assert len(slack.bot_posts("D1", "sent")) == 1
    assert d.run_once()["readback"] == 0  # never twice

    # 5. "Thanks" is not a new request.
    slack.say("C1", "U1", "thanks", thread_ts=post["ts"])
    assert d.run_once() == {"asked": 0, "ticket": 0, "read": 1, "readback": 0}

    # 6. A correction after the readback becomes a new, labelled ticket.
    slack.say("C1", "U1", "no, 3", thread_ts=post["ts"])
    assert d.run_once()["ticket"] == 1
    new = slack.bot_posts("D1", "ticket")[-1]
    assert "Correction after readback" in new["text"] and "4 → 3" in new["text"]


def test_clear_request_goes_straight_to_office_and_done_reply_finishes_it(setup):
    slack, d = setup
    post = slack.say("C1", "U1", "wendys mats to 6: 6")
    assert d.run_once() == {"asked": 0, "ticket": 1, "read": 0, "readback": 0}
    assert not slack.bot_posts("C1", "question")
    [t] = slack.bot_posts("D1", "ticket")
    assert "Updated request" not in t["text"]
    slack.say("D1", "U2", "Done", thread_ts=t["ts"])
    assert d.run_once()["readback"] == 1
    [rb] = slack.bot_posts("C1", "readback")
    assert "Office note" not in rb["text"] and rb["thread_ts"] == post["ts"]


def test_gives_up_asking_after_two_questions(setup):
    slack, d = setup
    post = slack.say("C1", "U1", "more mats at wendys")
    d.run_once()
    slack.say("C1", "U1", "the usual ones", thread_ts=post["ts"])
    d.run_once()
    assert len(slack.bot_posts("C1", "question")) == 2
    slack.say("C1", "U1", "you know", thread_ts=post["ts"])
    assert d.run_once()["ticket"] == 1
    assert "Please call them" in slack.bot_posts("D1", "ticket")[0]["text"]


def test_update_before_office_finishes_replaces_the_open_ticket(setup):
    slack, d = setup
    post = slack.say("C1", "U1", "wendys mats: 6")
    d.run_once()
    [old] = slack.bot_posts("D1", "ticket")
    slack.say("C1", "U1", "no, 3", thread_ts=post["ts"])
    assert d.run_once()["ticket"] == 1
    assert [desk.meta(m)["kind"] for m in slack.thread_of("D1", old["ts"])][1:] == ["replaced"]
    # ✅ on the replaced ticket does nothing; only the new one sends a readback.
    slack.react("D1", old["ts"], "U2", "white_check_mark")
    assert d.run_once()["readback"] == 0


def test_real_route_channels_need_the_live_setting(monkeypatch):
    monkeypatch.setattr("sys.argv", ["desk", "--routes", "route-12,route-3-test"])
    monkeypatch.delenv("SERVICE_DESK_LIVE", raising=False)
    with pytest.raises(SystemExit, match="#route-12 are real route channels"):
        desk.main()

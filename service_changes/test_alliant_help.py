import json
import os
from types import SimpleNamespace

from . import alliant_help
from .test_webchat import FakeClaude, text, tool


class Slack:
    def __init__(self, replies=None):
        self.posts, self.reactions, self.replies = [], [], replies or []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)

    def reactions_add(self, **kw):
        self.reactions.append(kw)

    def conversations_replies(self, **kw):
        return {"messages": self.replies}


def notes(tmp_path):
    d = tmp_path / "alliant_university"
    (d / "Courses").mkdir(parents=True, exist_ok=True)
    (d / "INDEX.md").write_text("# INDEX\n09 Wearers - Add, Remove, Transfer and Lookup.md: wearers\n")
    (d / "AGENT GUIDE - Using the Alliant knowledge.md").write_text("# AGENT GUIDE\nSafety rules.\n")
    (d / "09 Wearers - Add, Remove, Transfer and Lookup.md").write_text(
        "# Wearers\nTo transfer a wearer open \"Wearers\" and click \"Transfer\".\nUnrelated line.\n")
    (d / "Courses" / "C2 Wearer basics.md").write_text("Wearer course " + "x" * 30000)
    return str(tmp_path)


def helper(tmp_path, claude, slack=None):
    return alliant_help.Helper(slack or Slack(), claude, notes(tmp_path), "CHELP", "UBOT", scott="USCOTT")


def test_notes_search_read_and_guide(tmp_path):
    n = alliant_help.Notes(os.path.join(notes(tmp_path), "alliant_university"))
    assert n.ready()
    hits = n.search("how do I transfer a wearer")
    assert hits.splitlines()[0] == "09 Wearers - Add, Remove, Transfer and Lookup.md"
    assert 'click "Transfer"' in hits and "Unrelated" not in hits
    assert n.read("09 Wearers - Add, Remove, Transfer and Lookup").startswith("09 Wearers")
    long = n.read("C2 Wearer basics.md", part=2)
    assert long.startswith("Courses/C2 Wearer basics.md (part 2 of 2)")
    assert "No file named" in n.read("../../etc/passwd")
    guide = n.guide()
    assert "# INDEX" in guide and "Safety rules" in guide and "Wearer course" not in guide


def test_a_question_gets_an_answer_in_the_thread(tmp_path):
    claude = FakeClaude([tool("search", query="transfer wearer")],
                        [tool("read", name="09 Wearers - Add, Remove, Transfer and Lookup.md")],
                        [text('1. Open *"Wearers"*\n2. Click *"Transfer"*\n_Source: 09 Wearers_')])
    h = helper(tmp_path, claude)
    ev = {"type": "message", "channel": "CHELP", "user": "UANA", "ts": "100.1", "text": "How do I move a wearer to another account?"}
    assert h.wants(ev) and not h.wants(ev)  # Slack can deliver an event twice
    assert h.handle(ev) == "answered"
    [post] = h.slack.posts
    assert post["thread_ts"] == "100.1" and '"Transfer"' in post["text"] and "USCOTT" not in post["text"]
    assert claude.calls[0]["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert 'click \\"Transfer' in json.dumps(claude.calls[2]["messages"])


def test_not_covered_tags_scott_and_logs(tmp_path):
    claude = FakeClaude([tool("not_covered", question="How do I set up Propelr?")],
                        [text("The notes don't cover that. A person will follow up.")])
    h = helper(tmp_path, claude)
    ev = {"type": "message", "channel": "CHELP", "user": "UANA", "ts": "100.2", "text": "Propelr setup?"}
    assert h.wants(ev) and h.handle(ev) == "not covered"
    assert "<@USCOTT>" in h.slack.posts[0]["text"]
    with open(os.path.join(str(tmp_path), "alliant_unanswered.jsonl")) as f:
        assert json.loads(f.readline())["question"] == "How do I set up Propelr?"


def test_follow_ups_only_in_threads_the_bot_is_in(tmp_path):
    claude = FakeClaude([text("Yes, before System Create.")])
    replies = [{"user": "UANA", "ts": "100.3", "text": "How do I add a wearer?"},
               {"user": "UBOT", "bot_id": "B1", "ts": "100.4", "text": "Open Wearers..."},
               {"user": "UANA", "ts": "100.5", "text": "Does it show on tomorrow's ticket?"}]
    h = helper(tmp_path, claude, Slack(replies))
    ev = {"type": "message", "channel": "CHELP", "user": "UANA", "ts": "100.5", "thread_ts": "100.3", "text": "Does it show..."}
    assert h.handle(ev) == "answered"
    assert [m["role"] for m in claude.calls[0]["messages"]] == ["user", "assistant", "user"]
    h2 = helper(tmp_path, FakeClaude(), Slack([replies[0], {"user": "USONJA", "ts": "100.6", "text": "I'll show you"}]))
    assert h2.handle({**ev, "ts": "100.6"}) is None and h2.slack.posts == []


def test_ignores_bots_edits_and_other_channels(tmp_path):
    h = helper(tmp_path, FakeClaude())
    base = {"type": "message", "channel": "CHELP", "user": "UANA", "ts": "1.0"}
    assert not h.wants({**base, "bot_id": "B1"})
    assert not h.wants({**base, "user": "UBOT"})
    assert not h.wants({**base, "subtype": "message_changed"})
    assert not h.wants({**base, "channel": "COTHER"})


def test_no_notes_yet(tmp_path):
    h = alliant_help.Helper(Slack(), FakeClaude(), str(tmp_path), "CHELP", "UBOT")
    assert h.handle({"type": "message", "channel": "CHELP", "user": "UANA", "ts": "1.0", "text": "hi"}) == "notes missing"


def test_private_messages_to_the_bot():
    from types import SimpleNamespace
    from .desk import Desk

    class S(Slack):
        def conversations_history(self, **kw):
            return {"messages": []}

    looked = []
    d = SimpleNamespace(slack=S(), is_office=lambda u: u == "USONJA", is_owner=lambda u: False,
                        office_lookup=lambda ev, top, channel=None: looked.append(channel) or "4000" in ev["text"],
                        helper=SimpleNamespace(answer=lambda turns, user: ("Open Customer > Specials", False)))
    ev = lambda user, text: {"type": "message", "channel_type": "im", "channel": "D1", "user": user, "ts": "1.0", "text": text}
    assert Desk.dm(d, ev("UDRIVER", "how do I start my route on the handheld?")) == "how-to" and looked == []
    assert Desk.dm(d, ev("USONJA", "how many mats at 4000?")) == "lookup" and looked == ["D1"]
    assert Desk.dm(d, ev("USONJA", "how do I make a special?")) == "how-to"
    assert d.slack.posts[-1] == {"channel": "D1", "text": "Open Customer > Specials", "unfurl_links": False}


def test_scott_gets_donna_in_his_private_messages(monkeypatch):
    from types import SimpleNamespace
    from .desk import Desk
    import service_changes.parser as parser

    class S(Slack):
        def conversations_history(self, **kw):
            return {"messages": [{"ts": "2.0", "user": "U4KFSQCDQ", "text": "what's our biggest risk this week?"},
                                 {"ts": "1.5", "bot_id": "B1", "text": "Morning, Harvey."}]}

    seen = {}
    monkeypatch.setattr(parser, "owner_chat", lambda c, turns, briefing: seen.update(turns=turns) or "Leave it with me.")
    monkeypatch.delenv("OWNER_SLACK_ID", raising=False)
    d = SimpleNamespace(slack=S(), me="UBOT", claude=None, is_office=lambda u: False, helper=None,
                        office_lookup=lambda ev, top, channel=None: False, briefing=lambda: "",
                        is_owner=lambda u: Desk.is_owner(None, u))
    d.owner_turns = lambda ch, ev: Desk.owner_turns(d, ch, ev)
    ev = lambda user, text: {"type": "message", "channel_type": "im", "channel": "D1", "user": user, "ts": "2.0", "text": text}
    assert Desk.dm(d, ev("U4KFSQCDQ", "what's our biggest risk this week?")) == "donna"
    assert d.slack.posts[-1]["text"] == "Leave it with me."
    assert seen["turns"] == [{"role": "user", "content": "what's our biggest risk this week?"}]  # bot's line first is dropped
    assert Desk.dm(d, ev("UDRIVER", "what's our biggest risk?")) == "no notes"  # anyone else: no Donna, no data
    monkeypatch.setenv("OWNER_SLACK_ID", "UOTHER")
    assert not Desk.is_owner(None, "U4KFSQCDQ")


def test_donnas_read_on_the_operation_and_morning_brief(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime
    from .desk import Desk, PULSE
    from .context import ALASKA
    import service_changes.parser as parser
    import threading

    class S(Slack):
        def conversations_history(self, channel, **kw):
            return {"messages": {"CDESK": [{"ts": "1700000000.0", "bot_id": "B1", "text": "📱 Customer text: towels were late again"}],
                                 "CR4": [{"ts": "1700000100.0", "user": "UMIKE", "text": "Truck 4 heater is out"},
                                         {"ts": "1700000200.0", "user": "UMIKE", "subtype": "channel_join", "text": "joined"}]}[channel]}

    d = SimpleNamespace(slack=S(), me="UBOT", desk_name="service-desk", channels={"service-desk": "CDESK", "route-4": "CR4"},
                        name=lambda u: {"UMIKE": "Mike Driver"}.get(u, u), data_dir=str(tmp_path), claude=None,
                        briefing=lambda: "SW: 3 customers")
    text = Desk.pulse(d)
    assert "#service-desk:\n" in text and "Donna: 📱 Customer text: towels were late again" in text
    assert "Mike Driver: Truck 4 heater is out" in text and "joined" not in text
    assert text.index("#service-desk") < text.index("#route-4")  # the office channel (customers) first
    assert PULSE.search("how are things going?") and PULSE.search("what could be better") and not PULSE.search("how many mats at 4000")

    d.pulse = lambda hours=48: Desk.pulse(d, hours)
    got = {}
    monkeypatch.setattr(parser, "owner_chat", lambda c, turns, about: got.update(about=about) or "Towels were late twice.")
    monkeypatch.setattr(threading, "Thread", lambda target, daemon: SimpleNamespace(start=target))
    monday_6 = datetime(2026, 10, 26, 6, 30, tzinfo=ALASKA).timestamp()
    Desk.owner_brief_pass(d, monday_6)
    assert d.slack.posts == []  # before 7
    Desk.owner_brief_pass(d, monday_6 + 3600)
    Desk.owner_brief_pass(d, monday_6 + 7200)  # once a day
    assert d.slack.posts == [{"channel": "U4KFSQCDQ", "text": "☕ *Morning brief*\nTowels were late twice.", "unfurl_links": False}]
    assert "last 72 hours" in got["about"]  # Monday covers the weekend
    Desk.owner_brief_pass(d, datetime(2026, 10, 31, 9, tzinfo=ALASKA).timestamp())  # Saturday: none
    assert len(d.slack.posts) == 1


def test_hey_donna_voice(monkeypatch):
    from types import SimpleNamespace
    from .desk import Desk, spoken
    import service_changes.parser as parser
    assert spoken("<@U4KFSQCDQ> *4000-1-00001* SAFEWAY: 12 mats _(Alliant as of Oct 26)_") == "4000-1-00001 SAFEWAY: 12 mats"

    asked = []

    def lookup(h, top, channel=None, say=None):
        asked.append(h["text"])
        if "mats" in h["text"] and "Answer:" not in h["text"]:
            say("<@U4KFSQCDQ> Which customer?", {"kind": "office_ask", "q": h["text"]})
            return True
        if "Answer: Safeway 1807" in h["text"]:
            say("<@U4KFSQCDQ> *4000-1-01807* SAFEWAY: 6 mats a week _(Alliant as of Oct 26)_", {"kind": "answer"})
            return True
        return False
    chats = []
    monkeypatch.setattr(parser, "owner_chat", lambda c, turns, about, spoken=False: chats.append((turns, spoken)) or "Leave it with me.")
    d = SimpleNamespace(claude=None, office_lookup=lookup, briefing=lambda: "", pulse=lambda: "PULSE")
    assert Desk.voice(d, "  ", "c1").startswith("I didn't catch anything") and not chats and not asked
    assert Desk.voice(d, "how many mats do they get", "c1") == "Which customer?"
    assert Desk.voice(d, "Safeway 1807", "c1") == "4000-1-01807 SAFEWAY: 6 mats a week"
    assert asked[-1] == "how many mats do they get\nDonna asked: Which customer?\nAnswer: Safeway 1807"
    assert Desk.voice(d, "should we raise the route 4 minimum?", "c1") == "Leave it with me."
    turns, was_spoken = chats[-1]
    assert was_spoken and len(turns) == 5 and turns[-1]["content"] == "should we raise the route 4 minimum?"


def test_donna_voice_needs_scotts_key(monkeypatch):
    import io
    from .webchat import make_handler
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "k" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    H = make_handler(None, "https://x")
    sent = []

    def call(auth):
        h = H.__new__(H)
        h.path, h.rfile = "/api/donna", io.BytesIO(b'{"text":"hi"}')
        h.headers = {"Authorization": auth, "Content-Length": "13"}
        h._send = lambda code, body, ctype, cache=0: sent.append((code, body))
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: io.BytesIO(b"Hi Harvey."))
        h.do_POST()
        return sent[-1]
    assert call("Bearer wrong") == (404, b"not found")
    assert call("Bearer " + "k" * 32) == (200, b"Hi Harvey.")
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "short")
    assert call("Bearer short")[0] == 404  # a weak key never works


def test_donna_simple_address(monkeypatch):
    import io, json
    import urllib.request
    from .webchat import make_handler
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "k" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    got, sent = [], []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: got.append(json.loads(req.data)) or io.BytesIO(b"Hi Harvey."))
    H = make_handler(None, "https://x")

    def get(path):
        h = H.__new__(H)
        h.path, h.headers = path, {}
        h._send = lambda code, body, ctype, cache=0: sent.append((code, body))
        h.do_GET()
        return sent[-1]
    assert get("/api/donna?key=" + "k" * 32 + "&text=How%20are%20things%20going%3F") == (200, b"Hi Harvey.")
    assert got[-1] == {"text": "How are things going?", "convo": "phone"}
    assert get("/api/donna?key=nope&text=hi")[0] == 404 and len(got) == 1


def test_donna_page_has_no_key(monkeypatch):
    from .webchat import make_handler
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "k" * 32)
    H = make_handler(None, "https://x")
    sent = []
    h = H.__new__(H)
    h.path, h.headers = "/donna", {}
    h._send = lambda code, body, ctype, cache=0: sent.append((code, body, ctype))
    h.do_GET()
    code, body, ctype = sent[-1]
    assert code == 200 and ctype.startswith("text/html") and b"/api/donna" in body and b"k" * 32 not in body
    h.path = "/donna-icon.png"
    h.do_GET()
    assert sent[-1][0] == 200 and sent[-1][2] == "image/png" and sent[-1][1][:4] == b"\x89PNG"

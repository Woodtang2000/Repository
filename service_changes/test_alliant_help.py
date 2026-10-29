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
    monkeypatch.setattr(parser, "owner_chat", lambda c, turns, about, spoken=False, person="": chats.append((turns, spoken)) or "Leave it with me.")
    d = SimpleNamespace(claude=None, office_lookup=lookup, briefing=lambda: "", pulse=lambda: "PULSE", sku_answer=lambda t, u=None: "", owner_change=lambda k, t, c, **kw: None,
                        sounds_like_lookup=lambda t: "mats" in t or "Safeway" in t)
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
    assert got[-1] == {"text": "How are things going?", "convo": "phone", "user": "U4KFSQCDQ"}
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


def test_donnas_voice_from_elevenlabs(monkeypatch):
    import io, json
    import urllib.request
    from . import webchat
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "k" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: calls.append(req) or io.BytesIO(b"ID3mp3"))
    H = webchat.make_handler(None, "https://x")
    sent = []

    def post(auth, text):
        h = H.__new__(H)
        body = json.dumps({"text": text}).encode()
        h.path, h.rfile, h.headers = "/api/donna/voice", io.BytesIO(body), {"Authorization": auth, "Content-Length": str(len(body))}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b, ctype))
        h.do_POST()
        return sent[-1]
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert post("Bearer " + "k" * 32, "Hi Harvey.")[0] == 204 and not calls  # not set up: the phone's own voice
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "VOICE1")
    assert post("Bearer " + "k" * 32, "Hi Harvey.") == (200, b"ID3mp3", "audio/mpeg")
    assert calls[-1].full_url.startswith("https://api.elevenlabs.io/v1/text-to-speech/VOICE1")
    assert calls[-1].headers["Xi-api-key"] == "el-test" and json.loads(calls[-1].data)["text"] == "Hi Harvey."
    assert json.loads(calls[-1].data)["voice_settings"]["speed"] == 1.12
    assert post("Bearer nope", "x")[0] == 404 and len(calls) == 1  # never spends voice credit for a stranger


def test_spoken_text_flows():
    from .webchat import smooth
    assert smooth("Two hundred a week — want more?") == "Two hundred a week, want more?"
    assert smooth("Here's the deal: towels are late...\nI'll call them.") == "Here's the deal, towels are late, I'll call them."


def test_only_account_questions_take_the_slow_lookup():
    from types import SimpleNamespace
    from .desk import Desk
    from .context import Alliant, Customer
    d = SimpleNamespace(alliant=Alliant(customers=[Customer("4000-1-01807", "SAFEWAY 1807 DELI", "4", ["Mon"])]))
    look = lambda t: Desk.sounds_like_lookup(d, t)
    assert look("how many mats does safeway get") and look("what's on route 4") and look("what about Safeway?")
    assert not look("should we raise prices next year") and not look("tell me a joke")


def test_elevenlabs_agent_gets_donnas_answers(monkeypatch):
    import io, json
    import urllib.request
    from .webchat import make_handler
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "k" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    got = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: got.append((req.full_url, json.loads(req.data))) or io.BytesIO(b"Hi Harvey."))
    H = make_handler(None, "https://x")

    def post(auth, body):
        raw = json.dumps(body).encode()
        h = H.__new__(H)
        h.path, h.rfile, h.headers = "/v1/chat/completions", io.BytesIO(raw), {"Authorization": auth, "Content-Length": str(len(raw))}
        sent, out = [], io.BytesIO()
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.send_response = lambda code: sent.append((code, None))
        h.send_header, h.end_headers, h.wfile = (lambda *a: None), (lambda: None), out
        h.do_POST()
        return sent[0][0], sent[0][1] if sent[0][1] is not None else out.getvalue()
    msgs = [{"role": "system", "content": "ElevenLabs' own prompt"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hey."}, {"role": "user", "content": "how are things?"}]
    assert post("Bearer wrong", {"messages": msgs})[0] == 401 and not got
    code, body = post("Bearer " + "k" * 32, {"messages": msgs, "model": "donna"})
    assert code == 200 and json.loads(body)["choices"][0]["message"]["content"] == "Hi Harvey."
    assert got[-1][0].endswith("/chat") and got[-1][1]["messages"] == msgs
    code, body = post("Bearer " + "k" * 32, {"messages": msgs, "stream": True})
    lines = [l for l in body.decode().split("\n\n") if l]
    assert json.loads(lines[0][6:])["choices"][0]["delta"]["content"] == "Hi Harvey." and lines[-1] == "data: [DONE]"


def test_desk_answers_a_conversation_with_its_own_history():
    from types import SimpleNamespace
    from .desk import Desk
    seen = {}
    d = SimpleNamespace(voice=lambda text, convo: seen.update(text=text, convo=convo, turns=list(d.talks[convo]["turns"])) or "OK")
    msgs = [{"role": "system", "content": "x"}, {"role": "assistant", "content": "Hi Scott."},
            {"role": "user", "content": "how are things"}, {"role": "assistant", "content": "Good."},
            {"role": "user", "content": [{"type": "text", "text": "and route 4?"}]}]
    assert Desk.voice_messages(d, msgs) == "OK"
    assert seen["text"] == "and route 4?" and seen["turns"] == [{"role": "user", "content": "how are things"},
                                                                {"role": "assistant", "content": "Good."}]


def test_elevenlabs_tool_asks_the_desk(monkeypatch):
    import io, json
    import urllib.request
    from .webchat import make_handler
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "k" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    got = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: got.append(json.loads(req.data)) or io.BytesIO(b"Twelve mats."))
    H = make_handler(None, "https://x")
    sent = []

    def post(auth):
        raw = b'{"question": "how many mats does safeway 1807 get"}'
        h = H.__new__(H)
        h.path, h.rfile, h.headers = "/api/donna/tool", io.BytesIO(raw), {"Authorization": auth, "Content-Length": str(len(raw))}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.do_POST()
        return sent[-1]
    assert post("Bearer no")[0] == 401 and not got
    assert post("Bearer " + "k" * 32) == (200, b'{"answer": "Twelve mats."}')
    assert got[-1]["text"] == "how many mats does safeway 1807 get"


def test_talk_page_gets_a_link_into_the_private_agent(monkeypatch):
    import io, json
    import urllib.error
    import urllib.request
    from .webchat import make_handler
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "k" * 32)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_x")
    monkeypatch.setattr("time.sleep", lambda s: None)
    asked = []

    def urlopen(req, timeout):
        asked.append((req.full_url, req.headers.get("Xi-api-key")))
        if "get-signed-url" in req.full_url:  # the other spelling works
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
        return io.BytesIO(b'{"signed_url": "wss://api.elevenlabs.io/x?token=1"}')
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    H = make_handler(None, "https://x")
    sent = []

    def post(auth):
        h = H.__new__(H)
        h.path, h.rfile, h.headers = "/api/donna/agent", io.BytesIO(b""), {"Authorization": auth, "Content-Length": "0"}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.do_POST()
        return sent[-1]
    assert post("Bearer nope")[0] == 401 and not asked
    code, body = post("Bearer " + "k" * 32)
    assert code == 200 and json.loads(body)["signed_url"].startswith("wss://")
    assert asked[-1] == ("https://api.elevenlabs.io/v1/convai/conversation/get_signed_url?agent_id=agent_x", "el-test")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert post("Bearer " + "k" * 32)[0] == 503


def test_route_questions_by_voice_skip_the_parse(monkeypatch):
    from types import SimpleNamespace
    from .desk import Desk
    from .context import Alliant, Customer
    import service_changes.parser as parser
    from .parser import Answer
    seen = {}
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, facts, asker="": seen.update(q=q, facts=facts, asker=asker)
                        or Answer(answer="*Thursday* on route 1: Carlile first, then Costco.", found=True))
    data = Alliant(customers=[Customer("1000-1-00002", "CARLILE", "1", ["Thu"]), Customer("1300-1-00001", "COSTCO", "2", ["Thu"])])
    d = SimpleNamespace(claude=None, alliant=data, asker=lambda u: "owner of the company, in the office", sku_answer=lambda t, u=None: "", owner_change=lambda k, t, c, **kw: None,
                        office_lookup=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no parse for a route")))
    assert Desk.voice(d, "what stops are on route 1 thursday", "r") == "Thursday on route 1: Carlile first, then Costco."
    assert "CARLILE" in seen["facts"] and "COSTCO" not in seen["facts"] and "owner" in seen["asker"]


def test_balances_only_for_scott_and_bill_in_private(monkeypatch, tmp_path):
    """The private money files (data/private/: ar, open invoices, pricing, account details) answer Scott and Bill in a
    private message or by voice, prices included; anyone else, or a channel, gets nothing from them."""
    from types import SimpleNamespace
    from .desk import Desk
    from .context import Alliant, Customer
    import service_changes.parser as parser
    from .parser import Answer
    priv = tmp_path / "private"
    priv.mkdir()
    (priv / "ar.csv").write_text("account,name,balance,over_90,last_payment_date,last_payment_amount\n"
                                 "1205-1-00004,MIDAS #4,\"1,240.50\",200.00,2026-09-30,600.00\n"
                                 "1205-1-00007,MIDAS 7,100.00,0,2026-10-20,50.00\n")
    (priv / "pricing.csv").write_text("account,sku,description,price,price_type\n1205-1-00004,5-01-01,TOWEL BAR MOP,0.42,8\n"
                                      "1205-1-00004,6-03-10,MAT 3X5 CHARCOAL,3.10,8\n")
    (priv / "open_invoices.csv").write_text("account,invoice,inv_date,amount,balance\n1205-1-00004,88123,2026-07-01,300.00,200.00\n")
    (priv / "account_details.csv").write_text("account,contract_expiry,sales_rep\n1205-1-00004,2027-03-31,Bill\n")
    data = Alliant(customers=[Customer("1205-1-00004", "MIDAS #4", "4", ["Mon"]), Customer("1205-1-00007", "MIDAS 7", "4", ["Thu"])])
    seen = []
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, facts, asker="": seen.append(facts) or Answer(answer="Owes $1,240.50.", found=True))
    said = []
    d = SimpleNamespace(alliant=data, desk_id="CDESK", claude=None, asker=lambda u: "owner", data_dir=str(tmp_path),
                        office_group=lambda t, p: [], say=None)
    parsed = SimpleNamespace(account_number="1205-1-00004")
    ask = lambda user, here, q="how much does Midas 4 owe us": Desk.ar_answer(d, user, q, parsed, here,
                                                                               lambda t, p: said.append((t, p["ar"])))
    ask("UDRIVER", "D123")
    assert said[-1][1] == "refused" and "aren't something I can share" in said[-1][0] and not seen
    ask("U9SGPJP0C", "CDESK")  # Bill, but in the office channel
    assert said[-1][1] == "private" and not seen
    ask("U9SGPJP0C", "D123")  # Bill, privately
    assert said[-1][1] == "answered" and "balance 1,240.50" in seen[-1] and "last payment date 2026-09-30" in seen[-1]
    assert "Open invoices (1" in seen[-1] and "TOWEL BAR MOP" not in seen[-1]  # only what a balance question needs
    ask("U4KFSQCDQ", "voice", "what do we charge Midas 4 for bar mops")  # Scott by voice: prices aren't filtered here
    assert said[-1][1] == "answered" and "price 0.42" in seen[-1] and "CHARCOAL" not in seen[-1]
    from .desk import AR_Q
    for q in ("what does Chili's pay for bar mops", "how much do we get for mats at Midas", "what are bar mops billed at",
              "what does a bath blanket cost Chili's", "has Midas paid"):
        assert AR_Q.search(q), q
    ask("U4KFSQCDQ", "D1", "when does the Midas 4 contract expire")
    assert "contract expiry 2027-03-31" in seen[-1]
    d.office_group = lambda t, p: ["1205-1-00004", "1205-1-00007"]
    ask("U4KFSQCDQ", "D1", "what do the Midas stores owe")
    assert "TOTAL owed across these 2 accounts (already added up): $1,340.50" in seen[-1]
    d.data_dir = str(tmp_path / "empty")
    ask("U4KFSQCDQ", "D1")
    assert said[-1][1] == "missing"


def test_drivers_and_customers_never_get_balances():
    from .context import Alliant, Customer, account_facts
    from .sms import customer_facts
    data = Alliant(customers=[Customer("1205-1-00004", "MIDAS #4", "4", ["Mon"])],
                   ar={"1205-1-00004": {"balance": "1240.50", "last_payment_date": "2026-09-30"}})
    for facts in (account_facts(data, "1205-1-00004"), customer_facts(data, "1205-1-00004")):
        assert "1240" not in facts and "2026-09-30" not in facts


def test_voice_answers_alliant_how_to_from_the_notes():
    from types import SimpleNamespace
    from .desk import Desk
    d = SimpleNamespace(claude=None, sku_answer=lambda t, u=None: "", owner_change=lambda k, t, c, **kw: None,
                        helper=SimpleNamespace(answer=lambda turns, user: ("1. Open *Go To Account*.\n2. Add the SKU in the "
                                               "*SKU Catalog*.\n\n_Source: 02 Customer Items_", False)),
                        office_lookup=lambda *a, **k: (_ for _ in ()).throw(AssertionError("not a lookup")))
    out = Desk.voice(d, "how do I add a new SKU to an account's catalog in Alliant", "v")
    assert "Go To Account" in out and "SKU Catalog" in out and "Source" not in out and "*" not in out


def test_bill_has_his_own_key_and_donna_knows_its_him(monkeypatch):
    """VOICE_KEYS gives Bill his own key; the agent conversation carries a session token (ElevenLabs {{donna_session}})
    back on every ask_donna call, so the desk hears it's Bill, and without it nobody is taken for Scott."""
    import io
    import json
    import urllib.request
    from . import webchat
    monkeypatch.setenv("OWNER_VOICE_TOKEN", "s" * 32)
    monkeypatch.setenv("VOICE_KEYS", "Bill:U9SGPJP0C:" + "b" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(webchat, "agent_link", lambda: "wss://signed")
    got = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: got.append(json.loads(req.data)) or io.BytesIO(b"Hi Bill."))
    assert webchat.voice_person("b" * 32) == ("U9SGPJP0C", "Bill") and webchat.voice_person("s" * 32)[1] == "Scott"
    assert webchat.voice_person("x" * 32) is None
    H = webchat.make_handler(None, "https://x")
    sent = []

    def post(path, key, body=b"{}"):
        h = H.__new__(H)
        h.path, h.rfile = path, io.BytesIO(body)
        h.headers = {"Authorization": "Bearer " + key, "Content-Length": str(len(body))}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.do_POST()
        return sent[-1]
    code, body = post("/api/donna/agent", "b" * 32)
    link = json.loads(body)
    assert code == 200 and link["name"] == "Bill" and link["signed_url"] == "wss://signed"
    ask = lambda extra: post("/api/donna/tool", "s" * 32, json.dumps({"question": "how many mats at chilis", **extra}).encode())
    assert json.loads(ask({"donna_session": link["session"]})[1])["answer"] == "Hi Bill."
    assert got[-1]["user"] == "U9SGPJP0C"
    assert "donna_session" in json.loads(ask({})[1])["answer"] and len(got) == 1  # no session: refused, not Scott
    assert "Hang up" in json.loads(ask({"donna_session": "made-up"})[1])["answer"]
    assert post("/api/donna/agent", "x" * 32)[0] == 401

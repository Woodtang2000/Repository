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

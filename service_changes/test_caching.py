"""Prompt caching helpers (caching.py): Guardrail's request, Scott OK 11/20."""
import json
from types import SimpleNamespace

from . import caching


def test_system_and_last_turn_are_marked_and_the_callers_turns_are_untouched():
    assert caching.system_blocks("rules") == [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "q"}]
    out = caching.mark_last(turns)
    assert out[-1]["content"] == [{"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}}]
    assert turns[-1]["content"] == "q" and out[0] is turns[0]  # a copy: the stored conversation isn't changed
    tools = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
    assert caching.mark_last(tools)[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[0]["content"][0]
    assert caching.mark_last([]) == [] and caching.flat(caching.system_blocks("a")) == "a"


def test_usage_is_tallied_by_day_and_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_DESK_DATA", str(tmp_path))
    r = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, cache_read_input_tokens=900, cache_creation_input_tokens=0,
                                              output_tokens=50))
    caching.note("webchat", r)
    caching.note("webchat", r)
    caching.note("phone", SimpleNamespace())  # no usage: ignored
    day = json.load(open(tmp_path / "claude_usage.json"))
    [d] = day.values()
    assert d["webchat"] == {"calls": 2, "input": 200, "cache_read": 1800, "cache_write": 0, "output": 100}
    assert "read share  90%" in caching.report()

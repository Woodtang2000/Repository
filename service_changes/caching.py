"""Prompt caching for Donna's Claude calls (Guardrail's request, Scott OK 11/20): the unchanging front of a request is
marked so repeat calls read it from Anthropic's cache at a fraction of the price, and each call's token use is tallied
by day so the saving can be checked ("python -m service_changes.caching" prints it).

- system_blocks(): the system prompt as one cached block (the website chat's facts sheet, the phone agent's rules,
  Scott's and Bill's briefing). Anything that changes per message stays out of it.
- mark_last(): a second breakpoint on the conversation's last block, so the next turn of the same conversation (or the
  next round of a tool loop) reads everything before it from the cache. A copy: the caller's turns aren't changed.
- note(): adds a response's usage to <data>/claude_usage.json, one entry per day and kind of call. Never raises.
Current models cache any prefix over 512 tokens; shorter ones simply aren't cached (no error, no extra cost).
"""
import json
import os
import threading
import time

CACHED = {"type": "ephemeral"}
_lock = threading.Lock()


def system_blocks(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "cache_control": CACHED}]


def mark_last(messages: list[dict]) -> list[dict]:
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, "cache_control": CACHED}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content = list(content)
        content[-1] = {**content[-1], "cache_control": CACHED}
        last["content"] = content
    else:
        return out
    out[-1] = last
    return out


def flat(content) -> str:
    """Text of a prompt given as a string or as content blocks (for tests and logs)."""
    if isinstance(content, str) or content is None:
        return content or ""
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def _path() -> str:
    return os.path.join(os.environ.get("SERVICE_DESK_DATA", os.path.join(os.path.dirname(__file__), "data")),
                        "claude_usage.json")


def note(kind: str, response) -> None:
    """Tally input, cache-read, cache-write and output tokens for today and this kind of call."""
    try:
        u = getattr(response, "usage", None)
        if u is None:
            return
        row = {"input": getattr(u, "input_tokens", 0) or 0, "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
               "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0, "output": getattr(u, "output_tokens", 0) or 0}
        if not any(isinstance(v, int) for v in row.values()):
            return
        day = time.strftime("%Y-%m-%d")
        with _lock:
            try:
                data = json.load(open(_path()))
            except (OSError, ValueError):
                data = {}
            d = data.setdefault(day, {}).setdefault(kind, {"calls": 0, "input": 0, "cache_read": 0, "cache_write": 0, "output": 0})
            d["calls"] += 1
            for k, v in row.items():
                d[k] += int(v) if isinstance(v, int) else 0
            data = {k: v for k, v in sorted(data.items())[-60:]}  # two months is plenty
            with open(_path() + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(_path() + ".tmp", _path())
    except Exception as e:  # counting is extra: never hold up a reply over it
        print(time.strftime("%H:%M"), "usage note:", str(e)[:120], flush=True)


def report(days: int = 7) -> str:
    """Per day and kind: calls, uncached input, cache reads and writes, output, and the share of input read from cache."""
    try:
        data = json.load(open(_path()))
    except (OSError, ValueError):
        return "No usage recorded yet."
    lines = []
    for day in sorted(data)[-days:]:
        tot = {"calls": 0, "input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
        for kind, d in sorted(data[day].items()):
            for k in tot:
                tot[k] += d[k]
            seen = d["input"] + d["cache_read"] + d["cache_write"]
            lines.append(f"{day} {kind:16} calls {d['calls']:4}  input {d['input']:9,}  cache read {d['cache_read']:9,}  "
                         f"cache write {d['cache_write']:8,}  output {d['output']:8,}  read share "
                         f"{(d['cache_read'] / seen * 100 if seen else 0):3.0f}%")
        seen = tot["input"] + tot["cache_read"] + tot["cache_write"]
        lines.append(f"{day} {'ALL':16} calls {tot['calls']:4}  input {tot['input']:9,}  cache read {tot['cache_read']:9,}  "
                     f"cache write {tot['cache_write']:8,}  output {tot['output']:8,}  read share "
                     f"{(tot['cache_read'] / seen * 100 if seen else 0):3.0f}%")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    if args.data:
        os.environ["SERVICE_DESK_DATA"] = args.data
    print(report(args.days))

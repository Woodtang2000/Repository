"""Colored to-do bars in Slack, the same as the HR bot's (REQUEST from the HR agent, 2026-10-26).

Slack can't color a message, so a message that asks someone to do something carries its text in one attachment with a
colored bar: gold while it's open, green when it's done (with who and when), grey when it was closed without doing.
Messages with their own button or reaction (tickets ✅, sign-up cards ✅, sales "Give to") keep it and turn green when
it's recorded; the rest get a ✓ Done button, and Done gets an Undo. Confirmations and reports get no bar.
Once done, a message shrinks to a line or two (its full text moves into its thread), so the channel stays readable.

Posted with no top-level text (it would show twice); Slack notifications use the attachment's fallback."""
from datetime import datetime

from .context import ALASKA

GOLD, GREEN, BLUE, GREY = "#f9af10", "#2eb67d", "#1d9bd1", "#9aa5a8"
DONE, UNDO = "task_done", "task_undo"


def _sections(text: str) -> list[dict]:
    """Section blocks of at most 3,000 characters, split on line breaks."""
    out, cur = [], ""
    for line in (text or " ").split("\n"):
        while len(line) > 2900:  # one very long line
            out.append(line[:2900])
            line = line[2900:]
        if cur and len(cur) + 1 + len(line) > 2900:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    out.append(cur or " ")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": t}} for t in out]


def _buttons(done: bool, undo: bool) -> list[dict]:
    els = []
    if done:
        els.append({"type": "button", "action_id": DONE, "value": "done", "text": {"type": "plain_text", "text": "✓ Done"}})
    if undo:
        els.append({"type": "button", "action_id": UNDO, "value": "undo", "text": {"type": "plain_text", "text": "Undo"}})
    return [{"type": "actions", "block_id": "task", "elements": els}] if els else []


def attachment(text: str, color: str = GOLD, button: bool = True, status: str = "", undo: bool = False,
               extra: list | None = None) -> list[dict]:
    """The attachments for a to-do message: its text, any blocks of its own (e.g. sales buttons), the status line, and
    ✓ Done (open) or Undo (done by the button)."""
    blocks = _sections(text) + list(extra or [])
    if status:
        blocks.append({"type": "context", "block_id": "status", "elements": [{"type": "mrkdwn", "text": status}]})
    blocks += _buttons(button and not status, undo)
    return [{"color": color, "fallback": plain(text)[:150], "blocks": blocks}]


def plain(text: str) -> str:
    return (text or "").replace("*", "").replace("_", "")


def stamp(who: str, verb: str = "Done", now: datetime | None = None) -> str:
    """":white_check_mark: Done · Bill · 10/26 2:15 PM" (Alaska time)."""
    t = (now or datetime.now(ALASKA)).astimezone(ALASKA)
    icon = ":white_check_mark:" if verb != "Closed" else ":no_entry_sign:"
    return f"{icon} {verb} · {who.split()[0] if who else 'someone'} · {t.month}/{t.day} {t.strftime('%-I:%M %p')}"


def body(m: dict) -> str:
    """A message's text: the top-level text, or what's in its to-do attachment."""
    if m.get("text"):
        return m["text"]
    return "\n".join(b["text"]["text"] for a in m.get("attachments") or [] for b in a.get("blocks") or []
                     if b.get("type") == "section" and b.get("block_id") != "status").strip()


def restyle(m: dict, color: str, status: str = "", button: bool = False, undo: bool = False) -> list[dict]:
    """The message's to-do attachment re-colored, with its status line and buttons replaced."""
    a = dict((m.get("attachments") or [{}])[0])
    blocks = [b for b in a.get("blocks") or [] if b.get("block_id") not in ("status", "task")]
    if not blocks:
        blocks = _sections(body(m))
    if status:
        blocks.append({"type": "context", "block_id": "status", "elements": [{"type": "mrkdwn", "text": status}]})
    blocks += _buttons(button, undo)
    a.update(color=color, blocks=blocks, fallback=a.get("fallback") or plain(body(m))[:150])
    return [a]


def summary(text: str, lines: int = 2) -> str:
    """The first line or two of a message, for the one-line version a done message shrinks to."""
    keep = [ln.strip() for ln in (text or "").split("\n") if ln.strip() and not ln.startswith(">")][:lines]
    return "  ·  ".join(keep)[:600] or (text or "")[:200]


def shrink(slack, channel: str, m: dict, color: str, status: str, undo: bool = False, lines: int = 2) -> list[dict]:
    """Done messages shrink so the channel stays readable: the full text goes into the message's thread (nothing is
    lost) and the message itself becomes a one-line summary with its status. Returns the new attachments."""
    full = body(m)
    try:
        slack.chat_postMessage(channel=channel, thread_ts=m["ts"], text=full, unfurl_links=False,
                               metadata={"event_type": "service_desk", "event_payload": {"kind": "todo_full"}})
    except Exception as e:  # never lose the status change over the thread copy
        print("todo: full text not copied to the thread:", e, flush=True)
        return restyle(m, color, status=status, undo=undo)
    return attachment(summary(full, lines), color=color, button=False, status=status, undo=undo)


def _full_text(slack, channel: str, ts: str) -> tuple[str, str] | None:
    """The full text a shrunk message put in its thread: (text, the thread reply's ts)."""
    r = slack.conversations_replies(channel=channel, ts=ts, limit=50, include_all_metadata=True)
    for m in reversed(r.get("messages", [])[1:]):
        if ((m.get("metadata") or {}).get("event_payload") or {}).get("kind") == "todo_full":
            return m.get("text", ""), m["ts"]
    return None


def handle_action(slack, payload: dict, name_of) -> str | None:
    """✓ Done and Undo on a to-do message: green with who and when, or back to gold. Returns what happened."""
    act = next((a for a in payload.get("actions", []) if a.get("action_id") in (DONE, UNDO)), None)
    if not act:
        return None
    user = payload.get("user", {}).get("id")
    msg, channel = payload.get("message") or {}, (payload.get("channel") or {}).get("id")
    if not user or not channel or not msg.get("ts") or msg.get("bot_id") is None and not msg.get("attachments"):
        return "ignored"
    if act["action_id"] == DONE:
        atts = shrink(slack, channel, msg, GREEN, stamp(name_of(user)), undo=True, lines=1)
    else:  # Undo: the full text back from the thread, gold, ✓ Done again
        found = _full_text(slack, channel, msg["ts"])
        atts = attachment(found[0]) if found else restyle(msg, GOLD, button=True)
        if found:
            try:
                slack.chat_delete(channel=channel, ts=found[1])
            except Exception:
                pass
    slack.chat_update(channel=channel, ts=msg["ts"], text="", attachments=atts)
    return f"{act['action_id']} {msg['ts']}"

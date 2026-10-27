from datetime import datetime

from . import todo
from .context import ALASKA


class Slack:
    def __init__(self):
        self.updates, self.thread, self.deleted = [], [], []

    def chat_update(self, **kw):
        self.updates.append(kw)

    def chat_postMessage(self, **kw):
        self.thread.append({"ts": f"2.{len(self.thread)}", "text": kw["text"], "metadata": kw.get("metadata")})
        return {"ts": self.thread[-1]["ts"]}

    def conversations_replies(self, **kw):
        return {"messages": [{"ts": "1.0"}] + self.thread}

    def chat_delete(self, channel, ts):
        self.deleted.append(ts)


def test_open_done_undo():
    [a] = todo.attachment("*W1* WENDY'S\nPlease check the mats")
    assert a["color"] == todo.GOLD and a["fallback"].startswith("W1 WENDY'S")
    assert a["blocks"][-1]["elements"][0]["action_id"] == todo.DONE
    msg = {"ts": "1.0", "bot_id": "B", "text": "", "attachments": [a]}
    payload = {"actions": [{"action_id": todo.DONE}], "user": {"id": "U1"}, "channel": {"id": "C1"}, "message": msg}
    slack = Slack()
    assert todo.handle_action(slack, payload, lambda u: "Bill Dubinsky") == "task_done 1.0"
    [done] = slack.updates[-1]["attachments"]
    assert done["color"] == todo.GREEN and slack.updates[-1]["text"] == ""
    status = next(b for b in done["blocks"] if b.get("block_id") == "status")["elements"][0]["text"]
    assert status.startswith(":white_check_mark: Done · Bill · ")
    assert [e["action_id"] for e in done["blocks"][-1]["elements"]] == [todo.UNDO]
    assert todo.body({"attachments": [done]}) == "*W1* WENDY'S"  # shrunk to its first line
    assert slack.thread[0]["text"] == "*W1* WENDY'S\nPlease check the mats"  # the full text, in the thread
    payload["actions"], payload["message"] = [{"action_id": todo.UNDO}], {**msg, "attachments": [done]}
    todo.handle_action(slack, payload, lambda u: "Bill Dubinsky")
    [back] = slack.updates[-1]["attachments"]
    assert back["color"] == todo.GOLD and not any(b.get("block_id") == "status" for b in back["blocks"])
    assert [e["action_id"] for e in back["blocks"][-1]["elements"]] == [todo.DONE]
    assert todo.body({"attachments": [back]}) == "*W1* WENDY'S\nPlease check the mats" and slack.deleted == ["2.0"]
    assert todo.handle_action(slack, {"actions": [{"action_id": "sales_give_bill"}]}, str) is None


def test_long_messages_split_and_stamp():
    [a] = todo.attachment("x" * 2000 + "\n" + "y" * 2000, button=False)
    sections = [b for b in a["blocks"] if b["type"] == "section"]
    assert len(sections) == 2 and all(len(b["text"]["text"]) <= 3000 for b in sections)
    assert not any(b["type"] == "actions" for b in a["blocks"])
    when = datetime(2026, 10, 26, 14, 15, tzinfo=ALASKA)
    assert todo.stamp("Bill Dubinsky", now=when) == ":white_check_mark: Done · Bill · 10/26 2:15 PM"

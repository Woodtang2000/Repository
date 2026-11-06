"""#customer-messages: one line per person per day, every message in its thread; Scott's and staff's never."""
import pytest

from . import comms


class Slack:
    def __init__(self):
        self.posts, self.updates = [], []

    def conversations_list(self, **kw):
        return [{"channels": [{"id": "CM", "name": "customer-messages", "is_member": True}]}]

    def chat_postMessage(self, channel, text, thread_ts=None, **kw):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ts": f"{len(self.posts)}.0"}

    def chat_update(self, channel, ts, text, **kw):
        self.updates.append((ts, text))


@pytest.fixture
def slack(tmp_path, monkeypatch):
    s = Slack()
    monkeypatch.setattr(comms, "_client", s)
    monkeypatch.setattr(comms, "SYNC", True)
    monkeypatch.setattr(comms, "_channel", {})
    monkeypatch.setenv("SERVICE_DESK_DATA", str(tmp_path))
    monkeypatch.setenv("OWNER_PHONES", "9072306513")
    (tmp_path / "customer_phones.csv").write_text("phone,accounts,name\n(907) 406-9656,5040-1-00000,Toni\n")
    (tmp_path / "customers.csv").write_text("account,name,route,service_days\n5040-1-00000,CHILIS,4,Tue\n")
    return s


def test_one_line_per_person_per_day_with_everything_in_its_thread(slack):
    comms.log("in", "text", "+19074069656", "Switch the 4x8 and 3x5 to Waterhog please")
    comms.log("out", "text", "+19074069656", "Happy to! Which color Waterhog?")
    top = [p for p in slack.posts if p["thread_ts"] is None]
    assert len(top) == 1 and "Toni" in top[0]["text"] and "5040-1-00000 CHILIS" in top[0]["text"]
    thread = [p["text"] for p in slack.posts if p["thread_ts"] == "1.0"]
    assert "⬅️ *Toni* by text" in thread[0] and "Waterhog" in thread[0]
    assert "➡️ *Donna* by text" in thread[1] and "Which color" in thread[1]
    assert "2 messages today" in slack.updates[-1][1]


def test_scotts_and_staff_messages_are_never_logged(slack):
    comms.log("out", "text", "+19072306513", "Donna: Matson owes 1,234.00")
    comms.log("out", "email", "kirk@snowwhitelinen.com", "Special S0148780")
    assert slack.posts == []



def test_tests_never_reach_the_real_slack(monkeypatch):
    monkeypatch.setattr(comms, "_client", None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-real-looking")
    monkeypatch.setattr(comms, "_post", lambda *a: (_ for _ in ()).throw(AssertionError("posted")))
    comms.log("in", "email", "jane@acme.com", "add 200 shop towels")  # pytest is running: nothing posted

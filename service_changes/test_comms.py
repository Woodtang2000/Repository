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


def test_an_unlinked_email_gets_a_suggestion_and_buttons(slack, tmp_path, monkeypatch):
    (tmp_path / "customers.csv").write_text("account,name,route,service_days\n5040-1-00000,CHILIS,4,Tue\n"
                                            "1950-1-00000,ACME TIRE,2,Mon\n1950-1-00001,ACME TIRE WASILLA,2,Mon\n")
    (tmp_path / "customer_cards.csv").write_text("account,name,route,email,location_email,phone\n"
                                                 "1950-1-00000,ACME TIRE,2,ap@acmetire.com,,\n")
    comms._ALLIANT.clear()
    monkeypatch.setattr(comms, "_client", slack)
    import service_changes.sms as sms
    monkeypatch.setattr(sms, "open_card", lambda *a, **k: None)
    monkeypatch.setattr(sms, "cards_channel", lambda s, f: "CC")
    seen = []
    slack.chat_postMessage = (lambda orig: lambda channel, text, thread_ts=None, **kw: (seen.append(kw), orig(channel, text, thread_ts))[1])(slack.chat_postMessage)
    comms.log("in", "email", "jane@acmetire.com", "Can we add 20 bar towels?")
    head = seen[0]
    blocks = head["blocks"]
    assert "Best match: *1950-1-00000* ACME TIRE" in blocks[1]["elements"][0]["text"]
    ids = [e["action_id"] for e in blocks[2]["elements"]]
    assert ids[0] == "msglink_add1" and "msglink_all" in ids and "msglink_pick" in ids and "msglink_block" not in ids
    import json
    assert json.loads(blocks[2]["elements"][0]["value"]) == {"a": "1950-1-00000", "m": 0, "k": "jane@acmetire.com"}


def test_a_linked_customer_gets_no_buttons(slack, monkeypatch):
    seen = []
    slack.chat_postMessage = (lambda orig: lambda channel, text, thread_ts=None, **kw: (seen.append(kw), orig(channel, text, thread_ts))[1])(slack.chat_postMessage)
    comms.log("in", "text", "+19074069656", "Thanks!")
    assert "blocks" not in seen[0]

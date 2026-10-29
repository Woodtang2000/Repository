from types import SimpleNamespace

from . import announce
from .context import Alliant, Customer


class Slack:
    def __init__(self):
        self.posts, self.updates = [], []

    def chat_postMessage(self, channel, text, **kw):
        self.posts.append({"channel": channel, "text": text, **kw})
        return {"ts": "5.5", "channel": channel}

    def chat_update(self, channel, ts, text, **kw):
        self.updates.append(text)


def desk(tmp_path):
    (tmp_path / "customer_phones.csv").write_text("phone,accounts,name\n(907) 555-1234,1205-1-00004,Jane at Midas\n"
                                                  "(907) 555-9999,7000-2-00001,Hospital\n")
    data = Alliant(customers=[Customer("1205-1-00004", "MIDAS #4", "4", ["Thu"]), Customer("1300-1-00001", "COSTCO", "4", ["Thu"])],
                   cards={"1205-1-00004": {"email": "ap@midas.com; owner@midas.com"}, "1300-1-00001": {"email": "deli@costco.com"}},
                   stops=[{"branch": "1", "route": "4", "day": "Thu", "account": "1205-1-00004"},
                          {"branch": "1", "route": "4", "day": "Thu", "account": "1300-1-00001"},
                          {"branch": "2", "route": "4", "day": "Thu", "account": "7000-2-00001"}])
    posts = []
    d = SimpleNamespace(alliant=data, data_dir=str(tmp_path), desk_id="CDESK", slack=Slack(), claude=None,
                        is_office=lambda u: u == "UANA", is_owner=lambda u: u == "U4KFSQCDQ", name=lambda u: "Ana Faamita",
                        post=lambda ch, text, payload, **kw: posts.append(text))
    d.posted = posts
    return d


def test_route_4_is_running_late(tmp_path, monkeypatch):
    d = desk(tmp_path)
    monkeypatch.setattr(announce, "parse", lambda c, t, today: announce.Announcement(
        is_announcement=True, branch="1", route="4", day="Thu", subject="Running late today",
        message="Our route 4 truck is running about 2 hours late today. Sorry for the wait!"))
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body: sent.append(("text", to, body)))
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: sent.append(("email", to, subject)))
    a = announce.Announcer(d)
    assert not a.propose({"ts": "1.1", "user": "UDRIVER", "text": "Donna, let route 4 customers know we're late"})
    assert a.propose({"ts": "1.1", "user": "UANA", "text": "Donna, let route 4 customers know we're 2 hours late"})
    p = d.slack.posts[-1]
    assert "2 customers (Snow White · route 4 · Thu deliveries): 1 texts, 3 emails" in p["text"] and not sent
    assert p["blocks"][1]["elements"][0]["text"]["text"] == "Send to 4"  # the TLG route 4 hospital isn't in it
    tap = lambda user, act: a.act({"user": {"id": user}, "actions": [{"action_id": act, "value": "1.1"}],
                                   "container": {"message_ts": "5.5"}})
    assert tap("UDRIVER", announce.SEND) == "ignored: not office" and not sent
    assert tap("UANA", announce.SEND).startswith("sent")
    assert ("text", "+19075551234", "Snow White Linen: Our route 4 truck is running about 2 hours late today. Sorry for the wait!") in sent
    assert {s[1] for s in sent if s[0] == "email"} == {"ap@midas.com", "owner@midas.com", "deli@costco.com"}
    assert "Sent by Ana to Snow White · route 4" in d.slack.updates[-1]
    assert tap("UANA", announce.SEND) == "already handled" and len(sent) == 4


def test_unclear_who_asks_first(tmp_path, monkeypatch):
    d = desk(tmp_path)
    monkeypatch.setattr(announce, "parse", lambda c, t, today: announce.Announcement(
        is_announcement=True, message="We're closed Friday.", subject="Closed Friday"))
    assert announce.Announcer(d).propose({"ts": "1.1", "user": "UANA", "text": "announce: we're closed friday"})
    assert "Which customers should get it?" in d.posted[-1]
    assert announce.ASK.search("Donna, let route 4 customers know we're running 2 hours late")
    assert announce.ASK.search("Announce: Thanksgiving, Thursday deliveries move to Wednesday")
    assert not announce.ASK.search("how many mats does Midas get")

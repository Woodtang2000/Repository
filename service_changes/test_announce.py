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
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: sent.append(("text", to, body)))
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
    assert ("text", "+19075551234", "Snow White Linen: Our route 4 truck is running about 2 hours late today. Sorry for the wait! Reply STOP to opt out.") in sent
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


def test_a_burst_that_bird_refuses_is_retried_and_misses_can_be_resent(tmp_path, monkeypatch):
    # Thanksgiving notice, 11/17: 10 emails went out, the other 165 got "429 Too Many Requests" and were never retried
    import urllib.error
    from types import SimpleNamespace
    from . import announce, bird
    import service_changes.sms as sms
    waits, sent, calls = [], [], {"n": 0}

    def flaky(to, subject, text, **kw):
        calls["n"] += 1
        if to == "down@x.com" or calls["n"] in (2, 3):  # Bird says slow down twice, and one address never works
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "3"}, None)
        sent.append(to)
    monkeypatch.setattr(bird, "send", flaky)
    monkeypatch.setattr(bird, "signature", lambda company: "Donna")
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: sent.append(to))
    a = {"at": 1.0, "message": "Closed Thursday.", "subject": "Thanksgiving", "who": "all",
         "phones": {"+19075551234": "1205-1-00004"}, "emails": {"a@x.com": "1205-1-00004", "b@x.com": "1205-1-00004",
                                                                  "down@x.com": "1205-1-00004"}}
    ann = announce.Announcer(SimpleNamespace(data_dir=str(tmp_path)))
    out = ann.send(a, sleep=waits.append)
    assert out["emails"] == 2 and out["texts"] == 1 and out["missing"] == {"phones": [], "emails": ["down@x.com"]}
    assert 20.0 in waits and set(sent) == {"+19075551234", "a@x.com", "b@x.com"}  # retried after waiting out part of Bird's minute
    again = ann.send(a, only={"phones": [], "emails": ["b@x.com"]}, sleep=waits.append)
    assert again["emails"] == 1 and sent[-1] == "b@x.com"  # a resend goes only to the ones named
    # a notice sent before misses were kept: the message log says who got it that day
    import json
    (tmp_path / "comms_threads.json").write_text(json.dumps({"2026-11-17|a@x.com": "1.0", "2026-11-17|9075551234": "2.0",
                                                             "2026-11-16|b@x.com": "3.0"}))
    assert announce.delivered_on(str(tmp_path), ["2026-11-17"]) == {"a@x.com", "9075551234"}


def test_when_the_email_service_is_down_she_stops_and_keeps_the_rest(tmp_path, monkeypatch):
    # 11/19 resend: Bird answered "503 Service Unavailable" to every email
    import urllib.error
    from types import SimpleNamespace
    from . import announce, bird
    tries = []
    def down(to, subject, text, **kw):
        tries.append(to)
        raise urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
    monkeypatch.setattr(bird, "send", down)
    monkeypatch.setattr(bird, "signature", lambda company: "Donna")
    a = {"at": 1.0, "message": "m", "subject": "s", "who": "all", "phones": {},
         "emails": {f"c{i}@x.com": "1205-1-00004" for i in range(20)}}
    out = announce.Announcer(SimpleNamespace(data_dir=str(tmp_path))).send(a, sleep=lambda s: None)
    assert out["emails"] == 0 and len(out["missing"]["emails"]) == 20  # all kept for the resend
    assert len(set(tries)) == announce.GIVE_UP  # five tried (each retried), then she stopped


def test_a_notice_is_one_line_in_customer_messages_not_one_per_customer(tmp_path, monkeypatch):
    # 11/18: the Thanksgiving resend posted a line per customer, 119 in a row, in #customer-messages
    from types import SimpleNamespace
    from . import announce, bird, comms
    import service_changes.sms as sms
    logged, notes, flags = [], [], []
    monkeypatch.setattr(comms, "log", lambda *a, **kw: logged.append(a))
    monkeypatch.setattr(comms, "note", notes.append)
    monkeypatch.setattr(bird, "_call", lambda *a, **kw: None)
    monkeypatch.setattr(bird, "signature", lambda company: "Donna")
    monkeypatch.setattr(sms, "send_sms", lambda to, body, log=True: flags.append(log))
    a = {"at": 1.0, "message": "Closed Thursday.", "subject": "Thanksgiving", "who": "all",
         "phones": {"+19075551234": "1205-1-00004"}, "emails": {"a@x.com": "1205-1-00004", "b@x.com": "1205-1-00004"}}
    out = announce.Announcer(SimpleNamespace(data_dir=str(tmp_path))).send(a, sleep=lambda s: None)
    assert out["emails"] == 2 and out["texts"] == 1
    assert logged == [] and flags == [False]  # nothing per customer
    assert len(notes) == 1 and "1 texts, 2 emails" in notes[0] and "Closed Thursday." in notes[0]

"""Outreach emails Donna sends in a rep's name (outreach.py) and their Slack cards (desk.outreach_*)."""
from . import outreach
from .test_desk import setup  # noqa: F401  (the desk fixture)

DESC = ("Why: Alsco customer, contract ends 2025-11-03. Touch 1 of 6 (track A).\nPhone: 907-522-3700\n\n"
        "Copy this email into Gmail:\nTo: Elizabeth Filkins <efilkins@inletworldwideoil.com>\n"
        "Subject: Comparing your uniform service before it renews\n\nHi Elizabeth,\n\nI'm Jesse with Snow White.\n\n"
        "Would a 15-minute Zoom this week work?\n\nThanks,\n\nJesse Tingley\nSnow White Inc · 700 I St, Anchorage, AK 99501\n"
        "Direct (907) 351-5662 · Office (907) 222-7738\nIf you'd rather not hear from me, just reply \"no thanks\".\n\n"
        "When done, set Status = Completed and pick an Outcome.")


def test_the_email_comes_out_of_the_task_comments():
    e = outreach.parse(DESC)
    assert e["to"] == "efilkins@inletworldwideoil.com" and e["to_name"] == "Elizabeth Filkins"
    assert e["subject"] == "Comparing your uniform service before it renews"
    assert e["body"].startswith("Hi Elizabeth,") and e["body"].endswith('reply "no thanks".')
    assert "When done" not in e["body"] and "Subject" not in e["body"]
    assert outreach.parse("Why: call them.\nPhone: 555") is None  # a call task


def test_the_layout_keeps_the_reps_signature():
    h = outreach.html(outreach.parse(DESC)["body"])
    assert "logo/sw.png" in h and "Jesse Tingley" in h and "Hi Elizabeth," in h and "donna-avatar" not in h


class SF:
    def __init__(self, closed=False, opted_out=False):
        self.patched, self.closed, self.opted_out = [], closed, opted_out

    def query(self, soql):
        if "FROM Contact" in soql:
            return [{"Email": "x@y.z", "HasOptedOutOfEmail": self.opted_out}]
        return [{"Id": "00T1", "Subject": "Outreach: Email – Jiffy Lube", "IsClosed": self.closed, "Description": DESC,
                 "WhoId": "003X", "What": {"Name": "Jiffy Lube"}}]

    def patch(self, obj, rid, body):
        self.patched.append((obj, rid, body))


def _wire(monkeypatch, sf):
    from . import bird
    sent = []
    monkeypatch.setattr(outreach, "_sf", lambda: (sf.query, sf.patch, lambda v: f"'{v}'"))
    monkeypatch.setattr(bird, "_call", lambda method, path, body=None, key="", **kw: sent.append((body, key)))
    monkeypatch.setenv("EMAIL_FROM", "donna@mail.snowwhitelinen.com")
    return sent


def test_send_goes_out_in_jesses_name_and_closes_the_task(monkeypatch):
    sf = SF()
    sent = _wire(monkeypatch, sf)
    rep = outreach.reps()[0]
    assert outreach.send("00T1", rep, "Jesse") == "sent to efilkins@inletworldwideoil.com"
    [(body, key)] = sent
    assert body["from"] == {"email": "jesse@mail.snowwhitelinen.com", "name": "Jesse Tingley – Snow White"}
    assert body["reply_to"] == ["jesse@snowwhitelinen.com"] and body["category"] == "marketing" and key == "outreach-00T1"
    [(obj, rid, upd)] = sf.patched
    assert obj == "Task" and upd["Status"] == "Completed" and upd["Outcome__c"] == "Done - no answer"
    assert "Sent by Donna in Jesse's name" in upd["Description"]


def test_never_sent_twice_or_to_an_opted_out_contact(monkeypatch):
    sent = _wire(monkeypatch, SF(closed=True))
    assert outreach.send("00T1", outreach.reps()[0], "Jesse") == "already closed in Salesforce" and not sent
    sent = _wire(monkeypatch, SF(opted_out=True))
    assert "Email Opt Out" in outreach.send("00T1", outreach.reps()[0], "Jesse") and not sent


def test_cards_and_buttons(setup, monkeypatch, tmp_path):
    from . import desk
    slack, d = setup
    d.data_dir = str(tmp_path)
    slack.msgs["UPZG94HPF"] = []
    slack.users.update({"UPZG94HPF": "Jesse James Tingley"})
    task = SF().query("Task")[0]
    monkeypatch.setattr(outreach, "due", lambda rep: [dict(task, Id="00T1", email=outreach.parse(DESC)),
                                                      dict(task, Id="00T2", email=outreach.parse(DESC))])
    calls = []
    monkeypatch.setattr(outreach, "send", lambda tid, rep, by: calls.append((tid, by)) or "sent to efilkins@inletworldwideoil.com")
    told = []
    slack.chat_postEphemeral = lambda channel, user, text: told.append(text)
    rep = outreach.reps()[0]
    assert d.outreach_cards(rep) == "2 outreach emails posted for Jesse."
    assert d.outreach_cards(rep).startswith("No outreach")  # not carded twice a day
    cards = [m for m in slack.msgs["UPZG94HPF"] if desk.meta(m) and desk.meta(m).get("kind") == "outreach"]
    assert len(cards) == 2 and "Subject:* Comparing your uniform service" in cards[0]["text"]
    tap = lambda user, aid, value: {"user": {"id": user}, "container": {"channel_id": "UPZG94HPF"},
                                    "actions": [{"action_id": aid, "value": value}]}
    d.outreach_action(tap("U2", "outreach_send", "00T1"))
    assert told and not calls  # Sonja can't send Jesse's email
    d.outreach_action(tap("UPZG94HPF", "outreach_skip", "00T2"))
    assert "Skipped by Jesse" in cards[1]["text"]
    d.outreach_action(tap("UPZG94HPF", "outreach_all", "UPZG94HPF"))
    assert calls == [("00T1", "Jesse")] and "Sent · Jesse" in cards[0]["text"]  # the skipped one stays skipped
    d.outreach_action(tap("UPZG94HPF", "outreach_send", "00T1"))
    assert len(calls) == 1  # never twice

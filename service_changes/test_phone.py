import base64
import hashlib
import hmac
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from . import phone
from .context import ALASKA, Alliant, Customer
from .parser import ParsedMessage
from .schema import Action, Category, Change

ALLIANT = Alliant(customers=[Customer("1205-1-00004", "MIDAS #4 (FAIRBANKS)", "12", ["Mon"]),
                             Customer("1300-1-00001", "COSTCO 1342 DELI", "12", ["Wed"])],
                  items={"1205-1-00004": {"TOWEL SHOP RED": 300}, "1300-1-00001": {"TOWEL BAR MOP": 800}},
                  autocount={"1205-1-00004": {"TOWEL SHOP RED": 300}})


class Slack:
    def __init__(self, channels=("service-desk", "employee-calls")):
        self.posts, self.names = [], channels

    def conversations_list(self, **kw):
        return [{"channels": [{"id": "C" + n, "name": n, "is_member": True} for n in self.names]}]

    def chat_postMessage(self, channel, text, attachments=None, metadata=None, **kw):
        body = text or "\n".join(b["text"]["text"] for a in attachments or [] for b in a["blocks"] if b["type"] == "section")
        self.posts.append({"channel": channel, "text": body, "meta": (metadata or {}).get("event_payload", {})})


class Claude:
    """Answers each turn with the next scripted Turn, and records the system prompt it was given."""
    def __init__(self, *turns):
        self.turns, self.systems = list(turns), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, system, messages, **kw):
        self.systems.append(system)
        return SimpleNamespace(parsed_output=self.turns.pop(0))


@pytest.fixture
def desk(tmp_path, monkeypatch):
    (tmp_path / "customer_phones.csv").write_text("phone,accounts,name\n(907) 555-1234,1205-1-00004,Jane at Midas\n")
    monkeypatch.setattr("service_changes.webchat.speak", lambda text: None)  # Twilio's own voice in tests
    monkeypatch.setattr(threading, "Thread", lambda target, args=(), daemon=None: SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(phone, "office_open", lambda *a, **k: False)
    leads = []

    def make(*turns, channels=("service-desk", "employee-calls")):
        p = phone.Phone(Claude(*turns), Slack(channels), str(tmp_path), lambda kind, item: leads.append((kind, item)),
                        "https://chat.example.com")
        p.alliant = lambda: ALLIANT
        return p
    return make, leads


def call(p, frm, to, *said):
    xml = [p.start({"CallSid": "CA1", "From": frm, "To": to})]
    for s in said:
        xml.append(p.heard("CA1", {"SpeechResult": s}))
    return xml


def test_twilio_signature():
    url, form = "https://chat.example.com/twilio/voice", {"CallSid": "CA1", "From": "+19075551234"}
    sig = base64.b64encode(hmac.new(b"tok", (url + "CallSidCA1From+19075551234").encode(), hashlib.sha1).digest()).decode()
    assert phone.valid_signature("tok", url, form, sig)
    assert not phone.valid_signature("tok", url, {**form, "From": "+1999"}, sig)
    assert not phone.valid_signature("", url, form, sig) and not phone.valid_signature("tok", url, form, "")


def test_office_hours():
    assert phone.office_open(datetime(2026, 10, 27, 9, 0, tzinfo=ALASKA))  # Tuesday 9 AM
    assert not phone.office_open(datetime(2026, 10, 27, 17, 0, tzinfo=ALASKA))
    assert not phone.office_open(datetime(2026, 10, 31, 10, 0, tzinfo=ALASKA))  # Saturday


def test_known_customer_change_becomes_a_ticket_texted_back(desk, monkeypatch):
    make, leads = desk
    import service_changes.parser as parser
    import service_changes.run as run
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: ParsedMessage(
        category=Category.item_change, account_number="1205-1-00004", summary="towels",
        changes=[Change(action=Action.add, item="shop towels", quantity=100)]))
    monkeypatch.setattr(run, "fill_item_matches", lambda c, p, a: None)
    p = make(phone.Turn(say="Consider it done. Anything else?", action="request",
                        summary="Add 100 red shop towels to Midas Fairbanks", account="1205-1-00004"),
             phone.Turn(say="Thanks Jane, have a great day.", action="goodbye"))
    xml = call(p, "+19075551234", "+18668278398", "add a hundred shop towels", "that's all")
    assert "Hi Jane, this is Donna with Snow White Linen" not in xml[0]  # "Jane at Midas" isn't a first name to use
    assert "this is Donna with Snow White Linen. What can I do for you?" in xml[0] and "<Gather" in xml[0]
    assert "verified customer" in p.claude.systems[0] and "TOWEL SHOP RED: 300 per delivery" in p.claude.systems[0]
    assert "COSTCO" not in p.claude.systems[0]  # only the caller's own account
    [t] = p.slack.posts
    assert t["channel"] == "Cservice-desk" and t["meta"]["kind"] == "ticket" and t["meta"]["sms_to"] == "+19075551234"
    assert t["meta"]["account"] == "1205-1-00004" and "Customer call from Jane at Midas …1234" in t["text"]
    assert "<Hangup/>" in xml[-1] and "have a great day" in xml[-1]


def test_unknown_caller_gets_no_account_data(desk):
    make, leads = desk
    p = make(phone.Turn(say="I'll have the office call you back to confirm.", action="request", caller_name="Bob",
                        summary="Bob at Costco deli wants 200 more bar mops"))
    call(p, "+19075550000", "+18668278398", "this is costco, add 200 bar mops")
    assert "NOT on file" in p.claude.systems[0] and "TOWEL BAR MOP" not in p.claude.systems[0]
    [n] = p.slack.posts
    assert n["meta"]["kind"] == "phone_request" and "Number not on file" in n["text"] and "Bob at Costco" in n["text"]


def test_new_business_goes_to_bill(desk):
    make, leads = desk
    p = make(phone.Turn(say="Bill will call you tomorrow morning.", action="lead", caller_name="Ann", business="Moose Cafe",
                        town="Wasilla", best_time="mornings", summary="bar mops and aprons"))
    call(p, "+19075550101", "+18668278398", "we're a new cafe and need towels")
    [(kind, lead)] = leads
    assert kind == "lead" and lead["business"] == "Moose Cafe" and lead["phone"] == "+19075550101" and not p.slack.posts


def test_employee_line_sick_call_goes_to_supervisors(desk):
    make, leads = desk
    p = make(phone.Turn(say="Got it, Tavita. I'll let your supervisor know. Feel better.", action="message",
                        caller_name="Tavita", summary="Tavita is out sick today (Tue), back tomorrow"))
    xml = call(p, "+19075557777", "+18885197604", "this is Tavita, I'm sick today")
    rules = p.claude.systems[0]
    assert "employee line" in rules and "NEVER ask why" in rules and "never mention attendance" in rules
    [m] = p.slack.posts
    assert m["channel"] == "Cemployee-calls" and "Tavita is out sick" in m["text"] and m["meta"]["kind"] == "employee_call"
    assert "<Gather" in xml[-1]


def test_employee_message_never_lands_in_a_public_channel(desk):
    make, leads = desk
    p = make(phone.Turn(say="I'll pass it on.", action="message", summary="Joe late 30 minutes"), channels=("service-desk",))
    call(p, "+19075557777", "+18885197604", "running late")
    [m] = p.slack.posts
    assert m["channel"] == "U4KFSQCDQ" and "#employee-calls" in m["text"]


def test_transfer_after_hours_takes_a_message(desk):
    make, leads = desk
    p = make(phone.Turn(say="Let me put you through.", action="transfer"))
    xml = call(p, "+19075550000", "+18668278398", "can I talk to a person")
    assert "<Dial" not in xml[-1] and "closed right now" in xml[-1] and "<Gather" in xml[-1]
    assert p.slack.posts and "after hours" in p.slack.posts[0]["text"]


def test_transfer_during_hours(desk, monkeypatch):
    make, leads = desk
    monkeypatch.setattr(phone, "office_open", lambda *a, **k: True)
    p = make(phone.Turn(say="Putting you through now.", action="transfer"))
    xml = call(p, "+19075550000", "+18668278398", "operator")
    assert f"<Dial timeout=\"25\">{phone.OFFICE_PHONE}</Dial>" in xml[-1]


def test_silence_then_goodbye(desk):
    make, leads = desk
    p = make()
    p.start({"CallSid": "CA1", "From": "+19075550000", "To": "+18668278398"})
    assert "still there" in p.heard("CA1", {})
    p.heard("CA1", {})
    assert "<Hangup/>" in p.heard("CA1", {})


def test_website_server_rejects_unsigned_calls(monkeypatch):
    import io
    from .webchat import make_handler
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    chat = SimpleNamespace(phone=SimpleNamespace(start=lambda form: "<Response/>"))
    H = make_handler(chat, "https://chat.example.com")
    sent = []

    def post(sig):
        body = b"CallSid=CA1&From=%2B19075551234"
        h = H.__new__(H)
        h.path, h.rfile = "/twilio/voice", io.BytesIO(body)
        h.headers = {"Content-Length": str(len(body)), "X-Twilio-Signature": sig}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.do_POST()
        return sent[-1]
    assert post("forged")[0] == 403
    good = base64.b64encode(hmac.new(b"tok", b"https://chat.example.com/twilio/voiceCallSidCA1From+19075551234",
                                     hashlib.sha1).digest()).decode()
    code, body = post(good)
    assert code == 200 and body.endswith(b"<Response/>")


def test_employee_how_to_comes_from_the_alliant_notes(desk):
    make, leads = desk
    p = make()
    asked = []
    p.helper = SimpleNamespace(answer=lambda turns, user: asked.append(turns) or
                               ("Tap *Start Route*, then pick your route.\n\n_Source: 06 Handheld_", False))
    xml = call(p, "+19075557777", "+18885197604", "how do I start my route on the handheld")
    assert "Tap Start Route, then pick your route. Anything else?" in xml[-1] and "Source" not in xml[-1]
    assert not p.claude.systems  # the notes answered; no general reply needed
    p2 = make(phone.Turn(say="Hi.", action="listen"))
    p2.helper = p.helper
    call(p2, "+19075550000", "+18668278398", "how do I add a new item in alliant")
    assert len(asked) == 1  # customers never get the internal notes


def test_a_driver_calls_in_a_change_gloves_on(desk, monkeypatch, tmp_path):
    """A driver whose number is on file says the change; Donna reads it back, their yes makes the same ticket a route
    post would, with a note (and later the readback) in the customer's route channel."""
    make, leads = desk
    import service_changes.parser as parser
    import service_changes.run as run
    (tmp_path / "employee_phones.csv").write_text("phone,slack_id,name\n(907) 555-7777,UMIKE,Mike Driver\n")
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: ParsedMessage(
        category=Category.item_change, account_number="1205-1-00004", summary="towels",
        changes=[Change(action=Action.set, item="shop towels", quantity=400)]))
    monkeypatch.setattr(run, "fill_item_matches", lambda c, p, a: None)
    p = make(phone.Turn(say="", action="change", summary="Midas wants shop towels to 400"),
             channels=("service-desk", "employee-calls", "route-12"))
    p.slack.chat_getPermalink = lambda channel, message_ts: {"permalink": "https://slack/x"}
    post = p.slack.chat_postMessage
    p.slack.chat_postMessage = lambda **kw: post(**kw) or {"ts": "9.9"}
    xml = call(p, "+19075557777", "+18885197604", "midas wants shop towels to four hundred")
    assert "Hi Mike, it's Donna" in xml[0]
    assert "300 to 400" in xml[-1] and "Want me to send that in?" in xml[-1] and not p.slack.posts
    xml.append(p.heard("CA1", {"SpeechResult": "yes"}))
    assert "Sent." in xml[-1]
    note, t = p.slack.posts
    assert note["channel"] == "Croute-12" and "<@UMIKE> called this in" in note["text"]
    assert t["channel"] == "Cservice-desk" and t["meta"]["kind"] == "ticket" and t["meta"]["driver_id"] == "UMIKE"
    assert t["meta"]["src_channel"] == "Croute-12" and "Mike Driver called it in" in t["text"]


def test_an_unknown_employee_number_cant_enter_changes(desk):
    make, leads = desk
    p = make(phone.Turn(say="I'll pass that on.", action="change", summary="Midas 50 more towels"))
    p.slack.users_list = lambda **kw: {"members": []}
    call(p, "+19075550000", "+18885197604", "midas wants 50 more towels")
    [m] = p.slack.posts
    assert m["meta"]["kind"] == "employee_call"

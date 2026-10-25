import json
import threading
from datetime import date
import urllib.request
from types import SimpleNamespace

import pytest

from . import sales, salesforce, webchat


class Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


def text(t):
    return Block(type="text", text=t)


def tool(tool_name, **inp):
    return Block(type="tool_use", id=f"tu_{tool_name}", name=tool_name, input=inp)


class FakeClaude:
    """Plays back scripted answers and records each call's system prompt and messages."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=self.answers.pop(0))


@pytest.fixture
def posted():
    return []


def make(posted, *answers):
    claude = FakeClaude(*answers)
    return webchat.Chat(claude, lambda kind, item: posted.append((kind, item)), "/tmp"), claude


def test_a_new_business_lead_goes_to_sales(posted):
    chat, claude = make(posted,
                        [text("Congratulations! Restaurants your size often start around 120 bar mops. Who should Bill call?")],
                        [tool("save_lead", name="Ana Lee", business="Moose Pizza", town="Wasilla", phone="907-555-0101",
                              needs="bar mops, aprons, 3 mats")],
                        [text("Thanks Ana! Bill Dubinsky will reach out.")])
    r = chat.message("sw", "", "We're opening a 40-seat pizza place in Wasilla", ip="1.1.1.1")
    assert r["site"] == "sw" and "120 bar mops" in r["reply"] and r["events"] == []
    assert "Snow White Linen Supply" in claude.calls[0]["system"] and "FACTS" in claude.calls[0]["system"]
    r = chat.message("sw", r["session"], "Ana Lee, 907-555-0101", ip="1.1.1.1")
    assert r["events"] == [{"type": "lead"}] and "Bill Dubinsky" in r["reply"]
    [(kind, lead)] = posted
    assert kind == "lead" and lead["company"] == "sw" and lead["phone"] == "907-555-0101"
    assert "Visitor: We're opening a 40-seat pizza place" in lead["transcript"]


def test_wrong_company_hands_over_to_the_sister_company(posted):
    chat, claude = make(posted,
                        [text("That's our sister company's specialty."), tool("hand_off", summary="60-room hotel in Anchorage, needs sheets and towels")],
                        [text("Connecting you now.")],
                        [text("Hi, this is The Laundry Group, Snow White's sister company. How many rooms...")])
    r = chat.message("sw", "", "We run a 60-room hotel and need sheets and towels")
    assert r["site"] == "tlg"
    assert [e["type"] for e in r["events"]] == ["handoff", "continued"]
    assert "The Laundry Group" in r["events"][1]["reply"]
    assert "The Laundry Group" in claude.calls[2]["system"] and "60-room hotel" in claude.calls[2]["messages"][0]["content"]
    assert chat.sessions[r["session"]]["from"] == "sw"


def test_an_existing_customer_request_goes_to_the_office(posted):
    chat, _ = make(posted, [tool("service_request", name="Jo", business="Kobuk Coffee", phone="907-555-0102",
                                 request="2 more 3x5 mats starting next week")],
                   [text("Got it, the office will confirm.")])
    r = chat.message("sw", "", "We're a customer, need 2 more mats. Jo at Kobuk, 907-555-0102")
    assert r["events"] == [{"type": "request"}]
    assert posted[0][0] == "request" and posted[0][1]["request"] == "2 more 3x5 mats starting next week"


def test_limits(posted, monkeypatch):
    monkeypatch.setattr(webchat, "PER_IP_HOUR", 2)
    chat, _ = make(posted, [text("a")], [text("b")])
    chat.message("sw", "", "hi", ip="9.9.9.9")
    chat.message("sw", "", "hi", ip="9.9.9.9")
    assert "call us" in chat.message("sw", "", "hi", ip="9.9.9.9")["reply"]
    with pytest.raises(ValueError):
        chat.message("acme", "", "hi")


def test_http_widget_and_chat(posted):
    chat, _ = make(posted, [text("Hello from Snow White")])
    srv = webchat.ThreadingHTTPServer(("127.0.0.1", 0), webchat.make_handler(chat, "https://chat.example"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = urllib.request.urlopen(urllib.request.Request(base + "/widget.js?site=tlg", headers={"Origin": "https://www.thelaundrygroup.com"}))
        js = r.read().decode()
        assert r.headers["Access-Control-Allow-Origin"] == "https://www.thelaundrygroup.com"
        assert '"site": "tlg"' in js and "https://chat.example/logo/tlg.png" in js and "__CONFIG__" not in js
        r = urllib.request.urlopen(urllib.request.Request(base + "/widget.js", headers={"Origin": "https://evil.example"}))
        assert r.headers["Access-Control-Allow-Origin"] is None
        req = urllib.request.Request(base + "/api/chat", data=json.dumps({"site": "sw", "message": "hi"}).encode(),
                                     headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req).read())
        assert out["reply"] == "Hello from Snow White" and out["session"]
    finally:
        srv.shutdown()


def test_salesforce_records_for_a_lead():
    lead = {"name": "Ana Marie Lee", "business": "Moose Pizza", "business_type": "restaurant", "town": "Wasilla",
            "phone": "907-555-0101", "company": "sw", "handed_from": "tlg", "current_provider": "alsco now",
            "needs": "bar mops and 3 mats", "transcript": "Visitor: hi",
            "found": {"street": "123 Main St", "city": "Wasilla", "state": "AK", "zip": "99654", "phone": "907-555-0199"}}
    a = salesforce.account_body(lead, "005X")
    assert a["Name"] == "Moose Pizza" and a["Type"] == "Potential Customer" and a["OwnerId"] == "005X"
    assert a["BillingStreet"] == "123 Main St" and a["BillingPostalCode"] == "99654" and a["Phone"] == "907-555-0199"
    assert a["Industry"] == "Restaurant" and a["Current_Vendor__c"] == "Alsco" and "Website" not in a
    assert a["Description"].startswith("[Snow White Linen Supply] Website chat lead\nCame in on the The Laundry Group site")
    assert "Products: Towels, Mats" in a["Description"]
    c = salesforce.contact_body(lead, "001A", "005X")
    assert c["FirstName"] == "Ana Marie" and c["LastName"] == "Lee" and c["Phone"] == "907-555-0101" and "Email" not in c
    o = salesforce.opportunity_body(lead, "001A", "005X", today=date(2026, 10, 25))
    assert o["StageName"] == "Prospecting" and o["CloseDate"] == "2026-11-24" and o["LeadSource"] == "Website"
    assert o["AccountId"] == "001A" and o["Type"] == "New Business"
    no_web = salesforce.account_body({"business": "Bob's Garage", "town": "Palmer", "phone": "907-1"}, "005X")
    assert no_web["BillingCity"] == "Palmer" and no_web["BillingState"] == "AK" and no_web["Phone"] == "907-1"
    assert no_web["Industry"] == "Auto"
    assert salesforce._q("Bob's") == "'Bob\\'s'"


def test_find_business_reads_the_json_and_drops_blanks():
    class Claude:
        class messages:
            @staticmethod
            def create(**kw):
                assert kw["tools"][0]["type"] == "web_search_20250305" and "Moose Pizza" in kw["messages"][0]["content"]
                text = 'Found it: {"street": "123 Main St", "city": "Wasilla", "state": "AK", "zip": "", "phone": null}'
                return type("R", (), {"content": [type("B", (), {"type": "text", "text": text})()]})()
    assert sales.find_business({"business": "Moose Pizza", "town": "Wasilla"}, Claude()) == \
        {"street": "123 Main St", "city": "Wasilla", "state": "AK"}
    assert sales.find_business({"name": "no business"}, Claude()) == {}


def test_give_to_jesse_creates_the_salesforce_opportunity(tmp_path, monkeypatch):
    monkeypatch.setattr(salesforce, "configured", lambda: True)
    calls = []

    class Slack:
        def chat_postMessage(self, **kw): calls.append(("post", kw)); return {"ts": "1.0"}
        def chat_update(self, **kw): calls.append(("update", kw))
        def chat_postEphemeral(self, **kw): calls.append(("eph", kw))

    lead = {"name": "Ana Lee", "business": "Moose Pizza", "phone": "907-555-0101", "company": "sw"}
    sales.post_lead(Slack(), "CSALES", str(tmp_path), lead)
    post = calls[0][1]
    buttons = post["blocks"][1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Give to Bill", "Give to Jesse", "Give to Scott"]
    jesse = next(b for b in buttons if b["action_id"] == "sales_give_jesse")
    payload = {"actions": [jesse], "channel": {"id": "CSALES"}, "message": {"ts": "1.0"}, "user": {"id": "U1", "name": "bill"}}
    made = []
    out = sales.handle_action(Slack(), payload, str(tmp_path), find=lambda l: {"street": "1 Main St"},
                              create=lambda l, o: (made.append((o, l["found"])), ("006A", "https://sf/006A"))[1])
    assert made == [("0051W000005qDzpQAE", {"street": "1 Main St"})] and "Jesse James Tingley" in out
    upd = calls[-1][1]
    footer = upd["blocks"][-1]["elements"][0]["text"]
    assert "Given to *Jesse James Tingley* by bill" in footer and "Salesforce Opportunity" in footer
    assert "address not found" not in footer
    assert sales.handle_action(Slack(), payload, str(tmp_path), create=lambda l, o: 1 / 0) == "already in Salesforce"

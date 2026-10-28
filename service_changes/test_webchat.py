import json
import threading
from datetime import date
import urllib.request
from types import SimpleNamespace

import pytest

from . import sales, salesforce, todo, webchat


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
    assert a["ShippingStreet"] == "123 Main St" and a["ShippingCity"] == "Wasilla"
    assert a["Industry"] == "Restaurant" and a["Current_Vendor__c"] == "Alsco" and "Website" not in a
    assert a["Description"].startswith("[Snow White Linen Supply] Website chat lead\nCame in on the The Laundry Group site")
    assert "Products: Towels, Mats" in a["Description"]
    c = salesforce.contact_body(lead, "001A", "005X")
    assert c["FirstName"] == "Ana Marie" and c["LastName"] == "Lee" and c["Phone"] == "907-555-0101" and "Email" not in c
    assert c["Primary_Contact__c"] is True and "LeadSource" not in c
    assert "Primary_Contact__c" not in salesforce.contact_body(lead, "001A", "005X", primary=False)
    o = salesforce.opportunity_body(lead, "001A", "005X", "003C", today=date(2026, 10, 25))
    assert o["StageName"] == "Prospecting" and o["CloseDate"] == "2026-11-24" and o["LeadSource"] == "Website"
    assert o["AccountId"] == "001A" and o["Type"] == "New Business" and o["opportunityContact__c"] == "003C"
    assert (o["Address_1__c"], o["City__c"], o["State__c"], o["Zip__c"], o["Phone__c"]) == \
        ("123 Main St", "Wasilla", "AK", "99654", "907-555-0199")
    assert "opportunityContact__c" not in salesforce.opportunity_body(lead, "001A", "005X")
    no_web = salesforce.account_body({"business": "Bob's Garage", "town": "Palmer", "phone": "907-1"}, "005X")
    assert no_web["BillingCity"] == "Palmer" and no_web["BillingState"] == "AK" and no_web["Phone"] == "907-1"
    assert no_web["Industry"] == "Auto"
    assert salesforce._q("Bob's") == "'Bob\\'s'"


def test_pick_account_prefers_phone_or_street_then_a_lone_name_match():
    a = {"Id": "001A", "Name": "Joe's Diner", "Phone": "(907)555-0101", "BillingStreet": "1 Main St"}
    b = {"Id": "001B", "Name": "Joe's Diner", "Phone": "907-555-0202", "ShippingStreet": "9 Elm St."}
    assert salesforce.pick_account([a, b], ["+1 907 555 0202"], "") == ("001B", "")
    assert salesforce.pick_account([a, b], [None], "9 ELM ST") == ("001B", "")
    assert salesforce.pick_account([a], ["907-000-0000"], "") == ("001A", "")
    acct, note = salesforce.pick_account([a, b], ["907-000-0000"], "5 Oak St")
    assert acct is None and note.startswith("2 accounts are named Joe's Diner")
    assert salesforce.pick_account([], ["907-555-0101"], "") == (None, "")
    acct, note = salesforce.pick_account([a, dict(b, Phone="907 555 0101")], ["9075550101"], "")
    assert acct == "001A" and note.startswith("2 accounts match")


def test_duplicate_rule_points_the_chat_at_the_existing_account(monkeypatch):
    dup = [{"errorCode": "DUPLICATES_DETECTED", "message": "Use one of these records?", "duplicateResult": {
        "matchResults": [{"matchRecords": [{"record": {"attributes": {"type": "Account"}, "Id": "001OLD"}}]}]}}]
    assert salesforce.duplicate_ids(dup) == ["001OLD"]
    assert salesforce.duplicate_ids([{"errorCode": "REQUIRED_FIELD_MISSING"}]) == []
    calls = []

    def fake_call(method, url, token, body=None):
        calls.append((method, url.split("/v62.0/")[-1][:40], body))
        if "query" in url:
            return {"records": []}
        if url.endswith("/sobjects/Account"):
            raise salesforce.SalesforceError("dup", dup)
        return {"id": "003NEW" if url.endswith("Contact") else "006NEW"}
    monkeypatch.setattr(salesforce, "_token", lambda: ("tok", "https://sf"))
    monkeypatch.setattr(salesforce, "_call", fake_call)
    opp, link, note = salesforce.create_opportunity({"business": "Joes Diner", "name": "Joe Smith", "phone": "907-1"}, "005X")
    assert opp == "006NEW" and "duplicate rule" in note
    contact = next(b for m, u, b in calls if u.endswith("sobjects/Contact"))
    assert contact["AccountId"] == "001OLD" and "Primary_Contact__c" not in contact
    assert next(b for m, u, b in calls if u.endswith("sobjects/Opportunity"))["AccountId"] == "001OLD"


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
    assert post["attachments"][0]["color"] == "#f9af10" and post["text"] == ""  # gold to-do bar
    buttons = post["attachments"][0]["blocks"][1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Give to Bill", "Give to Jesse", "Give to Scott"]
    jesse = next(b for b in buttons if b["action_id"] == "sales_give_jesse")
    payload = {"actions": [jesse], "channel": {"id": "CSALES"}, "message": {"ts": "1.0"}, "user": {"id": "U1", "name": "bill"}}
    made = []
    out = sales.handle_action(Slack(), payload, str(tmp_path), find=lambda l: {"street": "1 Main St"},
                              create=lambda l, o: (made.append((o, l["found"])), ("006A", "https://sf/006A", ""))[1])
    assert made == [("0051W000005qDzpQAE", {"street": "1 Main St"})] and "Jesse James Tingley" in out
    upd = calls[-1][1]
    assert upd["attachments"][0]["color"] == "#2eb67d"  # green once given
    footer = upd["attachments"][0]["blocks"][-1]["elements"][0]["text"]
    assert ":white_check_mark: Given to Jesse James Tingley · bill · " in footer and "Salesforce Opportunity" in footer
    assert "address not found" not in footer
    assert sales.handle_action(Slack(), payload, str(tmp_path), create=lambda l, o: 1 / 0) == "already in Salesforce"


def test_a_slack_failure_never_loses_the_lead(tmp_path):
    def broken(kind, item):
        raise KeyError("sales-desk")
    claude = FakeClaude([tool("save_lead", name="Scotty Pilate", business="Scottie's Bodies", town="Wasilla",
                              phone="555-555-5678", needs="mats, shop towels, uniforms for 12")],
                        [text("Thanks Scotty! Bill will reach out.")])
    chat = webchat.Chat(claude, broken, str(tmp_path))
    r = chat.message("sw", "", "Scotty Pilate", ip="1.1.1.1")
    assert r["events"] == [{"type": "lead"}] and "Bill will reach out" in r["reply"]
    [kept] = list((tmp_path / "web_unsent").iterdir())
    assert json.loads(kept.read_text())["phone"] == "555-555-5678"


def test_lead_goes_to_the_office_when_sales_desk_is_missing(tmp_path):
    posts = []

    class Slack:
        def conversations_list(self, **kw):
            return [{"channels": [{"name": "service-desk", "id": "CDESK", "is_member": True},
                                  {"name": "sales-desk", "id": "CSALES", "is_member": False}]}]

        def chat_postMessage(self, **kw):
            posts.append(kw)
            return {"ts": "1.0"}
    post = webchat.slack_poster(Slack(), str(tmp_path), "sales-desk", "service-desk")
    post("lead", {"name": "Scotty", "business": "Scottie's Bodies", "phone": "555", "company": "sw"})
    assert posts[0]["channel"] == "CDESK" and "isn't set up for the Service Desk bot" in todo.body(posts[0])


def test_existing_customers_are_pointed_to_the_text_line(monkeypatch):
    chat = webchat.Chat(FakeClaude(), lambda k, i: None, "/tmp")
    monkeypatch.delenv("TEXT_LINE", raising=False)
    assert "text (866)" not in chat.system("sw") and "email service@snowwhitelinen.com" in chat.system("sw")
    monkeypatch.setenv("TEXT_LINE", "(866) TAP-TEXT, (866) 827-8398")
    assert "text (866) TAP-TEXT, (866) 827-8398: it recognizes them by their phone number" in chat.system("tlg")
    assert "{next_time}" not in chat.system("sw") and "519-7604" not in chat.system("sw")


def test_website_request_gets_an_office_only_account_hint():
    from .context import Alliant, Customer, Wearer
    a = Alliant(customers=[Customer("1300-1-00000", "KOBUK COFFEE (WASILLA)", "4", ["Tue"]),
                           Customer("1300-1-00001", "KOBUK COFFEE (PALMER)", "4", ["Wed"]),
                           Customer("1205-1-00007", "MIDAS #7", "4", ["Tue"])],
                items={"1300-1-00000": {"TOWEL BAR MOP GOLD STRIPE": 80, "MAT 3X5": 2}},
                autocount={"1300-1-00000": {"TOWEL BAR MOP GOLD STRIPE": 40}},
                wearers={"1300-1-00000": [Wearer("3", "Jo", "Lee")]})
    hint = webchat.account_hint(a, "Kobuk Coffee", "Wasilla", "2 more bar towels and a shirt for Jo")
    assert hint[0].startswith("🔎 _Probably_ *1300-1-00000* KOBUK COFFEE (WASILLA)")
    assert "TOWEL BAR MOP GOLD STRIPE 40/delivery (inventory 80)" in hint[1] and "MAT" not in hint[1]
    assert "#3 Jo Lee" in hint[2]
    assert webchat.account_hint(a, "Kobuk Coffee", "", "mats")[0].startswith("🔎 _Possible accounts:_")
    assert "No account matched" in webchat.account_hint(a, "Moose Pizza", "Wasilla", "mats")[0]
    assert webchat.account_hint(None, "Kobuk", "", "") == []


def test_portal_login_help(monkeypatch):
    chat = webchat.Chat(FakeClaude(), lambda k, i: None, "/tmp")
    monkeypatch.delenv("PORTAL_URL", raising=False)
    assert "so the office can help" in chat.system("sw") and "{portal}" not in chat.system("sw")
    monkeypatch.setenv("PORTAL_URL", "https://portal.example/login")
    sys_ = chat.system("tlg")
    assert "Sign-in page: https://portal.example/login" in sys_ and '"Forgot password"' in sys_
    assert "Never ask for or accept their password" in sys_


def test_each_company_loads_from_its_own_address():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webchat.make_handler(None, "https://chat.snowwhitelinen.com"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    get = lambda path, host: urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{srv.server_port}{path}", headers={"Host": host})).read().decode()
    try:
        tlg = get("/try", "chat.thelaundrygroup.com")
        assert "The Laundry Group" in tlg and "https://chat.thelaundrygroup.com/widget.js?site=tlg" in tlg
        assert '"api": "https://chat.thelaundrygroup.com"' in get("/widget.js?site=tlg", "chat.thelaundrygroup.com")
        assert "https://chat.snowwhitelinen.com/widget.js?site=sw" in get("/try", "chat.snowwhitelinen.com")
        assert '"api": "https://chat.snowwhitelinen.com"' in get("/widget.js?site=sw", "evil.example")
    finally:
        srv.shutdown()


def test_catalog_is_ours_and_checked():
    from . import catalog
    cat = catalog.load()
    for site in ("sw", "tlg"):
        skus = catalog.skus(cat[site])
        assert len(skus) == len(set(skus)) and all(catalog.SKU.fullmatch(s) for s in skus)
        assert "price" not in json.dumps(cat[site]).lower()
        ids = {it["id"] for g in cat[site]["groups"] for it in g["items"]}
        assert cat[site]["industries"][-1].get("all")
        assert all(x in ids for i in cat[site]["industries"] for x in i.get("suggest", []))
    lines, err = catalog.lines(cat["sw"], [{"id": "mat-confetti-grey", "qty": 2, "option": "4x6", "how_often": "Weekly"},
                                           {"id": "cook-shirt-white", "qty": 8}])
    assert err is None and lines[0]["sku"] == "10-10-04" and lines[1]["wearers"] == 8
    assert catalog.line_text(lines[0]) == "Confetti Grey mat (4x6) [10-10-04]: 2 weekly"
    assert catalog.lines(cat["sw"], [{"id": "made-up", "qty": 1}])[1]
    assert catalog.lines(cat["sw"], [{"id": "mat-confetti-grey", "qty": 2, "option": "9x9", "how_often": "Weekly"}])[1]
    assert catalog.lines(cat["sw"], [{"id": "bar-mop", "qty": 0, "how_often": "Weekly"}])[1]
    assert catalog.pic_path("/tmp", "../etc/passwd") is None
    assert catalog.picture_api_path("https://x/servlet/servlet.ImageServer?id=01540000000S1Qb&oid=1") == "sobjects/Document/01540000000S1Qb/Body"
    assert catalog.picture_api_path("https://x/renditionDownload?versionId=0683r00000CoVIZ&x=1") == "sobjects/ContentVersion/0683r00000CoVIZ/VersionData"


def test_quote_request_goes_to_sales_with_items():
    posted = []
    chat = webchat.Chat(FakeClaude(), lambda kind, item: posted.append((kind, item)), "/tmp")
    base = {"name": "Pat Jones", "business": "Pat's Diner", "phone": "907-555-0101", "street": "123 Main St",
            "items": [{"id": "bar-mop", "qty": 40, "how_often": "Weekly"}], "other": "table linens"}
    assert chat.quote("sw", {**base, "phone": ""})["error"].startswith("Please enter a phone")
    assert "catalog" in chat.quote("sw", {**base, "items": [{"id": "x", "qty": 1}]})["error"]
    assert chat.quote("sw", {**base, "website": "spam"}) == {"ok": True} and posted == []
    assert chat.quote("sw", base) == {"ok": True}
    [(kind, lead)] = posted
    assert kind == "lead" and lead["source"] == "quote" and lead["items"][0]["sku"] == "5-01-01"
    card = sales.card_text({**lead, "id": "1"})
    assert "quote request" in card and "Bar mops [5-01-01]: 40 weekly" in card and "table linens" in card
    desc = salesforce.description(lead)
    assert "- Bar mops [5-01-01]: 40 weekly" in desc and "Chat:" not in desc
    assert salesforce.account_body(lead, "005")["BillingStreet"] == "123 Main St"
    assert "Current_Vendor__c" not in salesforce.account_body({**lead, "current_provider": "No linen company now"}, "005")
    assert salesforce.account_body({**lead, "current_provider": "Yes, has a linen company now"}, "005")["Current_Vendor__c"] == "Other competitor"
    assert chat.quote("tlg", {**base, "items": [], "other": "", "interests": ["Laundry soap and chemicals"]}) == {"ok": True}
    assert "Interested in: Laundry soap and chemicals" in sales.card_text({**posted[-1][1], "id": "2"})


def test_quote_page_served_for_each_company():
    srv = webchat.ThreadingHTTPServer(("127.0.0.1", 0), webchat.make_handler(
        webchat.Chat(FakeClaude(), lambda *a: None, "/tmp"), "https://chat.snowwhitelinen.com"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    get = lambda path, host: urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{srv.server_port}{path}", headers={"Host": host})).read().decode()
    try:
        sw, tlg = get("/quote", "chat.snowwhitelinen.com"), get("/quote", "chat.thelaundrygroup.com")
        assert "Confetti Grey" in sw and "Patient gowns" not in sw
        assert "Patient gowns" in tlg and '"site": "tlg"' in tlg and "https://chat.thelaundrygroup.com/logo/tlg.png" in tlg
    finally:
        srv.shutdown()


def test_quote_items_become_opportunity_products():
    items = [{"sku": "5-01-01", "name": "Bar mops", "option": "", "qty": 150, "how_often": "Weekly"},
             {"sku": "2-5020WH", "name": "Cook shirts, white", "option": "", "wearers": 8},
             {"sku": "", "name": "Custom logo mat", "option": "", "qty": 2, "how_often": "Weekly"}]
    entries = {"5-01-01": {"Id": "01uA", "UnitPrice": 0.33}, "2-5020WH": {"Id": "01uB", "UnitPrice": 0.35}}
    lines, missed = salesforce.item_lines(items, entries)
    assert lines[0] == {"PricebookEntryId": "01uA", "Quantity": 150, "UnitPrice": 0.33,
                        "Description": "Website quote: per week", "SortOrder": 1}
    assert lines[1]["Quantity"] == 88 and "8 employees x 11" in lines[1]["Description"]
    assert missed == ["Custom logo mat"]


def test_missing_pictures_borrow_a_close_match(tmp_path):
    from . import catalog
    d = str(tmp_path)
    for sku, data in (("1-02-02", b"\xff\xd8\xff white"), ("6-01-05", b"\xff\xd8\xff screen")):
        (tmp_path / "catalog_pics").mkdir(exist_ok=True)
        (tmp_path / "catalog_pics" / (sku + ".img")).write_bytes(data)
    got = catalog.borrow_pictures(d, catalog.load(), ["1-02-06", "6-01-55", "9-99-99"])
    assert got == {"1-02-06": "1-02-02", "6-01-55": "6-01-05"}
    assert open(catalog.pic_path(d, "6-01-55"), "rb").read().endswith(b"screen")


def test_drivers_field_guide_needs_its_link(monkeypatch):
    from .webchat import make_handler
    monkeypatch.setenv("DRIVER_GUIDE_KEY", "route-guide-2026x")
    H = make_handler(None, "https://chat.example.com")
    sent = []

    def get(path):
        h = H.__new__(H)
        h.path, h.headers = path, {}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b, ctype))
        h.send_response = lambda code: sent.append((code, b"", ""))
        h.send_header = h.end_headers = lambda *a: None
        h.do_GET()
        return sent[-1]
    assert get("/drivers/wrong-guess-12345")[0] == 404
    assert get("/drivers/route-guide-2026x")[0] == 301
    code, body, ctype = get("/drivers/route-guide-2026x/")
    assert code == 200 and b"Donna Field Guide" in body and b"(888) 519-7604" in body and b"noindex" in body
    assert get("/drivers/route-guide-2026x/guide.pdf")[2] == "application/pdf"
    assert get("/drivers/route-guide-2026x/donna.png")[2] == "image/png"

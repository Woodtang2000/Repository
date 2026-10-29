import pytest

from . import sms
from .test_desk import flat
from .context import Alliant, Customer, Wearer
from .parser import Answer
from .schema import Action, Category, Change, ParsedMessage

ALLIANT = Alliant(
    customers=[Customer("M4", "MIDAS #4 (FAIRBANKS)", "12", ["Mon"]), Customer("M8", "MIDAS #8 (PALMER)", "7", ["Tue"]),
               Customer("C1", "COSTCO 1342 DELI", "12", ["Wed"])],
    items={"M4": {"TOWEL SHOP RED": 300}, "M8": {"TOWEL SHOP RED": 100}, "C1": {"TOWEL BAR MOP": 800}},
    autocount={"M4": {"TOWEL SHOP RED": 300}, "C1": {"TOWEL BAR MOP": 400}},
    wearers={"M4": [Wearer("7", "Tim", "Smith")]},
    garments={("M4", "7"): {"SHIRT XL": 11}, ("C1", "9"): {"COAT M": 5}},
    cards={"M4": {"special_instructions": "Slow payer, call Bob", "phone": "(907)555-0100"}, "C1": {}},
)


class FakeSlack:
    def __init__(self):
        self.posts = []

    def conversations_list(self, **kw):
        return [{"channels": [{"id": "D1", "name": "service-desk-test"}]}]

    def chat_postMessage(self, channel, text, metadata=None, attachments=None, **kw):
        m = {"channel": channel, "ts": f"{len(self.posts) + 1}.0", "text": flat(text, attachments), "user": "UBOT",
             "bot_id": "B1", "metadata": metadata, "meta": (metadata or {}).get("event_payload", {}),
             "attachments": attachments}
        self.posts.append(m)
        return {"ts": m["ts"]}

    def say(self, text, user="U2"):  # a person typing in the office channel
        self.posts.append({"channel": "D1", "ts": f"{len(self.posts) + 1}.0", "text": text, "user": user, "meta": {}})

    def react(self, ts, user="U2"):
        m = next(p for p in self.posts if p["ts"] == ts)
        m.setdefault("reactions", []).append({"name": "white_check_mark", "users": [user]})

    def conversations_history(self, channel, **kw):
        return {"messages": list(reversed([p for p in self.posts if p["channel"] == channel]))}

    def chat_update(self, channel, ts, text, metadata=None, attachments=None):
        m = next(p for p in self.posts if p["ts"] == ts)
        m.update(text=flat(text, attachments), metadata=metadata, meta=metadata["event_payload"], attachments=attachments)

    def users_info(self, user):
        return {"user": {"real_name": "Sonja Burke"}}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    (tmp_path / "customer_phones.csv").write_text("phone,accounts,name\n(907) 555-1234,M4,Jane at Midas\n"
                                                  "+1 907 555 9999,M4;M8,Midas owner\n")
    texts, seen = [], {}

    def fake_parse(client, text, channel, ts, alliant, author="", office=False, previous=""):
        seen["alliant"], seen["text"] = alliant, text
        last = text.splitlines()[-1].lower()
        if "how many" in last:
            return ParsedMessage(category=Category.lookup, account_number="C1" if "costco" in last else "M4",
                                 summary=last)
        if "thanks" in last:
            return ParsedMessage(category=Category.not_a_request, summary="thanks")
        qty = None if "more towels" in text and "200" not in text else 200
        p = ParsedMessage(category=Category.item_change, account_number="C1" if "costco" in last else "M4",
                          summary="towels", changes=[Change(action=Action.add, item="shop towels", quantity=qty)])
        if qty is None:
            p.questions_for_driver = ["How many more shop towels?"]
        return p

    import service_changes.parser as parser
    import service_changes.run as run
    monkeypatch.setattr(parser, "parse_message", fake_parse)
    monkeypatch.setattr(parser, "answer_lookup",
                        lambda c, q, facts, asker="": (seen.__setitem__("facts", facts), Answer(answer="300 per delivery", found=True))[1])
    monkeypatch.setattr(run, "fill_item_matches", lambda c, p, a, **kw: None)
    slack = FakeSlack()
    td = sms.TextDesk(slack, None, ALLIANT, "service-desk-test", str(tmp_path), send=lambda to, body: texts.append((to, body)))
    return td, slack, texts, seen


def test_only_keeps_nothing_but_the_customers_accounts():
    mine = sms.only(ALLIANT, ["M4"])
    assert [c.account for c in mine.customers] == ["M4"]
    assert set(mine.items) == {"M4"} and set(mine.cards) == {"M4"} and list(mine.garments) == [("M4", "7")]
    import dataclasses
    assert all(f.name in ("as_of", "exported_at") or not getattr(mine, f.name) or f.name in ("customers",) or set(
        k if isinstance(k, str) else k[0] for k in getattr(mine, f.name)) <= {"M4"} for f in dataclasses.fields(mine))


def test_customer_facts_leave_out_inventory_notes_and_contacts():
    facts = sms.customer_facts(ALLIANT, "M4")
    assert "TOWEL SHOP RED: 300 per delivery" in facts and "Tim Smith: SHIRT XL x11" in facts
    assert "Slow payer" not in facts and "555-0100" not in facts and "inventory" not in facts.lower()


def test_claude_only_ever_sees_the_numbers_own_account(desk):
    td, slack, texts, seen = desk
    td.handle("+19075551234", "how many bar mops does costco get", "S1")
    assert [c.account for c in seen["alliant"].customers] == ["M4"]  # Costco was never in front of Claude
    assert "COSTCO" not in seen["facts"] and seen["facts"].startswith("M4 MIDAS #4")  # answered from M4 only


def test_a_change_for_another_account_lands_on_their_own(desk):
    td, slack, texts, seen = desk
    td.handle("+19075551234", "add 200 bar mops to costco", "S1")
    [t] = [p for p in slack.posts if p["meta"]["kind"] == "ticket"]
    assert t["meta"]["account"] == "M4" and t["meta"]["sms_to"] == "+19075551234" and "COSTCO" not in t["text"]


def test_unclear_then_answer_then_ticket(desk):
    td, slack, texts, seen = desk
    td.handle("+19075551234", "need more towels", "S1")
    assert texts[-1][1] == "Happy to! Just one quick question: How many more shop towels?" and not slack.posts
    td.handle("+19075551234", "200", "S2")
    assert "Customer: need more towels" in seen["text"] and "Customer: 200" in seen["text"]
    [t] = slack.posts
    assert t["meta"]["kind"] == "ticket" and "📱 Customer text from Jane at Midas (…1234)" in t["text"]
    assert texts[-1][1].startswith("Consider it done")


def test_two_locations_asks_which(desk):
    td, slack, texts, seen = desk

    def no_acct(*a, **k):
        return ParsedMessage(category=Category.item_change, summary="towels",
                             changes=[Change(action=Action.add, item="shop towels", quantity=200)])
    import service_changes.parser as parser
    parser.parse_message, old = no_acct, parser.parse_message
    try:
        td.handle("+19075559999", "add 200 shop towels", "S1")
    finally:
        parser.parse_message = old
    assert "Which location" in texts[-1][1] and "MIDAS #4" in texts[-1][1] and "MIDAS #8" in texts[-1][1]
    assert "COSTCO" not in texts[-1][1]


def test_new_number_signs_up_and_the_office_approves(desk, monkeypatch):
    import service_changes.parser as parser
    from .parser import Identified
    td, slack, texts, seen = desk
    monkeypatch.setattr(parser, "identify_customer", lambda c, text, customers: Identified(
        person_name="Bob", accounts=["M8", "M4"] if "palmer" in text.lower() else []))
    td.handle("+15551110000", "add 200 more towels", "S1")  # held: nothing about any account goes out
    assert texts == [("+15551110000", sms.who_are_you("add 200 more towels"))] and "alliant" not in seen
    assert texts[0][1].startswith("Absolutely! This is Donna. Who do I have the pleasure of working with today?")
    assert sms.who_are_you("hello").startswith("Hi there! ") and "Reply STOP to opt out" in texts[0][1]
    td.handle("+15551110000", "Bob, Midas in Palmer", "S2")
    [card] = [p for p in slack.posts if p["meta"].get("kind") == "sms_signup"]
    assert "Best match: *M8* MIDAS #8 (PALMER)" in card["text"] and "Could also be: M4" in card["text"]
    assert texts[-1][1] == sms.WAITING
    td.handle("+15551110000", "hello?", "S3")  # still waiting: held, no reply
    assert len(texts) == 2

    slack.react(card["ts"])
    assert td.office_pass() == 1
    assert "+15551110000,M8,Bob" in open(td.data_dir + "/customer_phones.csv").read()
    assert texts[2][0] == "+15551110000" and texts[2][1].startswith(sms.SET_UP.format(names="MIDAS #8 (PALMER)"))
    assert len(texts) == 3 and texts[2][1].count("Consider it done") == 1  # welcome and answer in one text
    assert [c.account for c in seen["alliant"].customers] == ["M8"]  # the held request ran, for M8 only
    assert card["meta"]["kind"] == "sms_approved" and td.office_pass() == 0


def test_office_commands_in_the_channel(desk, monkeypatch):
    td, slack, texts, seen = desk
    slack.say("add (907) 555-7777 to M4")
    slack.say("remove 907-555-1234")
    assert td.office_pass() == 2
    phones = sms.load_phones(td.data_dir + "/customer_phones.csv")
    assert phones["9075557777"]["accounts"] == ["M4"] and "9075551234" not in phones
    assert texts[-1] == ("+19075557777", sms.SET_UP.format(names="MIDAS #4 (FAIRBANKS)"))
    assert td.office_pass() == 0  # each command once


def test_phone_commands_are_recognised():
    assert sms.is_phone_command("add 907-555-1234 to Midas Fairbanks")
    assert sms.is_phone_command("Add +1 (907) 555 1234 for 1205-1-00004")
    assert sms.is_phone_command("remove 9075551234")
    assert not sms.is_phone_command("add 200 shop towels to Midas")


def test_plain_text_for_phones():
    assert sms.plain("<@U1> ✅ *1205-1-00004* _MIDAS_ – `5-01` <https://x|#route-1>") == "✅ 1205-1-00004 MIDAS – 5-01 #route-1"


def test_two_numbers_reply_from_the_one_texted(tmp_path, monkeypatch):
    from . import sms
    monkeypatch.setenv("TWILIO_NUMBER", "(866) 827-8398, 18885197604")  # written any which way
    monkeypatch.setattr(sms, "LAST_NUMBER", str(tmp_path / "last.json"))
    calls = []

    def twilio(method, path, params=None):
        calls.append((method, params))
        if method == "GET" and params["To"] == "+18885197604":
            return {"messages": [{"direction": "inbound", "from": "+19075550101", "to": "+18885197604",
                                  "date_created": "x", "sid": "SM1", "body": "hi"}]}
        return {"messages": []}
    monkeypatch.setattr(sms, "_twilio", twilio)
    from datetime import datetime
    assert [m["sid"] for m in sms.inbound(datetime(2026, 10, 25))] == ["SM1"]
    assert {p["To"] for m, p in calls if m == "GET"} == {"+18668278398", "+18885197604"}
    sms.send_sms("+19075550101", "done")
    sms.send_sms("+19075550199", "hello")
    sends = [p for m, p in calls if m == "POST"]
    assert sends[0]["From"] == "+18885197604" and sends[1]["From"] == "+18668278398"


def test_messages_sign_as_the_customers_company():
    from .context import company_for
    assert company_for("1205-1-00007") == "Snow White Linen"
    assert company_for("3100-2-00001") == "The Laundry Group"
    assert company_for(None) == "Snow White Linen"


def test_demo_mode_for_sales_calls(desk, monkeypatch):
    from . import demo
    td, slack, texts, seen = desk
    monkeypatch.setenv("DEMO_FROM", "(907) 555-4242")
    monkeypatch.setattr(demo, "ENTER_SECONDS", 0)
    td.handle("+19075550000", "demo restaurant", "S0")  # not a demo number: a stranger signing up, no demo
    assert "demo" not in td.state or "9075550000" not in td.state["demo"]
    td.handle("+19075554242", "Demo restaurant", "S1")
    assert texts[-1][1].startswith("🎬 Demo on: you're Maria at Northern Lights Bistro")
    td.handle("+19075554242", "how many bar mops costco", "S2")  # (fake parser: "how many" is a lookup)
    assert [c.account for c in seen["alliant"].customers] == ["9901-1-00001"]  # only the made-up business
    td.handle("+19075554242", "add 200 bar mops", "S3")
    assert texts[-1][1].startswith("Consider it done") and not slack.posts  # nothing to the office
    td.demo_pass()
    assert texts[-1][1].startswith("All done!") and "Ana entered it for you" in texts[-1][1]

    import service_changes.parser as parser
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("API down")))
    td.handle("+19075554242", "What does Danny get for uniforms?", "S4")
    assert texts[-1][1] == "Danny Kim gets 11 black cook shirts, size L."  # the scripted answer, no fumble
    td.handle("+19075554242", "demo off", "S5")
    assert td.state["demo"] == {} and not slack.posts


def test_demo_businesses_and_cheat_sheet():
    from . import demo
    b = demo.load()
    assert demo.pick("tire shop", b) == "auto" and demo.pick("a hotel", b) == "hotel" and demo.pick("xyz", b) is None
    assert all(v["account"].startswith("99") and v["script"] for v in b.values())
    assert "demo restaurant" in demo.cheat_sheet()


def test_slack_phone_links_in_office_commands(desk):
    td, slack, texts, seen = desk
    slack.say("add <tel:9075557777|(907) 555-7777> to M4")
    slack.say("add <tel:9075558888|(907) >555-8888 to M4")  # Slack linked only part of it
    assert td.office_pass() == 2
    phones = sms.load_phones(td.data_dir + "/customer_phones.csv")
    assert phones["9075557777"]["accounts"] == ["M4"] and phones["9075558888"]["accounts"] == ["M4"]


def test_donnas_workstation_question_and_the_customers_answer(desk, tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from .desk import Desk
    td, slack, texts, seen = desk
    sent = []
    monkeypatch.setattr(sms, "send_sms", lambda to, body: sent.append((to, body)))
    d = SimpleNamespace(data_dir=td.data_dir, routes={}, post=None)
    t = {"ts": "9.1", "text": "*M4* MIDAS\n> Customer: change the shop towels to 60\n",
         "metadata": {"event_type": "service_desk",
                      "event_payload": {"kind": "ticket", "account": "1205-1-00004", "sms_to": "+19075551234"}}}
    assert Desk.ask_requester(d, t, "60 in total, or 60 more?") == "customer"
    assert sent == [("+19075551234", "Snow White Linen: Happy to! Just one quick question on your request: 60 in total, or 60 more?")]
    td.take_asks()
    convo = td.state["convos"]["9075551234"]
    assert convo["lines"] == ["Customer: change the shop towels to 60", "Snow White asked: 60 in total, or 60 more?"]
    td.handle("+19075551234", "60 total", "S9")  # read together with the request
    assert "Customer: change the shop towels to 60" in seen["text"] and "Customer: 60 total" in seen["text"]


def test_a_phone_caller_is_linked_with_a_button_and_their_request_runs(desk):
    """An unknown caller's card: Add / Move buttons; once tapped (desk.link_contact marks the card) the number is on
    the account and what they asked for on the call becomes the usual ticket, answered by text."""
    td, slack, texts, seen = desk
    sms.signup_card(slack, "D1", td.alliant, "+19075551234", "Jane", "add 200 towels", ["M8"], request="add 200 towels",
                    current=["M4"], head="📞 *Phone request* from (907) 555-1234, a number not on file")
    card = slack.posts[-1]
    els = card["attachments"][0]["blocks"][-1]["elements"]
    assert [e["text"]["text"] for e in els] == ["Move to Midas #8 (Palmer)", "Add Midas #8 (Palmer) too", "Block"]
    assert "This number is now on: M4 MIDAS #4 (FAIRBANKS)" in card["text"]
    card["meta"].update(approved_by="U2", move=True)  # "Move to Midas #8" tapped
    assert td.office_pass() == 1
    phones = sms.load_phones(td.data_dir + "/customer_phones.csv")
    assert phones["9075551234"]["accounts"] == ["M8"]  # off Midas #4: they changed jobs
    assert [c.account for c in seen["alliant"].customers] == ["M8"] and "add 200 towels" in seen["text"]
    assert any(p["meta"].get("kind") == "ticket" and p["meta"].get("sms_to") == "+19075551234" for p in slack.posts)
    assert "Moved to M8" in card["text"]


def test_an_owner_with_several_accounts_adds_one_more(desk):
    td, slack, texts, seen = desk
    sms.signup_card(slack, "D1", td.alliant, "+19075559999", "", "", ["C1"], current=["M4", "M8"])
    card = slack.posts[-1]
    card["meta"].update(approved_by="U2", move=False, accounts=["C1"])
    td.office_pass()
    assert sms.load_phones(td.data_dir + "/customer_phones.csv")["9075559999"]["accounts"] == ["M4", "M8", "C1"]


def test_a_blocked_number_gets_nothing_and_unblock_undoes_it(desk):
    td, slack, texts, seen = desk
    sms.block(td.data_dir, "+19075551234", by="Sonja Burke", why="competitor asking about Midas")
    assert "9075551234" not in sms.load_phones(td.data_dir + "/customer_phones.csv")  # off every account
    td.handle("+19075551234", "how many towels does Midas get?", "S9")
    assert not texts and "alliant" not in seen  # no reply, no account looked at
    slack.say("unblock 907-555-1234")
    td.office_pass()
    assert "9075551234" not in sms.blocked(td.data_dir) and "Unblocked" in slack.posts[-1]["text"]


def test_a_number_asking_about_different_customers_is_flagged(desk):
    td, slack, texts, seen = desk
    td.card("+19075550000", "Bob", "Midas in Fairbanks", ["M4"])
    assert "⚠️" not in slack.posts[-1]["text"]
    td.card("+19075550000", "Bob", "Midas again", ["M4"])  # the same account again: fine
    assert "⚠️" not in slack.posts[-1]["text"]
    from .sms import other_accounts  # locations of one customer share the account's first part: not flagged
    assert other_accounts(td.data_dir, "+19075550001", ["1205-1-00008"], ["1205-1-00004"]) == []
    td.card("+19075550000", "Bob", "Costco deli", ["C1"])  # now a different customer
    warned = [p for p in slack.posts if "⚠️" in p["text"]]
    assert any(p["channel"] == "D1" and "has also asked about or is on: M4" in p["text"] for p in warned)
    assert any(p["channel"] == "U4KFSQCDQ" for p in warned)  # Scott hears about it directly


def test_caller_id_name_is_a_hint_with_warnings():
    lines = sms.caller_id_lines({"name": "CHILIS GRILL", "who": "business", "kind": "landline", "carrier": "GCI"}, "CHILIS")
    assert lines == ["Caller ID: CHILIS GRILL, landline, GCI"]
    assert "another linen or uniform company" in sms.caller_id_lines({"name": "ALASKA LINEN SERVICE", "kind": "landline"}, "CHILIS")[-1]
    assert "throwaway" in sms.caller_id_lines({"name": "", "kind": "nonFixedVoip"}, "CHILIS")[-1]
    assert "doesn't match Chilis" in sms.caller_id_lines({"name": "MOOSE CAFE", "who": "business", "kind": "landline"}, "CHILIS")[-1]
    assert sms.caller_id_lines({}, "CHILIS") == []
    assert not any("another linen" in l for l in sms.caller_id_lines({"name": "SNOW WHITE LINEN", "kind": "mobile"}, ""))


def test_caller_id_lookup_is_cached(tmp_path, monkeypatch):
    import io
    import json as j
    import urllib.request
    calls = []
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: calls.append(req.full_url) or io.BytesIO(j.dumps(
        {"caller_name": {"caller_name": "CHILIS GRILL", "caller_type": "BUSINESS"},
         "line_type_intelligence": {"type": "landline", "carrier_name": "GCI"}}).encode()))
    assert sms.caller_id(str(tmp_path), "+19072292058")["name"] == "CHILIS GRILL"
    assert sms.caller_id(str(tmp_path), "(907) 229-2058")["kind"] == "landline" and len(calls) == 1  # once, then cached
    assert "Fields=caller_name,line_type_intelligence" in calls[0]


def test_scott_texts_changes_after_his_code_phrase(desk, monkeypatch):
    td, slack, texts, seen = desk
    monkeypatch.setenv("OWNER_PHONES", "907-230-6513")
    monkeypatch.setenv("OWNER_PHONE_SECRET", "bar mops in the snow")
    asked = []
    monkeypatch.setattr(sms.TextDesk, "ask_desk", lambda self, text, key: asked.append((text, key)) or "Homer Golf Course: "
                        "20 bib aprons added. Want me to put that in?")
    me = "+19072306513"
    td.handle(me, "add 20 bib aprons to homer golf course", "S1")
    assert texts[-1][1].startswith("Donna: hi Scott! Text me your code phrase") and not asked  # held, nothing done yet
    td.handle(me, "wrong words", "S2")
    assert "not it" in texts[-1][1] and not asked
    td.handle(me, "Bar mops in the snow.", "S3")
    assert asked == [("add 20 bib aprons to homer golf course", "9072306513")]  # the held request, right after
    assert texts[-1] == (me, "Donna: Homer Golf Course: 20 bib aprons added. Want me to put that in?")
    td.handle(me, "yes", "S4")
    assert asked[-1] == ("yes", "9072306513")  # verified now: straight to the desk
    assert "pending" not in td.state or "9072306513" not in td.state["pending"]  # never signed up as a customer


def test_three_wrong_phrases_lock_scotts_number(desk, monkeypatch):
    td, slack, texts, seen = desk
    monkeypatch.setenv("OWNER_PHONES", "9072306513")
    monkeypatch.setenv("OWNER_PHONE_SECRET", "bar mops in the snow")
    monkeypatch.setattr(sms.TextDesk, "ask_desk", lambda self, text, key: (_ for _ in ()).throw(AssertionError("no")))
    posted = []
    slack.chat_postMessage = lambda channel, text, **kw: posted.append((channel, text))
    me = "+19072306513"
    for i, said in enumerate(["what does chili's owe", "guess one", "guess two", "guess three", "bar mops in the snow"]):
        td.handle(me, said, f"S{i}")
    assert posted and posted[0][0] == "U4KFSQCDQ" and "3 times" in posted[0][1]
    assert len(texts) == 3  # asked for the phrase, then two "not it"; nothing after the lock, not even the right phrase

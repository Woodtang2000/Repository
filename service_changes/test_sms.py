import pytest

from . import sms
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

    def chat_postMessage(self, channel, text, metadata=None, **kw):
        self.posts.append({"channel": channel, "text": text, "meta": metadata["event_payload"]})
        return {"ts": "1.0"}


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
    monkeypatch.setattr(run, "fill_item_matches", lambda c, p, a: None)
    slack = FakeSlack()
    td = sms.TextDesk(slack, None, ALLIANT, "service-desk-test", str(tmp_path), send=lambda to, body: texts.append((to, body)))
    return td, slack, texts, seen


def test_only_keeps_nothing_but_the_customers_accounts():
    mine = sms.only(ALLIANT, ["M4"])
    assert [c.account for c in mine.customers] == ["M4"]
    assert set(mine.items) == {"M4"} and set(mine.cards) == {"M4"} and list(mine.garments) == [("M4", "7")]


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
    assert texts[-1][1] == "Quick question: How many more shop towels?" and not slack.posts
    td.handle("+19075551234", "200", "S2")
    assert "Customer: need more towels" in seen["text"] and "Customer: 200" in seen["text"]
    [t] = slack.posts
    assert t["meta"]["kind"] == "ticket" and "📱 Customer text from Jane at Midas (…1234)" in t["text"]
    assert texts[-1][1].startswith("Got it")


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


def test_unknown_number_gets_one_polite_reply_and_the_office_is_told(desk):
    td, slack, texts, seen = desk
    td.handle("+15551110000", "add mats", "S1")
    td.handle("+15551110000", "hello?", "S2")
    assert texts == [("+15551110000", sms.NOT_SET_UP)] and "seen" not in seen
    assert [p["meta"]["kind"] for p in slack.posts] == ["sms_unknown"]


def test_plain_text_for_phones():
    assert sms.plain("<@U1> ✅ *1205-1-00004* _MIDAS_ – `5-01` <https://x|#route-1>") == "✅ 1205-1-00004 MIDAS – 5-01 #route-1"

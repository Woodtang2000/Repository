from types import SimpleNamespace

from . import notices
from .context import Alliant, Customer, Wearer


def setup(tmp_path, monkeypatch):
    from . import bird
    sent, posts = [], []
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: sent.append((to, subject, text)))
    data = Alliant(customers=[Customer("5045-1-00000", "HOMER GOLF COURSE", "5", ["Tue"])],
                   cards={"5045-1-00000": {"email": "scott@homergolf.com"}},
                   wearers={"5045-1-00000": [Wearer("7", "DAVE", "JONES")]},
                   items={"5045-1-00000": {"TOWEL BAR MOP": 50}},
                   orders={"5045-1-00000": [{"po": "P1", "line": "1", "cust_code": "5045-1-00000", "wearer": "7",
                                             "description": "SHIRT", "qty_ordered": "5", "qty_received": "2"}]})
    d = SimpleNamespace(alliant=data, data_dir=str(tmp_path), desk_id="CDESK",
                        post=lambda ch, text, payload, **kw: posts.append(text))
    return d, sent, posts


def test_garments_arrived_and_welcome(tmp_path, monkeypatch):
    d, sent, posts = setup(tmp_path, monkeypatch)
    n = notices.Notices(d)
    assert "baseline" in n.pass_() and not sent  # the first look sends nothing about the past
    d.alliant.orders["5045-1-00000"][0]["qty_received"] = "5"  # the rest came in
    d.alliant.customers.append(Customer("6000-1-00001", "MOOSE CAFE", "5", ["Wed"]))
    d.alliant.cards["6000-1-00001"] = {"email": "ann@moosecafe.com"}
    d.alliant.items["6000-1-00001"] = {"TOWEL BAR MOP": 40}
    assert n.pass_() == {"garments": 1, "welcome": 1}
    to, subject, text = sent[0]
    assert to == "scott@homergolf.com" and subject == "Snow White Linen: new garments on the way"
    assert "Dave Jones's new shirt has arrived" in text and "(Tuesday)" in text and "866-TAP-TEXT (866-827-8398)" in text
    to, subject, text = sent[1]
    assert to == "ann@moosecafe.com" and "Welcome to Snow White Linen!" in text and "40 Towel Bar Mop" in text
    assert "$" not in text and "Wednesday" in text
    assert len(posts) == 2 and "Donna emailed" in posts[0]
    assert n.pass_() == {"garments": 0, "welcome": 0} and len(sent) == 2  # once each
    monkeypatch.setenv("CUSTOMER_NOTICES", "off")
    assert n.pass_() == {}


def test_one_welcome_for_several_new_locations_without_billing_lines(tmp_path, monkeypatch):
    # 11/17: a lodge manager got 4 welcome emails in the same minute, one saying "You're set up for: 0 Nog Lbs Delivered"
    d, sent, posts = setup(tmp_path, monkeypatch)
    n = notices.Notices(d)
    n.pass_()
    for acct, name, day in (("7001-2-00000", "TALKEETNA LODGE", "Mon"), ("7002-2-00000", "DENALI LODGE", "Wed")):
        d.alliant.customers.append(Customer(acct, name, "4", [day, "Thu"]))
        d.alliant.cards[acct] = {"email": "gm@lodges.com"}
    d.alliant.customers.append(Customer("7003-1-00000", "PIZZA PLACE", "5", ["Fri"]))
    d.alliant.cards["7003-1-00000"] = {"email": "owner@pizza.com"}
    d.alliant.items["7003-1-00000"] = {"NOG LBS DELIVERED": 0, "FUEL SURCHARGE": 1, "TOWEL BAR MOP": 40}
    assert n.pass_()["welcome"] == 2
    lodge = next(t for to, s, t in sent if to == "gm@lodges.com")
    assert len([1 for to, s, t in sent if to == "gm@lodges.com"]) == 1  # one email, both lodges
    assert "your 2 locations" in lodge and "Talkeetna Lodge: deliveries on Monday and Thursday" in lodge and "Denali Lodge" in lodge
    pizza = next(t for to, s, t in sent if to == "owner@pizza.com")
    assert "40 Towel Bar Mop" in pizza and "Nog" not in pizza and "Surcharge" not in pizza


def test_a_held_welcome_waits_until_the_hold_comes_off(tmp_path, monkeypatch):
    d, sent, posts = setup(tmp_path, monkeypatch)
    n = notices.Notices(d)
    n.pass_()
    d.alliant.customers.append(Customer("7001-2-00000", "TALKEETNA LODGE", "4", ["Mon"]))
    d.alliant.cards["7001-2-00000"] = {"email": "gm@pursuit.com"}
    monkeypatch.setenv("WELCOME_HOLD", "pursuit.com")
    assert n.pass_()["welcome"] == 0 and not sent
    monkeypatch.setenv("WELCOME_HOLD", "")
    assert n.pass_()["welcome"] == 1 and sent[0][0] == "gm@pursuit.com"


def test_a_welcome_is_noted_under_the_deals_new_accounts_post(tmp_path, monkeypatch):
    # Sales, 11/18: every automatic customer email shows under the deal's #new-accounts post
    import json
    from . import new_accounts
    d, sent, posts = setup(tmp_path, monkeypatch)
    slack_posts = []
    d.slack = SimpleNamespace(chat_postMessage=lambda **kw: slack_posts.append(kw))
    na = new_accounts.NewAccounts(d)
    d.new_accounts = lambda: na
    (tmp_path / "new_accounts.json").write_text(json.dumps({"r1": {"thread": "111.222", "status": "sent",
                                                                    "result": {"customer_number": "6000-1-00001"}}}))
    n = notices.Notices(d)
    n.pass_()
    d.alliant.customers.append(Customer("6000-1-00001", "MOOSE CAFE", "5", ["Wed"]))
    d.alliant.cards["6000-1-00001"] = {"email": "ann@moosecafe.com"}
    assert n.pass_()["welcome"] == 1
    assert len(slack_posts) == 1 and slack_posts[0]["thread_ts"] == "111.222"
    assert slack_posts[0]["channel"] == new_accounts.CHANNEL
    assert slack_posts[0]["text"].startswith("📧 Welcome email sent ") and slack_posts[0]["text"].endswith("to ann@moosecafe.com")
    assert "$" not in slack_posts[0]["text"] and "http" not in slack_posts[0]["text"]
    assert na.note_welcome(["9999-1-00001"], ["x@y.com"]) == 0  # no deal post for it: nothing posted

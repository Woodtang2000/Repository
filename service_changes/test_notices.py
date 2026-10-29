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

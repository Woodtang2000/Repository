from datetime import date, datetime
from types import SimpleNamespace

from .context import Alliant, Customer
from . import notices, stops_watch


def make(tmp_path, items, autocount, **kw):
    a = Alliant(customers=[Customer("4000-1-00400", "SAFEWAY 1739AS (P)", "4", ["Tue"])], items=items, autocount=autocount,
                cards={"4000-1-00400": {"email": "store1739@safeway.com"}}, **kw)
    posts, mails = [], []
    d = SimpleNamespace(alliant=a, data_dir=str(tmp_path), desk_id="CDESK", channels={"route-4": "CR4"},
                        post=lambda ch, text, payload, **k: posts.append((ch, text)))
    return d, posts, mails


def run(d, monkeypatch, mails, day, hour=8):
    monkeypatch.setattr(notices.Notices, "_send", lambda self, acct, to, subject, body, kind, note: mails.append((to, subject, body)))
    w = stops_watch.StopsWatch(d, today=day)
    w.now = datetime(day.year, day.month, day.day, hour)
    return w.pass_()


def test_a_stopped_mat_gets_an_email_a_driver_and_an_office_reminder(tmp_path, monkeypatch):
    d, posts, mails = make(tmp_path, {"4000-1-00400": {"MAT CONFETTI GREY 3X10": 10, "TOWEL BAR MOP": 60}},
                           {"4000-1-00400": {"MAT CONFETTI GREY 3X10": 5, "TOWEL BAR MOP": 60}})  # still a customer
    assert "baseline" in run(d, monkeypatch, mails, date(2026, 10, 27))  # first look: history, nothing sent
    d.alliant.autocount["4000-1-00400"]["MAT CONFETTI GREY 3X10"] = 0  # stopped in Alliant, inventory left
    assert run(d, monkeypatch, mails, date(2026, 10, 29))["items"] == 1
    [(to, subject, body)] = mails
    assert to == ["store1739@safeway.com"] and subject == "items to send back"
    assert "- 10 Mat Confetti Grey 3x10 (stopped October 29)" in body and "Thursday, November 12 is billed as a loss" in body
    assert "we'd much rather have them back" in body and not posts
    run(d, monkeypatch, mails, date(2026, 11, 4))  # day 6, a Wednesday: nothing yet
    assert not posts and len(mails) == 1
    run(d, monkeypatch, mails, date(2026, 11, 10))  # day 12, a Tuesday (their delivery day): driver and office
    assert posts[0][0] == "CR4" and "please pick up 10 Mat Confetti Grey 3x10" in posts[0][1] and "Nov 12" in posts[0][1]
    assert posts[1][0] == "CDESK" and "Stopped items still in inventory" in posts[1][1]
    run(d, monkeypatch, mails, date(2026, 11, 10))
    assert len(posts) == 2 and len(mails) == 1  # once each
    d.alliant.items["4000-1-00400"]["MAT CONFETTI GREY 3X10"] = 0  # the office lowered it: off the list
    run(d, monkeypatch, mails, date(2026, 11, 11))
    assert stops_watch.StopsWatch(d).load()["items"] == {}


def test_a_stopped_wearers_garments_email_the_customer_only(tmp_path, monkeypatch):
    d, posts, mails = make(tmp_path, {"4000-1-00400": {"TOWEL BAR MOP": 60}}, {"4000-1-00400": {"TOWEL BAR MOP": 60}})
    run(d, monkeypatch, mails, date(2026, 10, 27))
    d.alliant.stopped_wearers[("4000-1-00400", "7")] = {"name": "DAVE SMITH", "stopped": "10/28/2026"}
    d.alliant.in_service[("4000-1-00400", "7")] = [
        {"barcode": "000000111222", "description": "SHIRT COOK BLACK", "size_a": "XL", "status": "V"},
        {"barcode": "000000111223", "description": "SHIRT COOK BLACK", "size_a": "XL", "status": "I"}]  # back at the plant
    assert run(d, monkeypatch, mails, date(2026, 10, 29))["garments"] == 1
    [(to, subject, body)] = mails
    assert "Dave Smith's garments (stopped October 28):" in body and "barcode 000000111222" in body
    assert "000000111223" not in body and "Wednesday, November 11" in body and not posts  # staff aren't told
    assert "We're looking for these exact ones" in body  # Bobby's coveralls: a different garment back doesn't count
    run(d, monkeypatch, mails, date(2026, 11, 7))  # day 10, one still out: one friendly reminder
    assert len(mails) == 2 and mails[1][1] == "a reminder about items to send back" and "friendly reminder" in mails[1][2]
    run(d, monkeypatch, mails, date(2026, 11, 8))
    assert len(mails) == 2 and not posts


def test_a_whole_account_stopped_gets_totals_not_barcodes(tmp_path, monkeypatch):
    from .context import Wearer
    d, posts, mails = make(tmp_path, {"4000-1-00400": {"MAT CONFETTI GREY 3X10": 10}}, {"4000-1-00400": {"MAT CONFETTI GREY 3X10": 5}})
    d.alliant.wearers["4000-1-00400"] = [Wearer("7", "DAVE", "SMITH"), Wearer("8", "ANA", "LEE")]
    run(d, monkeypatch, mails, date(2026, 10, 27))
    d.alliant.autocount["4000-1-00400"]["MAT CONFETTI GREY 3X10"] = 0  # the account is stopped: everything
    d.alliant.wearers["4000-1-00400"] = []
    for w, name in (("7", "DAVE SMITH"), ("8", "ANA LEE")):
        d.alliant.stopped_wearers[("4000-1-00400", w)] = {"name": name, "stopped": "10/29/2026"}
        d.alliant.in_service[("4000-1-00400", w)] = [{"barcode": f"0000{w}{i}", "description": "SHIRT COOK BLACK",
                                                       "size_a": "XL", "status": "V"} for i in range(3)]
    run(d, monkeypatch, mails, date(2026, 10, 29))
    [(to, subject, body)] = mails
    assert "Now that your service has ended" in body and "- 10 Mat Confetti Grey 3x10\n- 6 Shirt Cook Black XL" in body
    assert "barcode" not in body and "exact ones" not in body

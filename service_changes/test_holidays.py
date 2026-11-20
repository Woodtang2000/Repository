from datetime import date

from . import announce, holidays
from .test_announce import desk


def test_the_six_holidays():
    assert holidays.holidays(2027) == [
        (date(2027, 1, 1), "New Year's Day"), (date(2027, 5, 31), "Memorial Day"), (date(2027, 7, 4), "the 4th of July"),
        (date(2027, 9, 6), "Labor Day"), (date(2027, 11, 25), "Thanksgiving"), (date(2027, 12, 25), "Christmas Day")]


def test_stops_slide_toward_wednesday_or_thursday_on_routes_5_and_12():
    # Christmas 2026 on a Friday: Friday's stops on Thursday, Thursday's on Wednesday (Scott, 11/13)
    assert holidays.moves(date(2026, 12, 25)) == {date(2026, 12, 24): date(2026, 12, 23), date(2026, 12, 25): date(2026, 12, 24)}
    assert holidays.moves(date(2026, 11, 26)) == {date(2026, 11, 26): date(2026, 11, 25)}  # Thanksgiving
    # Memorial Day: Monday's on Tuesday, Tuesday's on Wednesday
    assert holidays.moves(date(2027, 5, 31)) == {date(2027, 5, 31): date(2027, 6, 1), date(2027, 6, 1): date(2027, 6, 2)}
    assert holidays.slide_day("1", "12") == "Thu" and holidays.slide_day("1", "05") == "Thu" and holidays.slide_day("2", "5") == "Wed"
    kenai = holidays.moves(date(2027, 5, 31), "Thu")  # M T W F: Wednesday's go to Thursday too
    assert kenai[date(2027, 6, 2)] == date(2027, 6, 3) and len(kenai) == 3
    assert holidays.moves(date(2026, 11, 26), "Thu") == {}  # Thanksgiving: their Thursday is already off
    assert holidays.moves(date(2027, 7, 4)) == {}  # a Sunday: nothing moves here


def test_a_week_ahead_each_customer_gets_their_own_day_after_the_office_taps_send(tmp_path, monkeypatch):
    d = desk(tmp_path)
    d.alliant.stops += [{"branch": "1", "route": "12", "day": "Fri", "account": "8800-1-00001"},
                        {"branch": "1", "route": "4", "day": "Mon", "account": "1300-1-00001"}]
    (tmp_path / "customer_phones.csv").write_text("phone,accounts,name\n(907) 555-1234,1205-1-00004,Jane\n"
                                                  "(907) 555-2222,8800-1-00001,Kenai\n")
    d.announcer = lambda a=announce.Announcer(d): a
    assert holidays.holiday_pass(d, date(2026, 12, 16)) == []  # 9 days out: not yet
    assert holidays.holiday_pass(d, date(2026, 12, 17)) == ["holiday/2026-12-25"]  # the preview, the day before
    assert holidays.holiday_pass(d, date(2026, 12, 17)) == []  # once
    card = d.slack.posts[-1]["text"]
    assert "*Christmas Day, Friday December 25*: 3 customers' deliveries move" in card and "Thu → Wed 12/23: 2" in card and "1 Laundry Group customers hear we're open as usual" in card and "on its own *Friday December 18 at 9 AM*" in card
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: sent.append((to, body)))
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: sent.append((to, text)))
    assert d.announcer().act({"user": {"id": "UANA"}, "actions": [{"action_id": announce.SEND, "value": "holiday/2026-12-25"}],
                              "container": {"message_ts": "5.5"}}).startswith("sent")
    texts = dict(sent)
    assert texts["+19075551234"] == ("Snow White Linen: We're closed Friday, December 25 for Christmas Day, so your Thursday "
                                     "delivery comes Wednesday, December 23 instead. Questions? Just reply, or reply STOP to opt out.")
    assert "your Friday delivery comes Thursday, December 24" in texts["+19075552222"]  # route 12
    assert "Monday" not in " ".join(texts.values())  # Monday stops don't move at Christmas


def test_the_laundry_group_is_open_every_holiday():
    from .context import holiday_schedule
    assert holidays.slide_day("2", "4") == "Wed" and holidays.open_on_holidays("2") and not holidays.open_on_holidays("1")
    s = holiday_schedule(date(2026, 11, 12), branch="2", route="4")
    assert "Thanksgiving, Thursday, November 26: we're open; deliveries are as usual." in s and "instead" not in s


def test_laundry_group_customers_hear_we_are_open(tmp_path, monkeypatch):
    d = desk(tmp_path)  # its TLG route 4 hospital has a texting number
    d.announcer = lambda a=announce.Announcer(d): a
    assert holidays.holiday_pass(d, date(2027, 11, 17)) == ["holiday/2027-11-25"]  # Thanksgiving 2027
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: sent.append((to, body)))
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: sent.append((to, text)))
    d.announcer().act({"user": {"id": "UANA"}, "actions": [{"action_id": announce.SEND, "value": "holiday/2027-11-25"}],
                       "container": {"message_ts": "5.5"}})
    assert dict(sent)["+19075559999"] == ("The Laundry Group: Thanksgiving is Thursday, November 25, and we're open as "
                                          "always: your deliveries that week come on their usual days. Questions? Just reply, or reply STOP to opt out.")
    assert holidays.open_accounts(d.alliant, date(2027, 7, 4)) == []  # a Sunday: no deliveries that day anyway


def test_a_weekend_holiday_moves_nothing():
    from .context import holiday_schedule
    s = holiday_schedule(date(2027, 6, 20))
    assert "the 4th of July is Sunday, July 4, a weekend: deliveries that week are as usual." in s


def test_a_holiday_notice_goes_out_on_its_own_unless_cancelled(tmp_path, monkeypatch):
    # Scott, 11/18: "Holiday notices should just go out." The day-before card is a preview with Cancel.
    from datetime import datetime
    from .context import ALASKA
    d = desk(tmp_path)
    d.announcer = lambda a=announce.Announcer(d): a
    assert holidays.holiday_pass(d, date(2027, 11, 17)) == ["holiday/2027-11-25"]
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: sent.append(to))
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: sent.append(to))
    ann = d.announcer()
    before = datetime(2027, 11, 18, 8, 55, tzinfo=ALASKA).timestamp()
    at = datetime(2027, 11, 18, 9, 0, tzinfo=ALASKA).timestamp()
    assert ann.auto_pass(before) == [] and sent == []  # not yet
    assert ann.auto_pass(at) == ["holiday/2027-11-25"] and sent  # 9 AM a week ahead: out it goes
    assert ann.auto_pass(at + 600) == []  # once
    assert ann._load()["holiday/2027-11-25"]["status"] == "sent"
    assert "Sent by Donna (on schedule)" in d.slack.updates[-1]  # the preview card now says it went


def test_a_cancelled_holiday_notice_never_goes_out(tmp_path, monkeypatch):
    from datetime import datetime
    from .context import ALASKA
    d = desk(tmp_path)
    d.announcer = lambda a=announce.Announcer(d): a
    holidays.holiday_pass(d, date(2027, 11, 17))
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: sent.append(to))
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: sent.append(to))
    ann = d.announcer()
    assert ann.act({"user": {"id": "UANA"}, "actions": [{"action_id": announce.CANCEL, "value": "holiday/2027-11-25"}],
                    "container": {"message_ts": "5.5"}}) == "cancelled"
    assert ann.auto_pass(datetime(2027, 11, 18, 9, 0, tzinfo=ALASKA).timestamp()) == [] and sent == []


def test_without_the_preview_the_card_is_posted_at_send_time_and_goes_now(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLIDAY_PREVIEW", "off")
    d = desk(tmp_path)
    d.announcer = lambda a=announce.Announcer(d): a
    assert holidays.holiday_pass(d, date(2027, 11, 17)) == []  # no day-before card
    assert holidays.holiday_pass(d, date(2027, 11, 18)) == ["holiday/2027-11-25"]
    assert "sent on its own *now*" in d.slack.posts[-1]["text"]
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: None)
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: None)
    import time as _t
    assert d.announcer().auto_pass(_t.time() + 1) == ["holiday/2027-11-25"]


def _everyone(tmp_path):
    d = desk(tmp_path)
    d.alliant.stops.append({"branch": "1", "route": "7", "day": "Mon", "account": "1500-1-00001"})
    d.alliant.cards["1500-1-00001"] = {"email": "mon@cafe.com"}
    (tmp_path / "customer_phones.csv").write_text((tmp_path / "customer_phones.csv").read_text()
                                                  + "(907) 555-7777,1500-1-00001,Monday Cafe\n")
    (tmp_path / "customer_emails.csv").write_text("email,accounts,name\nlinked@midas.com,1205-1-00004,Store manager\n")
    d.announcer = lambda a=announce.Announcer(d): a
    return d


def test_every_customer_hears_and_the_alliant_card_email_always_does(tmp_path):
    # Scott, 11/19: "always include the Alliant card email too... Every customer should see the email"
    d = _everyone(tmp_path)
    assert holidays.holiday_pass(d, date(2027, 11, 17)) == ["holiday/2027-11-25"]
    a = d.announcer()._load()["holiday/2027-11-25"]
    assert {"linked@midas.com", "ap@midas.com", "owner@midas.com", "mon@cafe.com"} <= set(a["emails"])
    assert "+19075557777" not in a["phones"] and "+19075551234" in a["phones"]  # texts only where the day moves
    assert a["messages"]["1500-1-00001"] == ("We're closed Thursday, November 25 for Thanksgiving. Your deliveries that week "
                                             "come on their usual days. We're delivering as usual on Friday, November 26. "
                                             "Questions? Just reply.")
    assert "your Thursday delivery comes Wednesday, November 24 instead. We're delivering as usual on Friday" in \
        a["messages"]["1205-1-00004"]
    assert "1 more hear their days don't change" in d.slack.posts[-1]["text"]
    assert "delivering as usual" not in holidays.usual_notice("Christmas Day", date(2026, 12, 25))  # Thursday holidays only
    assert holidays.usual_accounts(d.alliant, date(2027, 7, 4), set()) == []  # a weekend: no notice


def test_a_notice_already_sent_is_topped_up_to_everyone_once(tmp_path, monkeypatch):
    d = _everyone(tmp_path)
    ann = d.announcer()
    data = {"holiday/2027-11-25": {"status": "sent", "who": "Thanksgiving notice", "message": "old", "subject": "Thanksgiving",
                                   "messages": {"1205-1-00004": "old Midas"}, "phones": {"+19075551234": "1205-1-00004"},
                                   "emails": {"linked@midas.com": "1205-1-00004"}, "at": 1.0, "sent": {"texts": 1, "emails": 1}}}
    ann._save(data)
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body, **kw: sent.append(to))
    monkeypatch.setattr(bird, "send", lambda to, subject, text, **kw: sent.append((to, text)))
    monkeypatch.setattr(announce, "EMAIL_GAP", 0)
    said = []
    assert announce.top_up(ann, "holiday/2027-11-25", False, said.append) == {"texts": 0, "emails": 0} and not sent
    assert "1 more texts and 4 more emails" in said[0]  # the TLG hospital's text; Midas' 2 card emails, Costco's, Monday's
    announce.top_up(ann, "holiday/2027-11-25", True, said.append)
    to = {x[0] for x in sent if isinstance(x, tuple)}
    assert to == {"ap@midas.com", "owner@midas.com", "deli@costco.com", "mon@cafe.com"}
    assert "linked@midas.com" not in to  # already told
    mails = [x for x in sent if isinstance(x, tuple)]
    midas = [t for e, t in mails if e == "ap@midas.com"][0]
    assert "old Midas" in midas  # they keep what Midas was told
    assert "usual days" in [t for e, t in mails if e == "mon@cafe.com"][0]
    sent.clear()
    announce.top_up(ann, "holiday/2027-11-25", True, said.append)
    assert sent == []  # once

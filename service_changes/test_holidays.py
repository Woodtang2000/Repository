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
    assert holidays.holiday_pass(d, date(2026, 12, 15)) == []  # 10 days out: not yet
    assert holidays.holiday_pass(d, date(2026, 12, 16)) == ["holiday/2026-12-25"]
    assert holidays.holiday_pass(d, date(2026, 12, 16)) == []  # once
    card = d.slack.posts[-1]["text"]
    assert "*Christmas Day, Friday December 25*: 3 customers' deliveries move" in card and "Thu → Wed 12/23: 2" in card and "1 Laundry Group customers hear we're open as usual" in card and "by *Friday December 18*" in card
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body: sent.append((to, body)))
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
    assert holidays.holiday_pass(d, date(2027, 11, 16)) == ["holiday/2027-11-25"]  # Thanksgiving 2027
    sent = []
    import service_changes.sms as sms
    from . import bird
    monkeypatch.setattr(sms, "send_sms", lambda to, body: sent.append((to, body)))
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

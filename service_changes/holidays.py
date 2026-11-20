"""Holiday deliveries (Scott, 11/15): the six holidays we close, how each route's stops slide that week, and the notice
customers get a week ahead.

- Closed: New Year's Day, Memorial Day, the 4th of July, Labor Day, Thanksgiving and Christmas Day.
- Most routes run 4 tens, Mon Tue Thu Fri, and use Wednesday to absorb the holiday: a stop between Wednesday and the
  holiday slides one day toward Wednesday (Thanksgiving: Thursday's stops on Wednesday; Christmas on a Friday: Friday's
  on Thursday and Thursday's on Wednesday; Memorial Day: Monday's on Tuesday and Tuesday's on Wednesday).
- Routes 5 and 12 (Kenai Peninsula, Fairbanks) run Mon Tue Wed Fri around third-party shipping and slide the same way
  using Thursday.
- The Laundry Group (branch 2) works six days a week and doesn't close for holidays (Scott, 11/15): its stops never
  move, and its customers are told, in the same notice, that we're open as always (peace of mind).
- A holiday on a weekend moves nothing (Scott, 11/15), and Snow White customers get no notice for it.
- Every customer hears (Scott, 11/19: "Every customer should see the email. It is good customer service even if it
  doesn't affect them"): those whose day moves get their new day by text and email; everyone else gets an email that
  their days don't change. Each account's Alliant card email is always included, with its linked contacts. A Thursday
  holiday (Thanksgiving) also says we're delivering as usual on Friday.
- A week before (NOTICE_DAYS), at 9 AM, the notice goes out on its own (Scott, 11/18: "Holiday notices should just go
  out"): every customer whose delivery moves, by text and email, each told their own day.
- The day before that, Donna posts a preview in #service-desk with Send now / Cancel (HOLIDAY_PREVIEW, default on).
  With the preview off, the card is posted at send time and the notice goes straight out.
"""
import os
from datetime import date, timedelta

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NOTICE_DAYS = 7          # customers hear a week before the holiday (Scott, 11/15)
SEND_HOUR = 9            # it goes out at 9 AM Alaska time on that day


def preview() -> bool:
    """The day-before preview with Cancel (Guardrail's suggestion, 11/18; Scott to decide): on unless HOLIDAY_PREVIEW=off."""
    return os.environ.get("HOLIDAY_PREVIEW", "on").lower() not in ("off", "0", "no", "false")


def post_days() -> int:
    return NOTICE_DAYS + 1 if preview() else NOTICE_DAYS
SLIDE_DAY = "Wed"
# (branch, route) -> the day that route absorbs a holiday (Scott, 11/15: routes 5 and 12 work M T W F)
SLIDE = {("1", "5"): "Thu", ("1", "12"): "Thu"}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def holidays(year: int) -> list[tuple[date, str]]:
    return [(date(year, 1, 1), "New Year's Day"), (_last_weekday(year, 5, 0), "Memorial Day"),
            (date(year, 7, 4), "the 4th of July"), (_nth_weekday(year, 9, 0, 1), "Labor Day"),
            (_nth_weekday(year, 11, 3, 4), "Thanksgiving"), (date(year, 12, 25), "Christmas Day")]


def open_on_holidays(branch: str) -> bool:
    return (str(branch).lstrip("0") or "1") == "2"  # The Laundry Group: six days a week, every holiday


def slide_day(branch: str, route: str) -> str:
    return SLIDE.get((str(branch).lstrip("0") or "1", str(route).lstrip("0")), SLIDE_DAY)


def moves(holiday: date, slide: str = SLIDE_DAY) -> dict[date, date]:
    """{the usual delivery date: the date it's delivered instead} for one route's week. Weekends and a holiday on the
    slide day itself move nothing."""
    h = holiday.weekday()
    s = DAYS.index(slide)
    if h >= 5 or h == s:
        return {}
    monday = holiday - timedelta(days=h)
    if h > s:   # later in the week than the slide day: each day from the slide day to the holiday moves back one
        return {monday + timedelta(days=d): monday + timedelta(days=d - 1) for d in range(s + 1, h + 1)}
    return {monday + timedelta(days=d): monday + timedelta(days=d + 1) for d in range(h, s)}  # earlier: forward one


def upcoming(today: date, days: int = 60) -> list[tuple[date, str]]:
    return [(d, name) for y in (today.year, today.year + 1) for d, name in holidays(y) if today <= d <= today + timedelta(days=days)]


def _day(d: date) -> str:
    return f"{d:%A, %B} {d.day}"


def schedule_text(today: date, branch: str = "1", route: str = "", days: int = 60) -> list[str]:
    """Plain lines for the holidays in the next `days` days, for one route (or the usual routes when none is given)."""
    out = []
    if open_on_holidays(branch):
        return [f"- {name}, {_day(d)}: we're open; deliveries are as usual." for d, name in upcoming(today, days)]
    for d, name in upcoming(today, days):
        if d.weekday() >= 5:
            out.append(f"- {name} is {_day(d)}, a weekend: deliveries that week are as usual.")
            continue
        m = moves(d, slide_day(branch, route) if route else SLIDE_DAY)
        if not m:
            out.append(f"- {name}, {_day(d)}: closed; this route's deliveries that week are as usual.")
            continue
        out.append(f"- {name}, {_day(d)}: closed. " + " ".join(f"{_day(a)} deliveries come {_day(b)} instead."
                                                              for a, b in sorted(m.items())))
    return out


def _friday(holiday: date) -> str:
    """Thanksgiving (any Thursday holiday): Friday's stops don't move, and customers like to hear it (Scott, 11/19)."""
    return f" We're delivering as usual on {_day(holiday + timedelta(days=1))}." if holiday.weekday() == 3 else ""


def notice(name: str, holiday: date, account_moves: list[tuple[date, date]]) -> str:
    """What one customer reads: their own day or days."""
    moved = " and ".join(f"your {a:%A} delivery comes {_day(b)}" for a, b in sorted(account_moves))
    return f"We're closed {_day(holiday)} for {name}, so {moved} instead.{_friday(holiday)} Questions? Just reply."


def usual_notice(name: str, holiday: date) -> str:
    """Everyone whose days don't move: we're closed, which stops move that week (Scott, 11/19: "should also say that the
    Thursday stops will be delivered on Wednesday"), and theirs don't."""
    moved = " and ".join(f"{a:%A} deliveries come {_day(b)}" for a, b in sorted(moves(holiday).items()))
    return (f"We're closed {_day(holiday)} for {name}" + (f", so {moved} this week" if moved else "") + ". "
            f"Your deliveries come on their usual days.{_friday(holiday)} Questions? Just reply.")


def usual_accounts(alliant, holiday: date, moved) -> list[str]:
    """Snow White customers with a route stop whose day doesn't move (none for a weekend holiday: no notice then)."""
    if holiday.weekday() >= 5:
        return []
    return list(dict.fromkeys(r["account"] for r in getattr(alliant, "stops", [])
                              if r.get("account") and r["account"] not in moved and not open_on_holidays(r.get("branch", "1"))))


def all_notices(alliant, name: str, holiday: date) -> tuple[dict[str, str], set[str]]:
    """{account: its message} for every customer, and the accounts that also get a text (their day moves, or TLG)."""
    per_account = plan(alliant, holiday)
    still_open = [a for a in open_accounts(alliant, holiday) if a not in per_account]
    messages = {acct: notice(name, holiday, m) for acct, m in per_account.items()}
    messages.update({acct: open_notice(name, holiday) for acct in still_open})
    messages.update({acct: usual_notice(name, holiday) for acct in usual_accounts(alliant, holiday, messages)})
    return messages, set(per_account) | set(still_open)


def open_notice(name: str, holiday: date) -> str:
    """The Laundry Group's customers: peace of mind that we're working the holiday (Scott, 11/15)."""
    return (f"{name} is {_day(holiday)}, and we're open as always: your deliveries that week come on their usual days. "
            "Questions? Just reply.")


def open_accounts(alliant, holiday: date) -> list[str]:
    """The Laundry Group customers with a stop the holiday week (none for a Sunday holiday: no deliveries that day)."""
    if holiday.weekday() == 6:
        return []
    return list(dict.fromkeys(r["account"] for r in getattr(alliant, "stops", [])
                              if r.get("account") and open_on_holidays(r.get("branch", "1"))))


def plan(alliant, holiday: date) -> dict[str, list[tuple[date, date]]]:
    """{account: [(usual date, new date), ...]} for every stop whose day moves that week (route_stops.csv)."""
    out: dict[str, list[tuple[date, date]]] = {}
    monday = holiday - timedelta(days=holiday.weekday())
    for r in getattr(alliant, "stops", []):
        day = (r.get("day") or "")[:3].title()
        if day not in DAYS or not r.get("account") or open_on_holidays(r.get("branch", "1")):
            continue
        usual = monday + timedelta(days=DAYS.index(day))
        new = moves(holiday, slide_day(r.get("branch", "1"), r.get("route", ""))).get(usual)
        if new and (usual, new) not in out.get(r["account"], []):
            out.setdefault(r["account"], []).append((usual, new))
    return out


def holiday_pass(desk, today: date) -> list[str]:
    """Once a day: a holiday post_days() away gets its notice card in #service-desk (once). Returns what was posted."""
    from .announce import Announcer  # noqa: F401  (the Send / Cancel buttons and the sending live there)
    posted = []
    for d, name in upcoming(today, post_days()):
        if (d - today).days != post_days():
            continue
        key = f"holiday/{d.isoformat()}"
        ann = desk.announcer()
        if key in ann._load():
            continue
        per_account, still_open = plan(desk.alliant, d), open_accounts(desk.alliant, d)
        usual = usual_accounts(desk.alliant, d, set(per_account) | set(still_open))
        if (per_account or still_open) and ann.propose_holiday(key, name, d, per_account, still_open, usual):
            posted.append(key)
    return posted


def enabled() -> bool:
    return os.environ.get("HOLIDAY_NOTICES", "on").lower() not in ("off", "0", "no", "false")

"""Turn one route-channel Slack message into a ParsedMessage with Claude."""
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from .context import Alliant, route_from_channel, service_day
from .schema import ParsedMessage

MODEL = "claude-opus-5-5"

SYSTEM = """\
You read service-change messages that route drivers and office staff post in Snow White Linen's
Slack route channels. Snow White is a linen and uniform rental company. Each customer has a
standing weekly invoice in Alliant (the route accounting system) that repeats unless changed.

Classify the message and extract every change in it. Rules:
- One Change per item or per wearer garment line. "Add 1 bag stand and 1 laundry bag" is two changes.
- "Add N" / "increase N" / "N more" is add. "Decrease N" / "reduce by N" / "remove N" is decrease.
  "Increase to N" / "reduce to N" / "should be N" is set with quantity N. "Stop" / "cancel" / "remove the X" is stop.
- Keep the driver's stated total ("Total 40", "making 7 total", "2 total") in stated_total. Do not compute it.
- Wearer changes: put the employee in wearer, the garment in item, the size in size. A new wearer with
  shirts and pants is two changes. "Stop all garments for X" is one stop change with item "all garments".
- Drivers abbreviate: "2" can mean "to", "4" can mean "for", "N" can mean "in"; "3\\"10" means 3x10 mat.
- Use the candidate customer list to fill account_number only when one candidate clearly matches.
  Store numbers and departments matter: "Safeway 1817 Deli" is not "Safeway 1817 Meat".
- Add a question for the driver only when something needed to enter the change is genuinely missing or
  ambiguous (which customer, which mat, which size, which employee). Do not ask about things that are clear.
- Alliant frequency codes: 1-4 = once a month in that week, 5 or 6 = every other week, 7 = weekly,
  8 = more than once a week, 9 = first delivery only, 0 = no delivery, A1-A3 = once every 8 weeks in that week. Write frequency in plain words ("frequency 7" -> "weekly"; "bi-weekly" -> "every other week").
- Pack sizes are quantities: "a 10 pack of black aprons" is 10 aprons. Replacing a 20 pack with a 10 pack is
  set to 10, not stop plus add 1.
- When the driver gives a total and then a breakdown ("making a total of 10: 5 orange 24 oz and 5 blue 16 oz"),
  the breakdown numbers are each item's new total: set 24 oz to 5 and 16 oz to 5.
- "Stop everything except X" is one stop change for the rest; do not add a second change for X.
- Never carry a size from one garment to another ("coats 3x" says nothing about the shirts). A new wearer's
  garment with no size gets a question.
- A wearer who "needs" a garment may mean a one-time replacement or a standing addition; ask which unless
  the driver says.
- Holds, closures, skipped weeks and cancelled accounts are hold_or_closure with NO changes: describe them in
  summary only ("closed 9/5 to 9/13"). Route order, redates and stop moves are route_or_schedule with no changes.
- When part of a message is clear and part isn't, list the clear changes AND ask about the rest; don't hold
  back a certain change because another one is in doubt.
- Record what the driver is asking the office to do, not everything mentioned. If a customer stopped two mats but
  the driver thinks only one should go, that is a question for the driver, not two stops.
- Garments for a department (meat, deli, bakery, seafood) belong to that department's account when one exists.
- The list may show other names a customer goes by ("also called: BSI"); use them to match.
- Office staff confirm changes in the channel ("added 2 more", "stopped wet mops", "decrease 40 bar mops" right
  after a driver asked for it, "done", "ok"). A message from office staff that confirms or repeats the previous
  request is not_a_request. Office staff can also post new requests (a customer called in); treat those normally.
- A message that only makes sense with the previous one ("Actually cancel the special", "make that 6") applies to
  that previous request: say what it changes, using the customer from the previous message.
- Plant completion posts ("You're 100% complete with the linens"), "ok", "done", and other replies are not_a_request.
"""


def build_prompt(text: str, channel: str, ts: str, alliant: Alliant, author: str = "", office: bool = False,
                 previous: str = "") -> str:
    route = route_from_channel(channel)
    day = service_day(ts)
    lines = [f"Channel: #{channel} (route {route or 'unknown'}), posted {day}."]
    cands = alliant.candidates(route, day)
    if cands:
        lines.append(f"Customers on this route (account | name | service days). Prefer {day} stops; "
                     "drivers sometimes post a day late:")
        lines += [f"{c.account} | {c.name} | {';'.join(c.service_days)}"
                  + (f" | also called: {', '.join(alliant.aliases[c.account])}" if c.account in alliant.aliases else "")
                  for c in cands]
    else:
        lines.append("No customer list loaded; leave account_number null.")
    if previous:
        lines += ["", "Previous message in the channel (context only, already handled):", previous]
    if author:
        lines += ["", f"Posted by: {author} ({'office staff' if office else 'driver or sales'})"]
    lines += ["", "Message:", text]
    return "\n".join(lines)


class ItemMatch(BaseModel):
    alliant_items: list[Optional[str]] = Field(
        description="For each driver item, in order: the exact Alliant item name it refers to, or null if none clearly fits.")


def match_items(client: anthropic.Anthropic, driver_items: list[str], account_items: list[str]) -> list[str | None]:
    """Second pass, only for items the word match couldn't place: let Claude pick from the account's real list."""
    prompt = ("Alliant items on this customer's account:\n" + "\n".join(account_items)
              + "\n\nWhich Alliant item does each driver item mean? Drivers leave out colors and use slang "
                "('mop heads' = MOP WET, 'bibs' = APRON ... BIB), but a color they DO name must match: if no item "
                "on the account is that color, return null (it may already be stopped). "
                "Return null if two items fit equally or none fits.\n"
              + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(driver_items)))
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        output_format=ItemMatch,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    names = response.parsed_output.alliant_items if response.parsed_output else []
    return [n if n in account_items else None for n in (names + [None] * len(driver_items))[: len(driver_items)]]


def parse_message(client: anthropic.Anthropic, text: str, channel: str, ts: str, alliant: Alliant,
                  author: str = "", office: bool = False, previous: str = "") -> ParsedMessage:
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(text, channel, ts, alliant, author, office, previous)}],
        output_format=ParsedMessage,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Could not parse message (stop_reason={response.stop_reason})")
    return response.parsed_output

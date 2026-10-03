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
- Plant completion posts ("You're 100% complete with the linens"), "ok", "done", and other replies are not_a_request.
"""


def build_prompt(text: str, channel: str, ts: str, alliant: Alliant) -> str:
    route = route_from_channel(channel)
    day = service_day(ts)
    lines = [f"Channel: #{channel} (route {route or 'unknown'}), posted {day}."]
    cands = alliant.candidates(route, day)
    if cands:
        lines.append(f"Customers on this route (account | name | service days). Prefer {day} stops; "
                     "drivers sometimes post a day late:")
        lines += [f"{c.account} | {c.name} | {';'.join(c.service_days)}" for c in cands]
    else:
        lines.append("No customer list loaded; leave account_number null.")
    lines += ["", "Message:", text]
    return "\n".join(lines)


class ItemMatch(BaseModel):
    alliant_items: list[Optional[str]] = Field(
        description="For each driver item, in order: the exact Alliant item name it refers to, or null if none clearly fits.")


def match_items(client: anthropic.Anthropic, driver_items: list[str], account_items: list[str]) -> list[str | None]:
    """Second pass, only for items the word match couldn't place: let Claude pick from the account's real list."""
    prompt = ("Alliant items on this customer's account:\n" + "\n".join(account_items)
              + "\n\nWhich Alliant item does each driver item mean? Drivers leave out colors and use slang "
                "('mop heads' = MOP WET, 'bibs' = APRON ... BIB). Return null if two items fit equally or none fits.\n"
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


def parse_message(client: anthropic.Anthropic, text: str, channel: str, ts: str, alliant: Alliant) -> ParsedMessage:
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(text, channel, ts, alliant)}],
        output_format=ParsedMessage,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Could not parse message (stop_reason={response.stop_reason})")
    return response.parsed_output

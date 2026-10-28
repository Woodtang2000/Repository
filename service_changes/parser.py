"""Turn one route-channel Slack message into a ParsedMessage with Claude."""
import os
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
- When we asked "<customer> has N <item> now. Add N more (total T), or should they have N total?", read the answer:
  "add N", "N more", "more", "add", "yes", "T", "total T" or "both" means N MORE: put T in stated_total. "N total",
  "just N", "only N" or "keep N" means they should have N: action set with quantity N. Never ask it again after an
  answer like these; if the answer also says how often ("weekly"), that's the frequency.
- Pack sizes are quantities: "a 10 pack of black aprons" is 10 aprons. Replacing a 20 pack with a 10 pack is
  set to 10, not stop plus add 1.
- When the driver gives a total and then a breakdown ("making a total of 10: 5 orange 24 oz and 5 blue 16 oz"),
  the breakdown numbers are each item's new total: set 24 oz to 5 and 16 oz to 5.
- "Stop everything except X" is one stop change for the rest; do not add a second change for X.
- New wearers get a set number of each garment: the standard is 11 for someone who works 5 days a week (5 at the
  laundry, 5 delivered, 1 being worn), 13 for 6 days: 2 x days worked + 1. If the driver says how many days they work,
  use that as the quantity; if the driver gives no quantity at all, leave quantity null (the standard is filled in
  later). Never ask how many garments a new wearer needs.
- Never carry a size from one garment to another ("coats 3x" says nothing about the shirts). A new wearer's
  garment with no size gets a question.
- A wearer who "needs" a garment, or a new garment for a wearer, is a standing addition unless the driver says it
  is a one-time replacement. Don't ask which.
- A change to today's delivery only ("set bar mops to 0 today only", "skip the mats today", "short 10 towels this
  delivery") is today_only with NO changes: the driver changes it on the handheld and the office only needs to know.
  Describe it in summary ("bar mops 0 today only"). Don't ask questions about it.
- Holds, closures, skipped weeks and cancelled accounts are hold_or_closure with NO changes: describe them in
  summary only ("closed 9/5 to 9/13"). Route order, redates and stop moves are route_or_schedule with no changes.
- When part of a message is clear and part isn't, list the clear changes AND ask about the rest; don't hold
  back a certain change because another one is in doubt.
- Record what the driver is asking the office to do, not everything mentioned. If a customer stopped two mats but
  the driver thinks only one should go, that is a question for the driver, not two stops.
- Garments for a department (meat, deli, bakery, seafood) belong to that department's account when one exists.
- Drivers sometimes write the end of the account number instead of the name ("Swickard 00009" = 2074-1-00009):
  match it to the candidate whose account number ends that way.
- "Fix the inventory, 6 total" / "inventory should be 20" is about Alliant's inventory (reg inv), not the
  per-delivery count: set inventory true on that change.
- One message can cover several accounts ("20 bar mops for the bakery and a new employee John in tire",
  "add a mat at Safeway 1817 and 1821"). Then give each change its own account_number and leave the message's
  account_number null. When everything is for one account, leave the changes' account_number null.
- The list may show other names a customer goes by ("also called: BSI"); use them to match.
- Office staff confirm changes in the channel ("added 2 more", "stopped wet mops", "decrease 40 bar mops" right
  after a driver asked for it, "done", "ok"). A message from office staff that confirms or repeats the previous
  request is not_a_request. Office staff can also post new requests (a customer called in); treat those normally.
- A message that only makes sense with the previous one ("Actually cancel the special", "make that 6") applies to
  that previous request: say what it changes, using the customer from the previous message.
- Plant completion posts ("You're 100% complete with the linens"), "ok", "done", and other replies are not_a_request.
- A report that shows a standing change is needed is that change, not just an operational issue: garments still
  coming for someone who "doesn't work there anymore" / "left" is a stop for that wearer; deliveries still going to a
  customer that is closed for the season is hold_or_closure. List the stop (or describe the hold) and keep the rest
  of the report in summary.
- Plant and dispatch staff talk to drivers in the route channels: carts or mats left at the plant, "pick up the mat at
  Coast Pizza Thursday", "call me", route coverage, feedback on a delivery. Those are not_a_request. From them, only a
  change to a customer's standing items or wearers, a hold, or a special order is a request.
- A question asking what a customer has or gets ("how many bar mops does Humpy's get?", "what size is Juan?",
  "what's their stop number?", "when do they get mats?") is lookup with no changes: fill the customer and account and
  put the question in summary. A question that asks the office to do something ("can you call them?") is not lookup.
  After Service Desk (Donna) answered a lookup, a follow-up ("and shop towels?") is another lookup for the same customer.
  Questions about where garments are or whether they've been ordered or came in ("do we have garments on the way for
  new employee Dave?", "where are Joey's shirts?", "did the new coats come in?") are lookup too: put the wearer and
  the question in summary. A customer's address or phone number is a lookup.
- Anything that asks the office to change something in Alliant (add, stop, size change, hold, a service code or
  repair on a garment, an inventory count) is that request, never lookup, even if it also asks a question.
- A question about the route itself, not one customer ("what are my stops today?", "who's after Midas?", "how many
  stops on Thursday?", "what's the stop order?") is route_lookup: put the question in summary.
- A question about how to do something in Alliant or on the handheld ("how do I add a stop on the handheld?", "how do I
  scan a garment back in?", "how do I void a ticket?") is howto: put the question in summary.
- A request to have the plant pull garments for repair or a closer look is service_code with NO changes: "put a
  service code on 1234567, torn pocket", "Joey's shirts need buttons", "audit all of Juan's garments", "inspect
  every garment at Midas 7, they look worn". Fill service: scope garment (barcodes given), wearer (all of one
  employee's garments) or customer (the whole account); kind repair, inspection (audit, condition check), relabel or
  other; reason in a few words. "Take an inventory at Midas 7" / "we need a count at Kobuk" is service_code with
  scope customer and kind inventory (say in reason what to count if the driver did: garments, mats, linen). Ask only if you can't tell which garment/wearer/customer or what's wrong. A new
  garment or replacement for a wearer is still wearer_change.
- The message may be a whole thread: the original request, questions Service Desk (Donna) asked, and the driver's answers,
  sometimes followed by what the office entered and the driver's reply to that. Read it as one request as it stands
  now: answers fill in or override the original. Only ask again about what is still unanswered. If the driver's last
  reply only acknowledges the office ("thanks", "ok", "👍"), it is not_a_request. If the driver says the office got
  it wrong, list the changes needed to make it right and say so in summary.
"""


def prompt_parts(text: str, channel: str, ts: str, alliant: Alliant, author: str = "", office: bool | str = False,
                 previous: str = "") -> tuple[str, str]:
    """(route context, this message). The context is the same for every message on a route and day, so it is cached."""
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
    rest = []
    if previous:
        rest += ["Previous message in the channel (context only, already handled):", previous, ""]
    if author:
        who = office if isinstance(office, str) else "office staff" if office else "driver or sales"
        rest += [f"Posted by: {author} ({who})", ""]
    rest += ["Message:", text]
    return "\n".join(lines), "\n".join(rest)


def build_prompt(*args, **kw) -> str:
    return "\n\n".join(prompt_parts(*args, **kw))


class Answer(BaseModel):
    answer: str = Field(description="Short plain answer for a route driver, from the account data only.")
    found: bool = Field(description="False if the account data doesn't contain what was asked.")


def answer_lookup(client: anthropic.Anthropic, question: str, facts: str, asker: str = "route driver") -> Answer:
    """Answer a driver's (or the customer's own) question about one account from its Alliant data."""
    prompt = (f"Alliant data for one customer account:\n{facts}\n\nA {asker} asks:\n{question}\n\n"
              + (f"Answer in one or two short lines a {asker.split()[-1]} can read on a phone, with the numbers they need "
                 "(per-delivery count and how often). " if "office" not in asker else
                 "Answer in a few short lines for the office. For garment questions say where each garment is from "
                 "its last scan and date, and for orders what was ordered, when, and what is still open. ")
              + "Use only the data above; if it doesn't say, set found to false and say what you couldn't find."
              + (" " + OWNER_VOICE if "owner" in asker else ""))
    response = client.beta.messages.parse(
        model=model or MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        output_format=Answer,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    return response.parsed_output or Answer(answer="", found=False)


OWNER_VOICE = ("You are Donna, and this is Scott, the owner: you're his Donna and he's your Harvey. Talk to him like "
               "the best right hand in the business: direct, confident, a step ahead, a little wit, no hedging and no "
               "customer-service politeness. Lead with the answer, then the one thing he should notice or do about it "
               "(a risk, an opportunity, who to call). Short lines he can read on a phone. Account numbers: NNNN-1-NNNNN is a "
               "Snow White Linen customer, NNNN-2-NNNNN is The Laundry Group; each company has its own route numbers.")


SPOKEN = (" This is a voice conversation: your reply is read aloud on his phone. Talk, don't write: no lists, "
          "headings, markdown, emoji, links, dashes or colons; numbers as people say them; two to four sentences unless "
          "he asks for more. Sound warm and easy, like you're smiling: full sentences that flow into each other (and, so, "
          "but), never clipped one-word fragments or a bare number on its own, which sound curt out loud. Confident, "
          "never cold. End on the next move, or a question if you need one.")


def owner_chat(client: anthropic.Anthropic, turns: list[dict], briefing: str, spoken: bool = False, person: str = "") -> str:
    """Donna talking with Scott in his private Slack message with the bot: strategy, plans, what to do next.
    `turns` is the conversation so far ([{"role", "content"}], ending with Scott's message). `person`: someone else
    Scott gave the Donna app to (Bill): the business, not Scott's private right hand."""
    if person:
        system = (f"You are Donna, the service desk for Snow White Linen and The Laundry Group, talking with {person}, "
                  "whom Scott (the owner) gave you to. Warm, sharp and brief. Help with customers, accounts, routes, service, "
                  "sales and Alliant. You are not Scott's private assistant here: don't share his plans or private "
                  "matters, your read on staff or on the team's messages, or anything about pay, discipline or HR; say "
                  "that's one for Scott. For a number about a customer, route or garment, ask for it as a question and "
                  "you'll pull it from Alliant; never make numbers up. Account numbers: NNNN-1-NNNNN is Snow White Linen, "
                  "NNNN-2-NNNNN The Laundry Group. Don't bring up that you're an AI, but if asked, say so honestly."
                  + (SPOKEN.replace("his phone", "their phone").replace("he asks", "they ask") if spoken else "")
                  + "\n\nWhere things stand:\n" + briefing)
        response = client.messages.create(model=os.environ.get("VOICE_MODEL", "claude-sonnet-5-5") if spoken else MODEL,
                                          max_tokens=2000, system=system, messages=turns)
        return "".join(b.text for b in response.content if b.type == "text").strip()
    system = (OWNER_VOICE + " He's here to kick some ass and you're here to help him do it: think with him about "
              "customers, routes, service, sales and the team, push back when he's wrong, and offer the next move. "
              "For a number about a specific customer, route or garment, tell him to ask it as a question (\"how many "
              "mats does Safeway 1807 get?\") and you'll pull it from Alliant; never make numbers up. Don't bring up "
              "that you're an AI, but if he asks, say so honestly."
              + (SPOKEN if spoken else "") + "\n\nWhere things stand:\n" + briefing)
    response = client.messages.create(
        model=os.environ.get("VOICE_MODEL", "claude-sonnet-5-5") if spoken else MODEL,
        max_tokens=2000,
        system=system,
        messages=turns,
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


class ItemMatch(BaseModel):
    alliant_items: list[Optional[str]] = Field(
        description="For each driver item, in order: the exact Alliant item name it refers to, or null if none clearly fits.")


def match_items(client: anthropic.Anthropic, driver_items: list[str], account_items: list[str],
                model: str | None = None) -> list[str | None]:
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


class ReplyCheck(BaseModel):
    reply: bool = Field(description="True if the new message answers, corrects or acknowledges what Service Desk said "
                                    "about that request; false if it is a separate request (another customer or item).")


def is_reply(client: anthropic.Anthropic, bot_said: str, request: str, message: str) -> bool:
    """Drivers answer in the main channel, not a thread: is this message about the bot's last question or readback?"""
    prompt = (f"A driver posted this service request:\n{request}\n\nService Desk then told the driver:\n{bot_said}\n\n"
              f"The driver's next message in the channel:\n{message}\n\n"
              "Is that message a reply to Service Desk about the same request (an answer, a correction, a thanks), "
              "or a new, separate request?")
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        output_format=ReplyCheck,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    return bool(response.parsed_output and response.parsed_output.reply)


def parse_message(client: anthropic.Anthropic, text: str, channel: str, ts: str, alliant: Alliant,
                  author: str = "", office: bool | str = False, previous: str = "", model: str | None = None) -> ParsedMessage:
    """`model`: a faster one for phone calls (phone.CHECK_MODEL); everything else uses MODEL."""
    context, message = prompt_parts(text, channel, ts, alliant, author, office, previous)
    response = client.beta.messages.parse(
        model=model or MODEL,
        max_tokens=16000,
        # Cache the instructions and the route's customer list: only the message itself changes between calls.
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": message}]}],
        output_format=ParsedMessage,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Could not parse message (stop_reason={response.stop_reason})")
    return response.parsed_output


class Identified(BaseModel):
    person_name: Optional[str] = Field(None, description="The texter's own name, if they gave it.")
    accounts: list[str] = Field(default_factory=list, description=(
        "Account numbers from the list that the business could be, best match first, at most 3. Empty if none fits."))


class Junk(BaseModel):
    junk: bool = Field(description="True for marketing, spam, newsletters, sales pitches TO us, vendor offers, job "
                                   "seekers or recruiters, invoices or notifications from services, phishing. False for a "
                                   "customer or a business interested in becoming one, writing about linen, mat, mop, "
                                   "towel, uniform or laundry service, their account, a delivery, a bill, or a question.")
    kind: str = Field(description="One or two words: marketing, spam, newsletter, vendor pitch, recruiting, notification, "
                                  "phishing, or customer.")


def is_junk_email(client: anthropic.Anthropic, sender: str, subject: str, body: str, bulk: bool = False) -> Junk:
    """Is an email from an unknown sender junk (so it gets no reply and no sign-up card)? When unsure, not junk:
    a real customer must never be filtered out."""
    prompt = (f"An email arrived at the customer service address of Snow White Linen Supply, a linen and uniform rental "
              f"company in Alaska.\nFrom: {sender}\nSubject: {subject}\n"
              + ("(It was sent with bulk-mail / mailing-list headers.)\n" if bulk else "")
              + f"\n{body[:3000]}\n\nIs it junk? If there's any real chance it's a customer or a prospect, it is NOT junk.")
    response = client.beta.messages.parse(
        model=os.environ.get("FILTER_MODEL", "claude-haiku-5-5"),
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
        output_format=Junk,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    return response.parsed_output or Junk(junk=False, kind="customer")


def identify_customer(client: anthropic.Anthropic, text: str, customers: list) -> Identified:
    """Which customer account(s) someone means by "Jane, Midas in Fairbanks". Only the office sees the result:
    it is a suggestion for a person to approve, never access by itself."""
    listing = "\n".join(f"{c.account} | {c.name}" for c in customers)
    prompt = (f"Customer accounts (account | name):\n{listing}\n\nSomeone texting us wrote:\n{text}\n\n"
              "Which account(s) could their business be? Use only account numbers from the list.")
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        output_format=Identified,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    found = response.parsed_output or Identified()
    known = {c.account for c in customers}
    found.accounts = [a for a in found.accounts if a in known][:3]
    return found

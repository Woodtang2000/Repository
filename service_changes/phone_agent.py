"""Donna's phone lines on ElevenLabs' voice agents: the same live conversation as Scott's Donna app (quick replies, the
caller can talk over her), with the rules and the Slack side unchanged from phone.py.

ElevenLabs answers both numbers (imported from Twilio; texting stays on Twilio). Each line has its own agent, whose
tools call this server (webchat, POST /api/phone/tool/<name>, header X-Donna-Key: PHONE_AGENT_TOKEN):

  caller_info      who's calling (a verified customer and their account facts, a known employee), office open?
  customer_request a customer's change or problem: the ✅ ticket a text makes (verified), else a call-back note
  new_lead         a new business: to Bill in #sales-desk
  employee_message sick, late, a problem on route: #employee-calls
  driver_change    a known driver's customer change: checked, read back, sent on their yes (phone.Phone.driver_change)
  handheld_help    how to do something on the handheld or in Alliant, from the Alliant University notes (employees)

  python -m service_changes.phone_agent --setup    make or update both agents, move both numbers' calls to them
  python -m service_changes.phone_agent --undo     calls back to the old way (phone.py, Twilio speech)
"""
import argparse
import json
import os
import re
import time

from .phone import CUSTOMER_RULES, DRIVER_RULES, EMPLOYEE_LINE, EMPLOYEE_RULES, OFFICE_PHONE, PERSONALITY, Turn, digits, \
    office_open

TOOLS = ("caller_info", "customer_request", "new_lead", "employee_message", "driver_change", "handheld_help",
         "verify_owner", "ask_donna", "item_options")
WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "to": "2", "too": "2", "three": "3", "four": "4", "for": "4",
         "five": "5", "six": "6", "seven": "7", "eight": "8", "ate": "8", "nine": "9"}


def owner_phones() -> set[str]:
    """Scott's own numbers (OWNER_PHONES, comma separated): calls from them are offered Harvey mode, after his PIN."""
    return {digits(x) for x in os.environ.get("OWNER_PHONES", "").split(",") if len(digits(x)) == 10}


def trusted(caller: str, staff: dict | None = None) -> dict | None:
    """Who besides a customer this number is, for owner-level texting and calls, before their code phrase:
    Scott (OWNER_PHONES, or his Slack profile phone): {"user", "name": "Scott", "harvey": True}; or a partner from
    TRUSTED_PHONES ("9072403690:U9SGPJP0C:Bill", comma separated): business only, never Harvey mode."""
    d = digits(caller)[-10:]
    boss = [u.strip() for u in os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",") if u.strip()]
    if d in owner_phones() or (staff or {}).get("user") in boss:
        return {"user": boss[0], "name": "Scott", "harvey": True}
    for entry in os.environ.get("TRUSTED_PHONES", "").split(","):
        num, _, rest = entry.strip().partition(":")
        user, _, name = rest.partition(":")
        if len(digits(num)) >= 10 and digits(num)[-10:] == d and user.strip():
            return {"user": user.strip(), "name": name.strip() or "there", "harvey": False}
    return None


def secret(user: str = "") -> str:
    """A code phrase for the phone and texting: Scott's is OWNER_PHONE_SECRET (OWNER_PHONE_PIN works too), anyone
    else's PHONE_SECRET_<their Slack id>. Three words or more ("bar mops in the snow") or 4+ digits; empty when not
    set or too short to be safe."""
    boss = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
    if user and user != boss:
        raw = (os.environ.get(f"PHONE_SECRET_{user}") or "").strip()
    else:
        raw = (os.environ.get("OWNER_PHONE_SECRET") or os.environ.get("OWNER_PHONE_PIN") or "").strip()
    if re.search(r"[a-z]", raw, re.I):
        return raw if len(raw.split()) >= 3 else ""
    return raw if len(spoken_digits(raw)) >= 4 else ""


def secret_matches(said: str, real: str) -> bool:
    """A phrase as speech-to-text writes it: case, punctuation and a slip of a letter or two don't matter."""
    import difflib
    import hmac
    if not real:
        return False
    if not re.search(r"[a-z]", real, re.I):
        return hmac.compare_digest(spoken_digits(said).encode(), spoken_digits(real).encode())
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    a, b = norm(said), norm(real)
    return hmac.compare_digest(a.encode(), b.encode()) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.9


def spoken_digits(said: str) -> str:
    """"one two three four", "12 34", "1-2-3-4" -> "1234"."""
    return "".join(WORDS.get(w, re.sub(r"\D", "", w)) for w in re.findall(r"[a-z]+|\d+", said.lower()))


class PhoneTools:
    """The agents' tools, on top of phone.Phone (its Slack posting, caller lookups and driver-change checks)."""

    def __init__(self, phone):
        self.phone = phone
        self.calls: dict[str, dict] = {}

    def call(self, body: dict) -> dict:
        """This conversation's state, made on first use: like phone.Phone.start, keyed by ElevenLabs' conversation id."""
        conv = str(body.get("conversation_id") or "")[:80] or "unknown"
        now = time.time()
        for k in [k for k, c in self.calls.items() if now - c["at"] > 3600]:
            self.calls.pop(k)
        if conv not in self.calls:
            caller, called = str(body.get("caller") or ""), str(body.get("called") or "")
            line = "employees" if digits(called) == EMPLOYEE_LINE or body.get("line") == "employees" else "customers"
            who = self.phone.caller(caller) if line == "customers" else self.phone.staff(caller)
            person = trusted(caller, self.phone.staff(caller))
            self.calls[conv] = {"line": line, "from": caller, "who": who, "turns": [], "change": None, "posted": 0, "at": now,
                                "scott": bool(person and secret(person["user"])), "person": person, "owner": False,
                                "tries": 0, "conv": conv}
        c = self.calls[conv]
        c["at"] = now
        return c

    def handle(self, name: str, body: dict) -> dict:
        if name not in TOOLS:
            return {"error": "unknown tool"}
        c = self.call(body)
        if name != "caller_info" and self.phone.blocked(c["from"]):
            return {"blocked": True, "note": "This number is blocked: end the call politely."}
        said = str(body.get("summary") or body.get("said") or body.get("question") or "").strip()[:2000]
        # (no transcript lines: on these calls Donna's tools get summaries, not the caller's own words)
        out = getattr(self, name)(c, body, said)
        print(time.strftime("%H:%M"), f"phone agent: {name} ({c['line']}, …{digits(c['from'])[-4:]})", flush=True)
        return out

    def greeting(self, body: dict) -> dict:
        """ElevenLabs asks before it answers a call (its conversation initiation webhook: caller_id, called_number): her
        opening line, by name when the number is known, plus dynamic variables for the agent's prompt."""
        from .context import company_for
        caller, called = str(body.get("caller_id") or ""), str(body.get("called_number") or "")
        line = "employees" if digits(called) == EMPLOYEE_LINE else "customers"
        boss = {u.strip() for u in os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")}
        if self.phone.blocked(caller):
            return {"type": "conversation_initiation_client_data", "dynamic_variables": {"caller_name": "", "caller_known": "no"},
                    "conversation_config_override": {"agent": {"first_message": FIRST[line]}}}
        staff = self.phone.staff(caller)
        person = trusted(caller, staff)
        scott = bool(person and secret(person["user"]))
        who = self.phone.caller(caller) if line == "customers" else staff
        first = ((who or {}).get("name") or "").split()[0] if (who or {}).get("name") else ""
        if first.lower().strip(",.") in ("donna", "snow", "the"):
            first = ""
        if scott:
            hello, first = f"Hi {person['name']}, it's Donna. What's the phrase?", person["name"]
        elif line == "employees":
            hello = f"Hi {first}, it's Donna. What do you need?" if first else FIRST["employees"]
        elif who:
            company = company_for(who["accounts"][0])
            hello = (f"Hi {first}, this is Donna with {company}. What can I do for you?" if first and " at " not in
                     who.get("name", "") else f"Thanks for calling {company}, this is Donna. What can I do for you?")
        else:
            hello = FIRST["customers"]
        print(time.strftime("%H:%M"), f"phone agent: greeting {line} …{digits(caller)[-4:]}"
              + (f" ({person['name']})" if scott else " (known)" if who else ""), flush=True)
        return {"type": "conversation_initiation_client_data",
                "dynamic_variables": {"caller_name": first or "", "caller_known": "yes" if who or scott else "no"},
                "conversation_config_override": {"agent": {"first_message": hello}}}

    # ---- the tools -------------------------------------------------------------------------------------------------

    def caller_info(self, c: dict, body: dict, said: str) -> dict:
        if self.phone.blocked(c["from"]):  # the office blocked this number
            c["who"], c["scott"] = None, False
            return {"blocked": True, "note": "This number is blocked. Don't discuss any account or take any request; say "
                    "the office will be glad to help at 907-222-7738 during business hours, and end the call politely."}
        info = {"line": c["line"], "office_open": office_open(),
                "office_hours": os.environ.get("PHONE_OFFICE_HOURS", "Mon-Fri 8:00-17:00") + " Alaska time"}
        if c["scott"]:
            n = c["person"]["name"]
            info.update(owner_candidate=True, name=n, note=f"This is {n}'s number. Greet them, ask for their secret phrase, "
                        "and call verify_owner with what they say. Until it says verified, treat them like any caller.")
        who = c["who"]
        if c["line"] == "employees":
            info.update(known=bool(who), name=(who or {}).get("name", ""),
                        note="A known employee: they can report customer changes (driver_change)." if who else
                        "Number not on file: take messages only; they can't enter customer changes by phone.")
            return info
        if who:
            from .sms import customer_facts, only
            mine = only(self.phone.alliant(), who["accounts"])
            info.update(verified_customer=True, name=who.get("name", ""),
                        account_facts="\n\n".join(customer_facts(mine, a) for a in who["accounts"]))
        else:
            info.update(verified_customer=False, note="Number not on file: no account details; changes become a "
                                                      "call-back note for the office.")
        return info

    def customer_request(self, c: dict, body: dict, said: str) -> dict:
        turn = Turn(say="", action="request", summary=said, caller_name=str(body.get("caller_name") or ""),
                    account=str(body.get("account") or ""), urgent=str(body.get("urgent") or "").lower() in ("yes", "true", "1"),
                    business=str(body.get("business") or ""), town=str(body.get("town") or ""))
        c["posted"] += 1
        if c["posted"] > 5:
            return {"result": "Already passed several requests from this call; don't send more."}
        import threading
        if c["line"] == "customers" and c["who"]:
            if c.get("asking"):  # their answer to her question: read together with the request
                turn.summary = f"{c['asking']['text']}\nDonna asked: {c['asking']['q']}\nThe customer answered: {said}"
                c["posted"] -= 1
            checked = self.phone.customer_check(c, turn)
            questions = list(dict.fromkeys(checked[1].questions)) if checked else []
            if questions and (c.get("asking") or {}).get("n", 0) < 2:  # the same question a text would get, asked now
                c["asking"] = {"text": turn.summary, "q": " ".join(questions[:2]), "n": (c.get("asking") or {}).get("n", 0) + 1}
                return {"status": "question", "say": c["asking"]["q"]}
            c["asking"] = None
            # the ticket is posted in the background: the caller isn't kept waiting
            threading.Thread(target=self.phone.record, args=(f"agent-{c['posted']}", c, turn), daemon=True).start()
            from .checks import stop_notice
            notice = stop_notice(checked[1]) if checked else ""
            return {"status": "sent", "result": "Sent to the office. They'll get a text the moment it's entered."
                    + (f" Tell them: {notice}" if notice else "")}
        self.phone.office_note(c, turn, verified=False)
        return {"result": "Passed to the office; they'll call back to confirm before changing anything."}

    def new_lead(self, c: dict, body: dict, said: str) -> dict:
        self.phone.post("lead", {"name": str(body.get("caller_name") or ""), "business": str(body.get("business") or ""),
                                 "business_type": "", "town": str(body.get("town") or ""), "size": "", "phone": c["from"],
                                 "email": "", "best_time": str(body.get("best_time") or ""), "current_provider": "",
                                 "needs": said, "company": "sw", "notes": "Came in by phone (Donna, customer line).",
                                 "handed_from": "", "page": "phone", "transcript": said, "created_at": time.time()})
        return {"result": "Sent to Bill, who handles new customers. He'll reach out."}

    def employee_message(self, c: dict, body: dict, said: str) -> dict:
        turn = Turn(say="", action="message", summary=said, caller_name=str(body.get("caller_name") or ""),
                    urgent=bool(body.get("urgent")))
        self.phone.employee_message(c, turn)
        return {"result": "Passed to their supervisor."}

    def driver_change(self, c: dict, body: dict, said: str) -> dict:
        """First call: the change as the driver said it. Later calls: their answer to her question, or yes / no /
        a correction to her read-back. `say` is what to tell them; `status` what happened."""
        if c["line"] != "employees" or not c["who"]:
            return {"status": "not_allowed", "say": "I can't enter changes from this number. I'll pass it to the office.",
                    "note": "Use employee_message instead."}
        if c.get("change"):
            from .desk import NO, YES
            ch = c["change"]
            if ch.get("confirm") and YES.match(said):
                c["change"] = None
                self.phone.send_change(c, ch)
                return {"status": "sent", "say": "Sent. You'll see the readback in the route channel once the office "
                                                 "enters it."}
            if ch.get("confirm") and NO.match(said) and len(said.split()) <= 4:
                c["change"] = None
                return {"status": "cancelled", "say": "Okay, I won't send it."}
            said = (f"{ch['text']}\nCorrection: {said}" if ch.get("confirm") else
                    f"{ch['text']}\nDonna asked: {ch['asked']}\nThe answer: {said}")
        turn = self.phone.driver_change(c, said)
        status = "confirm" if (c.get("change") or {}).get("confirm") else "question" if c.get("change") else "not_a_change"
        if status == "not_a_change":
            self.phone.employee_message(c, Turn(say="", action="message", summary=said,
                                                caller_name=c["who"].get("name", "")))
        return {"status": status, "say": turn.say}

    def verify_owner(self, c: dict, body: dict, said: str) -> dict:
        """Scott's secret phrase or PIN (secret()), only from his own number, three tries a call: caller ID alone can be
        faked."""
        real = secret((c.get("person") or {}).get("user", ""))
        if not c["scott"] or not real or c["tries"] >= 3:
            return {"verified": False, "note": "Not available on this call. Carry on as a normal call."}
        c["tries"] += 1
        if secret_matches(str(body.get("pin") or said), real):
            c["owner"] = True
            if not c["person"]["harvey"]:
                n = c["person"]["name"]
                print(time.strftime("%H:%M"), f"phone agent: {n} verified (business only)", flush=True)
                return {"verified": True, "mode": "business", "note": f"Business mode: {n} is one of the owners. For a "
                        "customer, an account, a balance or a change on any account, call ask_donna with their words and "
                        "give its answer in your own voice. Friendly and businesslike: no Harvey mode, nothing about the "
                        "team or people, Slack or email."}
            print(time.strftime("%H:%M"), "phone agent: Scott verified (Harvey mode)", flush=True)
            return {"verified": True, "note": "Harvey mode: you're Scott's Donna now, exactly like his Donna app. For "
                    "anything about the business, customers, the team and people, Slack, email, balances or a change, "
                    "call ask_donna with his words and tell him the answer in your own voice."}
        return {"verified": False, "note": "That's not it." + (" One more try." if c["tries"] == 2 else
                                                                  " No more tries on this call." if c["tries"] >= 3 else "")}

    def ask_donna(self, c: dict, body: dict, said: str) -> dict:
        """Harvey mode on the phone: the same desk.voice his Donna app uses (lookups, balances, his read on the team,
        changes with her read-back and his yes)."""
        if not c["owner"]:
            return {"answer": "", "note": "Only after verify_owner."}
        import urllib.request
        owner = (c.get("person") or {}).get("user") or os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
        req = urllib.request.Request(f"http://127.0.0.1:{os.environ.get('DESK_VOICE_PORT', '8091')}/voice",
                                     data=json.dumps({"text": said, "convo": "phone-" + c["conv"], "user": owner}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=110) as r:
                return {"answer": r.read().decode("utf-8", "replace")}
        except Exception as e:
            print(time.strftime("%H:%M"), "phone agent: ask_donna failed:", str(e)[:200], flush=True)
            return {"answer": "I can't reach the office data right now. Try me again in a minute."}

    def item_options(self, c: dict, body: dict, said: str) -> dict:
        """"What colors do tablecloths come in?": the catalog's colors and sizes (no prices), on either line."""
        from .checks import options_facts
        from .context import product_notes
        return {"options": options_facts(self.phone.alliant(), said) or product_notes(said)
                or "Nothing by that name in the catalog."}

    def handheld_help(self, c: dict, body: dict, said: str) -> dict:
        helper = getattr(self.phone, "helper", None)
        if c["line"] != "employees" or not helper:
            return {"answer": "", "note": "Not available on this line."}
        how, _ = helper.answer([{"role": "user", "content": said + "\n\n(Answered on a phone call: a few short spoken "
                                 "sentences, steps in order, no lists or formatting, no source line.)"}], None)
        how = re.sub(r"\n*_?Source:.*$", "", how, flags=re.S)
        return {"answer": re.sub(r"[*_`#>]", "", how).strip()[:900]}


# ---- the agents ------------------------------------------------------------------------------------------------------

OWNER_RULES = """
Scott, the owner: if caller_info says owner_candidate, greet him by name and ask for his secret phrase; call verify_owner
with exactly what he says. Until it says verified, he's an ordinary caller (no account or company details). Once verified, everything above
about this line no longer limits you: you're his Donna and he's your Harvey, the best right hand in the business,
direct, confident, a step ahead, a little wit, warm. For anything about the business, customers, the team and people,
Slack, email, balances or a change, call ask_donna with his exact words and give him its answer in your own voice,
never changing a fact or number. A change: ask_donna reads it back; pass his yes, no or correction to ask_donna word for
word. Never say a change is done unless ask_donna said so. Never repeat his secret phrase back.
Bill, the other owner, gets the same greeting and phrase check; when verify_owner says mode "business", be his
friendly, businesslike Donna (no Harvey mode): ask_donna for customers, accounts, balances and changes on any account,
the same read-back and yes. Never repeat his phrase back either.
"""

AGENT_RULES = """
How to work on the phone (you are speaking, not writing):
- At the very start of every call, before anything else, call caller_info. Use what it returns: the caller's name, whether
  they're a verified customer or known employee, their account facts, and whether the office is open.
- Talk like a person: one to three short sentences, one question at a time, no lists, numbers the way people say them.
- When a tool returns `say`, say it in your own warm words without changing any fact, number or item.
- Transfers: only while the office is open (caller_info), with transfer_to_number. When it's closed, take a message.
- End the call with end_call once they're done and you've said goodbye.
"""

CUSTOMER_TOOLS_NOTE = """Your tools on this line:
- customer_request: an existing customer's change or service problem, once you have what the office needs (which item, how
  many, which location, which employee and sizes). summary = office-ready (the account, exactly what to change, quantities).
  If it returns status "question" (which color, more or total...), ask the customer its `say` in your own words, then
  call customer_request again with summary = their answer. Otherwise it tells you whether they'll be texted or called
  back; say so.
- A caller whose number isn't on file (caller_info): before customer_request, get their name, their business and which
  location (the street or town), so the office can match them to the right account in one tap.
- new_lead: a new business. Fill caller_name, business, town, best_time; summary = what they need.
- item_options: when they ask what colors or sizes something comes in, or seem unsure which one they want. Offer a color
  that goes with what they already have (caller_info's account facts), and remember they may just be asking.
"""

EMPLOYEE_TOOLS_NOTE = """Your tools on this line:
- employee_message: sick, late, absent, a problem on route, or anything for a supervisor (summary: who, what, when).
- driver_change: a known employee reporting a customer change. Call it with `said` = the change in their words. It returns
  status and `say`: "question" (ask them that, then call driver_change again with their answer), "confirm" (read it back
  and ask if you should send it; then call driver_change with their yes, no or correction, word for word), "sent",
  "cancelled", "not_a_change" or "not_allowed". Never say a change is sent unless status is "sent".
- handheld_help: how to do something on the handheld or in Alliant mobile. Say the answer simply, step by step.
- item_options: what colors and sizes an item comes in.
"""


def prompt(line: str) -> str:
    office = (f"The office number for transfers is {OFFICE_PHONE}; caller_info says whether it's open. When it's "
              "closed, take a message instead and say someone will call back next business day.")
    if line == "employees":
        rules = EMPLOYEE_RULES.format(office=office) + DRIVER_RULES.format(name="the caller (if caller_info says they're "
                                                                                 "known)").replace("set action change",
                                                                                                   "call driver_change")
        return PERSONALITY + "\n\n" + rules + "\n" + EMPLOYEE_TOOLS_NOTE + OWNER_RULES + AGENT_RULES
    who = ("If caller_info says they're a verified customer, help them with their own accounts using only its account "
           "facts. If not, you can't see or discuss any account: take their name, business, location and request and say "
           "the office will call back to confirm; if they'd like texting, they can text 866-TAP-TEXT, that's 866-827-8398, to get verified.")
    rules = CUSTOMER_RULES.format(who=who, confirm="(customer_request tells you how they'll hear back)", office=office)
    rules = re.sub(r"Actions \(set `action`\):.*", "", rules, flags=re.S)
    rules = rules.replace("account facts given below", "account facts caller_info gives you")
    return PERSONALITY + "\n\n" + rules + "\n" + CUSTOMER_TOOLS_NOTE + OWNER_RULES + AGENT_RULES


def _api(method: str, path: str, body: dict | None = None) -> dict:
    import urllib.error
    import urllib.request
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    req = urllib.request.Request("https://api.elevenlabs.io" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"xi-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"ElevenLabs {method} {path} failed: {e.code} {e.read(600).decode('utf-8', 'replace')}")
    return json.loads(raw or b"{}") if raw.strip() else {}


SAVED = "phone_agents.json"  # the two agents' ids and the numbers' ElevenLabs ids, for updates and --undo
FIRST = {"customers": "Thanks for calling Snow White Linen, this is Donna. How can I help?",
         "employees": "Hi, this is Donna on the employee line. What can I do for you?"}
LABEL = {"customers": "Donna – customer line", "employees": "Donna – employee line"}
FIELDS = {  # tool -> (description, {property: description}); every tool also gets the caller, number and conversation
    "caller_info": ("Call first on every call: who is calling and what they can do.", {}),
    "customer_request": ("An existing customer's change or service problem, or their answer to the question it asked. "
                         "status question: ask them its `say`, then call it again with their answer as summary.",
                         {"summary": "Office-ready: the account, exactly what to change, quantities.",
                          "account": "Their account number if known, else empty.", "caller_name": "The caller's name.",
                          "business": "Their business name, as they said it.",
                          "town": "Which location: the street or town they're calling about.",
                          "urgent": "yes if it can't wait, else no."}),
    "new_lead": ("A new business interested in service: send to Bill.",
                 {"summary": "What they need.", "caller_name": "Their name.", "business": "The business name.",
                  "town": "Their town.", "best_time": "A good time to call back."}),
    "employee_message": ("Sick, late, absent, a problem on route, or anything else for a supervisor.",
                         {"summary": "Who, what, when.", "caller_name": "The employee's name.",
                          "urgent": "yes if it can't wait, else no."}),
    "driver_change": ("A known employee's customer change, their answer to your question, or their yes/no/correction "
                      "to your read-back.", {"said": "Exactly what the caller said."}),
    "handheld_help": ("How to do something on the handheld or in Alliant mobile.", {"question": "Their question."}),
    "item_options": ("What colors and sizes an item comes in, or what an item is for (bar mops, utility towels, apron "
                     "styles, mat sizes and kinds). No prices.",
                     {"question": "What they asked, with the item."}),
    "verify_owner": ("Check Scott's secret phrase, only when caller_info says owner_candidate.",
                     {"pin": "Exactly the words or numbers he said."}),
    "ask_donna": ("Harvey mode only (after verify_owner): anything Scott asks or tells you about the business, people, "
                  "Slack, email, balances, or a change, and his yes or no to a change you read back.",
                  {"question": "Exactly what Scott said."}),
}
LINE_TOOLS = {"customers": ("caller_info", "customer_request", "new_lead", "item_options", "verify_owner", "ask_donna"),
              "employees": ("caller_info", "employee_message", "driver_change", "handheld_help", "item_options",
                            "verify_owner", "ask_donna")}
SYSTEM = {"caller": "system__caller_id", "called": "system__called_number", "conversation_id": "system__conversation_id"}


def _template() -> tuple[dict, dict]:
    """Scott's Donna agent (ELEVENLABS_AGENT_ID) and its ask_donna webhook tool: the phone agents copy its voice, model
    and the exact shape ElevenLabs uses for a webhook tool, so nothing here has to guess ElevenLabs' format."""
    agent = _api("GET", f"/v1/convai/agents/{os.environ.get('ELEVENLABS_AGENT_ID', 'agent_2401m5yb5t1vffc94cb4j5qfr67c')}")
    prompt_cfg = agent["conversation_config"]["agent"]["prompt"]
    tools = list(prompt_cfg.get("tools") or [])
    for tid in prompt_cfg.get("tool_ids") or []:
        t = _api("GET", f"/v1/convai/tools/{tid}")
        tools.append(t.get("tool_config") or t)
    hook = next((t for t in tools if t.get("type") == "webhook"), None)
    if not hook:
        raise SystemExit("Couldn't find the ask_donna webhook tool on the Donna agent to copy.")
    return agent, hook


def _tool(hook: dict, name: str, url: str, token: str) -> dict:
    import copy
    t = copy.deepcopy(hook)
    for k in ("id", "tool_id"):
        t.pop(k, None)
    desc, fields = FIELDS[name]
    t.update(name=name, description=desc)
    api = t["api_schema"]
    # The key rides in the address's path (a header copied from the Donna agent can be one of ElevenLabs' stored secrets,
    # and a ?query can be dropped); the path is sent exactly as written.
    api.update(url=f"{url}/api/phone/{token}/{name}", method="POST")
    api.pop("query_params_schema", None)
    api.pop("path_params_schema", None)
    api["request_headers"] = {}
    body = api.get("request_body_schema") or {"type": "object", "properties": {}}
    props = body.get("properties") or {}
    dyn = next((p for p in props.values() if any(isinstance(v, str) and v == "donna_session" for v in p.values())), None)
    llm = next((p for p in props.values() if p is not dyn), {"type": "string", "description": ""})
    dyn_key = next((k for k, v in (dyn or {}).items() if v == "donna_session"), "dynamic_variable")
    new = {}
    for prop, var in SYSTEM.items():
        p = copy.deepcopy(dyn) if dyn else {"type": "string", "description": ""}
        p[dyn_key] = var
        p["description"] = ""
        new[prop] = p
    for prop, d in fields.items():
        p = copy.deepcopy(llm)
        p.pop(dyn_key, None)
        p.update(type="string", description=d)
        new[prop] = p
    body.update(properties=new, required=list(new))
    api["request_body_schema"] = body
    return t


def _system_tools() -> list[dict]:
    return [{"type": "system", "name": "end_call", "description": "End the call once the caller is done and you've said "
             "goodbye.", "params": {"system_tool_type": "end_call"}},
            {"type": "system", "name": "transfer_to_number", "description": "Put the caller through to the office, only "
             "while caller_info says it's open.", "params": {"system_tool_type": "transfer_to_number", "transfers": [
                 {"phone_number": OFFICE_PHONE, "condition": "The caller wants a person and the office is open."}]}}]


def _agent_body(agent: dict, hook: dict, line: str, url: str, token: str, system: bool) -> dict:
    import copy
    cc = copy.deepcopy(agent["conversation_config"])
    a = cc["agent"]
    a["first_message"] = FIRST[line]
    a.pop("dynamic_variables", None)
    p = a["prompt"]
    p["prompt"] = prompt(line)
    p.pop("tool_ids", None)
    p["tools"] = [_tool(hook, n, url, token) for n in LINE_TOOLS[line]] + (_system_tools() if system else [])
    for k in ("built_in_tools", "knowledge_base", "rag"):
        p.pop(k, None)
    return {"name": LABEL[line], "conversation_config": cc}


def setup(public_url: str, data_dir: str, undo: bool = False) -> None:
    from . import phone
    from .phone import CUSTOMER_LINE
    from .sms import _twilio
    path = os.path.join(data_dir, SAVED)
    saved = json.load(open(path)) if os.path.exists(path) else {"agents": {}, "numbers": {}}
    numbers = {"customers": CUSTOMER_LINE, "employees": EMPLOYEE_LINE}
    if undo:
        for line, pid in list(saved["numbers"].items()):
            _api("DELETE", f"/v1/convai/phone-numbers/{pid}")
            print(f"+1{numbers[line]}: removed from ElevenLabs")
            saved["numbers"].pop(line)
        json.dump(saved, open(path, "w"))
        phone.setup(public_url, data_dir)  # calls back to phone.py (Twilio speech); texting untouched
        return
    token = os.environ.get("PHONE_AGENT_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("Add PHONE_AGENT_TOKEN (24+ characters) to /etc/service-desk.env and deploy first.")
    agent, hook = _template()
    for line in ("customers", "employees"):
        aid = saved["agents"].get(line)
        for system in (True, False):
            body = _agent_body(agent, hook, line, public_url, token, system)
            try:
                if aid:
                    _api("PATCH", f"/v1/convai/agents/{aid}", body)
                else:
                    aid = _api("POST", "/v1/convai/agents/create", body)["agent_id"]
                break
            except SystemExit as e:
                if not system:
                    raise
                print(f"{LABEL[line]}: ElevenLabs didn't take the end-call/transfer tools ({str(e)[:160]}); "
                      "made it without them: add End call and Transfer to number in its Tools.")
        saved["agents"][line] = aid
        print(f"{LABEL[line]}: agent {aid}")
    json.dump(saved, open(path, "w"))
    listed = _api("GET", "/v1/convai/phone-numbers")
    listed = listed if isinstance(listed, list) else listed.get("phone_numbers", [])
    have = {digits(n.get("phone_number", "")): n for n in listed}
    for line, num in numbers.items():
        tw = (_twilio("GET", "IncomingPhoneNumbers.json", {"PhoneNumber": "+1" + num}).get("incoming_phone_numbers") or [{}])[0]
        sms_before = tw.get("sms_url")
        pid = (have.get(num) or {}).get("phone_number_id") or saved["numbers"].get(line)
        if not pid:
            pid = _api("POST", "/v1/convai/phone-numbers", {
                "phone_number": "+1" + num, "label": LABEL[line], "provider": "twilio",
                "sid": os.environ.get("TWILIO_ACCOUNT_SID", ""), "token": os.environ.get("TWILIO_AUTH_TOKEN", "")})["phone_number_id"]
        _api("PATCH", f"/v1/convai/phone-numbers/{pid}", {"agent_id": saved["agents"][line]})
        saved["numbers"][line] = pid
        json.dump(saved, open(path, "w"))
        after = (_twilio("GET", "IncomingPhoneNumbers.json", {"PhoneNumber": "+1" + num}).get("incoming_phone_numbers") or [{}])[0]
        if tw.get("sid") and after.get("sms_url") != sms_before:  # texting must stay with the Service Desk
            _twilio("POST", f"IncomingPhoneNumbers/{tw['sid']}.json", {"SmsUrl": sms_before or "", "SmsMethod": tw.get("sms_method") or "POST"})
            print(f"+1{num}: texting setting put back to {sms_before}")
        print(f"+1{num}: calls now answered by {LABEL[line]} (texts still go to {sms_before or 'the Service Desk'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--data", default=os.environ.get("SERVICE_DESK_DATA", "service_changes/data"))
    ap.add_argument("--public-url", default=os.environ.get("WEBCHAT_PUBLIC_URL", "https://chat.snowwhitelinen.com"))
    args = ap.parse_args()
    if args.setup or args.undo:
        setup(args.public_url.rstrip("/"), args.data, undo=args.undo)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

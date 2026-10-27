"""The sales desk: website chat leads land in #sales-desk with "Give to" buttons. A click looks the business up on the
web (street address, phone, website: what New Account (Guided) does by hand), creates the Salesforce Account and
Opportunity for that person (salesforce.py) and marks the card. Leads are kept as JSON in <data>/web_leads/ so the desk's Slack
listener, which receives the button click, can read what the chat service saved."""
import json
import os
import re
import time

from . import salesforce, todo

KIND = "service_desk"
LOOKUP_MODEL = os.environ.get("SALES_LOOKUP_MODEL", "claude-sonnet-5-5")
FOUND_KEYS = ("street", "city", "state", "zip", "phone", "website")


def find_business(lead: dict, claude=None) -> dict:
    """The business's street address, phone and website from a web search, or {} when not found or unsure."""
    if not lead.get("business"):
        return {}
    try:
        if claude is None:
            import anthropic
            claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SERVICE_DESK_API_KEY"))
        where = f" in {lead['town']}, Alaska" if lead.get("town") else " in Alaska"
        r = claude.messages.create(
            model=LOOKUP_MODEL, max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3,
                    "user_location": {"type": "approximate", "region": "Alaska", "country": "US"}}],
            messages=[{"role": "user", "content":
                       f"Find the business \"{lead['business']}\"{where}"
                       + (f" (a {lead['business_type']})" if lead.get("business_type") else "")
                       + ". Reply with only a JSON object with the keys street, city, state, zip, phone, website. "
                       "Use the business's own listing (its website, Google or Yelp page). Leave a key as an empty "
                       "string when you can't find it or aren't sure it is this business; never guess."}])
        text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print("business lookup failed:", e, flush=True)
        return {}
    return {k: str(data[k]).strip() for k in FOUND_KEYS if isinstance(data.get(k), (str, int)) and str(data[k]).strip()}


def lead_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "web_leads")
    os.makedirs(d, exist_ok=True)
    return d


def save(data_dir: str, lead: dict) -> str:
    lead_id = lead.get("id") or f"{int(time.time() * 1000)}"
    lead["id"] = lead_id
    path = os.path.join(lead_dir(data_dir), lead_id + ".json")
    with open(path + ".tmp", "w") as f:
        json.dump(lead, f)
    os.replace(path + ".tmp", path)
    return lead_id


def load(data_dir: str, lead_id: str) -> dict | None:
    if not lead_id.isdigit():
        return None
    try:
        with open(os.path.join(lead_dir(data_dir), lead_id + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def card_text(lead: dict) -> str:
    company = salesforce.COMPANY.get(lead.get("company"), "")
    quote = lead.get("source") == "quote"
    lines = [f"{'🧾' if quote else '🧲'} *New website {'quote request' if quote else 'lead'} · {company}*",
             f"*{lead.get('business') or 'Business'}*" + "".join(f" · {lead[k]}" for k in ("business_type", "town") if lead.get(k)),
             "Contact: " + " · ".join(x for x in (lead.get("name"), lead.get("phone"), lead.get("email")) if x)]
    if lead.get("handed_from"):
        lines.append(f"_Came in on the {salesforce.COMPANY.get(lead['handed_from'], '')} site, handed over_")
    if lead.get("street"):
        lines.append("Address: " + ", ".join(x for x in (lead["street"], lead.get("town"), lead.get("zip")) if x))
    for label, key in (("Best time", "best_time"), ("Size", "size"), ("Uses now", "current_provider"),
                       ("Contract ends", "contract_end"), ("Needs", "needs" if not quote else ""), ("Notes", "notes")):
        if key and lead.get(key):
            lines.append(f"{label}: {lead[key]}")
    if quote:
        from .catalog import line_text
        lines += ["*Items:*"] + [f"• {line_text(x)}" for x in lead.get("items") or []]
        lines += [f"• Interested in: {i}" for i in lead.get("interests") or []]
        if lead.get("other"):
            lines.append(f"• Something else: {lead['other']}")
    return "\n".join(lines)


def blocks(lead: dict, footer: str = "", short: bool = False) -> list:
    text = todo.summary(card_text(lead), 2) if short else card_text(lead)[:2900]
    b = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if footer:
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    else:
        b.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": f"Give to {name.split()[0]}"}, "action_id": f"sales_give_{key}",
             "value": json.dumps({"lead": lead["id"], "owner": key}), **({"style": "primary"} if key == "bill" else {})}
            for key, (name, _) in salesforce.OWNERS.items()]})
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Creates the Account and Opportunity in Salesforce for that person."}]})
    return b


def post_lead(slack, channel_id: str, data_dir: str, lead: dict) -> None:
    save(data_dir, lead)
    slack.chat_postMessage(channel=channel_id, text="", unfurl_links=False,
                           attachments=[{"color": todo.GOLD, "fallback": todo.plain(card_text(lead))[:150], "blocks": blocks(lead)}],
                           metadata={"event_type": KIND, "event_payload": {"kind": "sales_lead", "lead": lead["id"]}})


def handle_action(slack, payload: dict, data_dir: str, create=salesforce.create_opportunity, find=find_business) -> str | None:
    """A "Give to" click in #sales-desk. Returns what happened, for the log."""
    act = next((a for a in payload.get("actions", []) if a.get("action_id", "").startswith("sales_give_")), None)
    if not act:
        return None
    v = json.loads(act.get("value") or "{}")
    lead, owner = load(data_dir, str(v.get("lead", ""))), salesforce.OWNERS.get(v.get("owner"))
    channel, ts = payload["channel"]["id"], payload["message"]["ts"]
    clicker = payload.get("user", {}).get("name") or payload.get("user", {}).get("username") or "someone"
    if not lead or not owner:
        return "unknown lead"
    if lead.get("sf_id"):
        return "already in Salesforce"
    if not salesforce.configured():
        slack.chat_postEphemeral(channel=channel, user=payload["user"]["id"],
                                 text="Salesforce isn't connected to Donna yet, so nothing was created.")
        return "salesforce not configured"
    if "found" not in lead:
        lead["found"] = find(lead)
        save(data_dir, lead)
    try:
        sf_id, link, note = create(lead, owner[1])
    except Exception as e:
        slack.chat_postEphemeral(channel=channel, user=payload["user"]["id"],
                                 text=f"Salesforce didn't take it: {str(e)[:300]}")
        return f"salesforce error: {e}"
    lead.update(sf_id=sf_id, sf_owner=owner[0], given_by=clicker, given_at=time.time())
    save(data_dir, lead)
    try:  # the full card goes into the thread; the card itself shrinks to its first lines (todo.shrink)
        slack.chat_postMessage(channel=channel, thread_ts=ts, text=card_text(lead), unfurl_links=False,
                               metadata={"event_type": KIND, "event_payload": {"kind": "todo_full"}})
        short = True
    except Exception:
        short = False
    slack.chat_update(channel=channel, ts=ts, text="", attachments=[{"color": todo.GREEN, "fallback": todo.plain(card_text(lead))[:150],
                      "blocks": blocks(lead, short=short, footer=todo.stamp(clicker, f"Given to {owner[0]}") + f" · <{link}|Salesforce Opportunity>"
                                                    + ("" if lead["found"].get("street") or lead.get("street") else " · address not found online, add it in Salesforce")
                                                    + (f" · ⚠️ {note}" if note else ""))}])
    return f"lead {lead['id']} -> {owner[0]} ({sf_id})"

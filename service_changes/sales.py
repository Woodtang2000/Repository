"""The sales desk: website chat leads land in #sales-desk with "Give to" buttons. A click creates the Salesforce Lead
for that person (salesforce.py) and marks the card. Leads are kept as JSON in <data>/web_leads/ so the desk's Slack
listener, which receives the button click, can read what the chat service saved."""
import json
import os
import time

from . import salesforce

KIND = "service_desk"


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
    lines = [f"🧲 *New website lead · {company}*",
             f"*{lead.get('business') or 'Business'}*" + "".join(f" · {lead[k]}" for k in ("business_type", "town") if lead.get(k)),
             "Contact: " + " · ".join(x for x in (lead.get("name"), lead.get("phone"), lead.get("email")) if x)]
    if lead.get("handed_from"):
        lines.append(f"_Came in on the {salesforce.COMPANY.get(lead['handed_from'], '')} site, handed over_")
    for label, key in (("Best time", "best_time"), ("Size", "size"), ("Uses now", "current_provider"), ("Needs", "needs"),
                       ("Notes", "notes")):
        if lead.get(key):
            lines.append(f"{label}: {lead[key]}")
    return "\n".join(lines)


def blocks(lead: dict, footer: str = "") -> list:
    b = [{"type": "section", "text": {"type": "mrkdwn", "text": card_text(lead)[:2900]}}]
    if footer:
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    else:
        b.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": f"Give to {name.split()[0]}"}, "action_id": f"sales_give_{key}",
             "value": json.dumps({"lead": lead["id"], "owner": key}), **({"style": "primary"} if key == "bill" else {})}
            for key, (name, _) in salesforce.OWNERS.items()]})
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Creates the Lead in Salesforce for that person."}]})
    return b


def post_lead(slack, channel_id: str, data_dir: str, lead: dict) -> None:
    save(data_dir, lead)
    slack.chat_postMessage(channel=channel_id, text=card_text(lead), blocks=blocks(lead), unfurl_links=False,
                           metadata={"event_type": KIND, "event_payload": {"kind": "sales_lead", "lead": lead["id"]}})


def handle_action(slack, payload: dict, data_dir: str, create=salesforce.create_lead) -> str | None:
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
                                 text="Salesforce isn't connected to the Service Desk yet, so the Lead wasn't created.")
        return "salesforce not configured"
    try:
        sf_id, link = create(lead, owner[1])
    except Exception as e:
        slack.chat_postEphemeral(channel=channel, user=payload["user"]["id"],
                                 text=f"Salesforce didn't take the Lead: {str(e)[:300]}")
        return f"salesforce error: {e}"
    lead.update(sf_id=sf_id, sf_owner=owner[0], given_by=clicker, given_at=time.time())
    save(data_dir, lead)
    slack.chat_update(channel=channel, ts=ts, text=card_text(lead),
                      blocks=blocks(lead, footer=f"✅ Given to *{owner[0]}* by {clicker} · <{link}|Salesforce Lead>"))
    return f"lead {lead['id']} -> {owner[0]} ({sf_id})"

"""Sales outreach emails sent by Donna in a rep's name (Scott, 11/2: "have Donna send them from Jesse's name").

The Sales Manager agent (Mac mini, ~5:45 AM) writes each rep's "Outreach: Email – <account>" tasks in Salesforce with
the whole email in the task's Comments ("Copy this email into Gmail: To / Subject / body"). Each weekday morning Donna
lists that day's emails for the rep in Slack with ✅ Send and Skip (and Send all). Send re-reads the task (so an edit
in Salesforce is what goes out), emails it through Bird from the rep's name with replies to the rep's own inbox, in the
company layout, then closes the task: Status Completed, Outcome "Done - no answer" (no reply yet; the Sales Manager
plans the next touch from it), and a note on the task saying when Donna sent it.

Bird sends these as marketing mail (its default category): it adds the one-click unsubscribe header and link, and
never sends to anyone who unsubscribed or complained. A contact marked Email Opt Out in Salesforce is never sent.

OUTREACH_REPS: "slack_id|salesforce_user_id|Name|email" entries, comma separated (default: Jesse)."""
import html as _h
import os
import re
import urllib.parse
from datetime import datetime

from .context import ALASKA

DEFAULT_REPS = "UPZG94HPF|0051W000005qDzpQAE|Jesse Tingley|jesse@snowwhitelinen.com"
OUTCOME = "Done - no answer"  # the Task Outcome picklist has no "email sent"; this is the Sales Manager's no-reply-yet


def reps() -> list[dict]:
    out = []
    for item in os.environ.get("OUTREACH_REPS", DEFAULT_REPS).split(","):
        parts = [p.strip() for p in item.split("|")]
        if len(parts) == 4 and all(parts):
            out.append({"slack": parts[0], "sf": parts[1], "name": parts[2], "email": parts[3]})
    return out


def rep_for(slack_id: str = "", sf_id: str = "") -> dict | None:
    return next((r for r in reps() if (slack_id and r["slack"] == slack_id) or (sf_id and r["sf"] == sf_id)), None)


def parse(description: str) -> dict | None:
    """The email the Sales Manager wrote into a task: {"to_name", "to", "subject", "body"}; None if there isn't one."""
    m = re.search(r"Copy this email into Gmail:\s*\n(.*)", description or "", re.S)
    if not m:
        return None
    text = re.split(r"\n\s*When done, set Status", m.group(1))[0].strip()
    to = re.search(r"^To:\s*(.*?)\s*<([^>\s]+@[^>\s]+)>\s*$", text, re.M) or re.search(r"^To:\s*()([^\s<>]+@[^\s<>]+)\s*$", text, re.M)
    subject = re.search(r"^Subject:\s*(.+)$", text, re.M)
    if not to or not subject:
        return None
    body = text[subject.end():].strip()
    return {"to_name": to.group(1).strip(' "'), "to": to.group(2).strip(), "subject": subject.group(1).strip(),
            "body": body} if body else None


def html(body: str, company: str = "Snow White Linen") -> str:
    """A rep's email in the company layout: logo, gold rule, the letter, the rep's signature as written, navy footer.
    Plainer than Donna's (no heading, no picture): it should read as a note from a person."""
    from .bird import BRANDS
    b = BRANDS.get(company, BRANDS["Snow White Linen"])
    base = os.environ.get("WEBCHAT_PUBLIC_URL", "https://chat.snowwhitelinen.com").rstrip("/")
    letter, sep, sig = body.rpartition("\n\nThanks,\n\n")
    if not sep:
        letter, sig = body, ""
    paras = "".join(f'<p style="margin:0 0 14px">{_h.escape(p).replace(chr(10), "<br>")}</p>'
                    for p in letter.split("\n\n") if p.strip())
    sig_html = ""
    if sig:
        lines = sig.split("\n")
        sig_html = (f'<p style="margin:0 0 6px">Thanks,</p><p style="margin:0;font-size:14px;line-height:1.5">'
                    f'<b style="color:{b["navy"]};font-size:15px">{_h.escape(lines[0])}</b><br>'
                    + "<br>".join(f'<span style="color:#5b6770">{_h.escape(ln)}</span>' for ln in lines[1:] if ln.strip())
                    + "</p>")
    return (f'<table role="presentation" width="100%" style="background:#f3f5f6;border-collapse:collapse"><tr><td align="center" '
            f'style="padding:20px 10px"><table role="presentation" width="600" style="max-width:600px;width:100%;background:#ffffff;'
            f'border-collapse:collapse;font:15px/1.55 Arial,sans-serif;color:#1c2226">'
            f'<tr><td style="padding:20px 28px 14px"><img src="{base}/logo/{b["logo"]}.png" height="40" alt="{_h.escape(b["name"])}" '
            f'style="display:block;height:40px;border:0"></td></tr>'
            f'<tr><td style="height:3px;background:{b["gold"]};font-size:0;line-height:0">&nbsp;</td></tr>'
            f'<tr><td style="padding:24px 28px 18px">{paras}{sig_html}</td></tr>'
            f'<tr><td style="padding:14px 28px;background:{b["navy"]};color:#ffffff;font:12px/1.5 Arial,sans-serif">'
            f'<b>{_h.escape(b["name"])}</b> · {_h.escape(b["addr"])}</td></tr></table></td></tr></table>')


def _sf():
    from . import salesforce as sf
    token, instance = sf._token()
    base = f"{instance}/services/data/{sf.API}"
    query = lambda soql: sf._call("GET", f"{base}/query?" + urllib.parse.urlencode({"q": soql}), token).get("records") or []
    patch = lambda obj, rid, body: sf._call("PATCH", f"{base}/sobjects/{obj}/{rid}", token, body)
    return query, patch, sf._q


FIELDS = "Id, Subject, Status, IsClosed, ActivityDate, Description, WhoId, WhatId, What.Name, OwnerId"


def due(rep: dict, today: str | None = None) -> list[dict]:
    """The rep's open outreach email tasks due today or earlier, oldest first, that hold an email Donna can send."""
    query, _, q = _sf()
    today = today or datetime.now(ALASKA).strftime("%Y-%m-%d")
    rows = query(f"SELECT {FIELDS} FROM Task WHERE OwnerId = {q(rep['sf'])} AND IsClosed = false "
                 f"AND Subject LIKE 'Outreach: Email%' AND ActivityDate <= {today} ORDER BY ActivityDate, CreatedDate LIMIT 60")
    return [dict(r, email=parse(r.get("Description") or "")) for r in rows if parse(r.get("Description") or "")]


def send(task_id: str, rep: dict, by: str) -> str:
    """Send one outreach email now and close its task. Returns what happened, one line."""
    from . import bird
    query, patch, q = _sf()
    rows = query(f"SELECT {FIELDS} FROM Task WHERE Id = {q(task_id)} LIMIT 1")
    if not rows:
        return "not found in Salesforce"
    t = rows[0]
    if t.get("IsClosed"):
        return "already closed in Salesforce"
    e = parse(t.get("Description") or "")
    if not e:
        return "no email in the task (its Comments were changed)"
    if t.get("WhoId"):
        c = query(f"SELECT Email, HasOptedOutOfEmail FROM Contact WHERE Id = {q(t['WhoId'])} LIMIT 1")
        if c and c[0].get("HasOptedOutOfEmail"):
            return "not sent: the contact is marked Email Opt Out in Salesforce"
    domain = os.environ.get("EMAIL_FROM", "service@mail.snowwhitelinen.com").split("@")[-1]
    sender = f"{rep['email'].split('@')[0].lower()}@{domain}"
    body = {"from": {"email": sender, "name": f"{rep['name']} – Snow White"}, "to": [e["to"]],
            "reply_to": [rep["email"]], "subject": e["subject"], "text": e["body"], "html": html(e["body"]),
            "category": "marketing", "tags": ["outreach"]}
    bird._call("POST", "/v1/email/messages", body=body, key=f"outreach-{task_id}")
    from . import comms
    comms.log("out", "email", e["to"], f"(sales outreach in {rep['name']}'s name, OK'd by {by})\nSubject: {e['subject']}\n{e['body']}")
    when = datetime.now(ALASKA).strftime("%-m/%-d %-I:%M %p")
    patch("Task", task_id, {"Status": "Completed", "Outcome__c": OUTCOME,
                            "Description": ((t.get("Description") or "") + f"\n\nSent by Donna in {rep['name'].split()[0]}'s "
                                            f"name {when} (OK'd by {by}). Replies go to {rep['email']}.")[:32000]})
    return f"sent to {e['to']}"

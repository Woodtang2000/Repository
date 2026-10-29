"""Bird email API, standard library only: the received emails (a mailbox forwarded to a Bird inbound address) and
sending from the verified sending domain. Needs BIRD_API_KEY (a workspace key, bk_us1_...)."""
import json
import os
import re
import urllib.parse
import urllib.request


def _host() -> str:
    m = re.match(r"b[km]_([a-z]{2}\d+)_", os.environ["BIRD_API_KEY"])
    return f"https://{m.group(1) if m else 'us1'}.platform.bird.com"


def _call(method: str, path: str, params: dict | None = None, body: dict | None = None, key: str = "") -> dict:
    url = _host() + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": f"Bearer {os.environ['BIRD_API_KEY']}", "Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def received(since_iso: str, inbound_address_id: str = "") -> list[dict]:
    """Emails received since `since_iso`, oldest first, each with its plain-text body."""
    params = {"received_after": since_iso, "limit": 100}
    if inbound_address_id:
        params["inbound_address_id"] = inbound_address_id
    msgs = _call("GET", "/v1/email/inbound-messages", params).get("data", [])
    for m in msgs:
        b = _call("GET", f"/v1/email/inbound-messages/{m['id']}/body")
        m["text"] = b.get("text") or re.sub(r"<[^>]+>", " ", b.get("html") or "")
    return sorted(msgs, key=lambda m: m.get("received_at") or "")


def signature(company: str = "Snow White Linen") -> str:
    """Donna's sign-off on every customer email: texting is the quickest way to reach her."""
    return (f"Donna\n{company}\nText me any time: 866-TAP-TEXT (866-827-8398)\nOffice: (907) 222-7738")


def _html(text: str) -> str:
    """The plain text as a simple email, with Donna's picture beside her sign-off (the text stays the fallback)."""
    import html
    body, _, sig = text.rpartition("\n\nDonna\n")
    if not body:
        body, sig = text, ""
    pic = os.environ.get("WEBCHAT_PUBLIC_URL", "https://chat.snowwhitelinen.com").rstrip("/") + "/donna-avatar.png"
    para = "".join(f"<p style=\"margin:0 0 12px\">{html.escape(p).replace(chr(10), '<br>')}</p>"
                   for p in body.split("\n\n") if p.strip())
    lines = ("Donna\n" + sig).split("\n") if sig else []
    sign = ("" if not lines else
            f'<table role="presentation" style="margin-top:16px;border-top:3px solid #f9af10;padding-top:10px"><tr>'
            f'<td style="padding-right:12px;vertical-align:top"><img src="{pic}" width="56" height="56" alt="Donna" '
            f'style="border-radius:50%;display:block"></td><td style="font:14px/1.45 Arial,sans-serif;color:#1c2226">'
            f'<b style="color:#003746">{html.escape(lines[0])}</b><br>'
            + "<br>".join(html.escape(l) for l in lines[1:]) + "</td></tr></table>")
    return (f'<div style="font:15px/1.5 Arial,sans-serif;color:#1c2226;max-width:600px">{para}{sign}</div>')


def send(to: str, subject: str, text: str, in_reply_to: str = "", key: str = "", company: str = "") -> None:
    """From Donna (EMAIL_FROM, e.g. donna@mail.snowwhitelinen.com), named for the customer's company; replies go to
    EMAIL_REPLY_TO (service@), which comes back to the desk."""
    company = company or ("The Laundry Group" if "The Laundry Group" in text[-200:] else "Snow White Linen")
    body = {"from": {"email": os.environ.get("EMAIL_FROM", "service@mail.snowwhitelinen.com"),
                     "name": f"Donna \u2013 {company}"},
            "to": [to], "subject": subject, "text": text, "html": _html(text), "category": "transactional"}
    if os.environ.get("EMAIL_REPLY_TO"):
        body["reply_to"] = [os.environ["EMAIL_REPLY_TO"]]
    if in_reply_to:
        body["headers"] = {"In-Reply-To": in_reply_to, "References": in_reply_to}
    _call("POST", "/v1/email/messages", body=body, key=key)

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


def send(to: str, subject: str, text: str, in_reply_to: str = "", key: str = "") -> None:
    body = {"from": {"email": os.environ.get("EMAIL_FROM", "service@mail.snowwhitelinen.com"),
                     "name": "Snow White Linen"},
            "to": [to], "subject": subject, "text": text, "category": "transactional"}
    if os.environ.get("EMAIL_REPLY_TO"):
        body["reply_to"] = [os.environ["EMAIL_REPLY_TO"]]
    if in_reply_to:
        body["headers"] = {"In-Reply-To": in_reply_to, "References": in_reply_to}
    _call("POST", "/v1/email/messages", body=body, key=key)

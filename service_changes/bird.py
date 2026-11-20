"""Bird email API, standard library only: the received emails (a mailbox forwarded to a Bird inbound address) and
sending from the verified sending domain. Needs BIRD_API_KEY (a workspace key, bk_us1_...)."""
import json
import os
import re
import urllib.parse
import urllib.request


def _host(api_key: str = "") -> str:
    m = re.match(r"b[km]_([a-z]{2}\d+)_", api_key or os.environ["BIRD_API_KEY"])
    return f"https://{m.group(1) if m else 'us1'}.platform.bird.com"


def _call(method: str, path: str, params: dict | None = None, body: dict | None = None, key: str = "",
          api_key: str = "") -> dict:
    """api_key: another Bird key than Donna's (the Sales key for welcome emails)."""
    api_key = api_key or os.environ["BIRD_API_KEY"]
    url = _host(api_key) + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def attachments(message_id: str) -> list[dict]:
    """A received email's attachments: [{id, filename, content_type, size}], metadata only."""
    return _call("GET", f"/v1/email/inbound-messages/{message_id}/attachments").get("data", [])


def attachment(message_id: str, attachment_id: str) -> bytes:
    """One attachment's raw bytes."""
    req = urllib.request.Request(_host() + f"/v1/email/inbound-messages/{message_id}/attachments/{attachment_id}",
                                 headers={"Authorization": f"Bearer {os.environ['BIRD_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


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


def reply_address(company: str = "Snow White Linen") -> str:
    """The one address customers should use (Barry at Lucky Market, 11/16: "Currently showing 3 separate emails.
    Which one?"). Emails go out from a sending address (EMAIL_FROM) but every reply lands here."""
    tlg = company == "The Laundry Group"
    return ((tlg and os.environ.get("EMAIL_REPLY_TO_TLG")) or os.environ.get("EMAIL_REPLY_TO")
            or "service@snowwhitelinen.com").strip()


def signature(company: str = "Snow White Linen") -> str:
    """Donna's sign-off on every customer email: texting is the quickest way to reach her, and the one email address."""
    from .context import office_phone
    return (f"Donna\n{company}\nText me any time: 866-TAP-TEXT (866-827-8398)\nEmail: {reply_address(company)}\n"
            f"Office: {office_phone(company)}")


BRANDS = {  # brand/README.md: the same logo, navy and gold as every other document
    "Snow White Linen": {"logo": "sw", "navy": "#003746", "gold": "#f9af10", "tint": "#eaf0f1",
                         "name": "Snow White Linen Supply", "addr": "700 'I' Street · Anchorage, AK 99501"},
    "The Laundry Group": {"logo": "tlg", "navy": "#003f50", "gold": "#ffb807", "tint": "#eaf0f2",
                          "name": "The Laundry Group", "addr": "300 E 5th Ave · Anchorage, AK 99501"},
}


# A holiday notice gets some holiday flair (Scott, 11/19): a greeting and seasonal art in a band under the logo, in the
# company's own navy and gold ("make the colors of the emails stay our company colors just add some holiday themed flair").
# Each also gets a few words for the holiday (Scott, 11/19): Thanksgiving and Christmas thank them for their business and
# support; New Year's, the 4th of July and Labor Day are fun; Memorial Day is respectful. Email only, never the texts.
THEMES = {  # each holiday's own words, a different one each year so it never reads like a form letter (Scott, 11/19)
    "Thanksgiving": {"greeting": "Happy Thanksgiving", "art": "🍂 🦃 🍁", "closing": "Warm wishes,", "wishes": [
        "Thank you for your business and support this year. From all of us, we wish you and your family a happy "
        "Thanksgiving!",
        "We're thankful for customers like you. Thank you for your business and support, and from our whole team, "
        "happy Thanksgiving to you and your family!",
        "This time of year we count our blessings, and you're one of them. Thank you for your business and support. "
        "Happy Thanksgiving from all of us!"]},
    "Christmas Day": {"greeting": "Merry Christmas", "art": "🎄 ❄️ 🎁", "closing": "Warm wishes,", "wishes": [
        "Thank you for your business and support this year. From all of us, we wish you and your family a merry "
        "Christmas!",
        "From our crew to yours, thank you for a great year together. Wishing you and your family a merry Christmas!",
        "It's been a pleasure working with you this year. Thank you for your business and support, and merry Christmas "
        "to you and yours!"]},
    "New Year's Day": {"greeting": "Happy New Year", "art": "✨ 🎉 ✨", "closing": "Cheers,", "wishes": [
        "Here's to fresh towels and a fresh start! Thank you for a great year, and happy New Year from all of us!",
        "New year, same spotless service. Thanks for being with us, and happy New Year!",
        "We'll be ringing in the new year with a clean slate and clean linens. Thank you for your business, and happy "
        "New Year!"]},
    "Memorial Day": {"greeting": "Honoring those who served", "art": "🇺🇸", "closing": "With gratitude,", "wishes": [
        "Today we remember and honor those who gave their lives serving our country. We hope you have a peaceful "
        "Memorial Day.",
        "We're grateful for the men and women who gave everything for our country. Wishing you a meaningful Memorial "
        "Day."]},
    "the 4th of July": {"greeting": "Happy 4th of July", "art": "🎆 🇺🇸 🎆", "closing": "Cheers,", "wishes": [
        "Enjoy the fireworks, the barbecue and a day off from the laundry. Happy 4th of July from all of us!",
        "Red, white and freshly laundered! Have a safe and happy 4th of July.",
        "Grill something tasty and leave the laundry to us. Happy Independence Day!"]},
    "Labor Day": {"greeting": "Happy Labor Day", "art": "🇺🇸 🏕️", "closing": "Cheers,", "wishes": [
        "You've earned a day off, and so have we. Happy Labor Day!",
        "Here's to the last long weekend of summer. Enjoy it, and happy Labor Day from all of us!",
        "Kick your feet up. The work will still be here Tuesday, and so will we. Happy Labor Day!"]},
}


def wish(theme: str, year: int) -> str:
    """This holiday's words for this year (they rotate, so no two years read the same)."""
    w = THEMES.get(theme, {}).get("wishes") or []
    return w[year % len(w)] if w else ""


def _banner(theme: str, b: dict) -> str:
    t = THEMES.get(theme)
    if not t:
        return ""
    import html
    return (f'<tr><td style="height:3px;background:{b["gold"]};font-size:0;line-height:0">&nbsp;</td></tr>'
            f'<tr><td style="padding:20px 28px;background:{b["navy"]};color:#ffffff;text-align:center;'
            f'font:bold 24px/1.3 Georgia,serif">{html.escape(t["greeting"])}'
            f'<div style="font-size:26px;line-height:1.4;margin-top:6px">{t["art"]}</div></td></tr>'
            f'<tr><td style="height:3px;background:{b["gold"]};font-size:0;line-height:0">&nbsp;</td></tr>')


def _html(text: str, company: str = "Snow White Linen", title: str = "", theme: str = "") -> str:
    """The plain text as a branded email, like the company's other materials: its logo, a gold rule, a navy heading,
    Donna's sign-off card with her picture, and a navy footer. Tables and inline styles only (what mail apps keep);
    the plain text stays the fallback."""
    import html
    from .context import office_phone
    b = BRANDS.get(company, BRANDS["Snow White Linen"])
    base = os.environ.get("WEBCHAT_PUBLIC_URL", "https://chat.snowwhitelinen.com").rstrip("/")
    body, _, sig = text.rpartition("\n\nDonna\n")
    if not body:
        body, sig = text, ""
    closing = THEMES.get(theme, {}).get("closing", "Thank you,")  # a holiday email's words already thank them
    body = re.sub(r"\n\n(?:Thank you|Warm wishes|Cheers|With gratitude),\s*$", "", body)
    paras = [p for p in body.split("\n\n") if p.strip()]

    def block(p: str) -> str:
        lines = p.split("\n")
        if len(lines) > 1 and all(re.match(r"^\s*[-•]", ln) or not ln.strip() or ln.startswith("    ") for ln in lines[1:]):
            head = f'<p style="margin:0 0 6px;font-weight:bold;color:{b["navy"]}">{html.escape(lines[0])}</p>'
            items = "".join(f'<tr><td style="padding:3px 0;border-bottom:1px solid #e3e8eb">{html.escape(ln.strip(" -•"))}</td></tr>'
                            for ln in lines[1:] if ln.strip())
            return head + f'<table role="presentation" width="100%" style="margin:0 0 14px;border-collapse:collapse">{items}</table>'
        if all(re.match(r"^\s*[-•]", ln) for ln in lines):
            items = "".join(f'<tr><td style="padding:6px 10px;background:{b["tint"]};border-left:3px solid {b["gold"]}">'
                            f'{html.escape(ln.strip(" -•"))}</td></tr><tr><td style="height:4px"></td></tr>' for ln in lines)
            return f'<table role="presentation" width="100%" style="margin:0 0 14px;border-collapse:collapse">{items}</table>'
        return f'<p style="margin:0 0 14px">{html.escape(p).replace(chr(10), "<br>")}</p>'

    content = "".join(block(p) for p in paras)
    lines = ("Donna\n" + sig).split("\n") if sig else []
    card = ("" if not lines else
            f'<table role="presentation" style="margin-top:8px;border-collapse:collapse"><tr>'
            f'<td style="padding-right:12px;vertical-align:top"><img src="{base}/donna-avatar.png" width="56" height="56" '
            f'alt="Donna" style="border-radius:50%;display:block;border:2px solid {b["gold"]}"></td>'
            f'<td style="font:14px/1.45 Arial,sans-serif;color:#1c2226;vertical-align:top">'
            f'<b style="color:{b["navy"]};font-size:15px">{html.escape(lines[0])}</b><br>'
            + "<br>".join(html.escape(l) for l in lines[1:]) + "</td></tr></table>")
    title = title[:1].upper() + title[1:]
    heading = (f'<h1 style="margin:0 0 14px;font:bold 20px/1.3 Arial,sans-serif;color:{b["navy"]}">'
               f'{html.escape(title)}</h1>') if title else ""
    return (f'<table role="presentation" width="100%" style="background:#f3f5f6;border-collapse:collapse"><tr><td align="center" '
            f'style="padding:20px 10px"><table role="presentation" width="600" style="max-width:600px;width:100%;background:#ffffff;'
            f'border-collapse:collapse;font:15px/1.55 Arial,sans-serif;color:#1c2226">'
            f'<tr><td style="padding:20px 28px 14px"><img src="{base}/logo/{b["logo"]}.png" height="44" alt="{html.escape(b["name"])}" '
            f'style="display:block;height:44px;border:0"></td></tr>'
            + (_banner(theme, b) or f'<tr><td style="height:3px;background:{b["gold"]};font-size:0;line-height:0">&nbsp;</td></tr>')
            + f'<tr><td style="padding:24px 28px 8px">{heading}{content}<p style="margin:0 0 6px">{closing}</p>{card}</td></tr>'
            f'<tr><td style="padding:16px 28px;background:{b["navy"]};color:#ffffff;font:12px/1.5 Arial,sans-serif">'
            f'<b>{html.escape(b["name"])}</b> · {html.escape(b["addr"])}<br>'
            f'Text us any time: <span style="color:{b["gold"]}">866-TAP-TEXT (866-827-8398)</span> · Office {office_phone(company)}'
            f'</td></tr></table></td></tr></table>')


def send(to: str, subject: str, text: str, in_reply_to: str = "", key: str = "", company: str = "",
         attachments: list[dict] | None = None, log: bool = True, theme: str = "") -> None:
    """From Donna (EMAIL_FROM, e.g. donna@mail.snowwhitelinen.com), named for the customer's company; replies go to
    EMAIL_REPLY_TO (service@), which comes back to the desk. The Laundry Group's customers: EMAIL_FROM_TLG and
    EMAIL_REPLY_TO_TLG when set (donna@mail.thelaundrygroup.com, donna@thelaundrygroup.com), else the same as above."""
    company = company or ("The Laundry Group" if "The Laundry Group" in text[-200:] else "Snow White Linen")
    tlg = company == "The Laundry Group"  # its customers hear from Donna at The Laundry Group's own address, once set
    sender = (tlg and os.environ.get("EMAIL_FROM_TLG")) or os.environ.get("EMAIL_FROM", "service@mail.snowwhitelinen.com")
    reply_to = (tlg and os.environ.get("EMAIL_REPLY_TO_TLG")) or os.environ.get("EMAIL_REPLY_TO")
    body = {"from": {"email": sender, "name": f"Donna \u2013 {company}"},
            "to": [to], "subject": subject, "text": text, "category": "transactional",
            "html": _html(text, company, re.sub(r"^(?:Re:\s*)?(?:Snow White Linen|The Laundry Group):\s*", "", subject).strip()
                          if not subject.lower().startswith("re:") else "", theme=theme)}
    if reply_to:
        body["reply_to"] = [reply_to]
    if in_reply_to:
        body["headers"] = {"In-Reply-To": in_reply_to, "References": in_reply_to}
    if attachments:  # [{"filename": ..., "content": bytes}]
        import base64
        body["attachments"] = [{"filename": a["filename"], "content": base64.b64encode(a["content"]).decode()}
                               for a in attachments[:5]]
    _call("POST", "/v1/email/messages", body=body, key=key)
    if not log:  # a notice to many customers: one summary line in #customer-messages instead (announce.py)
        return
    from . import comms
    comms.log("out", "email", to, f"Subject: {subject}\n{text}" + (f"\n(attached: {', '.join(a['filename'] for a in attachments[:5])})"
                                                                   if attachments else ""))

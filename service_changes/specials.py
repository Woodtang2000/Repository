"""A special invoice Donna made, to the customer: one email from Donna with the delivery ticket PDF attached ("your
special delivery is set up, here it is"), in place of the usual change-confirmation email.

Two halves meet here, in either order, through <data>/specials/: the PDF arrives by Alliant's one-time email
(email_desk.special_pdf saves it as S0148777.pdf) and her workstation's report names the invoice number and the
account (alliant_jobs.result saves S0148777.json). Whichever comes second sends the email, once. Goes to the billing
contact on the account's Alliant card; not for accounts held in STOP_EMAILS_HOLD, or with CUSTOMER_NOTICES=off.
"""
import json
import os
import re

DIR = "specials"


def _dir(data_dir: str) -> str:
    path = os.path.join(data_dir, DIR)
    os.makedirs(path, exist_ok=True)
    return path


def save_pdf(data_dir: str, number: str, pdf: bytes) -> None:
    with open(os.path.join(_dir(data_dir), f"{number}.pdf"), "wb") as f:
        f.write(pdf)


def save_job(data_dir: str, number: str, account: str, what: str) -> None:
    path = os.path.join(_dir(data_dir), f"{number}.json")
    j = json.load(open(path)) if os.path.exists(path) else {}
    j.update(account=account, what=what)
    with open(path, "w") as f:
        json.dump(j, f)


def set_driver_post(data_dir: str, number: str, channel: str, ts: str) -> None:
    """Where the route's driver was told about this special (alliant_jobs.tell_driver): its PDF goes in that thread."""
    path = os.path.join(_dir(data_dir), f"{number}.json")
    j = json.load(open(path)) if os.path.exists(path) else {}
    j.update(driver_channel=channel, driver_ts=ts)
    with open(path, "w") as f:
        json.dump(j, f)


def share_with_driver(data_dir: str, slack, number: str) -> str:
    """The delivery ticket PDF in the driver's thread for this special (no paper), once both are in, once."""
    d = _dir(data_dir)
    pdf, job, done = (os.path.join(d, f"{number}{x}") for x in (".pdf", ".json", ".driver"))
    if os.path.exists(done) or not (os.path.exists(pdf) and os.path.exists(job)):
        return ""
    j = json.load(open(job))
    if not j.get("driver_channel"):
        return ""
    slack.files_upload_v2(channel=j["driver_channel"], thread_ts=j.get("driver_ts"), content=open(pdf, "rb").read(),
                          filename=f"{number}.pdf", title=f"Delivery ticket {number}")
    open(done, "w").write(j["driver_channel"])
    return "pdf in the driver's thread"


def try_send(data_dir: str, alliant, number: str) -> str:
    """The customer email, if both halves are here and it hasn't gone yet."""
    d = _dir(data_dir)
    pdf, job, done = (os.path.join(d, f"{number}{x}") for x in (".pdf", ".json", ".sent"))
    if os.path.exists(done) or not (os.path.exists(pdf) and os.path.exists(job)):
        return ""
    j = json.load(open(job))
    acct = j.get("account", "")
    held = {x.strip() for x in os.environ.get("STOP_EMAILS_HOLD", "").split(",") if x.strip()}
    if os.environ.get("CUSTOMER_NOTICES", "on").lower() == "off" or acct.split("-")[0] in held:
        open(done, "w").write("held")
        return "held"
    card = getattr(alliant, "cards", {}).get(acct, {})
    from .context import customer_emails  # a sub-account (Costco, Safeway): its own linked contacts only
    to = customer_emails(alliant, acct, data_dir)
    if not to:
        open(done, "w").write("no email on the card")
        return "no email"
    from . import bird
    from .context import company_for
    company = company_for(acct)
    name = next((c.name for c in getattr(alliant, "customers", []) if c.account == acct), acct).title()
    what = re.sub(r"^✅\s*\S+\s+.*?–\s*", "", j.get("what") or "").strip() or "your special delivery"
    body = (f"Hello,\n\nYour special delivery for {name} is set up: {what}. The delivery ticket is attached "
            f"(invoice {number}).\n\nIf anything needs to change, just reply to this email or text me at 866-TAP-TEXT "
            f"(866-827-8398).\n\nThank you,\n\n" + bird.signature(company))
    data = open(pdf, "rb").read()
    for e in to:
        bird.send(e, f"{company}: your special delivery for {name}", body, company=company, key=f"special/{number}/{e}",
                  attachments=[{"filename": f"{number}.pdf", "content": data}])
    open(done, "w").write(", ".join(to))
    return "sent to " + ", ".join(to)

"""Donna's side of the Sales Handoff protocol (deploy/sales_handoff_protocol.md, v3.1): new accounts and locations
entered in Alliant from #new-accounts.

- Listens only in #new-accounts (NEW_ACCOUNTS_CHANNEL) and acts only on messages from the Sales Handoff bot
  (SALES_HANDOFF_BOT_ID). Until that ID is set, nothing is acted on. Everything else there is ignored and logged.
- An alliant_entry_request: checked for the v3.1 required fields (missing ones -> `waiting`, saying which), queued for
  her Alliant workstation as one job with exact instructions (house rules applied, every field not in the request left
  at Alliant's default). needs_stock_ok -> prepared, `waiting`, queued only when the stock_ok message comes.
- When the workstation reports it entered: read back live (alliant_live "billing"), the invoice check (the contract's
  expected 4-week invoice against Alliant's items × price × deliveries), then the result in the thread: `entered`,
  `waiting` (with every difference) or `failed`.
- A request_id already handled is never entered again: the earlier result is posted again.
- alliant_wearer_request (phase 2) and welcome_email_request (Bird, the Sales key) likewise.
Prices and contract details only ever go in that thread.
"""
import json
import os
import re
import threading
import time
from datetime import datetime

from .context import ALASKA

CHANNEL = os.environ.get("NEW_ACCOUNTS_CHANNEL", "C0CFF0X9DMF")
VERSION = "3.1"
REQUIRED = ("request_id", "company", "account_name", "location_name", "service_address", "location_contact",
            "invoice_email", "salesperson", "contract", "install_date", "route", "delivery_days", "rates", "lines",
            "expected_invoice")
LINE_REQUIRED = ("sku", "description", "billing_basis", "quantity", "unit_price", "frequency")
FREQ_DELIVERIES = {"weekly": 4, "eow": 2, "e4w": 1, "e12w": 1 / 3}  # deliveries in a 4-week period, per the protocol
GARMENT_SKU = re.compile(r"^2-")  # Snow White's garment SKUs start 2- (chef coats 2-0413WH, pants 2-2020WH...)


def bot_id() -> str:
    return os.environ.get("SALES_HANDOFF_BOT_ID", "").strip()


def from_sales_handoff(ev: dict) -> bool:
    b = bot_id()
    return bool(b) and b in (ev.get("user"), ev.get("bot_id"), (ev.get("bot_profile") or {}).get("id"))


def parse(text: str) -> dict | None:
    """The message's fenced JSON block (the protocol's only machine part)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def missing(req: dict) -> list[str]:
    out = [k for k in REQUIRED if req.get(k) in (None, "", [], {})]
    for i, ln in enumerate(req.get("lines") or []):
        out += [f"lines[{i}].{k}" for k in LINE_REQUIRED if ln.get(k) in (None, "")]
        if str(ln.get("frequency", "")).lower() not in FREQ_DELIVERIES and not re.fullmatch(
                r"[0-9FYX]|B(?:[1-9]|1[0-2])", str(ln.get("frequency", ""))):
            out.append(f"lines[{i}].frequency ({ln.get('frequency')!r} isn't weekly, EOW, E4W, E12W or an Alliant code)")
        if str(ln.get("billing_basis", "")).title() not in ("Usage", "Inventory"):
            out.append(f"lines[{i}].billing_basis ({ln.get('billing_basis')!r}: Usage or Inventory)")
    if req.get("company") not in ("SW", "TLG"):
        out.append("company (SW or TLG)")
    return out


_SKUS: dict = {}


def is_dondees(req: dict) -> bool:
    names = " ".join(str(req.get(k) or "") for k in ("company", "account_name", "location_name")).lower()
    return bool(re.search(r"\bdondee", names.replace("\u2019", "'")))


def is_garment(sku: str, data_dir: str | None = None) -> bool:
    """From Alliant's own SKU list (skus.csv: item_type "garment"); a SKU not in it: Snow White's 2- numbering."""
    import csv
    d = data_dir or os.environ.get("SERVICE_DESK_DATA", os.path.join(os.path.dirname(__file__), "data"))
    if d not in _SKUS:
        try:
            with open(os.path.join(d, "skus.csv"), newline="", encoding="utf-8-sig") as f:
                _SKUS[d] = {r.get("sku", "").strip().upper(): (r.get("item_type") or "").strip().lower() for r in csv.DictReader(f)}
        except OSError:
            _SKUS[d] = {}
    kind = _SKUS[d].get((sku or "").strip().upper())
    return kind == "garment" if kind is not None else bool(GARMENT_SKU.match(sku or ""))


def price_type(line: dict) -> int:
    """House rule: inventory 14 garments / 21 other; usage 9 garments / 8 other. The SKU decides garment."""
    g = is_garment(line.get("sku", ""))
    return (14 if g else 21) if str(line.get("billing_basis", "")).title() == "Inventory" else (9 if g else 8)


def deliveries(code: str, days: int = 1) -> float:
    """Deliveries in 4 weeks for an Alliant frequency code (house rules list)."""
    c = (code or "").strip().upper()
    if c == "7":
        return 4.0
    if c == "8":
        return 4.0 * max(days, 2)
    if c in ("5", "6"):
        return 2.0
    if c in ("1", "2", "3", "4"):
        return 1.0
    if re.fullmatch(r"B(?:[1-9]|1[0-2])", c):
        return 1 / 3
    return 0.0  # 0 no delivery, 9 first only, X stopped, F, Y


def instructions(req: dict) -> str:
    """The workstation job: exact, plain steps for Alliant with the house rules applied."""
    a = req.get("service_address") or {}
    c = req.get("location_contact") or {}
    opt = req.get("optional") or {}
    branch = 1 if req["company"] == "SW" else 2
    existing = req.get("existing_alliant_account")
    lines = []
    for ln in req["lines"]:
        f = str(ln["frequency"])
        code = {"weekly": "7"}.get(f.lower(), f if not f.lower() in FREQ_DELIVERIES else f"{f.upper()} (pick the week "
                                   "code that fits the route's cycle: EOW 5 or 6, E4W 1-4, E12W B1-B12)")
        inv = ln["quantity"]
        auto = ln.get("autocount") or (ln["quantity"] if str(ln["billing_basis"]).title() == "Usage" else max(1, round(ln["quantity"] / 2)))
        lines.append(f"- {ln['sku']} {ln['description']}: price {ln['unit_price']}, price type {price_type(ln)}, "
                     f"starting inventory {inv}, autocount {auto}, frequency {code}")
    out = [
        "NEW ACCOUNT ENTRY from #new-accounts (Sales Handoff protocol v3.1). Follow your rules section \"New accounts\".",
        f"Request: {req['request_id']} · requested by {req.get('requested_by', '')}",
        (f"Add a NEW LOCATION (customer) under existing account {existing}, branch {branch}."
         if existing else f"Create a NEW ACCOUNT (next free number: {'6000s' if branch == 2 else '1000-9999, not the 6000s'}) "
                          f"and its first customer, branch {branch}."),
        f"Customer number: {opt.get('customer_number') or 'next free (00000 for a new account, else the next by 1)'}",
        f"Account name: {req['account_name']} · Location name: {req['location_name']}",
        f"Service address: {a.get('line1', '')} {a.get('line2', '')}, {a.get('city', '')} {a.get('state', '')} {a.get('zip', '')}",
        f"Location contact: {c.get('name', '')} · {c.get('phone', '')} · {c.get('email', '')}",
        f"Invoice email (contact with Email Invoices checked): {req['invoice_email']}",
        "Payment Charge, Net 30, statements by period, account locked with Copy New SKUs to Customers. "
        "Tax: leave for the office. Templates and customer groups: none.",
        f"Salesperson: {req['salesperson']}",
        f"Contract: number {req['contract'].get('number')}, start {req['contract'].get('start_date')}, "
        f"{req['contract'].get('term_months')} months" + (f"; {json.dumps(opt['contract_extra'])}" if opt.get("contract_extra") else ""),
        f"Route {req['route']}, delivery days {', '.join(req['delivery_days'])}, install {req['install_date']}. "
        "Place the stop in the route's sequence by address.",
        f"Charges (account and customer charge settings): service {req['rates'].get('service_charge_pct')}%, "
        f"inventory {req['rates'].get('inventory_charge_pct')}%, fuel {req['rates'].get('fuel_charge')} per delivery",
        "Items (account catalog, then customer items, starting inventory by a Transactions entry):", *lines,
    ]
    extras = {k: v for k, v in opt.items() if v not in (None, "", [], {}) and k not in ("customer_number", "contract_extra")}
    if extras:
        out.append("Also on the contract (enter these; everything else stays at Alliant's default): " + json.dumps(extras))
    else:
        out.append("Everything not listed here stays at Alliant's default.")
    out.append("Report entered with a note that starts: customer_number=<acct-branch-cust>; stop_sequence=<n>; "
               "codes=<sku>:<frequency code>,... (the codes you used for every line). Anything you couldn't enter: "
               "needs_person with what and why. Never type a price, price type or charge that isn't above.")
    return "\n".join(out)


class NewAccounts:
    def __init__(self, desk):
        self.desk = desk
        self.path = os.path.join(desk.data_dir, "new_accounts.json")
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            return json.load(open(self.path))
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def post(self, thread: str, line: str, payload: dict) -> None:
        self.desk.slack.chat_postMessage(channel=CHANNEL, thread_ts=thread, unfurl_links=False,
                                         text=f"{line}\n```json\n{json.dumps(payload, indent=1)}\n```")

    def result(self, req: dict, thread: str, status: str, notes: str = "", **extra) -> dict:
        r = {"type": "alliant_entry_result", "version": VERSION, "request_id": req.get("request_id"), "status": status,
             "customer_number": extra.pop("customer_number", ""), "account_number": extra.pop("account_number", ""),
             "route": req.get("route"), "stop_sequence": extra.pop("stop_sequence", None),
             "delivery_days": req.get("delivery_days"), "install_date": req.get("install_date"),
             "codes_chosen": extra.pop("codes_chosen", []), "invoice_check": extra.pop("invoice_check", None),
             "verified_live": extra.pop("verified_live", False), "notes": notes, **extra}
        line = {"entered": f"Entered in Alliant: {r['customer_number']}, route {r['route']}, {', '.join(r['delivery_days'] or [])}, "
                           f"install {r['install_date']}. Verified live, projected invoice matches the contract.",
                "waiting": f"Waiting: {notes}", "failed": f"Couldn't enter it: {notes}"}[status]
        self.post(thread, line, r)
        with self.lock:
            data = self.load()
            data.setdefault(req["request_id"], {}).update(result=r, thread=thread, status=status)
            self.save(data)
        return r

    # ---- incoming --------------------------------------------------------------------------------------------

    def on_message(self, ev: dict) -> str:
        if ev.get("channel") != CHANNEL or ev.get("type") != "message" or ev.get("subtype") in ("message_changed", "message_deleted"):
            return ""
        if not from_sales_handoff(ev):
            if ev.get("user") != getattr(self.desk, "me", None):
                print(time.strftime("%H:%M"), "new-accounts: ignored a message not from Sales Handoff", flush=True)
            return "ignored"
        msg = parse(ev.get("text", ""))
        if not msg:
            return "no JSON"
        thread = ev.get("thread_ts") or ev["ts"]
        kind = msg.get("type")
        if kind == "alliant_entry_request":
            return self.entry(msg, thread)
        if kind == "stock_ok":
            return self.stock_ok(msg, thread)
        if kind == "alliant_wearer_request":
            return self.wearers(msg, thread)
        if kind == "welcome_email_request":
            return self.welcome(msg, thread)
        return f"not for Donna: {kind}"

    def entry(self, req: dict, thread: str) -> str:
        rid = req.get("request_id", "")
        known = self.load().get(rid)
        if known and known.get("result"):  # repeats are never entered twice
            self.post(thread, "Already handled; here is the result again.", known["result"])
            return "repeat"
        if is_dondees(req):  # Dondee's isn't in Alliant (Scott, 11/10); Sales Handoff never sends it, this is the backstop
            self.result(req, thread, "failed", "Dondee's Laundry is not in Alliant; nothing entered")
            return "failed: Dondee's"
        gaps = missing(req)
        if gaps:
            self.result(req, thread, "waiting", "missing or unclear: " + "; ".join(gaps))
            return "waiting: missing"
        with self.lock:
            data = self.load()
            data[rid] = {"req": req, "thread": thread, "status": "prepared", "at": time.time()}
            self.save(data)
        if req.get("needs_stock_ok"):
            self.result(req, thread, "waiting", "prepared; waiting for Ana's stock OK before saving")
            return "waiting: stock"
        return self.queue(rid)

    def stock_ok(self, msg: dict, thread: str) -> str:
        rid = msg.get("request_id", "")
        with self.lock:
            data = self.load()
            rec = data.get(rid)
            if not rec or rec.get("status") not in ("prepared", "waiting"):
                return "stock OK for nothing waiting"
            if msg.get("install_date"):
                rec["req"]["install_date"] = msg["install_date"]
            self.save(data)
        return self.queue(rid)

    def queue(self, rid: str) -> str:
        with self.lock:
            data = self.load()
            rec = data[rid]
            req = rec["req"]
            jid = self.desk.jobs().custom_job("new_account", instructions(req),
                                              {"request_id": rid, "account": req.get("existing_alliant_account") or ""})
            rec.update(status="queued", job=jid)
            self.save(data)
        return f"queued {jid}"

    # ---- the workstation finished: read back, invoice check, result ----------------------------------------------

    def job_done(self, job: dict, question: str = "") -> str:
        rid = job.get("request_id", "")
        rec = self.load().get(rid) or {}
        req, thread = rec.get("req") or {"request_id": rid}, rec.get("thread")
        if not thread:
            return "no thread"
        if job.get("kind") == "new_wearers":
            st = "entered" if job["status"] == "entered" else "waiting" if job["status"] == "ask" else "failed"
            self.result(req, thread, st, (question + " " if question else "") + (job.get("note") or "")[:1500],
                        customer_number=req.get("customer_number", ""))
            return f"wearers {st}"
        if job["status"] != "entered":
            st = "waiting" if job["status"] == "ask" else "failed"
            self.result(req, thread, st, ((question + " ") if question else "") + (job.get("note") or "")[:1500])
            return st
        note = job.get("note") or ""
        cust = (re.search(r"customer_number\s*=\s*(\d{1,6}-[12]-\w{1,8})", note) or [None, ""])[1]
        seq = re.search(r"stop_sequence\s*=\s*(\d+)", note)
        codes = [{"sku": s, "frequency_code": c} for s, c in re.findall(r"([\w-]+)\s*:\s*(B?\d{1,2}|[FYX])\b",
                                                                        (re.search(r"codes\s*=\s*([^;\n]+)", note) or [None, ""])[1])]
        if not cust:
            self.result(req, thread, "waiting", "entered on the workstation, but its report has no customer number; a "
                                                "person please check: " + note[:600])
            return "no customer number"
        check = self.invoice_check(req, cust)
        ok = check.get("ok")
        self.result(req, thread, "entered" if ok else "waiting",
                    "" if ok else check.get("why", "the projected invoice differs from the contract"),
                    customer_number=cust, account_number=cust.split("-")[0],
                    stop_sequence=int(seq.group(1)) if seq else None,
                    codes_chosen=[x for x in codes if str(next((l.get("frequency") for l in req.get("lines", [])
                                                                 if l.get("sku") == x["sku"]), "")).upper() in ("EOW", "E4W", "E12W")],
                    invoice_check={k: v for k, v in check.items() if k not in ("ok", "why")},
                    verified_live=check.get("live", False))
        return "entered" if ok else "waiting: invoice check"

    def invoice_check(self, req: dict, cust: str) -> dict:
        """The contract's expected 4-week invoice (from Sales Handoff) against Alliant, live: each item's price ×
        quantity × deliveries in 4 weeks, by its frequency code. Charges (service %, inventory %, fuel) are compared
        once their Alliant fields are mapped; until then they're listed as not checked."""
        from . import alliant_live
        r = alliant_live.lookup("billing", self.desk.data_dir, fresh=True, account=cust)
        if r["source"] != "live":
            return {"ok": False, "live": False, "why": f"couldn't read Alliant live ({r['note']}); a person please check"}
        rows = {x.get("sku"): x for x in r["rows"]}
        exp = req.get("expected_invoice") or {}
        diffs, total = [], 0.0
        for ln in exp.get("lines") or []:
            row = rows.get(ln.get("sku"))
            if not row:
                diffs.append({"what": f"line {ln.get('sku')}", "expected": ln.get("amount"), "alliant": "not on the location"})
                continue
            days = sum(1 for d in ("d_mon", "d_tue", "d_wed", "d_thu", "d_fri", "d_sat", "d_sun") if row.get(d))
            basis = next((l for l in req.get("lines", []) if l.get("sku") == ln.get("sku")), {})
            qty = float(row.get("inventory") or 0) if str(basis.get("billing_basis", "")).title() == "Inventory" \
                else float(row.get("autocount") or 0)
            amount = round(qty * float(row.get("unit_price") or 0) * deliveries(str(row.get("frequency", "")), days), 2)
            total += amount
            if abs(amount - float(ln.get("amount") or 0)) > 1.00:
                diffs.append({"what": f"line {ln.get('sku')}", "expected": ln.get("amount"), "alliant": amount})
            want = price_type(basis) if basis else None
            if want is not None and str(row.get("price_type", "")).split(".")[0] != str(want):
                diffs.append({"what": f"price type {ln.get('sku')}", "expected": want, "alliant": row.get("price_type")})
        lines_expected = round(sum(float(l.get("amount") or 0) for l in exp.get("lines") or []), 2)
        out = {"live": True, "expected_lines_total": lines_expected, "alliant_lines_total": round(total, 2),
               "expected_total": exp.get("total"), "differences": diffs,
               "not_checked": ["service_charge", "inventory_charge", "fuel"]}
        out["ok"] = not diffs and abs(lines_expected - total) <= 1.00
        if not out["ok"]:
            out["why"] = f"{len(diffs)} difference(s) between the contract and Alliant; fix before install day"
        return out

    # ---- phase 2 and the welcome email ---------------------------------------------------------------------------

    def wearers(self, msg: dict, thread: str) -> str:
        rid = msg.get("request_id", "")
        known = self.load().get(rid)
        if known and known.get("result"):
            self.post(thread, "Already handled; here is the result again.", known["result"])
            return "repeat"
        cust = msg.get("customer_number", "")
        if not re.fullmatch(r"\d{1,6}-[12]-\w{1,8}", cust or "") or not msg.get("wearers"):
            self.result({"request_id": rid}, thread, "waiting", "missing customer_number or wearers")
            return "waiting"
        text = ["NEW WEARERS from #new-accounts (phase 2). Follow your rules section \"New accounts\".",
                f"Customer {cust}. Garment prices come from the account catalog: never type one.",
                "For each wearer: add the wearer (next wearer number unless one is given), then each garment with its "
                "size and changes (quantity), embroidery and emblem as given."]
        for w in msg["wearers"]:
            text.append(f"- {w.get('name')}" + "".join(f" · {k} {w[k]}" for k in ("wearer_number", "locker", "department") if w.get(k)))
            for g in w.get("garments") or []:
                size = g.get("size") or "x".join(x for x in (g.get("size_a"), g.get("size_b")) if x)
                text.append(f"    {g.get('sku')} size {size}, {g.get('changes')} changes, embroidery {g.get('embroidery') or 'none'}, "
                            f"emblem {g.get('emblem_code') or 'none'}")
        with self.lock:
            data = self.load()
            data[rid] = {"req": {"request_id": rid, "customer_number": cust}, "thread": thread, "status": "queued"}
            data[rid]["job"] = self.desk.jobs().custom_job("new_wearers", "\n".join(text),
                                                           {"request_id": rid, "account": cust})
            self.save(data)
        return "queued wearers"

    def welcome(self, msg: dict, thread: str) -> str:
        rid = msg.get("request_id", "")
        known = self.load().get(rid)
        if known and known.get("result"):
            self.post(thread, "Already sent; here is the result again.", known["result"])
            return "repeat"
        to = (msg.get("to") or {})
        res = {"type": "welcome_email_result", "version": VERSION, "request_id": rid, "status": "failed",
               "bird_message_id": "", "notes": ""}
        key = os.environ.get("BIRD_SALES_API_KEY", "").strip()
        if not key:
            res["notes"] = "the Sales Bird key (BIRD_SALES_API_KEY) isn't set up on Donna's server yet"
        elif "@" not in (to.get("email") or ""):
            res["notes"] = "no signer email"
        else:
            try:
                res["bird_message_id"] = send_welcome(msg, key)
                res["status"] = "sent"
            except Exception as e:
                res["notes"] = f"Bird didn't take it: {str(e)[:200]}"
        self.post(thread, "Welcome email sent." if res["status"] == "sent" else f"Welcome email not sent: {res['notes']}", res)
        if res["status"] == "sent":
            with self.lock:
                data = self.load()
                data[rid] = {"result": res, "thread": thread, "status": "sent"}
                self.save(data)
        return res["status"]


def welcome_text(msg: dict) -> tuple[str, str, str]:
    """(company, subject, text): the welcome, install day, what happens then, how to reach us. No prices."""
    from .context import office_phone
    company = "The Laundry Group" if msg.get("company") == "TLG" else "Snow White Linen"
    name = ((msg.get("to") or {}).get("name") or "").split()[0] if (msg.get("to") or {}).get("name") else ""
    try:
        day = datetime.strptime(msg.get("install_date", ""), "%Y-%m-%d").strftime("%A, %B %-d")
    except ValueError:
        day = msg.get("install_date", "")
    portal = os.environ.get("PORTAL_URL", "").strip()
    text = "\n\n".join(x for x in [
        f"Hi {name}," if name else "Hello,",
        f"Welcome to {company}! We're glad to be working with you.",
        f"Your first delivery is {day}. After that, your route driver comes every {msg.get('route_day', '')}"
        + (f" ({msg.get('frequency').lower()})" if msg.get("frequency") else "") + ".",
        "On install day your driver brings everything on your order, sets it up with you, and shows you how pickup "
        "and drop-off work. If anything isn't right, tell the driver or text us.",
        "Any change you ask for (more, fewer, a new item, a new employee) is confirmed to you in writing once it's done.",
        f"You can see your account and invoices anytime in our customer portal: {portal}" if portal else
        "We'll send you a link to our customer portal, where you can see your account and invoices.",
        "Text us 24/7 at 866-TAP-TEXT (866) 827-8398.",
        f"Our office is open Monday to Friday, 8 to 5: {office_phone(company)}.",
        "Thanks for choosing us,",
    ] if x)
    return company, f"Welcome to {company}", text + "\n\n" + company


def send_welcome(msg: dict, key: str) -> str:
    from . import bird
    company, subject, text = welcome_text(msg)
    tlg = company == "The Laundry Group"
    sender = (tlg and os.environ.get("WELCOME_FROM_TLG")) or os.environ.get("WELCOME_FROM", "welcome@mail.snowwhitelinen.com")
    reply = (tlg and os.environ.get("EMAIL_REPLY_TO_TLG")) or os.environ.get("EMAIL_REPLY_TO", "service@snowwhitelinen.com")
    body = {"from": {"email": sender, "name": company}, "to": [msg["to"]["email"]], "reply_to": [reply],
            "subject": subject, "text": text, "html": bird._html(text.rsplit("\n\n", 1)[0], company, subject),
            "category": "transactional"}
    r = bird._call("POST", "/v1/email/messages", body=body, key=f"welcome-{msg['request_id']}", api_key=key)
    from . import comms
    comms.log("out", "email", msg["to"]["email"], f"Subject: {subject}\n{text}")
    return str(r.get("id") or r.get("message_id") or "")

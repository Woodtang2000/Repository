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
TEST_CHANNEL = "C0CG9NLBF4Y"  # #sales-handoff-test
STOCKROOM_ID = os.environ.get("STOCKROOM_CHANNEL_ID", "C0CCYH2T96C")  # #stockroom (private): the garment watch
STOCKROOM_PEOPLE = os.environ.get("STOCKROOM_PEOPLE", "U04CX5DR1NV,U4KLRNUG5")  # Ana, Sonja
WATCH_DAYS, WATCH_HOUR, REMIND_DAYS = 90, 8, 3


def test_mode() -> bool:
    """Pointed at #sales-handoff-test (or NEW_ACCOUNTS_TEST=on): every check runs, but nothing goes to Alliant's
    workstation and no welcome email is sent. "Donna pause" isn't enough: a paused job still runs on "Donna resume"."""
    return CHANNEL == TEST_CHANNEL or os.environ.get("NEW_ACCOUNTS_TEST", "").lower() in ("on", "1", "yes", "true")
VERSION = "3.1"
REQUIRED = ("request_id", "company", "account_name", "location_name", "service_address", "location_contact",
            "invoice_email", "salesperson", "contract", "install_date", "route", "delivery_days", "rates", "lines",
            "expected_invoice")
LINE_REQUIRED = ("sku", "description", "billing_basis", "quantity", "unit_price", "frequency")
WEEK_CODES = {"eow": r"[56]", "e4w": r"[1-4]", "e12w": r"B[1-4]"}  # Alliant's 12-week codes are B1-B4 (Scott, 11/11)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # Saturday is a normal TLG day (Scott, 11/11)  # the exact codes each one may use
FREQ_DELIVERIES = {"weekly": 4, "eow": 2, "e4w": 1, "e12w": 1 / 3}  # deliveries in a 4-week period, per the protocol
GARMENT_SKU = re.compile(r"^2-")  # Snow White's garment SKUs start 2- (chef coats 2-0413WH, pants 2-2020WH...)


def bot_id() -> str:
    return os.environ.get("SALES_HANDOFF_BOT_ID", "U0CF01ML0AK").strip()  # the Sales Handoff app's bot user (11/10)


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
                r"[0-9FYX]|B[1-4]", str(ln.get("frequency", "")).upper()):
            out.append(f"lines[{i}].frequency ({ln.get('frequency')!r} isn't weekly, EOW, E4W, E12W or an Alliant code)")
        fc = str(ln.get("frequency_code") or "").strip().upper()
        if fc:  # optional: the exact Alliant week code (Scott, 11/10); it has to fit the line's frequency
            f = str(ln.get("frequency", "")).lower()
            if f not in WEEK_CODES or not re.fullmatch(WEEK_CODES[f], fc):
                out.append(f"lines[{i}].frequency_code ({fc!r} doesn't fit {ln.get('frequency')!r}: EOW takes 5 or 6, "
                           "E4W 1-4, E12W B1-B4, weekly none)")
        if str(ln.get("billing_basis", "")).title() not in ("Usage", "Inventory"):
            out.append(f"lines[{i}].billing_basis ({ln.get('billing_basis')!r}: Usage or Inventory)")
    if req.get("company") not in ("SW", "TLG"):
        out.append("company (SW or TLG)")
    out += day_gaps(req)
    garments = [i for i, ln in enumerate(req.get("lines") or []) if is_garment(str(ln.get("sku", "")))]
    logo_mats = [i for i, ln in enumerate(req.get("lines") or []) if str(ln.get("sku", "")).strip().startswith("3-")]
    if (garments or logo_mats) and not req.get("needs_stock_ok"):
        out.append("needs_stock_ok (true when there are garments, custom items or logo mats)")
    for i, ln in enumerate(req.get("lines") or []):  # Scott, 11/11: only mats (10-, logo mats 3-) go less than weekly
        if not is_mat(str(ln.get("sku", ""))) and str(ln.get("frequency", "")).lower() not in ("weekly", "7", "8"):
            out.append(f"lines[{i}].frequency ({ln.get('sku')} isn't a mat: only mats go less often than weekly)")
    cond = str((req.get("optional") or {}).get("conditions") or "").strip()
    if cond:  # protocol: if it changes a charge or anything Donna would set, she asks; she never guesses a charge
        out.append(f"optional.conditions (\"{cond[:200]}\": Scott or Bill to say how it's set up; send it again "
                   "as an additional_charges entry, or with conditions empty if nothing needs setting)")
    return out


_SKUS: dict = {}


def account_days(req: dict) -> list[str]:
    return [str(d).strip().title()[:3] for d in req.get("delivery_days") or []]


def per_week(req: dict) -> int:
    """The account's deliveries a week: deliveries_per_week, or the number of delivery days."""
    try:
        return int(req.get("deliveries_per_week") or 0) or len(account_days(req))
    except (TypeError, ValueError):
        return len(account_days(req))


def line_days(ln: dict) -> list[str]:
    return [str(d).strip().title()[:3] for d in ln.get("days") or []]


def plan(req: dict, ln: dict) -> tuple[str, list[str], int]:
    """(Alliant frequency code, the item's days if known, its deliveries a week). Weekly items: 7 on one day, 8 on
    more (Scott, 11/11); other frequencies keep their week code, on one day."""
    days, acct = line_days(ln), per_week(req)
    if str(ln.get("frequency", "")).lower() != "weekly":
        return str(ln.get("frequency_code") or ln.get("frequency")).upper(), days, 1
    n = len(days) or int(ln.get("times_per_week") or acct or 1)
    if not days and n == acct:
        days = account_days(req)  # every delivery
    return ("7" if n == 1 else "8"), days, n


def day_gaps(req: dict) -> list[str]:
    out, acct, dd = [], per_week(req), account_days(req)
    bad = [d for d in dd if d not in DAYS]
    if bad:
        out.append(f"delivery_days ({', '.join(bad)}: use Mon, Tue, Wed, Thu, Fri, Sat or Sun)")
    if req.get("deliveries_per_week") not in (None, ""):
        try:
            n = int(req["deliveries_per_week"])
        except (TypeError, ValueError):
            n = -1
        if not 1 <= n <= 6:
            out.append(f"deliveries_per_week ({req['deliveries_per_week']!r}: 1 to 6)")
        elif dd and n != len(dd):
            out.append(f"deliveries_per_week ({n}, but delivery_days lists {len(dd)} days)")
    for i, ln in enumerate(req.get("lines") or []):
        weekly = str(ln.get("frequency", "")).lower() == "weekly"
        days, tpw = line_days(ln), ln.get("times_per_week")
        if tpw not in (None, ""):
            try:
                t = int(tpw)
            except (TypeError, ValueError):
                t = 0
            if not 1 <= t <= max(acct, 1):
                out.append(f"lines[{i}].times_per_week ({tpw!r}: 1 to the account's {acct} a week)")
            elif t > 1 and not weekly:
                out.append(f"lines[{i}].times_per_week ({t} a week only fits a weekly line)")
            elif days and len(days) != t:
                out.append(f"lines[{i}].days ({len(days)} days but times_per_week {t})")
        if days:
            off = [d for d in days if d not in dd]
            if off:
                out.append(f"lines[{i}].days ({', '.join(off)} isn't one of the account's delivery days)")
            if not weekly and len(days) != 1:
                out.append(f"lines[{i}].days (an {ln.get('frequency')} line goes on one day)")
    return out


def is_mat(sku: str) -> bool:
    """Mats are SKUs 10- (and logo mats 3-) (Scott, 11/11)."""
    return (sku or "").strip().startswith(("10-", "3-"))


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
    if (sku or "").strip().startswith("4-01-"):
        return False  # NOG: the customer's own garments we only launder; no stock, no wearers to bill (garment_check)
    return kind == "garment" if kind is not None else bool(GARMENT_SKU.match(sku or ""))


def price_type(line: dict) -> int:
    """House rule: garments 9 whatever the billing (Scott, 11/10: billed per garment issued, by the auto count, never
    14); other items inventory 21 / usage 8. The SKU decides garment."""
    if is_garment(line.get("sku", "")):
        return 9
    return 21 if str(line.get("billing_basis", "")).title() == "Inventory" else 8


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
    if re.fullmatch(r"B[1-4]", c):
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
                                   "code that fits the route's cycle: EOW 5 or 6, E4W 1-4, E12W B1-B4)")
        if str(ln.get("frequency_code") or "").strip():
            code = f"{str(ln['frequency_code']).strip().upper()} (given in the request: use exactly this code)"
        if is_garment(ln["sku"]):  # billed on the wearers (phase 2), by the auto count of garments issued
            lines.append(f"- {ln['sku']} {ln['description']}: GARMENT, account catalog only: price {ln['unit_price']}, "
                         f"price type 9. No customer item line and no starting inventory: the wearers come in phase 2.")
            continue
        inv = ln["quantity"]
        auto = ln.get("autocount") or (ln["quantity"] if str(ln["billing_basis"]).title() == "Usage" else max(1, round(ln["quantity"] / 2)))
        pc, pdays, n = plan(req, ln)
        if per_week(req) > 1:  # which of the customer's days this item goes out (frequency 8: tick them)
            if str(ln.get("frequency", "")).lower() == "weekly":
                code = (f"{pc} on {', '.join(pdays)}" if pdays else
                        f"{pc} on {n} of the customer's days (choose to fit the route, spread through the week)")
            else:
                code += f", on {pdays[0]}" if pdays else ", on one of the customer's days (choose to fit the route)"
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
               "codes=<sku>:<frequency code>,... (the codes you used for every line); days=<sku>:<Mon+Thu>,... (the "
               "days ticked for every line, when the customer has more than one delivery day). Anything you couldn't enter: "
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
            self.result(req, thread, "waiting", "prepared; waiting for Ana's stock OK before saving"
                        + (" (TEST MODE: nothing will be sent to Alliant)" if test_mode() else ""),
                        **(dict(preview(req), test=True) if test_mode() else {}))
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
            if test_mode():
                rec.update(status="test")
                self.save(data)
        if test_mode():
            self.result(req, rec["thread"], "waiting", "TEST MODE: every check passed and the Alliant entry is ready, "
                        "but nothing was sent to Alliant (this is #sales-handoff-test). codes_chosen, lines and "
                        "invoice_check show what I would enter; the invoice check is worked from the request, not Alliant.",
                        test=True, **preview(req))
            return "test: not queued"
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
        if job.get("kind") == "garment_fix":
            return self.fix_done(job, question)
        rid = job.get("request_id", "")
        rec = self.load().get(rid) or {}
        req, thread = rec.get("req") or {"request_id": rid}, rec.get("thread")
        if not thread:
            return "no thread"
        if job.get("kind") == "new_wearers":
            st = "entered" if job["status"] == "entered" else "waiting" if job["status"] == "ask" else "failed"
            n_w = int(rec.get("wearer_count") or 0)
            said = {"entered": f"{n_w} wearer{'s' if n_w != 1 else ''} entered" if n_w else "wearers entered",
                    "waiting": "her workstation has a question about the wearers; Scott or Ana please see her report",
                    "failed": "her workstation couldn't enter the wearers; Scott or Ana please see her report"}[st]
            self.result(req, thread, st, said, customer_number=req.get("customer_number", ""))  # no names in Slack
            note = ((question + " ") if question else "") + (job.get("note") or "")
            if note.strip():  # her full report (it can name wearers) goes to Scott privately, never the channel
                owner = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
                try:
                    self.desk.slack.chat_postMessage(channel=owner, unfurl_links=False, text=(
                        f"👕 Wearers for {req.get('customer_number', '')} ({st}): " + note)[:3900])
                except Exception as e:
                    print(time.strftime("%H:%M"), "wearer report not sent:", str(e)[:150], flush=True)
            if st == "entered":
                self.start_watch(req.get("customer_number", ""))
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
                    days_chosen=check.get("days", []),
                    invoice_check={k: v for k, v in check.items() if k not in ("ok", "why", "days")},
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
        diffs, total, day_list = [], 0.0, []
        garments = [ln for ln in exp.get("lines") or [] if is_garment(str(ln.get("sku", "")))]
        for ln in exp.get("lines") or []:
            if ln in garments:
                continue  # billed on the wearers after phase 2; the garment watch in #stockroom follows them
            row = rows.get(ln.get("sku"))
            if not row:
                diffs.append({"what": f"line {ln.get('sku')}", "expected": ln.get("amount"), "alliant": "not on the location"})
                continue
            on = [d for d in DAYS if row.get("d_" + d.lower()) not in (None, "", 0, "0", False, "False")]
            days = len(on)
            want_days = line_days(basis) if (basis := next((l for l in req.get("lines", []) if l.get("sku") == ln.get("sku")), {})) else []
            if per_week(req) > 1:
                day_list.append({"sku": ln.get("sku"), "frequency_code": str(row.get("frequency", "")).strip(), "days": on})
                if want_days and sorted(want_days, key=DAYS.index) != on:
                    diffs.append({"what": f"days {ln.get('sku')}", "expected": want_days, "alliant": on})
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
        lines_expected = round(sum(float(l.get("amount") or 0) for l in exp.get("lines") or [] if l not in garments), 2)
        out = {"live": True, "expected_lines_total": lines_expected, "alliant_lines_total": round(total, 2),
               "expected_total": exp.get("total"), "differences": diffs,
               "not_checked": ["service_charge", "inventory_charge", "fuel"]
               + [f"line {g.get('sku')} (garment: billed on the wearers; the garment watch follows it)" for g in garments]}
        out["days"] = day_list
        out["ok"] = not diffs and abs(lines_expected - total) <= 1.00
        if not out["ok"]:
            out["why"] = f"{len(diffs)} difference(s) between the contract and Alliant; fix before install day"
        return out

    # ---- the garment watch (#stockroom) ---------------------------------------------------------------------------
    # Scott, 11/10: garments are billed per garment issued (price type 9, by the auto count). Stock often comes in over
    # weeks, and when an auto count isn't raised as garments are issued, we underbill. For 90 days after a new garment
    # account's wearers are entered, Donna compares each wearer's garments issued with the auto count every morning,
    # raises an auto count that's behind (the issued garments back it up), and tells Ana and Sonja in #stockroom what
    # she changed. Anything else (an auto count above what's issued, a total off the contract) she asks them about.
    # Donna helps the stockroom keep up; Ana and Sonja still decide everything that isn't routine.

    def watches(self) -> dict:
        return self.load().get("_watch") or {}

    def _save_watch(self, cust: str, **kw) -> dict:
        with self.lock:
            data = self.load()
            w = data.setdefault("_watch", {}).setdefault(cust, {})
            w.update(kw)
            self.save(data)
            return dict(w)

    def start_watch(self, cust: str) -> str:
        """After phase 2: the contract's garment counts (per delivery, from the phase 1 request) are the target."""
        if not re.fullmatch(r"\d{1,6}-[12]-\w{1,8}", cust or "") or cust in self.watches():
            return ""
        entry = next((r for r in self.load().values() if isinstance(r, dict) and r.get("req")
                      and (r.get("result") or {}).get("customer_number") == cust), {})
        req = entry.get("req") or {}
        contract = {ln["sku"]: float(ln.get("quantity") or 0) for ln in req.get("lines") or []
                    if is_garment(str(ln.get("sku", "")))}
        self._save_watch(cust, name=req.get("location_name") or req.get("account_name") or "", contract=contract,
                         since=time.time(), done=False)
        return "watching"

    def _tags(self) -> str:
        return " ".join(f"<@{u.strip()}>" for u in STOCKROOM_PEOPLE.split(",") if u.strip())

    def _say(self, cust: str, text: str) -> None:
        w = self.watches().get(cust) or {}
        if not w.get("ts"):
            head = self.desk.slack.chat_postMessage(
                channel=STOCKROOM_ID, unfurl_links=False,
                text=f"🧺 *{w.get('name') or 'New account'} · {cust}*: new garment account. Every morning for {WATCH_DAYS} "
                     "days I'll compare the garments issued to each wearer with the auto count (what we bill), raise "
                     "an auto count that's behind, and say here what I changed. Anything unusual I'll ask you about. "
                     + self._tags())
            w = self._save_watch(cust, ts=head["ts"])
        self.desk.slack.chat_postMessage(channel=STOCKROOM_ID, thread_ts=w["ts"], text=text[:3900], unfurl_links=False)

    def garment_pass(self, now: float) -> None:
        """Once a morning (from 8 AM): every new garment account still being watched."""
        local = datetime.fromtimestamp(now, ALASKA)
        if local.hour < WATCH_HOUR:
            return
        day = local.strftime("%Y-%m-%d")
        with self.lock:
            data = self.load()
            if data.get("_watch_day") == day or not any(not w.get("done") for w in (data.get("_watch") or {}).values()):
                return
            data["_watch_day"] = day
            self.save(data)
        for cust, w in self.watches().items():
            if not w.get("done"):
                try:
                    print(time.strftime("%H:%M"), "garment watch", cust, self.check_garments(cust, now), flush=True)
                except Exception as e:
                    print(time.strftime("%H:%M"), "garment watch", cust, "error:", str(e)[:200], flush=True)

    def check_garments(self, cust: str, now: float | None = None) -> str:
        from . import alliant_live
        now = now or time.time()
        w = self.watches().get(cust) or {}
        r = alliant_live.lookup("garment_billing", self.desk.data_dir, fresh=True, account=cust)
        if r["source"] != "live":
            return "Alliant not live; tomorrow"
        rows = [x for x in r["rows"] if not str(x.get("sku", "")).startswith("4-01-")]  # NOG: customer-owned
        if not rows:
            return "no wearer garments yet"
        num = lambda v: int(float(v or 0))
        skus: dict = {}
        for x in rows:
            t = skus.setdefault(x["sku"], {"item": x.get("item") or x["sku"], "inv": 0, "auto": 0, "issued": 0, "order": 0})
            t["inv"] += num(x.get("inventory"))
            t["auto"] += num(x.get("autocount"))
            t["issued"] += num(x.get("issued"))
            t["order"] += num(x.get("on_order"))
        contract = w.get("contract") or {}
        for sku, t in skus.items():  # the ratio rule: inventory equal to the auto count, or double it
            q = float(contract.get(sku) or 0)
            t["ratio"] = 2 if q and t["inv"] >= 1.5 * q else 1
        fixes, asks = [], []
        for x in rows:
            t = skus[x["sku"]]
            inv, auto, issued = num(x.get("inventory")), num(x.get("autocount")), num(x.get("issued"))
            want = min(issued, inv) // t["ratio"]
            who = f"#{x['wearer']} {x.get('name') or ''}".strip()
            what = f"{x['sku']} {t['item']}"
            if auto < want:
                fixes.append({"wearer": x["wearer"], "who": who, "what": what, "from": auto, "to": want, "issued": issued})
            elif auto > want and issued >= inv:
                asks.append(f"{who} · {what}: auto count {auto}, but {issued} issued (inventory {inv})")
            elif auto > want:
                asks.append(f"{who} · {what}: billing {auto} while only {issued} of {inv} are issued")
        short = {sku: t for sku, t in skus.items() if t["issued"] < t["inv"]}
        off = [f"{t['item']}: billing {t['auto']} a delivery, the contract says {contract[sku]:g}"
               for sku, t in skus.items() if contract.get(sku) and not short.get(sku)
               and abs(t["auto"] - float(contract[sku])) >= 1 and not any(f["what"].startswith(sku + " ") for f in fixes)]
        said = []
        if fixes and not w.get("fix_job"):
            lines = [f"- wearer {f['who']} · {f['what']}: Auto Count {f['from']} → {f['to']} ({f['issued']} issued)" for f in fixes]
            jid = self.desk.jobs().custom_job("garment_fix", "\n".join([
                f"GARMENT AUTO COUNT FIX (garment watch, new account {cust}). Follow your rules section "
                "\"Garment auto count fixes\".",
                "For each line: open the wearer's garment line and check its Auto Count shows the first number. If it "
                "does, set it to the second number and save; if it doesn't, change nothing on that line and say so. "
                "Change nothing else.", *lines]), {"account": cust})
            self._save_watch(cust, fix_job=jid)
            self._say(cust, "🔧 Billing is behind the garments issued, so I'm raising these auto counts in Alliant:\n"
                      + "\n".join(lines[:25]) + "\nI'll confirm here when it's saved.")
            said.append("fix")
        if asks or off:
            key = "|".join(asks + off)
            if key != w.get("asked") or now - w.get("asked_at", 0) > REMIND_DAYS * 86400:
                self._say(cust, f"❓ {self._tags()} these don't match and I've left them for you:\n"
                          + "\n".join(f"• {a}" for a in (asks + off)[:25]))
                self._save_watch(cust, asked=key, asked_at=now)
                said.append("ask")
        if short and now - w.get("short_at", 0) > REMIND_DAYS * 86400:
            self._say(cust, "⏳ Still coming in:\n" + "\n".join(
                f"• {t['item']}: {t['issued']} of {t['inv']} issued" + (f", {t['order']} on order" if t["order"] else
                                                                         ", *nothing on order*") for t in short.values())
                      + ("\n" + self._tags() + " something has nothing on order." if any(not t["order"] for t in short.values()) else ""))
            self._save_watch(cust, short_at=now)
            said.append("short")
        if not fixes and not asks and not off and not short and not w.get("fix_job"):
            self._say(cust, "✅ Every garment is issued and billing matches: " + ", ".join(
                f"{t['item']} {t['issued']} issued, billing {t['auto']}" for t in skus.values()) + ". I've stopped watching this one.")
            self._save_watch(cust, done=True, done_at=now)
            return "done"
        if now - w.get("since", now) > WATCH_DAYS * 86400:
            self._say(cust, f"🛑 {self._tags()} {WATCH_DAYS} days are up and this account still isn't settled (above). "
                      "I've stopped checking it daily; the Monday garment check still covers it.")
            self._save_watch(cust, done=True, done_at=now)
            return "timed out"
        return ", ".join(said) or "nothing new"

    def fix_done(self, job: dict, question: str = "") -> str:
        cust = job.get("account", "")
        self._save_watch(cust, fix_job="")
        note = ((question + " ") if question else "") + (job.get("note") or "")
        if job["status"] == "entered":
            self._say(cust, "✅ Auto counts updated in Alliant. " + note[:1500])
        else:
            self._say(cust, f"⚠️ {self._tags()} I couldn't update the auto counts: {note[:1500]}\nPlease take a look.")
        return f"garment fix {job['status']}"

    # ---- phase 2 and the welcome email ---------------------------------------------------------------------------

    def wearers(self, msg: dict, thread: str) -> str:
        rid = msg.get("request_id", "")
        known = self.load().get(rid)
        if known and known.get("result") and known.get("status") != "waiting":  # a waiting one may be posted again
            self.post(thread, "Already handled; here is the result again.", known["result"])
            return "repeat"
        cust = msg.get("customer_number", "")
        if re.fullmatch(r"\d{1,6}-[12]-\w{1,8}", cust or "") and not msg.get("wearers") and msg.get("wearer_count"):
            # protocol v3.1 (11/11): the names come from the Sales Handoff REST endpoint, never Slack
            got, why = fetch_wearers(rid, cust, msg.get("wearer_count"))
            if not got:
                self.result({"request_id": rid, "customer_number": cust}, thread, "waiting",
                            f"{msg.get('wearer_count')} wearers ready, but I couldn't fetch them ({why}); I'll try "
                            "again if the request is posted again, or a person can enter them", customer_number=cust)
                return "waiting: endpoint"
            msg = dict(msg, wearers=got)
        if not re.fullmatch(r"\d{1,6}-[12]-\w{1,8}", cust or "") or not msg.get("wearers"):
            self.result({"request_id": rid}, thread, "waiting", "missing customer_number or wearers")
            return "waiting"
        text = ["NEW WEARERS from #new-accounts (phase 2). Follow your rules section \"New accounts\".",
                f"Customer {cust}. Garment prices come from the account catalog: never type one.",
                "For each wearer: add the wearer (next wearer number unless one is given), then each garment with its "
                "size and changes (inventory), embroidery and emblem as given, price type 9. Auto Count: the garments "
                "of it already issued to that wearer (0 if none yet); the garment watch raises it as they're issued."]
        for w in msg["wearers"]:
            text.append(f"- {w.get('name')}" + "".join(f" · {k} {w[k]}" for k in ("wearer_number", "locker", "department") if w.get(k)))
            for g in w.get("garments") or []:
                size = g.get("size") or "x".join(x for x in (g.get("size_a"), g.get("size_b")) if x)
                text.append(f"    {g.get('sku')} size {size}, {g.get('changes')} changes, embroidery {g.get('embroidery') or 'none'}, "
                            f"emblem {g.get('emblem_code') or 'none'}")
        if test_mode():
            self.result({"request_id": rid}, thread, "waiting", f"TEST MODE: wearers for {cust} checked "
                        f"({len(msg['wearers'])}), nothing sent to Alliant", test=True)
            return "test: wearers not queued"
        with self.lock:
            data = self.load()
            data[rid] = {"req": {"request_id": rid, "customer_number": cust}, "thread": thread, "status": "queued",
                         "wearer_count": len(msg["wearers"])}
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
        if test_mode():
            res.update(status="test", notes=f"TEST MODE: the welcome email to {to.get('email') or '(no email)'} "
                                            "was built but not sent")
        elif not key:
            res["notes"] = "the Sales Bird key (BIRD_SALES_API_KEY) isn't set up on Donna's server yet"
        elif "@" not in (to.get("email") or ""):
            res["notes"] = "no signer email"
        else:
            try:
                res["bird_message_id"] = send_welcome(msg, key)
                res["status"] = "sent"
            except Exception as e:
                res["notes"] = f"Bird didn't take it: {str(e)[:200]}"
        self.post(thread, "Welcome email sent." if res["status"] == "sent" else res["notes"] if res["status"] == "test"
                  else f"Welcome email not sent: {res['notes']}", res)
        if res["status"] == "sent":
            with self.lock:
                data = self.load()
                data[rid] = {"result": res, "thread": thread, "status": "sent"}
                self.save(data)
        return res["status"]


def preview(req: dict) -> dict:
    """Test mode (Scott, 11/12): what Donna would enter, and the invoice math worked from the request by the protocol's
    rules (Alliant isn't touched, so this checks Sales' expected_invoice, not Alliant's projection)."""
    acct, lines, codes, days_chosen, total_lines = per_week(req), [], [], [], 0.0
    for ln in req.get("lines") or []:
        code, days, n = plan(req, ln)
        garment = is_garment(str(ln.get("sku", "")))
        f = str(ln.get("frequency", "")).lower()
        if f in ("eow", "e4w", "e12w") and not str(ln.get("frequency_code") or "").strip():
            code = {"eow": "5 or 6", "e4w": "1-4", "e12w": "B1-B4"}[f] + " (chosen at entry to fit the route)"
        per4 = 4.0 * n if f == "weekly" else {"eow": 2.0, "e4w": 1.0, "e12w": 1 / 3}.get(f, deliveries(code))
        amount = round(float(ln.get("quantity") or 0) * float(ln.get("unit_price") or 0) * per4, 2)
        total_lines += amount
        basis = str(ln.get("billing_basis", "")).title()
        lines.append({"sku": ln.get("sku"), "price_type": price_type(ln), "frequency_code": code,
                      "days": days or ([d for d in account_days(req)] if f == "weekly" and n == acct else []),
                      **({"enter": "catalog only (price, price type 9); billed on the wearers in phase 2"} if garment else
                         {"starting_inventory": ln.get("quantity"),
                          "autocount": ln.get("autocount") or (ln.get("quantity") if basis == "Usage"
                                                               else max(1, round(float(ln.get("quantity") or 0) / 2)))}),
                      "deliveries_in_4_weeks": per4, "amount": amount})
        if f in ("eow", "e4w", "e12w"):
            codes.append({"sku": ln.get("sku"), "frequency_code": code})
        if acct > 1:
            days_chosen.append({"sku": ln.get("sku"), "frequency_code": code, "days": lines[-1]["days"] or "chosen at entry"})
    rates, exp = req.get("rates") or {}, req.get("expected_invoice") or {}
    total_lines = round(total_lines, 2)
    service = round(total_lines * float(rates.get("service_charge_pct") or 0) / 100, 2)
    inventory = round(total_lines * float(rates.get("inventory_charge_pct") or 0) / 100, 2)
    fuel = round(float(rates.get("fuel_charge") or 0) * 4 * max(acct, 1), 2)
    mine = {"lines": total_lines, "service_charge": service, "inventory_charge": inventory, "fuel": fuel,
            "total": round(total_lines + service + inventory + fuel, 2)}
    diffs = []
    for ln, x in zip(req.get("lines") or [], lines):
        want = next((e.get("amount") for e in exp.get("lines") or [] if e.get("sku") == ln.get("sku")), None)
        if want is not None and abs(float(want) - x["amount"]) > 0.01:
            diffs.append({"what": f"line {ln.get('sku')}", "expected": want, "donna": x["amount"]})
    for k in ("service_charge", "inventory_charge", "fuel", "total"):
        if exp.get(k) is not None and abs(float(exp[k]) - mine[k]) > 0.01:
            diffs.append({"what": k, "expected": exp[k], "donna": mine[k]})
    return {"codes_chosen": codes, "days_chosen": days_chosen, "lines": lines,
            "invoice_check": {"from": "the request (test mode; Alliant not read)", "expected_total": exp.get("total"),
                              "donna_total": mine["total"], "donna": mine, "differences": diffs}}


def fetch_wearers(request_id: str, customer_number: str, count) -> tuple[list, str]:
    """The wearer list from the Sales Handoff endpoint: GET <SALES_HANDOFF_URL>/wearers/<request_id> with the key
    SALES_HANDOFF_KEY (Scott enters it on Donna's server). Returns (wearers, "") or ([], why). Never logs names."""
    import urllib.error
    import urllib.parse
    import urllib.request
    base, key = os.environ.get("SALES_HANDOFF_URL", "").rstrip("/"), os.environ.get("SALES_HANDOFF_KEY", "").strip()
    if not base or not key:
        return [], "the Sales Handoff endpoint isn't set up on Donna's server yet"
    url = f"{base}/wearers/{urllib.parse.quote(request_id, safe='')}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return [], f"the endpoint answered {e.code}"
    except Exception as e:
        return [], f"the endpoint didn't answer ({type(e).__name__})"
    wearers = body.get("wearers") if isinstance(body, dict) else None
    if body.get("request_id") not in (None, request_id) or body.get("customer_number") not in (None, customer_number):
        return [], "the endpoint's answer is for a different request or customer"
    if not isinstance(wearers, list) or not wearers or not all(isinstance(w, dict) and w.get("name") and
                                                                isinstance(w.get("garments"), list) for w in wearers):
        return [], "the endpoint's list is empty or not in the agreed shape"
    try:
        if count not in (None, "") and int(count) != len(wearers):
            return [], f"Slack said {count} wearers but the endpoint has {len(wearers)}"
    except (TypeError, ValueError):
        pass
    return wearers, ""


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

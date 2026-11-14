"""Garment repair and replacement requests (Scott, 11/14): the QR page, driver mode, texts, emails and calls all end as
an approved Alliant service code, and the loop is closed with the customer. Spec: deploy/uniform_requests/mockups.html.

- The Salesforce page (built by the Sales agent) calls Donna server to server through webchat.py, with UNIFORM_API_KEY:
  /api/uniform/garment (barcode -> garment type and size, never a name), /api/uniform/match (no tag: company and first
  name -> account suggestion), /api/uniform/request (a saved request).
- Donna suggests the code and scope, posts a card in #uniform-requests (no wearer names in Slack), the office approves
  with one tap, and her Alliant workstation enters the code (kind "service_code"). Then she watches Alliant until the
  plant clears the code and tells the requester, the driver and the customer.
- UNIFORM_TEST (on until Scott switches it off): cards go to #uniform-requests-test, nothing is entered in Alliant and
  every text or email goes to Scott instead of the customer.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime

from .context import ALASKA

# Snow White's service codes (Alliant, 10/24) plus SZ, ST and WR (Scott, 11/14).
CODES = {"AA": "Account Garment Audit", "BR": "Button Replacement", "GM": "General Mend", "GP": "Garment Patched",
         "NT": "Replace Name/Company Patch", "RL": "Change Label", "SS": "Seam Repair", "ZR": "Zipper Replacement",
         "SZ": "Check Fit / Size", "ST": "Stain Treatment", "WR": "Worn, Replace"}
# problem -> (code, scope, what the requester called it)
PROBLEMS = {"torn": ("SS", "garment", "ripped or torn"), "button": ("BR", "garment", "a button"),
            "zipper": ("ZR", "garment", "the zipper"), "fit_big": ("SZ", "wearer", "too big"),
            "fit_small": ("SZ", "wearer", "too small"), "stain": ("ST", "garment", "stained"),
            "worn": ("WR", "garment", "worn out"), "patch": ("NT", "garment", "the name or company patch"),
            "other": ("GM", "garment", "something else"), "audit": ("AA", "customer", "the garments look old")}
HOW = {"garment": 'Inquiry > "Garment" tab > scan or type the barcode > "Add Service Code"',
       "wearer": 'Stockroom > "Add/Remove Service Code" > scope "Specific Wearers" > "Append" > "Begin"',
       "customer": 'Stockroom > "Add/Remove Service Code" > scope "Specific Customer" > "Append" > "Begin"'}
REPEAT_DAYS, WEARER_DAYS = 60, 30
STUCK_DAYS, NUDGE_DAYS = 14, 7  # a code still on 14 days after entry: ask the stockroom, again weekly (Scott, 11/14)
BARCODE = re.compile(r"\d{4,12}")  # the garment number under the barcode; leading zeros optional (Scott, 11/14)


def test_mode() -> bool:
    return os.environ.get("UNIFORM_TEST", "on").lower() not in ("off", "0", "no", "false")


def _key() -> bytes:
    return os.environ.get("UNIFORM_API_KEY", "").encode()


def sign(data: dict) -> str:
    """A garment_ref: who the garment belongs to, for Donna only, signed so the page can't forge one."""
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_key() or b"-", raw.encode(), hashlib.sha256).hexdigest()[:24]
    return f"g1.{raw}.{sig}"


def unsign(ref: str) -> dict | None:
    try:
        _, raw, sig = (ref or "").split(".")
        if not hmac.compare_digest(sig, hmac.new(_key() or b"-", raw.encode(), hashlib.sha256).hexdigest()[:24]):
            return None
        return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        return None


def nice(name: str) -> str:
    """Alliant's capitals, readable on a phone: "SHIRT MIMIX LS SHIRT NAVY" -> "Shirt Mimix LS Shirt Navy",
    "THREE BEARS BIG LAKE C STORE #27" -> "Three Bears Big Lake C Store #27". Short codes (LS, FR, XL, AK) stay capitals."""
    out = []
    for w in (name or "").split():
        core = re.sub(r"[^A-Za-z]", "", w)
        if (len(core) <= 2 and core.isupper() and core not in ("OF", "AT", "IN", "ON", "TO", "BY")) or re.search(r"\d", w):
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:].lower() if w.isupper() else w)
    return " ".join(out)


def describe(row: dict) -> tuple[str, str]:
    """("Coverall Navy", "52 L") from a garments_in_service row: what it is and its size, nothing about who."""
    item = nice((row.get("item") or row.get("description") or row.get("sku") or "garment").strip())
    size = " ".join(x for x in (row.get("size_a"), row.get("size_b")) if x)
    return item, size


def lookup(alliant, barcode: str) -> dict:
    """Barcode -> what the page may show (garment, size) and what Apex may keep (account), from the hourly file."""
    b = re.sub(r"\D", "", barcode or "")
    if not BARCODE.fullmatch(b):
        return {"found": False}
    row = alliant.garment(b) if alliant else None
    if not row:
        return {"found": False}
    item, size = describe(row)
    acct = row.get("cust_code", "")
    return {"found": True, "garment": item, "size": size, "account": acct,
            "garment_ref": sign({"b": row.get("barcode") or b, "a": acct, "w": row.get("wearer", ""),
                                 "s": row.get("sku", "")})}


def match(alliant, company: str, first_name: str) -> dict:
    """No tag: the accounts whose name fits the company (best first), and whether that account has a wearer of that
    first name. Nothing about the wearer goes back: the office sees it on the card's Salesforce record."""
    words = [w for w in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(w) > 2 and w not in ("the", "inc", "llc")]
    if not words or not alliant:
        return {"accounts": [], "confidence": "low"}
    scored = []
    for c in alliant.customers:
        name = c.name.lower()
        hits = sum(1 for w in words if w in name)
        if hits:
            scored.append((hits / len(words), c.account))
    scored.sort(key=lambda x: -x[0])
    accounts = [a for s, a in scored[:5] if s >= 0.5]
    first = (first_name or "").strip().lower()
    has = bool(first) and any(w.first.lower().startswith(first) for a in accounts[:3] for w in alliant.wearers.get(a, []))
    return {"accounts": accounts, "confidence": "high" if accounts and has and scored[0][0] == 1 else "low"}


def stops(alliant, route: str, branch: str = "1", day: str = "") -> dict:
    """Driver mode: today's stops on the driver's route, in stop order (route_stops.csv), so the driver picks the account
    in one tap; the first garment scanned then confirms it. Accounts and names only: drivers serve these customers."""
    day = (day or datetime.now(ALASKA).strftime("%a"))[:3].title()
    r, b = str(route or "").lstrip("0"), str(branch or "1").lstrip("0") or "1"
    rows = [x for x in getattr(alliant, "stops", None) or [] if str(x.get("route", "")).lstrip("0") == r
            and str(x.get("branch", "1")).lstrip("0") == b and str(x.get("day", ""))[:3].title() == day]
    num = lambda v: int(v) if str(v or "").isdigit() else 9999
    rows.sort(key=lambda x: num(x.get("sequence")))
    return {"route": r, "branch": b, "day": day,
            "stops": [{"sequence": x.get("sequence", ""), "account": x.get("account", ""), "name": nice(x.get("name", "")),
                       "city": nice(x.get("city", ""))} for x in rows]}


class Uniform:
    def __init__(self, desk):
        self.desk = desk
        self.lock = threading.Lock()
        self._channel = None

    # ---- storage --------------------------------------------------------------------------------------------------

    def path(self) -> str:
        return os.path.join(self.desk.data_dir, "uniform_requests.json")

    def load(self) -> dict:
        try:
            with open(self.path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"requests": {}, "seq": 1000}

    def save(self, data: dict) -> None:
        with open(self.path() + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(self.path() + ".tmp", self.path())

    def channel(self) -> str:
        if self._channel is None:
            name = os.environ.get("UNIFORM_CHANNEL", "uniform-requests") + ("-test" if test_mode() else "")
            self._channel = self.desk.channels.get(name) or ""
        return self._channel

    # ---- a new request --------------------------------------------------------------------------------------------

    def suggest(self, garments: list[dict], account: str, data: dict) -> tuple[str, str, str]:
        """(code, scope, why): the first garment's problem decides; a wearer with another request in 30 days, or two
        garments of one wearer now, suggests the whole wearer; "garments look old" is an account audit."""
        problem = (garments[0].get("problem") if garments else "other") or "other"
        code, scope, _ = PROBLEMS.get(problem, PROBLEMS["other"])
        if scope != "garment":
            return code, scope, ""
        wearers = [w for w in (g.get("_wearer") for g in garments) if w]
        now = time.time()
        recent = [r for r in data["requests"].values() if r.get("account") == account and now - r.get("at", 0) < WEARER_DAYS * 86400
                  and set(r.get("wearers") or []) & set(wearers)]
        if recent:
            return code, "wearer", f"this wearer had {recent[0]['ref']} {_ago(recent[0]['at'])}"
        if len(wearers) >= 2 and len(set(wearers)) == 1:
            return code, "wearer", "several garments of one wearer"
        return code, "garment", ""

    def new(self, body: dict) -> dict:
        """A request from the page (or Donna's own intake). Returns {"ok", "ref", "suggested", "card"}."""
        garments = []
        for g in (body.get("garments") or [])[:20]:
            ref = unsign(str(g.get("garment_ref") or "")) or {}
            barcode = re.sub(r"\D", "", str(g.get("barcode") or ref.get("b") or ""))
            row = self.desk.alliant.garment(barcode) if barcode else None
            item, size = describe(row) if row else ("", "")
            problem = str(g.get("problem") or "other")
            garments.append({"barcode": barcode, "problem": problem if problem in PROBLEMS else "other",
                             "note": str(g.get("note") or "")[:300], "item": item, "size": size,
                             "_wearer": (row or {}).get("wearer") or ref.get("w", ""),
                             "_account": (row or {}).get("cust_code") or ref.get("a", "")})
        account = str(body.get("account") or next((g["_account"] for g in garments if g["_account"]), "") or "")
        with self.lock:
            data = self.load()
            data["seq"] = int(data.get("seq", 1000)) + 1
            ref = str(body.get("ref") or f"UR-{data['seq']}")
            code, scope, why = self.suggest(garments, account, data)
            repeat = next((r["ref"] for r in sorted(data["requests"].values(), key=lambda r: -r.get("at", 0))
                           if account and r.get("account") == account and time.time() - r.get("at", 0) < REPEAT_DAYS * 86400), "")
            rec = {"ref": ref, "at": time.time(), "status": "new", "source": str(body.get("source") or "qr"),
                   "brand": str(body.get("brand") or "SW"), "account": account, "garments": garments,
                   "wearers": sorted({g["_wearer"] for g in garments if g["_wearer"]}),
                   "fallback": body.get("fallback") or None, "photos": int(body.get("photos") or 0),
                   "contact": {k: str((body.get("contact") or {}).get(k) or "") for k in ("phone", "email")},
                   "driver": body.get("driver") or None, "sf_request_id": str(body.get("sf_request_id") or ""),
                   "code": code, "scope": scope, "why": why, "repeat_of": repeat}
            data["requests"][ref] = rec
            self.save(data)
        posted = self.card(rec)
        if repeat:
            self.tell_scott(f"🔁 Repeat garment complaint on *{self.acct_name(account)}*: {ref} after {repeat} (within "
                            f"{REPEAT_DAYS} days). The card is in #{os.environ.get('UNIFORM_CHANNEL', 'uniform-requests')}.")
        return {"ok": True, "ref": ref, "suggested": {"code": code, "scope": scope}, "card": "posted" if posted else "no channel"}

    # ---- the card -------------------------------------------------------------------------------------------------

    def acct_name(self, account: str) -> str:
        c = next((c for c in self.desk.alliant.customers if c.account == account), None)
        return f"{account} {c.name}" if c else (account or "account not matched")

    def lines(self, rec: dict) -> list[str]:
        src = {"qr": "from the QR page", "driver": "scanned by the driver", "text": "by text", "email": "by email",
               "phone": "by phone"}.get(rec["source"], rec["source"])
        out = [f"🧵 *{rec['ref']}{' · TEST' if test_mode() else ''} · {self.acct_name(rec['account'])}*"]
        for g in rec["garments"]:
            what = " ".join(x for x in (g["item"] or "Garment", g["size"]) if x)
            out.append(f"{what} · *{PROBLEMS[g['problem']][2].capitalize()}*" + (f" · _{g['note']}_" if g["note"] else ""))
        if rec.get("fallback"):
            out.append(f"No tag: said \"{(rec['fallback'] or {}).get('company', '')}\", item {(rec['fallback'] or {}).get('item', '?')}"
                       " (the first name is on the Salesforce record)")
        out.append(f"{src}" + (f" · 📎 {rec['photos']} photo{'s' if rec['photos'] != 1 else ''}" if rec["photos"] else ""))
        target = {"garment": "this garment" if len(rec["garments"]) == 1 else "these garments", "wearer": "all of this wearer's garments",
                  "customer": "the whole account"}[rec["scope"]]
        out.append(f"Suggested: *{rec['code']} {CODES[rec['code']]}* on {target}" + (f" ({rec['why']})" if rec["why"] else ""))
        if rec.get("repeat_of"):
            out.append(f"🔁 Repeat: {rec['repeat_of']} on this account in the last {REPEAT_DAYS} days (Scott told)")
        return out

    def buttons(self, rec: dict) -> dict:
        v = lambda **k: json.dumps({"ref": rec["ref"], **k})
        els = [{"type": "button", "action_id": "uniform_approve", "style": "primary", "value": v(),
                "text": {"type": "plain_text", "text": f"✅ Approve: {rec['code']} on {'garment' if rec['scope'] == 'garment' else rec['scope']}"}},
               {"type": "static_select", "action_id": "uniform_code", "placeholder": {"type": "plain_text", "text": "Other code…"},
                "options": [{"text": {"type": "plain_text", "text": f"{c} {d}"[:75]}, "value": v(code=c)} for c, d in CODES.items()]},
               {"type": "static_select", "action_id": "uniform_scope", "placeholder": {"type": "plain_text", "text": "Scope…"},
                "options": [{"text": {"type": "plain_text", "text": t}, "value": v(scope=s)} for s, t in
                            (("garment", "This garment"), ("wearer", "Whole wearer"), ("customer", "Whole account"))]},
               {"type": "button", "action_id": "uniform_reject", "value": v(),
                "text": {"type": "plain_text", "text": "Reject"}}]
        return {"type": "actions", "block_id": "uniform", "elements": els}

    def card(self, rec: dict, status: str = "") -> bool:
        ch = self.channel()
        if not ch:
            print(time.strftime("%H:%M"), "uniform: no #uniform-requests channel (or Donna isn't in it)", flush=True)
            return False
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(self.lines(rec))[:2900]}}]
        if rec["status"] == "new":
            blocks.append(self.buttons(rec))
        if status:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status[:2000]}]})
        text = self.lines(rec)[0]
        if rec.get("ts"):
            self.desk.slack.chat_update(channel=ch, ts=rec["ts"], text=text, blocks=blocks)
        else:
            rec["ts"] = self.desk.slack.chat_postMessage(channel=ch, text=text, blocks=blocks, unfurl_links=False)["ts"]
            with self.lock:
                data = self.load()
                if rec["ref"] in data["requests"]:
                    data["requests"][rec["ref"]]["ts"] = rec["ts"]
                    self.save(data)
        return True

    def update(self, ref: str, **kw) -> dict:
        with self.lock:
            data = self.load()
            rec = data["requests"][ref]
            rec.update(kw)
            self.save(data)
            return rec

    # ---- the office's tap -----------------------------------------------------------------------------------------

    def act(self, payload: dict) -> str:
        user = payload.get("user", {}).get("id")
        act = next((x for x in payload.get("actions", []) if x.get("action_id", "").startswith("uniform_")), {})
        value = json.loads((act.get("selected_option") or {}).get("value") or act.get("value") or "{}")
        ref = value.get("ref", "")
        if not (self.desk.is_office(user) or self.desk.is_owner(user)):
            self.desk.slack.chat_postEphemeral(channel=self.channel(), user=user, text="Only the office approves uniform requests.")
            return "ignored: not office"
        rec = self.load()["requests"].get(ref)
        if not rec or rec["status"] != "new":
            return "not open"
        who = (self.desk.name(user) or "office").split()[0]
        aid = act.get("action_id")
        if aid == "uniform_code":
            rec = self.update(ref, code=value["code"])
            self.card(rec)
            return f"{ref} code {value['code']}"
        if aid == "uniform_scope":
            rec = self.update(ref, scope=value["scope"])
            self.card(rec)
            return f"{ref} scope {value['scope']}"
        if aid == "uniform_reject":
            rec = self.update(ref, status="rejected", by=who, decided_at=time.time())
            self.card(rec, f"❌ Rejected by {who}. Nothing entered in Alliant.")
            return f"{ref} rejected"
        rec = self.update(ref, status="approved", by=who, by_id=user, decided_at=time.time())
        if test_mode():
            self.card(rec, f"✅ Approved by {who}. TEST: I would enter *{rec['code']}* now ({HOW[rec['scope']]}); nothing was "
                           "sent to Alliant.")
            return f"{ref} approved (test)"
        jid = self.desk.jobs().custom_job("service_code", self.job_text(rec), {"account": rec["account"], "request_id": ref})
        rec = self.update(ref, job=jid)
        self.card(rec, f"✅ Approved by {who}. 🤖 Entering *{rec['code']}* in Alliant now.")
        return f"{ref} approved, job {jid}"

    def job_text(self, rec: dict) -> str:
        """The workstation job: exactly which code on exactly which garments (rules: "Service codes")."""
        code = rec["code"]
        lines = [f"SERVICE CODE from #uniform-requests ({rec['ref']}), approved by {rec.get('by', 'the office')}. Follow your "
                 'rules section "Service codes".', f"Code: {code} {CODES[code]}. Removal: Until Removed. Append (never replace).",
                 f"Account: {rec['account']}"]
        notes = "; ".join(f"{g['problem']}: {g['note']}" for g in rec["garments"] if g["note"])
        if rec["scope"] == "garment":
            lines.append("One garment at a time: " + HOW["garment"] + ". Barcodes:")
            lines += [f"- {g['barcode']}" for g in rec["garments"] if g["barcode"]]
        elif rec["scope"] == "wearer":
            lines.append(HOW["wearer"] + f". Wearer number(s): {', '.join(rec['wearers']) or 'see the request'}.")
        else:
            lines.append(HOW["customer"] + ".")
        lines.append(f"Special Request: {rec['ref']}" + (f" {notes}"[:120] if notes else ""))
        lines.append("Before adding: check the garment shows this account. A garment already holding 3 codes: needs_person. "
                     "Report entered with how many garments got the code.")
        return "\n".join(lines)

    def job_done(self, job: dict, question: str = "") -> str:
        ref = job.get("request_id", "")
        rec = self.load()["requests"].get(ref)
        if not rec:
            return "unknown request"
        if job["status"] == "entered":
            rec = self.update(ref, status="entered", entered_at=time.time())
            self.card(rec, f"✅ Approved by {rec.get('by')}. 🤖 Entered in Alliant {datetime.now(ALASKA):%-I:%M %p}. ⏳ Waiting for "
                           "the plant to pull it at the next scan.")
            return f"{ref} entered"
        rec = self.update(ref, status="approved", job="")
        self.card(rec, f"⚠️ I couldn't enter it: {((question + ' ') if question else '') + (job.get('note') or '')}"[:1500]
                  + f"\nPlease enter *{rec['code']}* by hand: {HOW[rec['scope']]}.")
        return f"{ref} {job['status']}"

    # ---- closing the loop -----------------------------------------------------------------------------------------

    def done_pass(self, now: float | None = None) -> None:
        """Every few hours: an entered request is done when the code is gone from its garments in Alliant (the plant
        pulled and fixed or replaced them). Garments: live; a wearer or account: the hourly garment file."""
        now = now or time.time()
        if now - getattr(self, "_last", 0) < 3 * 3600:
            return
        self._last = now
        for ref, rec in list(self.load()["requests"].items()):
            if rec.get("status") != "entered":
                continue
            try:
                if self.cleared(rec):
                    self.close(ref)
                elif now - rec.get("entered_at", now) > STUCK_DAYS * 86400 and now - rec.get("nudged_at", 0) > NUDGE_DAYS * 86400:
                    self.nudge(ref, now)
            except Exception as e:
                print(time.strftime("%H:%M"), "uniform done check", ref, str(e)[:150], flush=True)

    def cleared(self, rec: dict) -> bool:
        code = rec["code"]
        if rec["scope"] == "garment":
            from . import alliant_live
            for g in rec["garments"]:
                if not g["barcode"]:
                    continue
                r = alliant_live.lookup("garment", self.desk.data_dir, barcode=g["barcode"])
                row = (r.get("rows") or [{}])[0]
                if r.get("source") != "live" or code in str(row.get("service_code") or ""):
                    return False
            return True
        rows = [x for (a, w), xs in self.desk.alliant.in_service.items() if a == rec["account"]
                and (rec["scope"] == "customer" or w in rec["wearers"]) for x in xs]
        return bool(rows) and not any(code in str(x.get("service_code") or x.get("srv_cod") or "") for x in rows)

    def nudge(self, ref: str, now: float) -> None:
        """Count Station doesn't remove codes by itself at Snow White, so a repair can be done with its code still on:
        after 14 days, ask the stockroom (Ana, Sonja) in #stockroom; weekly after that. No wearer names."""
        rec = self.update(ref, nudged_at=now)
        days = int((now - rec.get("entered_at", now)) // 86400)
        what = "; ".join(" ".join(x for x in (g["item"] or "garment", g["size"], f"#{g['barcode']}" if g["barcode"] else "") if x)
                         for g in rec["garments"][:5])
        text = (f"⏳ *{ref}* · {self.acct_name(rec['account'])}: *{rec['code']} {CODES[rec['code']]}* went on in Alliant "
                f"{days} days ago and is still on ({what or rec['scope']}). Was it fixed? If yes, clear the code "
                "(Inquiry > Garment > Clear Srvc. Cd.) and I'll close the request and tell the customer. If it can't be "
                "fixed, tell the office in the card's thread.")
        from .new_accounts import STOCKROOM_ID, STOCKROOM_PEOPLE
        if test_mode():  # never pings the stockroom from a test: the note goes in the test card's thread
            if rec.get("ts") and self.channel():
                self.desk.slack.chat_postMessage(channel=self.channel(), thread_ts=rec["ts"], text="🧪 TEST, would post in "
                                                 "#stockroom: " + text, unfurl_links=False)
            return
        tags = " ".join(f"<@{u.strip()}>" for u in STOCKROOM_PEOPLE.split(",") if u.strip())
        self.desk.slack.chat_postMessage(channel=STOCKROOM_ID, text=f"{tags} {text}", unfurl_links=False)
        if rec.get("ts") and self.channel():
            self.desk.slack.chat_postMessage(channel=self.channel(), thread_ts=rec["ts"], unfurl_links=False,
                                             text=f"⏳ Still open after {days} days: asked the stockroom in #stockroom.")

    def close(self, ref: str) -> None:
        rec = self.update(ref, status="done", done_at=time.time())
        what = " and ".join(" ".join(x for x in (g["item"].lower() or "garment", f"(size {g['size']})" if g["size"] else "") if x)
                            for g in rec["garments"][:3]) or "garments"
        fixed = {"WR": "replaced", "SZ": "checked for size", "AA": "checked", "ST": "treated"}.get(rec["code"], "repaired")
        brand = "The Laundry Group" if rec.get("brand") == "TLG" or "-2-" in rec["account"] else "Snow White Linen"
        msg = f"{brand}: your {what} {'has' if len(rec['garments']) == 1 else 'have'} been {fixed} and go back with your " \
              f"next delivery. Ref {ref}. Reply here if anything else needs fixing."
        told = []
        if rec["contact"].get("phone"):
            told.append(self.send_text(rec["contact"]["phone"], msg))
        if rec["contact"].get("email"):
            told.append(self.send_email(rec["contact"]["email"], f"{brand}: request {ref} is done", msg))
        route = next((c.route for c in self.desk.alliant.customers if c.account == rec["account"]), "")
        ch = self.desk.routes.get(f"route-{route}") if route else None
        if ch and not test_mode():
            self.desk.slack.chat_postMessage(channel=ch, text=f"✔️ {ref} done for {self.acct_name(rec['account'])}: "
                                             f"{rec['code']} cleared in Alliant; the {what} go back on this delivery.")
            told.append(f"#route-{route}")
        self.card(rec, f"✔️ Done {datetime.now(ALASKA):%-m/%-d}: the code came off in Alliant. Told: {', '.join(t for t in told if t) or 'nobody (no contact on the request)'}.")

    def send_text(self, phone: str, msg: str) -> str:
        if test_mode():
            self.tell_scott(f"🧪 TEST, would text {phone[-4:].rjust(len(phone), '•')}: {msg}")
            return "Scott (test)"
        from .sms import send_sms
        send_sms(phone, msg)
        return "the requester by text"

    def send_email(self, to: str, subject: str, msg: str) -> str:
        if test_mode():
            self.tell_scott(f"🧪 TEST, would email {to}: {msg}")
            return "Scott (test)"
        from . import bird
        bird.send(to, subject, msg)
        return "the requester by email"

    def tell_scott(self, text: str) -> None:
        owner = os.environ.get("OWNER_SLACK_ID", "U4KFSQCDQ").split(",")[0].strip()
        try:
            self.desk.slack.chat_postMessage(channel=owner, text=text, unfurl_links=False)
        except Exception as e:
            print(time.strftime("%H:%M"), "uniform: Scott not told:", str(e)[:120], flush=True)


def _ago(t: float) -> str:
    d = int((time.time() - t) // 86400)
    return "today" if d == 0 else f"{d} day{'s' if d != 1 else ''} ago"

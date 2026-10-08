"""AR collections: build the working file, today's batch and the reminder drafts.

  python3 -m ar_collections.build <data_dir> <out_dir> [YYYY-MM-DD]

<data_dir> holds what the agent pulls each morning (read-only sources):
  ns_open.json          NetSuite open CustInvc rows: id, tranid, externalid, sub, code, nm, parent,
                        trandate, duedate, memo, bal  (SuiteQL, see README)
  customer_detail.csv   Alliant Feed: contacts, route, delivery days, active
  open_ar.csv           Alliant Feed: unapplied credits (types P/F)
  payments_YYYY-MM.csv  Alliant Feed: last payment date (any months present are used)
  receipts_to_post.csv  Alliant Feed: Sonja's list (bank receipts not yet applied)
  log/*.csv, log.csv    AR Collections log (optional): customer,date,action,amount,response
  holds.csv             AR Collections/holds.csv (optional): customer,reason  (Scott/Sonja holds)

Rules are the ones in "AR Collections Agent - brief 2026-10-08.md" and its v2. Nothing here writes to
NetSuite or Alliant; output is files for Sonja and Scott.
"""
import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

SUBS = {8: "SW", 9: "TLG"}
COMPANY = {"SW": "Snow White Linen", "TLG": "The Laundry Group"}
MAILBOX = {"SW": "accounting@snowwhitelinen.com", "TLG": "accounting@thelaundrygroup.com"}
# Unifirst subcontract accounts: list balances for Scott, never contact.
UNIFIRST = re.compile(r"SAFEWAY|COSTCO|\bLSG\b|LSG-|UNIFIRST|ALBERTSONS|CARRS", re.I)
# COD / route remnant accounts.
COD_ACCOUNTS = {str(n) for n in range(990001, 990016)} | {"1030"}
# Brief: check these before any contact (receipts waiting, possible misapplications).
CHECK_FIRST = {
    "6001": "Alaska Regional / HCA: big July HCA receipts and a $55K HCA receipt on Sonja's list; "
            "ask Scott whether old items are misapplications before contacting",
    "6002": "Lakefront: $21.7K + $8.2K receipts waiting to be applied; check those first",
    "5013": "Lakefront (SW): $8.2K receipt waiting to be applied; check first",
    "6003": "SCF departments: $725 items may be applied to the wrong department; check first",
    # Billed to another linen company, like the Unifirst accounts. Asked Scott 10/8 whether to contact.
    "1864": "billed to Cintas (national accounts: NTW/TCI Tire, Duluth Trading, Key Bank, ...): ask Scott whether this is a subcontract like Unifirst",
    "5041": "Defense Commissaries billed to Vestis (AP FSSC): ask Scott whether this is a subcontract like Unifirst",
}
GENERIC = {"ALASKA", "ANCHORAGE", "RESTAURANT", "SERVICES", "SERVICE", "COMPANY", "HOTEL", "FOODS", "GROUP",
           "CORPORATION", "PAYMENT", "CREDIT", "PREAUTHORIZED", "GENERAL", "NORTH", "SOUTH", "COFFEE", "SUPPLY"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WRITE_OFF_BEFORE = date(2025, 1, 1)  # items dated 2022-2024: list for Scott, don't chase


def d(s):
    s = (s or "").strip()[:10]
    if not s:
        return None
    if "/" in s:
        return datetime.strptime(s, "%m/%d/%Y").date()
    return date.fromisoformat(s)


KEEP_UPPER = {"MV", "US", "AK", "LLC", "ER", "II", "III", "DOI", "SCF", "ANTHC", "HCA", "NTW", "TCI", "VFW", "AMC",
              "BP", "PTP", "TLG", "SW", "USA", "ATM", "EVS", "NOG"}


def nice(name):
    return " ".join(w if w.strip("().,#&'") in KEEP_UPPER else w.title() for w in name.split())


def money(x):
    return f"${x:,.2f}"


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def contact_day(delivery):
    """Business day before the delivery day (Mon delivery -> previous Fri)."""
    return {"Mon": "Fri", "Tue": "Mon", "Wed": "Tue", "Thu": "Wed", "Fri": "Thu", "Sat": "Fri"}.get(delivery)


def week_start(day):
    return day - timedelta(days=day.weekday())


def load_invoices(data_dir):
    rows = []
    for r in json.load(open(os.path.join(data_dir, "ns_open.json"))):
        co = SUBS.get(r["sub"])
        if not co:
            continue
        ext = r.get("externalid") or ""
        memo = r.get("memo") or ""
        if ext.startswith("ALL-OPEN"):
            m = re.search(r"Alliant (\d+)(?:-\d+)? dated (\d{4}-\d\d-\d\d)", memo)
            inv, orig = (m.group(1), d(m.group(2))) if m else (r["tranid"], d(r["trandate"]))
            due = orig + timedelta(days=30)
        else:
            inv, orig, due = r["tranid"], d(r["trandate"]), d(r["duedate"])
        code = r["code"]
        account = code.split("-")[0]
        parent = r.get("parent") or f"{account} {r['nm']}"
        rows.append({"co": co, "code": code, "name": r["nm"], "account": account,
                     "group": (co, parent.split(" ")[0]), "parent_name": parent.split(" ", 1)[-1],
                     "invoice": inv, "date": orig, "due": due, "balance": float(r["bal"])})
    return rows


def main(data_dir, out_dir, today):
    os.makedirs(out_dir, exist_ok=True)
    invoices = load_invoices(data_dir)

    # Alliant customers by code (codes are reused: prefer the active row).
    cust = {}
    for c in read_csv(os.path.join(data_dir, "customer_detail.csv")):
        key = (c["cust_code"])
        if key not in cust or (c["active"] == "1" and cust[key]["active"] != "1"):
            cust[key] = c
    code_to_id = {k: v["cust_id"] for k, v in cust.items()}
    by_group_codes = defaultdict(set)
    for k, c in cust.items():
        co = "SW" if c["branch"] == "1" else "TLG" if c["branch"] == "2" else None
        if co:
            by_group_codes[(co, c["account"])].add(k)

    # Last payment (Alliant) by cust_id; write-offs don't count.
    last_pay = {}
    for path in glob.glob(os.path.join(data_dir, "payments_*.csv")):
        for p in read_csv(path):
            if p["type"] == "W" or float(p["pay_amount"] or 0) <= 0:
                continue
            pd = d(p["pay_date"])
            if pd and pd > last_pay.get(p["cust_id"], date.min):
                last_pay[p["cust_id"]] = pd
    pay_months = sorted(os.path.basename(p)[9:16] for p in glob.glob(os.path.join(data_dir, "payments_*.csv")))

    # Unapplied credits (Alliant P/F items with a negative balance) by code, and what Alliant still shows
    # open per invoice: a reminder never lists an invoice Alliant has closed since NetSuite was posted.
    credits = defaultdict(float)
    alliant_open = defaultdict(float)
    for r in read_csv(os.path.join(data_dir, "open_ar.csv")):
        bal = float(r.get("balance") or 0)
        if r.get("type") in ("P", "F") and bal < 0:
            credits[r["cust_code"]] += -bal
        else:
            alliant_open[(r["cust_code"], r["invoice"])] += bal

    # Receipts at the bank not yet applied in Alliant (Sonja's list, part a) -> hold the account.
    pending = defaultdict(list)
    unmatched_payers = []
    for r in read_csv(os.path.join(data_dir, "receipts_to_post.csv")):
        if not r["list"].startswith("a"):
            continue
        codes = set(re.findall(r"\b(\d{4})-([12])-\d{5}\b", r.get("suggested_invoices") or ""))
        for acct, br in codes:
            pending[("SW" if br == "1" else "TLG", acct)].append(f"{r['date']} {money(float(r['amount']))}")
        if not codes:
            unmatched_payers.append((r["payer"].upper(), f"{r['date']} {money(float(r['amount']))}"))

    holds = {row["customer"].strip(): row.get("reason", "") for row in read_csv(os.path.join(data_dir, "holds.csv"))}
    # The Dropbox connector can only create files, so the log is one file per day in log/ (plus log.csv if
    # someone keeps one by hand).
    log = read_csv(os.path.join(data_dir, "log.csv"))
    for path in sorted(glob.glob(os.path.join(data_dir, "log", "*.csv"))):
        log += read_csv(path)

    groups = defaultdict(list)
    for inv in invoices:
        groups[inv["group"]].append(inv)

    work, unifirst, writeoffs, batch, sonja_fix = [], [], [], [], []
    for (co, acct), items in groups.items():
        name = items[0]["parent_name"]
        key = f"{co} {acct}"
        open_amt = sum(i["balance"] for i in items)
        age = lambda i: (today - i["date"]).days
        past_due = [i for i in items if i["due"] < today]
        over45 = sum(i["balance"] for i in items if age(i) > 45)
        over90 = sum(i["balance"] for i in items if age(i) > 90)
        oldest = min(i["date"] for i in items)
        codes = by_group_codes.get((co, acct), set()) | {i["code"] for i in items}
        lp = max((last_pay.get(code_to_id.get(c, ""), date.min) for c in codes), default=date.min)
        lp = None if lp == date.min else lp
        no_pay = lp is None or (today - lp).days >= 45
        credit = sum(credits.get(c, 0) for c in codes)
        # A bank receipt with no suggested account: hold any customer whose name shows up in the payer text.
        words = [w for w in re.findall(r"[A-Z]{5,}", name.upper()) if w not in GENERIC]
        for payer, what in unmatched_payers:
            if any(w in payer for w in words):
                pending[(co, acct)].append(what + " (matched by name)")

        if UNIFIRST.search(name) or any(UNIFIRST.search(i["name"]) for i in items):
            unifirst.append({"company": co, "account": acct, "name": name, "open_ar": round(open_amt, 2),
                             "over_45": round(over45, 2), "over_90": round(over90, 2), "oldest_item": oldest})
            continue
        if acct in COD_ACCOUNTS:
            continue
        old = [i for i in items if i["date"] < WRITE_OFF_BEFORE]
        for i in old:
            writeoffs.append({"company": co, "account": acct, "customer": i["name"], "code": i["code"],
                              "invoice": i["invoice"], "date": i["date"], "balance": round(i["balance"], 2)})
        chase = []
        for i in past_due:
            left = min(i["balance"], alliant_open.get((i["code"], i["invoice"]), 0.0))
            if i["date"] >= WRITE_OFF_BEFORE and left > 0.005:
                chase.append(dict(i, balance=left))
        chase_over45 = [i for i in chase if age(i) > 45]
        if over45 <= 0:
            continue  # brief: working file is the over-45 customers

        # Contact and schedule, from the parent account (dept 00000) first, else the other active depts.
        depts = sorted((cust[c] for c in codes if c in cust), key=lambda c: c["dept"])
        active = [c for c in depts if c["active"] == "1"]
        email = next((c["email"] for c in depts if c["email"].strip()), "")
        contact_name = next((c["contact"] for c in depts if c["email"].strip()), "")
        days = []
        for c in (active[:1] if active and active[0]["delivery_days"] else active):
            days += [x for x in c["delivery_days"].split(";") if x]
        if not days:
            for c in active:
                days += [x for x in c["delivery_days"].split(";") if x]
        delivery = min(days, key=DAYS.index) if days else ""
        if delivery == "Sun":
            delivery = ""
        cday = contact_day(delivery) if delivery else "Fri"

        # Escalation from log.csv: weeks with a contact so far.
        mine = [r for r in log if r["customer"] in (key, name)]
        contact_weeks = sorted({week_start(d(r["date"])) for r in mine
                                if r["action"] in ("reminder", "follow-up", "call")})
        contacted_this_week = week_start(today) in contact_weeks
        step = len([w for w in contact_weeks if w < week_start(today)]) + 1

        reason = ""
        if key in holds or name in holds:
            reason = "hold: " + (holds.get(key) or holds.get(name) or "Scott/Sonja hold")
        elif acct in CHECK_FIRST:
            reason = "check first: " + CHECK_FIRST[acct]
        elif pending.get((co, acct)):
            reason = "receipt at bank not yet applied: " + "; ".join(pending[(co, acct)])
        elif not active:
            reason = "inactive in Alliant: Scott to decide (not a route customer)"
        elif not chase_over45:
            reason = "only 2022-2024 items over 45 days: write-off candidates for Scott"
        elif not email:
            reason = "no billing email in Alliant: Sonja to call or add one"

        group = "no payment 45+ days" if no_pay else "paying, has old items"
        action = ("hold" if reason else
                  "friendly reminder" if step == 1 else "follow-up" if step == 2 else "flag for phone call")
        row = {"company": co, "account": acct, "name": name, "email": email, "contact": contact_name,
               "delivery_day": delivery or "(none)", "contact_day": cday,
               "open_ar": round(open_amt, 2), "past_due": round(sum(i["balance"] for i in past_due), 2),
               "over_45": round(over45, 2), "over_90": round(over90, 2), "oldest_item": oldest,
               "last_payment": lp or f"none since {pay_months[0] if pay_months else '?'}",
               "group": group, "unapplied_credit": round(credit, 2), "escalation_step": step,
               "suggested_action": action, "hold_reason": reason}
        work.append(row)
        if not delivery and active:
            sonja_fix.append({"company": co, "account": acct, "name": name, "issue": "no delivery day in Alliant"})
        if reason.startswith("no billing email"):
            sonja_fix.append({"company": co, "account": acct, "name": name,
                              "issue": f"no billing email in Alliant ({money(row['past_due'])} past due)"})

        todays_contact_day = DAYS[today.weekday()]
        if (not reason and cday == todays_contact_day and not contacted_this_week):
            batch.append((row, chase, credit))

    work.sort(key=lambda r: -r["over_45"])
    write(os.path.join(out_dir, "working_file.csv"), work)
    write(os.path.join(out_dir, "unifirst_balances.csv"), sorted(unifirst, key=lambda r: -r["open_ar"]))
    write(os.path.join(out_dir, "writeoff_candidates.csv"), sorted(writeoffs, key=lambda r: (r["date"], r["account"])))
    write(os.path.join(out_dir, "sonja_fix_in_alliant.csv"), sonja_fix)

    # One message per billing contact: accounts that share a billing email are combined.
    merged = {}
    for row, chase, credit in batch:
        k = (row["company"], row["email"].lower())
        if k in merged:
            m = merged[k]
            m[0] = dict(m[0], name=m[0]["name"] + " / " + row["name"], account=m[0]["account"] + "+" + row["account"],
                        group=m[0]["group"] if m[0]["group"] == row["group"] else "paying, has old items")
            m[1] = m[1] + chase
            m[2] += credit
        else:
            merged[k] = [row, chase, credit]
    batch = list(merged.values())

    ddir = os.path.join(out_dir, "drafts", today.isoformat())
    os.makedirs(ddir, exist_ok=True)
    brows = []
    for row, chase, credit in sorted(batch, key=lambda b: -sum(i["balance"] for i in b[1])):
        fname = re.sub(r"[^A-Za-z0-9]+", " ", f"{row['company']} {row['account']} {row['name']}").strip() + ".md"
        with open(os.path.join(ddir, fname), "w") as f:
            f.write(draft(row, chase, credit, today))
        brows.append({"company": row["company"], "account": row["account"], "name": row["name"],
                      "to": row["email"], "from": MAILBOX[row["company"]], "step": row["suggested_action"],
                      "past_due": round(sum(i["balance"] for i in chase), 2), "group": row["group"],
                      "delivery_day": row["delivery_day"], "draft": fname,
                      "note": "billing email looks like an invoice-intake portal; a person may be better"
                      if re.search(r"@[^;, ]*(invoic|ghx|coupa|ariba|tungsten|basware)", row["email"], re.I) else ""})
    write(os.path.join(out_dir, f"batch_{today.isoformat()}.csv"), brows)
    write(os.path.join(out_dir, f"log_{today.isoformat()}.csv"), [
        {"customer": f"{b['company']} {a}", "date": today.isoformat(),
         "action": "reminder" if b["step"] == "friendly reminder" else b["step"],
         "amount": b["past_due"], "response": "drafted for Sonja"}
        for b in brows for a in b["account"].split("+")])
    return work, unifirst, writeoffs, brows, sonja_fix


def draft(row, chase, credit, today):
    co = row["company"]
    company = COMPANY[co]
    chase = sorted(chase, key=lambda i: i["date"])
    total = sum(i["balance"] for i in chase)
    person = row["contact"].strip()
    greet = f"Hi {person.split()[0].title()}," if person and "@" not in person and not re.search(
        r"\b(AP|A/P|ACCOUNTS?|PAYABLES?|INBOX|BILLING|INVOICES?|ACCOUNTING|OFFICE|MANAGER|DEPT|TEAM)\b",
        person, re.I) else "Hello,"
    if len(chase) <= 30 and len({i["code"] for i in chase}) == 1:
        table = "| Invoice | Date | Balance |\n|---|---|---|\n" + "\n".join(
            f"| {i['invoice']} | {i['date'].strftime('%m/%d/%Y')} | {money(i['balance'])} |" for i in chase)
    elif len(chase) <= 30:
        table = "| Location | Invoice | Date | Balance |\n|---|---|---|---|\n" + "\n".join(
            f"| {nice(i['name'])} | {i['invoice']} | {i['date'].strftime('%m/%d/%Y')} | {money(i['balance'])} |"
            for i in chase)
    else:
        per = defaultdict(lambda: [0, 0.0, None])
        for i in chase:
            p = per[i["name"]]
            p[0] += 1
            p[1] += i["balance"]
            p[2] = min(p[2] or i["date"], i["date"])
        table = "| Location | Invoices | Oldest | Balance |\n|---|---|---|---|\n" + "\n".join(
            f"| {nice(n)} | {c} | {o.strftime('%m/%d/%Y')} | {money(b)} |" for n, (c, b, o) in sorted(per.items()))
    subject_name = nice(row["name"])
    if row["suggested_action"] == "follow-up":
        subject = f"Following up: open invoices for {subject_name}"
        opener = ("I'm following up on my note from last week about the open invoices on your account. "
                  "I know these can get buried, so here they are again.")
    elif row["group"] == "paying, has old items":
        subject = f"A few invoices that may have slipped through - {subject_name}"
        opener = ("Thank you for your recent payments. While reconciling the account I noticed a few older "
                  "invoices that look like they may have slipped through.")
    else:
        subject = f"Open invoices for {subject_name} - copies available"
        opener = ("I'm reaching out because some invoices on your account are still showing open. "
                  "It's possible they never made it to the right inbox.")
    if len(chase) > 30:
        table += "\n\nI can send a full statement with every invoice listed; just let me know."
    credit_note = (f"\nI also see an unapplied credit of {money(credit)} on your account. I'll make sure it's "
                   f"applied, which brings the total down.\n" if credit > 0.005 else "")
    return f"""<!-- To: {row['email']} | From: {MAILBOX[co]} | {row['suggested_action']} | {row['group']} -->
**Subject:** {subject}

{greet}

{opener}

{table}

**Total: {money(total)}**
{credit_note}
If you need copies of any of these, just reply and I'll send them right over. If they're already paid,
please let me know the date and check or reference number so I can match it up.

Paying is easy, whichever works best for you:
- our online customer portal
- credit card (call or reply and we'll take it over the phone)
- ACH
- check, mailed to our office

Thank you for your business. We appreciate you!

Sonja Burke
Accounts Receivable
{company}
"""


def write(path, rows):
    with open(path, "w", newline="") as f:
        if not rows:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    today = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date.today()
    work, unifirst, writeoffs, batch, fix = main(sys.argv[1], sys.argv[2], today)
    print(f"working file: {len(work)} customers; batch {today}: {len(batch)}; "
          f"unifirst: {len(unifirst)}; write-off items: {len(writeoffs)}; Sonja fixes: {len(fix)}")

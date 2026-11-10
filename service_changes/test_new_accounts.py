"""#new-accounts: Donna's side of the Sales Handoff protocol v3.1 (fake Slack, fake Alliant)."""
import copy
import json

import pytest

from . import new_accounts as na
from .test_desk import setup  # noqa: F401

REQ = {
    "type": "alliant_entry_request", "version": "3.1", "request_id": "opp-1-1", "requested_by": "Sonja Burke",
    "company": "SW", "opportunity_id": "006X", "account_name": "Team CC", "existing_alliant_account": "2058",
    "location_name": "Team CC - Wasilla",
    "service_address": {"line1": "491 S Willow", "line2": "", "city": "Wasilla", "state": "AK", "zip": "99654"},
    "location_contact": {"name": "Pat", "phone": "9075550000", "email": "pat@teamcc.com"}, "invoice_email": "ap@teamcc.com",
    "salesperson": "Jesse James Tingley", "contract": {"number": "00002301", "start_date": "2026-11-17", "term_months": 36},
    "install_date": "2026-11-17", "route": 6, "delivery_days": ["Tue"],
    "rates": {"service_charge_pct": 18, "inventory_charge_pct": 13, "fuel_charge": 6.00},
    "lines": [{"sku": "5-03-01", "description": "TOWEL UTILITY BLUE", "billing_basis": "Usage", "quantity": 40, "unit_price": 0.29, "frequency": "weekly"},
              {"sku": "10-07-05", "description": "MAT CHARCOAL HEATHER 4X8", "billing_basis": "Usage", "quantity": 1, "unit_price": 13.25, "frequency": "EOW"}],
    "expected_invoice": {"period_weeks": 4, "lines": [{"sku": "5-03-01", "amount": 46.40}, {"sku": "10-07-05", "amount": 26.50}],
                         "service_charge": 13.12, "inventory_charge": 0.0, "fuel": 24.0, "total": 110.02},
    "phase": 1, "needs_stock_ok": False, "optional": {"customer_number": None, "delivery_instructions": "Back door"},
}


def msg(obj, user="UHANDOFF", thread="100.0"):
    return {"type": "message", "channel": na.CHANNEL, "user": user, "ts": "101.0", "thread_ts": thread,
            "text": "@Donna please enter this location.\n```json\n" + json.dumps(obj) + "\n```"}


@pytest.fixture
def d(setup, tmp_path, monkeypatch):
    slack, desk = setup
    desk.data_dir = str(tmp_path)
    slack.msgs.setdefault(na.CHANNEL, [])
    slack.msgs[na.CHANNEL].append({"ts": "100.0", "user": "UHANDOFF", "text": "Team CC - Wasilla handoff"})
    monkeypatch.setenv("SALES_HANDOFF_BOT_ID", "UHANDOFF")
    return slack, desk


def results(slack):
    return [json.loads(m["text"].split("```json\n")[1].split("\n```")[0]) for m in slack.msgs[na.CHANNEL] if "```json" in m["text"]
            and m.get("user") == "UBOT"]


def test_only_the_sales_handoff_bot_is_acted_on(d, monkeypatch):
    slack, desk = d
    assert desk.new_accounts().on_message(msg(REQ, user="U2")) == "ignored"  # Sonja typing JSON
    monkeypatch.setenv("SALES_HANDOFF_BOT_ID", "")
    assert desk.new_accounts().on_message(msg(REQ)) == "ignored"  # not set up yet: nothing at all
    assert desk.jobs().next() is None and not results(slack)


def test_missing_fields_get_waiting_with_what_is_missing(d):
    slack, desk = d
    bad = copy.deepcopy(REQ)
    bad["invoice_email"] = ""
    bad["lines"][1]["frequency"] = "sometimes"
    assert desk.new_accounts().on_message(msg(bad)) == "waiting: missing"
    [r] = results(slack)
    assert r["status"] == "waiting" and "invoice_email" in r["notes"] and "lines[1].frequency" in r["notes"]
    assert desk.jobs().next() is None


def test_a_good_request_is_queued_with_house_rules_and_defaults(d):
    slack, desk = d
    assert desk.new_accounts().on_message(msg(REQ)).startswith("queued")
    job = desk.jobs().next()
    assert job["kind"] == "new_account" and job["request_id"] == "opp-1-1"
    t = job["ticket"]
    assert "NEW LOCATION (customer) under existing account 2058, branch 1" in t
    assert "price type 8" in t and "frequency 7" in t and "EOW" in t and "default" in t and "Back door" in t


def test_entered_is_read_back_live_and_the_invoice_checked(d, monkeypatch):
    slack, desk = d
    from . import alliant_live
    rows = [{"sku": "5-03-01", "autocount": 40, "inventory": 40, "unit_price": 0.29, "price_type": 8, "frequency": "7", "d_tue": 1},
            {"sku": "10-07-05", "autocount": 1, "inventory": 1, "unit_price": 13.25, "price_type": 8, "frequency": "6", "d_tue": 1}]
    monkeypatch.setattr(alliant_live, "lookup", lambda name, data_dir, fresh=False, **kw:
                        {"lookup": name, "params": kw, "rows": rows, "source": "live", "at": "", "note": ""})
    desk.new_accounts().on_message(msg(REQ))
    job = desk.jobs().next()
    desk.jobs().result(job["id"], "entered", "customer_number=2058-1-00001; stop_sequence=14; codes=5-03-01:7,10-07-05:6")
    r = results(slack)[-1]
    assert r["status"] == "entered" and r["customer_number"] == "2058-1-00001" and r["stop_sequence"] == 14
    assert r["codes_chosen"] == [{"sku": "10-07-05", "frequency_code": "6"}] and r["verified_live"]
    assert r["invoice_check"]["alliant_lines_total"] == 72.90 and r["invoice_check"]["differences"] == []
    # a typo in Alliant: waiting, with the difference
    rows[0]["unit_price"] = 0.39
    desk.new_accounts().on_message(msg({**REQ, "request_id": "opp-2-1"}))
    job = desk.jobs().next()
    desk.jobs().result(job["id"], "entered", "customer_number=2058-1-00002; stop_sequence=15; codes=5-03-01:7,10-07-05:6")
    r = results(slack)[-1]
    assert r["status"] == "waiting" and r["invoice_check"]["differences"][0]["what"] == "line 5-03-01"
    assert r["invoice_check"]["differences"][0]["alliant"] == 62.4


def test_a_repeat_is_never_entered_twice(d, monkeypatch):
    slack, desk = d
    bad = {**REQ, "invoice_email": ""}
    desk.new_accounts().on_message(msg(bad))
    assert desk.new_accounts().on_message(msg(bad)) == "repeat"
    assert len(results(slack)) == 2 and results(slack)[0] == results(slack)[1]


def test_stock_ok_releases_a_prepared_entry(d):
    slack, desk = d
    desk.new_accounts().on_message(msg({**REQ, "needs_stock_ok": True}))
    assert results(slack)[-1]["status"] == "waiting" and desk.jobs().next() is None
    desk.new_accounts().on_message(msg({"type": "stock_ok", "version": "3.1", "request_id": "opp-1-1",
                                        "confirmed_by": "Ana Faamita", "install_date": "2026-11-19"}))
    job = desk.jobs().next()
    assert job["kind"] == "new_account" and "install 2026-11-19" in job["ticket"]


def test_welcome_email(d, monkeypatch):
    slack, desk = d
    from . import bird
    w = {"type": "welcome_email_request", "version": "3.1", "request_id": "opp-1-welcome-1", "company": "SW",
         "customer_number": "2058-1-00001", "to": {"name": "Pat Lee", "email": "pat@teamcc.com"},
         "install_date": "2026-11-17", "route_day": "Tuesday", "frequency": "Weekly"}
    monkeypatch.delenv("BIRD_SALES_API_KEY", raising=False)
    assert desk.new_accounts().on_message(msg(w)) == "failed" and "BIRD_SALES_API_KEY" in results(slack)[-1]["notes"]
    sent = []
    monkeypatch.setenv("BIRD_SALES_API_KEY", "bk_us1_sales")
    monkeypatch.setattr(bird, "_call", lambda m, p, body=None, key="", api_key="", **kw: sent.append((body, key, api_key)) or {"id": "msg_1"})
    assert desk.new_accounts().on_message(msg(w)) == "sent"
    body, key, api_key = sent[0]
    assert api_key == "bk_us1_sales" and key == "welcome-opp-1-welcome-1" and body["category"] == "transactional"
    assert "Tuesday, November 17" in body["text"] and "866-TAP-TEXT" in body["text"] and "$" not in body["text"]
    assert desk.new_accounts().on_message(msg(w)) == "repeat" and len(sent) == 1  # once per request_id

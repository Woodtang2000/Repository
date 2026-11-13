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
                         "service_charge": 13.12, "inventory_charge": 9.48, "fuel": 24.0, "total": 119.50},
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


def test_dondees_is_never_entered(d):
    slack, desk = d
    req = dict(copy.deepcopy(REQ), account_name="Dondee\u2019s Laundry", location_name="Dondee's - Eagle River")
    assert desk.new_accounts().on_message(msg(req)) == "failed: Dondee's"
    assert "not in Alliant" in slack.msgs[na.CHANNEL][-1]["text"]
    assert not desk.new_accounts().load().get(req["request_id"], {}).get("req")


def test_test_channel_never_reaches_alliant_or_customers(d, monkeypatch):
    slack, desk = d
    monkeypatch.setenv("NEW_ACCOUNTS_TEST", "on")
    sent = []
    monkeypatch.setattr(na, "send_welcome", lambda *a: sent.append(a) or "x")
    monkeypatch.setenv("BIRD_SALES_API_KEY", "k")
    assert desk.new_accounts().on_message(msg(REQ)) == "test: not queued"
    assert not desk.jobs().load()["jobs"]
    assert "TEST MODE" in slack.msgs[na.CHANNEL][-1]["text"]
    w = {"type": "alliant_wearer_request", "request_id": "w-1", "customer_number": "5045-1-00000",
         "wearers": [{"name": "Pat", "garments": [{"sku": "2-0413WH", "size": "L", "changes": 11}]}]}
    assert desk.new_accounts().on_message(msg(w)) == "test: wearers not queued"
    e = {"type": "welcome_email_request", "request_id": "e-1", "to": {"name": "Pat", "email": "pat@teamcc.com"}}
    assert desk.new_accounts().on_message(msg(e)) == "test"
    assert not desk.jobs().load()["jobs"] and not sent


def test_garments_in_phase_1_and_conditions_are_asked_about(d):
    req = copy.deepcopy(REQ)
    req["lines"].append({"sku": "2-QP14NV", "description": "PANT FR", "billing_basis": "Usage", "quantity": 22,
                         "unit_price": 4.0, "frequency": "weekly"})
    req["optional"]["conditions"] = "$30 each prep charge on each new issue FR garment"
    gaps = na.missing(req)
    assert any(g.startswith("needs_stock_ok") for g in gaps)
    assert any("prep charge" in g for g in gaps)
    req["needs_stock_ok"], req["optional"]["conditions"] = True, None
    assert na.missing(req) == []
    assert na.price_type(req["lines"][-1]) == 9 and na.price_type(dict(req["lines"][-1], billing_basis="Inventory")) == 9
    text = na.instructions(req)
    assert "2-QP14NV PANT FR: GARMENT, account catalog only" in text and "price type 9" in text


def _watch(d, monkeypatch, rows):
    slack, desk = d
    from . import alliant_live
    monkeypatch.setattr(alliant_live, "lookup", lambda *a, **k: {"source": "live", "rows": rows, "note": ""})
    na_ = desk.new_accounts()
    with na_.lock:
        data = na_.load()
        data["opp-1-1"] = {"req": dict(REQ, lines=REQ["lines"] + [{"sku": "2-QP14NV", "quantity": 22}]),
                           "result": {"customer_number": "5050-1-00000"}, "thread": "100.0"}
        na_.save(data)
    slack.msgs.setdefault(na.STOCKROOM_ID, [])
    assert na_.start_watch("5050-1-00000") == "watching"
    return slack, desk, na_


def row(w, inv, auto, issued, order=0):
    return {"wearer": w, "name": f"Wearer {w}", "sku": "2-QP14NV", "item": "PANT FR", "inventory": inv,
            "autocount": auto, "issued": issued, "on_order": order}


def test_garment_watch_raises_an_auto_count_that_is_behind(d, monkeypatch):
    slack, desk, w = _watch(d, monkeypatch, [row("1", 11, 5, 11), row("2", 11, 11, 11)])
    assert w.check_garments("5050-1-00000") == "fix"
    jobs = [j for j in desk.jobs().load()["jobs"].values() if j["kind"] == "garment_fix"]
    assert len(jobs) == 1 and "Auto Count 5 → 11" in jobs[0]["ticket"]
    posts = [m["text"] for m in slack.msgs[na.STOCKROOM_ID]]
    assert "new garment account" in posts[0] and "<@U04CX5DR1NV>" in posts[0] and "raising these auto counts" in posts[-1]
    assert not any("$" in p for p in posts)  # counts only, never prices
    assert w.check_garments("5050-1-00000") == "nothing new"  # one fix at a time
    assert w.job_done({**jobs[0], "status": "entered", "note": "1 line updated"}) == "garment fix entered"
    assert "Auto counts updated" in slack.msgs[na.STOCKROOM_ID][-1]["text"]


def test_garment_watch_reports_what_is_still_coming_and_finishes(d, monkeypatch):
    slack, desk, w = _watch(d, monkeypatch, [row("1", 11, 6, 6, order=5), row("2", 11, 11, 11)])
    assert w.check_garments("5050-1-00000") == "short"
    assert "6 of 11" in slack.msgs[na.STOCKROOM_ID][-1]["text"] or "17 of 22" in slack.msgs[na.STOCKROOM_ID][-1]["text"]
    from . import alliant_live
    monkeypatch.setattr(alliant_live, "lookup", lambda *a, **k: {"source": "live", "rows": [row("1", 11, 11, 11), row("2", 11, 11, 11)]})
    assert w.check_garments("5050-1-00000") == "done"
    assert w.watches()["5050-1-00000"]["done"] and "✅" in slack.msgs[na.STOCKROOM_ID][-1]["text"]


def test_garment_watch_asks_instead_of_lowering_a_bill(d, monkeypatch):
    slack, desk, w = _watch(d, monkeypatch, [row("1", 11, 11, 4, order=7), row("2", 11, 11, 11)])
    assert "ask" in w.check_garments("5050-1-00000")
    assert not [j for j in desk.jobs().load()["jobs"].values() if j["kind"] == "garment_fix"]
    assert "billing 11 while only 4 of 11 are issued" in "\n".join(m["text"] for m in slack.msgs[na.STOCKROOM_ID])


def test_a_given_frequency_code_is_used_and_must_fit():
    req = copy.deepcopy(REQ)
    req["lines"][1]["frequency_code"] = "6"
    assert na.missing(req) == []
    assert "frequency 6 (given in the request: use exactly this code)" in na.instructions(req)
    for bad, line in (("3", 1), ("B4", 1), ("5", 0)):  # E4W code on EOW, E12W code on EOW, a code on a weekly line
        r = copy.deepcopy(REQ)
        r["lines"][line]["frequency_code"] = bad
        assert any("frequency_code" in g for g in na.missing(r)), bad
    r = copy.deepcopy(REQ)
    r["lines"][1].update(frequency="E12W", frequency_code="b4")
    assert na.missing(r) == [] and "frequency B4 (given" in na.instructions(r)
    r["lines"][1]["frequency_code"] = "B11"  # Alliant's 12-week codes are B1-B4 only
    assert any("frequency_code" in g for g in na.missing(r))


def test_the_sales_handoff_bot_is_known_without_a_setting(monkeypatch):
    monkeypatch.delenv("SALES_HANDOFF_BOT_ID", raising=False)
    assert na.bot_id() == "U0CF01ML0AK"


def test_only_mats_go_less_than_weekly_and_logo_mats_need_stock_ok():
    r = copy.deepcopy(REQ)
    r["lines"][0]["frequency"] = "EOW"  # a towel, not a mat
    assert any("isn't a mat" in g for g in na.missing(r))
    r = copy.deepcopy(REQ)
    r["lines"].append({"sku": "3-LOGO-01", "description": "LOGO MAT 3X5", "billing_basis": "Usage", "quantity": 1,
                       "unit_price": 9.0, "frequency": "E4W"})
    gaps = na.missing(r)
    assert any(g.startswith("needs_stock_ok") for g in gaps) and not any("isn't a mat" in g for g in gaps)
    r["needs_stock_ok"] = True
    assert na.missing(r) == []


def test_wearer_names_stay_out_of_slack(d):
    slack, desk = d
    w = {"type": "alliant_wearer_request", "request_id": "w-9", "customer_number": "5045-1-00000", "wearer_count": 4}
    assert desk.new_accounts().on_message(msg(w)) == "waiting: endpoint"
    assert "4 wearers ready" in slack.msgs[na.CHANNEL][-1]["text"] and "isn't set up" in slack.msgs[na.CHANNEL][-1]["text"]
    w = {"type": "alliant_wearer_request", "request_id": "w-10", "customer_number": "5045-1-00000",
         "wearers": [{"name": "Pat Example", "garments": [{"sku": "2-0413WH", "size": "L", "changes": 11}]}]}
    desk.new_accounts().on_message(msg(w))
    job = desk.jobs().next()
    desk.jobs().result(job["id"], "entered", "Added Pat Example #12 with 11 coats")
    posts = "\n".join(m["text"] for m in slack.msgs[na.CHANNEL])
    assert "1 wearer entered" in posts and "Pat Example" not in posts


def test_deliveries_per_week_and_each_items_days():
    r = copy.deepcopy(REQ)
    r.update(deliveries_per_week=5, delivery_days=["Mon", "Tue", "Wed", "Thu", "Fri"])
    r["lines"][0]["times_per_week"] = 2                      # towels twice a week, days chosen at entry
    r["lines"][1]["days"] = ["Wed"]                          # the EOW mat on Wednesdays
    r["lines"].append({"sku": "10-01-01", "description": "MAT 3X5", "billing_basis": "Usage", "quantity": 2,
                       "unit_price": 5.0, "frequency": "weekly", "days": ["Mon", "Thu"]})
    r["lines"].append({"sku": "5-01-01", "description": "BAR TOWEL", "billing_basis": "Usage", "quantity": 100,
                       "unit_price": 0.2, "frequency": "weekly"})  # every delivery
    assert na.missing(r) == []
    text = na.instructions(r)
    assert "frequency 8 on 2 of the customer's days" in text
    assert "on Wed" in text and "frequency 8 on Mon, Thu" in text and "frequency 8 on Mon, Tue, Wed, Thu, Fri" in text
    one = copy.deepcopy(r)
    one["lines"][2]["days"] = ["Thu"]
    assert "frequency 7 on Thu" in na.instructions(one)
    for change, what in ((dict(deliveries_per_week=3), "deliveries_per_week"),):
        b = copy.deepcopy(r); b.update(change)
        assert any(g.startswith(what) for g in na.missing(b))
    b = copy.deepcopy(r); b["lines"][2]["days"] = ["Mon", "Sat"]
    assert any("isn't one of the account's delivery days" in g for g in na.missing(b))
    b = copy.deepcopy(r); b["lines"][1]["times_per_week"] = 2
    assert any("only fits a weekly line" in g for g in na.missing(b))
    b = copy.deepcopy(r); b["lines"][2]["times_per_week"] = 3
    assert any("2 days but times_per_week 3" in g for g in na.missing(b))
    tlg = copy.deepcopy(REQ)
    tlg.update(company="TLG", deliveries_per_week=6, delivery_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
    assert na.missing(tlg) == []


def test_wearers_are_fetched_from_the_endpoint(d, monkeypatch):
    slack, desk = d
    import io
    import urllib.request
    seen = {}
    def fake(req, timeout=0):
        seen.update(url=req.full_url, auth=req.headers.get("Authorization"))
        return io.BytesIO(json.dumps({"request_id": "w-11", "customer_number": "5045-1-00000", "wearers": [
            {"name": "Pat Example", "garments": [{"sku": "2-0413WH", "size": "L", "changes": 11}]},
            {"name": "Sam Example", "garments": [{"sku": "2-0413WH", "size": "M", "changes": 11}]}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setenv("SALES_HANDOFF_URL", "http://127.0.0.1:8099/api/handoff")
    monkeypatch.setenv("SALES_HANDOFF_KEY", "test-key")
    w = {"type": "alliant_wearer_request", "request_id": "w-11", "customer_number": "5045-1-00000", "wearer_count": 2}
    assert desk.new_accounts().on_message(msg(w)) == "queued wearers"
    assert seen == {"url": "http://127.0.0.1:8099/api/handoff/wearers/w-11", "auth": "Bearer test-key"}
    job = desk.jobs().next()
    assert "Pat Example" in job["ticket"]  # names go to her workstation only
    assert not any("Pat Example" in m["text"] for m in slack.msgs[na.CHANNEL])
    w2 = dict(w, request_id="w-12", wearer_count=3)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: io.BytesIO(json.dumps(
        {"request_id": "w-12", "wearers": [{"name": "A", "garments": []}]}).encode()))
    assert desk.new_accounts().on_message(msg(w2)) == "waiting: endpoint"
    assert "Slack said 3 wearers but the endpoint has 1" in slack.msgs[na.CHANNEL][-1]["text"]
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: io.BytesIO(json.dumps(
        {"request_id": "w-12", "wearers": [{"name": c, "garments": []} for c in "ABC"]}).encode()))
    assert desk.new_accounts().on_message(msg(w2)) == "queued wearers"  # posted again once it's fixed


def test_test_mode_shows_what_donna_would_enter_and_checks_the_math(d, monkeypatch):
    slack, desk = d
    monkeypatch.setenv("NEW_ACCOUNTS_TEST", "on")
    assert desk.new_accounts().on_message(msg(REQ)) == "test: not queued"
    res = json.loads(slack.msgs[na.CHANNEL][-1]["text"].split("```json")[1].split("```")[0])
    assert [x["price_type"] for x in res["lines"]] == [8, 8]
    assert res["codes_chosen"][0]["sku"] == "10-07-05" and "chosen at entry" in res["codes_chosen"][0]["frequency_code"]
    ic = res["invoice_check"]
    assert ic["donna"]["lines"] == 72.9 and ic["donna_total"] == 119.5 and ic["differences"] == []
    bad = dict(copy.deepcopy(REQ), request_id="opp-1-2")
    bad["expected_invoice"]["total"] = 110.02
    desk.new_accounts().on_message(msg(bad))
    res = json.loads(slack.msgs[na.CHANNEL][-1]["text"].split("```json")[1].split("```")[0])
    assert res["invoice_check"]["differences"] == [{"what": "total", "expected": 110.02, "donna": 119.5}]

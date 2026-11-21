"""Uniform requests (uniform.py): lookup without names, the card, approval, the Alliant job and closing the loop."""
import json
from types import SimpleNamespace

import pytest

from . import uniform
from .test_desk import setup  # noqa: F401

ROW = {"barcode": "000004729628", "cust_code": "1003-1-00002", "wearer": "37", "sku": "2-CT10NV",
       "item": "COVERALL NAVY", "size_a": "52", "size_b": "L", "service_code": ""}


@pytest.fixture
def u(setup, tmp_path, monkeypatch):
    slack, d = setup
    d.data_dir = str(tmp_path)
    monkeypatch.setenv("UNIFORM_API_KEY", "k" * 32)
    monkeypatch.setattr(d.alliant, "by_barcode", {ROW["barcode"]: ROW}, raising=False)
    monkeypatch.setattr(d.alliant, "customers", [SimpleNamespace(account="1003-1-00002", name="BOB'S SERVICE'S", route="12",
                                                                 service_days=["Wed"])])
    monkeypatch.setattr(d.alliant, "wearers", {"1003-1-00002": [SimpleNamespace(employee="37", first="Mike", last="Example",
                                                                                 name="Mike Example")]}, raising=False)
    d.channels["uniform-requests-test"], d.channels["uniform-requests"] = "CUT", "CU"
    slack.msgs["CUT"], slack.msgs["CU"] = [], []
    slack.chat_postEphemeral = lambda **kw: None
    monkeypatch.setattr(d, "is_office", lambda user: user == "U2")
    return slack, d


def tap(action_id, ref, user="U2", **extra):
    v = json.dumps({"ref": ref, **extra})
    act = {"action_id": action_id, **({"selected_option": {"value": v}} if action_id in ("uniform_code", "uniform_scope")
                                      else {"value": v})}
    return {"user": {"id": user}, "actions": [act]}


def test_a_barcode_shows_only_the_garment_and_size(u):
    slack, d = u
    out = uniform.lookup(d.alliant, "4729628")  # typed without Alliant's zero padding
    assert out["found"] and out["garment"] == "Coverall Navy" and out["size"] == "52 L" and out["account"] == "1003-1-00002"
    assert out["wearer"] == "Mike Example"  # printed on the tag itself (Scott, 11/14)
    assert uniform.unsign(out["garment_ref"])["w"] == "37"
    assert uniform.unsign(out["garment_ref"][:-2] + "00") is None  # a forged ref is refused
    assert uniform.lookup(d.alliant, "abc") == {"found": False}
    assert uniform.lookup(d.alliant, "12") == {"found": False}


def test_a_request_gets_a_card_without_names_and_test_approval_enters_nothing(u):
    slack, d = u
    ref = uniform.lookup(d.alliant, "000004729628")["garment_ref"]
    out = d.uniform().new({"source": "qr", "garments": [{"garment_ref": ref, "problem": "torn", "note": "left sleeve"}],
                           "contact": {"phone": "+19075550100"}, "photos": 1})
    assert out["suggested"] == {"code": "SS", "scope": "garment"} and out["card"] == "posted"
    assert slack.msgs["CUT"]  # posted in the test channel
    card = "\n".join(d.uniform().lines(d.uniform().load()["requests"][out["ref"]]))
    assert "BOB'S SERVICE'S" in card and "Coverall Navy 52 L" in card and "Ripped or torn" in card and "Mike Example (#37)" in card
    assert d.uniform().act(tap("uniform_approve", out["ref"], user="U9")) == "ignored: not office"
    assert d.uniform().act(tap("uniform_code", out["ref"], code="GP")) == f"{out['ref']} code GP"
    assert d.uniform().act(tap("uniform_approve", out["ref"])) == f"{out['ref']} approved (test)"
    assert not d.jobs().load()["jobs"]


def test_live_approval_queues_the_code_and_the_loop_closes(u, monkeypatch):
    slack, d = u
    monkeypatch.setenv("UNIFORM_TEST", "off")
    d.uniform()._channel = None
    out = d.uniform().new({"source": "driver", "garments": [{"barcode": "000004729628", "problem": "button"}]})
    assert d.uniform().act(tap("uniform_approve", out["ref"])).startswith(f"{out['ref']} approved, job")
    job = d.jobs().next()
    assert job["kind"] == "service_code" and "Code: BR Button Replacement" in job["ticket"] and "000004729628" in job["ticket"]
    d.jobs().result(job["id"], "entered", "1 garment got BR")
    assert d.uniform().load()["requests"][out["ref"]]["status"] == "entered"
    from . import alliant_live
    monkeypatch.setattr(alliant_live, "lookup", lambda *a, **k: {"source": "live", "rows": [dict(ROW, service_code="")]})
    sent = []
    monkeypatch.setattr(d.uniform(), "send_text", lambda phone, msg: sent.append(msg) or "text")
    d.uniform().done_pass(0 + 10 ** 10)
    rec = d.uniform().load()["requests"][out["ref"]]
    assert rec["status"] == "done"


def test_a_repeat_on_the_account_tells_scott_and_a_wearer_repeat_suggests_the_wearer(u):
    slack, d = u
    slack.msgs.setdefault("U4KFSQCDQ", [])
    d.uniform().new({"garments": [{"barcode": "000004729628", "problem": "torn"}]})
    out = d.uniform().new({"garments": [{"barcode": "000004729628", "problem": "stain"}]})
    assert out["suggested"] == {"code": "ST", "scope": "wearer"}
    assert any("Repeat garment complaint" in m.get("text", "") for m in slack.msgs["U4KFSQCDQ"])


def test_doesnt_fit_is_a_size_check_for_the_wearer_and_old_garments_an_audit(u):
    slack, d = u
    assert d.uniform().new({"garments": [{"barcode": "000004729628", "problem": "fit_small"}]})["suggested"] == \
        {"code": "SZ", "scope": "wearer"}
    assert d.uniform().new({"account": "1003-1-00002", "garments": [{"problem": "audit"}]})["suggested"] == \
        {"code": "AA", "scope": "customer"}


def test_no_tag_match_suggests_the_account_without_names(u):
    slack, d = u
    out = uniform.match(d.alliant, "Bob's Service", "Mike")
    assert out == {"accounts": ["1003-1-00002"], "confidence": "high"}


def test_driver_mode_gets_todays_stops_in_order(u, monkeypatch):
    slack, d = u
    monkeypatch.setattr(d.alliant, "stops", [
        {"branch": "1", "route": "12", "day": "Wed", "sequence": "2", "account": "1003-1-00002", "name": "BOB'S", "city": "Wasilla"},
        {"branch": "1", "route": "12", "day": "Wed", "sequence": "1", "account": "1950-1-00000", "name": "ACME", "city": "Wasilla"},
        {"branch": "1", "route": "12", "day": "Thu", "sequence": "1", "account": "1111-1-00000", "name": "THU", "city": ""},
        {"branch": "2", "route": "12", "day": "Wed", "sequence": "1", "account": "6001-2-00000", "name": "TLG", "city": ""}], raising=False)
    out = uniform.stops(d.alliant, "12", "1", "Wednesday")
    assert [s["account"] for s in out["stops"]] == ["1950-1-00000", "1003-1-00002"] and out["day"] == "Wed"


def test_names_read_well_on_a_phone():
    assert uniform.nice("SHIRT MIMIX LS SHIRT NAVY") == "Shirt Mimix LS Shirt Navy"
    assert uniform.nice("THREE BEARS BIG LAKE C STORE #27") == "Three Bears Big Lake C Store #27"
    assert uniform.nice("FISHER'S FUEL INC") == "Fisher's Fuel Inc"
    assert uniform.nice("PANT FR IQ SERIES") == "Pant FR IQ Series"


def test_a_code_still_on_after_14_days_asks_the_stockroom_weekly(u, monkeypatch):
    slack, d = u
    monkeypatch.setenv("UNIFORM_TEST", "off")
    d.uniform()._channel = None
    from . import new_accounts
    slack.msgs.setdefault(new_accounts.STOCKROOM_ID, [])
    out = d.uniform().new({"garments": [{"barcode": "000004729628", "problem": "torn"}]})
    d.uniform().update(out["ref"], status="entered", entered_at=1_000_000.0)
    from . import alliant_live
    monkeypatch.setattr(alliant_live, "lookup", lambda *a, **k: {"source": "live", "rows": [dict(ROW, service_code="SS")]})
    d.uniform()._last = 0
    d.uniform().done_pass(1_000_000.0 + 13 * 86400)
    assert not slack.msgs[new_accounts.STOCKROOM_ID]  # not yet
    d.uniform()._last = 0
    d.uniform().done_pass(1_000_000.0 + 15 * 86400)
    posts = [m["text"] for m in slack.msgs[new_accounts.STOCKROOM_ID]]
    assert len(posts) == 1 and "SS Seam Repair* went on in Alliant 15 days ago" in posts[0] and "Mike Example (#37)" in posts[0]
    d.uniform()._last = 0
    d.uniform().done_pass(1_000_000.0 + 18 * 86400)
    assert len(slack.msgs[new_accounts.STOCKROOM_ID]) == 1  # weekly, not daily
    d.uniform()._last = 0
    d.uniform().done_pass(1_000_000.0 + 23 * 86400)
    assert len(slack.msgs[new_accounts.STOCKROOM_ID]) == 2


def test_driver_words_map_to_problems():
    assert uniform.problem_of("torn left pocket") == "torn"
    assert uniform.problem_of("zipper broken") == "zipper"
    assert uniform.problem_of("missing buttons") == "button"
    assert uniform.problem_of("too small, needs a bigger size") == "fit_small"
    assert uniform.problem_of("grease stain on the front") == "stain"
    assert uniform.problem_of("new name tag") == "patch"
    assert uniform.problem_of("audit all garments", "inspection") == "audit"
    assert uniform.problem_of("relabel", "relabel") == "label"


def test_a_driver_post_becomes_a_uniform_request(u):
    slack, d = u
    from .schema import ServiceFlag
    parsed = SimpleNamespace(account_number="1003-1-00002", summary="garment torn",
                             service=ServiceFlag(scope="garment", kind="repair", reason="torn sleeve",
                                                 barcodes=["000004729628"]))
    out = uniform.driver_request(d, parsed, {"user": "UDRV", "ts": "1.0"}, "route-12")
    rec = d.uniform().load()["requests"][out["ref"]]
    assert out["suggested"] == {"code": "SS", "scope": "garment"} and rec["source"] == "driver"
    assert rec["garments"][0]["barcode"] == "000004729628" and rec["driver"]["route"] == "route-12"
    w = SimpleNamespace(account_number="1003-1-00002", summary="all of Mike's garments are worn",
                        service=ServiceFlag(scope="wearer", kind="inspection", reason="all worn out", wearer="Mike"))
    out = uniform.driver_request(d, w, {"user": "UDRV", "ts": "2.0"}, "route-12")
    rec = d.uniform().load()["requests"][out["ref"]]
    assert out["suggested"] == {"code": "WR", "scope": "wearer"} and rec["wearers"] == ["37"]
    assert "Wearer: Mike Example (#37)" in "\n".join(d.uniform().lines(rec))
    inv = SimpleNamespace(account_number="1003-1-00002", summary="count",
                          service=ServiceFlag(scope="customer", kind="inventory", reason="inventory count"))
    assert uniform.driver_request(d, inv, {"user": "UDRV", "ts": "3.0"}, "route-12") is None


def test_the_photos_go_in_the_cards_thread(u, monkeypatch):
    slack, d = u
    uploads = []
    slack.files_upload_v2 = lambda **kw: uploads.append(kw)
    monkeypatch.setattr(uniform, "sf_photos", lambda sid, limit=3: [("tear.jpg", b"jpegbytes")] if sid == "a0X000000000001AAA" else [])
    monkeypatch.setattr(uniform.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)))
    out = d.uniform().new({"sf_request_id": "a0X000000000001AAA", "photos": 1,
                           "garments": [{"barcode": "000004729628", "problem": "torn"}]})
    assert uploads and uploads[0]["thread_ts"] == d.uniform().load()["requests"][out["ref"]]["ts"]
    assert uploads[0]["file"] == b"jpegbytes" and uploads[0]["channel"] == "CUT"
    d.uniform().new({"sf_request_id": "a0X000000000002AAA", "photos": 1, "garments": [{"barcode": "000004729628", "problem": "torn"}]})
    assert "couldn't fetch the photo" in slack.msgs["CUT"][-1]["text"]


def test_a_driver_repair_texts_and_emails_the_customer_when_it_is_fixed(u, monkeypatch, tmp_path):
    slack, d = u
    monkeypatch.setenv("UNIFORM_TEST", "off")
    d.uniform()._channel = None
    (tmp_path / "customer_phones.csv").write_text("phone,accounts,name\n9075550100,1003-1-00002,Bob\n"
                                                  "9075550199,9999-1-00001,Someone else\n")
    (tmp_path / "customer_emails.csv").write_text("email,accounts,name\nbob@example.com,1003-1-00002;1003-1-00003,Bob\n"
                                                  "other@example.com,9999-1-00001,Other\n")
    texts, mails = [], []
    monkeypatch.setattr(d.uniform(), "send_text", lambda phone, msg: texts.append((phone, msg)) or "text")
    monkeypatch.setattr(d.uniform(), "send_email", lambda to, subj, msg: mails.append((to, subj, msg)) or "email")
    cards = []
    real_card = d.uniform().card
    monkeypatch.setattr(d.uniform(), "card", lambda rec, status="": cards.append(status) or real_card(rec, status))
    driver = d.uniform().new({"source": "driver", "account": "1003-1-00002",
                              "garments": [{"barcode": "000004729628", "problem": "button"}]})["ref"]
    d.uniform().close(driver)
    assert [p for p, _ in texts] == ["+19075550100"] and [t for t, _, _ in mails] == ["bob@example.com"]  # this account only
    assert texts[0][1].endswith("Reply here if anything else needs fixing, or reply STOP to opt out.") and mails[0][2].endswith("anything else needs fixing.")
    assert "The coverall navy (size 52 L) your driver picked up for repair has been repaired and will come back with your next delivery" in texts[0][1] and "Coverall navy (size 52 L)".lower() in texts[0][1].lower()
    assert d.uniform().load()["requests"][driver]["status"] == "done"
    assert "Told: the customer (1 by text, 1 by email)" in cards[-1]
    texts.clear(), mails.clear()
    qr = d.uniform().new({"source": "qr", "garments": [{"barcode": "000004729628", "problem": "torn"}],
                          "contact": {"phone": "+19075550123"}})["ref"]
    d.uniform().close(qr)  # a page request tells only the person who asked
    assert [p for p, _ in texts] == ["+19075550123"] and not mails
    assert "STOP" not in texts[0][1]  # a reply to someone who asked: no opt-out line needed


def test_a_request_donna_could_not_enter_can_be_sent_back(u, monkeypatch):
    slack, d = u
    monkeypatch.setenv("UNIFORM_TEST", "off")
    d.uniform()._channel = None
    ref = d.uniform().new({"source": "qr", "garments": [{"barcode": "000004729628", "problem": "button"}]})["ref"]
    d.uniform().act(tap("uniform_approve", ref))
    job = d.jobs().next()
    d.jobs().result(job["id"], "needs_person", "Garment Inquiry isn't reachable from my Alliant login")
    rec = d.uniform().load()["requests"][ref]
    assert rec["status"] == "approved" and not rec["job"] and d.uniform().retryable(rec)
    assert d.uniform().act(tap("uniform_retry", ref, user="U9")) == "ignored: not office"
    assert d.uniform().act(tap("uniform_retry", ref)).startswith(f"{ref} sent back")
    again = d.jobs().next()
    assert again["kind"] == "service_code" and again["id"] != job["id"] and "BR" in again["ticket"]
    assert d.uniform().retry(ref) .startswith(f"{ref} isn't waiting")  # once
    d.jobs().result(again["id"], "entered", "1 garment got BR")
    assert d.uniform().load()["requests"][ref]["status"] == "entered"
    assert d.uniform().retry_failed("U2") == []


def test_the_laundry_groups_repairs_are_barcode_only(u, monkeypatch):
    # Scott, 11/20: TLG garments are pooled (barcodes for wash counts, not assigned to wearers): skip the wearer name
    slack, d = u
    tlg = {**ROW, "barcode": "000005550001", "cust_code": "7000-2-00001", "wearer": "9", "item": "SCRUB TOP CEIL"}
    d.alliant.by_barcode[tlg["barcode"]] = tlg
    d.alliant.customers.append(SimpleNamespace(account="7000-2-00001", name="ANTHC", route="4", service_days=["Mon"]))
    d.alliant.wearers["7000-2-00001"] = [SimpleNamespace(employee="9", first="Pool", last="Rack", name="Pool Rack")]
    out = uniform.lookup(d.alliant, "5550001")
    assert out["found"] and out["wearer"] == "" and out["pooled"] and out["barcode"] == "000005550001"
    assert uniform.unsign(out["garment_ref"])["w"] == ""
    r = d.uniform().new({"source": "qr", "brand": "TLG", "garments": [{"garment_ref": out["garment_ref"], "problem": "fit_big"}]})
    assert r["suggested"] == {"code": "SZ", "scope": "garment"}  # too big: this garment, not "the wearer"
    rec = d.uniform().load()["requests"][r["ref"]]
    card = "\n".join(d.uniform().lines(rec))
    assert "barcode 000005550001" in card and "Pool Rack" not in card and rec["wearers"] == []
    assert "Whole wearer" not in json.dumps(d.uniform().buttons(rec))
    assert "no wearers" in d.uniform().act(tap("uniform_scope", r["ref"], scope="wearer"))
    assert uniform.lookup(d.alliant, "000004729628")["wearer"] == "Mike Example"  # Snow White unchanged

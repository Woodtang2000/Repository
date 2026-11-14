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
    assert "Mike" not in json.dumps(out) and "37" not in json.dumps({k: v for k, v in out.items() if k != "garment_ref"})
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

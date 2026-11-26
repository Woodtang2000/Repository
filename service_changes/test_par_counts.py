"""Par counting for the hospitals (par_counts.py): Linen Helper import, counts, the pull list, Alliant building totals."""
import json
from types import SimpleNamespace

import pytest

from . import par_counts
from .test_desk import setup  # noqa: F401


@pytest.fixture
def pars(setup, tmp_path, monkeypatch):
    slack, d = setup
    d.data_dir = str(tmp_path)
    slack.msgs["U0CJJ3L9PPV"] = []  # Chris Gowen's direct messages
    csv = tmp_path / "lh.csv"
    csv.write_text("Delivery Point,Item Number,Description,Par Level\n"
                   "3 West,7-01-00,MEDICAL SHEET FLAT,40\n3 West,7-01-04,MEDICAL PILLOW CASE,30\n"
                   "ICU,7-01-00,MEDICAL SHEET FLAT,20\nICU,7-02-00,MEDICAL TOWEL BATH,15\n"
                   "ICU,,BAD ROW,abc\n")
    h = par_counts.import_csv(str(csv), "6001-2-00000", "Alaska Regional Hospital")
    d.pars().add_hospital(h)
    monkeypatch.setattr(par_counts, "delivery_date", lambda now=None: "2026-12-01")
    return slack, d


def test_linen_helper_export_becomes_points_and_pars(pars):
    slack, d = pars
    h = d.pars().setup()["6001-2-00000"]
    assert [p["name"] for p in h["points"]] == ["3 West", "ICU"]
    assert h["points"][0]["items"] == [{"sku": "7-01-00", "item": "MEDICAL SHEET FLAT", "par": 40},
                                       {"sku": "7-01-04", "item": "MEDICAL PILLOW CASE", "par": 30}]
    assert len(h["points"][1]["items"]) == 2  # the bad row is skipped
    c = d.pars().config()
    assert c["date"] == "2026-12-01" and c["hospitals"][0]["counted"] == {}


def test_counts_make_the_combined_pull_list_in_the_tlg_channel(pars, monkeypatch):
    slack, d = pars
    out = d.pars().save({"account": "6001-2-00000", "point": "3-west", "who": "Samantha",
                         "counts": {"MEDICAL SHEET FLAT": "12", "MEDICAL PILLOW CASE": "35"}})
    assert out["ok"] and out["need"] == {"MEDICAL SHEET FLAT": 28, "MEDICAL PILLOW CASE": 0}
    d.pars().save({"account": "6001-2-00000", "point": "icu", "who": "Shakira",
                   "counts": {"MEDICAL SHEET FLAT": 5, "MEDICAL TOWEL BATH": 15}})
    assert d.pars().config()["hospitals"][0]["counted"] == {"3-west": "Samantha", "icu": "Shakira"}
    res = d.pars().finish({"account": "6001-2-00000", "who": "Samantha"})
    totals = {t["item"]: t["need"] for t in res["pull"]["totals"]}
    assert totals == {"MEDICAL SHEET FLAT": 43}  # 28 + 15; pillow cases and towels are at par
    assert [p["name"] for p in res["pull"]["points"]] == ["3 West", "ICU"]  # the page's printable list
    # nothing in a channel; Donna isn't in Alliant yet, so Chris hears the counts are done and to enter them himself
    assert res["alliant"] == "" and not d.jobs().load()["jobs"]
    note = slack.msgs["U0CJJ3L9PPV"][-1]["text"]
    assert "Alaska Regional Hospital: counts are done for Tuesday 12/1" in note and "please enter" in note
    assert "• MEDICAL SHEET FLAT: 43" in note


def test_building_totals_go_to_donnas_alliant_queue_once_she_is_back(pars, monkeypatch):
    slack, d = pars
    monkeypatch.setenv("PAR_ALLIANT", "on")
    d.pars().save({"account": "6001-2-00000", "point": "icu", "who": "S", "counts": {"MEDICAL TOWEL BATH": 3}})
    d.jobs().pause(True)
    assert d.pars().finish({"account": "6001-2-00000"})["alliant"] == ""  # paused on Alliant: not queued
    d.jobs().pause(False)
    jid = d.pars().finish({"account": "6001-2-00000"})["alliant"]
    job = d.jobs().load()["jobs"][jid]
    assert job["kind"] == "par_request" and "- 7-02-00 MEDICAL TOWEL BATH: 12" in job["ticket"]
    assert "3 West" in d.pars().pull_list("6001-2-00000", "2026-12-01")["missing"]
    job["status"] = "working"
    data = d.jobs().load(); data["jobs"][jid]["status"] = "working"; d.jobs().save(data)
    sent = len(slack.msgs["U0CJJ3L9PPV"])  # queued for Alliant: Chris waits for the entry
    d.jobs().result(jid, "entered", "MEDICAL TOWEL BATH 12 set")
    assert len(slack.msgs["U0CJJ3L9PPV"]) == sent + 1
    assert slack.msgs["U0CJJ3L9PPV"][-1]["text"] == \
        "✅ Alaska Regional Hospital: counts are done and entered in Alliant for Tuesday 12/1."


def test_unknown_points_are_refused(pars):
    slack, d = pars
    assert not d.pars().save({"account": "6001-2-00000", "point": "nope"})["ok"]
    assert not d.pars().finish({"account": "9999-2-00000"})["ok"]


def test_a_linen_room_first_then_its_delivery_units_add_up_to_its_account(pars):
    slack, d = pars
    p = d.pars()
    assert not p.change({"op": "unit", "account": "6021-2-00001", "name": "Building 1"})["ok"]  # no linen room yet
    assert p.change({"op": "room", "account": "6021-2-00001", "name": "Seward Windsong Lodge"})["ok"]
    # no units yet: the linen room itself is what's counted (a one-linen-room lodge)
    assert p.change({"op": "item", "account": "6021-2-00001", "point": "room", "item": "BATH TOWEL", "par": "300"})["ok"]
    room = next(h for h in p.config()["hospitals"] if h["account"] == "6021-2-00001")
    assert not room["units"] and room["points"][0]["id"] == "room" and room["points"][0]["items"][0]["par"] == 300
    # then buildings under it
    for b in ("Building 1", "Building 2", "Building 2"):
        p.change({"op": "unit", "account": "6021-2-00001", "name": b})
    room = next(h for h in p.config(admin=True)["hospitals"] if h["account"] == "6021-2-00001")
    assert room["units"] and [x["id"] for x in room["points"]] == ["building-1", "building-2", "building-2-2"]
    assert room["items"][0]["item"] == "BATH TOWEL"  # the linen room's own pars are kept for managers
    assert not p.change({"op": "item", "account": "6021-2-00001", "point": "building-1", "item": "BATH TOWEL",
                         "par": "lots"})["ok"]
    for b, par in (("building-1", 40), ("building-2", 25)):
        p.change({"op": "item", "account": "6021-2-00001", "point": b, "item": "bath towel", "par": str(par)})
    p.change({"op": "remove_unit", "account": "6021-2-00001", "point": "building-2-2"})
    assert p.points(p.setup()["6021-2-00001"])[0]["items"] == [{"sku": "", "item": "BATH TOWEL", "par": 40}]
    p.save({"account": "6021-2-00001", "point": "building-1", "counts": {"BATH TOWEL": 10}, "who": "Runner"})
    p.save({"account": "6021-2-00001", "point": "building-2", "counts": {"BATH TOWEL": 5}, "who": "Runner"})
    pl = p.pull_list("6021-2-00001", "2026-12-01")
    assert pl["totals"] == [{"item": "BATH TOWEL", "sku": "", "need": 50}] and pl["missing"] == []  # 30 + 20
    # a par cleared takes the item off the unit
    p.change({"op": "item", "account": "6021-2-00001", "point": "building-2", "item": "BATH TOWEL", "par": ""})
    assert p.points(p.setup()["6021-2-00001"])[1]["items"] == []


def test_the_techs_config_has_no_setup(pars):
    slack, d = pars
    assert d.pars().config()["admin"] is False and "items" not in d.pars().config()["hospitals"][0]


def test_chris_is_told_to_enter_them_when_donna_cant(pars, monkeypatch):
    slack, d = pars
    monkeypatch.setenv("PAR_ALLIANT", "on")
    d.pars().save({"account": "6001-2-00000", "point": "icu", "who": "S", "counts": {"MEDICAL TOWEL BATH": 3}})
    jid = d.pars().finish({"account": "6001-2-00000"})["alliant"]
    data = d.jobs().load(); data["jobs"][jid]["status"] = "working"; d.jobs().save(data)
    d.jobs().result(jid, "needs_person", "loading already started")
    note = slack.msgs["U0CJJ3L9PPV"][-1]["text"]
    assert "couldn't enter them in Alliant" in note and "• MEDICAL TOWEL BATH: 12" in note

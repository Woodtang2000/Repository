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
    d.channels["tlg-alaska-regional"] = "CARH"
    slack.msgs["CARH"] = []
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
    post = slack.msgs["CARH"][-1]["text"]
    assert "Pull list · Alaska Regional Hospital" in post and "MEDICAL SHEET FLAT: *43*" in post
    assert "<@U0CJJ3L9PPV> ready to build the carts." in post  # Chris Gowen (Scott, 11/26)
    assert "*3 West* (Samantha): MEDICAL SHEET FLAT 28" in post and "*ICU* (Shakira): MEDICAL SHEET FLAT 15" in post
    assert res["alliant"] == "" and not d.jobs().load()["jobs"]  # Alliant waits until Donna is back in (PAR_ALLIANT)


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
    d.jobs().result(jid, "entered", "MEDICAL TOWEL BATH 12 set")
    assert "Entered in Alliant (Request Entry)" in slack.msgs["CARH"][-1]["text"]


def test_unknown_points_are_refused(pars):
    slack, d = pars
    assert not d.pars().save({"account": "6001-2-00000", "point": "nope"})["ok"]
    assert not d.pars().finish({"account": "9999-2-00000"})["ok"]

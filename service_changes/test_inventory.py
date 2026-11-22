"""Garment inventory scans (inventory.py): the page's lookups, the #stockroom card, the Count Station job."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from . import inventory
from .context import ALASKA
from .test_desk import setup  # noqa: F401

ROWS = [{"barcode": "000004729628", "cust_code": "1003-1-00002", "wearer": "37", "sku": "2-CT10NV", "item": "COVERALL NAVY",
         "size_a": "52", "size_b": "L", "service_code": ""},
        {"barcode": "000004729629", "cust_code": "1003-1-00002", "wearer": "37", "sku": "2-CT10NV", "item": "COVERALL NAVY",
         "size_a": "52", "size_b": "L", "service_code": "SS"},
        {"barcode": "000004729630", "cust_code": "1003-1-00002", "wearer": "38", "sku": "2-SH01", "item": "SHIRT WORK GREY",
         "size_a": "L", "size_b": "", "service_code": ""},
        {"barcode": "000009999999", "cust_code": "2000-1-00001", "wearer": "1", "sku": "2-SH01", "item": "SHIRT WORK GREY",
         "size_a": "M", "size_b": "", "service_code": ""}]


@pytest.fixture
def inv(setup, tmp_path, monkeypatch):
    slack, d = setup
    d.data_dir = str(tmp_path)
    monkeypatch.setattr(d.alliant, "by_barcode", {r["barcode"]: r for r in ROWS}, raising=False)
    in_service = {}
    for r in ROWS:
        in_service.setdefault((r["cust_code"], r["wearer"]), []).append(r)
    monkeypatch.setattr(d.alliant, "in_service", in_service, raising=False)
    monkeypatch.setattr(d.alliant, "customers", [
        SimpleNamespace(account="1003-1-00002", name="BOB'S SERVICE'S", route="12", service_days=["Tue", "Fri"]),
        SimpleNamespace(account="2000-1-00001", name="OTHER CO", route="3", service_days=["Mon"])])
    monkeypatch.setattr(d.alliant, "stops", [], raising=False)
    monkeypatch.setattr(d.alliant, "wearers", {"1003-1-00002": [SimpleNamespace(employee="37", name="Mike Example"),
                                                                SimpleNamespace(employee="38", name="Ann Other")]}, raising=False)
    slack.msgs["CSTOCK"] = []
    monkeypatch.setenv("INVENTORY_CHANNEL_ID", "CSTOCK")
    return slack, d


def test_the_count_station_day_is_the_scan_day_or_the_next_delivery(inv):
    slack, d = inv
    tue = datetime(2026, 11, 24, 10, tzinfo=ALASKA)
    assert inventory.count_day(d.alliant, "1003-1-00002", tue) == "Tue"
    assert inventory.count_day(d.alliant, "1003-1-00002", datetime(2026, 11, 25, tzinfo=ALASKA)) == "Fri"  # a Wednesday
    assert inventory.count_day(d.alliant, "1003-1-00002", datetime(2026, 11, 28, tzinfo=ALASKA)) == "Tue"  # a Saturday


def test_accounts_and_a_scan_lookup_for_employees(inv):
    slack, d = inv
    assert [a["account"] for a in inventory.accounts(d.alliant, "bob")] == ["1003-1-00002"]
    assert inventory.accounts(d.alliant, "1003")[0]["days"] == ["Tue", "Fri"]
    g = inventory.lookup(d.alliant, "4729629", "1003-1-00002")
    assert g["found"] and g["here"] and g["garment"] == "Coverall Navy" and g["wearer"] == "Mike Example" and g["code"] == "SS"
    assert not inventory.lookup(d.alliant, "9999999", "1003-1-00002")["here"]
    assert not inventory.lookup(d.alliant, "12345678", "1003-1-00002")["found"]


def test_a_scan_posts_the_card_and_in_test_mode_enters_nothing(inv):
    slack, d = inv
    out = d.inventory().new({"account": "1003-1-00002", "who": "Kirk", "barcodes": ["4729628", "000004729629", "4729628",
                                                                                     "9999999", "55500001"]})
    assert out["ok"] and out["scanned"] == 4 and out["on_account"] == 2 and out["elsewhere"] == 1 and out["unknown"] == 1
    assert out["in_service"] == 3 and out["missing"] == 1
    card = slack.msgs["CSTOCK"][-1]["text"]
    assert "Inventory scan · 1003-1-00002 Bob's Service's" in card and "Scanned *2* of the *3*" in card
    assert "000004729630 · Shirt Work Grey L · Ann Other" in card  # the one nobody scanned
    assert "000009999999 (2000-1-00001)" in card and "TEST: I would enter 3 barcodes" in card
    assert not d.jobs().load()["jobs"]


def test_live_it_queues_the_count_station_job_and_reports_under_the_card(inv, monkeypatch):
    slack, d = inv
    monkeypatch.setenv("INVENTORY_TEST", "off")
    out = d.inventory().new({"account": "1003-1-00002", "who": "Kirk", "barcodes": ["4729628", "4729629"]})
    [job] = d.jobs().load()["jobs"].values()
    assert job["kind"] == "inventory" and "Count Station: status scan \"Inventory\"" in job["ticket"]
    assert "- 000004729628" in job["ticket"] and "- 000004729629" in job["ticket"] and "9999999" not in job["ticket"]
    job["status"] = "working"
    data = d.jobs().load()
    data["jobs"][job["id"]]["status"] = "working"
    d.jobs().save(data)
    d.jobs().result(job["id"], "entered", "2 entered\n000004729629: has service code SS, continued")
    reply = slack.msgs["CSTOCK"][-1]
    assert reply.get("thread_ts") and "Entered at the Count Station" in reply["text"] and "service code SS" in reply["text"]
    assert d.inventory().load()["scans"][out["ref"]]["status"] == "entered"


def test_nothing_without_an_account_or_barcodes(inv):
    slack, d = inv
    assert not d.inventory().new({"account": "nope", "barcodes": ["4729628"]})["ok"]
    assert not d.inventory().new({"account": "1003-1-00002", "barcodes": []})["ok"]

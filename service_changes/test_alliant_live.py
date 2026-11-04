"""alliant_live: fixed read-only lookups with a limit, a cache, a timeout and a fallback (no real database here)."""
import pytest

from . import alliant_live as live


class Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def execute(self, sql, params=None):
        assert live.safe_sql(sql)
        self.db.calls.append((sql, params))
        if self.db.fail:
            raise self.db.fail
        if "INFORMATION_SCHEMA" in sql:
            self.rows = [{"COLUMN_NAME": "sz_skugrup"}]
        elif "inskusiz" in sql:
            self.rows = [{"sz_size": "L", "sz_price": 1.201, "sz_lrc": 18.23}]
        elif "riskugrp s WITH (NOLOCK) WHERE LTRIM(RTRIM(s.sg_skugrup))" in sql:
            self.rows = [{"sg_skugrup": "2-0413WH", "sg_desc": "COAT CHEF WHITE", "sg_mspr": 0.57, "sg_maxwash": 32}]
        else:
            self.rows = [dict(r) for r in self.db.rows]

    def fetchmany(self, n):
        return self.rows[:n]

    def fetchone(self):
        return self.rows[0] if self.rows else None


class DB:
    def __init__(self, rows=None, fail=None):
        self.rows, self.fail, self.calls, self.commits, self.rollbacks, self.closed = rows or [], fail, [], 0, 0, 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = DB(rows=[{"account": "1205-1-00004", "sku": "0101", "item": "BAR MOP", "autocount": 60, "inventory": 120}])
    monkeypatch.setattr(live, "_connect", lambda: d)
    monkeypatch.setenv("ALLIANT_SQL_USER", "feeduser")
    monkeypatch.setenv("ALLIANT_SQL_KEY", "s3cret-key")
    monkeypatch.delenv("ALLIANT_LIVE_OFF", raising=False)
    (tmp_path / "current_items.csv").write_text("account,item,quantity,autocount,sku\n1205-1-00004,BAR MOP,50,50,0101\n")
    return d, str(tmp_path)


def test_every_fixed_lookup_is_a_nolock_select_and_writes_are_refused():
    for sql in live.SQL.values():
        assert live.safe_sql(sql)
    for bad in ("UPDATE dbo.riempitm SET ei_auto_ct = 1", "SELECT 1 FROM dbo.x WITH (NOLOCK); DELETE FROM dbo.x",
                "SELECT * FROM dbo.riempitm i", "SELECT * INTO dbo.y FROM dbo.x WITH (NOLOCK)", "EXEC sp_who"):
        assert not live.safe_sql(bad)


def test_a_live_lookup_binds_parameters_never_commits_and_is_cached(db):
    d, data = db
    r = live.lookup("items", data, account="1205-1-00004")
    assert r["source"] == "live" and r["rows"][0]["autocount"] == 60
    sql, params = d.calls[0]
    assert params == {"acct": 1205, "branch": 1, "dept": "00004"} and "1205" not in sql  # bound, not built in
    assert d.commits == 0 and d.rollbacks == 1 and d.closed == 1
    again = live.lookup("items", data, account="1205-1-00004")
    assert len(d.calls) == 1 and "cached" in again["note"]  # 2-minute cache: no second query


def test_the_hourly_limit_falls_back_to_the_hourly_files(db, monkeypatch):
    d, data = db
    monkeypatch.setenv("ALLIANT_LIVE_PER_HOUR", "2")
    live.lookup("items", data, account="1205-1-00004")
    live.lookup("holds", data, account="1205-1-00004")
    r = live.lookup("next_delivery", data, account="1205-1-00004")
    assert r["source"] == "files" and "limit" in r["note"] and len(d.calls) == 2


def test_alliant_down_uses_the_files_and_never_shows_the_login(db):
    d, data = db
    d.fail = RuntimeError("Login failed for user 'feeduser' using s3cret-key")
    r = live.lookup("items", data, account="1205-1-00004")
    assert r["source"] == "files" and r["rows"][0]["autocount"] == "50" and "up to an hour old" in r["note"]
    assert "feeduser" not in r["note"] and "s3cret" not in r["note"]
    log = open(f"{data}/alliant_live.log").read()
    assert "feeduser" not in log and "s3cret" not in log and "items" in log


def test_not_set_up_means_files(db, monkeypatch):
    d, data = db
    monkeypatch.delenv("ALLIANT_SQL_KEY")
    assert live.lookup("items", data, account="1205-1-00004")["source"] == "files" and not d.calls


def test_sku_read_back_shows_prices_to_scott_only(db):
    d, data = db
    r = live.lookup("sku", data, sku="2-0413WH")
    assert r["rows"][0]["sizes"][0]["sz_size"] == "L"
    owner, staff = live.summary(r, "owner"), live.summary(r, "staff")
    assert "0.57" in owner and "18.23" in owner
    assert "0.57" not in staff and "18.23" not in staff and "1.201" not in staff and "sg_maxwash=32" in staff


def test_bad_parameters_are_refused():
    with pytest.raises(ValueError):
        live.parse_account("1205; DROP TABLE x")
    with pytest.raises(ValueError):
        live.lookup("garment", barcode="x' OR 1=1 --")
    with pytest.raises(ValueError):
        live.lookup("anything_else", account="1205-1-00004")


def test_a_stopped_items_zero_inventory_shows_in_the_summary():
    r = {"lookup": "items", "params": {"acct": 5045}, "note": "Live", "rows": [
        {"sku": "5-01-01", "inventory": 0, "autocount": 0, "d_mon": 0, "d_tue": 1, "on_hold": False, "stop_date": "2026-10-29"}]}
    line = live.summary(r).splitlines()[2]
    assert "inventory=0" in line and "autocount=0" in line and "d_mon" not in line and "on_hold" not in line

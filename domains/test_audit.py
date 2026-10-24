from datetime import date

from . import audit, godaddy

ACCOUNT = [
    {"domain": "anthc.ai", "expires": "2027-03-01T00:00:00Z", "renewAuto": True, "privacy": True, "locked": True,
     "status": "ACTIVE", "nameServers": ["ns1.domaincontrol.com"]},
    {"domain": "alsco.ai", "expires": "2026-11-15T00:00:00Z", "renewAuto": False, "privacy": False, "locked": True,
     "status": "ACTIVE", "nameServers": ["ns1.domaincontrol.com"]},
    {"domain": "akfish.ai", "expires": "2027-06-01T00:00:00Z", "renewAuto": True, "privacy": True, "locked": True,
     "status": "ACTIVE", "nameServers": ["ns1.afternic.com"]},
]
DNS = {("anthc.ai", "A", "@"): [], ("anthc.ai", "CNAME", "www"): [{"data": "@"}],
       ("alsco.ai", "A", "@"): [{"data": "34.102.136.180"}], ("alsco.ai", "CNAME", "www"): [{"data": "@"}]}


def fake(monkeypatch, deleted):
    monkeypatch.setattr(godaddy, "domains", lambda: ACCOUNT)
    monkeypatch.setattr(godaddy, "records", lambda n, t, h: DNS.get((n, t, h)))
    monkeypatch.setattr(godaddy, "delete_records", lambda n, t, h: deleted.append((n, t, h)))


def test_flags(monkeypatch):
    fake(monkeypatch, [])
    rows = {r["domain"]: r for r in (audit.inspect(d, date(2026, 10, 24)) for d in ACCOUNT)}
    assert rows["anthc.ai"]["flags"] == [] and rows["anthc.ai"]["fix"] == []
    f = rows["alsco.ai"]["flags"]
    assert f[0].startswith("PARKED") and "auto-renew OFF" in f and "privacy OFF" in f
    assert any("expires within" in x for x in f) and rows["alsco.ai"]["fix"] == [("A", "@")]
    assert any("FOR SALE" in x for x in rows["akfish.ai"]["flags"])


def test_report_groups(monkeypatch, capsys):
    fake(monkeypatch, [])
    audit.main([])
    out = capsys.readouterr().out
    assert out.index("Category A") < out.index("anthc.ai") < out.index("Category B") < out.index("alsco.ai")
    assert out.index("Not categorized") < out.index("akfish.ai")


def test_fix_is_dry_run_unless_apply(monkeypatch, capsys):
    deleted = []
    fake(monkeypatch, deleted)
    audit.main(["--fix"])
    assert deleted == [] and "would delete A @ on alsco.ai" in capsys.readouterr().out
    audit.main(["--fix", "--apply"])
    assert deleted == [("alsco.ai", "A", "@")]

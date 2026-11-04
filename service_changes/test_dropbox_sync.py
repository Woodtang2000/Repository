"""deploy/dropbox_sync.py: the feed from the local copy on agent-server, Dropbox only as the fallback."""
import importlib.util
import os

import pytest

spec = importlib.util.spec_from_file_location("dropbox_sync", os.path.join(os.path.dirname(__file__), "..", "deploy", "dropbox_sync.py"))
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)

CSV = {"customers.csv": "account,name,route,service_days\n1205-1-00004,CHILIS,4,Tue\n",
       "customer_cards.csv": "account,name\n1205-1-00004,CHILIS\n",
       "current_items.csv": "account,item,quantity,autocount,sku\n1205-1-00004,BAR MOP,50,50,0101\n",
       "garments.csv": "account,employee,item,size,quantity\n1205-1-00004,1,COAT,L,11\n",
       "wearers.csv": "account,employee,first,last\n1205-1-00004,1,Ann,Lee\n",
       "holds.csv": "account,sku\n", "feed_done.txt": "2026-11-04T10:05:00-0900 done\n"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    feed, data = tmp_path / "feed", tmp_path / "data"
    feed.mkdir()
    (feed / "alliant_university").mkdir()
    (feed / "alliant_university" / "01 Customer Accounts.md").write_text("notes")
    (feed / "private - Scott and Bill only").mkdir()
    (feed / "private - Scott and Bill only" / "ar.csv").write_text("account,balance\n")
    for n, t in CSV.items():
        (feed / n).write_text(t)
    conf = {"app_key": "k", "app_secret": "s", "refresh_token": "r", "path": "/x.pdf"}
    monkeypatch.setattr(ds, "LOCAL_FEED", str(feed))
    monkeypatch.setattr(ds, "DATA", str(data))
    monkeypatch.setattr(ds, "load", lambda: conf)
    monkeypatch.setattr(ds, "save", lambda c: None)
    calls = []
    monkeypatch.setattr(ds, "access_token", lambda c: calls.append("dropbox") or "t")
    monkeypatch.setattr(ds, "sync_pdf", lambda c, a: calls.append("pdf"))
    return feed, data, conf, calls


def test_the_feed_comes_from_the_local_copy_without_dropbox(env):
    feed, data, conf, calls = env
    ds.sync()
    assert (data / "customers.csv").read_text() == CSV["customers.csv"]
    assert (data / "alliant_university" / "01 Customer Accounts.md").read_text() == "notes"
    assert oct(os.stat(data / "private" / "ar.csv").st_mode & 0o777) == "0o600"
    assert calls == []  # never signed in to Dropbox
    (data / "customers.csv").write_text("changed by hand")
    ds.sync()  # nothing new in the feed: nothing copied
    assert (data / "customers.csv").read_text() == "changed by hand"


def test_an_unreadable_local_copy_falls_back_to_dropbox(env, monkeypatch):
    feed, data, conf, calls = env
    monkeypatch.setattr(ds, "LOCAL_FEED", str(feed / "not-there"))
    monkeypatch.setattr(ds, "_meta", lambda auth, path: None)  # Dropbox has nothing either
    ds.sync()
    assert "dropbox" in calls and not (data / "customers.csv").exists()


def test_paths_never_leave_the_folder(env):
    feed, *_ = env
    with pytest.raises(ValueError):
        ds.LocalSource(str(feed)).read("/../secret")

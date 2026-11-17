from types import SimpleNamespace

from . import salesforce, sf_directory


def fake_sf(monkeypatch, contacts, accounts):
    monkeypatch.setenv("SF_DOMAIN", "x.my.salesforce.com")
    monkeypatch.setenv("SF_CLIENT_ID", "id")
    monkeypatch.setenv("SF_CLIENT_SECRET", "secret")
    monkeypatch.setattr(salesforce, "_token", lambda: ("tok", "https://x"))
    monkeypatch.setattr(salesforce, "_call", lambda m, url, t, body=None:
                        {"records": contacts if "FROM+Contact" in url else accounts})


ALLIANT = SimpleNamespace(customers=[SimpleNamespace(account="1205-1-00004"), SimpleNamespace(account="1205-1-00005"),
                                     SimpleNamespace(account="7000-2-00001")])


def test_salesforce_contacts_become_a_best_guess_for_the_office(tmp_path, monkeypatch):
    fake_sf(monkeypatch, [
        {"Name": "Jane Smith", "Phone": "(907) 555-1234", "MobilePhone": None, "Email": "Jane@Midas.com",
         "Account": {"AccountNumber": "1205", "Name": "Midas"}},
        {"Name": "Bookkeeper", "Phone": "907-555-9999", "Email": "", "Account": {"AccountNumber": "1", "Name": "A"}},
        {"Name": "Bookkeeper", "Phone": "907-555-9999", "Email": "", "Account": {"AccountNumber": "2", "Name": "B"}},
        {"Name": "Bookkeeper", "Phone": "907-555-9999", "Email": "", "Account": {"AccountNumber": "3", "Name": "C"}},
        {"Name": "Bookkeeper", "Phone": "907-555-9999", "Email": "", "Account": {"AccountNumber": "4", "Name": "D"}}],
        [{"Name": "Alaska Hospital", "Phone": "+1 907 555 0000", "AccountNumber": "7000"},
         {"Name": "Us", "Phone": "(907) 222-7738", "AccountNumber": "1205"}])
    d = str(tmp_path)
    assert sf_directory.refresh(d) and not sf_directory.refresh(d)  # once a day
    accounts, line = sf_directory.guess(d, ALLIANT, "+19075551234")
    assert accounts == ["1205-1-00004", "1205-1-00005"] and "Jane Smith at Midas (1205)" in line
    assert sf_directory.guess(d, ALLIANT, "jane@midas.com")[0] == ["1205-1-00004", "1205-1-00005"]
    assert sf_directory.guess(d, ALLIANT, "9075550000")[1].endswith("Alaska Hospital (main number) (7000)")
    assert sf_directory.guess(d, ALLIANT, "9075559999") == ([], "")  # on 4 accounts: too common to guess
    assert sf_directory.guess(d, ALLIANT, "9072227738") == ([], "")  # our own number
    assert sf_directory.guess(d, ALLIANT, "9075550101") == ([], "")  # not in Salesforce


def test_the_new_texter_card_shows_the_salesforce_guess(tmp_path, monkeypatch):
    from . import sms
    from .context import Alliant, Customer
    fake_sf(monkeypatch, [{"Name": "Jane Smith", "Phone": "(907) 555-1234", "Email": "",
                           "Account": {"AccountNumber": "1205", "Name": "Midas"}}], [])
    sf_directory.build(str(tmp_path))
    posted = []
    slack = SimpleNamespace(chat_postMessage=lambda **kw: posted.append(kw) or {"ts": "1.1"},
                            conversations_history=lambda **kw: {"messages": []})
    monkeypatch.setattr(sms, "cards_channel", lambda s, d: "CNEW")
    monkeypatch.setattr(sms, "open_card", lambda s, c, k: None)
    al = Alliant(customers=[Customer("1205-1-00004", "MIDAS #4", "4", ["Thu"])])
    sms.signup_card(slack, "CDESK", al, "+19075551234", "", "need more towels", [], data_dir=str(tmp_path))
    text = str(posted[-1])
    assert "Salesforce has this number for: Jane Smith at Midas (1205)" in text and "Best match: *1205-1-00004*" in text

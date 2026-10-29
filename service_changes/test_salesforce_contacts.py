from . import salesforce


def fake(monkeypatch, accounts, contacts):
    calls = []
    monkeypatch.setenv("SF_DOMAIN", "x.my.salesforce.com")
    monkeypatch.setenv("SF_CLIENT_ID", "id")
    monkeypatch.setenv("SF_CLIENT_SECRET", "secret")
    monkeypatch.setattr(salesforce, "_token", lambda: ("tok", "https://x"))

    def call(method, url, token, body=None):
        calls.append((method, url.split("/v62.0/")[-1], body))
        if method == "GET" and "FROM+Account" in url:
            return {"records": accounts}
        if method == "GET":
            return {"records": contacts}
        return {}
    monkeypatch.setattr(salesforce, "_call", call)
    return calls


def test_a_new_person_becomes_a_contact_on_the_account(monkeypatch):
    calls = fake(monkeypatch, [{"Id": "001A", "Name": "Homer Golf Course"}], [])
    out = salesforce.sync_contact(["5045-1-00000", "5045-1-00001"], "Jane Smith at Homer Golf", phone="+1 (907) 555-1234")
    assert out == ["Jane Smith added to Homer Golf Course"]
    [post] = [c for c in calls if c[0] == "POST"]
    assert post[2] == {"AccountId": "001A", "LastName": "Smith", "FirstName": "Jane", "MobilePhone": "9075551234",
                       "Description": "Added by Donna when the office linked them to the account."}
    assert "AccountNumber+%3D+%275045%27" in [c for c in calls if c[0] == "GET"][0][1]  # one Account for both locations


def test_an_existing_contact_only_gets_its_blanks_filled(monkeypatch):
    calls = fake(monkeypatch, [{"Id": "001A", "Name": "Chilis Alaska"}],
                 [{"Id": "003S", "Name": "Susan Dubinski", "Email": "susan@chilis.com", "MobilePhone": None, "Phone": "907-222-1111"}])
    assert salesforce.sync_contact(["5040-1-00000"], "Susan", email="Susan@Chilis.com", phone="907-229-2058") == \
        ["Susan Dubinski on Chilis Alaska: added mobile"]
    assert [c[2] for c in calls if c[0] == "PATCH"] == [{"MobilePhone": "9072292058"}]  # her email stays as it was
    calls.clear()
    assert salesforce.sync_contact(["5040-1-00000"], "Susan", email="susan@chilis.com") == ["Susan Dubinski on Chilis Alaska: already there"]
    assert not [c for c in calls if c[0] in ("PATCH", "POST")]


def test_no_account_or_two_writes_nothing(monkeypatch):
    calls = fake(monkeypatch, [], [])
    assert salesforce.sync_contact(["9999-1-00000"], "Bob", phone="9075550000")[0].startswith("⚠️ no Salesforce account")
    fake(monkeypatch, [{"Id": "1", "Name": "A"}, {"Id": "2", "Name": "B"}], [])
    assert "more than one" in salesforce.sync_contact(["1204-2-00001"], "Bob", phone="9075550000")[0]
    assert not [c for c in calls if c[0] in ("PATCH", "POST")]

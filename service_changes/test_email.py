import pytest

from . import email_desk, sms
from .parser import Answer, Identified
from .schema import Action, Category, Change, ParsedMessage
from .test_sms import ALLIANT, FakeSlack


def mail(i, sender, body, subject="Towels", spf=True, name="Jane"):
    return {"id": f"rem_{i}", "from": {"email": sender, "name": name}, "subject": subject, "text": body,
            "message_id": f"<m{i}@acme.com>", "spf_pass": spf, "dkim_pass": None, "received_at": f"2026-10-23T0{i}:00:00Z"}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    (tmp_path / "customer_emails.csv").write_text("email,accounts,name\nJane@Acme.com,M4,Jane at Midas\n")
    sent, inbox, seen = [], [], {}

    def fake_parse(client, text, channel, ts, alliant, author="", office=False, previous=""):
        seen["alliant"], seen["text"], seen["author"] = alliant, text, author
        if "how many" in text.lower():
            return ParsedMessage(category=Category.lookup, account_number="M4", summary="how many towels")
        return ParsedMessage(category=Category.item_change, account_number="C1" if "costco" in text.lower() else "M4",
                             summary="towels", changes=[Change(action=Action.add, item="shop towels", quantity=200)])

    import service_changes.parser as parser
    import service_changes.run as run
    monkeypatch.setattr(parser, "parse_message", fake_parse)
    monkeypatch.setattr(parser, "answer_lookup", lambda c, q, facts, asker="": Answer(answer="300 per delivery", found=True))
    monkeypatch.setattr(parser, "identify_customer", lambda c, text, customers: Identified(person_name="Bob", accounts=["M8"]))
    monkeypatch.setattr(run, "fill_item_matches", lambda c, p, a, **kw: None)
    slack = FakeSlack()
    ed = email_desk.EmailDesk(slack, None, ALLIANT, "service-desk-test", str(tmp_path),
                              send=lambda to, body: sent.append((to, body)), fetch=lambda since: list(inbox))
    ed.run_once(first=True)
    return ed, slack, sent, inbox, seen


def test_clean_body_drops_the_quoted_thread():
    body = "Please add 200 shop towels.\n\nThanks, Jane\n\nOn Tue, Oct 21, 2026 at 9:00 AM Snow White wrote:\n> old stuff"
    assert email_desk.clean_body(body) == "Please add 200 shop towels.\n\nThanks, Jane"


def test_known_sender_gets_a_ticket_and_only_their_accounts(desk):
    ed, slack, sent, inbox, seen = desk
    inbox.append(mail(1, "jane@acme.com", "Please add 200 shop towels at Fairbanks"))
    ed.run_once()
    assert [c.account for c in seen["alliant"].customers] == ["M4"] and "by email" in seen["author"]
    [t] = [p for p in slack.posts if p["meta"].get("kind") == "ticket"]
    assert t["meta"]["email_to"] == "jane@acme.com" and t["meta"]["email_msgid"] == "<m1@acme.com>"
    assert "📧 Customer email from Jane at Midas" in t["text"] and "couldn't be verified" not in t["text"]
    assert sent == [("jane@acme.com", sms.GOT_IT.format(how="email"))]
    ed.run_once()  # nothing new: nothing more
    assert len(sent) == 1


def test_another_account_is_refused_and_unverified_senders_are_flagged(desk):
    ed, slack, sent, inbox, seen = desk
    inbox.append(mail(1, "jane@acme.com", "add 200 bar mops for costco", spf=False))
    ed.run_once()
    [t] = [p for p in slack.posts if p["meta"].get("kind") == "ticket"]
    assert t["meta"]["account"] == "M4"  # only ever her own account
    assert "Confirm by phone" in t["text"]


def test_unknown_sender_gets_a_card_and_is_answered_after_approval(desk):
    ed, slack, sent, inbox, seen = desk
    inbox.append(mail(1, "bob@midas.com", "Bob from Midas Palmer, please add 200 shop towels", name="Bob"))
    ed.run_once()
    [card] = [p for p in slack.posts if p["meta"].get("kind") == "email_signup"]
    assert "bob@midas.com" in card["text"] and "M8" in card["text"]
    assert not [p for p in slack.posts if p["meta"].get("kind") == "ticket"]
    assert "getting your account confirmed" in sent[-1][1]
    slack.react(card["ts"])
    ed.run_once()
    assert "You're all set" in sent[-1][1] and "Consider it done" in sent[-1][1]  # one email, not two
    [t] = [p for p in slack.posts if p["meta"].get("kind") == "ticket"]  # the held email, now handled
    assert t["meta"]["email_to"] == "bob@midas.com"


def test_office_commands_and_auto_replies(desk):
    ed, slack, sent, inbox, seen = desk
    slack.say("add <mailto:pat@fly.com|pat@fly.com> to M8")
    ed.run_once()
    assert email_desk.load_emails(ed.data_dir + "/customer_emails.csv")["pat@fly.com"]["accounts"] == ["M8"]
    inbox.append(mail(1, "pat@fly.com", "I'm away until Monday", subject="Automatic reply: Towels"))
    inbox.append(mail(2, "mailer-daemon@acme.com", "bounce"))
    n = len(sent)
    ed.run_once()
    assert len(sent) == n  # never answered: no mail loops
    slack.say("remove pat@fly.com")
    ed.run_once()
    assert "pat@fly.com" not in email_desk.load_emails(ed.data_dir + "/customer_emails.csv")


def test_staff_forward_reads_every_account(desk):
    ed, slack, sent, inbox, seen = desk
    inbox.append(mail(1, "ana@snowwhitelinen.com", "Fwd: Costco wants 200 more bar mops", name="Ana"))
    ed.run_once()
    assert len(seen["alliant"].customers) == len(ALLIANT.customers)
    [t] = [p for p in slack.posts if p["meta"].get("kind") == "ticket"]
    assert t["meta"]["account"] == "C1" and t["meta"]["email_to"] == "ana@snowwhitelinen.com"
    assert sent == []  # staff aren't sent "got it"


def test_google_group_rewritten_sender(monkeypatch):
    from .email_desk import real_sender
    monkeypatch.setenv("EMAIL_REPLY_TO", "service@snowwhitelinen.com")
    assert real_sender({"from": {"email": "jo@kobuk.com"}}) == "jo@kobuk.com"
    via = {"from": {"email": "service@snowwhitelinen.com", "name": "Jo via service"},
           "reply_to": [{"email": "jo.k@gmail.com"}]}
    assert real_sender(via) == "jo.k@gmail.com"
    via_header = {"from": {"email": "Service@SnowWhiteLinen.com"},
                  "headers": [{"name": "Reply-To", "value": "Jo <jo.k@gmail.com>"}]}
    assert real_sender(via_header) == "jo.k@gmail.com"


def test_junk_mail_is_listed_not_answered_and_can_be_rescued(desk, monkeypatch):
    ed, slack, sent, inbox, seen = desk
    import service_changes.parser as parser
    from .parser import Junk
    slack.conversations_list = lambda **kw: [{"channels": [{"id": "D1", "name": "service-desk-test", "is_member": True},
                                                           {"id": "F1", "name": "email-filtered", "is_member": True}]}]
    slack.reactions_add = lambda **kw: None
    checked = []
    monkeypatch.setattr(parser, "is_junk_email", lambda c, sender, subject, body, bulk=False: checked.append(bulk) or
                        Junk(junk="SEO" in subject, kind="marketing"))
    m = mail(2, "growth@seo-rockets.io", "We can get you to page one of Google!", subject="SEO for Snow White", name="Kyle")
    m["headers"] = [{"name": "List-Unsubscribe", "value": "<mailto:x>"}]
    inbox.append(m)
    ed.run_once()
    assert checked == [True] and sent == []  # no reply: that would confirm the address to a spammer
    assert not [p for p in slack.posts if p["meta"].get("kind") == "email_signup"]
    [f] = [p for p in slack.posts if p["channel"] == "F1"]
    assert "marketing" in f["text"] and "SEO for Snow White" in f["text"] and f["meta"]["kind"] == "email_filtered"

    f.setdefault("reactions", []).append({"name": "white_check_mark", "users": ["U4KFSQCDQ"]})  # it was a customer after all
    ed.run_once()
    [card] = [p for p in slack.posts if p["meta"].get("kind") == "email_signup"]
    assert "growth@seo-rockets.io" in card["text"] and not ed.state["filtered"]
    ed.run_once()
    assert len([p for p in slack.posts if p["meta"].get("kind") == "email_signup"]) == 1  # rescued once

    inbox.append(mail(3, "amy@moosecafe.com", "Do you rent bar towels? We just opened.", subject="Towels", name="Amy"))
    ed.run_once()
    assert "getting your account confirmed" in sent[-1][1]  # a real prospect goes through as before


def test_emails_come_from_donna_with_her_picture(monkeypatch):
    from . import bird
    sent = []
    monkeypatch.setenv("BIRD_API_KEY", "bk_us1_x")
    monkeypatch.setenv("EMAIL_FROM", "donna@mail.snowwhitelinen.com")
    monkeypatch.setenv("EMAIL_REPLY_TO", "service@snowwhitelinen.com")
    monkeypatch.setattr(bird, "_call", lambda method, path, params=None, body=None, key="": sent.append(body) or {})
    bird.send("ap@chilis.com", "Re: mops", "All done! Bar mops to 60 a week.\n\n" + bird.signature("The Laundry Group"),
              company="The Laundry Group")
    b = sent[-1]
    assert b["from"] == {"email": "donna@mail.snowwhitelinen.com", "name": "Donna – The Laundry Group"}
    assert b["reply_to"] == ["service@snowwhitelinen.com"] and "866-TAP-TEXT (866-827-8398)" in b["text"]
    assert "donna-avatar.png" in b["html"] and "Bar mops to 60 a week." in b["html"] and "<b style" in b["html"]

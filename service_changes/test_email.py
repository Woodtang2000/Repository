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


def test_an_owners_own_address_and_a_forged_staff_sender(desk, monkeypatch):
    ed, slack, sent, inbox, seen = desk
    monkeypatch.setenv("EMAIL_TRUSTED", "bill@dubinsky.com")
    inbox.append(mail(1, "Bill@Dubinsky.com", "Fwd: Costco wants 200 more bar mops", name="Bill"))
    inbox.append(mail(2, "scotty@snowwhitelinen.com", "Fwd: Costco wants 200 more bar mops", spf=False, name="Scott"))
    inbox.append(mail(3, "Bill@TheLaundryGroup.com", "Fwd: Costco wants 200 more bar mops", name="Bill"))
    ed.run_once()
    tickets = [p for p in slack.posts if p["meta"].get("kind") == "ticket"]
    assert [t["meta"]["email_to"] for t in tickets] == ["bill@dubinsky.com", "scotty@snowwhitelinen.com",
                                                        "bill@thelaundrygroup.com"]  # the sister company's staff too
    assert "couldn't be verified" not in str(tickets[0]) and "couldn't be verified" in str(tickets[1])


def test_the_laundry_groups_customers_hear_from_its_own_address(monkeypatch):
    from . import bird
    calls = []
    monkeypatch.setattr(bird, "_call", lambda method, path, body=None, key="": calls.append(body))
    monkeypatch.setenv("EMAIL_FROM", "donna@mail.snowwhitelinen.com")
    monkeypatch.setenv("EMAIL_REPLY_TO", "service@snowwhitelinen.com")
    monkeypatch.delenv("EMAIL_FROM_TLG", raising=False)
    monkeypatch.delenv("EMAIL_REPLY_TO_TLG", raising=False)
    bird.send("a@b.com", "Hi", "Hello", company="The Laundry Group")
    assert calls[-1]["from"]["email"] == "donna@mail.snowwhitelinen.com"  # not set up yet: the one address
    monkeypatch.setenv("EMAIL_FROM_TLG", "donna@mail.thelaundrygroup.com")
    monkeypatch.setenv("EMAIL_REPLY_TO_TLG", "donna@thelaundrygroup.com")
    bird.send("a@b.com", "Hi", "Hello", company="The Laundry Group")
    assert calls[-1]["from"] == {"email": "donna@mail.thelaundrygroup.com", "name": "Donna – The Laundry Group"}
    assert calls[-1]["reply_to"] == ["donna@thelaundrygroup.com"]
    bird.send("a@b.com", "Hi", "Hello", company="Snow White Linen")
    assert calls[-1]["from"]["email"] == "donna@mail.snowwhitelinen.com" and calls[-1]["reply_to"] == ["service@snowwhitelinen.com"]


def test_replies_come_from_the_customers_own_company(desk, monkeypatch):
    from . import bird
    ed, slack, sent, inbox, seen = desk
    calls = []
    monkeypatch.setattr(bird, "send", lambda to, subject, text, in_reply_to="", key="", company="": calls.append((to, company, text)))
    (ed.data_dir and open(f"{ed.data_dir}/customer_emails.csv", "a").write("tom@kobuk.com,5100-2-00001,Tom at Kobuk\n"))
    ed.state["threads"]["tom@kobuk.com"] = {"subject": "Mats"}
    ed.state["threads"]["new@lodge.com"] = {"subject": "Hello", "to_tlg": True}
    ed._send("tom@kobuk.com", "Got it")  # branch 2 customer
    ed._send("jane@acme.com", "Got it")  # branch 1 (M4)
    ed._send("new@lodge.com", "Who are you?")  # not on file yet: wrote to donna@thelaundrygroup.com
    assert [c[1] for c in calls] == ["The Laundry Group", "Snow White Linen", "The Laundry Group"]
    assert "The Laundry Group" in calls[0][2] and "Snow White Linen" in calls[1][2]  # the signature matches


def test_one_email_to_both_donna_addresses_is_one_ticket(desk):
    ed, slack, sent, inbox, seen = desk
    one = mail(1, "jane@acme.com", "Please add 200 shop towels at Fairbanks")
    inbox.extend([one, {**one, "id": "rem_copy"}])  # forwarded by donna@snowwhitelinen.com and donna@thelaundrygroup.com
    ed.run_once()
    assert len([p for p in slack.posts if p["meta"].get("kind") == "ticket"]) == 1
    ed.run_once()  # and the same one again on the next look: still one
    assert len([p for p in slack.posts if p["meta"].get("kind") == "ticket"]) == 1


def test_a_branch_2_customer_anywhere_makes_the_whole_account_the_laundry_group(tmp_path):
    from .context import Alliant, company_for
    (tmp_path / "customers.csv").write_text("account,name,route,days\n1204-1-00000,KOBUK LODGE,1,Mon\n"
                                            "1204-2-00001,KOBUK LODGE ANNEX,2,Tue\n5045-1-00000,HOMER GOLF COURSE,5,Tue\n")
    Alliant.from_dir(str(tmp_path))
    assert company_for("1204-1-00000") == "The Laundry Group" and company_for("1204-2-00001") == "The Laundry Group"
    assert company_for("5045-1-00000") == "Snow White Linen"
    Alliant.from_dir(str(tmp_path / "none"))  # leave no Laundry Group numbers behind for other tests


def test_a_special_alliant_emailed_goes_to_kirk_not_the_desk(desk):
    ed, slack, sent, inbox, seen = desk
    posts, uploads = [], []
    slack.chat_postMessage = lambda channel, text, **kw: posts.append((channel, text)) or {"channel": "DKIRK", "ts": "1.1"}
    slack.files_upload_v2 = lambda **kw: uploads.append(kw)
    ed.attachments = lambda mid: [{"id": "att1", "filename": "S0148777.pdf", "content_type": "application/pdf"}]
    ed.attachment = lambda mid, aid: b"%PDF-1.7 invoice"
    m = mail(5, "invoices@alliantcloud.com", "Your invoice is attached", subject="Invoice S0148777")
    m["to"] = [{"email": "donna+specials@snowwhitelinen.com"}]
    inbox.append(m)
    ed.run_once()
    assert posts and posts[0][0] == "U5WUV01JA" and "Special invoice S0148777" in posts[0][1]
    import os
    assert os.path.exists(os.path.join(ed.data_dir, "specials", "S0148777.pdf"))  # kept for the customer's email
    assert uploads and uploads[0]["filename"] == "S0148777.pdf" and uploads[0]["content"].startswith(b"%PDF")
    assert not [p for p in slack.posts if p["meta"].get("kind") == "ticket"] and not sent  # no ticket, no reply
    ed.run_once()
    assert len(uploads) == 1  # once


def test_the_customer_gets_the_special_with_its_pdf_once_whichever_half_comes_first(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from . import bird, specials
    sent = []
    monkeypatch.setattr(bird, "send", lambda to, subject, text, in_reply_to="", key="", company="", attachments=None:
                        sent.append((to, subject, text, attachments)))
    alliant = SimpleNamespace(customers=[SimpleNamespace(account="5045-1-00000", name="HOMER GOLF COURSE")],
                              cards={"5045-1-00000": {"email": "pro@homergolf.com"}})
    d = str(tmp_path)
    specials.save_pdf(d, "S0148779", b"%PDF-1.7")
    assert specials.try_send(d, alliant, "S0148779") == ""  # her report isn't in yet
    specials.save_job(d, "S0148779", "5045-1-00000", "✅ 5045-1-00000 HOMER GOLF COURSE – special delivery of 5 Towel Bar Mop Gold Stripe on Tuesday")
    assert specials.try_send(d, alliant, "S0148779") == "sent to pro@homergolf.com"
    [(to, subject, text, att)] = sent
    assert subject == "Snow White Linen: your special delivery for Homer Golf Course"
    assert "is set up: special delivery of 5 Towel Bar Mop Gold Stripe on Tuesday. The delivery ticket is attached (invoice S0148779)" in text
    assert att == [{"filename": "S0148779.pdf", "content": b"%PDF-1.7"}]
    assert specials.try_send(d, alliant, "S0148779") == "" and len(sent) == 1  # once
    monkeypatch.setenv("STOP_EMAILS_HOLD", "5045")
    specials.save_pdf(d, "S0148780", b"%PDF")
    specials.save_job(d, "S0148780", "5045-1-00000", "x")
    assert specials.try_send(d, alliant, "S0148780") == "held" and len(sent) == 1


def test_each_company_signs_with_its_own_office_phone():
    from . import bird
    assert bird.signature("The Laundry Group").endswith("Office: (907) 519-0761")
    assert bird.signature("Snow White Linen").endswith("Office: (907) 222-7738")
    html = bird._html("Hello,\n\nHi.\n\nThank you,\n\n" + bird.signature("The Laundry Group"), "The Laundry Group", "test")
    assert "Office (907) 519-0761" in html and "/logo/tlg.png" in html and "#ffb807" in html


def test_the_driver_gets_the_delivery_ticket_pdf_in_their_thread(tmp_path):
    from types import SimpleNamespace
    from . import specials
    uploads = []
    slack = SimpleNamespace(files_upload_v2=lambda **kw: uploads.append(kw))
    d = str(tmp_path)
    specials.save_pdf(d, "S0148781", b"%PDF-1.7")
    assert specials.share_with_driver(d, slack, "S0148781") == ""  # no report, no driver post yet
    specials.save_job(d, "S0148781", "5045-1-00000", "x")
    specials.set_driver_post(d, "S0148781", "CR5", "9.9")
    assert specials.share_with_driver(d, slack, "S0148781") == "pdf in the driver's thread"
    assert uploads == [{"channel": "CR5", "thread_ts": "9.9", "content": b"%PDF-1.7", "filename": "S0148781.pdf",
                        "title": "Delivery ticket S0148781"}]
    assert specials.share_with_driver(d, slack, "S0148781") == "" and len(uploads) == 1  # once
    import json, os
    assert json.load(open(os.path.join(d, "specials", "S0148781.json")))["account"] == "5045-1-00000"  # kept both halves

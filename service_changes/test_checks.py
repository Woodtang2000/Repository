from .checks import check, readback
from .context import Alliant, Customer, route_from_channel, service_day
from .run import load_messages
from .parser import build_prompt
from .schema import Action, Category, Change, ParsedMessage

ALLIANT = Alliant(
    customers=[Customer("A1", "Denali Brewing Company", "4", ["Mon"]),
               Customer("A2", "Moose's Tooth", "1", ["Mon", "Thu"]),
               Customer("A3", "Tacos Cancun", "4", ["Fri"])],
    items={"A1": {"Wet Mops": 16}, "A2": {"3x5 Charcoal Heather Mat": 2}, "A3": {"3x10 Charcoal Heather Mat": 2}},
)


def msg(account, *changes, customer="Customer"):
    return ParsedMessage(category=Category.item_change, customer_as_written=customer,
                         account_number=account, changes=list(changes), summary="")


def test_route_and_day():
    assert route_from_channel("route-4") == "4"
    assert route_from_channel("route7") == "7"
    assert route_from_channel("general") is None
    assert service_day(load_messages("service_changes/examples/messages.json")[0]["ts"]) == "Fri"  # Kobuk, 2026-09-25


def test_candidates_narrow_by_route_and_day():
    assert [c.account for c in ALLIANT.candidates("4", "Fri")] == ["A3", "A1"]  # today's stops first
    assert [c.account for c in ALLIANT.candidates("4", "Mon")] == ["A1", "A3"]
    assert len(ALLIANT.candidates(None, "Mon")) == 3
    assert ALLIANT.match_account("denali brewing company", "4", "Mon") == "A1"
    assert ALLIANT.match_account("Denali", "4", "Mon") is None


def test_item_matching_on_real_alliant_names():
    from .checks import current_qty
    acct = Alliant(items={"A": {
        "TOWEL BAR MOP GOLD STRIPE": 60, "MOP WET 16 OZ": 5, "MOP WET 24 OZ": 5, "MAT CHARCOAL HEATHER 3X10": 6,
        "MAT CHARCOAL HEATHER 3X5": 1, "MAT BLACK COMFORT FLOW 3X5": 1, "APRON BLACK BIB": 40,
        "ROUTE LAUNDRY BAG": 3, "ROUTE BAG STAND": 3, "MAT CHARCOAL WATERHOG 4X6": 1}})
    assert current_qty(acct, "A", "barmops") == ("TOWEL BAR MOP GOLD STRIPE", 60)
    assert current_qty(acct, "A", "24oz orange mop heads") == ("MOP WET 24 OZ", 5)
    assert current_qty(acct, "A", "16 oz wet mops") == ("MOP WET 16 OZ", 5)
    assert current_qty(acct, "A", "3 x 10 mat") == ("MAT CHARCOAL HEATHER 3X10", 6)
    assert current_qty(acct, "A", "3x5 comfort flow") == ("MAT BLACK COMFORT FLOW 3X5", 1)
    assert current_qty(acct, "A", "black bibs") == ("APRON BLACK BIB", 40)
    assert current_qty(acct, "A", "laundry bags") == ("ROUTE LAUNDRY BAG", 3)
    assert current_qty(acct, "A", "4x6 water hog") == ("MAT CHARCOAL WATERHOG 4X6", 1)
    assert current_qty(acct, "A", "3x5 mat") is None   # two 3x5 mats: don't guess
    assert current_qty(acct, "A", "mop heads") is None  # 16 oz or 24 oz?
    assert current_qty(acct, "A", "16oz blue mop heads") == ("MOP WET 16 OZ", 5)
    assert current_qty(acct, "A", "logo mat") is None   # nothing specific left to match on
    # A color that exists elsewhere in Alliant must not be ignored.
    ihop = Alliant(items={"I": {"MAT CHARCOAL HEATHER 4X6": 1}, "H": {"MAT BRANDYWINE 3X10": 2}})
    assert current_qty(ihop, "I", "4x6 Brandywine mat") is None
    assert current_qty(ihop, "I", "4x6 charcoal mat") == ("MAT CHARCOAL HEATHER 4X6", 1)


def test_already_entered_is_not_a_question():
    acct = Alliant(items={"K": {"TOWEL BAR MOP GOLD STRIPE": 40}})
    r = check(msg("K", Change(action=Action.decrease, item="barmops", quantity=40, stated_total=40), customer="Kobuk"), acct)
    assert r.ready and r.changes[0].already_done
    assert r.changes[0].notes == ["Alliant already shows 40; may already be entered"]
    r = check(msg("K", Change(action=Action.stop, item="napkins")), acct)
    assert r.changes[0].notes == ["Not on this account in Alliant (may already be stopped)"]


def test_stated_total_matches():
    r = check(msg("A3", Change(action=Action.add, item="3x10 mat", quantity=4, stated_total=6), customer="Tacos Cancun"), ALLIANT)
    assert r.ready and r.changes[0].current == 2 and r.changes[0].new_total == 6
    assert readback(r) == "✅ A3 Tacos Cancun – 4 3x10 mat added – from 2 to 6"


def test_stated_total_mismatch_asks_driver():
    # The Denali Brewing mix-up: driver meant 28 total, could be read as +28.
    r = check(msg("A1", Change(action=Action.add, item="wet mops", quantity=28, stated_total=28), customer="Denali Brewing"), ALLIANT)
    assert not r.ready
    assert r.questions == ["Denali Brewing has 16 wet mops now. Adding 28 makes 44, but you said total 28. Which is right?"]


def test_set_shows_previous_quantity():
    r = check(msg("A1", Change(action=Action.set, item="wet mops", quantity=28), customer="Denali Brewing"), ALLIANT)
    assert r.ready and r.changes[0].new_total == 28
    assert readback(r) == "✅ A1 Denali Brewing Company – wet mops changed – from 16 to 28"


def test_decrease_below_zero_asks_driver():
    r = check(msg("A2", Change(action=Action.decrease, item="3x5 charcoal heather mat", quantity=3)), ALLIANT)
    assert not r.ready and "only has 2" in r.questions[0]


def test_no_total_and_unknown_item_are_notes_not_blockers():
    r = check(msg("A2", Change(action=Action.add, item="logo mat", quantity=1)), ALLIANT)
    assert r.ready
    assert r.changes[0].notes == ["No total given", "Not on this account in Alliant; check by hand"]


def test_wearer_readback():
    p = ParsedMessage(category=Category.wearer_change, customer_as_written="Midas #6", summary="", changes=[
        Change(action=Action.stop, item="all garments", wearer="Dalton"),
        Change(action=Action.add, item="pants", quantity=11, wearer="Karissa", size="32x30"),
        Change(action=Action.add, item="coats", quantity=2, wearer="Karissa", size="M"),
        Change(action=Action.add, item="pants", quantity=3, wearer="Adam", size="same size")])
    assert readback(check(p, ALLIANT)) == (
        "✅ Midas #6 – Dalton: all garments stopped; Karissa: 11 32x30 pants, 2 M coats added; Adam: 3 pants (same size) added")


def test_hold_readback_and_until():
    hold = ParsedMessage(category=Category.hold_or_closure, customer_as_written="China Sea", summary="On hold 9/5 to 9/13.")
    assert readback(check(hold, ALLIANT)) == "✅ China Sea – On hold 9/5 to 9/13."
    stop = msg(None, Change(action=Action.stop, item="all mats except one 3x10", effective="until further notice"), customer="Safeway 1807")
    r = check(stop, ALLIANT)
    assert r.changes[0].new_total is None
    assert readback(r) == "✅ Safeway 1807 – all mats except one 3x10 stopped – until further notice"


def test_prompt_lists_only_todays_route_customers():
    p = build_prompt("Denali brewing increase wet mops to 28", "route-4", "1790636236", ALLIANT)  # Mon 2026-09-28
    assert "route 4" in p and "posted Mon" in p
    assert "A1 | Denali Brewing Company | Mon" in p and "Moose" not in p
    assert p.index("Denali") < p.index("Tacos Cancun")


def test_decode_days():
    from .alliant_report import decode_days
    assert decode_days("   H   ") == ["Thu"]
    assert decode_days("M  H   ") == ["Mon", "Thu"]
    assert decode_days("MTWHFSU") == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert decode_days("") == []


def test_frequency_codes():
    from .context import frequency_label
    assert frequency_label("7") == "weekly"
    assert frequency_label("5") == frequency_label("6") == "every other week"
    assert frequency_label("2") == "monthly, week 2"
    assert frequency_label("8") == "more than once a week"
    assert frequency_label("9") == "first delivery only"
    assert frequency_label("0") == "no delivery"
    assert frequency_label("A2") == "every 8 weeks, week 2"
    assert frequency_label("B3") == "frequency B3"  # not defined yet
    assert frequency_label("") is None
    p = msg(None, Change(action=Action.add, item="3x10 charcoal heather mat", quantity=1, frequency="frequency 7"), customer="10th & M")
    assert readback(check(p, ALLIANT)) == "✅ 10th & M – 1 3x10 charcoal heather mat added – new on the account (weekly)"


def test_wearer_checks():
    from .context import Wearer
    a = Alliant(
        wearers={"M3": [Wearer("163", "Adam", ""), Wearer("91", "Dale", "A")],
                 "M6": [Wearer("20", "Karissa", ""), Wearer("1", "Rob", "Colins")]},
        garments={("M3", "163"): {"PANT WORK BLACK 32 32": 14, "SHIRT INDUSTRIAL MIDAS L/S L": 14, "JACKET TEAM BLACK L": 2},
                  ("M6", "20"): {"SHIRT INDUSTRIAL MIDAS S/S M": 11, "PANT WORK BLACK 32 30": 11}})
    # The Sep 9 request Ana asked about: Adam has 14 pants, +3 makes 17.
    p = ParsedMessage(category=Category.wearer_change, customer_as_written="Midas #3", account_number="M3", summary="",
                      changes=[Change(action=Action.add, item="pants", quantity=3, wearer="Adam", size="same size")])
    r = check(p, a)
    assert r.changes[0].wearer_number == "163" and r.changes[0].current == 14 and r.changes[0].new_total == 17
    assert readback(r) == "✅ Midas #3 – Adam: 3 pants (same size) (total 17) added"
    # Stopping someone who is already gone, and adding someone new.
    p = ParsedMessage(category=Category.wearer_change, customer_as_written="Midas #6", account_number="M6", summary="", changes=[
        Change(action=Action.stop, item="all garments", wearer="Dalton"),
        Change(action=Action.add, item="short sleeve shirts", quantity=11, wearer="Karissa", size="M"),
        Change(action=Action.add, item="pants", quantity=11, wearer="Riley")])
    r = check(p, a)
    assert r.changes[0].already_done and "No wearer named Dalton" in r.changes[0].notes[0]
    assert r.changes[1].alliant_item == "SHIRT INDUSTRIAL MIDAS S/S M" and r.changes[1].current == 11
    assert r.changes[1].already_done and r.changes[1].new_total == 11
    assert r.changes[2].notes == ["Riley is not in Alliant yet (new wearer)"]
    assert a.find_wearer("M6", "rob colins").employee == "1" and a.find_wearer("M6", "Bob") is None


def test_inventory_double_the_autocount():
    a = Alliant(items={"B": {"TOWEL BAR MOP GOLD STRIPE": 120, "APRON BLACK BIB": 40}, "K": {"TOWEL BAR MOP GOLD STRIPE": 80}})
    # Bake Shop: inventory 120 is double the autocount of 60, which is what was asked for.
    r = check(msg("B", Change(action=Action.set, item="bar mops", quantity=60)), a)
    assert r.changes[0].current == 60 and r.changes[0].inventory == 120 and r.changes[0].already_done
    # Decrease 10 bibs, total 10: only fits if the autocount is 20.
    r = check(msg("B", Change(action=Action.decrease, item="black bibs", quantity=10, stated_total=10)), a)
    assert r.ready and r.changes[0].current == 20 and r.changes[0].new_total == 10
    # Inventory equals autocount: Kobuk 80 - 40 = 40.
    r = check(msg("K", Change(action=Action.decrease, item="barmops", quantity=40, stated_total=40)), a)
    assert r.ready and r.changes[0].current == 80 and r.changes[0].inventory is None
    # Neither reading fits: still a question for the driver.
    r = check(msg("K", Change(action=Action.add, item="barmops", quantity=10, stated_total=100)), a)
    assert not r.ready
    # A real autocount from Alliant wins over guessing.
    a.autocount = {"K": {"TOWEL BAR MOP GOLD STRIPE": 40}}
    r = check(msg("K", Change(action=Action.add, item="barmops", quantity=20, stated_total=60)), a)
    assert r.ready and r.changes[0].current == 40 and r.changes[0].inventory == 80


CARD = """
Delivery Days

10/03/2026

Page 1 of 1

Customer Record Card

Mon Tue Wed Thu Fri Sat Sun

Stop Seq

Bag Count

4 0 0 0 7 00

0 0 0 0 0 0 0

TEST DINER

100 Main St

Anchorage AK 99501

Route 3

Account 9999-1-00001

Contact Pat Lee Contact Phone (907)555-0100

Email (907)555-0199pat@testdiner.com Contact Fax

Route 3 0 0 0 3 0 0

 18.00# of Invoices Contract ExpState Tax 1 5/1/2027 Service $0.00%
Install DateSales Rep Account TypeJESSE Restaurant 4/2/2024 Stop Minimum $30.00

Use back door after 2pmSpecial Instructions

  5-01-01 TOWEL BAR MOP GOLD STRIPE D  240  0  0 8  120 $0.2800 $0.00 N

Price ChangedDelivery Days M   F  0.00% or $0.2800 on 2/1/2026 By MARINA

  8

    12  2-LS524BK L SHIRT INDUSTRIAL MIDAS S/S B  11  11  0 9  0 $1.5800 N

Price ChangedDelivery Days M   F  5.33% or $1.5000 on 2/1/2026 By MARINA

M

M

7
"""


def test_record_card_parsing():
    from .record_cards import parse_text
    card = parse_text(CARD)["9999-1-00001"]
    assert card["name"] == "TEST DINER" and card["route"] == "3"
    assert card["stop_sequence"] == "Mon 4;Fri 7"
    assert card["email"] == "pat@testdiner.com" and card["phone"] == "(907)555-0100"
    assert card["special_instructions"] == "Use back door after 2pm"
    assert (card["sales_rep"], card["install_date"], card["contract_expires"], card["stop_minimum"]) == \
        ("JESSE", "4/2/2024", "5/1/2027", "30.00")
    towel, shirt = card["items"]
    assert (towel["sku"], towel["inventory"], towel["autocount"], towel["frequency"]) == ("5-01-01", 240, 120, "8")
    assert (shirt["wearer"], shirt["sku"], shirt["inventory"], shirt["frequency"]) == ("12", "2-LS524BK", 11, "7")


def test_aliases_and_department_fix():
    from .context import Wearer
    from .run import fix_department, _end_state
    a = Alliant(customers=[Customer("B1", "BOB'S SERVICES", "6", ["Thu"]), Customer("S0", "SAFEWAY 1817", "2", ["Fri"]),
                           Customer("S2", "SAFEWAY 1817 (MEAT)", "2", ["Fri"])],
                wearers={"S2": [Wearer("45", "Raven", "")]}, aliases={"B1": ["BSI"]})
    assert a.match_account("BSI", "6", "Thu") == "B1"
    assert "B1 | BOB'S SERVICES | Thu | also called: BSI" in build_prompt("BSI stop mop heads", "route-6", "1790636236", a)
    p = ParsedMessage(category=Category.wearer_change, customer_as_written="Safeway 1817", account_number="S0", summary="",
                      changes=[Change(action=Action.add, item="meat coat", quantity=1, wearer="Raven")])
    fix_department(p, a, "2", "Fri")
    assert p.account_number == "S2"
    # "add 4, total 6" and "set to 6" end in the same place
    assert _end_state(Change(action=Action.add, item="mat", quantity=4, stated_total=6)) == \
        _end_state(Change(action=Action.set, item="mat", quantity=6))


def test_office_ticket():
    from .checks import ticket
    a = Alliant(customers=[Customer("K1", "KOBUK COFFEE", "1", ["Fri"])],
                items={"K1": {"TOWEL BAR MOP GOLD STRIPE": 80}}, autocount={"K1": {"TOWEL BAR MOP GOLD STRIPE": 80}},
                frequency={"K1": {"TOWEL BAR MOP GOLD STRIPE": "7"}}, sku={("K1", "TOWEL BAR MOP GOLD STRIPE"): "5-01-01"},
                cards={"K1": {"stop_sequence": "Fri 11", "special_instructions": "Back door only"}})
    r = check(msg("K1", Change(action=Action.decrease, item="bar mops", quantity=40, stated_total=40), customer="Kobuk"), a)
    assert ticket(r, a).splitlines() == [
        "*K1*  KOBUK COFFEE",
        "_📝 Back door only_",
        "➖ *Remove 40*  TOWEL BAR MOP GOLD STRIPE `5-01-01` · 80 → *40* · weekly · inventory 80 → *40* too",
        "Readback when done: ✅ K1 KOBUK COFFEE – 40 bar mops decreased – from 80 to 40"]


def test_add_matching_current_asks():
    a = Alliant(items={"M": {"MAT CHARCOAL HEATHER 3X5": 2}})
    r = check(msg("M", Change(action=Action.add, item="3x5 charcoal heather mat", quantity=2), customer="Moose's Tooth"), a)
    assert r.questions == ["Moose's Tooth has 2 3x5 charcoal heather mat now. Add 2 more (total 4), or should they have 2 total?"]
    r = check(msg("M", Change(action=Action.add, item="3x5 charcoal heather mat", quantity=1)), a)
    assert not r.questions


def test_new_wearer_without_size_asks():
    from .context import Wearer
    a = Alliant(wearers={"M8": [Wearer("3", "Chris", "")]})
    p = ParsedMessage(category=Category.wearer_change, customer_as_written="Midas #8", account_number="M8", summary="",
                      changes=[Change(action=Action.add, item="shirts", wearer="Riley"),
                               Change(action=Action.add, item="pants", wearer="Riley", size="42x32")])
    assert check(p, a).questions == ["What size shirts for Riley?"]


def test_named_color_must_match():
    from .checks import colors_agree
    assert not colors_agree("4x6 Brandywine mat", "MAT CHARCOAL HEATHER 4X6")
    assert colors_agree("4x6 charcoal mat", "MAT CHARCOAL HEATHER 4X6")
    assert colors_agree("4x6 mat", "MAT CHARCOAL HEATHER 4X6")
    assert colors_agree("24oz orange mop heads", "MOP WET 24 OZ")


def test_same_question_is_not_asked_twice():
    from .checks import CheckedChange, Result
    p = msg("A1", Change(action=Action.add, item="shirts", wearer="Tim"))
    p.questions_for_driver = ["What shirt size does Tim need?", "Which 3x5 mat?"]
    cc = CheckedChange(change=p.changes[0])
    cc.questions = ["What size shirts for Tim?", "Midas has 1 3x5 mat now. Add 1 more (total 2), or should they have 1 total?"]
    assert Result(p, [cc]).questions == ["What shirt size does Tim need?", "Which 3x5 mat?",
                                         "Midas has 1 3x5 mat now. Add 1 more (total 2), or should they have 1 total?"]


def test_loads_the_alliant_sql_feed(tmp_path):
    from .context import account_facts
    (tmp_path / "customers.csv").write_text("account,name,route,service_days\n2500-2-00000,HORIZON HOUSE,2,Tue;Fri\n"
                                            "990001-1-00000,RTE 1 (DOWNTOWN),1,\n")
    (tmp_path / "customer_cards.csv").write_text(
        "account,name,route,service_days,stop_sequence,contact,phone,mobile,email,special_instructions,location_contact,location_email\n"
        "2500-2-00000,HORIZON HOUSE,2,Tue;Fri,Tue:4;Fri:4,AP INBOX,(907)555-0100,,ap@horizon.com,Back door,Maria,\n")
    (tmp_path / "current_items.csv").write_text("account,item,quantity,autocount,sku,days,frequency\n"
                                                "2500-2-00000,TOWEL BATH 22X44,240,200,5-05-01,Tue;Fri,8\n"
                                                "2500-2-00000,TOWEL BATH 22X44,160,0,5-05-01,Tue;Fri,8\n"
                                                "2500-2-00000,MAT CHARCOAL HEATHER 3X5,2,2,10-07-03,Tue,7\n")
    (tmp_path / "wearers.csv").write_text("account,employee,first,last,department,locker,added_date,stopped_date\n"
                                          "2500-2-00000,1,Ann,Lee,,,2020-01-01,\n2500-2-00000,2,Bob,Gone,,,2019-01-01,2024-05-01\n")
    (tmp_path / "garments.csv").write_text("account,employee,sku,size,item,quantity,autocount,days,frequency\n"
                                           "2500-2-00000,1,2-SH,XL,SHIRT,11,11,Tue,7\n")
    (tmp_path / "holds.csv").write_text("account,type,start,end,reason,employee,sku,size,item\n"
                                        "2500-2-00000,hold,,,Item on hold,0,10-07-03,,MAT CHARCOAL HEATHER 3X5\n")
    (tmp_path / "feed_done.txt").write_text("2026-10-06T20:48:40-0800\n")
    a = Alliant.from_dir(str(tmp_path))
    assert [c.account for c in a.customers] == ["2500-2-00000"]  # route placeholder accounts left out
    assert a.items["2500-2-00000"]["TOWEL BATH 22X44"] == 400 and a.autocount["2500-2-00000"]["TOWEL BATH 22X44"] == 200
    assert [w.name for w in a.wearers["2500-2-00000"]] == ["Ann Lee"]  # stopped wearers left out
    assert a.as_of == "Oct 6, 8:48 PM" and a.exported_at.isoformat() == "2026-10-06T20:48:40-08:00"
    facts = account_facts(a, "2500-2-00000")
    assert "On-site contact: Maria (907)555-0100" in facts and "Invoice contact: AP INBOX ap@horizon.com" in facts
    assert "On hold in Alliant: MAT CHARCOAL HEATHER 3X5" in facts
    p = ParsedMessage(category=Category.item_change, account_number="2500-2-00000", summary="",
                      changes=[Change(action=Action.add, item="3x5 charcoal heather mat", quantity=1)])
    from .checks import ticket
    assert "⏸️ on hold in Alliant" in ticket(check(p, a), a)
    del a.items["2500-2-00000"]["MAT CHARCOAL HEATHER 3X5"]  # the feed lists active lines only; held ones are in holds.csv
    assert "⏸️ *MAT CHARCOAL HEATHER 3X5* is on this account but on hold in Alliant" in ticket(check(p, a), a)


def test_removing_something_the_account_doesnt_have_asks_instead_of_ticketing():
    a = Alliant(customers=[Customer("7192-1-00001", "COSTCO PHARMACY", "12", ["Wed"]),
                           Customer("7192-1-00005", "COSTCO DELI", "12", ["Wed"])],
                items={"7192-1-00001": {"TOWEL SHOP": 10}, "7192-1-00005": {"TOWEL BAR MOP GOLD STRIPE": 800}},
                autocount={"7192-1-00005": {"TOWEL BAR MOP GOLD STRIPE": 400}})
    mats = msg("7192-1-00001", Change(action=Action.decrease, item="3x10 mats", quantity=2), customer="Costco")
    assert check(mats, a).questions == [
        "COSTCO PHARMACY doesn't have any 3x10 mats in Alliant at any of their locations. Which item did you mean?"]
    mops = msg("7192-1-00001", Change(action=Action.decrease, item="bar mops", quantity=2), customer="Costco")
    assert check(mops, a).questions == ["COSTCO PHARMACY doesn't have bar mops in Alliant. It's on COSTCO DELI "
                                        "(400 per delivery). Which location is this for?"]
    new = msg("7192-1-00001", Change(action=Action.add, item="3x10 mats", quantity=2), customer="Costco")
    assert not check(new, a).questions  # adding a new item is fine


def test_review_fixes():
    from .checks import CheckedChange, Result, split_by_account, ticket
    from .run import other_route_account
    # "Fix the inventory to 20": the inventory is 20, not the autocount doubled.
    a = Alliant(items={"S": {"TOWEL BAR MOP GOLD STRIPE": 40}}, autocount={"S": {"TOWEL BAR MOP GOLD STRIPE": 20}})
    r = check(msg("S", Change(action=Action.set, item="bar mops", quantity=20, inventory=True)), a)
    assert r.changes[0].current == 40 and r.changes[0].new_total == 20 and "inventory changed – from 40 to 20" in readback(r)
    # Butcher coats kept on a pool wearer are found; a size alone ("stop L") is not a garment.
    a = Alliant(items={"T": {}}, garments={("T", "2"): {"COAT BUTCHER WHITE XL": 64}, ("T", "4"): {"COAT BUTCHER WHITE L": 10}})
    r = check(msg("T", Change(action=Action.decrease, item="butcher coats", size="XL", quantity=20)), a)
    assert r.changes[0].current == 64 and r.changes[0].new_total == 44 and "on wearer #2" in ticket(r, a)
    r = check(msg("T", Change(action=Action.stop, item="L")), a)
    assert r.changes[0].wearer_number is None
    # A customer on another route, named exactly.
    a = Alliant(customers=[Customer("7518-1-00050", "THREE BEARS (50 KENAI)", "5", ["Mon"]),
                           Customer("7518-1-00051", "THREE BEARS (51 SOLDOTNA)", "5", ["Mon"])])
    p = ParsedMessage(category=Category.item_change, customer_as_written="Three Bears #50", summary="",
                      changes=[Change(action=Action.stop, item="toilet bowl clip")],
                      questions_for_driver=["Which Three Bears store is this?"])
    other_route_account(p, a)
    assert p.account_number == "7518-1-00050" and not p.questions_for_driver
    # Several accounts in one message: one ticket each.
    p = msg("2074-1-00009", Change(action=Action.add, item="bar mops", quantity=5, account_number="2074-1-00009"),
            Change(action=Action.add, item="aprons", quantity=5, account_number="2074-1-00011"))
    assert [x.account_number for x in split_by_account(p)] == ["2074-1-00009", "2074-1-00011"]
    # "Which/what kind of mat" isn't asked when the item matched one line on the account.
    a = Alliant(items={"C": {"MAT CHARCOAL HEATHER 3X5": 1}})
    p = msg("C", Change(action=Action.add, item="3x5 mat", quantity=1))
    p.questions_for_driver = ["What kind of 3x5 mat (color/type)?"]
    assert not any("kind" in q for q in check(p, a).questions)
    # Size swap shows the new size.
    a = Alliant(wearers={}, garments={("M", "7"): {"PANTS NAVY 38X34": 11}})
    p = ParsedMessage(category=Category.wearer_change, customer_as_written="Midas", account_number="M", summary="",
                      changes=[Change(action=Action.size_change, item="pants", wearer="Juztyn", size="38x32")])
    assert "38x32" in ticket(check(p, a), a)


def test_new_wearer_gets_the_standard_quantity():
    from .checks import check, ticket
    from .context import Alliant, Customer, Wearer
    from .schema import Action, Category, Change, ParsedMessage
    a = Alliant(customers=[Customer("M7", "MIDAS 7", "4", ["Tue"])], wearers={"X1": [Wearer("1", "Zed", "Ray")]})
    p = ParsedMessage(category=Category.wearer_change, account_number="M7", summary="new wearer Dave",
                      changes=[Change(action=Action.add, item="shirts", wearer="Dave Smith", size="L")])
    r = check(p, a)
    assert p.changes[0].quantity == 11 and not r.questions
    assert "Add 11" in ticket(r, a) and "standard 11 for a 5-day week; driver didn't say" in ticket(r, a)
    a.wearers = {"M7": [Wearer("1", "Ann", "Lee"), Wearer("2", "Bo", "Ng")]}
    a.garments = {("M7", "1"): {"SHT NAVY L": 13, "PNT NAVY 34": 13}, ("M7", "2"): {"SHT NAVY M": 13}}
    p.changes[0].quantity = None
    r = check(p, a)
    assert p.changes[0].quantity == 13 and "13 like most wearers here" in ticket(r, a)
    p.changes[0].quantity = 7  # the driver said 7: keep it
    assert check(p, a).changes[0].change.quantity == 7


def test_price_type_21_inventory_follows_the_auto_count():
    from .checks import check, ticket
    from .context import Alliant, Customer
    from .schema import Action, Category, Change, ParsedMessage
    a = Alliant(customers=[Customer("K1", "KOBUK", "4", ["Tue"])],
                items={"K1": {"MAT 3X5": 8, "BAR MOP": 40, "APRON": 7, "TOWEL": 10}},
                autocount={"K1": {"MAT 3X5": 4, "BAR MOP": 40, "APRON": 4, "TOWEL": 10}},
                price_type={"K1": {"MAT 3X5": "21", "BAR MOP": "21", "APRON": "21", "TOWEL": "8"}})
    t = lambda item, n: ticket(check(ParsedMessage(category=Category.item_change, account_number="K1", summary="s",
                                                   changes=[Change(action=Action.add, item=item, quantity=n)]), a), a)
    assert "4 → *6*" in t("MAT 3X5", 2) and "PT 21: inventory 8 → *12* too (stays double)" in t("MAT 3X5", 2)
    assert "PT 21: inventory 40 → *50* too" in t("BAR MOP", 10)
    assert "isn't equal to or double the auto count 4 (an old entry mistake): set the inventory to 5 or 10" in t("APRON", 1)
    assert "inventory 10 → *15* too" in t("TOWEL", 5) and "PT 8: " in t("TOWEL", 5)


def test_ratio_check_lists_old_entry_mistakes():
    from .context import Alliant, Customer
    from .ratio_check import mismatches
    a = Alliant(customers=[Customer("K1", "KOBUK", "4", ["Tue"])],
                items={"K1": {"MAT": 8, "BAR MOP": 40, "APRON": 7, "TOWEL": 20, "RAG": 5}},
                autocount={"K1": {"MAT": 4, "BAR MOP": 20, "APRON": 4, "TOWEL": 10, "RAG": 0}})
    [m] = mismatches(a)
    assert (m["item"], m["auto_count"], m["inventory"], m["suggested_inventory"]) == ("APRON", 4, 7, 8)


def test_new_item_ticket_names_the_usual_sku():
    from .checks import usual_sku
    from .context import Alliant
    a = Alliant(sku={("A1", "TOWEL BAR MOP GOLD STRIPE"): "5-01-01", ("A2", "TOWEL BAR MOP GOLD STRIPE"): "5-01-01",
                     ("A3", "TOWEL BAR MOP GOLD STRIPE"): "5-01-01", ("A1", "MAT BLACK 3X5"): "3-01-01",
                     ("A2", "MAT CHARCOAL HEATHER 3X5"): "3-02-01"})
    assert usual_sku(a, "bar mops") == ("5-01-01", "TOWEL BAR MOP GOLD STRIPE", 3)
    assert usual_sku(a, "3x5 mat") is None  # two colors, one each: a person picks
    assert usual_sku(a, "") is None


def test_usual_sku_works_on_a_texters_narrowed_data():
    from .checks import usual_sku
    from .context import Alliant, Customer
    from .sms import only
    a = Alliant(customers=[Customer("5045-1-00000", "HOMER GOLF COURSE", "9", ["Thu"])],
                sku={("A1", "TOWEL BAR MOP GOLD STRIPE"): "5-01-01", ("A2", "TOWEL BAR MOP GOLD STRIPE"): "5-01-01"},
                ar={"A1": {"balance": "10"}, "5045-1-00000": {"balance": "5"}})
    mine = only(a, ["5045-1-00000"])
    assert mine.sku == {} and mine.ar == {}  # nothing of another account's, and no balances at all
    assert usual_sku(mine, "bar mops") == ("5-01-01", "TOWEL BAR MOP GOLD STRIPE", 2)


def test_lookup_and_item_match_run_with_their_models():
    """answer_lookup has no model argument; match_items and parse_message take one (phone.CHECK_MODEL)."""
    from types import SimpleNamespace
    from . import parser
    seen = []
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        parse=lambda **kw: seen.append(kw["model"]) or SimpleNamespace(parsed_output=None))))
    parser.answer_lookup(client, "how many mats", "facts")
    parser.match_items(client, ["mops"], ["MOP WET"], model="claude-sonnet-5-5")
    assert seen == [parser.MODEL, "claude-sonnet-5-5"]


def test_new_feed_files_routes_garments_retired(tmp_path):
    from datetime import datetime
    from .context import ALASKA, Alliant, barcode_facts, route_facts
    (tmp_path / "customers.csv").write_text("account,name,route,service_days\n1205-1-00004,MIDAS #4,4,Mon\n")
    (tmp_path / "route_stops.csv").write_text(
        "branch,route,day,sequence,account,name,address1,city,delivery_notes\n"
        "1,4,Mon,2,1205-1-00004,MIDAS #4,1 A St,Fairbanks,back door\n1,4,Mon,1,1300-1-00001,COSTCO DELI,2 B St,Fairbanks,\n"
        "2,4,Mon,1,7000-2-00001,PROVIDENCE,3 C St,Anchorage,\n")
    (tmp_path / "routes.csv").write_text("branch,route,name,type,area,active\n1,4,R4,,RTE 4 (VALLEY),Y\n")
    (tmp_path / "garments_in_service.csv").write_text(
        "barcode,cust_code,wearer,sku,description,status,status_desc,washes,max_washes,weeks_in_service,repair_desc,"
        "repair_date,customer_name,wearer_name\n000000537456,1205-1-00004,7,6-01-55,SHIRT,O,OUT CLEAN,40,75,52,"
        "BUTTON,2026-10-01,MIDAS #4,JOEY SMITH\n")
    (tmp_path / "garments_retired.csv").write_text(
        "barcode,cust_code,customer_name,wearer,wearer_name,sku,description,sizes,in_service_date,retired_date,retired_as,"
        "weeks_in_service,washes,repairs,last_delivered,reason_code,abused\n000000111222,1205-1-00004,MIDAS #4,7,JOEY SMITH,"
        "6-01-55,SHIRT,L,2024-08-01,2026-10-27,Ragged out,118,74,3,2026-10-20,,\n")
    data = Alliant.from_dir(str(tmp_path))
    facts = route_facts(data, "4", datetime(2026, 10, 26, 9, 0, tzinfo=ALASKA))  # a Monday
    assert "Snow White Linen route 4 (RTE 4 (VALLEY))" in facts and "1. COSTCO DELI (Fairbanks), 2. MIDAS #4" in facts
    assert "first 1300-1-00001 COSTCO DELI, last 1205-1-00004 MIDAS #4" in facts and "MIDAS #4: back door" in facts
    assert "The Laundry Group route 4" in facts and "PROVIDENCE" in facts
    g = barcode_facts(data, ["537456"])  # typed without Alliant's padding zeros
    assert "out clean" in g and "40 of 75 washes" in g and "last repair BUTTON 2026-10-01" in g and "JOEY SMITH" in g
    assert "ragged out 2026-10-27 after 118 weeks" in barcode_facts(data, ["111222"])


def test_skus_csv_with_a_byte_order_mark(tmp_path):
    """The Mac export can start a CSV with a byte-order mark: the first column must still be "sku"."""
    from .context import Alliant, sku_facts
    (tmp_path / "skus.csv").write_bytes("﻿sku,description,weight_oz,price\n7-00-02,BATH BLANKET,30,4.10\n".encode("utf-8"))
    data = Alliant.from_dir(str(tmp_path))
    facts = sku_facts(data, "how much does a bath blanket weigh")
    assert "7-00-02" in facts and "weight 30 oz (= 1.88 lb)" in facts and "4.10" not in facts

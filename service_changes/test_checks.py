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
    assert readback(r) == "✅ Tacos Cancun – 4 3x10 mat added – total now 6"


def test_stated_total_mismatch_asks_driver():
    # The Denali Brewing mix-up: driver meant 28 total, could be read as +28.
    r = check(msg("A1", Change(action=Action.add, item="wet mops", quantity=28, stated_total=28), customer="Denali Brewing"), ALLIANT)
    assert not r.ready
    assert r.questions == ["Denali Brewing has 16 wet mops now. Adding 28 makes 44, but you said total 28. Which is right?"]


def test_set_shows_previous_quantity():
    r = check(msg("A1", Change(action=Action.set, item="wet mops", quantity=28), customer="Denali Brewing"), ALLIANT)
    assert r.ready and r.changes[0].new_total == 28
    assert readback(r) == "✅ Denali Brewing – wet mops set to 28 (was 16)"


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
    assert readback(check(p, ALLIANT)) == "✅ 10th & M – 1 3x10 charcoal heather mat added (weekly)"


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

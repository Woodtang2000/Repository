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
    assert [c.account for c in ALLIANT.candidates("4", "Mon")] == ["A1"]
    assert [c.account for c in ALLIANT.candidates("4", "Wed")] == ["A1", "A3"]  # nobody today: whole route
    assert ALLIANT.match_account("denali brewing company", "4", "Mon") == "A1"
    assert ALLIANT.match_account("Denali", "4", "Mon") is None


def test_item_matching():
    from .checks import _matches
    assert _matches("3x10 mat", "3x10 Charcoal Heather Mat")
    assert _matches("barmops", "Bar Mops") and _matches("bar mop", "Bar Mops")
    assert _matches("4 x 6 charcoal", "4x6 Charcoal Heather Mat")
    assert not _matches("3x5 mat", "3x10 Charcoal Heather Mat")
    assert not _matches("laundry bags", "Bag Stands")
    two = Alliant(items={"A": {"3x5 Charcoal Mat": 1, "3x5 Confetti Mat": 2}})
    from .checks import current_qty
    assert current_qty(two, "A", "3x5 mat") is None  # ambiguous: don't guess
    assert current_qty(two, "A", "3x5 confetti") == ("3x5 Confetti Mat", 2)


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
    assert r.changes[0].notes == ["No total given", "Item not found on account; check by hand"]


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
    assert "A1 | Denali Brewing Company" in p and "Tacos Cancun" not in p and "Moose" not in p

"""Structured shape of one route-channel Slack message after parsing."""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Category(str, Enum):
    item_change = "item_change"              # add / decrease / stop / restart a standing item
    wearer_change = "wearer_change"          # employee garments: new wearer, stop, size change, swap
    hold_or_closure = "hold_or_closure"      # seasonal hold, skip a week, closed, account cancelled
    special_order = "special_order"          # one-time delivery, not on the standing invoice
    today_only = "today_only"                # one delivery changed ("0 bar mops today only"): driver did it on the handheld
    route_or_schedule = "route_or_schedule"  # stop order, service day move, redate
    operational_issue = "operational_issue"  # missing/wrong items, damage, truck, pricing question
    lookup = "lookup"                        # a question about the account: "how many bar mops does Humpy's get?"
    service_code = "service_code"            # flag garments for the plant: repair, inspection/audit, relabel
    route_lookup = "route_lookup"            # a question about the driver's route: today's stops, stop order, who's next
    howto = "howto"                          # how to do something in Alliant or on the handheld
    not_a_request = "not_a_request"          # confirmations, plant completion posts, chatter


class Action(str, Enum):
    add = "add"            # quantity goes up by `quantity`
    decrease = "decrease"  # quantity goes down by `quantity`
    set = "set"            # quantity becomes `quantity` (driver gave only the new total)
    stop = "stop"          # quantity becomes 0
    restart = "restart"    # bring back a stopped item or wearer
    size_change = "size_change"
    other = "other"


class Change(BaseModel):
    action: Action
    item: str = Field(description="Item as a linen tech would say it, e.g. 'bar mops', '3x5 charcoal heather mat', 'shirts'.")
    quantity: Optional[int] = Field(None, description="Amount of the change for add/decrease; the new total for set.")
    stated_total: Optional[int] = Field(None, description="Total the driver said it should end up at ('Total 40', 'making 7 total'). Null if not stated.")
    frequency: Optional[str] = Field(None, description="e.g. 'weekly', 'every other week', 'once a month'. Null if not stated.")
    wearer: Optional[str] = Field(None, description="Employee name for garment changes.")
    size: Optional[str] = Field(None, description="Garment size, e.g. 'L', '2XL', '42x32'.")
    effective: Optional[str] = Field(None, description="When it starts if the driver said so, e.g. 'next week', 'this Friday'.")
    alliant_item: Optional[str] = Field(None, description="Exact Alliant item name from the account's item list, when one was provided and one clearly fits.")
    inventory: bool = Field(False, description=(
        "True only when the driver says the change is to the inventory ('fix the inventory, 6 total', 'inventory "
        "should be 20', 'reduce the inventory by half'), not to the per-delivery count. 'Stop the mats and cut the "
        "inventory in half' is two changes per item: a stop, and an inventory decrease. A driver who 'picked up' or "
        "'brought back' items (usually ones that were stopped) is an inventory decrease by that many."))
    keep_inventory: bool = Field(False, description=(
        "For a stop: True when they say to leave the inventory as it is for now ('leave it', 'keep the inventory', "
        "'don't pick them up yet'). Inventory to zero or cut down is its own inventory change instead."))
    fraction: Optional[float] = Field(None, description=(
        "A share of what's there now instead of a number: 'by half' / 'cut it in half' = 0.5, 'by a third' = 0.33, "
        "'a quarter' = 0.25, with action decrease (quantity null). The office works out the number."))
    account_number: Optional[str] = Field(None, description=(
        "Only when the message covers more than one account (two departments or stores): this change's own account "
        "from the candidate list. Null when the whole message is for one account."))


class ServiceFlag(BaseModel):
    """A service code the office puts on garments in Alliant so the plant pulls them at the next scan."""
    scope: str = Field(description="'garment' (specific barcodes), 'wearer' (all of one employee's garments) or "
                                   "'customer' (every garment on the account, e.g. an audit).")
    kind: str = Field(description="'repair' (tear, button, zipper, hem, patch), 'inspection' (audit, condition check, "
                                  "worn out?), 'relabel' (new label/emblem/name) or 'other'.")
    reason: str = Field(description="What the driver said is wrong or wanted, short: 'torn left pocket', 'audit all garments'.")
    barcodes: list[str] = Field(default_factory=list, description="Garment barcode numbers exactly as written.")
    wearer: Optional[str] = Field(None, description="Employee name when the scope is a wearer (or the garments are theirs).")


class ParsedMessage(BaseModel):
    category: Category
    customer_as_written: Optional[str] = Field(None, description="Customer name exactly as the driver wrote it.")
    account_number: Optional[str] = Field(None, description="Account number from the candidate list, only when the match is clear. Null otherwise.")
    changes: list[Change] = Field(default_factory=list)
    questions_for_driver: list[str] = Field(
        default_factory=list,
        description="Short questions that must be answered before anyone can enter this in Alliant.",
    )
    summary: str = Field(description="One line saying what was asked.")
    service: Optional[ServiceFlag] = Field(None, description="Only for service_code: what to flag and why.")

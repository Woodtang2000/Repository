"""Structured shape of one route-channel Slack message after parsing."""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Category(str, Enum):
    item_change = "item_change"              # add / decrease / stop / restart a standing item
    wearer_change = "wearer_change"          # employee garments: new wearer, stop, size change, swap
    hold_or_closure = "hold_or_closure"      # seasonal hold, skip a week, closed, account cancelled
    special_order = "special_order"          # one-time delivery, not on the standing invoice
    route_or_schedule = "route_or_schedule"  # stop order, service day move, redate
    operational_issue = "operational_issue"  # missing/wrong items, damage, truck, pricing question
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

"""Deterministic checks and readback text. No AI here: arithmetic stays in code."""
import re
from dataclasses import dataclass, field

from .context import Alliant
from .schema import Action, Category, Change, ParsedMessage


def _words(s: str) -> set[str]:
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", s.lower().replace(" x ", "x"))}


def _joined(s: str) -> str:
    return "".join(sorted(_words(s), key=s.lower().find))


def _matches(driver_item: str, alliant_item: str) -> bool:
    # "3x10 mat" fits "3x10 Charcoal Heather Mat"; "barmops" fits "Bar Mops".
    want, have = _words(driver_item), _words(alliant_item)
    return bool(want) and (want <= have or _joined(driver_item) == _joined(alliant_item))


def current_qty(alliant: Alliant, account: str | None, item: str) -> tuple[str, int] | None:
    """Find the account's line item that matches `item`. Returns None unless exactly one matches."""
    lines = alliant.items.get(account or "", {})
    hits = [(name, qty) for name, qty in lines.items() if _matches(item, name)]
    return hits[0] if len(hits) == 1 else None


@dataclass
class CheckedChange:
    change: Change
    current: int | None = None
    new_total: int | None = None
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Result:
    parsed: ParsedMessage
    changes: list[CheckedChange]

    @property
    def questions(self) -> list[str]:
        return self.parsed.questions_for_driver + [q for c in self.changes for q in c.questions]

    @property
    def ready(self) -> bool:
        return self.parsed.category in (Category.item_change, Category.wearer_change) and not self.questions


def check(parsed: ParsedMessage, alliant: Alliant) -> Result:
    who = parsed.customer_as_written or "this customer"
    out = []
    for ch in parsed.changes:
        cc = CheckedChange(ch)
        found = current_qty(alliant, parsed.account_number, ch.item) if not ch.wearer else None
        if found:
            cc.current = found[1]
        q = ch.quantity

        if ch.action == Action.stop:
            cc.new_total = 0 if cc.current is not None else None
        elif ch.action == Action.set:
            cc.new_total = q
        elif ch.action in (Action.add, Action.decrease) and q is not None and cc.current is not None:
            cc.new_total = cc.current + q if ch.action == Action.add else cc.current - q

        if cc.current is not None and ch.action == Action.decrease and q is not None and q > cc.current:
            cc.questions.append(f"{who} only has {cc.current} {ch.item} now. Decrease by {q}? What should the total be?")
        elif cc.current is not None and ch.stated_total is not None and cc.new_total is not None and ch.stated_total != cc.new_total:
            verb = {Action.add: "Adding", Action.decrease: "Decreasing", Action.set: "Setting to", Action.stop: "Stopping"}.get(ch.action, "This")
            cc.questions.append(
                f"{who} has {cc.current} {ch.item} now. {verb} {q if q is not None else ''} makes {cc.new_total}, "
                f"but you said total {ch.stated_total}. Which is right?".replace("  ", " ")
            )
        elif cc.new_total is None and ch.stated_total is not None:
            cc.new_total = ch.stated_total  # nothing in Alliant to check against; trust the driver's total

        if ch.action in (Action.add, Action.decrease) and ch.stated_total is None and not ch.wearer:
            cc.notes.append("No total given")
        if cc.current is None and not ch.wearer and parsed.account_number:
            cc.notes.append("Item not found on account; check by hand")
        out.append(cc)
    return Result(parsed, out)


VERBS = {Action.add: "added", Action.decrease: "decreased", Action.stop: "stopped", Action.restart: "restarted",
         Action.size_change: "size changed"}


def _what(ch: Change) -> str:
    qty = str(ch.quantity) if ch.quantity is not None and ch.action != Action.set else None
    if ch.size and " " in ch.size:  # "same size", "same as before"
        return " ".join(x for x in [qty, ch.item] if x) + f" ({ch.size})"
    return " ".join(x for x in [qty, ch.size, ch.item] if x)


def _tail(cc: CheckedChange) -> str:
    ch, s = cc.change, ""
    if cc.new_total is not None and ch.action not in (Action.stop, Action.set) and not ch.wearer:
        s += f" – total now {cc.new_total}"
    if cc.current is not None and ch.action in (Action.set, Action.stop):
        s += f" (was {cc.current})"
    if ch.frequency:
        s += f" ({ch.frequency})"
    if ch.effective:
        s += f" – {ch.effective}" if ch.effective.lower().startswith("until") else f" – starts {ch.effective}"
    return s


def readback(result: Result) -> str:
    """Tower-style confirmation for staff to post after entering the change in Alliant."""
    p = result.parsed
    if p.category == Category.hold_or_closure and not result.changes:
        return f"✅ {p.customer_as_written} – {p.summary}"
    parts: list[str] = []
    i = 0
    while i < len(result.changes):
        cc = result.changes[i]
        ch = cc.change
        if ch.wearer:
            # One wearer, one action: "Karissa: 11 M shirts, 11 32x30 pants added"
            group = [cc]
            while i + len(group) < len(result.changes):
                nxt = result.changes[i + len(group)].change
                if nxt.wearer != ch.wearer or nxt.action != ch.action:
                    break
                group.append(result.changes[i + len(group)])
            parts.append(f"{ch.wearer}: " + ", ".join(_what(g.change) for g in group)
                         + f" {VERBS.get(ch.action, 'changed')}" + _tail(group[-1]))
            i += len(group)
            continue
        verb = f"set to {ch.quantity}" if ch.action == Action.set else VERBS.get(ch.action, "changed")
        parts.append(f"{_what(ch)} {verb}{_tail(cc)}")
        i += 1
    return f"✅ {p.customer_as_written} – " + "; ".join(parts) if parts else ""

"""Deterministic checks and readback text. No AI here: arithmetic stays in code."""
import re
from dataclasses import dataclass, field

from .context import Alliant, frequency_label
from .schema import Action, Category, Change, ParsedMessage


# Driver wording -> Alliant wording, applied before comparing words.
SYNONYMS = [
    (r"(\d+)\s*x\s*(\d+)", r"\1x\2"),             # "4 x 6" -> "4x6"
    (r"(\d+)\s*oz\b", r"\1oz"),                   # "24 oz" -> "24oz"
    (r"\b(?:orange|blue)\s+(?=\d+\s*oz|mop|wet)", ""),  # mop head colors just mark the size (24oz orange, 16oz blue)
    (r"\bmop ?heads?\b|\bwet ?mops?\b", "mop wet"),
    (r"\bbar ?mops?\b", "bar mop"),
    (r"\bwater ?hogs?\b", "waterhog"),
    (r"\bmicro ?fibers?\b", "microfiber"),
    (r"\btable ?cloths?\b", "t/c"),
    (r"\bbrandy ?wine\b", "brandywine"),
]
FILLER = {"mat", "towel", "the", "a", "of", "and", "s"}  # too generic to tell lines apart on their own


def _words(s: str) -> set[str]:
    s = s.lower()
    for pat, rep in SYNONYMS:
        s = re.sub(pat, rep, s)
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9/]+", s)}


_VOCAB: dict[int, set[str]] = {}


def alliant_vocab(alliant: Alliant) -> set[str]:
    """Every word used in any Alliant item name."""
    key = id(alliant.items)
    if key not in _VOCAB:
        _VOCAB[key] = set().union(*(_words(n) for lines in alliant.items.values() for n in lines))
    return _VOCAB[key]


def current_qty(alliant: Alliant, account: str | None, item: str) -> tuple[str, int] | None:
    """The account's line item that `item` refers to, or None unless exactly one fits.

    Every word the driver used must appear in the Alliant item, except words that appear nowhere
    in Alliant ("please", "new"). A real color like "brandywine" is kept, so "4x6 brandywine"
    won't match a charcoal 4x6.
    """
    lines = alliant.items.get(account or "", {})
    if item in lines:
        return item, lines[item]
    known = _words(item) & alliant_vocab(alliant)
    if not known - FILLER:
        return None
    hits = [(n, q) for n, q in lines.items() if known <= _words(n)]
    return hits[0] if len(hits) == 1 else None


@dataclass
class CheckedChange:
    change: Change
    alliant_item: str | None = None
    frequency_now: str | None = None  # Alliant code for the matched item
    already_done: bool = False
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
        found = current_qty(alliant, parsed.account_number, ch.alliant_item or ch.item) if not ch.wearer else None
        if found:
            cc.alliant_item, cc.current = found
            cc.frequency_now = alliant.frequency.get(parsed.account_number or "", {}).get(cc.alliant_item)
        q = ch.quantity

        if ch.action == Action.stop:
            cc.new_total = 0 if cc.current is not None else None
        elif ch.action == Action.set:
            cc.new_total = q
        elif ch.action in (Action.add, Action.decrease) and q is not None and cc.current is not None:
            cc.new_total = cc.current + q if ch.action == Action.add else cc.current - q

        target = ch.stated_total if ch.stated_total is not None else (q if ch.action == Action.set else None)
        if cc.current is not None and target is not None and cc.current == target:
            # Alliant already shows what the driver asked for: entered already, or a repeat request.
            cc.already_done = True
            cc.new_total = cc.current
            cc.notes.append(f"Alliant already shows {cc.current}; may already be entered")
        elif cc.current == 0 and ch.action == Action.stop:
            cc.already_done = True
            cc.notes.append("Already 0 in Alliant")
        elif cc.current is not None and ch.action == Action.decrease and q is not None and q > cc.current:
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
        if cc.current is None and not ch.wearer and parsed.account_number and alliant.items:
            cc.notes.append("Not on this account in Alliant" + (" (may already be stopped)" if ch.action == Action.stop else "; check by hand"))
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
        m = re.fullmatch(r"(?:freq(?:uency)?\s*)?([0-9][0-9]?|[A-Z][0-9])", ch.frequency.strip(), re.I)
        s += f" ({frequency_label(m.group(1)) if m else ch.frequency})"
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

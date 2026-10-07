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
    (r"\bshort ?sleeves?\b|\bss\b", "s/s"),
    (r"\blong ?sleeves?\b|\bls\b", "l/s"),
]
COLORS = {"black", "blue", "brandywine", "brown", "burgundy", "charcoal", "confetti", "gold", "gray", "green", "grey",
          "ivory", "navy", "orange", "red", "white", "yellow", "heather", "galaxy"}


def colors_agree(driver_item: str, alliant_item: str) -> bool:
    """A color the driver names must be in the Alliant item ("4x6 brandywine" is never MAT CHARCOAL HEATHER 4X6).
    Orange and blue are exempt for mop heads, where they only mark the size."""
    said = (_words(driver_item) & COLORS) - ({"orange", "blue"} if "mop" in driver_item.lower() else set())
    return said <= _words(alliant_item)


FILLER = {"mat", "towel", "the", "a", "of", "and", "s"}  # too generic to tell lines apart on their own


def _words(s: str) -> set[str]:
    s = s.lower()
    for pat, rep in SYNONYMS:
        s = re.sub(pat, rep, s)
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9/]+", s)}


def alliant_vocab(alliant: Alliant) -> set[str]:
    """Every word used in any Alliant item or garment name."""
    if getattr(alliant, "_vocab", None) is None:
        names = [n for lines in alliant.items.values() for n in lines] + [n for g in alliant.garments.values() for n in g]
        alliant._vocab = set().union(*(_words(n) for n in names))
    return alliant._vocab


def _find(lines: dict[str, int], item: str, vocab: set[str]) -> tuple[str, int] | None:
    if item in lines:
        return item, lines[item]
    known = _words(item) & vocab
    if not known - FILLER:
        return None
    hits = [(n, q) for n, q in lines.items() if known <= _words(n)]
    return hits[0] if len(hits) == 1 else None


def current_qty(alliant: Alliant, account: str | None, item: str) -> tuple[str, int] | None:
    """The account's line item that `item` refers to, or None unless exactly one fits.

    Every word the driver used must appear in the Alliant item, except words that appear nowhere
    in Alliant ("please", "new"). A real color like "brandywine" is kept, so "4x6 brandywine"
    won't match a charcoal 4x6.
    """
    return _find(alliant.items.get(account or "", {}), item, alliant_vocab(alliant))


@dataclass
class CheckedChange:
    change: Change
    alliant_item: str | None = None
    wearer_number: str | None = None
    frequency_now: str | None = None  # Alliant code for the matched item
    already_done: bool = False
    current: int | None = None  # autocount (per delivery) where known; Alliant inventory otherwise
    inventory: int | None = None  # set only when Alliant inventory differs from the autocount
    new_total: int | None = None
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_FILLER = {"what", "which", "does", "do", "did", "the", "for", "and", "need", "needs", "should", "they", "have",
           "has", "get", "gets", "is", "are", "a", "an", "of", "to", "on", "or", "be", "will", "you", "it", "this"}


def _topic(q: str) -> set[str]:
    """The words of a question that matter, singular: "What size shirts for Tim?" -> {size, shirt, tim}."""
    words = re.findall(r"[a-z0-9]+", q.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _FILLER}


@dataclass
class Result:
    parsed: ParsedMessage
    changes: list[CheckedChange]
    customer_name: str | None = None  # Alliant's name for parsed.account_number, when it's a known account

    @property
    def questions(self) -> list[str]:
        asked = list(self.parsed.questions_for_driver)
        for q in (q for c in self.changes for q in c.questions):
            # Claude often already asked it in its own words ("What shirt size does Tim need?" vs our
            # "What size shirts for Tim?"): skip ours when every word that matters is already in one of theirs.
            if not any(_topic(q) <= _topic(a) for a in asked):
                asked.append(q)
        return asked

    @property
    def ready(self) -> bool:
        return self.parsed.category in (Category.item_change, Category.wearer_change) and not self.questions


def _check_wearer(cc: CheckedChange, account: str | None, alliant: Alliant) -> tuple[str, int] | None:
    """Look the wearer up by name; return their matching garment line, if one fits."""
    ch = cc.change
    if not alliant.wearers or not account:
        return None
    w = alliant.find_wearer(account, ch.wearer)
    if w is None:
        if ch.action == Action.add and not ch.size:
            cc.questions.append(f"What size {ch.item} for {ch.wearer}?")
        if ch.action == Action.stop:
            cc.already_done = True
            cc.notes.append(f"No wearer named {ch.wearer} on this account (may already be stopped)")
        elif ch.action == Action.add:
            cc.notes.append(f"{ch.wearer} is not in Alliant yet (new wearer)")
        else:
            cc.notes.append(f"No wearer named {ch.wearer} on this account; check by hand")
        return None
    cc.wearer_number = w.employee
    lines = alliant.garments.get((account, w.employee), {})
    found = _find(lines, ch.item, alliant_vocab(alliant))
    if not found and lines and (ch.action == Action.stop or "all" in ch.item.lower()):
        cc.notes.append(f"#{w.employee} {w.name} has " + ", ".join(f"{q} {n}" for n, q in lines.items()))
    return found


def _autocount(cc: CheckedChange, alliant: Alliant, account: str | None) -> int:
    """The per-delivery autocount drivers talk about, from the Alliant inventory number.

    Some accounts keep inventory equal to the autocount, others at double it. Use the real
    autocount when the export has one; otherwise take whichever reading fits the driver's numbers.
    """
    ch, inventory = cc.change, cc.current
    known = alliant.autocount.get(account or "", {}).get(cc.alliant_item)
    if known is not None:
        if known != inventory:
            cc.inventory = inventory
        return known
    target = ch.stated_total if ch.stated_total is not None else (ch.quantity if ch.action == Action.set else None)
    if target is None or inventory % 2 or inventory == 0:
        return inventory

    def result(base):  # what an add/decrease would leave; a set only fits when base already equals it
        q = ch.quantity or 0
        return {Action.add: base + q, Action.decrease: base - q}.get(ch.action)

    if inventory == target or result(inventory) == target:
        return inventory
    half = inventory // 2
    if half == target or result(half) == target:
        cc.inventory = inventory
        cc.notes.append(f"Alliant inventory is {inventory}, double the autocount of {half}")
        return half
    return inventory


def _not_here(cc: CheckedChange, account: str, alliant: Alliant) -> None:
    """Remove / change an item the account doesn't have: look at the customer's other locations ("Costco" means
    seven departments) and ask the driver, instead of sending the office a ticket for nothing."""
    ch = cc.change
    if alliant.on_hold(account) and _find({h["item"]: 0 for h in alliant.on_hold(account)}, ch.item, alliant_vocab(alliant)):
        return  # it's there, on hold: the ticket says so
    name = lambda a: next((c.name for c in alliant.customers if c.account == a), a)
    base = account.rsplit("-", 1)[0]
    hits = []
    for c in alliant.customers:
        if c.account != account and c.account.rsplit("-", 1)[0] == base:
            found = current_qty(alliant, c.account, ch.item)
            if found:
                per = alliant.autocount.get(c.account, {}).get(found[0], found[1])
                hits.append(f"{c.name} ({per} per delivery)")
    if hits:
        more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
        cc.questions.append(f"{name(account)} doesn't have {ch.item} in Alliant. "
                            + ("It's on " if len(hits) == 1 else "They're on ") + "; ".join(hits[:3]) + more
                            + ". Which location is this for?")
    elif ch.action != Action.stop:  # a stop for something not there is most likely already done
        anywhere = " at any of their locations" if any(c.account.rsplit("-", 1)[0] == base and c.account != account
                                                       for c in alliant.customers) else ""
        cc.questions.append(f"{name(account)} doesn't have any {ch.item} in Alliant{anywhere}. Which item did you mean?")


def check(parsed: ParsedMessage, alliant: Alliant) -> Result:
    who = parsed.customer_as_written or "this customer"
    out = []
    for ch in parsed.changes:
        cc = CheckedChange(ch)
        if ch.wearer:
            found = _check_wearer(cc, parsed.account_number, alliant)
        else:
            found = current_qty(alliant, parsed.account_number, ch.alliant_item or ch.item)
        if found:
            cc.alliant_item, cc.current = found
            cc.frequency_now = alliant.frequency.get(parsed.account_number or "", {}).get(cc.alliant_item)
            if not ch.wearer:
                cc.current = _autocount(cc, alliant, parsed.account_number)
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
        elif ch.wearer and ch.action == Action.add and cc.current is not None and cc.current == q:
            # "Add 11 pants for Karissa" and she already has exactly 11: almost always entered already.
            cc.already_done = True
            cc.new_total = cc.current
            cc.notes.append(f"Already has {cc.current}; may already be entered")
        elif (ch.action == Action.add and ch.stated_total is None and q and cc.current == q and not ch.wearer):
            # "Add two 3x5 mats" when they have 2: two more, or two in all? (Ana had to ask this one.)
            cc.questions.append(f"{who} has {cc.current} {ch.item} now. Add {q} more (total {cc.current + q}), "
                                f"or should they have {q} total?")
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
            if ch.action in (Action.decrease, Action.set, Action.stop):
                _not_here(cc, parsed.account_number, alliant)
        out.append(cc)
    cust = next((c for c in alliant.customers if c.account == parsed.account_number), None) if parsed.account_number else None
    return Result(parsed, out, cust.name if cust else None)


VERBS = {Action.add: "added", Action.decrease: "decreased", Action.stop: "stopped", Action.restart: "restarted",
         Action.size_change: "size changed"}


def _what(ch: Change) -> str:
    qty = str(ch.quantity) if ch.quantity is not None and ch.action != Action.set else None
    if ch.size and " " in ch.size:  # "same size", "same as before"
        return " ".join(x for x in [qty, ch.item] if x) + f" ({ch.size})"
    return " ".join(x for x in [qty, ch.size, ch.item] if x)


def _freq_words(text: str) -> str:
    """"frequency 7" or "7" -> "weekly"; plain words are kept."""
    m = re.fullmatch(r"(?:freq(?:uency)?\s*)?([0-9][0-9]?|[A-Z][0-9])", text.strip(), re.I)
    return frequency_label(m.group(1)) if m else text


def _tail(cc: CheckedChange) -> str:
    ch, s = cc.change, ""
    if cc.new_total is not None and ch.action not in (Action.stop, Action.set) and not ch.wearer:
        s += f" – total now {cc.new_total}"
    if cc.current is not None and ch.action in (Action.set, Action.stop):
        s += f" (was {cc.current})"
    if ch.frequency:
        s += f" ({_freq_words(ch.frequency)})"
    if ch.effective:
        s += f" – {ch.effective}" if ch.effective.lower().startswith("until") else f" – starts {ch.effective}"
    return s


def readback(result: Result) -> str:
    """Tower-style confirmation for staff to post after entering the change in Alliant."""
    p = result.parsed
    # The account and Alliant's name for it, so the driver can see which customer was actually changed.
    who = f"{p.account_number} {result.customer_name}" if result.customer_name else p.customer_as_written
    if p.category == Category.hold_or_closure and not result.changes:
        return f"✅ {who} – {p.summary}"
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
            parts.append(f"{ch.wearer}: " + ", ".join(
                _what(g.change) + (f" (total {g.new_total})" if g.new_total is not None and g.change.action in (Action.add, Action.decrease) else "")
                for g in group)
                         + f" {VERBS.get(ch.action, 'changed')}" + _tail(group[-1]))
            i += len(group)
            continue
        verb = f"set to {ch.quantity}" if ch.action == Action.set else VERBS.get(ch.action, "changed")
        parts.append(f"{_what(ch)} {verb}{_tail(cc)}")
        i += 1
    return f"✅ {who} – " + "; ".join(parts) if parts else ""


ICONS = {Action.add: "➕", Action.decrease: "➖", Action.set: "🔢", Action.stop: "⛔", Action.restart: "🔄",
         Action.size_change: "📏", Action.other: "✏️"}


def _do(ch: Change) -> str:
    """What to key in, verb first: "Add 2", "Set to 6", "Stop"."""
    q = ch.quantity
    return {Action.add: f"Add {q}" if q is not None else "Add",
            Action.decrease: f"Remove {q}" if q is not None else "Decrease",
            Action.set: f"Set to {q}",
            Action.stop: "Stop", Action.restart: "Restart", Action.size_change: "Size change"}.get(ch.action, "Change")


def ticket(result: Result, alliant: Alliant, with_readback: bool = True) -> str:
    """Work ticket for the office: account and name, then one line per thing to key into Alliant, verb first.
    The same fields are what an automated entry needs."""
    p = result.parsed
    acct = p.account_number
    cust = next((c for c in alliant.customers if c.account == acct), None)
    card = alliant.cards.get(acct or "", {})
    if cust:
        out = [f"*{acct}*  {cust.name}"]
    else:
        out = [f"⚠️ *Account not matched*  {p.customer_as_written or 'customer?'}: please look it up"]
    note = (card.get("special_instructions") or "").strip()
    if note and "Remittance" not in note:
        out.append(f"_📝 {note}_")
    for cc in result.changes:
        ch = cc.change
        if ch.wearer:
            who = f"#{cc.wearer_number} {ch.wearer}" if cc.wearer_number else f"{ch.wearer} _(new wearer)_"
            what = cc.alliant_item or " ".join(x for x in [ch.size, ch.item] if x)
            sku = alliant.sku.get((acct, cc.wearer_number, cc.alliant_item)) if cc.wearer_number and cc.alliant_item else None
            what = f"{who}: {what}"
        else:
            what = cc.alliant_item or f"{ch.item} _(not on account yet)_"
            sku = alliant.sku.get((acct, cc.alliant_item)) if cc.alliant_item else None
        bits = [f"{ICONS.get(ch.action, '✏️')} *{_do(ch)}*  {what}" + (f" `{sku}`" if sku else "")]
        if cc.current is not None and cc.new_total is not None:
            bits.append(f"{cc.current} → *{cc.new_total}*")
        elif cc.new_total is not None and ch.action != Action.set:
            bits.append(f"new total *{cc.new_total}*")
        freq = _freq_words(ch.frequency) if ch.frequency else frequency_label(cc.frequency_now)
        if freq:
            bits.append(freq)
        if ch.effective:
            bits.append(ch.effective)
        if cc.already_done:
            bits.append("☑️ already in Alliant")
        if cc.alliant_item and alliant.on_hold(acct, cc.alliant_item, sku):
            bits.append("⏸️ on hold in Alliant")
        elif not cc.alliant_item and not ch.wearer:
            # Not an active line, but maybe a held one: taking it off hold beats adding a new line.
            held = _find({h["item"]: 0 for h in alliant.on_hold(acct) if h.get("item")}, ch.item, alliant_vocab(alliant))
            if held:
                bits.append(f"⏸️ *{held[0]}* is on this account but on hold in Alliant")
        out.append(" · ".join(bits))
    if p.category == Category.hold_or_closure and not result.changes:
        out.append(f"⏸️ *{p.summary}*")
    if result.questions:
        out += [f"❓ {q}" for q in result.questions]
    elif with_readback and (rb := readback(result)):
        out.append(f"Readback when done: {rb}")
    return "\n".join(out)

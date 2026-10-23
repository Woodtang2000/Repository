"""Did the office enter it right? The morning after a ticket is ✅'d, compare what the ticket said the account would
have with what the nightly Alliant export shows, and flag the ones that don't match.

A ticket carries its expected end state in its metadata (`expect`, from `expectations()` when it is posted), and the
✅ adds `done_at`. Only tickets whose ✅ came before the export was taken are checked, and each is checked once.
"""
from .checks import Result, _find, alliant_vocab, current_qty
from .context import Alliant
from .schema import Action

CHECKED = (Action.add, Action.decrease, Action.set, Action.stop)


def expectations(result: Result) -> list[dict]:
    """What the account should look like once the ticket is entered: one entry per change the export can show."""
    out = []
    for cc in result.changes:
        ch = cc.change
        if ch.wearer and not cc.wearer_number:
            if ch.action == Action.add:  # a new wearer: they should exist by the next export
                out.append({"new_wearer": ch.wearer, "say": f"new wearer {ch.wearer}"})
            continue
        if ch.action not in CHECKED or cc.new_total is None:
            continue
        what = cc.alliant_item or " ".join(x for x in [ch.size, ch.item] if x)
        out.append({"item": what, "matched": bool(cc.alliant_item), "wearer": cc.wearer_number or "",
                    "total": int(cc.new_total), "inventory": bool(getattr(ch, "inventory", False)),
                    "say": f"{what}" + (f" (wearer #{cc.wearer_number})" if cc.wearer_number else "")})
    return out


def _actual(alliant: Alliant, account: str, e: dict) -> tuple[str | None, int | None, str]:
    """(line found, number Alliant shows, which number) for one expectation."""
    if e.get("wearer"):
        lines = alliant.garments.get((account, e["wearer"]), {})
        name = e["item"] if e["item"] in lines else (_find(lines, e["item"], alliant_vocab(alliant)) or (None,))[0]
        return name, (lines.get(name) if name else None), "garments"
    items = alliant.items.get(account, {})
    name = e["item"] if e["item"] in items else (current_qty(alliant, account, e["item"]) or (None,))[0]
    if not name:
        return None, None, ""
    if e.get("inventory"):
        return name, items.get(name), "inventory"
    auto = alliant.autocount.get(account, {}).get(name)
    return (name, auto, "autocount") if auto is not None else (name, items.get(name), "inventory")


def problems(alliant: Alliant, account: str, expect: list[dict]) -> list[str]:
    """What doesn't match, in words for the office. Empty when everything does."""
    out = []
    for e in expect:
        if "new_wearer" in e:
            who = e["new_wearer"].lower().split()
            if not any(all(w in x.name.lower() for w in who) for x in alliant.wearers.get(account, [])):
                out.append(f"New wearer {e['new_wearer']} isn't on the account in Alliant")
            continue
        name, qty, kind = _actual(alliant, account, e)
        want = e["total"]
        if want == 0:
            if name and qty:
                out.append(f"{name}: should be stopped, Alliant still shows {qty} ({kind})")
        elif name is None:
            out.append(f"{e['say']}: not on the account in Alliant (expected {want})")
        elif qty != want and not (kind == "inventory" and not e.get("inventory") and qty == 2 * want):
            out.append(f"{name}: expected {want}, Alliant shows {qty if qty is not None else 0} ({kind})"
                       + (" — nothing goes out" if not qty else ""))
    return out

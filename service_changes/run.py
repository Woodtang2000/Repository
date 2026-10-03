"""Parse route-channel messages and print what the office would see.

  python -m service_changes.run service_changes/examples/messages.json \
      --customers service_changes/examples/demo_customers.csv \
      --items service_changes/examples/demo_current_items.csv

Modes:
  (default)        call Claude for each message (needs ANTHROPIC_API_KEY)
  --replay LABELS  use hand-labeled results instead of Claude (no API key needed)
  --eval LABELS    call Claude and score it against the hand labels
"""
import argparse
import json
from datetime import datetime

from .checks import check, readback, ticket
from .context import ALASKA, Alliant, frequency_label, route_from_channel, service_day
from .schema import Category, ParsedMessage

ACTIONABLE = {Category.item_change, Category.wearer_change, Category.hold_or_closure, Category.special_order}


def load_messages(path: str) -> list[dict]:
    with open(path) as f:
        msgs = json.load(f)
    for m in msgs:
        if "ts" not in m:
            m["ts"] = str(datetime.fromisoformat(m["posted"]).replace(tzinfo=ALASKA).timestamp())
    return msgs


def render(m: dict, parsed: ParsedMessage, alliant: Alliant) -> str:
    route, day = route_from_channel(m["channel"]), service_day(m["ts"])
    if not parsed.account_number:
        parsed.account_number = alliant.match_account(parsed.customer_as_written, route, day)
    result = check(parsed, alliant)
    quoted = "\n".join("> " + line for line in m["text"].splitlines())
    out = [f"### #{m['channel']} · {day} · {m.get('user', '')}", quoted, "",
           f"**{parsed.category.value}** · {parsed.customer_as_written or '—'}"
           + (f" · acct {parsed.account_number}" if parsed.account_number
              else " · acct ? (not matched)" if alliant.customers and parsed.category in ACTIONABLE else "")
           + f" · {parsed.summary}"]
    if parsed.category in ACTIONABLE:
        for cc in result.changes:
            bits = [f"{cc.change.action.value} {cc.change.quantity if cc.change.quantity is not None else ''} {cc.change.item}".replace("  ", " ")]
            if cc.change.wearer:
                bits.append(f"wearer {cc.change.wearer}" + (f" (#{cc.wearer_number})" if cc.wearer_number else ""))
            if cc.current is not None:
                freq = frequency_label(cc.frequency_now)
                bits.append(f"Alliant: {cc.alliant_item} = {cc.current}" + (f" (inventory {cc.inventory})" if cc.inventory else "")
                            + (f" ({freq})" if freq else ""))
            if cc.new_total is not None:
                bits.append(f"new total {cc.new_total}")
            bits += cc.notes
            out.append("- " + " · ".join(bits))
        if result.changes and all(c.already_done for c in result.changes):
            out += ["", "☑️ Alliant already matches this request. Nothing to enter."]
        elif result.questions:
            out.append("")
            out += [f"❓ Ask driver: {q}" for q in result.questions]
        elif rb := readback(result):
            out += ["", f"Proposed readback: `{rb}`"]
        out += ["", "Office ticket:", *("> " + t for t in ticket(result, alliant).splitlines())]
    else:
        out.append("_No account change; not queued._")
    return "\n".join(out)


def fix_department(parsed: ParsedMessage, alliant: Alliant, route: str | None, day: str) -> None:
    """A garment change names a wearer: make sure the account is the department that wearer is on.

    "Raven @ Safeway 1817 needs a meat coat" can land on SAFEWAY 1817 when Raven is on SAFEWAY 1817 (MEAT).
    If the chosen account doesn't have the wearer and exactly one sister account (same name before the
    brackets, same route) does, switch to it.
    """
    names = [c.wearer for c in parsed.changes if c.wearer]
    if not names or not parsed.account_number or not alliant.wearers:
        return
    if any(alliant.find_wearer(parsed.account_number, n) for n in names):
        return
    by_acct = {c.account: c for c in alliant.customers}
    chosen = by_acct.get(parsed.account_number)
    if not chosen:
        return
    base = chosen.name.split("(")[0].strip().upper()
    sisters = [c.account for c in alliant.candidates(route, day)
               if c.account != chosen.account and c.name.split("(")[0].strip().upper() == base]
    hits = [a for a in sisters if all(alliant.find_wearer(a, n) for n in names)]
    if len(hits) == 1:
        parsed.account_number = hits[0]


def fill_item_matches(client, parsed: ParsedMessage, alliant: Alliant) -> None:
    """Ask Claude to place any item the word match couldn't find on the account."""
    from .checks import current_qty
    from .parser import match_items

    account_items = list(alliant.items.get(parsed.account_number or "", {}))
    todo = [c for c in parsed.changes
            if not c.wearer and account_items and current_qty(alliant, parsed.account_number, c.item) is None]
    if todo:
        for c, name in zip(todo, match_items(client, [c.item for c in todo], account_items)):
            c.alliant_item = name


def _end_state(c) -> tuple:
    """What a change leaves in Alliant, so "add 4, total 6" and "set to 6" score the same."""
    wearer = (c.wearer or "").lower()
    if c.action.value in ("add", "decrease") and c.stated_total is not None:
        return (wearer, "total", c.stated_total)
    if c.action.value == "set":
        return (wearer, "total", c.quantity)
    if c.action.value == "stop":
        return (wearer, "total", 0)
    if c.quantity is None:  # "reduce all towels in half": the kind of change matters less than that it's flagged
        return (wearer, "change", None)
    return (wearer, c.action.value, c.quantity)


def score(parsed: ParsedMessage, label: ParsedMessage, alliant: Alliant | None = None) -> list[str]:
    """Differences that matter for entering the change. Wording of items and questions is not scored."""
    issues = []
    if parsed.category != label.category:
        issues.append(f"category {parsed.category.value} != {label.category.value}")
    if label.account_number and parsed.account_number != label.account_number:
        issues.append(f"account {parsed.account_number} != {label.account_number}")
    got = sorted(_end_state(c) for c in parsed.changes)
    want = sorted(_end_state(c) for c in label.changes)
    if got != want:
        issues.append(f"changes {got} != {want}")
    # Count every question the bot would show, including ones the Alliant checks add.
    asked = check(parsed, alliant).questions if alliant else parsed.questions_for_driver
    if bool(asked) != bool(label.questions_for_driver):
        issues.append("asked a question" if asked else "missed a question")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("messages")
    ap.add_argument("--customers")
    ap.add_argument("--items")
    ap.add_argument("--data", help="folder with customers/current_items/garments/wearers.csv (replaces --customers/--items)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--replay")
    mode.add_argument("--eval")
    args = ap.parse_args()

    alliant = Alliant.from_dir(args.data) if args.data else Alliant.load(args.customers, args.items)
    messages = load_messages(args.messages)
    labels_path = args.replay or args.eval
    labels = {}
    if labels_path:
        with open(labels_path) as f:
            labels = {k: ParsedMessage.model_validate(v) for k, v in json.load(f).items()}

    client = None
    if not args.replay:
        import anthropic

        from .parser import parse_message
        import os
        # The Service Desk environment stores the key as SERVICE_DESK_API_KEY.
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SERVICE_DESK_API_KEY"))

    passed = 0
    for m in messages:
        if args.replay:
            parsed = labels[m["id"]]
        else:
            parsed = parse_message(client, m["text"], m["channel"], m["ts"], alliant)
            fix_department(parsed, alliant, route_from_channel(m["channel"]), service_day(m["ts"]))
            fill_item_matches(client, parsed, alliant)
        print(render(m, parsed, alliant), "\n")
        if args.eval:
            issues = score(parsed, labels[m["id"]], alliant)
            passed += not issues
            print(("✔ matches label" if not issues else "✘ " + "; ".join(issues)), "\n")
    if args.eval:
        print(f"**{passed}/{len(messages)} messages match the hand labels.**")


if __name__ == "__main__":
    main()

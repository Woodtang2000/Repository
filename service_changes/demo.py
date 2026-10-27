"""Demo mode: Donna on a sales call, with made-up businesses (demo.json), so the show always works.

A number (or email address) in DEMO_FROM in /etc/service-desk.env texts "demo restaurant" (or auto, grocery, hotel...)
and from then on Donna treats it as that made-up business: lookups come from the fake account, a change gets "Consider
it done!", and a little later "All done! ... entered it for you", the same texts a real customer gets. Nothing reaches
the office channel, Alliant or Salesforce, and no real account is ever in front of Claude. "demo off" ends it.
If Claude can't be reached mid-demo, the scripted answer for the closest script line goes out instead of an error.
"""
import json
import os
import re

from .context import Alliant, Customer, Wearer

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.json")
ENTER_SECONDS = int(os.environ.get("DEMO_ENTER_SECONDS", "40"))  # how long "the office" takes to enter a change
ENTERED_BY = os.environ.get("DEMO_ENTERED_BY", "Ana")
COMMAND = re.compile(r"^\s*demo\b\s*(.*?)\s*[.!]?\s*$", re.I | re.S)


def load() -> dict:
    with open(PATH) as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def allowed(key: str, key_of) -> bool:
    """Is this number or address one of DEMO_FROM's (written any which way)?"""
    return any(key == key_of(a.strip()) for a in os.environ.get("DEMO_FROM", "").split(",") if a.strip())


def pick(word: str, businesses: dict) -> str | None:
    """"restaurant", "a bar", "tire shop", "hotel" -> the business id; None if nothing fits."""
    words = set(re.findall(r"[a-z]+", word.lower()))
    if not words:
        return None
    for bid, b in businesses.items():
        if bid in words or words & set(re.findall(r"[a-z]+", (b["label"] + " " + b["name"]).lower())) - {"or", "and", "office"}:
            return bid
    return "office" if "office" in words else None


def alliant(b: dict, changed: dict | None = None) -> Alliant:
    """The made-up business as Alliant data, with any changes "entered" so far in this demo."""
    acct = b["account"]
    items = dict(b["items"])
    for item, qty in (changed or {}).items():
        if qty <= 0:
            items.pop(item, None)
        else:
            items[item] = qty
    data = Alliant(customers=[Customer(acct, b["name"], "D", ["Tue"])], items={acct: items},
                   autocount={acct: dict(items)}, frequency={acct: {i: "7" for i in items}})
    for emp, (first, last, garments) in b.get("wearers", {}).items():
        data.wearers.setdefault(acct, []).append(Wearer(emp, first, last))
        data.garments[(acct, emp)] = dict(garments)
    data.as_of = "today"
    return data


def fallback(b: dict, text: str) -> str:
    """The scripted answer for the script line closest to what was texted."""
    said = set(re.findall(r"[a-z]+", text.lower()))
    best = max(b["script"], key=lambda s: len(said & set(re.findall(r"[a-z]+", s[0].lower()))), default=None)
    return best[1] if best else "Consider it done! I've passed it to our office, and I'll text you the moment it's entered."


def started(b: dict) -> str:
    return (f"🎬 Demo on: you're {b['contact']} at {b['name'].title()}. Text me like a customer would. "
            "Text \"demo off\" when you're done.")


def cheat_sheet(businesses: dict | None = None) -> str:
    """The script, for printing or keeping on a phone."""
    out = []
    for bid, b in (businesses or load()).items():
        out.append(f"{b['label']}: text \"demo {bid}\"  ({b['name'].title()})")
        out += [f"  {i}. {q}" for i, (q, _) in enumerate(b["script"], 1)]
    return "\n".join(out)

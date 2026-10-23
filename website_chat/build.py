import csv, json, os, re, sys, statistics
from collections import defaultdict, Counter
feed, types_path, out_json = sys.argv[1], sys.argv[2], sys.argv[3]
branch = sys.argv[4] if len(sys.argv) > 4 else ""  # "1" Snow White, "2" The Laundry Group (account-branch-dept)
rd = lambda f: list(csv.DictReader(open(os.path.join(feed, f), encoding="utf-8-sig")))
types = json.load(open(types_path))
items, garments, wearers, cust = rd("current_items.csv"), rd("garments.csv"), rd("wearers.csv"), rd("customers.csv")
if branch:
    keep = lambda a: a.split("-")[1:2] == [branch]
    items, garments, wearers = [r for r in items if keep(r["account"])], [r for r in garments if keep(r["account"])], [r for r in wearers if keep(r["account"])]
    cust_all, cust = cust, [c for c in cust if keep(c["account"])]
names = " ".join(c["name"] for c in (cust_all if branch else cust)).upper()  # never let any customer's name through
MIN = 5

def family(item):
    i = item.upper()
    if i.startswith("MAT"): return "Floor mats"
    if "BAR MOP" in i or "KITCHEN" in i or "DISH" in i or "TOWEL UTILITY" in i: return "Kitchen towels and bar mops"
    if "TOWEL SHOP" in i or "SHOP TOWEL" in i: return "Shop towels"
    if "MICROFIBER" in i: return "Microfiber cloths"
    if i.startswith("MOP") or "DUST" in i: return "Mops and dust mops"
    if i.startswith("APRON"): return "Aprons"
    if i.startswith("MEDICAL") or "SCRUB" in i or "GOWN" in i: return "Medical linens"
    if i.startswith(("TABLE", "NAPKIN")) or "NAPKIN" in i or "TABLECLOTH" in i or re.search(r"\d+X\d+ (WHITE|BLACK|IVORY)", i) or "ROUND" in i: return "Table linens and napkins"
    if i.startswith("ROUTE") and ("BAG" in i or "STAND" in i): return "Laundry bags and stands"
    if i.startswith("ROUTE") or "TOILET" in i or "FRESH" in i or "SOAP" in i or "PAPER" in i: return "Restroom and janitorial supplies"
    if "FIRST AID" in i or "CABINET" in i: return "First aid service"
    if i.startswith(("TOWEL BATH", "TOWEL HAND", "TOWEL", "WASH CLOTH", "SHEET", "PILLOW", "BLANKET")): return "Bath and bed linens"
    if i.startswith(("COAT", "SHIRT", "PANT", "COVERALL", "JACKET", "SMOCK", "LAB COAT", "VEST", "CHEF")): return "Garments (pooled)"
    return "Other"

def public_name(item):
    """Item as a prospect may see it: no customer or logo names."""
    i = item.upper()
    if "LOGO" in i:
        size = re.search(r"\d+X\d+", i)
        return "Custom logo mat" + (f" {size.group(0).lower()}" if size else "")
    words = [w for w in re.findall(r"[A-Z0-9\"'.]+", i)]
    if any(len(w) > 3 and w in names.split() and w not in {"WHITE", "BLACK", "BLUE", "GREY", "GRAY", "RED", "GOLD", "GREEN", "NAVY", "MAT", "TOWEL", "APRON", "SHOP", "KITCHEN", "BAR", "BATH", "HAND", "COAT", "SHIRT", "PANTS", "MEDICAL", "ROUTE", "STRIPE", "WET", "DUST", "MOP", "FIRST", "AID"} and not re.match(r"\d", w) for w in words):
        return None  # could carry a customer's name: leave it out
    return item.title().replace("X", "x") if False else re.sub(r"(\d)X(\d)", r"\1x\2", item.title())

per = lambda r: float(r.get("autocount") or 0)  # the autocount is what goes out each delivery; inventory is not usage
catalog = defaultdict(Counter)
use = defaultdict(lambda: defaultdict(float))  # account -> family -> per-delivery qty
for r in items:
    f = family(r["item"])
    pn = public_name(r["item"])
    if pn and f != "Other":
        catalog[f][pn] += 1
    use[r["account"]][f] += per(r)

styles = Counter()
for g in garments:
    base = re.sub(r"\s+(\d?X{0,3}[SML]|\d+X\d+|\d+ ?[RLS]?|\d+X?L)$", "", g["item"].upper()).strip()
    pn = public_name(base)
    if pn: styles[pn] += 1
active_w = defaultdict(set)
for w in wearers:
    if not (w.get("stopped_date") or "").strip():
        active_w[w["account"]].add(w["employee"])

by_type = defaultdict(list)
for c in cust:
    t = types.get(c["account"], "unknown")
    if t != "unknown": by_type[t].append(c["account"])

bench = {}
for t, accts in sorted(by_type.items()):
    if len(accts) < MIN: continue
    fams = {}
    for f in sorted({f for a in accts for f in use.get(a, {})} - {"Other", "Laundry bags and stands"}):
        q = [use[a][f] for a in accts if use.get(a, {}).get(f)]
        if len(q) >= MIN:
            fams[f] = {"share_of_customers": round(100 * len(q) / len(accts)), "median_per_delivery": round(statistics.median(q)),
                       "typical_range": [round(statistics.quantiles(q, n=4)[0]), round(statistics.quantiles(q, n=4)[2])] if len(q) >= 8 else None,
                       "customers": len(q)}
    wc = [len(active_w[a]) for a in accts if active_w.get(a)]
    uni = {"share_with_uniforms": round(100 * len(wc) / len(accts)), "median_wearers": round(statistics.median(wc))} if len(wc) >= MIN else None
    bench[t] = {"customers": len(accts), "items": fams, "uniforms": uni}

facts = {"catalog": {f: sorted(c, key=lambda k: -c[k])[:25] for f, c in sorted(catalog.items())},
         "uniform_styles": [s for s, n in styles.most_common(40) if n >= MIN],
         "benchmarks": bench, "min_group": MIN}
json.dump(facts, open(out_json, "w"), indent=1)
print(json.dumps({f: len(v) for f, v in facts["catalog"].items()}), len(facts["uniform_styles"]), "styles")
for t, b in bench.items(): print(t, b["customers"], list(b["items"])[:6], b["uniforms"])

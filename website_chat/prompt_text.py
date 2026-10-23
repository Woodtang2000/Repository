import json, sys
TYPE = {"restaurant/bar/cafe": "Restaurants, bars and cafés", "grocery/deli/meat/bakery": "Grocery, deli, meat and bakery",
        "auto repair/tire/fleet": "Auto repair, tire and fleet", "industrial/construction/oilfield": "Industrial, construction and oilfield",
        "hospital/care facility": "Hospitals and care facilities", "medical/dental/vet clinic": "Medical, dental and vet clinics",
        "hotel/lodging": "Hotels and lodging", "office/retail/other services": "Offices, retail and other services",
        "school/government/church": "Schools, government and churches", "salon/spa/fitness": "Salons, spas and fitness"}
def text(path, only_types=None, skip_fams=()):
    f = json.load(open(path))
    L = ["PRODUCTS WE CARRY (names only; mention the few that fit the visitor):"]
    for fam, items in f["catalog"].items():
        if fam not in skip_fams: L.append(f"- {fam}: " + "; ".join(items))
    if f["uniform_styles"]: L.append("- Uniform styles: " + "; ".join(f["uniform_styles"]))
    L += ["", "WHAT SIMILAR CUSTOMERS TYPICALLY USE (anonymous averages of our current customers, per delivery; share = how many of that type use it; range = middle half):"]
    for t, b in sorted(f["benchmarks"].items(), key=lambda kv: -kv[1]["customers"]):
        if only_types and t not in only_types: continue
        parts = []
        for fam, s in sorted(b["items"].items(), key=lambda kv: -kv[1]["share_of_customers"]):
            r = f", range {s['typical_range'][0]}-{s['typical_range'][1]}" if s["typical_range"] else ""
            parts.append(f"{fam} {s['share_of_customers']}% use, typically {s['median_per_delivery']}{r}")
        u = b["uniforms"]
        if u: parts.append(f"uniforms {u['share_with_uniforms']}% have them, typically {u['median_wearers']} wearers")
        if parts: L.append(f"- {TYPE.get(t, t)}: " + "; ".join(parts))
    return "\n".join(L)
S = sys.argv[1]
sw = text(f"{S}/facts/public_b1.json", only_types=[k for k in TYPE if k not in ("hospital/care facility", "hotel/lodging")], skip_fams=("Medical linens",))
tlg = text(f"{S}/facts/public_b2.json")
json.dump({"sw": sw, "tlg": tlg}, open(f"{S}/facts/prompt_both.json", "w"))
print(len(sw), len(tlg)); print(tlg)

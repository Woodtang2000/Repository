import json, re, sys
src, out_json, out_md = sys.argv[1:4]
f = json.load(open(src))
DROP = re.compile(r"\b(NOG|COG|MISC|DAMAGED|RET|SPECIAL DELIVERY|SURE CHECK|4Y)\b", re.I)
def tidy(s):
    s = re.sub(r"^Route ", "", s); s = re.sub(r"^T/C ", "Tablecloth ", s); s = s.replace("Freshner", "Freshener")
    s = re.sub(r"\bFr\b", "FR", s); s = re.sub(r"\bSs\b", "Short Sleeve", s); s = re.sub(r"\bLs\b", "Long Sleeve", s)
    s = re.sub(r"\bOz\b", "oz", s); s = re.sub(r"\bOsfa\b", "One Size", s); s = re.sub(r"\bAsep\b", "", s)
    return re.sub(r"\s+", " ", s).strip()
cat = {}
for fam, items in f["catalog"].items():
    if fam in ("Laundry bags and stands", "Garments (pooled)"): continue
    keep = list(dict.fromkeys(tidy(i) for i in items if not DROP.search(i)))
    if keep: cat[fam] = keep
cat["Laundry bags and stands"] = ["Laundry bags and stands for soiled linens (included with service)"]
styles = list(dict.fromkeys(tidy(s) for s in f["uniform_styles"] + f["catalog"].get("Garments (pooled)", []) if not DROP.search(s)))
facts = {"catalog": cat, "uniform_styles": styles, "benchmarks": f["benchmarks"], "min_group": f["min_group"],
         "rules": ["Item names and anonymous averages only. No customer names, accounts, prices, costs, contract terms or delivery days.",
                   f"An average is shown only for a group of at least {f['min_group']} customers."]}
json.dump(facts, open(out_json, "w"), indent=1)

TYPE = {"restaurant/bar/cafe": "Restaurants, bars and cafés", "grocery/deli/meat/bakery": "Grocery, deli, meat and bakery",
        "auto repair/tire/fleet": "Auto repair, tire and fleet", "industrial/construction/oilfield": "Industrial, construction and oilfield",
        "hospital/care facility": "Hospitals and care facilities", "medical/dental/vet clinic": "Medical, dental and vet clinics",
        "hotel/lodging": "Hotels and lodging", "office/retail/other services": "Offices, retail and other services",
        "school/government/church": "Schools, government and churches", "salon/spa/fitness": "Salons, spas and fitness"}
L = ["# Website chat fact sheet: for Scott's review", "",
     "This is everything the website chat would know about Snow White's products and typical usage. It comes from the",
     "nightly Alliant service data (current items, garments, wearers). **Nothing else from Alliant goes to the chat.**", "",
     "**What the chat never gets:** customer names or accounts, prices or costs, contract terms, delivery days or routes,",
     "balances, or anything from the finance folder. Logo mats are listed only as \"custom logo mat\", and any item whose",
     "name could identify a customer was left out.", "",
     f"**Averages** are per delivery, across all current customers of that type, and are shown only for groups of at least",
     f"{f['min_group']} customers. Business type was worked out from each customer's name, so a few are mis-grouped; the",
     "averages are meant as a starting point (\"restaurants your size often start around...\"), never a quote.", "",
     "Please mark anything you'd rather the chat not say, and anything we don't actually offer to new customers.", "",
     "## Products the chat can mention", ""]
for fam, items in cat.items():
    L.append(f"**{fam}:** " + "; ".join(items))
    L.append("")
L += ["**Uniform styles:** " + "; ".join(styles), "", "## Typical usage by business type (anonymous)", ""]
for t, b in sorted(f["benchmarks"].items(), key=lambda kv: -kv[1]["customers"]):
    L.append(f"### {TYPE.get(t, t)} ({b['customers']} customers)")
    if b["items"]:
        L += ["", "| Item | Share who use it | Typical per delivery | Middle half |", "|---|---:|---:|---:|"]
        for fam, s in sorted(b["items"].items(), key=lambda kv: -kv[1]["share_of_customers"]):
            rng = f"{s['typical_range'][0]}–{s['typical_range'][1]}" if s["typical_range"] else "–"
            L.append(f"| {fam} | {s['share_of_customers']}% | {s['median_per_delivery']} | {rng} |")
    u = b["uniforms"]
    L += ["", f"Uniforms: {u['share_with_uniforms']}% have uniformed employees, typically {u['median_wearers']} wearers." if u
          else "Uniforms: too few customers of this type to say.", ""]
L += ["## Example of how the chat would use it", "",
      "> *Visitor:* We're opening a 40-seat restaurant in Wasilla.",
      "> *Chat:* Congratulations! Most restaurants we serve start with bar mops, aprons and a few entrance mats. A typical",
      "> kitchen runs around 120 bar mops and 40 aprons a delivery, but Bill will size it to your menu and hours. Who should",
      "> he reach out to?", ""]
open(out_md, "w").write("\n".join(L))
print(len(cat), "families", len(styles), "styles")

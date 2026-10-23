import csv, json, os, sys, anthropic
from concurrent.futures import ThreadPoolExecutor
feed, out = sys.argv[1], sys.argv[2]
cust = [r for r in csv.DictReader(open(os.path.join(feed, "customers.csv"), encoding="utf-8-sig"))]
TYPES = ["restaurant/bar/cafe", "grocery/deli/meat/bakery", "auto repair/tire/fleet", "medical/dental/vet clinic",
         "hospital/care facility", "hotel/lodging", "industrial/construction/oilfield", "office/retail/other services",
         "salon/spa/fitness", "school/government/church", "unknown"]
c = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ["SERVICE_DESK_API_KEY"])
def run(batch):
    listing = "\n".join(f"{x['account']} | {x['name']}" for x in batch)
    msg = c.messages.create(model="claude-haiku-5-5", max_tokens=8000, messages=[{"role": "user", "content":
        f"Classify each Alaska business by its name into exactly one of these types: {TYPES}. Department accounts (e.g. 'COSTCO DELI', 'SAFEWAY MEAT') take the department's type. Use 'unknown' if the name gives no clue.\n\n{listing}\n\nReply with only a JSON object mapping account to type."}])
    t = next(b.text for b in msg.content if b.type == "text")
    return json.loads(t[t.index("{"):t.rindex("}") + 1])
batches = [cust[i:i + 120] for i in range(0, len(cust), 120)]
res = {}
with ThreadPoolExecutor(6) as ex:
    for r in ex.map(run, batches): res.update(r)
json.dump(res, open(out, "w"), indent=0)
from collections import Counter
print(len(res), "of", len(cust)); print(Counter(res.values()).most_common())

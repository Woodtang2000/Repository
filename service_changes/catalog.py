"""The quote page's catalog (website_chat/catalog.json) and its pictures.

What a visitor picks on /quote is checked against the catalog here, so a request can only name our items. Pictures come
from Salesforce: each Product (SKU) whose Product Code is the Alliant SKU has a picture, but Salesforce only shows it to
someone logged in, so the server downloads them to <data>/catalog_pics/ and serves its own copies.

  python -m service_changes.catalog --data service_changes/data --pictures     download the pictures now
"""
import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import salesforce

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "website_chat", "catalog.json")
MAX_QTY = 5000
SKU = re.compile(r"[0-9A-Z][0-9A-Z-]{1,20}")


def load() -> dict:
    with open(PATH) as f:
        return json.load(f)


def skus(site_catalog: dict) -> list[str]:
    return [s for g in site_catalog["groups"] for it in g["items"]
            for s in ([o.get("sku") for o in it.get("options", [])] or [it.get("sku")]) if s]


def public(site_catalog: dict, have_picture) -> dict:
    """The catalog as the page gets it: each item marked with whether a picture is on hand."""
    out = json.loads(json.dumps(site_catalog))
    for g in out["groups"]:
        for it in g["items"]:
            first = next((o["sku"] for o in it.get("options", []) if o.get("sku")), it.get("sku"))
            it["picture"] = first if first and have_picture(first) else None
    return out


# Pack sizes ---------------------------------------------------------------------------------------------------------
# Some items only come in packs (bar mops in 20s, napkins in 100s): Salesforce Product2.Pack_Size__c, by Product Code
# (the SKU). The quote page steps those items by the pack, like the reps' Add items screen does. They're read from
# Salesforce every few minutes (keep_packs in webchat.py) and held in memory only, so a change there shows on the page
# within PACK_EVERY seconds. Until the first read (or if Salesforce is down at start) every item steps by 1.
PACK_EVERY = 300
_PACKS: dict[str, int] = {}
# Customer-specific SKUs (Scott 11/18: "Customer specific SKU's shouldn't be on the site"): any catalog SKU whose
# Salesforce Stock Class is Custom is left off the page and refused in a request, read in the same pass as the packs.
_HIDDEN: set[str] = set()


def pack_sizes() -> dict[str, int]:
    """{sku: pack size} for the catalog's items that come in packs of more than 1, as last read from Salesforce."""
    return dict(_PACKS)


def hidden() -> set[str]:
    """The catalog SKUs Salesforce marks Stock Class = Custom (one customer's item): never shown on the quote page."""
    return set(_HIDDEN)


def read_packs() -> str:
    """Read the catalog SKUs' pack sizes and Custom stock class from Salesforce into memory. Keeps the last good
    values if it fails."""
    if not salesforce.configured():
        return "Salesforce isn't connected, so items step by 1"
    wanted = sorted({s for site in ("sw", "tlg") for s in skus(load()[site])})
    token, instance = salesforce._token()
    q = ("SELECT ProductCode, Pack_Size__c, Stock_Class__c FROM Product2 WHERE IsActive = true "
         "AND (Pack_Size__c != null OR Stock_Class__c = 'Custom') AND ProductCode IN ("
         + ",".join(salesforce._q(s) for s in wanted) + ")")
    recs = salesforce._call("GET", f"{instance}/services/data/{salesforce.API}/query?" + urllib.parse.urlencode({"q": q}),
                            token).get("records") or []
    packs, custom = {}, set()
    for r in recs:
        if r.get("ProductCode") and r.get("Stock_Class__c") == "Custom":
            custom.add(r["ProductCode"])
        try:
            n = int(float(r.get("Pack_Size__c") or 0))
        except (TypeError, ValueError):
            continue
        if n > 1 and r.get("ProductCode"):
            packs[r["ProductCode"]] = max(n, packs.get(r["ProductCode"], 0))  # two active Products, one SKU: the bigger pack
    changed = packs != _PACKS or custom != _HIDDEN
    _PACKS.clear()
    _PACKS.update(packs)
    _HIDDEN.clear()
    _HIDDEN.update(custom)
    return (f"{len(packs)} catalog items come in packs" + (f"; hidden as customer-specific: {', '.join(sorted(custom))}" if custom else "")
            + (" (changed)" if changed else ""))


def visible(site_catalog: dict, hide: set[str]) -> dict:
    """The catalog without customer-specific SKUs: a size or color with a hidden SKU is dropped, and an item with
    nothing left (or whose own SKU is hidden) is dropped, along with its place in the industries' suggestions."""
    if not hide:
        return site_catalog
    out = json.loads(json.dumps(site_catalog))
    for g in out["groups"]:
        keep = []
        for it in g["items"]:
            if it.get("options"):
                it["options"] = [o for o in it["options"] if o.get("sku") not in hide]
                if not it["options"]:
                    continue
            elif it.get("sku") in hide:
                continue
            keep.append(it)
        g["items"] = keep
    out["groups"] = [g for g in out["groups"] if g["items"]]
    ids = {it["id"] for g in out["groups"] for it in g["items"]}
    for ind in out.get("industries", []):
        if "suggest" in ind:
            ind["suggest"] = [x for x in ind["suggest"] if x in ids]
    return out


def round_to_pack(qty: int, pack: int) -> int:
    """Up to the next whole pack, never past MAX_QTY (then down to the last whole pack under it)."""
    if pack <= 1 or qty <= 0:
        return qty
    up = -(-qty // pack) * pack
    return up if up <= MAX_QTY else MAX_QTY // pack * pack


def lines(site_catalog: dict, picked: list, packs: dict | None = None) -> tuple[list[dict], str | None]:
    """The visitor's picks as order lines [{sku, name, option, qty, how_often, wearers}], or an error to show them.
    Quantities of items that come in packs are rounded up to whole packs (the page already does; this is the backstop)."""
    items = {it["id"]: (g, it) for g in site_catalog["groups"] for it in g["items"]}
    out = []
    for p in picked[:80] if isinstance(picked, list) else []:
        if not isinstance(p, dict) or p.get("id") not in items:
            return [], "One of the items isn't in our catalog. Please reload the page and try again."
        g, it = items[p["id"]]
        try:
            qty = int(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not 1 <= qty <= MAX_QTY:
            return [], f"Please enter a quantity for {it['name']}."
        option = None
        if it.get("options"):
            option = next((o for o in it["options"] if o["label"] == p.get("option")), None)
            if option is None:
                return [], f"Please pick a size or color for {it['name']}."
        how = "Weekly"  # everything is quoted weekly
        name = it["name"] + (" mat" if g["name"] == "Floor mats" and "mat" not in it["name"].lower() else "")  # "Confetti Grey mat"
        line = {"sku": (option or it).get("sku", ""), "name": name, "option": option["label"] if option else "",
                "custom": bool(it.get("custom"))}
        if g.get("garments"):
            line["wearers"] = qty
        else:
            line.update(qty=round_to_pack(qty, (packs or {}).get(line["sku"], 1)), how_often=how)
        out.append(line)
    return out, None


def line_text(line: dict) -> str:
    name = line["name"] + (f" ({line['option']})" if line.get("option") else "")
    sku = f" [{line['sku']}]" if line.get("sku") else (" [no SKU yet]" if line.get("option") else "")
    if "wearers" in line:
        return f"{name}{sku}: {line['wearers']} employee{'s' if line['wearers'] != 1 else ''}"
    return f"{name}{sku}: {line['qty']} {line['how_often'].lower()}"


# Pictures -----------------------------------------------------------------------------------------------------------

def pic_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "catalog_pics")
    os.makedirs(d, exist_ok=True)
    return d


def pic_path(data_dir: str, sku: str) -> str | None:
    """The saved picture for an SKU, or None. SKUs are checked so a request can't reach other files."""
    if not SKU.fullmatch(sku or ""):
        return None
    path = os.path.join(pic_dir(data_dir), sku + ".img")
    return path if os.path.exists(path) else None


def picture_versions(data_dir: str, site_catalog: dict) -> dict[str, int]:
    """{sku: when its picture last changed} for the SKUs that have one. The page adds it to each picture's address, so a
    photo changed in Salesforce shows on the next sync instead of waiting out browsers' one-day cache."""
    out = {}
    for sku in skus(site_catalog):
        path = pic_path(data_dir, sku)
        if path:
            out[sku] = int(os.path.getmtime(path))
    return out


def image_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def picture_api_path(url: str) -> str | None:
    """Where the REST API serves a Product's picture: an ImageServer link names a Document (015...), a Files link a
    ContentVersion (068...)."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)
    if (q.get("versionId") or [""])[0].startswith("068"):
        return f"sobjects/ContentVersion/{q['versionId'][0]}/VersionData"
    if (q.get("id") or [""])[0].startswith("015"):
        return f"sobjects/Document/{q['id'][0]}/Body"
    return None


def sync_pictures(data_dir: str) -> str:
    """Download the catalog's pictures from Salesforce (active Products whose Product Code is the SKU)."""
    if not salesforce.configured():
        return "Salesforce isn't connected, so no pictures yet"
    wanted = sorted({s for site in ("sw", "tlg") for s in skus(load()[site])})
    token, instance = salesforce._token()
    base = f"{instance}/services/data/{salesforce.API}"
    q = ("SELECT ProductCode, URL_Picture__c, LastModifiedDate FROM Product2 WHERE IsActive = true AND ProductCode IN ("
         + ",".join(salesforce._q(s) for s in wanted) + ") ORDER BY LastModifiedDate")
    recs = salesforce._call("GET", f"{base}/query?" + urllib.parse.urlencode({"q": q}), token).get("records") or []
    got, missing = 0, []
    for sku in wanted:
        rec = next((r for r in reversed(recs) if r.get("ProductCode") == sku and picture_api_path(r.get("URL_Picture__c"))), None)
        if rec is None:
            missing.append(sku)
            continue
        req = urllib.request.Request(f"{base}/{picture_api_path(rec['URL_Picture__c'])}",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(8_000_000)
        except Exception as e:
            print(time.strftime("%H:%M"), f"catalog picture {sku} failed:", e, flush=True)
            missing.append(sku)
            continue
        if not image_type(data):
            missing.append(sku)
            continue
        path = os.path.join(pic_dir(data_dir), sku + ".img")
        if os.path.exists(path):
            with open(path, "rb") as f:
                if f.read() == data:  # unchanged: keep the file (and its version) as it is
                    got += 1
                    continue
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        got += 1
    borrowed = borrow_pictures(data_dir, load(), missing)
    missing = [m for m in missing if m not in borrowed]
    return (f"{got} pictures saved" + (f"; {len(borrowed)} borrowed ({', '.join(f'{k} from {v}' for k, v in borrowed.items())})" if borrowed else "")
            + (f"; none for {', '.join(missing)}" if missing else ""))


def borrow_pictures(data_dir: str, cat: dict, missing: list[str]) -> dict[str, str]:
    """Give SKUs without a photo a close match's: the catalog's picture_stand_ins first, else another size or color on
    the same card. Returns {sku: the sku it borrowed from}."""
    stand_ins = {k: v for k, v in cat.get("picture_stand_ins", {}).items() if not k.startswith("_")}
    borrowed = {}
    for sku in missing:
        siblings = [o.get("sku") for site in ("sw", "tlg") for g in cat[site]["groups"] for it in g["items"]
                    if sku in [o.get("sku") for o in it.get("options", [])] for o in it["options"]]
        for source in [stand_ins.get(sku)] + siblings:
            src = pic_path(data_dir, source) if source and source != sku and source not in missing else None
            if src:
                with open(src, "rb") as f:
                    data = f.read()
                path = os.path.join(pic_dir(data_dir), sku + ".img")
                if not os.path.exists(path) or open(path, "rb").read() != data:
                    with open(path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(path + ".tmp", path)
                borrowed[sku] = source
                break
    return borrowed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default="service_changes/data")
    ap.add_argument("--pictures", action="store_true", help="download the pictures from Salesforce")
    args = ap.parse_args()
    if args.pictures:
        print(sync_pictures(args.data))
    else:
        for site in ("sw", "tlg"):
            print(site, len(skus(load()[site])), "SKUs")


if __name__ == "__main__":
    main()

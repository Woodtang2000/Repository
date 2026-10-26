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
            for s in ([o["sku"] for o in it.get("options", [])] or ([it["sku"]] if it.get("sku") else []))]


def public(site_catalog: dict, have_picture) -> dict:
    """The catalog as the page gets it: each item marked with whether a picture is on hand."""
    out = json.loads(json.dumps(site_catalog))
    for g in out["groups"]:
        for it in g["items"]:
            first = (it.get("options") or [{}])[0].get("sku") or it.get("sku")
            it["picture"] = first if first and have_picture(first) else None
    return out


def lines(site_catalog: dict, picked: list) -> tuple[list[dict], str | None]:
    """The visitor's picks as order lines [{sku, name, option, qty, how_often, wearers}], or an error to show them."""
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
        how = p.get("how_often") or ""
        if not g.get("garments") and how not in site_catalog["frequencies"]:
            return [], f"Please pick how often you'd like {it['name']}."
        name = it["name"] + (" mat" if g["name"] == "Floor mats" and "mat" not in it["name"].lower() else "")  # "Confetti Grey mat"
        line = {"sku": (option or it).get("sku", ""), "name": name, "option": option["label"] if option else "",
                "custom": bool(it.get("custom"))}
        if g.get("garments"):
            line["wearers"] = qty
        else:
            line.update(qty=qty, how_often=how)
        out.append(line)
    return out, None


def line_text(line: dict) -> str:
    name = line["name"] + (f" ({line['option']})" if line.get("option") else "")
    sku = f" [{line['sku']}]" if line.get("sku") else ""
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
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        got += 1
    return f"{got} pictures saved" + (f"; none for {', '.join(missing)}" if missing else "")


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

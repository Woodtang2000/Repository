"""Read-only audit of every domain in the GoDaddy account, for holding domains without using them publicly
(Scott, 2026-10-24: "I bought all these to just hold real estate not to use them publicly").

  python -m domains.audit > domains/out/audit.md
  python -m domains.audit --fix              # list the parking records it would delete, change nothing
  python -m domains.audit --fix --apply      # delete them

A held domain should show nothing: no GoDaddy parked page (it carries ads, and ads on a page at someone else's
name are what a UDRP panel reads as bad faith), no for-sale listing, auto-renew on, privacy on, locked.
Grouping comes from domains/categories.csv (A institutional, B competitors, C other Alaskan); anything not listed
there shows under "Not categorized"."""
import argparse
import csv
import os
import sys
from datetime import date, datetime

from . import godaddy

# GoDaddy parked-page / lander / forwarding addresses. 50.63.202.x is the older parking range.
PARKING_IPS = {"34.102.136.180", "184.168.131.241", "3.33.130.190", "15.197.148.33", "76.223.105.230",
               "13.248.243.5", "34.98.99.30"}
PARKING_PREFIXES = ("50.63.202.",)
# Nameservers or CNAME targets that mean a parking or marketplace (for sale) page.
PARKING_HOSTS = ("park", "afternic", "sedo", "dan.com", "bodis", "above.com", "undeveloped", "lander")
GROUPS = {"A": "Category A: High-profile / institutional matches", "B": "Category B: Direct industry competitors",
          "C": "Category C: Other Alaskan commercial assets", "": "Not categorized"}
RENEW_WARN_DAYS = 60


def parked_ip(ip: str) -> bool:
    return ip in PARKING_IPS or ip.startswith(PARKING_PREFIXES)


def parked_host(host: str) -> bool:
    return any(p in host.lower() for p in PARKING_HOSTS)


def categories(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as f:
        return {r["domain"].strip().lower(): r["category"].strip().upper() for r in csv.DictReader(f)}


def inspect(d: dict, today: date) -> dict:
    """One domain's row: settings, '@' A and 'www' CNAME records, the problems found and the records to delete."""
    name = d["domain"]
    a = godaddy.records(name, "A", "@")
    cname = godaddy.records(name, "CNAME", "www")
    ns = d.get("nameServers") or []
    expires = (d.get("expires") or "")[:10]
    flags, fix = [], []
    if a is None:
        flags.append("DNS not at GoDaddy (" + ", ".join(ns[:2]) + "): check it there")
    else:
        bad = [r for r in a if parked_ip(r["data"])]
        if bad:
            flags.append("PARKED: @ points to GoDaddy parking " + ", ".join(r["data"] for r in bad))
            fix.append(("A", "@"))
        elif a:
            flags.append("LIVE: @ points to " + ", ".join(r["data"] for r in a))
    if cname and any(parked_host(r["data"]) for r in cname):
        flags.append("PARKED: www CNAME to " + ", ".join(r["data"] for r in cname))
        fix.append(("CNAME", "www"))
    if any(parked_host(n) for n in ns):
        flags.append("FOR SALE / PARKING nameservers " + ", ".join(ns) + ": delist in the marketplace")
    if not d.get("renewAuto"):
        flags.append("auto-renew OFF")
    if expires and (date.fromisoformat(expires) - today).days <= RENEW_WARN_DAYS:
        flags.append(f"expires within {RENEW_WARN_DAYS} days")
    if not d.get("privacy"):
        flags.append("privacy OFF")
    if not d.get("locked"):
        flags.append("transfer lock OFF")
    if d.get("status") not in (None, "ACTIVE"):
        flags.append("status " + d["status"])
    return {"domain": name, "expires": expires, "renew": "on" if d.get("renewAuto") else "OFF",
            "privacy": "on" if d.get("privacy") else "OFF", "a": "n/a" if a is None else ", ".join(r["data"] for r in a) or "none",
            "www": "n/a" if cname is None else ", ".join(r["data"] for r in cname) or "none", "flags": flags, "fix": fix}


def report(rows: list[dict], cats: dict[str, str], today: date) -> str:
    out = [f"# GoDaddy domain audit, {today.isoformat()}", "",
           f"{len(rows)} domains. {sum(1 for r in rows if r['fix'])} parked; "
           f"{sum(1 for r in rows if not r['flags'])} clean (nothing showing, renewing, private, locked).", ""]
    for key, title in GROUPS.items():
        group = [r for r in rows if cats.get(r["domain"].lower(), "") == key]
        if not group:
            continue
        out += [f"## {title}", "", "| Domain | Expires | Auto-renew | Privacy | @ A | www CNAME | Flags |",
                "|---|---|---|---|---|---|---|"]
        out += [f"| {r['domain']} | {r['expires']} | {r['renew']} | {r['privacy']} | {r['a']} | {r['www']} | "
                f"{'; '.join(r['flags']) or 'ok'} |" for r in sorted(group, key=lambda r: r["domain"])]
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--categories", default=os.path.join(os.path.dirname(__file__), "categories.csv"))
    ap.add_argument("--fix", action="store_true", help="list the parking records to delete")
    ap.add_argument("--apply", action="store_true", help="with --fix: delete them")
    args = ap.parse_args(argv)
    today = datetime.now().date()
    rows = [inspect(d, today) for d in godaddy.domains()]
    if not args.fix:
        print(report(rows, categories(args.categories), today))
        return 0
    todo = [(r["domain"], t, h) for r in rows for t, h in r["fix"]]
    for name, t, h in todo:
        if args.apply:
            godaddy.delete_records(name, t, h)
        print(f"{'deleted' if args.apply else 'would delete'} {t} {h} on {name}")
    if not todo:
        print("No parking records.")
    elif not args.apply:
        print(f"\n{len(todo)} records. Nothing changed; run again with --apply to delete them.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

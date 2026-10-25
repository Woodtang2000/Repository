"""Audit of every domain in the GoDaddy account, for holding domains without using them publicly
(Scott, 2026-10-24: "I bought all these to just hold real estate not to use them publicly"; "make them all
disappear on the internet").

  python -m domains.audit > domains/out/audit.md
  python -m domains.audit --dark             # list what going dark would change, change nothing
  python -m domains.audit --dark --apply     # do it

Going dark deletes every record that makes the domain answer: A/AAAA on '@' and 'www', the 'www' CNAME (so no
site, parked page or forwarding) and MX (so no email arrives addressed to the company the name matches), and turns
the transfer lock on. Nameservers, TXT and the registration itself stay. Auto-renew and privacy are reported, not
changed: renewing costs money, and GoDaddy sells privacy for some extensions.
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
# The records a dark domain doesn't have.
LIVE = [("A", "@"), ("AAAA", "@"), ("A", "www"), ("AAAA", "www"), ("CNAME", "www"), ("MX", "@")]
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
    """One domain's row: settings, '@' A and 'www' CNAME records, the problems found, and what going dark deletes."""
    name = d["domain"]
    recs = godaddy.records(name)
    ns = d.get("nameServers") or []
    expires = (d.get("expires") or "")[:10]
    get = lambda t, h: [r["data"] for r in recs or [] if r["type"] == t and r["name"] == h]
    flags = []
    if recs is None:
        flags.append("DNS not at GoDaddy (" + ", ".join(ns[:2]) + "): check it there")
    else:
        web = get("A", "@") + get("AAAA", "@")
        if any(parked_ip(ip) for ip in web):
            flags.append("PARKED: @ points to GoDaddy parking/forwarding " + ", ".join(web))
        elif web:
            flags.append("LIVE: @ points to " + ", ".join(web))
        if any(parked_host(h) for h in get("CNAME", "www")):
            flags.append("PARKED: www CNAME to " + ", ".join(get("CNAME", "www")))
        if get("MX", "@"):
            flags.append("receives email (MX " + ", ".join(get("MX", "@")) + ")")
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
    dark = [(t, h) for t, h in LIVE if get(t, h)]
    return {"domain": name, "expires": expires, "renew": "on" if d.get("renewAuto") else "OFF",
            "privacy": "on" if d.get("privacy") else "OFF",
            "a": "n/a" if recs is None else ", ".join(get("A", "@")) or "none",
            "www": "n/a" if recs is None else ", ".join(get("CNAME", "www")) or "none", "flags": flags,
            "dark": dark, "lock": not d.get("locked")}


def report(rows: list[dict], cats: dict[str, str], today: date) -> str:
    out = [f"# GoDaddy domain audit, {today.isoformat()}", "",
           f"{len(rows)} domains. {sum(1 for r in rows if r['dark'] or r['lock'])} not dark yet; "
           f"{sum(1 for r in rows if not r['flags'])} clean (nothing showing, renewing, private, locked).", ""]
    for key, title in GROUPS.items():
        group = [r for r in rows if cats.get(r["domain"].lower(), "") == key]
        if not group:
            continue
        out += [f"## {title}", "", "| Domain | Expires | Auto-renew | Privacy | @ A | www CNAME | Flags |",
                "|---|---|---|---|---|---|---|"]
        out += [f"| {r['domain']} | {r['expires']} | {r['renew']} | {r['privacy']} | {r['a']} | {r['www']} | "
                f"{'; '.join(r['flags']) or 'ok'} |" for r in sorted(group, key=lambda r: (r["expires"], r["domain"]))]
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--categories", default=os.path.join(os.path.dirname(__file__), "categories.csv"))
    ap.add_argument("--dark", action="store_true", help="list what going dark would delete and lock")
    ap.add_argument("--apply", action="store_true", help="with --dark: do it")
    args = ap.parse_args(argv)
    today = datetime.now().date()
    rows = [inspect(d, today) for d in godaddy.domains()]
    if not args.dark:
        print(report(rows, categories(args.categories), today))
        return 0
    verb = "" if args.apply else "would "
    n = 0
    for r in rows:
        for t, h in r["dark"]:
            if args.apply:
                godaddy.delete_records(r["domain"], t, h)
            print(f"{verb}delete {t} {h} on {r['domain']}")
            n += 1
        if r["lock"]:
            if args.apply:
                godaddy.lock(r["domain"])
            print(f"{verb}lock {r['domain']}")
            n += 1
    if not n:
        print("All domains are already dark and locked.")
    elif not args.apply:
        print(f"\n{n} changes. Nothing changed; run again with --apply to make them.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

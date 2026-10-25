"""Where each domain is registered, from the public registry (RDAP): registrar, expiry, nameservers, lock. Needs no
account access, so it finds the domains outside GoDaddy (Network Solutions, Squarespace / former Google Domains)
before consolidating them at GoDaddy (Scott, 2026-10-25: "I would like them consolidated").

  python3 -m domains.lookup snowwhitelinen.com thelaundrygroup.com ...
  python3 -m domains.lookup --file domains.txt > domains/out/registrars.md     # one domain per line

Run it where the network is open (the service desk server); rdap.org forwards each name to its registry."""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

RDAP = "https://rdap.org/domain/"
# Registrar names as the registries spell them -> what to do
MOVE = {"godaddy": "stays (GoDaddy)", "network solutions": "transfer to GoDaddy", "squarespace": "transfer to GoDaddy",
        "google": "transfer to GoDaddy"}


def rdap(name: str) -> dict | None:
    req = urllib.request.Request(RDAP + name, headers={"Accept": "application/rdap+json", "User-Agent": "domains-lookup"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def summary(name: str, data: dict | None) -> dict:
    if data is None:
        return {"domain": name, "registrar": "not registered", "expires": "", "nameservers": "", "locked": "",
                "action": "available (nobody owns it)"}
    events = {e.get("eventAction"): (e.get("eventDate") or "")[:10] for e in data.get("events", [])}
    registrar = next((x[3] for e in data.get("entities", []) if "registrar" in e.get("roles", [])
                      for x in (e.get("vcardArray") or [None, []])[1] if x[0] == "fn"), "")
    status = " ".join(data.get("status", []))
    action = next((v for k, v in MOVE.items() if k in registrar.lower()), "check: other registrar")
    return {"domain": name, "registrar": registrar, "expires": events.get("expiration", ""),
            "nameservers": ", ".join(sorted(n.get("ldhName", "").lower() for n in data.get("nameservers", []))),
            "locked": "yes" if "transfer prohibited" in status else "no", "action": action}


def table(rows: list[dict]) -> str:
    out = ["| Domain | Registrar | Expires | Nameservers | Transfer lock | Action |", "|---|---|---|---|---|---|"]
    out += [f"| {r['domain']} | {r['registrar']} | {r['expires']} | {r['nameservers']} | {r['locked']} | {r['action']} |"
            for r in sorted(rows, key=lambda r: (r["action"].startswith("stays"), r["expires"] or "9", r["domain"]))]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("domains", nargs="*")
    ap.add_argument("--file", help="a file with one domain per line")
    args = ap.parse_args(argv)
    names = list(args.domains)
    if args.file:
        names += [l.strip() for l in open(args.file) if l.strip() and not l.startswith("#")]
    rows = []
    for name in dict.fromkeys(n.lower().removeprefix("www.") for n in names):
        try:
            rows.append(summary(name, rdap(name)))
        except Exception as e:
            rows.append({"domain": name, "registrar": f"lookup failed: {e}", "expires": "", "nameservers": "",
                         "locked": "", "action": "try again"})
        time.sleep(0.5)  # registries rate-limit
    print(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""GoDaddy Domains API, standard library only. Needs GODADDY_KEY and GODADDY_SECRET (a Production key from
developer.godaddy.com/keys). GoDaddy only answers the Domains API for accounts with 10+ domains or a Domain Pro /
Discount Domain Club plan; anything else gets 403 ACCESS_DENIED."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GODADDY_API", "https://api.godaddy.com")


def _call(method: str, path: str, params: dict | None = None, body=None):
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"sso-key {os.environ['GODADDY_KEY']}:{os.environ['GODADDY_SECRET']}",
                                          "Accept": "application/json", "Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:  # GoDaddy allows 60 requests a minute
                raise
            time.sleep(int(e.headers.get("Retry-After") or 15))


def domains() -> list[dict]:
    """Every domain in the account (all statuses), following the marker pagination."""
    out, marker = [], ""
    while True:
        params = {"limit": 1000, "includes": ["contacts", "nameServers"]}
        if marker:
            params["marker"] = marker
        page = _call("GET", "/v1/domains", params) or []
        out += page
        if len(page) < 1000:
            return out
        marker = page[-1]["domain"]


def domain(name: str) -> dict:
    return _call("GET", f"/v1/domains/{name}")


def records(name: str) -> list[dict] | None:
    """Every DNS record on the domain, or None when GoDaddy doesn't host the zone (outside nameservers)."""
    try:
        return _call("GET", f"/v1/domains/{name}/records") or []
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            return None
        raise


def delete_records(name: str, rtype: str, host: str) -> None:
    _call("DELETE", f"/v1/domains/{name}/records/{rtype}/{host}")


def lock(name: str) -> None:
    _call("PATCH", f"/v1/domains/{name}", body={"locked": True})

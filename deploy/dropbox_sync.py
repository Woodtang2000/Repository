"""Keep the desk's customer data in step with the Record Cards PDF in Dropbox.

  python3 deploy/dropbox_sync.py setup   # once: connect the Dropbox app, install the hourly check
  python3 deploy/dropbox_sync.py         # the hourly check (cron runs this)

The Dropbox app uses "App folder" access, so it can only see Dropbox/Apps/<app name>/ and nothing else.
Each check compares the PDF's content hash with the last one used; only a changed PDF is downloaded, rebuilt
with refresh_data.sh (which refuses an incomplete build) and loaded by restarting the desk.
Standard library only, so it runs with the system python3.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_DIR = os.path.expanduser("~/.config/service-desk")
CONF = os.path.join(CONF_DIR, "dropbox.json")
PDF_NAME = "Alliant Customer Record Cards.pdf"
CRON = f"17 * * * * python3 {REPO}/deploy/dropbox_sync.py >> {os.path.expanduser('~/dropbox_sync.log')} 2>&1"


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M"), *a, flush=True)


def post(url, data=None, headers=None, body=None):
    req = urllib.request.Request(url, data=body if body is not None else
                                 (urllib.parse.urlencode(data).encode() if data else None), headers=headers or {})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), dict(r.headers)


def load():
    with open(CONF) as f:
        return json.load(f)


def save(conf):
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    fd = os.open(CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(conf, f, indent=1)


def access_token(conf):
    raw, _ = post("https://api.dropboxapi.com/oauth2/token",
                  {"grant_type": "refresh_token", "refresh_token": conf["refresh_token"],
                   "client_id": conf["app_key"], "client_secret": conf["app_secret"]})
    return json.loads(raw)["access_token"]


def setup():
    print("Dropbox app details (from dropbox.com/developers/apps -> your app -> Settings).")
    # The browser terminal's paste can slip in invisible characters; keys, secrets and codes are plain ASCII.
    clean = lambda v: re.sub(r"[^A-Za-z0-9_-]", "", v)
    key = clean(input("App key: "))
    secret = clean(input("App secret: "))
    print(f"(key {len(key)} characters, secret {len(secret)} characters; Dropbox's are usually 15)")
    print("\nOpen this link, click Allow, and copy the code Dropbox shows:\n")
    print("https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(
        {"client_id": key, "response_type": "code", "token_access_type": "offline"}))
    code = clean(input("\nCode: "))
    try:
        raw, _ = post("https://api.dropboxapi.com/oauth2/token",
                      {"code": code, "grant_type": "authorization_code", "client_id": key, "client_secret": secret})
    except urllib.error.HTTPError as e:
        # invalid_grant: the code was already used or has expired (they last a few minutes, once).
        # invalid_client: the app key or secret is wrong.
        sys.exit(f"Dropbox said: {e.read().decode(errors='replace')}\nRun setup again and use a fresh code.")
    tok = json.loads(raw)
    if "refresh_token" not in tok:
        sys.exit(f"Dropbox didn't return a refresh token: {tok}")
    conf = {"app_key": key, "app_secret": secret, "refresh_token": tok["refresh_token"], "path": "/" + PDF_NAME,
            "last_hash": ""}
    save(conf)
    print(f"\nConnected. Saved to {CONF} (only you can read it).")

    crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = [ln for ln in crontab.splitlines() if "dropbox_sync.py" not in ln] + [CRON]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    print("Hourly check installed (17 minutes past each hour). Running the first check now...\n")
    sync()


FEED_FILES = ["customers.csv", "customer_cards.csv", "current_items.csv", "garments.csv", "wearers.csv", "holds.csv"]
FEED_REQUIRED = {"customers.csv": {"account", "name", "route", "service_days"},
                 "customer_cards.csv": {"account", "name"},
                 "current_items.csv": {"account", "item", "quantity", "autocount", "sku"},
                 "garments.csv": {"account", "employee", "item", "size", "quantity"},
                 "wearers.csv": {"account", "employee", "first", "last"}}
STALE_HOURS = 36
DATA = os.path.join(REPO, "service_changes", "data")


def _meta(auth, path):
    try:
        raw, _ = post("https://api.dropboxapi.com/2/files/get_metadata",
                      headers={**auth, "Content-Type": "application/json"}, body=json.dumps({"path": path}).encode())
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return None
        raise


def _download(auth, path) -> bytes:
    data, _ = post("https://content.dropboxapi.com/2/files/download",
                   headers={**auth, "Dropbox-API-Arg": json.dumps({"path": path}),
                            "Content-Type": "application/octet-stream"}, body=b"")
    return data


def _restart():
    for svc in ("service-desk", "service-desk-sms"):
        subprocess.run(["sudo", "-n", "systemctl", "try-restart", svc], check=False,
                       stderr=subprocess.DEVNULL)  # texting isn't installed until Twilio is set up


def _rows(path):
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        return set(r.fieldnames or []), sum(1 for _ in r)


def sync_feed(conf, auth, done_meta) -> bool:
    """The nightly Alliant SQL feed (customers.csv … holds.csv, feed_done.txt written last). Returns True when the
    desk's data is now from the feed (installed now or already current)."""
    if done_meta["content_hash"] == conf.get("feed_hash"):
        return True
    log(f"New Alliant feed in Dropbox (feed_done.txt {done_meta.get('server_modified')}), downloading")
    tmp = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp, "feed_done.txt"), "wb") as f:
            f.write(_download(auth, "/feed_done.txt"))
        for name in FEED_FILES:
            if name == "holds.csv" and _meta(auth, "/holds.csv") is None:
                continue
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(_download(auth, "/" + name))
        # Check before replacing anything: every file there with its columns, and not far fewer customers than now.
        for name, need in FEED_REQUIRED.items():
            cols, n = _rows(os.path.join(tmp, name))
            if not need <= cols or n == 0:
                log(f"Feed {name} is missing columns {sorted(need - cols)} or empty; keeping the current data.")
                return False
        new_n = _rows(os.path.join(tmp, "customers.csv"))[1]
        old = os.path.join(DATA, "customers.csv")
        old_n = _rows(old)[1] if os.path.exists(old) else 0
        if new_n < old_n * 0.9:
            log(f"Feed has {new_n} customers against {old_n} now; looks incomplete, keeping the current data.")
            return False
        os.makedirs(DATA, exist_ok=True)
        for name in os.listdir(tmp):
            shutil.copy(os.path.join(tmp, name), os.path.join(DATA, name + ".new"))
            os.replace(os.path.join(DATA, name + ".new"), os.path.join(DATA, name))
        # Built from the PDF only; it would override the feed's autocounts.
        for stale in ("card_lines.csv",) + (() if "holds.csv" in os.listdir(tmp) else ("holds.csv",)):
            if os.path.exists(os.path.join(DATA, stale)):
                os.remove(os.path.join(DATA, stale))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    conf["feed_hash"] = done_meta["content_hash"]
    save(conf)
    _restart()
    log(f"Alliant feed installed ({new_n} customers) and desk restarted.")
    return True


def feed_age_hours(done_meta) -> float:
    t = datetime.strptime(done_meta["server_modified"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def sync():
    conf = load()
    token = access_token(conf)
    auth = {"Authorization": f"Bearer {token}"}
    done = _meta(auth, "/feed_done.txt")
    if done and sync_feed(conf, auth, done) and feed_age_hours(done) <= STALE_HOURS:
        return  # the feed is current: the PDF is only a fallback
    if done:
        log(f"Alliant feed is {feed_age_hours(done):.0f} hours old; checking the PDF fallback.")
    sync_pdf(conf, auth)


def sync_pdf(conf, auth):
    """Fallback: the Customer Record Cards PDF, used only when the feed is missing or stale."""
    meta = _meta(auth, conf["path"])
    if meta is None:
        log(f"No '{conf['path'][1:]}' in the app's Dropbox folder.")
        return
    if meta["content_hash"] == conf.get("last_hash"):
        return  # unchanged: stay quiet
    log(f"New PDF in Dropbox (modified {meta.get('server_modified')}), downloading")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(_download(auth, conf["path"]))
        pdf = f.name
    try:
        r = subprocess.run(["bash", os.path.join(REPO, "deploy", "refresh_data.sh"), pdf],
                           capture_output=True, text=True)
        print(r.stdout + r.stderr, end="", flush=True)
        if r.returncode != 0:
            log("Rebuild refused or failed; keeping the current data. Will try again next hour.")
            return
    finally:
        os.unlink(pdf)
    for feed_only in ("feed_done.txt", "holds.csv"):  # the data is the PDF's now, dated by its files
        if os.path.exists(os.path.join(DATA, feed_only)):
            os.remove(os.path.join(DATA, feed_only))
    conf["last_hash"] = meta["content_hash"]
    save(conf)
    _restart()
    log("Data updated from the PDF and desk restarted.")


if __name__ == "__main__":
    setup() if sys.argv[1:] == ["setup"] else sync()

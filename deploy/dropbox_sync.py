"""Keep the desk's customer data in step with the Record Cards PDF in Dropbox.

  python3 deploy/dropbox_sync.py setup   # once: connect the Dropbox app, install the hourly check
  python3 deploy/dropbox_sync.py         # the hourly check (cron runs this)

The Dropbox app uses "App folder" access, so it can only see Dropbox/Apps/<app name>/ and nothing else.
Each check compares the PDF's content hash with the last one used; only a changed PDF is downloaded, rebuilt
with refresh_data.sh (which refuses an incomplete build) and loaded by restarting the desk.
Standard library only, so it runs with the system python3.

LOCAL COPY (agent-server, 11/4): the Alliant Feed's hourly pull writes the same app folder on this server, under the
alliantfeed account's Dropbox (DESK_LOCAL_FEED, default below). When `donna` can read feed_done.txt there, the feed
files, money files and Alliant University notes are copied from it directly, with no Dropbox round trip; anything that
can't be read there comes from Dropbox as before. Only ever reads that folder; never writes to it.
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
# Garment tracking (barcode status, custom-scan text, open customer POs): used once the Mac export writes them.
OPTIONAL = {"holds.csv", "garments_in_service.csv", "purchase_orders.csv", "garment_scans.csv", "open_work_orders.csv",
            "skus.csv",  # SKU details (weights in ounces), no prices
            "garments_retired.csv",  # garments out of service in the last 2 years (barcode lookups)
            "routes.csv", "route_stops.csv"}  # route names, and every stop per day in driving order
# Money files, for Scott and Bill only (desk.private_answer): ar, open invoices, pricing, account details. Kept apart in
# data/private/ (owner-only permissions), never in the feed files staff answers come from.
PRIVATE_DIR = "/private - Scott and Bill only"
PRIVATE_FILES = ("ar.csv", "open_invoices.csv", "pricing.csv", "account_details.csv")
FEED_REQUIRED = {"customers.csv": {"account", "name", "route", "service_days"},
                 "customer_cards.csv": {"account", "name"},
                 "current_items.csv": {"account", "item", "quantity", "autocount", "sku"},
                 "garments.csv": {"account", "employee", "item", "size", "quantity"},
                 "wearers.csv": {"account", "employee", "first", "last"}}
STALE_HOURS = 36
DATA = os.path.join(REPO, "service_changes", "data")
LOCAL_FEED = os.environ.get("DESK_LOCAL_FEED", "/home/alliantfeed/Dropbox/Scott Woodland/Apps/SWL Service Desk")


class DropboxSource:
    """The app folder through the Dropbox API."""
    name = "Dropbox"

    def __init__(self, auth):
        self.auth = auth

    def version(self, path):  # None when it isn't there
        m = _meta(self.auth, path)
        return m["content_hash"] if m else None

    def modified(self, path):
        m = _meta(self.auth, path)
        return datetime.strptime(m["server_modified"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if m else None

    def read(self, path) -> bytes:
        return _download(self.auth, path)

    def notes(self, folder):  # [(path relative to folder, version)] of the .md files
        if _meta(self.auth, folder) is None:
            return None
        entries, body, url = [], {"path": folder, "recursive": True}, "https://api.dropboxapi.com/2/files/list_folder"
        while True:
            raw, _ = post(url, headers={**self.auth, "Content-Type": "application/json"}, body=json.dumps(body).encode())
            r = json.loads(raw)
            entries += [e for e in r["entries"] if e[".tag"] == "file" and e["name"].endswith(".md")]
            if not r.get("has_more"):
                break
            url, body = url + "/continue", {"cursor": r["cursor"]}
        return [(e["path_display"][len(folder) + 1:], e["content_hash"]) for e in entries]


class LocalSource:
    """The same app folder, as the Alliant Feed writes it on this server (read only). A file's version is its size and
    modification time, so nothing is read until it has changed."""
    name = "local copy"

    def __init__(self, root):
        self.root = root

    def _p(self, path):
        rel = path.lstrip("/")
        if ".." in rel.split("/"):
            raise ValueError("path outside the folder")
        return os.path.join(self.root, rel)

    def version(self, path):
        try:
            st = os.stat(self._p(path))
        except OSError:
            return None
        return f"local:{st.st_size}:{st.st_mtime_ns}" if os.access(self._p(path), os.R_OK) else None

    def modified(self, path):
        try:
            return datetime.fromtimestamp(os.path.getmtime(self._p(path)), timezone.utc)
        except OSError:
            return None

    def read(self, path) -> bytes:
        with open(self._p(path), "rb") as f:
            return f.read()

    def notes(self, folder):
        top = self._p(folder)
        if not os.path.isdir(top) or not os.access(top, os.R_OK | os.X_OK):
            return None
        out = []
        for d, _, files in os.walk(top):
            for n in files:
                if n.endswith(".md"):
                    full = os.path.join(d, n)
                    rel = os.path.relpath(full, top).replace(os.sep, "/")
                    v = self.version(folder + "/" + rel)
                    if v:
                        out.append((rel, v))
        return out


def local_source():
    """The local copy when donna can read it (feed_done.txt readable), else None."""
    src = LocalSource(LOCAL_FEED)
    return src if src.version("/feed_done.txt") else None


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
    for svc in ("service-desk", "service-desk-sms", "service-desk-email"):
        subprocess.run(["sudo", "-n", "systemctl", "try-restart", svc], check=False,
                       stderr=subprocess.DEVNULL)  # texting isn't installed until Twilio is set up


def _rows(path):
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        return set(r.fieldnames or []), sum(1 for _ in r)


def sync_feed(conf, src, version) -> bool:
    """The Alliant SQL feed (customers.csv … holds.csv, feed_done.txt written last), from `src` (the local copy or
    Dropbox). Returns True when the desk's data is now from the feed (installed now or already current)."""
    key = "feed_local" if isinstance(src, LocalSource) else "feed_hash"
    if version == conf.get(key):
        return True
    log(f"New Alliant feed in the {src.name} (feed_done.txt {src.modified('/feed_done.txt'):%Y-%m-%d %H:%M}Z), copying")
    tmp = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp, "feed_done.txt"), "wb") as f:
            f.write(src.read("/feed_done.txt"))
        for name in FEED_FILES + sorted(OPTIONAL - set(FEED_FILES)):
            if name in OPTIONAL and src.version("/" + name) is None:
                continue
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(src.read("/" + name))
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
    conf[key] = version
    save(conf)
    # The feed refreshes hourly now: the desk, texting and email reload when feed_done.txt changes, no restart needed.
    log(f"Alliant feed installed ({new_n} customers); Donna reloads it by herself.")
    return True


def feed_age_hours(src) -> float:
    t = src.modified("/feed_done.txt")
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600 if t else 1e9


NOTES_DIR = "/alliant_university"  # the Alliant University notes, for #alliant-help (service_changes/alliant_help.py)


def sync_notes(conf, src) -> None:
    """Mirror <app folder>/alliant_university/ into data/alliant_university/, copying only changed files. The desk
    reads the folder on each question, so no restart is needed."""
    entries = src.notes(NOTES_DIR)
    if entries is None:
        return
    old, new, local = conf.get("notes_hashes", {}), {}, os.path.join(DATA, "alliant_university")
    changed = 0
    for rel, version in entries:
        if ".." in rel.split("/"):
            continue
        new[rel] = version
        dest = os.path.join(local, rel)
        if old.get(rel) == version and os.path.exists(dest):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".new", "wb") as f:
            f.write(src.read(f"{NOTES_DIR}/{rel}"))
        os.replace(dest + ".new", dest)
        changed += 1
    for rel in set(old) - set(new):
        try:
            os.remove(os.path.join(local, rel))
        except OSError:
            pass
    if changed or set(old) != set(new):
        conf["notes_hashes"] = new
        save(conf)
        log(f"Alliant University notes: {changed} file(s) updated, {len(new)} in all.")


def sync_private(conf, src) -> None:
    """Mirror the Scott-and-Bill-only money files into data/private/ (folder 700, files 600), only when changed. The
    desk reads them fresh on each private question, so no restart is needed."""
    local = os.path.join(DATA, "private")
    os.makedirs(local, mode=0o700, exist_ok=True)
    os.chmod(local, 0o700)
    old, new, changed = conf.get("private_hashes", {}), {}, 0
    for name in PRIVATE_FILES:
        version = src.version(f"{PRIVATE_DIR}/{name}")
        if version is None:
            continue
        new[name] = version
        dest = os.path.join(local, name)
        if old.get(name) == version and os.path.exists(dest):
            continue
        fd = os.open(dest + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(src.read(f"{PRIVATE_DIR}/{name}"))
        os.replace(dest + ".new", dest)
        changed += 1
    if changed:
        conf["private_hashes"] = new
        save(conf)
        log(f"Private Scott-and-Bill files: {changed} updated ({', '.join(sorted(new))}).")


def sync():
    conf = load()
    local = local_source()
    _dbx = []

    def dropbox():  # only signs in to Dropbox when something has to come from there
        if not _dbx:
            _dbx.append(DropboxSource({"Authorization": f"Bearer {access_token(conf)}"}))
        return _dbx[0]

    for what, step in (("Alliant University notes", sync_notes), ("Private Scott-and-Bill files", sync_private)):
        try:
            step(conf, local) if local else step(conf, dropbox())
        except Exception as e:  # never let these get in the way of the customer data
            if local:
                log(f"{what}: local copy not readable ({type(e).__name__}); using Dropbox.")
                try:
                    step(conf, dropbox())
                except Exception as e2:
                    log(f"{what} not synced: {e2}")
            else:
                log(f"{what} not synced: {e}")
    src = local
    version = local.version("/feed_done.txt") if local else None
    if local:
        try:
            if sync_feed(conf, local, version) and feed_age_hours(local) <= STALE_HOURS:
                return  # the feed is current: the PDF is only a fallback
        except OSError as e:  # a file the feed wrote that donna can't read yet: this round from Dropbox
            log(f"Alliant feed: local copy not readable ({type(e).__name__}: {e.filename}); using Dropbox.")
        src = None
    if src is None:
        src = dropbox()
        version = src.version("/feed_done.txt")
        if version and sync_feed(conf, src, version) and feed_age_hours(src) <= STALE_HOURS:
            return
    if version:
        log(f"Alliant feed is {feed_age_hours(src):.0f} hours old; checking the PDF fallback.")
    sync_pdf(conf, dropbox().auth)


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

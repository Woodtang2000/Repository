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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

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
    key = input("App key: ").strip()
    secret = input("App secret: ").strip()
    print("\nOpen this link, click Allow, and copy the code Dropbox shows:\n")
    print("https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(
        {"client_id": key, "response_type": "code", "token_access_type": "offline"}))
    code = input("\nCode: ").strip()
    raw, _ = post("https://api.dropboxapi.com/oauth2/token",
                  {"code": code, "grant_type": "authorization_code", "client_id": key, "client_secret": secret})
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


def sync():
    conf = load()
    token = access_token(conf)
    auth = {"Authorization": f"Bearer {token}"}
    try:
        raw, _ = post("https://api.dropboxapi.com/2/files/get_metadata",
                      headers={**auth, "Content-Type": "application/json"},
                      body=json.dumps({"path": conf["path"]}).encode())
    except urllib.error.HTTPError as e:
        if e.code == 409:
            log(f"No '{conf['path'][1:]}' in the app's Dropbox folder yet.")
            return
        raise
    meta = json.loads(raw)
    if meta["content_hash"] == conf.get("last_hash"):
        return  # unchanged: stay quiet
    log(f"New PDF in Dropbox (modified {meta.get('server_modified')}), downloading")
    data, _ = post("https://content.dropboxapi.com/2/files/download",
                   headers={**auth, "Dropbox-API-Arg": json.dumps({"path": conf["path"]}),
                            "Content-Type": "application/octet-stream"}, body=b"")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
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
    conf["last_hash"] = meta["content_hash"]
    save(conf)
    subprocess.run(["sudo", "-n", "systemctl", "restart", "service-desk"], check=False)
    log("Data updated and desk restarted.")


if __name__ == "__main__":
    setup() if sys.argv[1:] == ["setup"] else sync()

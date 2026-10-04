#!/usr/bin/env bash
# Pull the latest code and restart the desk.   bash deploy/update.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
git pull --ff-only
"$REPO/.venv/bin/pip" install -q --upgrade anthropic pydantic slack_sdk
sudo systemctl restart service-desk
sleep 5
journalctl -u service-desk -n 5 --no-pager

#!/usr/bin/env bash
# Pull the latest code and restart Donna (the Service Desk).
#   On agent-server (since 11/4/2026, code owned by the donna account):  sudo bash /home/donna/Repository/deploy/update.sh
#   Anywhere else Donna is the enabled service:                         bash deploy/update.sh
# Refuses to run where Donna's services are disabled (the old Lightsail server), so there are never two Donnas.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO")"
as_owner() { if [ "$(id -un)" = "$OWNER" ]; then "$@"; else sudo -u "$OWNER" "$@"; fi; }

if [ "$(systemctl is-enabled service-desk 2>/dev/null || true)" != "enabled" ]; then
  echo "Donna isn't enabled on this server (she runs on agent-server now). Not pulling or starting anything here."
  exit 1
fi
as_owner git -C "$REPO" pull --ff-only
as_owner "$REPO/.venv/bin/pip" install -q --upgrade anthropic pydantic slack_sdk reportlab
sudo systemctl daemon-reload   # picks up any change to the service files
sudo systemctl restart service-desk
for s in service-desk-sms service-desk-email service-desk-webchat; do   # texting, email and website chat
  if [ "$(systemctl is-enabled "$s" 2>/dev/null || true)" = "enabled" ]; then sudo systemctl restart "$s"; fi
done
sleep 5
journalctl -u 'service-desk*' --since '-2 min' --no-pager | tail -20

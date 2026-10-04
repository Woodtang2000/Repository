#!/usr/bin/env bash
# One-time setup of the Service Desk on a fresh Ubuntu server (Amazon Lightsail, Ubuntu 24.04).
# Run from inside the cloned repository:   sudo bash deploy/setup.sh
# Safe to run again; it never overwrites the token file.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-ubuntu}"
ENV_FILE=/etc/service-desk.env

echo "== Installing Python and the PDF text tool"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip poppler-utils git

echo "== Python packages"
sudo -u "$RUN_AS" python3 -m venv "$REPO/.venv"
sudo -u "$RUN_AS" "$REPO/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_AS" "$REPO/.venv/bin/pip" install -q anthropic pydantic slack_sdk
sudo -u "$RUN_AS" mkdir -p "$REPO/service_changes/data"

if [ ! -f "$ENV_FILE" ]; then
  echo "== Creating $ENV_FILE (fill in the tokens next)"
  cat > "$ENV_FILE" <<'EOF'
# Service Desk settings. Only root can read this file.
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SERVICE_DESK_API_KEY=sk-ant-...

# Channels. Test channels until go-live.
DESK_CHANNEL=service-desk-test
ROUTE_CHANNELS=route-12-test

# Go-live: set to 1 and list the real channels above, e.g. ROUTE_CHANNELS=route-1,route-2,route-12
SERVICE_DESK_LIVE=0
EOF
  chmod 600 "$ENV_FILE"
fi

echo "== Installing the service"
cat > /etc/systemd/system/service-desk.service <<EOF
[Unit]
Description=Service Desk (Slack route channels -> office tickets)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$REPO/.venv/bin/python -m service_changes.desk --data service_changes/data --desk \${DESK_CHANNEL} --routes \${ROUTE_CHANNELS} --listen
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q service-desk

if grep -q '\.\.\.$' "$ENV_FILE"; then
  echo
  echo "Next: put the tokens in with   sudo nano $ENV_FILE"
fi
if [ ! -f "$REPO/service_changes/data/customers.csv" ]; then
  echo "Next: load the data with       bash deploy/refresh_data.sh <Customer Record Cards.pdf>"
fi
echo "Then start it with              sudo systemctl restart service-desk"
echo "Watch it with                   journalctl -u service-desk -f"

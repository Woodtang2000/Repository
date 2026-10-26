#!/usr/bin/env bash
# Website chat (service_changes/webchat.py) behind Caddy for HTTPS, next to the desk.
#   sudo bash deploy/webchat_setup.sh            install and start (Caddy gets the certificate on first visit)
#   sudo bash deploy/webchat_setup.sh off        stop it
# Before running: in Lightsail, attach a static IP to the instance and open ports 80 and 443 (Networking tab), and
# point chat.snowwhitelinen.com (an A record) at that static IP. Leads need a #sales-desk Slack channel with the bot
# invited, and Interactivity turned on in the Slack app (for the "Give to" buttons). Salesforce Accounts and
# Opportunities need SF_DOMAIN, SF_CLIENT_ID and SF_CLIENT_SECRET in /etc/service-desk.env (an external client app
# with the client credentials flow).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-ubuntu}"
ENV_FILE=/etc/service-desk.env
HOST="${WEBCHAT_HOST:-chat.snowwhitelinen.com}"

if [ "${1:-}" = "off" ]; then
  systemctl disable --now service-desk-webchat
  echo "Website chat stopped (Caddy left running)."
  exit 0
fi

grep -q "^WEBCHAT_PUBLIC_URL=" "$ENV_FILE" || echo "WEBCHAT_PUBLIC_URL=https://$HOST" >> "$ENV_FILE"
grep -q "^SALES_CHANNEL=" "$ENV_FILE" || echo "SALES_CHANNEL=sales-desk" >> "$ENV_FILE"
grep -q "^TEXT_LINE=" "$ENV_FILE" || echo "TEXT_LINE=" >> "$ENV_FILE"   # fill in once the customer text line is approved
for v in SF_DOMAIN SF_CLIENT_ID SF_CLIENT_SECRET; do grep -q "^$v=" "$ENV_FILE" || echo "$v=" >> "$ENV_FILE"; done

cat > /etc/systemd/system/service-desk-webchat.service <<EOF2
[Unit]
Description=Website chat for Snow White Linen Supply and The Laundry Group
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$REPO/.venv/bin/python -m service_changes.webchat --data service_changes/data --port 8088
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF2

if ! command -v caddy >/dev/null; then
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
cat > /etc/caddy/Caddyfile <<EOF3
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8088
}
EOF3

systemctl daemon-reload
systemctl enable --now service-desk-webchat
systemctl restart service-desk-webchat
systemctl reload caddy || systemctl restart caddy
systemctl restart service-desk   # so the desk handles the "Give to" buttons
sleep 4
journalctl -u service-desk-webchat -n 3 --no-pager
echo
echo "Test page: https://$HOST/try   (switch sites with the link on the page)"
echo "Snow White site:  <script src=\"https://$HOST/widget.js?site=sw\" async></script>"
echo "TLG site:         <script src=\"https://$HOST/widget.js?site=tlg\" async></script>"

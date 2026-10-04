#!/usr/bin/env bash
# Customer texting (service_changes/sms.py) as a service next to the desk.
#   sudo bash deploy/sms_setup.sh        install and start
#   sudo bash deploy/sms_setup.sh off    stop it
# Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_NUMBER in /etc/service-desk.env, and the
# phone -> account list in service_changes/data/customer_phones.csv.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-ubuntu}"
ENV_FILE=/etc/service-desk.env

if [ "${1:-}" = "off" ]; then
  systemctl disable --now service-desk-sms
  echo "Customer texting stopped."
  exit 0
fi

for v in TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_NUMBER; do
  grep -q "^$v=" "$ENV_FILE" || echo "$v=" >> "$ENV_FILE"
done
PHONES="$REPO/service_changes/data/customer_phones.csv"
[ -f "$PHONES" ] || sudo -u "$RUN_AS" bash -c "printf 'phone,accounts,name\n' > '$PHONES'"

cat > /etc/systemd/system/service-desk-sms.service <<EOF2
[Unit]
Description=Service Desk customer texting (Twilio -> office tickets)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$REPO/.venv/bin/python -m service_changes.sms --data service_changes/data --desk \${DESK_CHANNEL}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF2
systemctl daemon-reload

if grep -qE '^TWILIO_(ACCOUNT_SID|AUTH_TOKEN|NUMBER)=$' "$ENV_FILE"; then
  echo "Next: fill in TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_NUMBER (e.g. +18885551234) with"
  echo "      sudo nano $ENV_FILE    then run this script again."
  exit 0
fi
systemctl enable --now service-desk-sms
systemctl restart service-desk   # so the desk can text readbacks too
sleep 5
journalctl -u service-desk-sms -n 3 --no-pager
echo "Customer phones go in $PHONES (phone,accounts,name)."

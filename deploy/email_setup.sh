#!/usr/bin/env bash
# Customer email (service_changes/email_desk.py) as a service next to the desk.
#   sudo bash deploy/email_setup.sh        install and start
#   sudo bash deploy/email_setup.sh off    stop it
# Needs BIRD_API_KEY in /etc/service-desk.env (Bird dashboard -> API keys, scopes emails + email_management).
# The customer mailbox (EMAIL_REPLY_TO) must forward to the Bird inbound address, and mail.snowwhitelinen.com must be
# verified in Bird for the replies. Approved senders live in service_changes/data/customer_emails.csv.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-ubuntu}"
ENV_FILE=/etc/service-desk.env

if [ "${1:-}" = "off" ]; then
  systemctl disable --now service-desk-email
  echo "Customer email stopped."
  exit 0
fi

grep -q "^BIRD_API_KEY=" "$ENV_FILE" || echo "BIRD_API_KEY=" >> "$ENV_FILE"
grep -q "^BIRD_INBOUND_ADDRESS_ID=" "$ENV_FILE" || echo "BIRD_INBOUND_ADDRESS_ID=ina_01m5ktqgjmem7vbwncm5wa0wkm" >> "$ENV_FILE"
grep -q "^EMAIL_FROM=" "$ENV_FILE" || echo "EMAIL_FROM=service@mail.snowwhitelinen.com" >> "$ENV_FILE"
grep -q "^EMAIL_REPLY_TO=" "$ENV_FILE" || echo "EMAIL_REPLY_TO=service@snowwhitelinen.com" >> "$ENV_FILE"
EMAILS="$REPO/service_changes/data/customer_emails.csv"
[ -f "$EMAILS" ] || sudo -u "$RUN_AS" bash -c "printf 'email,accounts,name\n' > '$EMAILS'"

cat > /etc/systemd/system/service-desk-email.service <<EOF2
[Unit]
Description=Service Desk customer email (Bird -> office tickets)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$REPO/.venv/bin/python -m service_changes.email_desk --data service_changes/data --desk \${DESK_CHANNEL}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF2
systemctl daemon-reload

if grep -qE '^BIRD_API_KEY=$' "$ENV_FILE"; then
  echo "Next: paste the Bird API key (bk_us1_...) after BIRD_API_KEY= with"
  echo "      sudo nano $ENV_FILE    then run this script again."
  exit 0
fi
systemctl enable --now service-desk-email
systemctl restart service-desk   # so the desk can email readbacks too
sleep 5
journalctl -u service-desk-email -n 3 --no-pager
echo "Approved customer emails go in $EMAILS (email,accounts,name), or approve them from the sign-up cards."

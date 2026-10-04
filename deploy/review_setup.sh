#!/usr/bin/env bash
# Silent review: every hour, read the real #route-N channels and post what the bot would have done to
# #service-desk-review. Drivers see nothing. React 👎 on anything it got wrong.
#   sudo bash deploy/review_setup.sh          install and start
#   sudo bash deploy/review_setup.sh off      stop it (e.g. once the routes are live)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-ubuntu}"

if [ "${1:-}" = "off" ]; then
  systemctl disable --now service-desk-review.timer
  echo "Silent review stopped."
  exit 0
fi

cat > /etc/systemd/system/service-desk-review.service <<EOF
[Unit]
Description=Service Desk silent review (real route channels -> #service-desk-review)

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$REPO
EnvironmentFile=/etc/service-desk.env
ExecStart=$REPO/.venv/bin/python -m service_changes.bot --data service_changes/data --to service-desk-review --since-minutes 70
EOF

cat > /etc/systemd/system/service-desk-review.timer <<EOF
[Unit]
Description=Run the Service Desk silent review every hour

[Timer]
OnCalendar=*:05
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now service-desk-review.timer
echo "Silent review installed: runs at 5 past every hour. First run now..."
systemctl start service-desk-review.service
journalctl -u service-desk-review -n 3 --no-pager

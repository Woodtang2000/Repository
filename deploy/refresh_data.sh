#!/usr/bin/env bash
# Rebuild service_changes/data from the Alliant Customer Record Cards PDF.
#   bash deploy/refresh_data.sh "Alliant Customer Record Cards.pdf"
# The running desk picks the new data up within the hour; restart it to use it right away.
# Refuses to replace the data if the new build finds far fewer accounts than the current one.
set -euo pipefail

PDF="${1:?usage: bash deploy/refresh_data.sh <Customer Record Cards.pdf>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$REPO/service_changes/data"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pdftotext "$PDF" "$TMP/cards.txt"
cd "$REPO"
"$REPO/.venv/bin/python" -m service_changes.record_cards "$TMP/cards.txt" --bot-data --out "$TMP/data"

count() { [ -f "$1/customers.csv" ] && echo $(( $(wc -l < "$1/customers.csv") - 1 )) || echo 0; }
new=$(count "$TMP/data")
old=$(count "$DATA")
echo "accounts: $old now, $new in the new build"
if [ "$new" -eq 0 ] || [ "$new" -lt $(( old * 9 / 10 )) ]; then
  echo "Not replacing the data: the new build looks incomplete." >&2
  exit 1
fi

mkdir -p "$DATA"
cp "$TMP/data/"*.csv "$DATA/"
echo "Data updated."

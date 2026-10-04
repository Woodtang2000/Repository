#!/usr/bin/env bash
# Rebuild service_changes/data from the Alliant Customer Record Cards PDF.
#   bash deploy/refresh_data.sh "Alliant Customer Record Cards.pdf"
# The parser was written against Dropbox's text extraction. pypdf matches it exactly (866 accounts, 6175 lines
# on the Oct 2026 PDF); pdftotext -raw is kept as a fallback. Whichever the parser reads more from is used.
# The running desk picks the new data up within the hour; restart it to use it right away.
# Refuses to replace the data if the new build finds far fewer accounts than the current one.
set -euo pipefail

PDF="${1:?usage: bash deploy/refresh_data.sh <Customer Record Cards.pdf>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
DATA="$REPO/service_changes/data"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

"$PY" -c "import pypdf" 2>/dev/null || "$REPO/.venv/bin/pip" install -q pypdf

pdftotext -raw "$PDF" "$TMP/poppler-raw.txt"
"$PY" -c "import sys, pypdf; print('\n'.join(p.extract_text() or '' for p in pypdf.PdfReader(sys.argv[1]).pages))" "$PDF" > "$TMP/pypdf.txt"

cd "$REPO"
best="" best_score=0
for txt in "$TMP"/*.txt; do
  score=$("$PY" -c "
import sys
from service_changes.record_cards import parse_text
cards = parse_text(open(sys.argv[1], errors='replace').read())
print(len(cards), sum(len(c['items']) for c in cards.values()))" "$txt")
  read -r accounts lines <<< "$score"
  echo "$(basename "$txt" .txt): $accounts accounts, $lines lines"
  if [ $(( accounts * 100000 + lines )) -gt "$best_score" ]; then
    best="$txt" best_score=$(( accounts * 100000 + lines ))
  fi
done
if [ -z "$best" ]; then
  echo "None of the PDF readers gave text the parser understands." >&2
  exit 1
fi
echo "using $(basename "$best" .txt)"
"$PY" -m service_changes.record_cards "$best" --bot-data --out "$TMP/data"

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

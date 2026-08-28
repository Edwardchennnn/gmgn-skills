#!/usr/bin/env bash
# Regression net for gmgn-wallet-analysis. Zero API calls, so it can run on every change.
#   ./tests/run.sh          diff every fixture against its snapshot
#   ./tests/run.sh --bless  accept the current output as the new snapshot
set -uo pipefail
cd "$(dirname "$0")/.."
S=tests/snapshots; A=skills/gmgn-wallet-analysis/analyze.py; fail=0
for f in tests/fixtures/*.json; do
  n=$(basename "$f" .json)
  for lang in zh en; do
    out=$(python3 "$A" --fixture "$f" $lang 2>&1)
    snap="$S/$n.$lang.md"
    if [ "${1:-}" = "--bless" ]; then printf '%s\n' "$out" > "$snap"; continue; fi
    if ! diff -q <(printf '%s\n' "$out") "$snap" >/dev/null 2>&1; then
      echo "── CHANGED  $n.$lang"; diff <(printf '%s\n' "$out") "$snap" | head -20; fail=1
    fi
    # A dossier that prints no decision is the failure this net exists to catch.
    printf '%s\n' "$out" | grep -qE '^#{1,2} (🔴|🟡|🟢|⚪)' || { echo "── NO VERDICT  $n.$lang"; fail=1; }
  done
done
[ "${1:-}" = "--bless" ] && { echo "snapshots blessed"; exit 0; }
[ $fail -eq 0 ] && echo "all fixtures match" || echo "REGRESSIONS ABOVE"
exit $fail

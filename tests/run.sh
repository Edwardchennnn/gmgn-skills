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
    # A key written in Chinese falls back to itself, so the English report prints Chinese.
    # Invisible while zh was the default. BSD grep has no -P, so this is done in python.
    if [ "$lang" = en ]; then
      leak=$(printf '%s\n' "$out" | python3 -c '
import sys, re
bad = [(n, l) for n, l in enumerate(sys.stdin.read().splitlines(), 1)
       if re.search(r"[\u4e00-\u9fff]", l)]
for n, l in bad[:3]: print("  L%d %s" % (n, l[:70]))
')
      if [ -n "$leak" ]; then echo "── CHINESE IN EN  $n"; echo "$leak"; fail=1; fi
    fi
  done
done
[ "${1:-}" = "--bless" ] && { echo "snapshots blessed"; exit 0; }
[ $fail -eq 0 ] && echo "all fixtures match" || echo "REGRESSIONS ABOVE"
exit $fail

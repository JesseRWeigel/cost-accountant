#!/usr/bin/env bash
# Run from a disposable checkout at the repository root.
set -euo pipefail
export LC_ALL=C.UTF-8
test -f pricing.claude-api-2026-11-09.json
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Fail closed if a command accidentally attempts to use the usual transcript location.
export HOME="$tmp/home"
mkdir "$HOME"
export COST_ACCOUNTANT_TRANSCRIPTS="$tmp/nonexistent"
echo "Offline: no private transcripts or live API calls; not equivalent to scripts/verify.sh."
python3 -m unittest discover -s tests
python3 scripts/check_independent.py
python3 scripts/demo_pipeline.py --out "$tmp/demo"
python3 -m accountant.cli extract --stages-from "$tmp/demo/run.jsonl" \
  --root "$tmp/demo" --no-redact --out "$tmp/demo/ledger.json"
python3 - "$tmp/demo/ledger.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    ledger = json.load(f)
assert ledger["reconciled"]
assert ledger["totals"]["stages"] == 4
assert {"image", "audio", "text"} <= set(ledger["by_medium"])
assert ledger["totals"]["usd_total"] > 0
print("Local demo recorder: four stages, three media, positive cost, reconciled")
PY
cp docs/index.html "$tmp/index.html"
python3 scripts/build_docs.py
cmp "$tmp/index.html" docs/index.html
echo "Historical documentation rebuild is byte-identical."
echo "OFFLINE VERIFY OK (real-transcript extraction and full verify.sh not run)"

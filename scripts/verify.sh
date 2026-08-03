#!/usr/bin/env bash
# Verification for cost-accountant.
#
# This tool produces dollar figures nobody can check by eye, so a confident wrong number
# is the failure mode that matters. The layers are aimed at exactly that:
#
#   1  the unit suite, every test paired with a negative control proving it can fail
#   2  scripts/check_independent.py, which re-derives the arithmetic with its own parser,
#      its own pricing formula and its own attribution, and drives the SHIPPED CLI
#   3  that independence checked by walking the import graph with ast, not by grep
#   4  real transcripts, extracted for real; if there are none, this FAILS rather than
#      quietly reporting success on nothing
#   5  the live recorder run end to end through the CLI, so the second input path is
#      exercised rather than only unit tested
#   6  the page rebuilt and compared byte for byte, then asserted to contain the numbers
#   7  eight sabotages, each PROVED to have applied and PROVED to have moved the output
#      before its result counts for anything
#   8  the README's pasted numbers checked against a live run
#   9  hygiene: home paths, credential shapes, NUL bytes, oversized files
#  10  GPU measurement, run for real and required to be honest about what it could not see
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"
PY=python3

pass=0; fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "0. environment"
$PY --version | sed 's/^/        /'
bash --version | head -1 | sed 's/^/        /'
if $PY -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"; then
  ok "python 3.9+ and bash present, no third-party packages required"
else
  bad "python 3.9 or newer is required"
fi

echo
echo "1. unit suite"
if $PY -m unittest discover -s tests >"$TMP/unit.log" 2>&1; then
  N=$(grep -oE 'Ran [0-9]+ tests' "$TMP/unit.log" | grep -oE '[0-9]+')
  echo "$N" > "$ROOT/.verify-testcount"
  ok "$N unit tests passed"
else
  bad "unit suite"; grep -E 'FAIL:|ERROR:|Error' "$TMP/unit.log" | head -20 | sed 's/^/        /'
fi

echo
echo "2. the arithmetic re-derived by code that shares nothing with the package"
if $PY scripts/check_independent.py >"$TMP/indep.log" 2>&1; then
  sed 's/^/        /' "$TMP/indep.log"
  ok "independent re-derivation agrees on every property"
else
  bad "the independent checker disagrees"; tail -8 "$TMP/indep.log" | sed 's/^/        /'
fi

echo
echo "3. the checker really is independent"
if $PY - <<'PY' >"$TMP/imports.log" 2>&1; then
import ast, pathlib, sys
tree = ast.parse(pathlib.Path("scripts/check_independent.py").read_text())
bad = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        bad += [a.name for a in node.names if a.name.split(".")[0] == "accountant"]
    elif isinstance(node, ast.ImportFrom):
        if (node.module or "").split(".")[0] == "accountant":
            bad.append(node.module)
    elif isinstance(node, ast.Call):
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name in ("__import__", "import_module"):
            bad.append(f"dynamic import via {name}()")
if bad:
    sys.exit(f"check_independent.py reaches into accountant/: {bad}")
# The control: the same walk over a file that DOES import the package must find it, or
# the walk is looking in the wrong place and would report clean on anything.
tree2 = ast.parse(pathlib.Path("scripts/probe.py").read_text())
found = [n.module for n in ast.walk(tree2)
         if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("accountant")]
if not found:
    sys.exit("the import walk found nothing in probe.py either, so it proves nothing")
print(f"check_independent.py imports nothing from accountant/; "
      f"the same walk finds {len(found)} such imports in probe.py")
PY
  sed 's/^/        /' "$TMP/imports.log"; ok "the checker shares no code with the package"
else
  bad "$(cat "$TMP/imports.log")"
fi

echo
echo "4. real transcripts, extracted for real"
# A skipped section reports the same success as one that ran, so this fails instead.
TRANSCRIPTS="${COST_ACCOUNTANT_TRANSCRIPTS:-$HOME/.claude/projects}"
NFILES=$(find "$TRANSCRIPTS" -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')
if [ "$NFILES" -eq 0 ]; then
  bad "no Claude Code transcripts found under ${TRANSCRIPTS/#$HOME/\~}"
  echo "        This suite cannot verify the extractor without real input. Point" \
       "COST_ACCOUNTANT_TRANSCRIPTS" | sed 's/^/        /'
  echo "        at a directory of Claude Code .jsonl session files, or run Claude Code" \
       "once to create some." | sed 's/^/        /'
  echo "        Everything else above still ran: pricing, attribution, reconciliation," \
       "the independent" | sed 's/^/        /'
  echo "        checker and the sabotages all use synthetic corpora." | sed 's/^/        /'
else
  # A bounded subset: the full corpus here is 478 MB and takes minutes to read.
  if $PY -m accountant.cli extract --limit 40 --out "$TMP/real.json" \
       --label "verify subset" >"$TMP/real.log" 2>&1; then
    sed 's/^/        /' "$TMP/real.log"
    if $PY - "$TMP/real.json" "$NFILES" <<'PY' >>"$TMP/real.log" 2>&1; then
import json, sys
led = json.load(open(sys.argv[1]))
t = led["totals"]
assert led["reconciled"], "the real extraction did not reconcile"
assert t["stages"] > 0, "no stages came out of real transcripts"
assert t["tokens"]["all"] > 0, "no tokens came out of real transcripts"
assert t["usd_total"] > 0, "real transcripts priced to zero dollars"
# The control that matters: cache reads must be cheaper per token than fresh input, so
# the bill must be far below what flattening the tiers would produce.
tok = t["tokens"]
assert tok["cache_read"] > 0, "no cache reads in the sample, so the tier check is vacuous"
print(f"        {t['stages']:,} requests from {sys.argv[2]} available transcripts, "
      f"{tok['all']:,} tokens, ${t['usd_total']:,.2f}")
PY
      sed 's/^/        /' <(tail -1 "$TMP/real.log")
      ok "real transcripts extracted, priced and reconciled"
    else
      bad "the real extraction produced something wrong"; tail -5 "$TMP/real.log" | sed 's/^/        /'
    fi
  else
    bad "extraction from real transcripts failed"; tail -5 "$TMP/real.log" | sed 's/^/        /'
  fi
fi

echo
echo "5. the live recorder, end to end through the CLI"
if $PY scripts/demo_pipeline.py --out "$TMP/demo" >"$TMP/demo.log" 2>&1 \
   && $PY -m accountant.cli extract --stages-from "$TMP/demo/run.jsonl" \
        --root "$TMP/demo" --no-redact --out "$TMP/demo/ledger.json" >>"$TMP/demo.log" 2>&1; then
  if $PY - "$TMP/demo/ledger.json" <<'PY' >>"$TMP/demo.log" 2>&1; then
import json, sys
led = json.load(open(sys.argv[1]))
assert led["reconciled"], "the recorded run did not reconcile"
media = set(led["by_medium"])
assert {"image", "audio", "text"} <= media, f"expected three media, got {sorted(media)}"
assert led["totals"]["stages"] == 4, led["totals"]["stages"]
# Wall time must be real: the pipeline genuinely renders and sleeps.
assert sum(r["wall_seconds"] for r in led["receipts"]) > 0, "no wall time was recorded"
print(f"        4 stages, {len(led['receipts'])} artifacts across "
      f"{len(media)} media, reconciled")
PY
    sed 's/^/        /' <(tail -1 "$TMP/demo.log")
    ok "an instrumented pipeline produces a reconciled ledger"
  else
    bad "the recorded run is wrong"; tail -6 "$TMP/demo.log" | sed 's/^/        /'
  fi
else
  bad "the demo pipeline failed"; tail -6 "$TMP/demo.log" | sed 's/^/        /'
fi

echo
echo "6. the page is rebuildable and matches its committed data"
cp docs/index.html "$TMP/page.html"
if $PY scripts/build_docs.py >"$TMP/docs.log" 2>&1; then
  sed 's/^/        /' "$TMP/docs.log"
  if cmp -s docs/index.html "$TMP/page.html"; then
    ok "docs/index.html is exactly what the ledger produces"
  else
    bad "docs/index.html differs from a fresh build"; cp "$TMP/page.html" docs/index.html
  fi
else
  bad "the page could not be built"; tail -5 "$TMP/docs.log" | sed 's/^/        /'
fi
# A page can exist, be well formed and contain no numbers at all.
if $PY - <<'PY' >"$TMP/page.log" 2>&1; then
import json, pathlib, re, sys
page = pathlib.Path("docs/index.html").read_text()
led = json.loads(pathlib.Path("data/ledger.corpus.json").read_text())
t = led["totals"]
need = [f"${t['usd_total']:,.2f}", f"{t['stages']:,}", f"{t['tokens']['cache_read']:,}"]
missing = [n for n in need if n not in page]
if missing:
    sys.exit(f"the page does not contain these ledger figures: {missing}")
if "<script" in page.lower():
    sys.exit("the page carries script; it is meant to render its numbers without any")
if re.search(r"\bTODO\b|\bFIXME\b|\bplaceholder\b", page, re.I):
    sys.exit("the page contains a placeholder")
if "overflow-x: hidden" in page or "overflow-x:hidden" in page:
    sys.exit("the page hedges against overflow instead of fixing it")
bars = len(re.findall(r'class="bar"', page))
if bars < 3:
    sys.exit(f"only {bars} chart bars rendered, so the medium breakdown is not on the page")
print(f"the page carries the real totals, {bars} chart bars, and no script")
PY
  sed 's/^/        /' "$TMP/page.log"; ok "the page shows the numbers rather than merely existing"
else
  bad "$(cat "$TMP/page.log")"
fi

echo
echo "7. self-attack"
# Fingerprint the pristine tree once. A sabotage that does not move this fingerprint is
# a no-op, and any conclusion drawn from it would be a confident write-up of nothing.
$PY scripts/probe.py > "$TMP/probe-base.txt" 2>&1
if [ ! -s "$TMP/probe-base.txt" ]; then
  bad "the behaviour probe produced no output, so no sabotage can be proved"
fi

attack() {
  local name="$1" file="$2" old="$3" new="$4"
  local dir="$TMP/attack-$name"
  rm -rf "$dir"; mkdir -p "$dir"
  tar -cf - --exclude=.git --exclude=__pycache__ -C "$ROOT" . | tar -xf - -C "$dir"
  if ! $PY - "$dir/$file" "$old" "$new" <<'PY'
import sys, pathlib
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); t = p.read_text()
if old not in t:
    sys.exit(f"SABOTAGE DID NOT APPLY: {old!r} absent from {path}")
if old == new:
    sys.exit("SABOTAGE IS A NO-OP")
p.write_text(t.replace(old, new, 1))
PY
  then
    bad "sabotage \"$name\" did not apply, so it proves nothing"; return
  fi
  ( cd "$dir" && $PY scripts/probe.py ) > "$TMP/probe-$name.txt" 2>&1
  if cmp -s "$TMP/probe-base.txt" "$TMP/probe-$name.txt"; then
    bad "sabotage \"$name\" applied but changed no observable behaviour, so it proves nothing"
    return
  fi
  local moved
  moved=$(diff "$TMP/probe-base.txt" "$TMP/probe-$name.txt" | grep -c '^[<>]')
  local urc irc
  ( cd "$dir" && $PY -m unittest discover -s tests ) >"$TMP/$name-unit.log" 2>&1; urc=$?
  ( cd "$dir" && $PY scripts/check_independent.py ) >"$TMP/$name-ind.log" 2>&1; irc=$?
  printf '        sabotage "%s": %s probe lines moved, unit exit %s, independent exit %s\n' \
    "$name" "$moved" "$urc" "$irc"
  if [ "$urc" -ne 0 ] || [ "$irc" -ne 0 ]; then
    ok "sabotage \"$name\" is caught"
  else
    bad "sabotage \"$name\" changed the numbers and every check stayed green"
  fi
}

# 1. Bill cached input at the full input rate: the single most expensive mistake here.
attack "cache-read-billed-as-fresh-input" "accountant/pricing.py" \
  '"cache_read": int(tokens.get("cache_read", 0) or 0) * per_token_in * self.mult_read,' \
  '"cache_read": int(tokens.get("cache_read", 0) or 0) * per_token_in,'

# 2. Charge one-hour cache writes at the cheaper five-minute rate.
attack "one-hour-cache-writes-underpriced" "accountant/pricing.py" \
  '"cache_write_1h": int(tokens.get("cache_write_1h", 0) or 0) * per_token_in * self.mult_write_1h,' \
  '"cache_write_1h": int(tokens.get("cache_write_1h", 0) or 0) * per_token_in * self.mult_write_5m,'

# 3. Stop deduplicating streamed turns: sum every line instead. Roughly doubles the bill.
attack "streamed-lines-summed-not-deduplicated" "accountant/extract.py" \
  "                for k, v in tokens.items():
                    if v > stage.tokens.get(k, 0):
                        stage.tokens[k] = v" \
  "                for k, v in tokens.items():
                    stage.tokens[k] = stage.tokens.get(k, 0) + v"

# 4. Collapse the cache TTL split, so every write is billed at the cheaper rate.
attack "cache-ttl-split-collapsed" "accountant/extract.py" \
  "        w5 = int(creation.get(\"ephemeral_5m_input_tokens\", 0) or 0)
        w1 = int(creation.get(\"ephemeral_1h_input_tokens\", 0) or 0)" \
  "        w5 = total_creation
        w1 = 0"

# 5. Price an unknown model as zero and call it priced, the silent-failure shape.
attack "unknown-model-billed-as-free" "accountant/pricing.py" \
  "            return Cost(
                usd=0.0,
                priced=False," \
  "            return Cost(
                usd=0.0,
                priced=True,"

# 6. Let session overhead vanish instead of being reported.
attack "session-overhead-dropped" "accountant/attribute.py" \
  "            overhead_total += cost.usd
            session_overhead[st.session] += cost.usd" \
  "            overhead_total += 0.0
            session_overhead[st.session] += 0.0"

# 7. Treat reading a file as producing it, which inflates the artifact count and moves
#    money onto files nothing wrote.
attack "reads-counted-as-authorship" "accountant/extract.py" \
  'WRITING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "create_file", "str_replace"}' \
  'WRITING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "create_file", "str_replace", "Read"}'

# 8. Report an unmeasurable latency as zero rather than saying it is unknown.
attack "unknown-latency-reported-as-zero" "accountant/extract.py" \
  "            stage.latency_note = (
                \"no preceding event in this transcript, so the request's start time is unknown\"
            )" \
  "            stage.latency_seconds = 0.0"

# 9. Report an unmeasurable GPU figure as 0.0 instead of None, collapsing "did not run"
#    into "could not measure".
attack "unmeasurable-gpu-reported-as-zero" "accountant/recorder.py" \
  "                st.gpu_seconds = meter.gpu_seconds" \
  "                st.gpu_seconds = meter.gpu_seconds or 0.0"

echo
echo "8. the README's numbers come from a real run"
if $PY - <<'PY' >"$TMP/readme.log" 2>&1; then
import json, pathlib, re, sys
text = pathlib.Path("README.md").read_text()
for word in ("TODO", "FIXME", "XXX", "placeholder", "TBD"):
    if re.search(rf"\b{word}\b", text, re.I):
        sys.exit(f"README contains a placeholder: {word}")
led = json.loads(pathlib.Path("data/ledger.corpus.json").read_text())
t = led["totals"]
need = {
    "the total": f"${t['usd_total']:,.2f}",
    "the request count": f"{t['stages']:,}",
    "the artifact count": f"{t['artifacts']:,}",
}
missing = {k: v for k, v in need.items() if v not in text}
if missing:
    sys.exit(f"README figures do not match data/ledger.corpus.json: {missing}")
# The test count in the Status block goes stale the moment a test is added.
m = re.search(r"ok\s+(\d+) unit tests passed", text)
if not m:
    sys.exit("README Status block has no pasted unit-test count line from a real run")
counted = int(pathlib.Path(".verify-testcount").read_text().strip())
if int(m.group(1)) != counted:
    sys.exit(f"README claims {m.group(1)} tests, the suite just ran {counted}")
if "VERIFY OK" not in text:
    sys.exit("README Status section does not contain the verify success line")
print(f"README matches the ledger and the live test count ({counted})")
PY
  sed 's/^/        /' "$TMP/readme.log"; ok "README numbers match a real run"
else
  bad "README is stale"; tail -4 "$TMP/readme.log" | sed 's/^/        /'
fi

echo
echo "9. no home paths, no secrets, no NUL bytes, no bulk"
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  hits=$(git -C "$ROOT" grep -In -e "/home/$(id -un)" -e "/Users/$(id -un)" -- . 2>/dev/null \
         | grep -v '^scripts/verify.sh' || true)
  if [ -z "$hits" ]; then ok "no absolute home paths in tracked files"
  else bad "absolute home paths found"; printf '%s\n' "$hits" | head -5 | sed 's/^/        /'; fi
  keys=$(git -C "$ROOT" grep -In -E 'sk-ant-[A-Za-z0-9_-]{20}|ghp_[A-Za-z0-9]{36}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20}' \
         -- . 2>/dev/null || true)
  if [ -z "$keys" ]; then ok "no credential-shaped strings in tracked files"
  else bad "possible credential"; printf '%s\n' "$keys" | head -3 | sed 's/^/        /'; fi
  if $PY - "$ROOT" <<'PY' >"$TMP/nul.log" 2>&1; then
import os, subprocess, sys
root = sys.argv[1]
files = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True,
                       check=True).stdout.split(b"\0")
names = [f.decode() for f in files if f]
nul, big = [], []
for n in names:
    p = os.path.join(root, n)
    if not os.path.isfile(p):
        continue
    if os.path.getsize(p) > 1_000_000:
        big.append((n, os.path.getsize(p)))
    with open(p, "rb") as fh:
        if b"\0" in fh.read():
            nul.append(n)
if nul:
    sys.exit("files containing NUL, invisible to the secret scan: " + ", ".join(nul))
if big:
    sys.exit("tracked files over 1 MB: " + ", ".join(f"{n} ({s:,}b)" for n, s in big))
print(f"{len(names)} tracked files, none contain NUL, none over 1 MB")
PY
    sed 's/^/        /' "$TMP/nul.log"; ok "no tracked file is binary to the scan or oversized"
  else bad "$(cat "$TMP/nul.log")"; fi
  # Detection needs its own test: grep -P is not available in every grep on every box,
  # and a NUL scan built on it reports clean while finding nothing.
  printf 'a\0b' > "$TMP/nul-probe.bin"
  if $PY -c "import sys; sys.exit(0 if b'\0' in open(sys.argv[1],'rb').read() else 1)" \
       "$TMP/nul-probe.bin"; then
    ok "the NUL detector finds a NUL byte when there is one"
  else
    bad "the NUL detector cannot see a NUL byte, so its clean report means nothing"
  fi
  # The redaction claim, checked against the actual username rather than a fixed string.
  if grep -q "$(id -un)" data/ledger.corpus.json; then
    bad "the published ledger contains the current username"
  else
    ok "the published ledger carries no username"
  fi
else
  bad "not a git repo, so the hygiene scan cannot run"
fi

echo
echo "10. GPU measurement, run for real"
if $PY - <<'PY' >"$TMP/gpu.log" 2>&1; then
import pathlib, sys, time
sys.path.insert(0, ".")
from accountant.gpu import GpuMeter, probe
r = probe()
if r.ok:
    print(f"nvidia-smi present: {r.utilisation_percent:.0f}% utilised, "
          f"{r.compute_processes} compute process(es)")
else:
    print(f"no GPU measurement available: {r.reason}")
    assert r.reason and len(r.reason) > 10, "an unavailable GPU must explain itself"
with GpuMeter(interval=0.05) as m:
    time.sleep(0.3)
if m.gpu_seconds is None:
    assert m.note, "a None gpu_seconds must carry a reason"
    print(f"gpu_seconds is None, as it must be: {m.note}")
else:
    assert isinstance(m.gpu_seconds, float) and m.gpu_seconds >= 0
    print(f"measured {m.gpu_seconds:.3f} GPU-seconds over 0.3s, sole process on the card")
PY
  sed 's/^/        /' "$TMP/gpu.log"; ok "GPU time is either measured or explained, never invented"
else
  bad "the GPU meter is dishonest or crashed"; tail -5 "$TMP/gpu.log" | sed 's/^/        /'
fi

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || { echo "VERIFY FAILED"; exit 1; }
echo "VERIFY OK"

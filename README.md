# cost-accountant

A per-artifact cost and latency accountant for AI pipelines. It reads real token usage out
of Claude Code transcripts, prices every token class at its own rate, attributes the spend
to the files that were actually produced, and emits a receipt per artifact plus a rolling
breakdown of cost per published piece by medium.

> Measurements described here were taken on one development machine: an RTX 5090 with
> 32 GB of VRAM, 12 cores, 48 GB of RAM, running Linux under WSL2. Numbers from your own
> hardware will differ.

**[Live page, every number generated from the ledger](https://jesserweigel.github.io/cost-accountant/)**

The problem it solves is narrow and specific. A modern agent turn spends almost all of its
tokens on cached input, and the token classes are priced differently:

| class | rate |
|---|---|
| input | full price |
| output | its own price, five times input on every current model |
| cache read | 0.1x the input price |
| cache write, five-minute TTL | 1.25x the input price |
| cache write, one-hour TTL | 2.0x the input price |

Add them together, multiply by one rate, and you get a confident number that is wrong. On
the corpus below that mistake produces **$27,443.79** against a real **$6,110.01**.

## Using it

```bash
# account for everything Claude Code has already spent on the development machine
python3 -m accountant.cli extract --out ledger.json
python3 -m accountant.cli report ledger.json

# the receipt for one artifact
python3 -m accountant.cli receipt ledger.json README.md

# scope the ledger to the sessions that built one directory
python3 -m accountant.cli extract --root . --only-sessions-touching-root \
    --no-redact --out mine.json

# instrument a live pipeline instead
python3 scripts/demo_pipeline.py --out run/
python3 -m accountant.cli extract --stages-from run/run.jsonl --root run/ --out run/ledger.json
```

Python 3.9 or newer and nothing else. No dependencies, no build step, no network access.

Instrumenting your own pipeline is a context manager. Wall time is measured, GPU seconds
are attempted, and token counts are whatever you hand it:

```python
from accountant.recorder import Recorder

rec = Recorder("run/stages.jsonl")
with rec.stage("render", artifact="out/cover.png"):
    draw()
with rec.stage("caption", artifact="out/cover.png", model="claude-opus-5") as s:
    resp = client.messages.create(...)
    s.tokens(input=resp.usage.input_tokens, output=resp.usage.output_tokens,
             cache_read=resp.usage.cache_read_input_tokens)
```

## What the corpus says

Every figure here comes from real Claude Code transcripts on one workstation: actual token
counts from `message.usage`, actual timestamps. The transcripts themselves are not
published. The extractor is, so you can re-derive the equivalent numbers from your own
history.

```
18,280 stages, 2,569 of which wrote a file
1,162 artifacts
4,608,389,223 tokens: input 552,154, output 15,648,554, cache_read 4,371,726,469,
                      cache_write_5m 27,731,076, cache_write_1h 192,730,970
$6,110.01 total  ($504.01 attributable directly, $5,606.00 session overhead)
```

**94.9% of the tokens are cache reads.** Billed at a tenth of the input rate, they are the
overwhelming majority of the volume and a modest share of the bill. Summing
`input_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens` into one figure
overstates this corpus by 4.5x. Both numbers are in `data/ledger.corpus.json`, broken down
per model, so the comparison can be checked rather than taken on trust.

**A streamed turn writes its full usage object on every line.** The 18,280 distinct API
requests here are spread across roughly twice as many usage-bearing lines, each repeating
the same `usage`. Summing over lines nearly doubles the bill. Deduplication is by
`requestId`, taking the field-wise maximum, because a few request groups pair a real usage
record with an all-zero twin and both first-wins and last-wins are a coin flip.

**Only 8.2% of the money lands on a file.** 2,569 of 18,280 requests wrote or edited
anything; the rest read, searched, ran commands and thought. That overhead is real and it
is most of the bill, so pretending otherwise would make every per-artifact figure a
fiction. Each receipt therefore carries two numbers: the direct cost of the requests that
wrote the file, and the cost with session overhead amortised across that session's
artifacts in proportion to direct cost. Overhead from a session that produced no artifact
has nowhere to go and is reported as unattributable rather than quietly spread around.

Cost per published piece, by medium:

| medium | pieces | total | per piece |
|---|---|---|---|
| code | 928 | $4,078.57 | $4.40 |
| text | 137 | $1,624.30 | $11.86 |
| page | 20 | $67.61 | $3.38 |
| data | 52 | $28.17 | $0.54 |
| other | 25 | $24.23 | $0.97 |

`data/ledger.self.json` is the same extractor pointed at this repository, so the project
bills itself: 20 artifacts, $19.10, with `accountant/ledger.py` the most expensive file at
$2.41 including amortised overhead. It is a lower bound, because the session that produced
it was still running when the ledger was written.

## Two bugs the numbers caught

Both were found by looking at output, not by reasoning about the code.

**Transcript discovery missed every subagent session.** Claude Code writes subagent
transcripts to `<session>/subagents/*.jsonl`, two levels below the project directory, and
the original glob only looked one level down. The first corpus ledger read 593 of 692
transcripts and reported $5,371. Recursive discovery found the rest and the real figure is
$6,110, so the omission was 14% of the bill rather than a rounding error. Subagents are
where a fleet spends most of its tokens, which is exactly why the gap mattered.

**Reconciliation passed on an attribution built from different stages.** The two derivations
each compared a pair of numbers computed from the same input, so an attribution assembled
from a dropped or duplicated stage list satisfied both. A test written to prove the checks
could fail found it. There is now a third check tying the halves together.

## Prices are configuration, not constants

`pricing.json` holds the rates, the cache multipliers, and a note about where they came
from. They are Anthropic list prices for the first-party API, taken from the `claude-api`
reference bundled with Claude Code, retrieved 2026-08-03. They are **not** what every
account pays: Bedrock and Vertex are priced separately, the Batches API is half price,
Sonnet 5 has an introductory rate, and negotiated rates exist. Edit the file.

A model with no entry is never billed as free. The `Cost` object carries a `priced` flag
and the token count it could not price, every report prints the unpriced total separately,
and reconciliation checks that both derivations agree on how much was left out.

The loader also refuses a price list that flattens the cache tiers. A read multiplier of
1.0 or more is not a discount, and a file claiming one would produce a wrong figure for
every artifact, so it fails to load rather than proceeding.

## GPU seconds, and the three states people collapse into one

`gpu_seconds` is a number only when the measurement is genuinely attributable. Otherwise it
is `None` with a reason. It is never `0.0` standing in for "do not know", because that is a
claim that the GPU did nothing.

- **measured**: `nvidia-smi` is present and this process was the only compute client on
  the card, so device utilisation integrated over the stage is ours.
- **shared**: other processes held the GPU. The device-wide figure is reported and the
  attributed figure stays `None`, because per-process utilisation is not exposed on every
  platform, including WSL2.
- **unavailable**: no `nvidia-smi`, or it failed. `None`, with the reason.

`python3 -m accountant.cli gpu` says which of the three applies on your machine.

## Verification

```bash
bash scripts/verify.sh
```

Ten layers. The ones that do the work:

- Every test is paired with a **negative control** proving it can fail. The deduplication
  test computes the naive line-sum alongside the deduplicated one and asserts the fixture
  genuinely differs; the cache-tier test computes what flattening the tiers would give and
  asserts it is a different number.
- `scripts/check_independent.py` **imports nothing from `accountant/`**. It writes its own
  synthetic corpus, prices it with its own arithmetic read from `pricing.json`, attributes
  it with plain dictionaries, drives the **shipped CLI** as a subprocess, and compares.
  That independence is checked by walking the import graph with `ast`, and the walk is
  itself controlled against a file that does import the package.
- Totals are **reconciled two independent ways**: price-then-sum against sum-then-price,
  and attributed-plus-unattributable against the total, with a third check tying the halves
  together.
- **Nine sabotages**, each proved to have applied and then proved to have moved a
  behavioural fingerprint before its result counts for anything. An attack that silently
  fails to change anything is a no-op with a confident write-up attached.
- Real transcripts are extracted for real. If there are none, that layer **fails** rather
  than passing quietly, and names the environment variable to set.
- The page is rebuilt and compared byte for byte, then asserted to contain the ledger's
  actual figures. A page can be well formed, contain no numbers, and pass every unit test.

## What is not covered

- **Attribution is a policy, not a fact.** A request does not come with a bill of
  materials. Both policies are documented, both are reported, and the residue is named.
- **Dollar figures are list-price estimates, not invoices.** They cannot see negotiated
  rates, batch discounts, or spend outside the API.
- **Latency is model latency, not human time.** It runs from the previous conversation
  event to the last content block of the response, so it includes queueing and waiting on
  tool results. Where the preceding event is missing from the transcript it is `None` with
  a note rather than zero.
- **Local stages cost $0.00 by default.** `pricing.json` sets `local.gpu_dollars_per_hour`
  to zero, because a GPU you already own has no marginal per-second price. Set it to an
  amortised rate if you want those stages to carry a figure.
- **Reading a full corpus is slow.** 478 MB of JSONL takes about four minutes here, almost
  all of it I/O. `--limit` bounds it. There is no incremental cache.
- **Only Claude Code transcripts are parsed.** Other providers would need their own
  extractor; the ledger, pricing and attribution layers do not care where a stage came
  from, which is what the recorder is for.

## Files

```
accountant/pricing.py     token classes, cache multipliers, unpriced-model handling
accountant/extract.py     transcript parser, deduplication, artifact and latency recovery
accountant/attribute.py   media taxonomy, direct and amortised attribution
accountant/ledger.py      serialisation and the reconciliation checks
accountant/recorder.py    live instrumentation for your own pipeline stages
accountant/gpu.py         GPU sampling that reports what it could not measure
accountant/cli.py         extract / report / receipt / gpu
pricing.json              the rates, meant to be edited
data/ledger.corpus.json   the published corpus ledger, artifact paths redacted
data/ledger.self.json     this repository accounting for its own construction
scripts/verify.sh         everything above, gated
scripts/check_independent.py   the audit that shares no code with the package
scripts/probe.py          the behavioural fingerprint the sabotages are measured against
scripts/build_docs.py     generates docs/index.html from the ledgers
scripts/demo_pipeline.py  a small instrumented pipeline, three media, real wall time
```

## Status

```
$ bash scripts/verify.sh
0. environment
        Python 3.12.3
        GNU bash, version 5.2.21(1)-release (x86_64-pc-linux-gnu)
  ok    python 3.9+ and bash present, no third-party packages required

1. unit suite
  ok    68 unit tests passed

2. the arithmetic re-derived by code that shares nothing with the package
        synthetic corpus: 16 properties re-derived independently, all agree
        ledger.corpus.json: 6 published-ledger properties hold
        ledger.self.json: 6 published-ledger properties hold
        28 properties checked with code that imports nothing from accountant/
  ok    independent re-derivation agrees on every property

3. the checker really is independent
        check_independent.py imports nothing from accountant/; the same walk finds 5 such imports in probe.py
  ok    the checker shares no code with the package

4. real transcripts, extracted for real
        wrote /tmp/tmp.ZrLTGURNYw/real.json: 440 stages, 0 artifacts, $33.36
                440 requests from 695 available transcripts, 24,298,407 tokens, $33.36
  ok    real transcripts extracted, priced and reconciled

5. the live recorder, end to end through the CLI
                4 stages, 3 artifacts across 3 media, reconciled
  ok    an instrumented pipeline produces a reconciled ledger

6. the page is rebuildable and matches its committed data
        wrote docs/index.html (20,375 bytes)
  ok    docs/index.html is exactly what the ledger produces
        the page carries the real totals, 8 chart bars, and no script
  ok    the page shows the numbers rather than merely existing

7. self-attack
        sabotage "cache-read-billed-as-fresh-input": 8 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "cache-read-billed-as-fresh-input" is caught
        sabotage "one-hour-cache-writes-underpriced": 8 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "one-hour-cache-writes-underpriced" is caught
        sabotage "streamed-lines-summed-not-deduplicated": 8 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "streamed-lines-summed-not-deduplicated" is caught
        sabotage "cache-ttl-split-collapsed": 8 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "cache-ttl-split-collapsed" is caught
        sabotage "unknown-model-billed-as-free": 2 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "unknown-model-billed-as-free" is caught
        sabotage "session-overhead-dropped": 10 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "session-overhead-dropped" is caught
        sabotage "reads-counted-as-authorship": 5 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "reads-counted-as-authorship" is caught
        sabotage "unknown-latency-reported-as-zero": 2 probe lines moved, unit exit 1, independent exit 1
  ok    sabotage "unknown-latency-reported-as-zero" is caught
        sabotage "unmeasurable-gpu-reported-as-zero": 2 probe lines moved, unit exit 1, independent exit 0
  ok    sabotage "unmeasurable-gpu-reported-as-zero" is caught

8. the README's numbers come from a real run
        README matches the ledger and the live test count (68)
  ok    README numbers match a real run

9. no home paths, no secrets, no NUL bytes, no bulk
  ok    no absolute home paths in tracked files
  ok    no credential-shaped strings in tracked files
        25 tracked files, none contain NUL, none over 1 MB
  ok    no tracked file is binary to the scan or oversized
  ok    the NUL detector finds a NUL byte when there is one
  ok    the published ledger carries no username

10. GPU measurement, run for real
        nvidia-smi present: 77% utilised, 1 compute process(es)
        measured 0.230 GPU-seconds over 0.3s, sole process on the card
  ok    GPU time is either measured or explained, never invented

24 passed, 0 failed
VERIFY OK
```

Task MEDIA-047 from [722 things to build](https://github.com/JesseRWeigel/722-things-to-build).

MIT.

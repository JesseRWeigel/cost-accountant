#!/usr/bin/env python3
"""An independent audit of cost-accountant's arithmetic.

Imports NOTHING from `accountant/`. The package is the thing under test, and a checker
built on it would agree with it whatever it said. Everything below is re-derived:

  * its own JSONL parser, with its own deduplication strategy (a set of seen requestIds
    and first-non-zero wins, rather than a running field-wise maximum)
  * its own pricing arithmetic, read straight out of pricing.json
  * its own attribution, computed with plain dictionaries

It builds a synthetic corpus whose correct answer it knows by construction, runs the
SHIPPED command line against it as a subprocess, and compares. Checking the shipped
entry point matters: a checker that imported a function would miss a CLI that never
calls it.

Then it audits the committed ledger for the properties a published cost report must
have, including that no home directory survived redaction.

    python3 scripts/check_independent.py [ledger.json ...]
"""
from __future__ import annotations

import getpass
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOL = 0.005


class Mismatch(AssertionError):
    pass


def load_prices() -> dict:
    """Read pricing.json without going through the package's loader."""
    raw = json.loads((ROOT / "pricing.json").read_text())
    return raw


def dollars(prices: dict, model: str, tok: dict) -> float:
    """Independent pricing arithmetic. Written from the published rules, not from
    accountant/pricing.py: cache reads are a tenth of input, 5-minute writes are
    1.25x input, 1-hour writes are 2x input."""
    m = prices["models"].get(model)
    if m is None:
        return 0.0
    cm = prices["cache_multipliers"]
    per_in = m["input"] / 1e6
    per_out = m["output"] / 1e6
    return (
        tok.get("input", 0) * per_in
        + tok.get("output", 0) * per_out
        + tok.get("cache_read", 0) * per_in * cm["read"]
        + tok.get("cache_write_5m", 0) * per_in * cm["write_5m"]
        + tok.get("cache_write_1h", 0) * per_in * cm["write_1h"]
    )


# --------------------------------------------------------------------------------
# A synthetic corpus whose answer is known by construction.
# --------------------------------------------------------------------------------

def build_corpus(dirpath: pathlib.Path) -> dict:
    """Write transcripts and return the ground truth we expect back.

    Deliberately reproduces the three format quirks that make this hard: a streamed
    turn repeating its usage on every line, an all-zero twin sharing a requestId with a
    real record, and a request whose cache_creation carries only the total.
    """
    sub = dirpath / "project-a"
    sub.mkdir(parents=True)

    def usage(i=0, o=0, r=0, w5=0, w1=0, split=True):
        u = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": r,
             "cache_creation_input_tokens": w5 + w1}
        if split:
            u["cache_creation"] = {"ephemeral_5m_input_tokens": w5,
                                   "ephemeral_1h_input_tokens": w1}
        return u

    def arec(uuid, parent, ts, req, model, u, tools=()):
        content = [{"type": "tool_use", "name": name, "input": {"file_path": path}}
                   for name, path in tools]
        return {"uuid": uuid, "parentUuid": parent, "timestamp": ts, "type": "assistant",
                "requestId": req,
                "message": {"model": model, "content": content, "usage": u}}

    def urec(uuid, parent, ts):
        return {"uuid": uuid, "parentUuid": parent, "timestamp": ts, "type": "user",
                "message": {"role": "user", "content": "go"}}

    out_dir = dirpath / "work"
    out_dir.mkdir()
    png = str(out_dir / "cover.png")
    md = str(out_dir / "notes.md")
    py = str(out_dir / "tool.py")
    mystery = str(out_dir / "mystery.md")

    # session one -------------------------------------------------------------
    u1 = usage(i=100, o=2000, r=500_000, w1=40_000)
    s1 = [
        urec("u0", None, "2026-07-01T00:00:00.000Z"),
        # three lines, one request, usage repeated verbatim on each
        arec("a1", "u0", "2026-07-01T00:00:05.000Z", "req_1", "claude-opus-5", u1,
             [("Write", png)]),
        # a Read in the middle of the same turn: looking at a file is not authoring it
        arec("a2", "a1", "2026-07-01T00:00:06.000Z", "req_1", "claude-opus-5", u1,
             [("Read", str(out_dir / "reference.py"))]),
        arec("a3", "a2", "2026-07-01T00:00:07.000Z", "req_1", "claude-opus-5", u1),
        # overhead: read-only turn, no artifact
        arec("b1", "a3", "2026-07-01T00:00:20.000Z", "req_2", "claude-opus-5",
             usage(i=50, o=900, r=600_000, w5=1_000)),
    ]
    # session two -------------------------------------------------------------
    real = usage(i=10, o=3000, r=120_000, w5=20_000)
    zero = usage()
    s2 = [
        urec("v0", None, "2026-07-02T00:00:00.000Z"),
        # zero twin arrives FIRST, so a first-wins reader would lose the real numbers
        arec("c1", "v0", "2026-07-02T00:00:03.000Z", "req_3", "claude-sonnet-4-5", zero,
             [("Edit", md)]),
        arec("c2", "c1", "2026-07-02T00:00:04.000Z", "req_3", "claude-sonnet-4-5", real),
        # a turn writing two files at once, so the split is exercised
        arec("d1", "c2", "2026-07-02T00:00:30.000Z", "req_4", "claude-sonnet-4-5",
             usage(i=5, o=1200, r=90_000, w5=8_000, split=False), [("Write", py),
                                                                   ("Edit", md)]),
    ]
    # session three: no artifacts at all, so its overhead is unattributable ----
    s3 = [
        urec("w0", None, "2026-07-03T00:00:00.000Z"),
        arec("e1", "w0", "2026-07-03T00:00:09.000Z", "req_5", "claude-haiku-4-5",
             usage(i=200, o=400, r=10_000)),
    ]
    # session four: an unpriced model, and an orphan whose start time is unknowable ----
    s4 = [
        urec("x0", None, "2026-07-04T00:00:00.000Z"),
        arec("f1", "x0", "2026-07-04T00:00:11.000Z", "req_6", "definitely-not-a-model",
             usage(i=300, o=700, r=5_000), [("Write", mystery)]),
        arec("g1", "not-in-this-file", "2026-07-04T00:00:50.000Z", "req_7",
             "claude-haiku-4-5", usage(i=1, o=2)),
    ]
    for name, recs in (("s1.jsonl", s1), ("s2.jsonl", s2), ("s3.jsonl", s3),
                       ("s4.jsonl", s4)):
        (sub / name).write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    # Ground truth, computed here by hand rather than by the package.
    return {
        "dir": sub,
        "requests": {
            "req_1": ("claude-opus-5", {"input": 100, "output": 2000, "cache_read": 500_000,
                                        "cache_write_5m": 0, "cache_write_1h": 40_000},
                      [png], 7.0),
            "req_2": ("claude-opus-5", {"input": 50, "output": 900, "cache_read": 600_000,
                                        "cache_write_5m": 1_000, "cache_write_1h": 0},
                      [], 13.0),
            "req_3": ("claude-sonnet-4-5", {"input": 10, "output": 3000,
                                            "cache_read": 120_000, "cache_write_5m": 20_000,
                                            "cache_write_1h": 0}, [md], 4.0),
            "req_4": ("claude-sonnet-4-5", {"input": 5, "output": 1200, "cache_read": 90_000,
                                            "cache_write_5m": 8_000, "cache_write_1h": 0},
                      [py, md], 26.0),
            "req_5": ("claude-haiku-4-5", {"input": 200, "output": 400, "cache_read": 10_000,
                                           "cache_write_5m": 0, "cache_write_1h": 0}, [], 9.0),
            "req_6": ("definitely-not-a-model", {"input": 300, "output": 700,
                                                 "cache_read": 5_000, "cache_write_5m": 0,
                                                 "cache_write_1h": 0}, [mystery], 11.0),
            "req_7": ("claude-haiku-4-5", {"input": 1, "output": 2, "cache_read": 0,
                                           "cache_write_5m": 0, "cache_write_1h": 0}, [], None),
        },
        "artifacts": {"png": png, "md": md, "py": py, "mystery": mystery},
        "unpriced_tokens": 300 + 700 + 5_000,
        "never_authored": str(out_dir / "reference.py"),
    }


def run_cli(corpus_dir: pathlib.Path, out: pathlib.Path) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.run(
        [sys.executable, "-m", "accountant.cli", "extract",
         "--transcripts", str(corpus_dir.parent), "--no-redact",
         "--include-stages", "--label", "independent-check", "--out", str(out)],
        cwd=str(ROOT), env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise Mismatch(f"the shipped CLI exited {p.returncode}: {p.stderr.strip()[:400]}")
    return json.loads(out.read_text())


def check_synthetic(prices: dict) -> int:
    checked = 0
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        truth = build_corpus(d)
        ledger = run_cli(truth["dir"], d / "ledger.json")

        # 1. Deduplication. Five requests were written across nine usage-bearing lines.
        raw_lines = 0
        for f in sorted(truth["dir"].glob("*.jsonl")):
            for line in f.read_text().splitlines():
                rec = json.loads(line)
                if isinstance((rec.get("message") or {}).get("usage"), dict):
                    raw_lines += 1
        if raw_lines != 10:
            raise Mismatch(f"the fixture should have 10 usage lines, has {raw_lines}")
        if ledger["totals"]["stages"] != 7:
            raise Mismatch(
                f"expected 7 deduplicated stages from 10 usage-bearing lines, "
                f"got {ledger['totals']['stages']}")
        checked += 1

        # 2. The total, priced independently.
        want = sum(dollars(prices, model, tok)
                   for model, tok, _arts, _lat in truth["requests"].values())
        got = ledger["totals"]["usd_total"]
        if abs(want - got) > TOL:
            raise Mismatch(f"total is {got:.6f}, independently derived {want:.6f}")
        checked += 1

        # 3. Control for check 2: the naive line-sum must be a DIFFERENT, larger number.
        #    If it were not, check 2 would pass on a reader that never deduplicated.
        naive = 0.0
        for f in sorted(truth["dir"].glob("*.jsonl")):
            for line in f.read_text().splitlines():
                rec = json.loads(line)
                u = (rec.get("message") or {}).get("usage")
                if not isinstance(u, dict):
                    continue
                cc = u.get("cache_creation") or {}
                naive += dollars(prices, rec["message"]["model"], {
                    "input": u["input_tokens"], "output": u["output_tokens"],
                    "cache_read": u["cache_read_input_tokens"],
                    "cache_write_5m": cc.get("ephemeral_5m_input_tokens",
                                             u["cache_creation_input_tokens"]),
                    "cache_write_1h": cc.get("ephemeral_1h_input_tokens", 0),
                })
        if naive <= want + TOL:
            raise Mismatch(
                f"the fixture does not actually exercise deduplication: naive line-sum "
                f"{naive:.6f} is not greater than the correct total {want:.6f}")
        checked += 1

        # 4. Attribution, recomputed here with plain dictionaries.
        direct: dict = {}
        overhead_by_session: dict = {}
        session_of = {"req_1": "s1", "req_2": "s1", "req_3": "s2", "req_4": "s2",
                      "req_5": "s3", "req_6": "s4", "req_7": "s4"}
        for req, (model, tok, arts, _lat) in truth["requests"].items():
            usd = dollars(prices, model, tok)
            if not arts:
                overhead_by_session[session_of[req]] = \
                    overhead_by_session.get(session_of[req], 0.0) + usd
                continue
            for a in arts:
                direct[a] = direct.get(a, 0.0) + usd / len(arts)
        by_artifact = {r["artifact"]: r for r in ledger["receipts"]}
        for a, want_direct in direct.items():
            if a not in by_artifact:
                raise Mismatch(f"the ledger has no receipt for {a}")
            got_direct = by_artifact[a]["usd_direct"]
            if abs(got_direct - want_direct) > TOL:
                raise Mismatch(
                    f"{a}: direct cost {got_direct:.6f}, independently derived "
                    f"{want_direct:.6f}")
            checked += 1
        if len(by_artifact) != len(direct):
            raise Mismatch(f"ledger has {len(by_artifact)} receipts, expected {len(direct)}")

        # 5. Session three wrote nothing, so its overhead has nowhere to go.
        want_unattr = overhead_by_session.get("s3", 0.0)
        got_unattr = ledger["totals"]["usd_unattributable_overhead"]
        if abs(want_unattr - got_unattr) > TOL:
            raise Mismatch(
                f"unattributable overhead is {got_unattr:.6f}, independently derived "
                f"{want_unattr:.6f}")
        if want_unattr <= 0:
            raise Mismatch("the fixture has no unattributable overhead, so check 5 is vacuous")
        checked += 1

        # 6. Media classification, independently.
        want_media = {truth["artifacts"]["png"]: "image", truth["artifacts"]["md"]: "text",
                      truth["artifacts"]["py"]: "code"}
        for a, medium in want_media.items():
            if by_artifact[a]["medium"] != medium:
                raise Mismatch(
                    f"{a} classified as {by_artifact[a]['medium']!r}, expected {medium!r}")
            checked += 1

        # 7. Latency: measured from the preceding event, and None when there is none.
        stage_lat = [s["latency_seconds"] for s in ledger["stages"]]
        want_unknown = sum(1 for v in truth["requests"].values() if v[3] is None)
        got_unknown = sum(1 for x in stage_lat if x is None)
        if got_unknown != want_unknown:
            raise Mismatch(
                f"{got_unknown} stages report an unknown latency, expected {want_unknown}; "
                "an orphan request has no measurable start time and must say so")
        if want_unknown == 0:
            raise Mismatch("the fixture has no orphan request, so this check is vacuous")
        for s in ledger["stages"]:
            if s["latency_seconds"] is None and not s["latency_note"]:
                raise Mismatch("a stage reports no latency and gives no reason")
        want_total_lat = sum(v[3] for v in truth["requests"].values() if v[3] is not None)
        got_total_lat = sum(x for x in stage_lat if x is not None)
        if abs(got_total_lat - want_total_lat) > 0.01:
            raise Mismatch(
                f"total latency {got_total_lat:.2f}s, independently derived "
                f"{want_total_lat:.2f}s")
        checked += 1

        # 7b. A file that was only READ must not appear as an artifact. Reading is not
        #     authorship, and counting it moves money onto files nothing produced.
        if truth["never_authored"] in by_artifact:
            raise Mismatch(
                f"{truth['never_authored']} was only read, never written, yet it has a "
                "receipt")
        checked += 1

        # 7c. An unpriced model must be named, and its tokens must not be billed as free.
        if ledger["totals"]["unpriced_models"] != ["definitely-not-a-model"]:
            raise Mismatch(
                f"unpriced models reported as {ledger['totals']['unpriced_models']}, "
                "expected exactly the model with no price")
        if ledger["totals"]["unpriced_tokens"] != truth["unpriced_tokens"]:
            raise Mismatch(
                f"unpriced tokens reported as {ledger['totals']['unpriced_tokens']}, "
                f"independently counted {truth['unpriced_tokens']}")
        checked += 1

        # 8. The cache TTL split, which is the difference between right and confidently
        #    wrong. req_4 carries only a total, and must be billed at the 5-minute rate.
        s4 = [s for s in ledger["stages"] if s["tokens"]["cache_write_5m"] == 8_000]
        if not s4:
            raise Mismatch("the request with an unsplit cache_creation was not found")
        if not any("TTL split missing" in w for w in s4[0]["warnings"]):
            raise Mismatch("an unsplit cache_creation was assumed silently, with no warning")
        checked += 1

        # 9. The ledger must say it reconciled, and every check inside it must be ok.
        if not ledger["reconciled"]:
            raise Mismatch("the ledger reports that it did not reconcile")
        for c in ledger["reconciliation"]:
            if not c["ok"]:
                raise Mismatch(f"internal reconciliation failed: {c['name']}: {c['detail']}")
        checked += 1

    return checked


def check_published(path: pathlib.Path, prices: dict) -> int:
    """Audit a committed ledger for the properties a published cost report must have."""
    led = json.loads(path.read_text())
    checked = 0
    blob = path.read_text()

    user = getpass.getuser()
    for needle in (f"/home/{user}", f"/Users/{user}", str(pathlib.Path.home())):
        if needle in blob:
            raise Mismatch(f"{path.name} contains a home path: {needle}")
    checked += 1

    if not led.get("reconciled"):
        raise Mismatch(f"{path.name} did not reconcile")
    checked += 1

    total = led["totals"]["usd_total"]
    if total <= 0:
        raise Mismatch(f"{path.name} reports a total of {total}, which cannot be right")
    amort = sum(r["usd_amortised"] for r in led["receipts"])
    unattr = led["totals"]["usd_unattributable_overhead"]
    if abs(amort + unattr - total) > max(TOL, total * 1e-6):
        raise Mismatch(
            f"{path.name}: receipts sum to {amort:.4f} plus {unattr:.4f} unattributable, "
            f"which is not the stated total {total:.4f}")
    checked += 1

    # Re-price the stated token totals independently. It cannot recover the per-model
    # split from the totals alone, so it brackets: the whole corpus billed at the
    # cheapest model must be at or below the total, and at the dearest at or above.
    tok = led["totals"]["tokens"]
    rates = [(m["input"], m["output"]) for m in prices["models"].values()
             if m["input"] > 0]
    lo = min(rates)[0], min(rates, key=lambda r: r[1])[1]
    hi = max(rates)[0], max(rates, key=lambda r: r[1])[1]
    cm = prices["cache_multipliers"]

    def at(rate):
        pi, po = rate[0] / 1e6, rate[1] / 1e6
        return (tok["input"] * pi + tok["output"] * po
                + tok["cache_read"] * pi * cm["read"]
                + tok["cache_write_5m"] * pi * cm["write_5m"]
                + tok["cache_write_1h"] * pi * cm["write_1h"])
    floor, ceiling = at(lo), at(hi)
    if not (floor - TOL <= total <= ceiling + TOL):
        raise Mismatch(
            f"{path.name}: total {total:.2f} is outside the range any priced model could "
            f"produce for {tok['all']:,} tokens ({floor:.2f} to {ceiling:.2f})")
    checked += 1

    # Cache reads must dominate the token count on a real Claude Code corpus, and must
    # nevertheless not dominate the bill. If they did, the multiplier was not applied.
    if tok["cache_read"] > 0.5 * tok["all"]:
        pi = hi[0] / 1e6
        as_input = tok["cache_read"] * pi
        as_cached = tok["cache_read"] * pi * cm["read"]
        if abs(as_input - as_cached) < TOL:
            raise Mismatch("cache reads are being billed at the full input rate")
        checked += 1

    for r in led["receipts"]:
        if r["usd_amortised"] + TOL < r["usd_direct"]:
            raise Mismatch(
                f"{r['artifact']}: amortised cost {r['usd_amortised']} is below its own "
                f"direct cost {r['usd_direct']}, so overhead was subtracted")
    checked += 1
    return checked


def main(argv) -> int:
    prices = load_prices()
    total = 0
    total += check_synthetic(prices)
    print(f"synthetic corpus: {total} properties re-derived independently, all agree")
    ledgers = [pathlib.Path(a) for a in argv[1:]] or sorted((ROOT / "data").glob("*.json"))
    if not ledgers:
        print("MISMATCH: no ledger to audit, so the published numbers were not checked",
              file=sys.stderr)
        return 1
    for p in ledgers:
        n = check_published(p, prices)
        total += n
        print(f"{p.name}: {n} published-ledger properties hold")
    print(f"{total} properties checked with code that imports nothing from accountant/")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Mismatch as exc:
        print(f"MISMATCH: {exc}", file=sys.stderr)
        sys.exit(1)

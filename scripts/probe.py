#!/usr/bin/env python3
"""Print a deterministic fingerprint of what this copy of the code actually does.

verify.sh runs this in a pristine tree and again in each sabotaged copy. If the two
fingerprints are identical, the sabotage did nothing and any conclusion drawn from it
would be worthless. Three attacks in this workspace's history "passed" because the
attack itself was a no-op, so the rule is: prove the output moved before asking whether
anything noticed.

Every line below is chosen because at least one sabotage moves it.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accountant.attribute import attribute  # noqa: E402
from accountant.extract import extract_file  # noqa: E402
from accountant.ledger import build_ledger  # noqa: E402
from accountant.pricing import Pricing, PricingError  # noqa: E402
from accountant.recorder import Recorder  # noqa: E402

TRANSCRIPT = [
    {"uuid": "u0", "parentUuid": None, "timestamp": "2026-07-01T00:00:00.000Z",
     "type": "user", "message": {"role": "user", "content": "go"}},
    # one request across three lines, each repeating the same usage
    *[{"uuid": f"a{i}", "parentUuid": ("u0" if i == 1 else f"a{i-1}"),
       "timestamp": f"2026-07-01T00:00:0{i + 4}.000Z", "type": "assistant",
       "requestId": "req_1", "message": {
           "model": "claude-opus-5",
           "content": ([{"type": "tool_use", "name": "Write",
                         "input": {"file_path": "/w/cover.png"}}] if i == 1 else
                       [{"type": "tool_use", "name": "Read",
                         "input": {"file_path": "/w/other.py"}}] if i == 2 else []),
           "usage": {"input_tokens": 100, "output_tokens": 2000,
                     "cache_read_input_tokens": 500_000,
                     "cache_creation_input_tokens": 40_000,
                     "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                        "ephemeral_1h_input_tokens": 40_000}}}}
      for i in (1, 2, 3)],
    # a request with only a cache_creation total, so the TTL assumption is exercised
    {"uuid": "b1", "parentUuid": "a3", "timestamp": "2026-07-01T00:00:20.000Z",
     "type": "assistant", "requestId": "req_2", "message": {
         "model": "claude-sonnet-4-5", "content": [],
         "usage": {"input_tokens": 5, "output_tokens": 300,
                   "cache_read_input_tokens": 90_000,
                   "cache_creation_input_tokens": 8_000}}},
    # a request on a model with no price at all
    {"uuid": "c1", "parentUuid": "b1", "timestamp": "2026-07-01T00:00:30.000Z",
     "type": "assistant", "requestId": "req_3", "message": {
         "model": "not-a-real-model", "content": [
             {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/notes.md"}}],
         "usage": {"input_tokens": 7, "output_tokens": 11,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
    # an orphan: its parent is not in this file, so latency is genuinely unknown
    {"uuid": "d1", "parentUuid": "not-in-this-file",
     "timestamp": "2026-07-01T00:00:40.000Z", "type": "assistant", "requestId": "req_4",
     "message": {"model": "claude-opus-5", "content": [],
                 "usage": {"input_tokens": 1, "output_tokens": 2,
                           "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0}}},
]


def main() -> int:
    out = []
    try:
        pricing = Pricing.load()
    except PricingError as exc:
        print(f"pricing-refused {exc}")
        return 0

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "probe.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in TRANSCRIPT) + "\n")
        stages = extract_file(p)

    out.append(f"stages {len(stages)}")
    for st in stages:
        out.append(
            f"stage tokens={json.dumps(st.tokens, sort_keys=True)} "
            f"artifacts={len(st.artifacts)} "
            f"latency={'none' if st.latency_seconds is None else f'{st.latency_seconds:.3f}'} "
            f"warnings={len(st.warnings)}")

    att = attribute(stages, pricing, redact=False)
    out.append(f"total {att.total_usd:.8f}")
    out.append(f"direct {att.attributed_direct_usd:.8f} overhead {att.overhead_usd:.8f}")
    out.append(f"unattributable {att.unattributable_overhead_usd:.8f}")
    out.append(f"unpriced tokens={att.unpriced_tokens} models={att.unpriced_models}")
    for r in sorted(att.receipts, key=lambda r: r.artifact):
        out.append(f"receipt {r.artifact} {r.medium} direct={r.usd_direct:.8f} "
                   f"amortised={r.usd_amortised:.8f}")

    led = build_ledger(stages, att, pricing, label="probe")
    out.append(f"reconciled {led['reconciled']}")
    for c in led["reconciliation"]:
        out.append(f"check {c['name']} {c['ok']}")

    # Price one token of each class, so a flattened tier moves the fingerprint.
    for cls in ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"):
        c = pricing.price_tokens("claude-opus-5", {cls: 1_000_000})
        out.append(f"price {cls} {c.usd:.8f} priced={c.priced}")

    # The recorder's answer for a stage too short to sample: None, or something else.
    rec = Recorder()
    with rec.stage("instant", measure_gpu=True):
        pass
    st = rec.stages[0]
    out.append(f"gpu_seconds_is_none {st.gpu_seconds is None} has_note {bool(st.gpu_note)}")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

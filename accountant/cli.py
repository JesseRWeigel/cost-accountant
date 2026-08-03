"""Command line for cost-accountant.

    python3 -m accountant.cli extract  --out data/ledger.json
    python3 -m accountant.cli report   data/ledger.json
    python3 -m accountant.cli receipt  data/ledger.json README.md
    python3 -m accountant.cli gpu
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from .attribute import attribute
from .extract import extract_all, find_transcripts
from .gpu import probe
from .ledger import build_ledger, load_ledger, write_ledger
from .pricing import Pricing, PricingError, usd
from .recorder import Recorder


def _bar(fraction: float, width: int = 24) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "#" * filled + "." * (width - filled)


def cmd_extract(args) -> int:
    pricing = Pricing.load(args.pricing)
    if args.stages_from:
        stages = Recorder.read(args.stages_from)
        source = f"recorded stages from {pathlib.Path(args.stages_from).name}"
    else:
        files = find_transcripts(args.transcripts)
        if not files:
            root = args.transcripts or "~/.claude/projects"
            print(f"no transcripts found under {root}", file=sys.stderr)
            print("Set COST_ACCOUNTANT_TRANSCRIPTS or pass --transcripts to point at a "
                  "directory of Claude Code .jsonl session files.", file=sys.stderr)
            return 2
        stages = list(extract_all(args.transcripts, limit=args.limit))
        source = f"{len(files) if not args.limit else min(args.limit, len(files))} transcripts"
    if not stages:
        print("no stages were extracted; nothing to account for", file=sys.stderr)
        return 2
    if args.only_sessions_touching_root:
        if not args.root:
            print("--only-sessions-touching-root needs --root", file=sys.stderr)
            return 2
        root = pathlib.Path(args.root).resolve()

        def under(p: str) -> bool:
            try:
                pathlib.Path(p).resolve().relative_to(root)
                return True
            except (ValueError, OSError):
                return False

        keep = {st.session for st in stages if any(under(a) for a in st.artifacts)}
        if not keep:
            print(f"no session wrote anything under {root}", file=sys.stderr)
            return 2
        before = len(stages)
        stages = [st for st in stages if st.session in keep]
        source += (f"; narrowed to the {len(keep)} session(s) that wrote under "
                   f"{root.name}, {len(stages)} of {before} stages")
    att = attribute(stages, pricing, root=args.root, redact=not args.no_redact)
    ledger = build_ledger(stages, att, pricing,
                          label=args.label or source,
                          include_stages=args.include_stages)
    if args.out:
        write_ledger(ledger, args.out)
        print(f"wrote {args.out}: {ledger['totals']['stages']:,} stages, "
              f"{ledger['totals']['artifacts']:,} artifacts, "
              f"{usd(ledger['totals']['usd_total'])}")
    else:
        json.dump(ledger, sys.stdout, indent=2)
        print()
    return 0 if ledger["reconciled"] else 1


def cmd_report(args) -> int:
    ledger = load_ledger(args.ledger)
    t = ledger["totals"]
    print(f"cost-accountant report: {ledger.get('label', '')}")
    print(f"  prices from {ledger['pricing']['file']} "
          f"(retrieved {ledger['pricing']['retrieved']}); cache read x"
          f"{ledger['pricing']['cache_multipliers']['read']}, "
          f"write x{ledger['pricing']['cache_multipliers']['write_5m']} / x"
          f"{ledger['pricing']['cache_multipliers']['write_1h']}")
    print()
    print(f"  {t['stages']:,} stages, {t['stages_with_artifacts']:,} of which wrote a file")
    print(f"  {t['artifacts']:,} artifacts")
    print(f"  {t['tokens']['all']:,} tokens: "
          + ", ".join(f"{k} {t['tokens'][k]:,}" for k in
                      ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")))
    print(f"  {usd(t['usd_total'])} total  "
          f"({usd(t['usd_direct'])} attributable directly, "
          f"{usd(t['usd_overhead'])} session overhead)")
    if t["unpriced_tokens"]:
        print(f"  UNPRICED: {t['unpriced_tokens']:,} tokens on models with no price "
              f"({', '.join(t['unpriced_models'])}). The total above excludes them.")
    print()
    print("  cost per artifact by medium (overhead amortised)")
    rows = sorted(ledger["by_medium"].items(), key=lambda kv: -kv[1]["usd_amortised"])
    top = max((v["usd_amortised"] for _, v in rows), default=1.0) or 1.0
    print(f"    {'medium':<8} {'pieces':>7} {'total':>11} {'per piece':>11}  share")
    for medium, v in rows:
        print(f"    {medium:<8} {v['artifacts']:>7,} {usd(v['usd_amortised']):>11} "
              f"{usd(v['usd_per_artifact']):>11}  {_bar(v['usd_amortised'] / top)}")
    print()
    print("  most expensive artifacts")
    for r in ledger["receipts"][:args.top]:
        print(f"    {usd(r['usd_amortised']):>10}  {r['medium']:<6} "
              f"{r['stages']:>4} stages  {r['wall_seconds']/60:>7.1f} min  {r['artifact']}")
    print()
    ok = True
    for c in ledger["reconciliation"]:
        print(f"  [{'ok  ' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
        ok = ok and c["ok"]
    return 0 if ok else 1


def cmd_receipt(args) -> int:
    ledger = load_ledger(args.ledger)
    matches = [r for r in ledger["receipts"] if args.artifact in r["artifact"]]
    if not matches:
        print(f"no artifact in {args.ledger} matches {args.artifact!r}", file=sys.stderr)
        return 2
    for r in matches[:args.limit]:
        print(f"RECEIPT  {r['artifact']}")
        print(f"  medium         {r['medium']}")
        print(f"  stages         {r['stages']} across {len(r['sessions'])} session(s)")
        print(f"  models         {', '.join(r['models']) or 'none recorded'}")
        print(f"  tokens         {r['tokens']['input']:,} in, {r['tokens']['output']:,} out, "
              f"{r['tokens']['cache_read']:,} cache read, "
              f"{r['tokens']['cache_write_5m'] + r['tokens']['cache_write_1h']:,} cache write")
        print(f"  wall clock     {r['wall_seconds']/60:.1f} min of model latency")
        if r.get("gpu_seconds") is not None:
            print(f"  gpu            {r['gpu_seconds']:.2f} GPU-seconds")
        elif r.get("gpu_note"):
            print(f"  gpu            not attributable: {r['gpu_note']}")
        print(f"  direct cost    {usd(r['usd_direct'])}")
        print(f"  with overhead  {usd(r['usd_amortised'])}")
        if r.get("first_touch"):
            print(f"  first / last   {r['first_touch']} -> {r['last_touch']}")
        print()
    return 0


def cmd_gpu(args) -> int:
    r = probe()
    if r.ok:
        print(f"GPU present: {r.utilisation_percent:.0f}% utilised, "
              f"{r.memory_used_mib:.0f} MiB used, {r.compute_processes} compute process(es)")
        if r.compute_processes > 1:
            print("More than one process is on the card, so GPU seconds measured here "
                  "cannot be attributed to a single pipeline.")
        return 0
    print(f"GPU seconds cannot be measured: {r.reason}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cost-accountant", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pricing", default=None, help="path to a pricing.json (default: bundled)")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="build a ledger from transcripts or recorded stages")
    e.add_argument("--transcripts", default=None, help="directory of Claude Code .jsonl files")
    e.add_argument("--stages-from", default=None, help="a JSONL run file written by Recorder")
    e.add_argument("--root", default=None,
                   help="keep only artifacts under this directory, named relative to it")
    e.add_argument("--only-sessions-touching-root", action="store_true",
                   help="keep only stages from sessions that wrote at least one file "
                        "under --root, so the totals describe the work that built it")
    e.add_argument("--no-redact", action="store_true",
                   help="keep absolute artifact paths (do not publish the result)")
    e.add_argument("--limit", type=int, default=None, help="only read the first N transcripts")
    e.add_argument("--include-stages", action="store_true", help="embed per-stage rows")
    e.add_argument("--label", default=None)
    e.add_argument("--out", default=None)
    e.set_defaults(func=cmd_extract)

    r = sub.add_parser("report", help="summarise a ledger")
    r.add_argument("ledger")
    r.add_argument("--top", type=int, default=10)
    r.set_defaults(func=cmd_report)

    c = sub.add_parser("receipt", help="print the receipt for one artifact")
    c.add_argument("ledger")
    c.add_argument("artifact")
    c.add_argument("--limit", type=int, default=5)
    c.set_defaults(func=cmd_receipt)

    g = sub.add_parser("gpu", help="report whether GPU seconds are measurable here")
    g.set_defaults(func=cmd_gpu)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except PricingError as exc:
        print(f"pricing error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

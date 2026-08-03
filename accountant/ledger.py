"""The ledger: serialise stages and receipts, and reconcile the totals two ways.

A cost report is a number nobody can check by eye, so it has to check itself. Two
independent derivations run on every ledger, and a disagreement is a failure rather
than a warning:

  A.  price-then-sum vs sum-then-price
      Cost every stage individually and add up the dollars, then separately add up all
      tokens by (model, class) and price those aggregates once. Same answer or the
      per-stage loop is dropping, duplicating, or misfiling stages.

  B.  attributed + unattributed vs total
      Every dollar either lands on an artifact or is named as overhead. If the two
      sides differ, attribution is leaking or inventing money.

Both compare in cents, not floats, so an accumulated 1e-12 does not fail the build and a
real half-cent gap does.
"""
from __future__ import annotations

import json
import pathlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .attribute import Attribution
from .pricing import Pricing, TOKEN_CLASSES, usd

# Anything above this is a real disagreement. Half a cent, in dollars.
TOLERANCE_USD = 0.005


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"[{'ok  ' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def reconcile(stages: Sequence, attribution: Attribution, pricing: Pricing) -> List[Check]:
    checks: List[Check] = []

    # --- A: price-then-sum vs sum-then-price -------------------------------------
    per_stage = 0.0
    aggregate_tokens: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {k: 0 for k in TOKEN_CLASSES})
    unpriced_per_stage = 0
    for st in stages:
        c = pricing.price_tokens(st.model, st.tokens)
        per_stage += c.usd
        unpriced_per_stage += c.unpriced_tokens
        slot = aggregate_tokens[st.model]
        for cls in TOKEN_CLASSES:
            slot[cls] += int(st.tokens.get(cls, 0) or 0)
    bulk = 0.0
    unpriced_bulk = 0
    for model, toks in aggregate_tokens.items():
        c = pricing.price_tokens(model, toks)
        bulk += c.usd
        unpriced_bulk += c.unpriced_tokens
    gap = abs(per_stage - bulk)
    checks.append(Check(
        "price-then-sum equals sum-then-price",
        gap <= TOLERANCE_USD,
        f"{usd(per_stage)} vs {usd(bulk)} (difference {gap:.6f})",
    ))
    checks.append(Check(
        "unpriced token counts agree between both derivations",
        unpriced_per_stage == unpriced_bulk,
        f"{unpriced_per_stage:,} vs {unpriced_bulk:,} tokens",
    ))

    # --- the link between A and B ------------------------------------------------
    # Without this, A compares two derivations from the same stage list and B compares
    # two derivations from the same attribution, so an attribution built from a
    # DIFFERENT set of stages passes both. A dropped or duplicated stage between the
    # two halves is exactly that shape, and it went unnoticed until a test looked.
    gap0 = abs(per_stage - attribution.total_usd)
    checks.append(Check(
        "the attribution was built from these stages",
        gap0 <= TOLERANCE_USD,
        f"stages price to {usd(per_stage)}, attribution reports {usd(attribution.total_usd)} "
        f"(difference {gap0:.6f})",
    ))

    # --- B: attributed + unattributed vs total -----------------------------------
    attributed = sum(r.usd_amortised for r in attribution.receipts)
    accounted = attributed + attribution.unattributable_overhead_usd
    gap2 = abs(accounted - attribution.total_usd)
    checks.append(Check(
        "attributed plus unattributable equals total",
        gap2 <= TOLERANCE_USD,
        f"{usd(attributed)} + {usd(attribution.unattributable_overhead_usd)} "
        f"= {usd(accounted)} vs total {usd(attribution.total_usd)} (difference {gap2:.6f})",
    ))
    direct_sum = sum(r.usd_direct for r in attribution.receipts)
    gap3 = abs(direct_sum + attribution.overhead_usd - attribution.total_usd)
    checks.append(Check(
        "direct attribution plus overhead equals total",
        gap3 <= TOLERANCE_USD,
        f"{usd(direct_sum)} + {usd(attribution.overhead_usd)} vs {usd(attribution.total_usd)} "
        f"(difference {gap3:.6f})",
    ))

    # --- C: the totals must not be vacuously zero --------------------------------
    checks.append(Check(
        "the ledger is not empty",
        attribution.total_usd > 0 and attribution.stages > 0,
        f"{attribution.stages:,} stages, {usd(attribution.total_usd)}",
    ))
    return checks


def build_ledger(stages: Sequence, attribution: Attribution, pricing: Pricing,
                 label: str = "", include_stages: bool = False) -> dict:
    checks = reconcile(stages, attribution, pricing)
    ledger = {
        "label": label,
        "generated_by": "cost-accountant",
        "pricing": {
            "file": pricing.origin,
            "source": pricing.source,
            "retrieved": pricing.retrieved,
            "cache_multipliers": {
                "read": pricing.mult_read,
                "write_5m": pricing.mult_write_5m,
                "write_1h": pricing.mult_write_1h,
            },
        },
        "totals": {
            "stages": attribution.stages,
            "stages_with_artifacts": attribution.stages_with_artifacts,
            "artifacts": len(attribution.receipts),
            "usd_total": round(attribution.total_usd, 6),
            "usd_direct": round(attribution.attributed_direct_usd, 6),
            "usd_overhead": round(attribution.overhead_usd, 6),
            "usd_unattributable_overhead": round(attribution.unattributable_overhead_usd, 6),
            "unpriced_tokens": attribution.unpriced_tokens,
            "unpriced_models": attribution.unpriced_models,
            "tokens": _token_totals(stages),
            "by_model": _by_model(stages, pricing),
        },
        "by_medium": {k: {kk: (round(vv, 6) if isinstance(vv, float) else vv)
                          for kk, vv in v.items()}
                      for k, v in sorted(attribution.by_medium().items())},
        "receipts": [_round_receipt(r.to_dict()) for r in attribution.receipts],
        "reconciliation": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        "reconciled": all(c.ok for c in checks),
    }
    if include_stages:
        ledger["stages"] = [_stage_dict(s) for s in stages]
    return ledger


def _by_model(stages: Sequence, pricing: Pricing) -> Dict[str, dict]:
    """Per-model token and dollar totals.

    Also records what the same tokens would have cost billed as one undifferentiated
    pile of input tokens, which is the mistake this project is about. Keeping it in the
    ledger means the claim in the README is derivable from the data rather than asserted.
    """
    agg: Dict[str, Dict[str, int]] = defaultdict(lambda: {k: 0 for k in TOKEN_CLASSES})
    counts: Dict[str, int] = defaultdict(int)
    for st in stages:
        counts[st.model] += 1
        for cls in TOKEN_CLASSES:
            agg[st.model][cls] += int(st.tokens.get(cls, 0) or 0)
    out: Dict[str, dict] = {}
    for model, toks in sorted(agg.items()):
        c = pricing.price_tokens(model, toks)
        price = pricing.models.get(model)
        flat = None
        if price is not None:
            non_output = sum(toks[k] for k in TOKEN_CLASSES if k != "output")
            flat = (non_output * price.input + toks["output"] * price.output) / 1_000_000.0
        out[model] = {
            "requests": counts[model],
            "tokens": dict(toks),
            "usd": round(c.usd, 6),
            "priced": c.priced,
            "usd_if_cache_tiers_ignored": round(flat, 6) if flat is not None else None,
        }
    return out


def _token_totals(stages: Sequence) -> Dict[str, int]:
    out = {k: 0 for k in TOKEN_CLASSES}
    for st in stages:
        for k in TOKEN_CLASSES:
            out[k] += int(st.tokens.get(k, 0) or 0)
    out["all"] = sum(out.values())
    return out


def _round_receipt(d: dict) -> dict:
    for k in ("usd_direct", "usd_amortised", "wall_seconds", "gpu_seconds"):
        if isinstance(d.get(k), float):
            d[k] = round(d[k], 6)
    return d


def _stage_dict(st) -> dict:
    return {
        "stage_id": st.stage_id,
        "session": st.session,
        "kind": st.kind,
        "model": st.model,
        "tokens": dict(st.tokens),
        "latency_seconds": (round(st.latency_seconds, 3)
                            if st.latency_seconds is not None else None),
        "latency_note": st.latency_note,
        "gpu_seconds": st.gpu_seconds,
        "gpu_note": st.gpu_note,
        "artifacts": len(st.artifacts),
        "warnings": st.warnings,
    }


def write_ledger(ledger: dict, path) -> pathlib.Path:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=False) + "\n")
    return path


def load_ledger(path) -> dict:
    return json.loads(pathlib.Path(path).read_text())

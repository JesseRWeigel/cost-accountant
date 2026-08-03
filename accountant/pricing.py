"""Turn token counts into dollars, and say so when it cannot.

The whole point of this module is that the four token classes are priced differently:

    input          full price
    cache_read     0.1x the input price
    cache_write_5m 1.25x the input price
    cache_write_1h 2.0x the input price
    output         its own price, usually 5x input

Adding them together and multiplying by one rate is the mistake this exists to prevent.
On a real Claude Code corpus cache reads dominate the token count, so treating them as
ordinary input inflates the bill by roughly an order of magnitude.

The second rule here: an unknown model is never priced at zero. A `Cost` carries a
`priced` flag and the tokens it could not price, and every report that aggregates costs
also aggregates the unpriced tokens so a gap is visible rather than silently absorbed.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

DEFAULT_PRICING = pathlib.Path(__file__).resolve().parent.parent / "pricing.json"

# The token classes an accounted stage can carry. Order matters only for display.
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


class PricingError(ValueError):
    """The pricing file is unusable. Raised rather than defaulting, because a silently
    substituted default price produces a confident wrong number."""


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float


@dataclass
class Cost:
    """The dollar cost of one stage, plus what could not be priced.

    `usd` is only meaningful when `priced` is True. When it is False, `usd` is the
    portion that could be priced (always 0.0 for an unknown model) and
    `unpriced_tokens` says how much was left out. Callers must propagate both.
    """
    usd: float = 0.0
    priced: bool = True
    reason: Optional[str] = None
    unpriced_tokens: int = 0
    by_class: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Pricing:
    """A loaded price list. Construct with `Pricing.load()`."""

    def __init__(self, raw: dict, origin: str = "<memory>"):
        self.origin = origin
        self.raw = raw
        for key in ("cache_multipliers", "models"):
            if key not in raw:
                raise PricingError(f"{origin}: pricing file has no {key!r} section")
        mult = raw["cache_multipliers"]
        try:
            self.mult_read = float(mult["read"])
            self.mult_write_5m = float(mult["write_5m"])
            self.mult_write_1h = float(mult["write_1h"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PricingError(f"{origin}: cache_multipliers is malformed: {exc}") from exc
        # A read multiplier of 1.0 means somebody flattened the cache tiers, which is the
        # exact bug this module exists to prevent. Refuse rather than produce the number.
        if self.mult_read >= 1.0:
            raise PricingError(
                f"{origin}: cache read multiplier is {self.mult_read}, which is not a discount. "
                "Cached input is cheaper than fresh input; a multiplier of 1.0 or more means the "
                "cache tiers were flattened and every total would be wrong."
            )
        if self.mult_write_5m < 1.0 or self.mult_write_1h < 1.0:
            raise PricingError(
                f"{origin}: cache write multipliers must be at least 1.0 "
                f"(got 5m={self.mult_write_5m}, 1h={self.mult_write_1h}); writing to the cache "
                "costs more than a plain input token, never less."
            )
        self.models: Dict[str, ModelPrice] = {}
        for name, entry in raw["models"].items():
            if name.startswith("_"):
                continue
            try:
                self.models[name] = ModelPrice(float(entry["input"]), float(entry["output"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise PricingError(f"{origin}: model {name!r} is malformed: {exc}") from exc
        if not self.models:
            raise PricingError(f"{origin}: no models are priced")
        local = raw.get("local", {})
        self.gpu_dollars_per_hour = float(local.get("gpu_dollars_per_hour", 0.0))
        self.source = raw.get("source", "unspecified")
        self.retrieved = raw.get("retrieved", "unknown")

    @classmethod
    def load(cls, path=None) -> "Pricing":
        path = pathlib.Path(path or DEFAULT_PRICING)
        if not path.exists():
            raise PricingError(f"no pricing file at {path}")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise PricingError(f"{path}: not valid JSON: {exc}") from exc
        return cls(raw, origin=str(path.name))

    def knows(self, model: str) -> bool:
        return model in self.models

    def price_tokens(self, model: str, tokens: Dict[str, int]) -> Cost:
        """Price one stage's token counts. Unknown model -> priced=False, never $0 silently."""
        total = sum(int(tokens.get(k, 0) or 0) for k in TOKEN_CLASSES)
        price = self.models.get(model)
        if price is None:
            return Cost(
                usd=0.0,
                priced=False,
                reason=f"no price for model {model!r} in {self.origin}",
                unpriced_tokens=total,
            )
        per_token_in = price.input / 1_000_000.0
        per_token_out = price.output / 1_000_000.0
        by_class = {
            "input": int(tokens.get("input", 0) or 0) * per_token_in,
            "output": int(tokens.get("output", 0) or 0) * per_token_out,
            "cache_read": int(tokens.get("cache_read", 0) or 0) * per_token_in * self.mult_read,
            "cache_write_5m": int(tokens.get("cache_write_5m", 0) or 0) * per_token_in * self.mult_write_5m,
            "cache_write_1h": int(tokens.get("cache_write_1h", 0) or 0) * per_token_in * self.mult_write_1h,
        }
        return Cost(usd=sum(by_class.values()), priced=True, by_class=by_class)

    def price_gpu_seconds(self, seconds: Optional[float]) -> Cost:
        """Price local GPU time. `None` seconds means it could not be measured, and that
        is reported rather than folded into the total as zero."""
        if seconds is None:
            return Cost(usd=0.0, priced=False, reason="gpu seconds were not measurable")
        return Cost(usd=seconds * self.gpu_dollars_per_hour / 3600.0, priced=True)


def usd(value: float) -> str:
    """Format a dollar amount, keeping sub-cent amounts legible instead of rounding to $0.00."""
    if value and abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"

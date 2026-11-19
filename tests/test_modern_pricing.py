"""Verify the opt-in November profile against the published per-million rates."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from accountant.pricing import Pricing  # noqa: E402

PROFILE = ROOT / "pricing.claude-api-2026-11-09.json"
MODEL_RATES = {
    "claude-sonnet-5-5": (2.0, 10.0, 0.1, 2.5, 4.0),
    "claude-opus-5-5": (4.0, 20.0, 0.2, 5.0, 8.0),
}
# This order follows the independent Nov 9 table above, not the rate-card JSON.
CLASSES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


class TestModernPricing(unittest.TestCase):
    def test_each_token_class_matches_the_published_rates(self):
        profile = Pricing.load(PROFILE)
        self.assertEqual(set(profile.models), set(MODEL_RATES))
        for model, expected in MODEL_RATES.items():
            with self.subTest(model=model):
                cost = profile.price_tokens(model, dict.fromkeys(CLASSES, 1_000_000))
                self.assertTrue(cost.priced)
                for cls, amount in zip(CLASSES, expected):
                    self.assertAlmostEqual(cost.by_class[cls], amount, msg=cls)
                self.assertAlmostEqual(cost.usd, sum(expected))

    def test_profiles_stay_separate_and_inexact_ids_remain_unpriced(self):
        default = Pricing.load()
        modern = Pricing.load(PROFILE)
        reads = {"cache_read": 1_000_000}
        self.assertAlmostEqual(default.price_tokens("claude-opus-5", reads).usd, 0.50)
        self.assertFalse(default.price_tokens("claude-opus-5-5", reads).priced)
        for model in ("claude-opus-5", "opus", "anthropic.claude-opus-5-5"):
            with self.subTest(model=model):
                cost = modern.price_tokens(model, reads)
                self.assertFalse(cost.priced)
                self.assertEqual(cost.unpriced_tokens, 1_000_000)

    def test_cli_ledger_uses_only_the_explicit_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "synthetic.jsonl"
            models = [*MODEL_RATES, "claude-opus-5"]
            log.write_text("".join(json.dumps({
                "stage_id": str(i), "session": "fixture", "kind": "llm",
                "model": model, "tokens": dict.fromkeys(CLASSES, 1_000_000)
            }) + "\n" for i, model in enumerate(models)))
            base = [sys.executable, "-m", "accountant.cli"]
            extract = ["extract", "--stages-from", str(log)]
            ordinary = json.loads(subprocess.check_output(base + extract, cwd=ROOT))
            result = json.loads(subprocess.check_output(
                base + ["--pricing", str(PROFILE)] + extract, cwd=ROOT))
        self.assertTrue(ordinary["reconciled"])
        self.assertTrue(result["reconciled"])
        self.assertAlmostEqual(ordinary["totals"]["usd_total"], 46.75)
        self.assertEqual(ordinary["totals"]["unpriced_tokens"], 10_000_000)
        self.assertIn("effective 2026-11-09", result["pricing"]["source"])
        self.assertEqual(result["totals"]["unpriced_tokens"], 5_000_000)
        self.assertAlmostEqual(result["totals"]["usd_total"], 55.8)
        self.assertAlmostEqual(result["totals"]["by_model"]["claude-sonnet-5-5"]["usd"], 18.6)
        self.assertAlmostEqual(result["totals"]["by_model"]["claude-opus-5-5"]["usd"], 37.2)


if __name__ == "__main__":
    unittest.main()

"""Pricing tests. Every assertion is paired with a control that proves it can fail."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accountant.pricing import Pricing, PricingError, usd  # noqa: E402

BASE = {
    "cache_multipliers": {"read": 0.1, "write_5m": 1.25, "write_1h": 2.0},
    "models": {"m": {"input": 10.0, "output": 50.0}},
}


class TestCacheClassesArePricedDifferently(unittest.TestCase):
    """The headline bug this module exists to prevent."""

    def setUp(self):
        self.p = Pricing(json.loads(json.dumps(BASE)))

    def test_one_million_of_each_class_costs_its_own_rate(self):
        M = 1_000_000
        self.assertAlmostEqual(self.p.price_tokens("m", {"input": M}).usd, 10.0)
        self.assertAlmostEqual(self.p.price_tokens("m", {"output": M}).usd, 50.0)
        self.assertAlmostEqual(self.p.price_tokens("m", {"cache_read": M}).usd, 1.0)
        self.assertAlmostEqual(self.p.price_tokens("m", {"cache_write_5m": M}).usd, 12.5)
        self.assertAlmostEqual(self.p.price_tokens("m", {"cache_write_1h": M}).usd, 20.0)

    def test_negative_control_flattening_the_tiers_changes_the_answer(self):
        """If cache tokens were billed as ordinary input, the number would differ. This
        proves the test above is measuring the distinction rather than a coincidence."""
        M = 1_000_000
        tokens = {"cache_read": M, "cache_write_1h": M}
        correct = self.p.price_tokens("m", tokens).usd
        naive = (M + M) * 10.0 / 1_000_000  # every cache token at the input rate
        self.assertNotAlmostEqual(correct, naive)
        self.assertAlmostEqual(correct, 21.0)
        self.assertAlmostEqual(naive, 20.0)

    def test_a_real_shaped_turn_is_dominated_by_cache_reads(self):
        # The shape of an actual Claude Code turn: tiny fresh input, big cache read.
        real = {"input": 2, "output": 1603, "cache_read": 20853, "cache_write_1h": 12515}
        c = self.p.price_tokens("m", real)
        self.assertTrue(c.priced)
        naive_all_input = (2 + 20853 + 12515) * 10.0 / 1e6 + 1603 * 50.0 / 1e6
        self.assertGreater(abs(c.usd - naive_all_input), 1e-6,
                           "flattening the tiers must move this number")


class TestUnknownModelIsNeverSilentlyFree(unittest.TestCase):
    def setUp(self):
        self.p = Pricing(json.loads(json.dumps(BASE)))

    def test_unknown_model_reports_that_it_could_not_price(self):
        c = self.p.price_tokens("no-such-model", {"input": 500, "output": 100})
        self.assertFalse(c.priced)
        self.assertEqual(c.unpriced_tokens, 600)
        self.assertIn("no-such-model", c.reason)

    def test_negative_control_a_known_model_is_priced(self):
        c = self.p.price_tokens("m", {"input": 500, "output": 100})
        self.assertTrue(c.priced)
        self.assertEqual(c.unpriced_tokens, 0)
        self.assertGreater(c.usd, 0)


class TestMalformedPricingIsRejected(unittest.TestCase):
    def _with(self, **over):
        raw = json.loads(json.dumps(BASE))
        raw["cache_multipliers"].update(over)
        return raw

    def test_flattened_read_multiplier_is_refused(self):
        with self.assertRaises(PricingError) as cm:
            Pricing(self._with(read=1.0))
        self.assertIn("not a discount", str(cm.exception))

    def test_write_multiplier_below_one_is_refused(self):
        with self.assertRaises(PricingError):
            Pricing(self._with(write_5m=0.5))

    def test_negative_control_the_real_multipliers_load(self):
        self.assertIsNotNone(Pricing(self._with()))

    def test_missing_section_is_refused(self):
        with self.assertRaises(PricingError):
            Pricing({"models": {"m": {"input": 1, "output": 2}}})

    def test_empty_model_table_is_refused(self):
        with self.assertRaises(PricingError):
            Pricing({"cache_multipliers": BASE["cache_multipliers"], "models": {}})

    def test_missing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(PricingError):
                Pricing.load(pathlib.Path(d) / "nope.json")


class TestShippedPricingFile(unittest.TestCase):
    def test_it_loads_and_names_its_source(self):
        p = Pricing.load()
        self.assertGreater(len(p.models), 5)
        self.assertNotEqual(p.source, "unspecified")
        self.assertNotEqual(p.retrieved, "unknown")

    def test_opus_and_haiku_are_not_the_same_price(self):
        p = Pricing.load()
        self.assertGreater(p.models["claude-opus-5"].input, p.models["claude-haiku-4-5"].input)

    def test_sub_cent_amounts_stay_legible(self):
        self.assertEqual(usd(0.0), "$0.00")
        self.assertEqual(usd(0.0034), "$0.0034")
        self.assertEqual(usd(12.5), "$12.50")


if __name__ == "__main__":
    unittest.main()

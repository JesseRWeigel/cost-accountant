"""Reconciliation must fail when the numbers are wrong, which is the only way to know
it would notice if they ever were."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accountant.attribute import attribute  # noqa: E402
from accountant.extract import Stage  # noqa: E402
from accountant.ledger import build_ledger, reconcile  # noqa: E402
from accountant.pricing import Pricing  # noqa: E402

PRICING = Pricing({
    "cache_multipliers": {"read": 0.1, "write_5m": 1.25, "write_1h": 2.0},
    "models": {"m": {"input": 10.0, "output": 50.0}, "n": {"input": 1.0, "output": 5.0}},
})


def stage(sid, out, artifacts=(), model="m", session="s"):
    return Stage(stage_id=sid, session=session, model=model,
                 tokens={"input": 1000, "output": out, "cache_read": 40000,
                         "cache_write_5m": 500, "cache_write_1h": 700},
                 latency_seconds=2.0, artifacts=list(artifacts),
                 started="2026-07-01T00:00:00+00:00", ended="2026-07-01T00:00:02+00:00")


def fixture():
    return [
        stage("a", 1000, ["/p/a.py"]),
        stage("b", 2000, ["/p/b.md"], model="n"),
        stage("c", 3000, []),
        stage("d", 400, ["/p/a.py", "/p/c.png"], model="n"),
    ]


class TestReconciliationPassesOnGoodData(unittest.TestCase):
    def test_all_checks_pass(self):
        stages = fixture()
        att = attribute(stages, PRICING, redact=False)
        checks = reconcile(stages, att, PRICING)
        self.assertTrue(all(c.ok for c in checks), [c.line() for c in checks])
        self.assertEqual(len(checks), 6)

    def test_the_ledger_records_that_it_reconciled(self):
        stages = fixture()
        led = build_ledger(stages, attribute(stages, PRICING, redact=False), PRICING)
        self.assertTrue(led["reconciled"])
        self.assertEqual(led["totals"]["artifacts"], 3)


class TestReconciliationCatchesCorruption(unittest.TestCase):
    """Negative controls. Each one breaks a different invariant, and the matching check
    must go red. A reconciliation that stays green here proves nothing."""

    def test_inflating_the_total_breaks_the_attribution_identity(self):
        stages = fixture()
        att = attribute(stages, PRICING, redact=False)
        att.total_usd *= 1.5
        failed = [c.name for c in reconcile(stages, att, PRICING) if not c.ok]
        self.assertIn("attributed plus unattributable equals total", failed)
        self.assertIn("direct attribution plus overhead equals total", failed)

    def test_losing_a_receipt_breaks_the_attribution_identity(self):
        stages = fixture()
        att = attribute(stages, PRICING, redact=False)
        att.receipts.pop()
        failed = [c.name for c in reconcile(stages, att, PRICING) if not c.ok]
        self.assertIn("attributed plus unattributable equals total", failed)

    def test_double_counting_one_stage_breaks_the_two_derivations(self):
        """Feeding reconcile a stage list that no longer matches the attribution is
        exactly what a duplicated or dropped stage would look like."""
        stages = fixture()
        att = attribute(stages, PRICING, redact=False)
        failed = [c.name for c in reconcile(stages + [stages[0]], att, PRICING) if not c.ok]
        self.assertIn("the attribution was built from these stages", failed)

    def test_an_empty_ledger_is_reported_rather_than_passing_vacuously(self):
        att = attribute([], PRICING, redact=False)
        checks = {c.name: c for c in reconcile([], att, PRICING)}
        self.assertFalse(checks["the ledger is not empty"].ok)

    def test_an_unpriced_model_is_visible_in_both_derivations(self):
        stages = fixture() + [stage("e", 5000, ["/p/e.md"], model="unknown-model")]
        att = attribute(stages, PRICING, redact=False)
        checks = {c.name: c for c in reconcile(stages, att, PRICING)}
        self.assertTrue(checks["unpriced token counts agree between both derivations"].ok)
        self.assertGreater(att.unpriced_tokens, 0)
        led = build_ledger(stages, att, PRICING)
        self.assertEqual(led["totals"]["unpriced_models"], ["unknown-model"])


class TestPriceThenSumEqualsSumThenPrice(unittest.TestCase):
    def test_the_two_derivations_agree_across_several_models(self):
        stages = fixture()
        att = attribute(stages, PRICING, redact=False)
        check = {c.name: c for c in reconcile(stages, att, PRICING)}
        self.assertTrue(check["price-then-sum equals sum-then-price"].ok)

    def test_negative_control_a_price_change_moves_the_number(self):
        """If the totals were computed from something other than the price table, this
        would not move, and the agreement above would be meaningless."""
        stages = fixture()
        cheap = Pricing({
            "cache_multipliers": {"read": 0.1, "write_5m": 1.25, "write_1h": 2.0},
            "models": {"m": {"input": 1.0, "output": 5.0}, "n": {"input": 1.0, "output": 5.0}},
        })
        a = attribute(stages, PRICING, redact=False).total_usd
        b = attribute(stages, cheap, redact=False).total_usd
        self.assertGreater(a, b)


if __name__ == "__main__":
    unittest.main()

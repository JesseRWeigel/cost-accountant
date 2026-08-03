"""Attribution and medium classification."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accountant.attribute import attribute, medium_of  # noqa: E402
from accountant.extract import Stage  # noqa: E402
from accountant.pricing import Pricing  # noqa: E402

PRICING = Pricing({
    "cache_multipliers": {"read": 0.1, "write_5m": 1.25, "write_1h": 2.0},
    "models": {"m": {"input": 10.0, "output": 50.0}},
})


def stage(sid, out, artifacts=(), session="s1", model="m", latency=1.0):
    return Stage(stage_id=sid, session=session, model=model,
                 tokens={"input": 0, "output": out, "cache_read": 0,
                         "cache_write_5m": 0, "cache_write_1h": 0},
                 latency_seconds=latency, artifacts=list(artifacts),
                 started="2026-07-01T00:00:00+00:00", ended="2026-07-01T00:00:01+00:00")


class TestMediumClassification(unittest.TestCase):
    def test_each_family_lands_in_its_own_bucket(self):
        cases = {
            "a/b/cover.png": "image", "x.SVG": "image", "loop.wav": "audio",
            "clip.mp4": "video", "index.html": "page", "style.css": "page",
            "d.csv": "data", "s.json": "data", "m.py": "code", "t.tsx": "code",
            "README.md": "text", "notes.txt": "text",
        }
        for path, want in cases.items():
            self.assertEqual(medium_of(path), want, path)

    def test_negative_control_unknown_and_extensionless_are_not_guessed(self):
        self.assertEqual(medium_of("thing.zzz"), "other")
        self.assertEqual(medium_of("Makefile"), "other")


class TestDirectAttributionAndOverhead(unittest.TestCase):
    def setUp(self):
        self.stages = [
            stage("s1", 1_000_000, ["/p/a.py"]),          # $50 -> a.py
            stage("s2", 1_000_000, ["/p/a.py", "/p/b.md"]),  # $50 split evenly
            stage("s3", 2_000_000, []),                   # $100 overhead
        ]
        self.att = attribute(self.stages, PRICING, root=None, redact=False)
        self.by = {r.artifact: r for r in self.att.receipts}

    def test_the_total_is_the_sum_of_the_stages(self):
        self.assertAlmostEqual(self.att.total_usd, 200.0)

    def test_a_multi_file_stage_splits_evenly(self):
        self.assertAlmostEqual(self.by["/p/a.py"].usd_direct, 75.0)
        self.assertAlmostEqual(self.by["/p/b.md"].usd_direct, 25.0)

    def test_a_stage_that_wrote_nothing_becomes_overhead(self):
        self.assertAlmostEqual(self.att.overhead_usd, 100.0)
        self.assertAlmostEqual(self.att.attributed_direct_usd, 100.0)

    def test_overhead_is_amortised_in_proportion_to_direct_cost(self):
        # $100 overhead over a 75/25 split
        self.assertAlmostEqual(self.by["/p/a.py"].usd_amortised, 75.0 + 75.0)
        self.assertAlmostEqual(self.by["/p/b.md"].usd_amortised, 25.0 + 25.0)

    def test_every_dollar_is_accounted_for_under_both_policies(self):
        direct = sum(r.usd_direct for r in self.att.receipts)
        amort = sum(r.usd_amortised for r in self.att.receipts)
        self.assertAlmostEqual(direct + self.att.overhead_usd, self.att.total_usd)
        self.assertAlmostEqual(amort + self.att.unattributable_overhead_usd,
                               self.att.total_usd)

    def test_negative_control_dropping_a_stage_breaks_the_identity(self):
        """Proves the identity above is a real constraint and not arithmetic that
        holds no matter what."""
        short = attribute(self.stages[:2], PRICING, root=None, redact=False)
        self.assertNotAlmostEqual(short.total_usd, self.att.total_usd)


class TestOverheadWithNothingToAttachTo(unittest.TestCase):
    def test_a_session_that_produced_no_artifact_keeps_its_overhead_visible(self):
        att = attribute([stage("x", 1_000_000, [], session="empty")], PRICING, redact=False)
        self.assertAlmostEqual(att.unattributable_overhead_usd, 50.0)
        self.assertEqual(att.receipts, [])
        self.assertAlmostEqual(att.total_usd, 50.0)

    def test_overhead_does_not_cross_session_boundaries(self):
        stages = [
            stage("a", 1_000_000, ["/p/a.py"], session="s1"),
            stage("b", 1_000_000, [], session="s2"),  # s2 wrote nothing
        ]
        att = attribute(stages, PRICING, redact=False)
        by = {r.artifact: r for r in att.receipts}
        self.assertAlmostEqual(by["/p/a.py"].usd_amortised, 50.0)
        self.assertAlmostEqual(att.unattributable_overhead_usd, 50.0)


class TestRedaction(unittest.TestCase):
    def test_redacted_keys_carry_no_directory_and_no_basename(self):
        att = attribute([stage("a", 10, ["/home/someone/private-thing/secret_plan.md"])],
                        PRICING, redact=True)
        key = att.receipts[0].artifact
        self.assertNotIn("home", key)
        self.assertNotIn("private-thing", key)
        self.assertNotIn("secret_plan", key)
        self.assertTrue(key.endswith(".md"))
        self.assertEqual(att.receipts[0].medium, "text")

    def test_negative_control_without_redaction_the_path_is_present(self):
        att = attribute([stage("a", 10, ["/home/someone/private-thing/secret_plan.md"])],
                        PRICING, redact=False)
        self.assertIn("secret_plan", att.receipts[0].artifact)

    def test_a_root_relativises_and_drops_everything_outside_it(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        inside = str(root / "accountant" / "pricing.py")
        att = attribute([stage("a", 10, [inside, "/etc/passwd"])], PRICING, root=root)
        self.assertEqual([r.artifact for r in att.receipts], ["accountant/pricing.py"])


class TestUnpricedModelsPropagate(unittest.TestCase):
    def test_an_unknown_model_is_surfaced_rather_than_billed_as_zero(self):
        att = attribute([stage("a", 5000, ["/p/a.py"], model="mystery-model")],
                        PRICING, redact=False)
        self.assertEqual(att.unpriced_models, ["mystery-model"])
        self.assertEqual(att.unpriced_tokens, 5000)
        self.assertAlmostEqual(att.total_usd, 0.0)

    def test_negative_control_a_known_model_reports_no_gap(self):
        att = attribute([stage("a", 5000, ["/p/a.py"])], PRICING, redact=False)
        self.assertEqual(att.unpriced_models, [])
        self.assertEqual(att.unpriced_tokens, 0)
        self.assertGreater(att.total_usd, 0)


class TestByMedium(unittest.TestCase):
    def test_pieces_and_per_piece_cost_are_grouped_by_medium(self):
        stages = [
            stage("a", 1_000_000, ["/p/one.png"]),
            stage("b", 1_000_000, ["/p/two.png"]),
            stage("c", 2_000_000, ["/p/notes.md"]),
        ]
        by = attribute(stages, PRICING, redact=False).by_medium()
        self.assertEqual(by["image"]["artifacts"], 2)
        self.assertAlmostEqual(by["image"]["usd_per_artifact"], 50.0)
        self.assertAlmostEqual(by["text"]["usd_per_artifact"], 100.0)


if __name__ == "__main__":
    unittest.main()

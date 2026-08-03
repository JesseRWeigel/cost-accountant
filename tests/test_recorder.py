"""The live recorder, including the states where a measurement is not available."""
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accountant.gpu import GpuMeter, probe  # noqa: E402
from accountant.recorder import Recorder  # noqa: E402


class TestStageRecording(unittest.TestCase):
    def test_wall_time_is_measured_and_is_not_zero(self):
        rec = Recorder()
        with rec.stage("sleep", artifact="out/x.png", measure_gpu=False):
            time.sleep(0.05)
        st = rec.stages[0]
        self.assertGreaterEqual(st.latency_seconds, 0.05)
        self.assertLess(st.latency_seconds, 10)
        self.assertEqual(st.artifacts, ["out/x.png"])

    def test_negative_control_a_shorter_stage_measures_shorter(self):
        """Proves the timer reads the clock rather than returning a constant."""
        rec = Recorder()
        with rec.stage("quick", measure_gpu=False):
            pass
        with rec.stage("slow", measure_gpu=False):
            time.sleep(0.08)
        self.assertLess(rec.stages[0].latency_seconds, rec.stages[1].latency_seconds)

    def test_tokens_accumulate_and_unknown_classes_are_rejected(self):
        rec = Recorder()
        with rec.stage("call", model="m", measure_gpu=False) as s:
            s.tokens(input=100, output=20)
            s.tokens(cache_read=9000)
        self.assertEqual(rec.stages[0].tokens["input"], 100)
        self.assertEqual(rec.stages[0].tokens["cache_read"], 9000)
        with self.assertRaises(ValueError):
            with rec.stage("bad", measure_gpu=False) as s:
                s.tokens(prompt_tokens=5)

    def test_a_stage_that_raises_is_still_recorded_and_still_raises(self):
        rec = Recorder()
        with self.assertRaises(RuntimeError):
            with rec.stage("boom", artifact="out/y.png", measure_gpu=False) as s:
                s.tokens(output=500)
                raise RuntimeError("model timed out")
        self.assertEqual(len(rec.stages), 1)
        st = rec.stages[0]
        self.assertEqual(st.tokens["output"], 500, "tokens spent before the failure count")
        self.assertTrue(any("did not complete" in w for w in st.warnings))

    def test_stages_round_trip_through_a_run_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "run.jsonl"
            rec = Recorder(p)
            with rec.stage("a", artifact="out/a.png", measure_gpu=False) as s:
                s.tokens(output=7)
            with rec.stage("b", model="m", measure_gpu=False) as s:
                s.tokens(input=3)
            back = Recorder.read(p)
            self.assertEqual(len(back), 2)
            self.assertEqual(back[0].artifacts, ["out/a.png"])
            self.assertEqual(back[1].tokens["input"], 3)


class TestGpuHonesty(unittest.TestCase):
    """The rule: gpu_seconds is a float only when it is genuinely attributable. It is
    None with a reason otherwise, and never 0.0 standing in for 'do not know'."""

    def test_a_probe_either_succeeds_or_explains_itself(self):
        r = probe()
        if r.ok:
            self.assertGreaterEqual(r.utilisation_percent, 0.0)
            self.assertIsNone(r.reason)
        else:
            self.assertTrue(r.reason)
            self.assertGreater(len(r.reason), 10)

    def test_an_unmeasurable_stage_reports_none_and_a_reason_not_zero(self):
        rec = Recorder()
        with rec.stage("instant", measure_gpu=True):
            pass
        st = rec.stages[0]
        if st.gpu_seconds is None:
            self.assertTrue(st.gpu_note, "None gpu_seconds must carry a reason")
        else:
            self.assertIsInstance(st.gpu_seconds, float)

    def test_a_note_explaining_unavailability_forbids_a_number(self):
        """The tightest form of the rule. If the recorder explains why it could not
        measure, it must not also report a figure, and 0.0 is a figure."""
        rec = Recorder()
        with rec.stage("instant", measure_gpu=True):
            pass
        st = rec.stages[0]
        excuses = ("shorter than one", "cannot be attributed", "not on PATH",
                   "could not be run", "could not parse", "returned no rows", "exited")
        if st.gpu_note and any(x in st.gpu_note for x in excuses):
            self.assertIsNone(
                st.gpu_seconds,
                f"gpu_note says {st.gpu_note!r} but gpu_seconds is {st.gpu_seconds!r}; "
                "an unmeasurable stage must report None, never 0.0")

    def test_gpu_measurement_off_is_distinguishable_from_gpu_unavailable(self):
        rec = Recorder()
        with rec.stage("no-gpu", measure_gpu=False):
            pass
        self.assertIsNone(rec.stages[0].gpu_seconds)
        self.assertIn("not requested", rec.stages[0].gpu_note)

    def test_the_meter_never_raises_and_always_terminates(self):
        with GpuMeter(interval=0.02) as meter:
            time.sleep(0.06)
        self.assertTrue(meter.gpu_seconds is None or isinstance(meter.gpu_seconds, float))
        if meter.gpu_seconds is None:
            self.assertTrue(meter.note)


if __name__ == "__main__":
    unittest.main()

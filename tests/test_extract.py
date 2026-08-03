"""Extractor tests, built on a synthetic transcript that reproduces the real format.

The fixture is written by hand rather than copied from a real session, because real
transcripts carry working directories, file contents and prompts that must not be
committed. Every structural quirk it reproduces was first observed in the real corpus
and is named in the comment above it.
"""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accountant.extract import extract_file, anon, _usage_to_tokens  # noqa: E402


def usage(inp=0, out=0, read=0, w5=0, w1=0, total_creation=None):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": w5 + w1 if total_creation is None else total_creation,
        "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
    }


def line(uuid, parent, ts, *, request=None, model=None, use=None, content=None):
    rec = {"uuid": uuid, "parentUuid": parent, "timestamp": ts, "type": "assistant"}
    if request:
        rec["requestId"] = request
    if use is not None or model is not None:
        rec["message"] = {"model": model or "m", "content": content or [], "usage": use}
    elif content is not None:
        rec["message"] = {"content": content}
    return json.dumps(rec)


def write_tool(path):
    return [{"type": "tool_use", "name": "Write", "input": {"file_path": path, "content": "x"}}]


class TestStreamedTurnsAreDeduplicated(unittest.TestCase):
    """The severe one: every line of a streamed turn repeats the FULL usage object."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "session.jsonl"
        u = usage(inp=2, out=1603, read=20853, w1=12515)
        self.path.write_text("\n".join([
            json.dumps({"uuid": "u0", "parentUuid": None, "timestamp": "2026-07-01T00:00:00.000Z",
                        "type": "user", "message": {"role": "user", "content": "hi"}}),
            # three lines, one request, identical usage on each
            line("a1", "u0", "2026-07-01T00:00:03.000Z", request="req_1", use=u),
            line("a2", "a1", "2026-07-01T00:00:04.000Z", request="req_1", use=u),
            line("a3", "a2", "2026-07-01T00:00:05.000Z", request="req_1", use=u),
        ]) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_lines_collapse_to_one_stage(self):
        stages = extract_file(self.path)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].tokens["output"], 1603)
        self.assertEqual(stages[0].tokens["cache_write_1h"], 12515)

    def test_negative_control_a_naive_line_sum_is_three_times_larger(self):
        """Proves the fixture actually exercises deduplication. If the file did not
        repeat usage, this control would find the same number and prove nothing."""
        naive = 0
        for raw in self.path.read_text().splitlines():
            rec = json.loads(raw)
            u = (rec.get("message") or {}).get("usage")
            if isinstance(u, dict):
                naive += u["output_tokens"]
        deduped = extract_file(self.path)[0].tokens["output"]
        self.assertEqual(naive, 1603 * 3)
        self.assertEqual(deduped, 1603)
        self.assertLess(deduped, naive)


class TestZeroTwinIsNotAllowedToWin(unittest.TestCase):
    """A few requestId groups pair a real usage object with an all-zero one."""

    def _extract(self, order):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.jsonl"
            real = usage(inp=10, out=2904, read=63791, w5=44432)
            zero = usage()
            pair = {"real": real, "zero": zero}
            p.write_text("\n".join(
                line(f"a{i}", None, f"2026-07-01T00:00:0{i}.000Z", request="req_z",
                     use=pair[which]) for i, which in enumerate(order)) + "\n")
            return extract_file(p)[0]

    def test_the_real_numbers_survive_whichever_order_they_arrive_in(self):
        for order in (["real", "zero"], ["zero", "real"]):
            st = self._extract(order)
            self.assertEqual(st.tokens["output"], 2904, f"order {order}")
            self.assertEqual(st.tokens["cache_write_5m"], 44432, f"order {order}")

    def test_negative_control_taking_the_last_line_would_lose_them(self):
        """Shows the field-wise maximum is doing work: last-wins gives zero here."""
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.jsonl"
            p.write_text("\n".join([
                line("a0", None, "2026-07-01T00:00:00.000Z", request="r", use=usage(out=2904)),
                line("a1", "a0", "2026-07-01T00:00:01.000Z", request="r", use=usage()),
            ]) + "\n")
            last_line_usage = json.loads(p.read_text().splitlines()[-1])["message"]["usage"]
            self.assertEqual(last_line_usage["output_tokens"], 0)
            self.assertEqual(extract_file(p)[0].tokens["output"], 2904)


class TestCacheTtlSplit(unittest.TestCase):
    def test_the_five_minute_and_one_hour_halves_stay_separate(self):
        tokens, warns = _usage_to_tokens(usage(w5=100, w1=200))
        self.assertEqual(tokens["cache_write_5m"], 100)
        self.assertEqual(tokens["cache_write_1h"], 200)
        self.assertEqual(warns, [])

    def test_a_missing_split_is_reported_not_guessed_silently(self):
        u = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 500}
        tokens, warns = _usage_to_tokens(u)
        self.assertEqual(tokens["cache_write_5m"], 500)
        self.assertEqual(tokens["cache_write_1h"], 0)
        self.assertTrue(any("TTL split missing" in w for w in warns))

    def test_a_split_that_disagrees_with_the_total_is_reported(self):
        _, warns = _usage_to_tokens(usage(w5=100, w1=200, total_creation=999))
        self.assertTrue(any("does not equal" in w for w in warns))

    def test_negative_control_a_consistent_split_warns_about_nothing(self):
        _, warns = _usage_to_tokens(usage(w5=100, w1=200, total_creation=300))
        self.assertEqual(warns, [])


class TestArtifactsAndLatency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "s.jsonl"
        self.path.write_text("\n".join([
            json.dumps({"uuid": "u0", "parentUuid": None,
                        "timestamp": "2026-07-01T00:00:00.000Z", "type": "user",
                        "message": {"role": "user", "content": "go"}}),
            line("a1", "u0", "2026-07-01T00:00:12.500Z", request="r1", use=usage(out=10),
                 content=write_tool("/tmp/proj/out/cover.png")),
            line("a2", "a1", "2026-07-01T00:00:13.000Z", request="r1", use=usage(out=10),
                 content=[{"type": "tool_use", "name": "Edit",
                           "input": {"file_path": "/tmp/proj/README.md"}}]),
            # a stage with no writes at all: session overhead
            line("b1", "a2", "2026-07-01T00:00:20.000Z", request="r2", use=usage(out=5),
                 content=[{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]),
        ]) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_edit_paths_are_collected_across_the_whole_request(self):
        st = extract_file(self.path)[0]
        self.assertEqual(st.artifacts, ["/tmp/proj/out/cover.png", "/tmp/proj/README.md"])

    def test_negative_control_a_read_only_stage_claims_no_artifact(self):
        stages = extract_file(self.path)
        self.assertEqual(stages[1].artifacts, [])

    def test_latency_runs_from_the_preceding_event_to_the_last_block(self):
        st = extract_file(self.path)[0]
        self.assertAlmostEqual(st.latency_seconds, 13.0, places=3)
        self.assertIsNone(st.latency_note)

    def test_an_unmeasurable_latency_says_so_rather_than_reporting_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.jsonl"
            # the parent uuid is not in this file, so the start time is genuinely unknown
            p.write_text(line("a1", "missing", "2026-07-01T00:00:01.000Z",
                              request="r", use=usage(out=1)) + "\n")
            st = extract_file(p)[0]
            self.assertIsNone(st.latency_seconds)
            self.assertIn("unknown", st.latency_note)


class TestNoPersonalDataLeaks(unittest.TestCase):
    def test_the_session_id_is_hashed_not_the_filename(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "my-private-project-name.jsonl"
            p.write_text(line("a", None, "2026-07-01T00:00:01.000Z",
                              request="r", use=usage(out=1)) + "\n")
            st = extract_file(p)[0]
            self.assertNotIn("my-private-project-name", st.session)
            self.assertNotIn("my-private-project-name", st.stage_id)

    def test_negative_control_the_hash_is_stable_and_distinguishing(self):
        self.assertEqual(anon("abc"), anon("abc"))
        self.assertNotEqual(anon("abc"), anon("abd"))


class TestMalformedInput(unittest.TestCase):
    def test_a_corrupt_line_does_not_abort_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.jsonl"
            p.write_text("{not json\n"
                         + line("a", None, "2026-07-01T00:00:01.000Z",
                                request="r", use=usage(out=7)) + "\n"
                         + "\n")
            stages = extract_file(p)
            self.assertEqual(len(stages), 1)
            self.assertEqual(stages[0].tokens["output"], 7)

    def test_a_line_with_no_request_id_is_kept_rather_than_merged(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.jsonl"
            p.write_text("\n".join([
                line("a", None, "2026-07-01T00:00:01.000Z", use=usage(out=3)),
                line("b", "a", "2026-07-01T00:00:02.000Z", use=usage(out=4)),
            ]) + "\n")
            stages = extract_file(p)
            self.assertEqual(len(stages), 2)
            self.assertEqual(sum(s.tokens["output"] for s in stages), 7)


if __name__ == "__main__":
    unittest.main()


class TestTranscriptDiscovery(unittest.TestCase):
    """Both directory layouts, and the bug where one hid the other."""

    def test_loose_files_and_nested_files_are_both_found(self):
        import tempfile as _tf
        from accountant.extract import find_transcripts
        with _tf.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "loose-a.jsonl").write_text("")
            (root / "loose-b.jsonl").write_text("")
            (root / "sub").mkdir()
            (root / "sub" / "nested.jsonl").write_text("")
            # Subagent transcripts sit two levels down. Discovery that stops at one
            # level misses them, and they are where most of a fleet's tokens go.
            (root / "sub" / "session" / "subagents").mkdir(parents=True)
            (root / "sub" / "session" / "subagents" / "deep.jsonl").write_text("")
            found = {p.name for p in find_transcripts(root)}
            self.assertEqual(
                found, {"loose-a.jsonl", "loose-b.jsonl", "nested.jsonl", "deep.jsonl"})

    def test_negative_control_a_directory_with_no_transcripts_finds_none(self):
        import tempfile as _tf
        from accountant.extract import find_transcripts
        with _tf.TemporaryDirectory() as d:
            (pathlib.Path(d) / "notes.txt").write_text("x")
            self.assertEqual(find_transcripts(pathlib.Path(d)), [])

    def test_a_missing_root_returns_nothing_rather_than_raising(self):
        from accountant.extract import find_transcripts
        self.assertEqual(find_transcripts("/definitely/not/a/real/path/xyz"), [])

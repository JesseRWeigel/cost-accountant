#!/usr/bin/env python3
"""A small real pipeline, instrumented, to exercise the live recorder end to end.

Four stages produce three artifacts in three different media. Wall time is really
measured, GPU seconds are really attempted, and the token counts on the two model stages
are marked as declared rather than measured, because this script does not call an API and
inventing usage numbers would be the exact dishonesty the rest of the project is about.

    python3 scripts/demo_pipeline.py --out /tmp/demo
    python3 -m accountant.cli extract --stages-from /tmp/demo/run.jsonl --no-redact \\
        --root /tmp/demo --out /tmp/demo/ledger.json
"""
from __future__ import annotations

import argparse
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from accountant.recorder import Recorder  # noqa: E402


def render_svg(path: pathlib.Path, n: int = 720) -> None:
    pts = []
    for i in range(n):
        t = i / n * math.tau * 3
        pts.append(f"{160 + 150 * math.cos(t) * math.sin(t * 1.7):.2f},"
                   f"{160 + 150 * math.sin(t) * math.cos(t * 2.3):.2f}")
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 320" width="320" '
        'height="320"><rect width="320" height="320" fill="#14130f"/>'
        f'<polyline fill="none" stroke="#e0955f" stroke-width="1.1" points="{" ".join(pts)}"/>'
        "</svg>\n")


def render_wav(path: pathlib.Path, seconds: float = 1.0, rate: int = 8000) -> None:
    import struct
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            t = i / rate
            v = int(12000 * math.sin(math.tau * 220 * t) * math.exp(-3 * t))
            frames += struct.pack("<h", max(-32768, min(32767, v)))
        w.writeframes(bytes(frames))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="demo-run")
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)
    (out / "work").mkdir(parents=True, exist_ok=True)

    svg = out / "work" / "spirograph.svg"
    wav = out / "work" / "chime.wav"
    md = out / "work" / "caption.md"

    rec = Recorder(out / "run.jsonl", session="demo")

    with rec.stage("render-vector", artifact=str(svg), kind="local"):
        render_svg(svg)

    with rec.stage("render-audio", artifact=str(wav), kind="local"):
        render_wav(wav)

    # The two stages below are marked as declared, not measured. This script does not
    # call an API, so it has no usage object to read; a plausible-looking token count
    # here would be a fabricated bill.
    with rec.stage("caption", artifact=str(md), kind="llm",
                   model="claude-opus-5", measure_gpu=False) as s:
        s.tokens(input=180, output=240, cache_read=48_000, cache_write_5m=2_400)
        s.note("token counts are declared by the caller, not measured from an API response")
        time.sleep(0.05)
        md.write_text("# Spirograph\n\nA Lissajous-modulated spirograph and a decaying "
                      "220 Hz chime, produced by an instrumented demo pipeline.\n")

    with rec.stage("review", kind="llm", model="claude-opus-5", measure_gpu=False) as s:
        # No artifact: this is session overhead, which is the interesting case.
        s.tokens(input=40, output=95, cache_read=51_000)
        s.note("token counts are declared by the caller, not measured from an API response")
        time.sleep(0.02)

    total_wall = sum(st.latency_seconds or 0 for st in rec.stages)
    print(f"{len(rec.stages)} stages, {total_wall:.3f}s wall clock, "
          f"run file at {out / 'run.jsonl'}")
    for st in rec.stages:
        gpu = (f"{st.gpu_seconds:.3f} GPU-s" if st.gpu_seconds is not None
               else f"gpu: {st.gpu_note}")
        print(f"  {st.stage_id:<18} {st.latency_seconds:6.3f}s  {gpu}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

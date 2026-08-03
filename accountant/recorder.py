"""Instrument a live pipeline so its stages land in the same ledger as the transcripts.

The extractor accounts for work that already happened. This accounts for work as it
happens, and both produce `Stage` records, so a pipeline that mixes a hosted model, a
local model, and a plain rendering step ends up with one comparable bill.

    from accountant.recorder import Recorder

    rec = Recorder("runs/today.jsonl")
    with rec.stage("render", artifact="out/cover.png") as s:
        draw()
    with rec.stage("caption", artifact="out/cover.png",
                   model="claude-opus-5") as s:
        s.tokens(input=1200, output=340, cache_read=88000)

Wall time is measured with `time.monotonic`, which does not run backwards when the
system clock is adjusted. GPU seconds come from `gpu.GpuMeter`, which reports None with
a reason rather than 0.0 when it cannot attribute them.

A stage that raises is still recorded, marked failed, because a step that burned tokens
and then crashed still cost money and hiding it understates the bill.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import pathlib
import time
from typing import Dict, Iterator, List, Optional

from .extract import Stage
from .gpu import GpuMeter
from .pricing import TOKEN_CLASSES


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


class StageHandle:
    """Handed to the body of a `with rec.stage(...)` block."""

    def __init__(self, stage: Stage):
        self._stage = stage

    def tokens(self, **counts: int) -> "StageHandle":
        for k, v in counts.items():
            if k not in TOKEN_CLASSES:
                raise ValueError(
                    f"unknown token class {k!r}; expected one of {', '.join(TOKEN_CLASSES)}")
            self._stage.tokens[k] = self._stage.tokens.get(k, 0) + int(v)
        return self

    def artifact(self, path: str) -> "StageHandle":
        if path not in self._stage.artifacts:
            self._stage.artifacts.append(path)
        return self

    def note(self, text: str) -> "StageHandle":
        self._stage.warnings.append(text)
        return self


class Recorder:
    """Collects stages in memory and, optionally, appends them to a JSONL run file."""

    def __init__(self, path=None, session: str = "live"):
        self.path = pathlib.Path(path) if path else None
        self.session = session
        self.stages: List[Stage] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def stage(self, name: str, artifact: Optional[str] = None, model: str = "",
              kind: str = "local", measure_gpu: bool = True) -> Iterator[StageHandle]:
        st = Stage(
            stage_id=f"{name}:{len(self.stages)}",
            session=self.session,
            kind=kind,
            model=model,
            tokens={k: 0 for k in TOKEN_CLASSES},
            started=_now(),
        )
        if artifact:
            st.artifacts.append(artifact)
        handle = StageHandle(st)
        t0 = time.monotonic()
        meter = GpuMeter() if measure_gpu else None
        ctx = meter if meter is not None else contextlib.nullcontext()
        failed = None
        try:
            with ctx:
                yield handle
        except Exception as exc:  # recorded, then re-raised
            failed = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            st.latency_seconds = time.monotonic() - t0
            st.ended = _now()
            if meter is not None:
                st.gpu_seconds = meter.gpu_seconds
                st.gpu_note = meter.note
            else:
                st.gpu_note = "GPU measurement was not requested for this stage"
            if failed:
                st.warnings.append(f"stage raised and did not complete: {failed}")
            self.stages.append(st)
            if self.path:
                with self.path.open("a") as fh:
                    fh.write(json.dumps(self._encode(st)) + "\n")

    @staticmethod
    def _encode(st: Stage) -> dict:
        return {
            "stage_id": st.stage_id, "session": st.session, "kind": st.kind,
            "model": st.model, "tokens": st.tokens, "started": st.started,
            "ended": st.ended, "latency_seconds": st.latency_seconds,
            "gpu_seconds": st.gpu_seconds, "gpu_note": st.gpu_note,
            "artifacts": st.artifacts, "warnings": st.warnings,
        }

    @staticmethod
    def read(path) -> List[Stage]:
        out: List[Stage] = []
        for line in pathlib.Path(path).read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            out.append(Stage(
                stage_id=d["stage_id"], session=d["session"], kind=d.get("kind", "local"),
                model=d.get("model", ""), tokens=d.get("tokens", {}),
                started=d.get("started"), ended=d.get("ended"),
                latency_seconds=d.get("latency_seconds"), gpu_seconds=d.get("gpu_seconds"),
                gpu_note=d.get("gpu_note"), artifacts=d.get("artifacts", []),
                warnings=d.get("warnings", []),
            ))
        return out

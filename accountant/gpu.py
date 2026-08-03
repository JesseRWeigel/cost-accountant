"""Measure GPU seconds, and refuse to make one up.

There are three distinct states here and collapsing any two of them is how an advisory
tool starts lying:

    measured     nvidia-smi is present, and this process was the only compute client on
                 the device for the whole stage, so device utilisation is ours.
    shared       nvidia-smi is present but other processes held GPU memory, so device
                 utilisation includes their work. We report the device-wide figure and
                 leave the attributed figure as None with a reason.
    unavailable  no nvidia-smi, or it failed. gpu_seconds is None with a reason.

`gpu_seconds` is None in the last two cases. It is never 0.0, because 0.0 is a claim
that the GPU did nothing, and we do not know that.

GPU seconds are the integral of utilisation over wall time: a stage of 10 s at 40%
utilisation is 4 GPU-seconds. Sampling is at a fixed interval, so short stages carry
real quantisation error, and `samples` is reported so a reader can judge it.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

SAMPLE_INTERVAL = 0.25


@dataclass
class GpuReading:
    ok: bool
    reason: Optional[str] = None
    utilisation_percent: float = 0.0
    memory_used_mib: float = 0.0
    compute_processes: int = 0


def probe() -> GpuReading:
    """One instantaneous reading. Never raises."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return GpuReading(False, "nvidia-smi is not on PATH, so GPU time cannot be measured")
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return GpuReading(False, f"nvidia-smi could not be run: {exc}")
    if out.returncode != 0:
        return GpuReading(False, f"nvidia-smi exited {out.returncode}: {out.stderr.strip()[:120]}")
    line = out.stdout.strip().splitlines()
    if not line:
        return GpuReading(False, "nvidia-smi returned no rows")
    try:
        util_s, mem_s = [p.strip() for p in line[0].split(",")[:2]]
        util, mem = float(util_s), float(mem_s)
    except (ValueError, IndexError):
        return GpuReading(False, f"could not parse nvidia-smi output: {line[0][:120]!r}")
    procs = 0
    try:
        p = subprocess.run(
            [exe, "--query-compute-apps=pid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=False)
        if p.returncode == 0:
            procs = len([x for x in p.stdout.splitlines() if x.strip()])
    except (OSError, subprocess.SubprocessError):
        procs = 0
    return GpuReading(True, None, util, mem, procs)


class GpuMeter:
    """Sample GPU utilisation in a background thread for the duration of a stage.

        with GpuMeter() as meter:
            do_work()
        meter.gpu_seconds     # float, or None if it could not be attributed
        meter.note            # why it is None, when it is
    """

    def __init__(self, interval: float = SAMPLE_INTERVAL):
        self.interval = interval
        self.samples: List[float] = []
        self.gpu_seconds: Optional[float] = None
        self.device_gpu_seconds: Optional[float] = None
        self.note: Optional[str] = None
        self.max_other_processes = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = 0.0
        self._first: Optional[GpuReading] = None

    def __enter__(self) -> "GpuMeter":
        self._first = probe()
        self._t0 = time.monotonic()
        if not self._first.ok:
            self.note = self._first.reason
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            r = probe()
            if r.ok:
                self.samples.append(r.utilisation_percent)
                self.max_other_processes = max(self.max_other_processes, r.compute_processes)

    def __exit__(self, *exc) -> None:
        elapsed = time.monotonic() - self._t0
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 4)
        if self._first is None or not self._first.ok:
            return
        if not self.samples:
            self.note = (
                f"the stage finished in {elapsed:.2f}s, shorter than one "
                f"{self.interval}s sample, so no utilisation was observed"
            )
            return
        mean_util = sum(self.samples) / len(self.samples) / 100.0
        self.device_gpu_seconds = mean_util * elapsed
        # Per-process utilisation is not exposed by nvidia-smi on every platform (it is
        # absent under WSL2), so with any other compute client on the card we cannot say
        # which share of the device figure was ours.
        if self.max_other_processes > 1:
            self.note = (
                f"{self.max_other_processes} compute processes shared the GPU during this "
                f"stage, so the device-wide {self.device_gpu_seconds:.2f} GPU-seconds "
                "cannot be attributed to this process alone"
            )
            self.gpu_seconds = None
        else:
            self.gpu_seconds = self.device_gpu_seconds

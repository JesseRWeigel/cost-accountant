"""Recover real, already-incurred cost and latency from Claude Code transcripts.

Claude Code writes one JSONL file per session under `~/.claude/projects/<encoded-cwd>/`.
Assistant lines carry `message.usage`, which is the same usage object the API returns.
That is real billing data for work that actually happened, which makes it a far better
input to a cost accountant than any synthetic benchmark.

Three things about the format cost real accuracy here, and the first is severe:

1.  **One API request produces several assistant lines, each repeating the FULL usage
    object.** A streamed turn is split across lines (one per content block) and every
    line carries the same `usage`. Summing over lines double counts. On a 691-file
    corpus, 23,486 usage-bearing lines collapse to 11,142 distinct requests, so a naive
    sum overstates the bill by roughly 2.1x. Deduplication is by `requestId`.

2.  A handful of duplicate `requestId` groups contain one real usage object and one
    all-zero object. Taking the first, or the last, is a coin flip. We take the maximum
    of each field across the group, which is stable regardless of file order.

3.  `cache_creation_input_tokens` is a total, and it hides the split that matters:
    `cache_creation.ephemeral_5m_input_tokens` costs 1.25x input while
    `ephemeral_1h_input_tokens` costs 2x. We read the split, and when only the total is
    present we say so rather than guessing.

Nothing from a transcript is written to disk by this module. `Stage.artifacts` holds
absolute paths while in memory; the ledger writer relativises or hashes them.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional

# Tools whose invocation means "this turn produced or changed an artifact".
WRITING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "create_file", "str_replace"}

DEFAULT_TRANSCRIPT_ROOT = pathlib.Path.home() / ".claude" / "projects"


def _iso(ts: str) -> Optional[_dt.datetime]:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def anon(value: str, length: int = 10) -> str:
    """A stable, non-reversible id for something we must not publish verbatim."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:length]


@dataclass
class Stage:
    """One accounted unit of work: a single API request, or one local pipeline step."""
    stage_id: str
    session: str
    kind: str = "llm"
    model: str = ""
    tokens: Dict[str, int] = field(default_factory=dict)
    started: Optional[str] = None
    ended: Optional[str] = None
    latency_seconds: Optional[float] = None
    latency_note: Optional[str] = None
    gpu_seconds: Optional[float] = None
    gpu_note: Optional[str] = None
    artifacts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def total_tokens(self) -> int:
        return sum(int(v or 0) for v in self.tokens.values())


def _usage_to_tokens(usage: dict) -> tuple[Dict[str, int], List[str]]:
    """Map an API usage object onto the five priced token classes."""
    warnings: List[str] = []
    creation = usage.get("cache_creation") or {}
    total_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    if isinstance(creation, dict) and ("ephemeral_5m_input_tokens" in creation
                                       or "ephemeral_1h_input_tokens" in creation):
        w5 = int(creation.get("ephemeral_5m_input_tokens", 0) or 0)
        w1 = int(creation.get("ephemeral_1h_input_tokens", 0) or 0)
        if w5 + w1 != total_creation:
            # Do not silently prefer one over the other. Keep the split (it is the
            # priced quantity) and record that the numbers disagree.
            warnings.append(
                f"cache_creation split {w5}+{w1} does not equal "
                f"cache_creation_input_tokens {total_creation}"
            )
    else:
        # Only the total is present. Charging it all at the 5-minute rate is the cheaper
        # of the two possibilities, so this can understate. Flag it rather than hide it.
        w5, w1 = total_creation, 0
        if total_creation:
            warnings.append("cache_creation TTL split missing; assumed 5-minute rate")
    tokens = {
        "input": int(usage.get("input_tokens", 0) or 0),
        "output": int(usage.get("output_tokens", 0) or 0),
        "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_write_5m": w5,
        "cache_write_1h": w1,
    }
    return tokens, warnings


def _artifacts_from_content(content) -> List[str]:
    out: List[str] = []
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in WRITING_TOOLS:
            continue
        inp = block.get("input")
        if not isinstance(inp, dict):
            continue
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
        if isinstance(path, str) and path.strip():
            out.append(path)
    return out


def extract_file(path: pathlib.Path) -> List[Stage]:
    """Parse one transcript into deduplicated stages."""
    session = anon(path.stem)
    # requestId -> accumulating stage
    groups: Dict[str, Stage] = {}
    order: List[str] = []
    # uuid -> timestamp, for turning a parent link into a latency measurement
    seen_at: Dict[str, str] = {}
    parent_of: Dict[str, Optional[str]] = {}
    first_line_of: Dict[str, str] = {}

    with path.open("r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            uuid = rec.get("uuid")
            ts = rec.get("timestamp")
            if isinstance(uuid, str) and isinstance(ts, str):
                seen_at[uuid] = ts
                parent_of[uuid] = rec.get("parentUuid")
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            # requestId is the identity of the API call. Without one, fall back to the
            # line's own uuid so the record is kept rather than dropped or merged.
            rid = rec.get("requestId") or f"noreq:{uuid}"
            tokens, warns = _usage_to_tokens(usage)
            stage = groups.get(rid)
            if stage is None:
                stage = Stage(
                    stage_id=anon(f"{path.stem}:{rid}", 12),
                    session=session,
                    model=msg.get("model") or "",
                    tokens=dict(tokens),
                    started=ts,
                    ended=ts,
                )
                stage.warnings.extend(warns)
                groups[rid] = stage
                order.append(rid)
                if isinstance(uuid, str):
                    first_line_of[rid] = uuid
            else:
                # Same request, another content block. Take the field-wise maximum: the
                # duplicates are normally identical, and where they are not, one member
                # of the pair is an all-zero record.
                for k, v in tokens.items():
                    if v > stage.tokens.get(k, 0):
                        stage.tokens[k] = v
                for w in warns:
                    if w not in stage.warnings:
                        stage.warnings.append(w)
                if ts:
                    if not stage.started or ts < stage.started:
                        stage.started = ts
                    if not stage.ended or ts > stage.ended:
                        stage.ended = ts
            stage.artifacts.extend(_artifacts_from_content(msg.get("content")))

    stages = []
    for rid in order:
        stage = groups[rid]
        # Latency: from the conversation event that preceded this response to the last
        # content block of the response. That is the wall-clock the user waited.
        head = first_line_of.get(rid)
        parent = parent_of.get(head) if head else None
        p_ts = seen_at.get(parent) if parent else None
        a, b = _iso(p_ts) if p_ts else None, _iso(stage.ended) if stage.ended else None
        if a and b and b >= a:
            stage.latency_seconds = (b - a).total_seconds()
            stage.started = p_ts
        else:
            stage.latency_note = (
                "no preceding event in this transcript, so the request's start time is unknown"
            )
        # de-duplicate artifact list, preserve order
        seen = set()
        stage.artifacts = [p for p in stage.artifacts if not (p in seen or seen.add(p))]
        stages.append(stage)
    return stages


def find_transcripts(root=None) -> List[pathlib.Path]:
    root = pathlib.Path(root or os.environ.get("COST_ACCOUNTANT_TRANSCRIPTS")
                        or DEFAULT_TRANSCRIPT_ROOT)
    if not root.exists():
        return []
    # Recursive, because there are at least three layouts and a fixed depth misses one:
    #   ~/.claude/projects/<encoded-cwd>/<session>.jsonl        main sessions
    #   ~/.claude/projects/<encoded-cwd>/<session>/subagents/*.jsonl   subagent sessions
    #   <a single project directory>/<session>.jsonl            when pointed at one
    # An earlier version globbed `*/*.jsonl` and fell back to `*.jsonl`, which found the
    # first layout, missed every subagent transcript, and on a project directory
    # containing any subdirectory returned only the nested file. Subagents are where most
    # of a fleet's tokens are spent, so that omission understated the bill by a wide
    # margin rather than by a rounding error.
    return sorted(root.rglob("*.jsonl"))


def extract_all(root=None, limit: Optional[int] = None) -> Iterator[Stage]:
    files = find_transcripts(root)
    if limit:
        files = files[:limit]
    for f in files:
        yield from extract_file(f)

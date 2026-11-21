"""Attribute stage costs to the artifacts they produced, and classify by medium.

An API request does not come with a bill of materials. Deciding which artifact a given
request paid for is a policy, not a fact, so this module makes the policy explicit and
reports what each one leaves over:

  direct     A stage that wrote or edited files is charged to those files, split evenly.
             A stage that touched no file is session overhead and stays unattributed.
             Honest, and on a real corpus it accounts for well under half the spend.

  amortised  Session overhead is spread across that session's artifacts in proportion to
             their direct cost. A session that produced no artifact keeps its overhead in
             the unattributable bucket, because there is nothing to spread it onto.

Both policies must reconcile: attributed + unattributed == total, to the cent. That check
lives in ledger.py and is wired into verify.

The medium taxonomy is deliberately coarse. The question the catalog task asks is "cost
per published piece by medium", and image / audio / video / page / code / data / text is
the granularity at which that comparison means anything.
"""
from __future__ import annotations

import pathlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .pricing import Pricing, TOKEN_CLASSES

MEDIA_BY_EXT = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".ico", ".avif"},
    "audio": {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".mid", ".midi", ".opus"},
    "video": {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"},
    "page": {".html", ".htm", ".css", ".xhtml"},
    "data": {".json", ".jsonl", ".csv", ".tsv", ".toml", ".yaml", ".yml", ".xml", ".sqlite", ".db", ".parquet"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".rs", ".go", ".c", ".h", ".cpp",
             ".hpp", ".java", ".kt", ".rb", ".php", ".lua", ".sql", ".mjs", ".cjs", ".vue", ".swift",
             ".zig", ".ex", ".exs", ".pl", ".r", ".scala", ".dart", ".ipynb"},
    "text": {".md", ".txt", ".rst", ".org", ".tex", ".adoc"},
}
_EXT_TO_MEDIUM = {ext: medium for medium, exts in MEDIA_BY_EXT.items() for ext in exts}


def medium_of(path: str) -> str:
    """Classify one path. Unknown extensions get their own bucket rather than a guess."""
    ext = pathlib.PurePosixPath(str(path).replace("\\", "/")).suffix.lower()
    if not ext:
        return "other"
    return _EXT_TO_MEDIUM.get(ext, "other")


@dataclass
class Receipt:
    """What one artifact cost to produce."""
    artifact: str
    medium: str
    stages: int = 0
    tokens: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in TOKEN_CLASSES})
    usd_direct: float = 0.0
    usd_amortised: float = 0.0
    unpriced_tokens: int = 0
    wall_seconds: float = 0.0
    gpu_seconds: Optional[float] = None
    gpu_note: Optional[str] = None
    first_touch: Optional[str] = None
    last_touch: Optional[str] = None
    models: List[str] = field(default_factory=list)
    sessions: List[str] = field(default_factory=list)

    def total_tokens(self) -> int:
        return sum(self.tokens.values())

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["tokens"] = dict(self.tokens)
        d["models"] = sorted(self.models)
        d["sessions"] = sorted(self.sessions)
        return d


@dataclass
class Attribution:
    receipts: List[Receipt]
    total_usd: float = 0.0
    attributed_direct_usd: float = 0.0
    overhead_usd: float = 0.0
    unattributable_overhead_usd: float = 0.0
    unpriced_tokens: int = 0
    unpriced_models: List[str] = field(default_factory=list)
    stages: int = 0
    stages_with_artifacts: int = 0

    def by_medium(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for r in self.receipts:
            slot = out.setdefault(r.medium, {
                "artifacts": 0, "stages": 0, "usd_direct": 0.0, "usd_amortised": 0.0,
                "tokens": 0, "wall_seconds": 0.0,
            })
            slot["artifacts"] += 1
            slot["stages"] += r.stages
            slot["usd_direct"] += r.usd_direct
            slot["usd_amortised"] += r.usd_amortised
            slot["tokens"] += r.total_tokens()
            slot["wall_seconds"] += r.wall_seconds
        for slot in out.values():
            slot["usd_per_artifact"] = slot["usd_amortised"] / slot["artifacts"] if slot["artifacts"] else 0.0
        return out


def _key(path: str, root: Optional[pathlib.Path], redact: bool) -> Optional[str]:
    p = pathlib.Path(path)
    if root is not None:
        try:
            rel = p.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return None
        return rel.as_posix()
    if redact:
        from .extract import anon
        return f"{medium_of(path)}/{anon(str(p))}{p.suffix.lower()}"
    return str(p)


def attribute(stages: Iterable, pricing: Pricing, root=None, redact: bool = True) -> Attribution:
    """Cost every stage, then split it across the artifacts it touched.

    `root`: keep only artifacts under this directory, named relative to it. Used to
    produce honest, readable receipts for a repository you own.
    `redact`: with no root, replace absolute paths with `<medium>/<hash><ext>` so a
    published ledger carries no home directory or private project name.
    """
    root = pathlib.Path(root) if root else None
    receipts: Dict[str, Receipt] = {}
    # per session: direct cost per artifact, and overhead
    session_direct: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    session_overhead: Dict[str, float] = defaultdict(float)

    total = 0.0
    direct_total = 0.0
    overhead_total = 0.0
    unpriced_tokens = 0
    unpriced_models: set = set()
    n_stages = 0
    n_with_artifacts = 0

    for st in stages:
        n_stages += 1
        cost = pricing.price_tokens(st.model, st.tokens)
        if not cost.priced:
            unpriced_tokens += cost.unpriced_tokens
            unpriced_models.add(st.model or "<empty>")
        total += cost.usd

        keys = []
        for a in st.artifacts:
            k = _key(a, root, redact)
            if k is not None:
                keys.append(k)
        # de-duplicate while preserving order
        seen = set()
        keys = [k for k in keys if not (k in seen or seen.add(k))]

        if not keys:
            overhead_total += cost.usd
            session_overhead[st.session] += cost.usd
            continue

        n_with_artifacts += 1
        share = cost.usd / len(keys)
        # Split indivisible tokens without dropping the remainder. Use the
        # receipt key order rather than the order of tool events for ties.
        token_parts = {cls: divmod(int(st.tokens.get(cls, 0) or 0), len(keys))
                       for cls in TOKEN_CLASSES}
        token_rank = {k: i for i, k in enumerate(sorted(keys))}
        wall_share = (st.latency_seconds or 0.0) / len(keys)
        for k in keys:
            r = receipts.get(k)
            if r is None:
                r = Receipt(artifact=k, medium=medium_of(k))
                receipts[k] = r
            r.stages += 1
            r.usd_direct += share
            for cls, (whole, remainder) in token_parts.items():
                tokens_here = whole + (token_rank[k] < remainder)
                r.tokens[cls] += tokens_here
                if not cost.priced:
                    r.unpriced_tokens += tokens_here
            r.wall_seconds += wall_share
            if st.gpu_seconds is not None:
                r.gpu_seconds = (r.gpu_seconds or 0.0) + st.gpu_seconds / len(keys)
            elif st.gpu_note and not r.gpu_note:
                r.gpu_note = st.gpu_note
            if st.started and (r.first_touch is None or st.started < r.first_touch):
                r.first_touch = st.started
            if st.ended and (r.last_touch is None or st.ended > r.last_touch):
                r.last_touch = st.ended
            if st.model and st.model not in r.models:
                r.models.append(st.model)
            if st.session not in r.sessions:
                r.sessions.append(st.session)
            session_direct[st.session][k] += share
        direct_total += cost.usd

    # Amortise session overhead onto that session's artifacts, proportional to direct cost.
    unattributable = 0.0
    for session, oh in session_overhead.items():
        direct_here = session_direct.get(session)
        base = sum(direct_here.values()) if direct_here else 0.0
        if not direct_here or base <= 0:
            unattributable += oh
            continue
        for k, d in direct_here.items():
            receipts[k].usd_amortised += oh * (d / base)
    for k, r in receipts.items():
        r.usd_amortised += r.usd_direct

    out = Attribution(
        receipts=sorted(receipts.values(), key=lambda r: -r.usd_amortised),
        total_usd=total,
        attributed_direct_usd=direct_total,
        overhead_usd=overhead_total,
        unattributable_overhead_usd=unattributable,
        unpriced_tokens=unpriced_tokens,
        unpriced_models=sorted(unpriced_models),
        stages=n_stages,
        stages_with_artifacts=n_with_artifacts,
    )
    return out

"""Chunk-quality metrics for the Markdown + structured chunkers.

Complements ``tools/chunking_smoke.py`` (orphan + heading-inversion focus) with
five additional metrics tuned for tracking fence-split and size-distribution
regressions across PRs:

    1. orphan_ratio         — % of chunks with est_tokens < min_chunk_tokens
    2. fence_split_ratio    — % of chunks whose content has an unbalanced ``` count
    3. json_oversize_ratio  — % of JSON chunks with est_tokens > max_chunk_tokens
    4. json_key_orphan      — % of JSON chunks with est_tokens < min_chunk_tokens
    5. size_percentiles     — p10 / p50 / p90 / p99 of chunk token counts

Run from repo root:
    uv run python tools/chunk_metrics.py
    uv run python tools/chunk_metrics.py --out /tmp/chunk_metrics.json
    uv run python tools/chunk_metrics.py --paths docs packages/memtomem/tests/fixtures/corpus_v2
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.chunking.restructured_text import ReStructuredTextChunker
from memtomem.chunking.structured import StructuredChunker
from memtomem.indexing.engine import _estimate_tokens, _merge_short_chunks
from memtomem.models import Chunk

REPO_ROOT = Path(__file__).resolve().parent.parent

# User-facing docs only. Retrieval-eval fixtures under
# ``packages/memtomem/tests/fixtures/`` are intentionally composed of short,
# atomic semantic units (postmortem / incident notes) and would skew orphan
# ratios as if the chunker were underperforming. Pass them explicitly via
# ``--paths`` when measuring the eval corpus itself.
DEFAULT_CORPUS = [
    Path("docs"),
    Path("README.md"),
    Path("CONTRIBUTING.md"),
    Path("CHANGELOG.md"),
    Path("CLAUDE.md"),
    Path("SECURITY.md"),
    Path("CLA.md"),
    Path("packages/memtomem/README.md"),
    Path("examples/notebooks/README.md"),
]

MIN_TOKENS = 128
MAX_TOKENS = 512
TARGET_TOKENS = 384

FENCE_LINE_RE = re.compile(r"^(?:```|~~~)", re.MULTILINE)


@dataclass
class Bucket:
    """Per-extension tally of counts + token sizes."""

    count: int = 0
    tokens: list[int] = field(default_factory=list)
    orphans: int = 0
    oversize: int = 0
    unbalanced_fence: int = 0
    contains_fence: int = 0


@dataclass
class Metrics:
    files_scanned: int = 0
    files_unsupported: int = 0
    pre_merge_total: int = 0
    post_merge_total: int = 0
    post_merge_by_ext: dict[str, Bucket] = field(default_factory=lambda: defaultdict(Bucket))
    pre_merge_by_ext: dict[str, Bucket] = field(default_factory=lambda: defaultdict(Bucket))

    def to_report(self) -> dict[str, Any]:
        def bucket_report(buckets: dict[str, Bucket]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            all_tokens: list[int] = []
            for ext, b in sorted(buckets.items()):
                if not b.count:
                    continue
                tokens = b.tokens
                all_tokens.extend(tokens)
                out[ext] = {
                    "count": b.count,
                    "orphan_ratio": round(b.orphans / b.count, 4),
                    "oversize_ratio": round(b.oversize / b.count, 4),
                    "contains_fence_ratio": round(b.contains_fence / b.count, 4),
                    "unbalanced_fence_ratio": round(
                        b.unbalanced_fence / max(1, b.contains_fence), 4
                    ),
                    "fence_split_vs_total": round(b.unbalanced_fence / b.count, 4),
                    "tokens": _describe(tokens),
                }
            if all_tokens:
                out["__all__"] = {"count": len(all_tokens), "tokens": _describe(all_tokens)}
            return out

        return {
            "files_scanned": self.files_scanned,
            "files_unsupported": self.files_unsupported,
            "pre_merge_total": self.pre_merge_total,
            "post_merge_total": self.post_merge_total,
            "post_merge_by_ext": bucket_report(self.post_merge_by_ext),
            "pre_merge_by_ext": bucket_report(self.pre_merge_by_ext),
        }


def _describe(tokens: list[int]) -> dict[str, Any]:
    tokens = sorted(tokens)
    if not tokens:
        return {}
    quantiles = statistics.quantiles(tokens, n=100) if len(tokens) >= 100 else []

    def pct(p: int) -> int:
        if quantiles:
            return int(quantiles[p - 1])
        k = max(0, min(len(tokens) - 1, round((p / 100) * (len(tokens) - 1))))
        return tokens[k]

    return {
        "min": tokens[0],
        "p10": pct(10),
        "p50": pct(50),
        "p90": pct(90),
        "p99": pct(99),
        "max": tokens[-1],
        "mean": round(statistics.mean(tokens), 1),
    }


def _collect_files(paths: list[Path], supported: set[str]) -> list[Path]:
    """Walk *paths* (files or dirs) and return all files with supported extensions."""
    out: list[Path] = []
    for p in paths:
        if not p.exists():
            print(f"WARN: path not found: {p}", file=sys.stderr)
            continue
        if p.is_file():
            out.append(p)
        else:
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix in supported:
                    out.append(child)
    return out


def _bucket_for(ext: str, buckets: dict[str, Bucket]) -> Bucket:
    return buckets[ext]


def _tally(chunk: Chunk, ext: str, buckets: dict[str, Bucket]) -> None:
    tokens = _estimate_tokens(chunk.content)
    b = _bucket_for(ext, buckets)
    b.count += 1
    b.tokens.append(tokens)
    if tokens < MIN_TOKENS:
        b.orphans += 1
    if tokens > MAX_TOKENS:
        b.oversize += 1
    fence_markers = FENCE_LINE_RE.findall(chunk.content)
    if fence_markers:
        b.contains_fence += 1
        if len(fence_markers) % 2 == 1:
            b.unbalanced_fence += 1


def run(paths: list[Path], out: Path | None) -> dict[str, Any]:
    registry = ChunkerRegistry(
        [
            MarkdownChunker(),
            StructuredChunker(),
            ReStructuredTextChunker(),
        ]
    )
    supported = set(registry.supported_extensions())
    files = _collect_files(paths, supported)
    metrics = Metrics()

    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            print(f"SKIP {f}: {exc}", file=sys.stderr)
            continue
        pre_chunks = registry.chunk_file(f, content)
        if not pre_chunks:
            metrics.files_unsupported += 1
            continue
        metrics.files_scanned += 1
        metrics.pre_merge_total += len(pre_chunks)
        post_chunks = _merge_short_chunks(pre_chunks, MIN_TOKENS, MAX_TOKENS, TARGET_TOKENS)
        metrics.post_merge_total += len(post_chunks)

        ext = f.suffix
        for c in pre_chunks:
            _tally(c, ext, metrics.pre_merge_by_ext)
        for c in post_chunks:
            _tally(c, ext, metrics.post_merge_by_ext)

    report = metrics.to_report()
    if out:
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"metrics written to {out}")
    return report


def _print_human(report: dict[str, Any]) -> None:
    print(
        f"\nFiles scanned: {report['files_scanned']}"
        f" (unsupported/empty: {report['files_unsupported']})"
    )
    print(f"Pre-merge chunks:  {report['pre_merge_total']}")
    print(f"Post-merge chunks: {report['post_merge_total']}")
    for label in ("post_merge_by_ext", "pre_merge_by_ext"):
        print(f"\n=== {label} ===")
        for ext, stats in report[label].items():
            if ext == "__all__":
                continue
            t = stats["tokens"]
            print(
                f"  {ext:<10} n={stats['count']:<4} "
                f"orphan={stats['orphan_ratio']:.1%}  "
                f"oversize={stats['oversize_ratio']:.1%}  "
                f"fence_split/total={stats['fence_split_vs_total']:.1%}  "
                f"tokens p10/50/90/99={t['p10']}/{t['p50']}/{t['p90']}/{t['p99']}"
            )
        if "__all__" in report[label]:
            t = report[label]["__all__"]["tokens"]
            print(
                f"  ALL        n={report[label]['__all__']['count']:<4} "
                f"tokens p10/50/90/99={t['p10']}/{t['p50']}/{t['p90']}/{t['p99']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths",
        nargs="+",
        type=Path,
        default=None,
        help="Paths (files or dirs) to chunk. Defaults to self-corpus.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write full JSON report to this path.",
    )
    args = parser.parse_args()
    paths = args.paths or [REPO_ROOT / p for p in DEFAULT_CORPUS]
    report = run(paths, args.out)
    _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

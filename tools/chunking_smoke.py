"""Smoke report for the markdown chunking + merge pipeline.

Runs ``MarkdownChunker`` + ``_merge_short_chunks`` on every ``.md`` file in a
directory and prints aggregate stats, including heading-inversion pair counts.
Useful when tuning chunk post-processing (see c4a0005 / e35ba03) without
needing to spin up the full index engine.

Usage (from repo root)::

    uv run python tools/chunking_smoke.py <corpus_dir> [label]
"""

from __future__ import annotations

import sys
from pathlib import Path

from memtomem.chunking.markdown import MarkdownChunker
from memtomem.config import IndexingConfig
from memtomem.indexing.engine import _estimate_tokens, _merge_short_chunks


def _heading_level(heading: str) -> int:
    stripped = heading.lstrip()
    level = 0
    for c in stripped:
        if c == "#":
            level += 1
        else:
            break
    if not 1 <= level <= 6 or len(stripped) <= level or stripped[level] != " ":
        return 0
    return level


def run(corpus_dir: Path, label: str) -> None:
    cfg = IndexingConfig()
    chunker = MarkdownChunker(indexing_config=cfg)
    files = sorted(corpus_dir.glob("*.md"))

    raw_total = 0
    merged_total = 0
    short_after = 0
    over_ceiling_after = 0
    reduced_files = 0
    inversion_orphans = 0

    for f in files:
        content = f.read_text(encoding="utf-8")
        raw = chunker.chunk_file(f, content)
        if not raw:
            continue

        # Count adjacent short-chunk heading-inversion pairs in the raw output
        # (pre-merge signal — post-merge the engine folds them forward).
        for i in range(len(raw) - 1):
            ch = raw[i].metadata.heading_hierarchy
            nh = raw[i + 1].metadata.heading_hierarchy
            if not ch or not nh:
                continue
            cl, nl = _heading_level(ch[0]), _heading_level(nh[0])
            if cl and nl and cl > nl and _estimate_tokens(raw[i].content) < cfg.min_chunk_tokens:
                inversion_orphans += 1

        merged = _merge_short_chunks(
            raw,
            cfg.min_chunk_tokens,
            cfg.max_chunk_tokens,
            cfg.target_chunk_tokens,
        )
        raw_total += len(raw)
        merged_total += len(merged)
        if len(merged) < len(raw):
            reduced_files += 1

        for c in merged:
            t = _estimate_tokens(c.content)
            if t < cfg.min_chunk_tokens:
                short_after += 1
            if t > cfg.max_chunk_tokens:
                over_ceiling_after += 1

    print(f"=== {label} ({len(files)} files) ===")
    print(f"  raw chunks:       {raw_total}")
    print(f"  after merge:      {merged_total}  (reduction: {raw_total - merged_total})")
    print(f"  files reduced:    {reduced_files}")
    print(f"  short (<min):     {short_after}")
    print(f"  over max:         {over_ceiling_after}")
    print(f"  inversion pairs:  {inversion_orphans}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    run(Path(sys.argv[1]).expanduser(), sys.argv[2] if len(sys.argv) > 2 else "run")

"""Explicit reviewed full-block masking, bound to one exact source snapshot.

No value-boundary guessing and no automatic enablement. A changed source,
malformed manifest, unlisted path, or residual scanner hit retains the original
normal guard input. Source text is never written. Manifest spans are one-based,
inclusive physical lines selected after reviewing the enclosing parsed block.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from memtomem import privacy
from memtomem.indexing.privacy_projection import IndexProjection


def prepare_reviewed_projection(
    path: Path, content: str, manifest_path: str, *, scope: str = "user"
) -> IndexProjection:
    original = IndexProjection(content, content)
    if scope == "project_shared":
        return original
    try:
        manifest = json.loads(Path(manifest_path).expanduser().read_text())
        if manifest["version"] != 1:
            return original
        entry = manifest["sources"].get(str(path.resolve()))
        if not entry or entry["source_sha256"] != hashlib.sha256(content.encode()).hexdigest():
            return original
        lines = content.splitlines(keepends=True)
        # This format names LF-delimited source lines only.
        if any(line.rstrip("\n").find("\r") >= 0 for line in lines):
            return original
        spans = entry["spans"]
        last = 0
        for start, end in spans:
            if (
                type(start) is not int
                or type(end) is not int
                or not (last < start <= end <= len(lines))
            ):
                return original
            for i in range(start - 1, end):
                suffix = "\n" if lines[i].endswith("\n") else ""
                lines[i] = ("# [REDACTED]" if i == start - 1 else "") + suffix
            last = end
        if not spans:
            return original
        projected = "".join(lines)
        if privacy.scan(projected):
            return original
        return IndexProjection(projected, projected, len(spans))
    except (OSError, ValueError, KeyError, TypeError):
        return original

"""Vendor override resolution — reads project tree only (ADR-0008 Invariant 1).

Fan-out modules call :func:`resolve` per-vendor before writing to the
runtime target. When the resolver returns a path, the caller MUST
byte-copy that file to the vendor target and skip auto-conversion for
that vendor (Invariant 4: full-file replacement).

The resolver intentionally takes only ``project_root`` — never the wiki —
to enforce Invariant 1: ``mm context install`` already copytreed the
wiki's ``overrides/`` subdir into the project, so fan-out never needs
the wiki at sync time. CI machines, archived projects, and machines
without ``~/.memtomem-wiki/`` all run fan-out unchanged.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.context._names import OVERRIDE_FORMATS

__all__ = ["resolve"]

# PR-C ships override resolution for skills only. Agents and commands
# carry an ``OVERRIDE_FORMATS`` row each so the matrix is shipped whole,
# but the resolver skips them until a follow-up PR opens the surface
# (drop this gate to activate). Tests pin the gate explicitly so the
# enable-day diff is a one-line revert.
_PR_C_ACTIVE_TYPES = frozenset({"skills"})


def resolve(
    project_root: Path,
    asset_type: str,
    name: str,
    vendor: str,
) -> Path | None:
    """Returns project's overrides/<vendor>.<ext> if exists, else None.

    Reads ONLY from project tree. ADR-0008 Invariant 1.
    """
    if asset_type not in _PR_C_ACTIVE_TYPES:
        return None
    fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
    if fmt is None:
        return None
    _, ext = fmt
    candidate = project_root / ".memtomem" / asset_type / name / "overrides" / f"{vendor}.{ext}"
    return candidate if candidate.is_file() else None

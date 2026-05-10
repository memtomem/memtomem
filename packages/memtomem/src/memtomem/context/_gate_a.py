"""ADR-0011 PR-E2 — Gate A error formatting helpers shared across extract paths.

Centralises the user-facing error message format for ``project_shared`` Gate A
hard-abort so ``extract_agents_to_canonical``, ``extract_skills_to_canonical``,
and ``extract_commands_to_canonical`` cannot drift on the wording. The message
deliberately echoes only the hit count and source path — never the matched
bytes themselves (``feedback_force_unsafe_redaction_valve_only.md`` and the
``RedactionHit`` docstring on the privacy module both pin the
"never echo secrets" contract).
"""

from __future__ import annotations

from pathlib import Path

from memtomem.config import TargetScope


def format_project_shared_block_message(
    src: Path,
    *,
    hits_count: int,
    scope: TargetScope,
    kind: str,
    imported_so_far: int = 0,
) -> str:
    """User-facing ``ClickException`` message for project_shared Gate A hard-abort.

    Args:
        src: Source file (or skill directory's offending file) that hit Gate A.
        hits_count: Number of pattern hits — count only, never echo bytes.
        scope: The destination scope. Always ``"project_shared"`` in practice;
            other scopes never invoke this helper.
        kind: Singular noun for the artifact kind ("agent", "skill", "command").
        imported_so_far: Files already imported in this run (clean ones that
            passed Gate A before this hit). Surface for cleanup hint.

    Returns:
        A multi-line string suitable for ``raise click.ClickException(...)``.
    """
    tail = (
        f"\n  {imported_so_far} clean {kind}(s) already imported in this run "
        f"remain in canonical — review or remove manually."
        if imported_so_far > 0
        else ""
    )
    return (
        f"Gate A: {src.name} contains {hits_count} privacy pattern hit(s); "
        f"import to scope='{scope}' rejected. git history is forever — "
        f"no force bypass available for project_shared (ADR-0011 §5).\n"
        f"  Retry with --scope=user or --scope=project_local, or remove the "
        f"secret from {src} first.{tail}"
    )

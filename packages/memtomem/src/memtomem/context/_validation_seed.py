"""First-run validation seeder for ADR-0026 §Validation.

Seeds a project on disk so the Context Gateway Overview surfaces all six
user-test affordances at once, giving a moderator a reproducible first-run
state to drive the lightweight 5-6 participant test described in ADR-0026
§Validation.

This module ships in the wheel (it lives under ``src/`` rather than
``tests/fixtures/``) so a naive participant who only ``pip install``-ed
memtomem can reproduce the state with one command: ``mm context
seed-validation <dir>`` (a hidden QA helper in ``cli/context_cmd.py``). The
guard test ``tests/test_ctx_validation_harness.py`` imports this same function,
so the seeder and its rot-guard stay in lockstep.

The six affordances (one per §Validation probe family):

============  ===================================================  ============
affordance    on-disk recipe                                       Overview tile
============  ===================================================  ============
out of sync   canonical in every runtime; only Claude differs      skills
not imported  runtime-only artifact, no canonical                  commands
empty type    nothing seeded for the type                          agents
mcp orphan    ``.mcp.json`` entry with no canonical definition     mcp_servers
parse error   malformed *canonical* mcp-server json (valid         mcp_servers
              ``.mcp.json``)
in sync       canonical byte-identical in every runtime            skills
============  ===================================================  ============

The two skills are seeded into *every* skill runtime (not just Claude) so the
only drift rows are the intended ones — a Claude-only seed would emit a spurious
``missing target`` row per skill per other runtime and bury the signal.

Direction matters and is easy to invert: the **Store** is ``.memtomem/`` and
**runtimes** are ``.claude/`` etc. A runtime-only artifact reads as "Not yet
imported" (pull into the Store); a Store artifact with a stale/missing runtime
copy reads as "Out of sync" (push to the runtime). ``test_ctx_validation_harness``
pins both directions against the real diff engine so a future edit cannot
silently swap them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memtomem.config import TargetScope
from memtomem.context._atomic import atomic_write_bytes, atomic_write_text
from memtomem.context._canonical_txn import canonical_sidecar_lock
from memtomem.context._runtime_targets import runtime_fanout_root
from memtomem.context.mcp_servers import CANONICAL_MCP_SERVER_ROOT, PROJECT_MCP_CONFIG
from memtomem.context.scope_resolver import ArtifactKind
from memtomem.context.skills import CANONICAL_SKILL_ROOT, SKILL_GENERATORS, SKILL_MANIFEST

# Names chosen to read naturally under the §Validation framing task
# ("get this project's skills into Claude Code").
SKILL_OUT_OF_SYNC = "code-review"
SKILL_IN_SYNC = "commit-helper"
COMMAND_NOT_IMPORTED = "summarize"
MCP_ORPHAN = "orphan-server"
MCP_PARSE_ERROR = "broken-server"

# The Claude runtime is the one named in the framing task; deriving its fan-out
# root from the production table (rather than hard-coding ".claude/skills")
# means the seeder follows any future relocation of the runtime layout.
_RUNTIME = "claude"
_SCOPE: TargetScope = "project_shared"


def _write_lf(path: Path, content: str) -> None:
    """Write fixtures with sync-style LF bytes on every platform.

    Byte-for-byte LF (never CRLF) so the in-sync baseline compares equal to its
    runtime copy on Windows too — mirrors ``_write_text_lf`` in the web route
    tests. Routed through ``atomic_write_bytes`` (not a bare ``write_bytes``)
    because this module lives under ``context/``, the gateway atomic-write
    surface (``test_no_bare_writes_on_gateway_surfaces``); the helper also
    creates parent dirs. ``0o644`` matches the runtime-readable convention of
    ``copy_tree_atomic``.
    """
    atomic_write_bytes(path, content.encode("utf-8"), mode=0o644)


def _runtime_root(artifact: ArtifactKind, project_root: Path) -> Path:
    root = runtime_fanout_root(artifact, _RUNTIME, _SCOPE, project_root)
    if root is None:  # pragma: no cover - claude has a project_shared fan-out
        raise RuntimeError(
            f"no {_RUNTIME} fan-out root for {artifact!r}; the runtime table changed"
        )
    return root


def _skill_runtime_roots(project_root: Path) -> dict[str, Path]:
    """Every runtime ``diff_skills`` fans skills out to, keyed by runtime name.

    Seeding a canonical skill into *all* of these (not just Claude) is what
    keeps the diff to a single intended row: a Claude-only copy would leave the
    other runtimes empty and emit a spurious ``missing target`` row per skill
    per runtime, drowning the out-of-sync / in-sync signal the probes rely on.
    Derived from ``SKILL_GENERATORS`` so it tracks ``diff_skills`` exactly.
    """
    roots: dict[str, Path] = {}
    for gen_name in SKILL_GENERATORS:
        runtime = gen_name.split("_", 1)[0]
        root = runtime_fanout_root("skills", runtime, _SCOPE, project_root)
        if root is not None:
            roots[runtime] = root
    return roots


def seed_adr0026_validation_states(project_root: Path) -> dict[str, Any]:
    """Seed the six ADR-0026 §Validation affordances under ``project_root``.

    Idempotent (overwrites in place). Returns a manifest describing what was
    written and the per-tile Overview verdict each state should produce, so the
    guard test and the moderator runbook can assert against a single source.
    """
    project_root = Path(project_root)
    project_root.mkdir(parents=True, exist_ok=True)

    # A project-root marker so ``mm`` / ``_find_project_root`` treat the seeded
    # directory as a project (cheapest marker, mirrors the CLI e2e seeder).
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        atomic_write_text(
            pyproject,
            '[project]\nname = "adr0026-firstrun-fixture"\nversion = "0.0.0"\n',
            mode=0o644,
        )

    skill_canon = project_root / CANONICAL_SKILL_ROOT
    mcp_canon = project_root / CANONICAL_MCP_SERVER_ROOT
    skill_runtimes = _skill_runtime_roots(project_root)
    cmd_runtime = _runtime_root("commands", project_root)

    # (a) OUT OF SYNC — the canonical exists in EVERY skill runtime, but only the
    # Claude copy diverges. Seeding all runtimes (not just Claude) keeps the diff
    # to a single "out of sync" row instead of one spurious "missing target" row
    # per skill per other runtime, which would otherwise dominate the tile.
    review_canonical = (
        f"# {SKILL_OUT_OF_SYNC}\n\nStore version (v2): reviews diffs for correctness.\n"
    )
    review_claude_stale = f"# {SKILL_OUT_OF_SYNC}\n\nRuntime version (v1): older copy in Claude.\n"
    # ADR-0030 §6: this first-party seeder writes canonical Store skills and can
    # target a live project via ``mm context validate-seed --force``, so its
    # canonical writes take the name-keyed lock like every other first-party
    # writer. The runtime-fan-out writes below are runtime targets, not
    # canonicals, and stay unlocked.
    with canonical_sidecar_lock(skill_canon, SKILL_OUT_OF_SYNC):
        _write_lf(skill_canon / SKILL_OUT_OF_SYNC / SKILL_MANIFEST, review_canonical)
    for runtime, root in skill_runtimes.items():
        body = review_claude_stale if runtime == _RUNTIME else review_canonical
        _write_lf(root / SKILL_OUT_OF_SYNC / SKILL_MANIFEST, body)

    # (f) IN SYNC — canonical byte-identical in every skill runtime (baseline +
    # the P5 "already synced, what does Sync do to it?" item).
    in_sync_body = f"# {SKILL_IN_SYNC}\n\nWrites Conventional Commit messages.\n"
    with canonical_sidecar_lock(skill_canon, SKILL_IN_SYNC):
        _write_lf(skill_canon / SKILL_IN_SYNC / SKILL_MANIFEST, in_sync_body)
    for root in skill_runtimes.values():
        _write_lf(root / SKILL_IN_SYNC / SKILL_MANIFEST, in_sync_body)

    # (b) NOT YET IMPORTED — runtime-only command, no canonical in the Store.
    _write_lf(
        cmd_runtime / f"{COMMAND_NOT_IMPORTED}.md",
        f"# {COMMAND_NOT_IMPORTED}\n\nRuntime-only command not yet in the Store.\n",
    )

    # (c) EMPTY TYPE — agents: seed nothing. (left intentionally empty)

    # (d) MCP ORPHAN + (e) PARSE ERROR both live on the mcp_servers tile.
    # The .mcp.json itself MUST stay valid JSON: a broken .mcp.json poisons
    # EVERY canonical mcp row as "parse error" and would destroy the orphan.
    # The parse error is a malformed *canonical* definition instead.
    _write_lf(
        project_root / PROJECT_MCP_CONFIG,
        json.dumps(
            {"mcpServers": {MCP_ORPHAN: {"command": "python", "args": ["-m", "orphan"]}}},
            indent=2,
        )
        + "\n",
    )
    # Malformed canonical JSON (truncated object) -> McpServerParseError.
    _write_lf(mcp_canon / f"{MCP_PARSE_ERROR}.json", '{"command": ')

    return {
        "project_root": str(project_root),
        "runtime": _RUNTIME,
        "framing_task": "get this project's skills into Claude Code",
        "states": {
            "out_of_sync": {
                "tile": "skills",
                "name": SKILL_OUT_OF_SYNC,
                "diff_status": "out of sync",
                "verdict": "needs_sync",
                "action": "Sync",
            },
            "not_yet_imported": {
                "tile": "commands",
                "name": COMMAND_NOT_IMPORTED,
                "diff_status": "missing canonical",
                "verdict": "not_saved",
                "action": "Import",
            },
            "empty_type": {"tile": "agents", "diff_status": None, "verdict": "empty"},
            "mcp_orphan": {
                "tile": "mcp_servers",
                "name": MCP_ORPHAN,
                "diff_status": "missing canonical",
                "action": "Manage",
            },
            "parse_error": {
                "tile": "mcp_servers",
                "name": MCP_PARSE_ERROR,
                "diff_status": "parse error",
                "verdict": "attention",
                "action": "Manage",
            },
            "in_sync": {
                "tile": "skills",
                "name": SKILL_IN_SYNC,
                "diff_status": "in sync",
            },
        },
    }

"""Pin the shared CLI and MCP migration preview text."""

from pathlib import Path

import pytest

from memtomem.cli.context_bundle_cmd import _migrate_action_label, _summarize_migrate_rows
from memtomem.context.migrate import MigrateRow, MigrateState


def _row(state: MigrateState, *, dir_exists: bool = False) -> MigrateRow:
    return MigrateRow(
        asset_type="agents",
        name="example",
        flat_path=Path("example.md"),
        dir_path=Path("example"),
        flat_exists=True,
        dir_exists=dir_exists,
        has_lock_entry=True,
        flat_dirty=None,
        state=state,
        reason="",
    )


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        pytest.param([], "0 actions", id="empty"),
        pytest.param(["noop"], "1 no-op", id="noop"),
        pytest.param(["migrate", "migrate"], "2 ready", id="count-ready"),
        pytest.param(["cleanup_flat"], "1 cleanup (flat+dir)", id="cleanup"),
        pytest.param(
            ["noop", "migrate", "refuse_dirty"],
            "1 ready, 1 need --force, 1 no-op",
            id="ready-force-noop",
        ),
        pytest.param(
            ["skip_orphan", "skip_orphan", "skip_manual"],
            "1 skip (manual), 2 skip (orphan)",
            id="group-skips",
        ),
        pytest.param(
            ["noop", "skip_orphan", "skip_manual", "refuse_dirty", "cleanup_flat", "migrate"],
            "1 ready, 1 cleanup (flat+dir), 1 need --force, 1 skip (manual), 1 skip (orphan), 1 no-op",
            id="fixed-order",
        ),
    ],
)
def test_summarize_migrate_rows(states: list[MigrateState], expected: str) -> None:
    assert _summarize_migrate_rows([_row(state) for state in states]) == expected


@pytest.mark.parametrize(
    ("state", "without_directory", "with_directory"),
    [
        ("migrate", "flat → dir", "flat → dir"),
        ("noop", "no-op", "no-op"),
        ("cleanup_flat", "flat+dir collision", "flat+dir collision"),
        ("refuse_dirty", "flat → dir", "flat+dir collision"),
        ("skip_manual", "skip (manual)", "skip (manual)"),
        ("skip_orphan", "skip (orphan)", "skip (orphan)"),
    ],
)
def test_migrate_action_label(
    state: MigrateState, without_directory: str, with_directory: str
) -> None:
    assert _migrate_action_label(_row(state)) == without_directory
    assert _migrate_action_label(_row(state, dir_exists=True)) == with_directory

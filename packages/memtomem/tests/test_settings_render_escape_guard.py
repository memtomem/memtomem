"""The settings CLI renderers never emit a raw control character.

#2477/#2478 closed a class of bug by hand — settings-derived text printed to a
terminal without ``scrub_text`` — and every review round found another site the
previous sweep had missed. A static check over the renderers' source only moved
that enumeration from call sites to syntax shapes (``.format``, ``%``, a bare
``click.echo(x)``, ``a or b``), so this module observes output instead.

Each registered renderer is driven through its branches with fake settings
objects whose every text field carries a control character tagged with the
field's name. The test then reads what was actually written and asserts three
things together:

* no unprintable character reached the output, however the string was built;
* every field the scenario expects to be shown DID reach the output, in its
  escaped form. Without this half a branch that never ran, or a field that was
  silently dropped, would pass for free;
* text only the named branch prints is present, because a field can also be
  printed by an earlier branch in the same run (a preview before a prompt).

What this cannot see is a branch no scenario drives. That gap is closed from
the other side: :func:`test_every_settings_function_is_classified` requires a
new settings-named function in the CLI to be registered, and
:func:`test_every_renderer_has_a_scenario` requires every registered renderer to
be driven here.

Paths and planner messages are poisoned as well as settings content: a
directory name can carry control characters too. Credentials are a separate
axis, pinned where they are redacted, in ``test_settings_text_scrubbing.py``.

The poison is BEL (``\\x07``), not an ANSI sequence. With colour off — the
``CliRunner`` default — Click strips CSI sequences such as ``\\x1b[7m`` from
everything it echoes, so a CSI poison that leaked unescaped would vanish before
this test could see it. A real terminal gets no such stripping. BEL survives
Click, so "no unprintable character in the output" is a check that can fail.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from memtomem.cli import context_cmd
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.settings import MalformedHookMatcher
from memtomem.context.settings_copy import HookCopyPlan, HookCopyResult, _sync_followup_command
from memtomem.context.settings_doctor import (
    DuplicateTier,
    HookSignature,
    UnportableHookCommand,
    UnscannedSettingsFile,
)
from memtomem.context.settings_migrate import MigrateMove, MigratePlan, MigrateResult

CLI_SOURCE = Path(context_cmd.__file__)

#: CLI functions that print settings-derived text.
RENDERERS: frozenset[str] = frozenset(
    {
        "_print_settings_detect",
        "_confirm_settings_host_writes",
        "_print_duplicate_tier_warnings",
        "_print_settings_generate",
        "_print_settings_diff",
        "settings_doctor_cmd",
        "_print_migrate_plan_human",
        "settings_migrate_cmd",
        "_print_hook_copy_plan",
        "_print_hook_copy_result",
        "settings_copy_cmd",
    }
)

#: Settings-named functions that are deliberately not renderers, with why.
NOT_RENDERERS: dict[str, str] = {
    "_hook_copy_payload": "builds the --json payload, which keeps raw values by contract",
}

#: A new CLI function whose name matches this must be classified above.
_SETTINGS_NAME = re.compile(r"settings|hook_copy|duplicate_tier|migrate_plan")

BEL = "\x07"


def poison(field: str) -> str:
    """Text for ``field`` carrying a control character, tagged so a miss is named."""
    return f"<{field}{BEL}>"


def escaped(field: str) -> str:
    return f"<{field}\\x07>"


def unprintable(output: str) -> list[str]:
    """Every character a terminal would act on rather than show, bar line endings.

    A line ending is ``\\n``, or ``\\r\\n`` where the stream translates newlines
    (``CliRunner`` output on Windows). Only the ``\\r`` of a ``\\r\\n`` pair is
    excused: a lone ``\\r`` moves the cursor to the start of the line, which is
    exactly the kind of overwrite this module exists to catch.
    """
    return [ch for ch in output.replace("\r\n", "\n") if not ch.isprintable() and ch != "\n"]


def ppath(field: str) -> Path:
    return Path(f"/poisoned/{poison(field)}/settings.json")


# ── scenario registry ──────────────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    renderer: str
    name: str
    run: Callable[[pytest.MonkeyPatch, Path], str]
    #: Fields whose escaped form must appear in the output.
    shown: frozenset[str]
    #: Text only the branch under test prints. A field in ``shown`` can also be
    #: printed by an earlier branch in the same run (a preview before a prompt),
    #: so this is what proves the named branch itself ran.
    branch_text: tuple[str, ...] = ()


SCENARIOS: list[Scenario] = []


def scenario(renderer: str, name: str, *shown: str, branch: tuple[str, ...] = ()):
    def register(fn: Callable[[pytest.MonkeyPatch, Path], str]):
        SCENARIOS.append(Scenario(renderer, name, fn, frozenset(shown), branch))
        return fn

    return register


def capture(fn: Callable[[], object], *, input: str | None = None) -> str:
    """Call a renderer directly; return stdout and stderr as one string."""
    runner = CliRunner()
    with runner.isolation(input=input) as streams:
        try:
            fn()
        except (click.exceptions.Abort, click.exceptions.Exit):  # a declined prompt
            pass
        except click.ClickException as exc:  # printed by Click's main in real runs
            exc.show()
    out, err = streams[0], streams[1]
    return out.getvalue().decode() + err.getvalue().decode()


def invoke(command: click.Command, args: list[str], *, input: str | None = None) -> str:
    result = CliRunner().invoke(command, args, input=input)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.output


def sig(prefix: str) -> HookSignature:
    return HookSignature(
        event=poison(f"{prefix}_event"),
        matcher=poison(f"{prefix}_matcher"),
        command_shape=f"hook.sh {poison(f'{prefix}_command')}",
    )


# ── sync: detect / host-write prompt / tier warnings / generate / diff ─


@scenario("_print_settings_detect", "detected-files", "detect_path", "detect_new_path")
def _detect(monkeypatch, tmp_path):
    files = [
        SimpleNamespace(agent="claude_settings", path=ppath("detect_path"), size=12),
        SimpleNamespace(agent="codex_settings", path=ppath("detect_new_path"), size=0),
    ]
    monkeypatch.setattr(context_cmd, "detect_settings_files", lambda *a, **k: files)
    return capture(lambda: context_cmd._print_settings_detect(tmp_path, "user"))


@scenario("_confirm_settings_host_writes", "host-write-prompt", "host_target")
def _host_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(context_cmd, "host_write_targets", lambda *a, **k: [ppath("host_target")])
    return capture(
        lambda: context_cmd._confirm_settings_host_writes(tmp_path, scope="user", yes=False),
        input="n\n",
    )


def _patch_doctor_findings(monkeypatch) -> None:
    monkeypatch.setattr(
        context_cmd,
        "detect_duplicate_tiers",
        lambda *a, **k: [DuplicateTier(tier="user", path=ppath("dup_path"), entries=(sig("dup"),))],
    )
    monkeypatch.setattr(
        context_cmd,
        "find_malformed_matchers",
        lambda *a, **k: [
            MalformedHookMatcher(
                source="tier",
                tier="user",
                path=ppath("mal_path"),
                event=poison("mal_event"),
                rule_index=0,
                matcher_type="list",
            )
        ],
    )
    monkeypatch.setattr(
        context_cmd,
        "find_unportable_hook_commands",
        lambda *a, **k: [
            UnportableHookCommand(
                source="canonical",
                path=ppath("unp_path"),
                event=poison("unp_event"),
                rule_index=0,
                hook_index=0,
                command=f"/home/alice/{poison('unp_command')}",
                unportable_literal=f"/home/alice/{poison('unp_literal')}",
            )
        ],
    )
    monkeypatch.setattr(
        context_cmd,
        "find_unscanned_settings_files",
        lambda *a, **k: [
            UnscannedSettingsFile(
                source="tier", tier="user", path=ppath("uns_path"), reason=poison("uns_reason")
            )
        ],
    )


@scenario(
    "_print_duplicate_tier_warnings",
    "all-four-finding-kinds",
    "dup_path",
    "mal_path",
    "mal_event",
    "unp_path",
    "unp_event",
    "unp_command",
    "unp_literal",
    "uns_path",
    "uns_reason",
)
def _tier_warnings(monkeypatch, tmp_path):
    _patch_doctor_findings(monkeypatch)
    return capture(lambda: context_cmd._print_duplicate_tier_warnings(tmp_path, scope="user"))


def _no_tier_warnings(monkeypatch) -> None:
    monkeypatch.setattr(context_cmd, "_print_duplicate_tier_warnings", lambda *a, **k: None)


@scenario(
    "_print_settings_generate",
    "every-status",
    "gen_target",
    "gen_warning",
    "gen_skipped",
    "gen_confirm",
    "gen_error",
    "gen_aborted",
)
def _generate(monkeypatch, tmp_path):
    _no_tier_warnings(monkeypatch)

    def row(status, *, target=None, warnings=(), reason=""):
        return SimpleNamespace(status=status, target=target, warnings=list(warnings), reason=reason)

    results = {
        "a": row("ok", target=ppath("gen_target"), warnings=[poison("gen_warning")]),
        "b": row("skipped", reason=poison("gen_skipped")),
        "c": row("needs_confirmation", reason=poison("gen_confirm")),
        "d": row("error", reason=poison("gen_error")),
        "e": row("aborted", reason=poison("gen_aborted")),
    }
    monkeypatch.setattr(context_cmd, "generate_all_settings", lambda *a, **k: results)
    return capture(
        lambda: context_cmd._print_settings_generate(tmp_path, scope="user", allow_host_writes=True)
    )


@scenario(
    "_print_settings_diff",
    "every-status",
    "diff_in_sync",
    "diff_out_of_sync",
    "diff_missing",
    "diff_skipped",
    "diff_error",
)
def _diff(monkeypatch, tmp_path):
    _no_tier_warnings(monkeypatch)

    def row(status, *, warnings=(), reason=""):
        return SimpleNamespace(status=status, warnings=list(warnings), reason=reason)

    results = {
        "a": row("in sync", warnings=[poison("diff_in_sync")]),
        "b": row("out of sync", warnings=[poison("diff_out_of_sync")]),
        "c": row("missing target", warnings=[poison("diff_missing")]),
        "d": row("skipped", reason=poison("diff_skipped")),
        "e": row("error", reason=poison("diff_error")),
    }
    monkeypatch.setattr(context_cmd, "diff_settings", lambda *a, **k: results)
    return capture(lambda: context_cmd._print_settings_diff(tmp_path, scope="user"))


# ── settings-doctor ───────────────────────────────────────────────


@scenario(
    "settings_doctor_cmd",
    "text-report",
    "dup_path",
    "dup_event",
    "dup_matcher",
    "dup_command",
    "mal_path",
    "mal_event",
    "unp_path",
    "unp_event",
    "unp_command",
    "unp_literal",
    "uns_path",
    "uns_reason",
)
def _doctor(monkeypatch, tmp_path):
    monkeypatch.setattr(context_cmd, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(context_cmd, "_resolve_cli_scope", lambda _flag: "project_local")
    _patch_doctor_findings(monkeypatch)
    return invoke(context_cmd.settings_doctor_cmd, [])


# ── settings-migrate ──────────────────────────────────────────────


# Real planner and result types, not look-alikes: ``applicable_moves`` and
# ``is_noop`` are then computed by the code that production runs, so a scenario
# cannot reach a branch through a combination the planner never produces.


def _move(prefix: str, *, conflict: bool = False, already: bool = False) -> MigrateMove:
    return MigrateMove(
        signature=sig(prefix),
        rule_to_write_at_target={},
        already_at_target=already,
        conflict_at_target=conflict,
        conflict_reason=poison(f"{prefix}_conflict") if conflict else "",
    )


def _migrate_plan(
    *, source_scope: str = "user", target_scope: str = "project_local", moves=()
) -> MigratePlan:
    return MigratePlan(
        source_scope=source_scope,
        target_scope=target_scope,
        source_path=ppath("mig_source_path"),
        target_path=ppath("mig_target_path"),
        moves=tuple(moves),
        project_root=Path("/poisoned"),
    )


@scenario("_print_migrate_plan_human", "no-moves", "mig_source_path")
def _migrate_preview_empty(monkeypatch, tmp_path):
    return capture(lambda: context_cmd._print_migrate_plan_human(_migrate_plan()))


@scenario(
    "_print_migrate_plan_human",
    "conflict-already-and-move",
    "mig_source_path",
    "mig_target_path",
    "m1_event",
    "m1_matcher",
    "m1_command",
    "m1_conflict",
    "m2_event",
    "m2_command",
    "m3_event",
    "m3_command",
)
def _migrate_preview_moves(monkeypatch, tmp_path):
    plan = _migrate_plan(moves=[_move("m1", conflict=True), _move("m2", already=True), _move("m3")])
    return capture(lambda: context_cmd._print_migrate_plan_human(plan))


def _patch_migrate(monkeypatch, tmp_path, plan) -> None:
    monkeypatch.setattr(context_cmd, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(context_cmd, "plan_migration", lambda *a, **k: plan)
    monkeypatch.setattr(context_cmd.privacy, "emit_project_shared_confirmation", lambda **k: None)


@scenario("settings_migrate_cmd", "planner-error", "mig_plan_error")
def _migrate_plan_error(monkeypatch, tmp_path):
    monkeypatch.setattr(context_cmd, "_find_project_root", lambda: tmp_path)

    def fail(*a, **k):
        raise ValueError(poison("mig_plan_error"))

    monkeypatch.setattr(context_cmd, "plan_migration", fail)
    return invoke(context_cmd.settings_migrate_cmd, ["--from", "user", "--to", "project_local"])


@scenario(
    "settings_migrate_cmd",
    "gate-b-prompt-target-leg",
    "mig_target_path",
    "t_event",
    branch=("into the project_shared tier",),
)
def _migrate_gate_b_target(monkeypatch, tmp_path):
    _patch_migrate(
        monkeypatch, tmp_path, _migrate_plan(target_scope="project_shared", moves=[_move("t")])
    )
    return invoke(
        context_cmd.settings_migrate_cmd,
        ["--from", "user", "--to", "project_shared", "--apply"],
        input="n\n",
    )


@scenario(
    "settings_migrate_cmd",
    "gate-b-prompt-source-leg",
    "mig_source_path",
    "s_event",
    branch=("from the project_shared tier",),
)
def _migrate_gate_b_source(monkeypatch, tmp_path):
    _patch_migrate(
        monkeypatch, tmp_path, _migrate_plan(source_scope="project_shared", moves=[_move("s")])
    )
    return invoke(
        context_cmd.settings_migrate_cmd,
        ["--from", "project_shared", "--to", "user", "--apply"],
        input="n\n",
    )


@scenario(
    "settings_migrate_cmd",
    "host-write-listing",
    "mig_source_path",
    "mig_target_path",
    branch=("modify the following files outside this project", "(target)", "(source)"),
)
def _migrate_host_writes(monkeypatch, tmp_path):
    _patch_migrate(monkeypatch, tmp_path, _migrate_plan(moves=[_move("h")]))
    monkeypatch.setattr(context_cmd, "_is_within", lambda *a, **k: False)
    return invoke(
        context_cmd.settings_migrate_cmd,
        ["--from", "user", "--to", "project_local", "--apply"],
        input="n\n",
    )


@scenario(
    "settings_migrate_cmd",
    "apply-writes-and-drift",
    "mig_source_path",
    "mig_target_path",
    "mig_apply_warning",
    # Both paths are also printed by the preview, so only these prove the
    # success lines themselves ran.
    branch=("✓ wrote target", "✓ cleaned source"),
)
def _migrate_apply(monkeypatch, tmp_path):
    plan = _migrate_plan(moves=[_move("a")])
    _patch_migrate(monkeypatch, tmp_path, plan)
    monkeypatch.setattr(context_cmd, "_is_within", lambda *a, **k: True)
    monkeypatch.setattr(
        context_cmd,
        "apply_migration",
        lambda *a, **k: MigrateResult(
            plan=plan,
            target_written=True,
            source_written=True,
            warnings=[poison("mig_apply_warning")],
        ),
    )
    return invoke(
        context_cmd.settings_migrate_cmd,
        ["--from", "user", "--to", "project_local", "--apply", "--yes"],
    )


@scenario("settings_migrate_cmd", "gate-a-refusal", "mig_gate_a")
def _migrate_gate_a(monkeypatch, tmp_path):
    _patch_migrate(monkeypatch, tmp_path, _migrate_plan(moves=[_move("g")]))
    monkeypatch.setattr(context_cmd, "_is_within", lambda *a, **k: True)

    def refuse(*a, **k):
        raise PrivacyScanError(poison("mig_gate_a"))

    monkeypatch.setattr(context_cmd, "apply_migration", refuse)
    return invoke(
        context_cmd.settings_migrate_cmd,
        ["--from", "user", "--to", "project_local", "--apply", "--yes"],
    )


# ── settings-copy ─────────────────────────────────────────────────


def _copy_plan(*, canonical_state: str = "missing", target_state: str = "missing") -> HookCopyPlan:
    """A real plan: ``label``, ``pending_*_write`` and ``is_noop`` come from its
    own properties, so the leg states alone decide which gates a scenario hits."""
    signature = sig("cp")
    return HookCopyPlan(
        src_project_root=Path("/poisoned/src"),
        dst_project_root=Path(f"/poisoned/{poison('cp_dst_root')}"),
        dst_scope="user",
        src_canonical_path=ppath("cp_src_canonical"),
        dst_canonical_path=ppath("cp_dst_canonical"),
        dst_target_path=ppath("cp_dst_target"),
        signature=signature,
        canonical_inner={},
        rule_for_canonical={},
        rule_for_target={},
        canonical_state=canonical_state,
        canonical_reason=poison("cp_canonical_reason") if canonical_state == "conflict" else "",
        target_state=target_state,
        target_reason=poison("cp_target_reason") if target_state == "conflict" else "",
    )


@scenario(
    "_print_hook_copy_plan",
    "conflict-and-missing-legs",
    "cp_event",
    "cp_matcher",
    "cp_command",
    "cp_src_canonical",
    "cp_dst_root",
    "cp_dst_canonical",
    "cp_canonical_reason",
    "cp_dst_target",
)
def _copy_preview(monkeypatch, tmp_path):
    plan = _copy_plan(canonical_state="conflict", target_state="missing")
    return capture(lambda: context_cmd._print_hook_copy_plan(plan))


@scenario("_print_hook_copy_plan", "exact-and-conflict-legs", "cp_target_reason", "cp_dst_target")
def _copy_preview_target_conflict(monkeypatch, tmp_path):
    plan = _copy_plan(canonical_state="exact", target_state="conflict")
    return capture(lambda: context_cmd._print_hook_copy_plan(plan))


def _follow_up(plan: HookCopyPlan) -> str:
    """The real follow-up command, built from the plan's poisoned destination."""
    return _sync_followup_command(plan.dst_project_root, plan.dst_scope)


@scenario(
    "_print_hook_copy_result",
    "written-canonical-already-target",
    "cp_dst_canonical",
    "cp_event",
    "cp_copy_warning",
    "cp_dst_root",
    branch=("Next: run `cd $'",),
)
def _copy_result_a(monkeypatch, tmp_path):
    plan = _copy_plan()
    result = HookCopyResult(
        plan=plan,
        canonical_written=True,
        canonical_already=False,
        target_written=False,
        target_already=True,
        warnings=[poison("cp_copy_warning")],
        sync_command=_follow_up(plan),
    )
    return capture(lambda: context_cmd._print_hook_copy_result(result))


@scenario(
    "_print_hook_copy_result", "already-canonical-written-target", "cp_event", "cp_dst_target"
)
def _copy_result_b(monkeypatch, tmp_path):
    plan = _copy_plan()
    result = HookCopyResult(
        plan=plan,
        canonical_written=False,
        canonical_already=True,
        target_written=True,
        target_already=False,
        warnings=[],
        sync_command=_follow_up(plan),
    )
    return capture(lambda: context_cmd._print_hook_copy_result(result))


def _patch_copy(monkeypatch, tmp_path, plan=None, *, dst=None, scope_rec=None) -> list[str]:
    if dst is None:
        dst = tmp_path / "dst"
        (dst / ".memtomem").mkdir(parents=True)
    monkeypatch.setattr(context_cmd, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(context_cmd, "_projects_gateway_cfg", lambda: None)
    monkeypatch.setattr(context_cmd, "_projects_discover", lambda *a, **k: [])
    monkeypatch.setattr(context_cmd, "resolve_project_selector", lambda *a, **k: (dst, scope_rec))
    monkeypatch.setattr(context_cmd.privacy, "emit_project_shared_confirmation", lambda **k: None)
    if plan is not None:
        monkeypatch.setattr(context_cmd, "plan_hook_copy", lambda *a, **k: plan)
    return ["--event", "PostToolUse", "--to-project", str(dst), "--to", "user"]


@scenario("settings_copy_cmd", "selector-error", "cp_plan_error")
def _copy_plan_error(monkeypatch, tmp_path):
    args = _patch_copy(monkeypatch, tmp_path)

    def fail(*a, **k):
        raise ValueError(poison("cp_plan_error"))

    monkeypatch.setattr(context_cmd, "plan_hook_copy", fail)
    return invoke(context_cmd.settings_copy_cmd, args)


@scenario(
    "settings_copy_cmd",
    "paused-destination",
    "cp_paused_root",
    branch=("is paused",),
)
def _copy_paused(monkeypatch, tmp_path):
    # Never touches the filesystem: the refusal fires before any store check.
    dst = Path(f"/poisoned/{poison('cp_paused_root')}")
    args = _patch_copy(
        monkeypatch,
        tmp_path,
        dst=dst,
        scope_rec=SimpleNamespace(enabled=False, scope_id="p-000000000000"),
    )
    return invoke(context_cmd.settings_copy_cmd, args)


@scenario(
    "settings_copy_cmd",
    "destination-without-store",
    "cp_storeless_root",
    branch=("has no .memtomem/ store", "Initialize it first: cd $'"),
)
def _copy_storeless(monkeypatch, tmp_path):
    # A path that does not exist has no .memtomem/ either — no directory needed,
    # so the scenario runs on filesystems that reject control characters.
    dst = Path(f"/poisoned/{poison('cp_storeless_root')}")
    args = _patch_copy(monkeypatch, tmp_path, dst=dst)
    return invoke(context_cmd.settings_copy_cmd, args)


@scenario(
    "settings_copy_cmd",
    "gate-b-prompt",
    "cp_dst_root",
    "cp_command",
    branch=("tracked canonical settings",),
)
def _copy_gate_b(monkeypatch, tmp_path):
    args = _patch_copy(monkeypatch, tmp_path, _copy_plan(target_state="exact"))
    return invoke(context_cmd.settings_copy_cmd, [*args, "--apply"], input="n\n")


@scenario(
    "settings_copy_cmd",
    "host-write-listing",
    "cp_dst_target",
    branch=("outside the destination project", "(user tier)"),
)
def _copy_host_writes(monkeypatch, tmp_path):
    args = _patch_copy(monkeypatch, tmp_path, _copy_plan(canonical_state="exact"))
    return invoke(context_cmd.settings_copy_cmd, [*args, "--apply"], input="n\n")


@scenario("settings_copy_cmd", "gate-a-refusal", "cp_gate_a")
def _copy_gate_a(monkeypatch, tmp_path):
    args = _patch_copy(monkeypatch, tmp_path, _copy_plan())

    def refuse(*a, **k):
        raise PrivacyScanError(poison("cp_gate_a"))

    monkeypatch.setattr(context_cmd, "apply_hook_copy", refuse)
    return invoke(
        context_cmd.settings_copy_cmd, [*args, "--apply", "--yes", "--confirm-project-shared"]
    )


# ── the checks ────────────────────────────────────────────────────


def test_every_settings_function_is_classified():
    """A renderer added under a settings name cannot slip past the registry."""
    tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    settings_named = {name for name in defined if _SETTINGS_NAME.search(name)}
    unclassified = settings_named - RENDERERS - set(NOT_RENDERERS)
    assert not unclassified, (
        f"classify {sorted(unclassified)} as RENDERERS (and add a scenario) "
        "or NOT_RENDERERS (with a reason)"
    )
    missing = (RENDERERS | set(NOT_RENDERERS)) - defined
    assert not missing, f"registered but gone (renamed?): {sorted(missing)}"


def test_every_renderer_has_a_scenario():
    driven = {s.renderer for s in SCENARIOS}
    assert driven == RENDERERS, (
        f"renderers without a scenario: {sorted(RENDERERS - driven)}; "
        f"scenarios for unregistered functions: {sorted(driven - RENDERERS)}"
    )


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda s: f"{s.renderer}[{s.name}]")
def test_renderer_output_carries_no_raw_control_characters(case, monkeypatch, tmp_path):
    output = case.run(monkeypatch, tmp_path)
    leaked = unprintable(output)
    assert not leaked, f"{case.renderer}[{case.name}] printed {leaked!r} unescaped:\n{output!r}"
    unreached = sorted(f for f in case.shown if escaped(f) not in output)
    assert not unreached, (
        f"{case.renderer}[{case.name}] never showed {unreached} — the scenario did "
        f"not drive that branch, so a clean output proves nothing there:\n{output}"
    )
    missing_branch = [text for text in case.branch_text if text not in output]
    assert not missing_branch, (
        f"{case.renderer}[{case.name}] did not print {missing_branch}: the branch it is "
        f"named for never ran, and its fields may have come from another one:\n{output}"
    )


# ── destination refusals shared with the artifact writers ─────────


class TestDestinationRefusals:
    """``settings-copy``, ``transfer`` and ``import`` refuse a paused or
    store-less destination with one helper each.

    The three commands used to carry their own copy of each message. Only the
    settings-copy copy was fixed at first, which is how the other two kept an
    unescaped path and an unquoted ``cd`` hint. The helpers are pinned here for
    every caller; the settings-copy scenarios above drive them end to end.
    """

    @pytest.mark.parametrize(
        "landing", ["transferred artifact", "imported artifact", "copied hook"]
    )
    def test_paused_refusal_escapes_the_path(self, landing):
        exc = context_cmd._paused_destination_error("p-000000000000", ppath("paused_root"), landing)
        assert not unprintable(exc.message)
        assert escaped("paused_root") in exc.message
        assert f"so the {landing} would not fan out there" in exc.message

    def test_missing_store_refusal_escapes_prose_and_quotes_the_hint(self):
        dst = ppath("storeless_root")
        exc = context_cmd._missing_store_error(dst)
        assert not unprintable(exc.message.replace("\n", ""))
        prose, hint = exc.message.split("\n")
        assert escaped("storeless_root") in prose
        # The pasteable hint is ANSI-C quoted: display-safe and still the same path.
        assert hint.strip().startswith("Initialize it first: cd $'")

    def test_missing_store_hint_is_plain_quoting_for_an_ordinary_path(self):
        # The expectation is computed from the platform's own path spelling:
        # str(Path) uses backslashes on Windows, and a POSIX literal here would
        # assert the separator rather than the quoting.
        dst = Path("/home/u/my project")
        exc = context_cmd._missing_store_error(dst)
        assert f"cd {shlex.quote(str(dst))} && mm context init" in exc.message
        assert "$'" not in exc.message

    @pytest.mark.parametrize("wording", ["sync enrollment is disabled", "has no .memtomem/ store"])
    def test_the_cli_has_one_copy_of_each_refusal(self, wording):
        """A fourth hand-written copy would drift the way the first three did."""
        assert CLI_SOURCE.read_text(encoding="utf-8").count(wording) == 1


def test_unprintable_excuses_crlf_line_endings_only():
    """Pins the line-ending rule on every platform, not just on Windows runners."""
    assert unprintable("a\r\nb\n") == []
    assert unprintable("a\rb") == ["\r"]
    assert unprintable("a\r\r\nb") == ["\r"]
    assert unprintable(f"a{BEL}\r\n") == [BEL]

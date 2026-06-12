"""CLI tests for ``mm context copy`` / ``mm context move`` (#1274, campaign #1270 A-3).

Engine semantics (two-root fan-out split, Gate A, pair locks, EXDEV) are
pinned by ``test_context_transfer.py``; this file pins what the CLI layer
adds on top of :func:`memtomem.context.transfer.transfer_artifact`:

- the #1274 acceptance round-trip: ``move agents foo --to-project <pathB>
  --to project_shared --apply --confirm-project-shared -y``;
- dry-run by default (zero disk mutation) printing the engine's exact
  follow-up sync command (``TransferResult.sync_command``);
- Gate B parity with ``migrate``: ``--yes`` alone refuses a
  project_shared landing, the prompt aborts cleanly, and
  ``--confirm-project-shared`` opts in;
- the option matrix: ``--as`` exists on copy only, ``--to`` /
  ``--to-project`` combination gates, and the source-tier default when
  ``--to`` is omitted;
- destination-project gates: an explicitly typed unregistered path is
  consent, a paused registered destination refuses with a resume hint,
  and a missing ``.memtomem/`` store errors with an ``mm context init``
  hint;
- HOME-isolated user-tier directions, including the destination-side
  gitignore marker on a project_local landing.

Isolation: HOME/USERPROFILE point into ``tmp_path`` so the user tier
(``~/.memtomem``) and user-tier runtime fan-out stay hermetic
(``feedback_path_home_cross_platform``), and ``ContextGatewayConfig`` is
monkeypatched so ``--to-project`` discovery reads a tmp
``known_projects.json`` (pattern from ``test_cli_context_projects.py``).
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context.projects import KnownProjectsStore, compute_scope_id

_SECRET_LITERAL = "AKIA1234567890ABCDEF"  # AWS-key shape — caught by the privacy scan


@pytest.fixture()
def cli_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two initialized project roots + isolated HOME, cwd at project A."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    for proj in (proj_a, proj_b):
        (proj / ".git").mkdir(parents=True)
        (proj / ".memtomem").mkdir()

    kp = tmp_path / "known_projects.json"

    class _FakeCfg:
        known_projects_path = kp
        experimental_claude_projects_scan = False
        auto_display_configured_projects = True

    monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())
    monkeypatch.chdir(proj_a)
    return {"a": proj_a.resolve(), "b": proj_b.resolve(), "home": home.resolve(), "kp": kp}


def _seed_agent(
    layout: dict[str, Path],
    scope: str,
    name: str = "foo",
    root_key: str = "a",
    body: str | None = None,
) -> Path:
    """Write a dir-layout canonical agent; return the artifact dir."""
    if scope == "user":
        base = layout["home"] / ".memtomem" / "agents"
    elif scope == "project_shared":
        base = layout[root_key] / ".memtomem" / "agents"
    elif scope == "project_local":
        base = layout[root_key] / ".memtomem" / "agents.local"
    else:
        raise ValueError(scope)
    artifact_dir = base / name
    artifact_dir.mkdir(parents=True)
    text = body if body is not None else f"---\nname: {name}\ndescription: t\n---\n\nhello\n"
    (artifact_dir / "agent.md").write_text(text, encoding="utf-8")
    return artifact_dir


def _invoke(args: list[str], **kwargs):
    return CliRunner().invoke(context, args, **kwargs)


# ── acceptance round-trip + dry-run ──────────────────────────────────


def test_acceptance_move_cross_project_shared_round_trip(cli_projects) -> None:
    """#1274 acceptance: the exact flag spelling round-trips end-to-end."""
    src = _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        [
            "move",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_shared",
            "--apply",
            "--confirm-project-shared",
            "-y",
        ]
    )
    assert result.exit_code == 0, result.output
    dst = cli_projects["b"] / ".memtomem" / "agents" / "foo"
    assert (dst / "agent.md").is_file()
    assert not src.exists()
    assert "✓ moved agents/foo: project_shared → project_shared" in result.output
    # Exact destination-project sync command (cd-prefixed until A-9).
    expected_sync = (
        f"cd {shlex.quote(str(cli_projects['b']))} && mm context sync --scope project_shared"
    )
    assert expected_sync in result.output


def test_dry_run_is_default_and_mutates_nothing(cli_projects) -> None:
    src = _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        [
            "move",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_shared",
        ]
    )
    assert result.exit_code == 0, result.output
    assert (src / "agent.md").is_file()
    assert not (cli_projects["b"] / ".memtomem" / "agents" / "foo").exists()
    assert "Run with --apply --confirm-project-shared to execute." in result.output
    assert "mm context sync --scope project_shared" in result.output


# ── Gate B parity with migrate ───────────────────────────────────────


def test_gate_b_yes_alone_refuses_project_shared(cli_projects) -> None:
    src = _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        [
            "move",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_shared",
            "--apply",
            "-y",
        ]
    )
    assert result.exit_code != 0
    assert "--confirm-project-shared" in result.output
    assert (src / "agent.md").is_file()
    assert not (cli_projects["b"] / ".memtomem" / "agents" / "foo").exists()


def test_gate_b_prompt_decline_aborts_without_mutation(cli_projects) -> None:
    src = _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        [
            "move",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_shared",
            "--apply",
        ],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "This will move the canonical into the git-tracked project_shared tier" in (
        result.output
    )
    assert (src / "agent.md").is_file()
    assert not (cli_projects["b"] / ".memtomem" / "agents" / "foo").exists()


# ── copy: --as rename, source untouched, copy-only option ────────────


def test_copy_as_rename_cross_project(cli_projects) -> None:
    src = _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        [
            "copy",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_shared",
            "--as",
            "bar",
            "--apply",
            "--confirm-project-shared",
            "-y",
        ]
    )
    assert result.exit_code == 0, result.output
    assert (src / "agent.md").is_file()  # copy never consumes the source
    dst_manifest = cli_projects["b"] / ".memtomem" / "agents" / "bar" / "agent.md"
    assert dst_manifest.is_file()
    assert "name: bar" in dst_manifest.read_text(encoding="utf-8")
    assert "✓ copied agents/foo: project_shared → project_shared as bar" in result.output


def test_move_does_not_define_as_option(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        [
            "move",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--as",
            "bar",
        ]
    )
    assert result.exit_code == 2
    assert "no such option" in result.output.lower()


# ── option-combination gates ─────────────────────────────────────────


def test_requires_to_or_to_project(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(["move", "agents", "foo"])
    assert result.exit_code == 2
    assert "nothing to do" in result.output


def test_to_user_with_to_project_refuses(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        ["move", "agents", "foo", "--to", "user", "--to-project", str(cli_projects["b"])]
    )
    assert result.exit_code == 2
    assert "user tier" in result.output


def test_yes_requires_apply(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(["move", "agents", "foo", "--to", "user", "-y"])
    assert result.exit_code == 2
    assert "--yes is only valid with --apply" in result.output


def test_omitted_to_keeps_source_tier(cli_projects) -> None:
    _seed_agent(cli_projects, "project_local", root_key="a")
    result = _invoke(["copy", "agents", "foo", "--to-project", str(cli_projects["b"]), "--apply"])
    assert result.exit_code == 0, result.output
    assert (cli_projects["b"] / ".memtomem" / "agents.local" / "foo" / "agent.md").is_file()


def test_omitted_to_with_user_source_and_to_project_errors(cli_projects) -> None:
    _seed_agent(cli_projects, "user")
    result = _invoke(["copy", "agents", "foo", "--to-project", str(cli_projects["b"])])
    assert result.exit_code == 2
    assert "lives at the user tier" in result.output


# ── destination-project gates ────────────────────────────────────────


def test_destination_without_store_errors_with_init_hint(cli_projects, tmp_path: Path) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    bare = tmp_path / "bare"
    bare.mkdir()
    result = _invoke(["move", "agents", "foo", "--to-project", str(bare), "--to", "project_shared"])
    assert result.exit_code != 0
    assert "no .memtomem/ store" in result.output
    assert "mm context init" in result.output


def test_paused_registered_destination_refuses_with_resume_hint(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    store = KnownProjectsStore(cli_projects["kp"])
    store.add(cli_projects["b"])
    sid = compute_scope_id(cli_projects["b"])
    store.set_enabled_by_scope_id(sid, False)
    result = _invoke(
        [
            "move",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_shared",
        ]
    )
    assert result.exit_code != 0
    assert "paused" in result.output
    assert f"mm context projects resume {sid}" in result.output


def test_to_project_accepts_scope_id_selector(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    KnownProjectsStore(cli_projects["kp"]).add(cli_projects["b"])
    sid = compute_scope_id(cli_projects["b"])
    result = _invoke(
        ["copy", "agents", "foo", "--to-project", sid, "--to", "project_local", "--apply"]
    )
    assert result.exit_code == 0, result.output
    assert (cli_projects["b"] / ".memtomem" / "agents.local" / "foo" / "agent.md").is_file()


def test_to_project_unknown_scope_id_errors(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        ["move", "agents", "foo", "--to-project", "p-deadbeef0000", "--to", "project_shared"]
    )
    assert result.exit_code != 0
    assert "no discovered project has scope_id" in result.output


# ── user-tier directions (HOME-isolated) ─────────────────────────────


def test_move_user_to_project_local_appends_destination_marker(cli_projects) -> None:
    src = _seed_agent(cli_projects, "user")
    result = _invoke(["move", "agents", "foo", "--to", "project_local", "--apply"])
    assert result.exit_code == 0, result.output
    assert not src.exists()
    assert (cli_projects["a"] / ".memtomem" / "agents.local" / "foo" / "agent.md").is_file()
    assert "Appended .gitignore marker" in result.output
    assert ".memtomem/*.local/" in (cli_projects["a"] / ".gitignore").read_text(encoding="utf-8")


def test_cross_project_local_marker_lands_at_destination(cli_projects) -> None:
    _seed_agent(cli_projects, "project_shared", root_key="a")
    result = _invoke(
        [
            "copy",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_local",
            "--apply",
        ]
    )
    assert result.exit_code == 0, result.output
    assert ".memtomem/*.local/" in (cli_projects["b"] / ".gitignore").read_text(encoding="utf-8")
    assert not (cli_projects["a"] / ".gitignore").exists()


def test_copy_project_local_to_user_prints_plain_sync_command(cli_projects) -> None:
    src = _seed_agent(cli_projects, "project_local", root_key="a")
    result = _invoke(["copy", "agents", "foo", "--to", "user", "--apply"])
    assert result.exit_code == 0, result.output
    assert (src / "agent.md").is_file()
    assert (cli_projects["home"] / ".memtomem" / "agents" / "foo" / "agent.md").is_file()
    # User-tier sync is project-independent — exact command, no cd prefix.
    assert "Next: run `mm context sync --scope user`" in result.output


# ── Gate A translation + help text ───────────────────────────────────


def test_gate_a_block_renders_as_cli_error_with_source_hint(cli_projects) -> None:
    src_dir = _seed_agent(
        cli_projects,
        "project_local",
        root_key="a",
        body=f"---\nname: foo\ndescription: t\n---\n\n{_SECRET_LITERAL}\n",
    )
    result = _invoke(
        [
            "move",
            "agents",
            "foo",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_shared",
            "--apply",
            "--confirm-project-shared",
        ]
    )
    assert result.exit_code != 0
    assert "Offending file" in result.output
    assert (src_dir / "agent.md").is_file()  # move rolled back
    assert not (cli_projects["b"] / ".memtomem" / "agents" / "foo").exists()  # zero residue


def test_invalid_source_name_is_a_clean_cli_error(cli_projects) -> None:
    """Traversal-shaped names fail validation BEFORE any path probe.

    With ``--to`` omitted the dispatch pre-probes the source tier from
    the raw name — without the up-front ``validate_name`` a name like
    ``../escape`` would hit the filesystem first (Codex review fold).
    """
    result = _invoke(["copy", "agents", "../escape", "--to-project", str(cli_projects["b"])])
    assert result.exit_code == 1
    assert result.output.strip(), "expected a one-line CLI error, got empty output"
    assert "Error" in result.output
    assert not isinstance(result.exception, Exception) or isinstance(
        result.exception, SystemExit
    ), f"unhandled exception leaked: {result.exception!r}"


def test_invalid_as_name_is_a_clean_cli_error(cli_projects) -> None:
    _seed_agent(cli_projects, "project_local", root_key="a")
    result = _invoke(["copy", "agents", "foo", "--to", "user", "--as", "../bad", "--apply"])
    assert result.exit_code == 1
    assert result.output.strip(), "expected a one-line CLI error, got empty output"
    assert "Error" in result.output
    # Nothing escaped the canonical roots.
    assert not (cli_projects["home"] / ".memtomem" / "agents" / "foo").exists()


def test_move_help_is_the_three_verb_comparison(cli_projects) -> None:
    """#1274 acceptance: move vs copy vs migrate documented in one place."""
    result = _invoke(["move", "--help"])
    assert result.exit_code == 0
    for needle in ("move ", "copy ", "migrate", "flat→dir layout adoption"):
        assert needle in result.output

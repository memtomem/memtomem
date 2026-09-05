"""CLI tests for ``mm context export`` / ``mm context import`` (ADR-0037, #2298).

Engine semantics — the wire grammar, both gates, the version-surface rules and
the promote transaction — are pinned by ``test_context_bundle.py``. This file
pins what the CLI layer adds on top: the round-trip a user actually types, Gate
B parity with every other project_shared write, the destination-project gates,
and the fact that no secret literal reaches the terminal.

Isolation follows ``test_cli_context_transfer.py``: HOME points into
``tmp_path`` so the user tier stays hermetic, and ``ContextGatewayConfig`` is
monkeypatched so ``--to-project`` discovery reads a tmp registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context

# Assembled at runtime — a literal AWS-key shape in the tree trips GitHub's
# push protection and makes this file unindexable.
SECRET = "api_key=" + "AKIA" + "TESTKEY" + "1234567890"


@pytest.fixture()
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    for proj in (proj_a, proj_b):
        (proj / ".git").mkdir(parents=True)
        (proj / ".memtomem").mkdir()

    class _FakeCfg:
        known_projects_path = tmp_path / "known_projects.json"
        experimental_claude_projects_scan = False
        auto_display_configured_projects = True

    monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())
    monkeypatch.chdir(proj_a)
    return {"a": proj_a.resolve(), "b": proj_b.resolve(), "home": home.resolve(), "tmp": tmp_path}


def _seed(
    root: Path, *, scope: str = "project_shared", name: str = "foo", body: str | None = None
) -> Path:
    store = root / ".memtomem" / ("skills" if scope == "project_shared" else "skills.local")
    art = store / name
    art.mkdir(parents=True)
    (art / "SKILL.md").write_text(body or f"---\nname: {name}\n---\nbody\n")
    return art


def _run(*args: str) -> "CliRunner.invoke":
    return CliRunner().invoke(context, list(args))


class TestExportCli:
    def test_round_trip_into_another_project(self, projects) -> None:
        _seed(projects["a"])
        out = projects["tmp"] / "foo.json"

        exported = _run("export", "skills", "foo", "--out", str(out))
        assert exported.exit_code == 0, exported.output
        assert out.exists()
        assert "exported skills/foo" in exported.output

        imported = _run(
            "import",
            str(out),
            "--to",
            "project_shared",
            "--to-project",
            str(projects["b"]),
            "--apply",
            "--confirm-project-shared",
            "-y",
        )
        assert imported.exit_code == 0, imported.output
        landed = projects["b"] / ".memtomem" / "skills" / "foo" / "SKILL.md"
        assert landed.read_text() == "---\nname: foo\n---\nbody\n"
        assert "landed untracked" in imported.output
        assert "mm context sync --scope project_shared" in imported.output

    def test_secret_exits_one_without_echoing_it(self, projects) -> None:
        _seed(projects["a"], body=f"---\nname: foo\n---\n{SECRET}\n")
        out = projects["tmp"] / "foo.json"

        result = _run("export", "skills", "foo", "--out", str(out))

        assert result.exit_code == 1
        assert SECRET not in result.output
        assert "SKILL.md" in result.output
        assert not out.exists()
        assert "Traceback" not in result.output

    def test_no_force_unsafe_option_exists(self, projects) -> None:
        """§4 is enforced by the absence of a valve; --help must not advertise one."""
        result = _run("export", "--help")
        assert result.exit_code == 0
        assert "unsafe" not in result.output

    def test_existing_out_is_a_clean_error(self, projects) -> None:
        _seed(projects["a"])
        out = projects["tmp"] / "foo.json"
        out.write_text("previous\n")

        result = _run("export", "skills", "foo", "--out", str(out))

        assert result.exit_code == 1
        assert "already exists" in result.output
        assert out.read_text() == "previous\n"


class TestImportCli:
    @pytest.fixture()
    def bundle(self, projects) -> Path:
        _seed(projects["a"])
        out = projects["tmp"] / "foo.json"
        assert _run("export", "skills", "foo", "--out", str(out)).exit_code == 0
        return out

    def test_to_is_required(self, bundle) -> None:
        result = _run("import", str(bundle), "--apply")
        assert result.exit_code == 2
        assert "--to" in result.output

    def test_dry_run_is_the_default_and_writes_nothing(self, projects, bundle) -> None:
        result = _run("import", str(bundle), "--to", "project_local")

        assert result.exit_code == 0
        assert "would import" in result.output
        assert "re-run with --apply" in result.output
        assert not (projects["a"] / ".memtomem" / "skills.local").exists()

    def test_yes_alone_does_not_satisfy_gate_b(self, projects, bundle) -> None:
        """Lands in project B, which holds no `foo` — so "nothing was written"
        is an observation about this command, not about the seeded source."""
        result = _run(
            "import",
            str(bundle),
            "--to",
            "project_shared",
            "--to-project",
            str(projects["b"]),
            "--apply",
            "-y",
        )

        assert result.exit_code == 1
        assert "--confirm-project-shared" in result.output
        assert not (projects["b"] / ".memtomem" / "skills" / "foo").exists()

    def test_prompt_decline_aborts_without_mutation(self, projects, bundle) -> None:
        result = CliRunner().invoke(
            context,
            [
                "import",
                str(bundle),
                "--to",
                "project_shared",
                "--to-project",
                str(projects["b"]),
                "--apply",
            ],
            input="n\n",
        )

        assert result.exit_code == 1
        assert not (projects["b"] / ".memtomem" / "skills" / "foo").exists()

    def test_force_unsafe_with_project_shared_is_a_usage_error(self, bundle) -> None:
        result = _run(
            "import",
            str(bundle),
            "--to",
            "project_shared",
            "--apply",
            "--confirm-project-shared",
            "--force-unsafe-import",
        )

        assert result.exit_code == 2
        assert "git-tracked" in result.output

    def test_missing_store_names_the_init_remedy(self, projects, bundle) -> None:
        bare = projects["tmp"] / "bare"
        bare.mkdir()

        result = _run(
            "import",
            str(bundle),
            "--to",
            "project_shared",
            "--to-project",
            str(bare),
            "--apply",
            "--confirm-project-shared",
            "-y",
        )

        assert result.exit_code == 1
        assert "mm context init" in result.output

    def test_project_local_landing_appends_the_gitignore_marker(self, projects, bundle) -> None:
        result = _run("import", str(bundle), "--to", "project_local", "--apply")

        assert result.exit_code == 0
        gitignore = (projects["a"] / ".gitignore").read_text()
        assert ".memtomem/*.local/" in gitignore

    def test_a_refused_import_leaves_the_destination_project_untouched(
        self, projects, bundle
    ) -> None:
        """The marker used to be appended before the bundle was even parsed.

        A malformed, privacy-blocked or colliding import then still edited the
        destination's .gitignore — a write the user never got an artifact for.
        Fails if the marker moves back ahead of the gates.
        """
        bad = projects["tmp"] / "bad.json"
        bad.write_text("{not json")
        gitignore = projects["a"] / ".gitignore"
        assert not gitignore.exists()

        result = _run("import", str(bad), "--to", "project_local", "--apply")

        assert result.exit_code == 1
        assert not gitignore.exists()

    def test_an_unprotectable_local_tier_refuses_instead_of_landing(
        self, projects, bundle, monkeypatch
    ) -> None:
        """ADR-0037 §6: receipt fails closed when the tier cannot be protected.

        The marker is what keeps a received artifact out of `git add -A`, and a
        bundle is the one input that arrives from another machine — warning and
        landing it anyway puts foreign bytes in a directory the next commit
        would stage. Fails if this reverts to a yellow warning.
        """
        import shutil

        shutil.rmtree(projects["a"] / ".git")

        result = _run("import", str(bundle), "--to", "project_local", "--apply")

        assert result.exit_code == 1
        assert "cannot protect the project_local tier" in result.output
        assert not (projects["a"] / ".memtomem" / "skills.local" / "foo").exists()

    def test_renamed_import_reports_both_names(self, projects, bundle) -> None:
        result = _run("import", str(bundle), "--to", "project_local", "--as", "foo2", "--apply")

        assert result.exit_code == 0
        assert "renamed from foo" in result.output
        landed = projects["a"] / ".memtomem" / "skills.local" / "foo2" / "SKILL.md"
        assert "name: foo2" in landed.read_text()

    def test_malformed_bundle_is_a_clean_error(self, projects) -> None:
        bad = projects["tmp"] / "bad.json"
        bad.write_text("{not json")

        result = _run("import", str(bad), "--to", "project_local", "--apply")

        assert result.exit_code == 1
        assert "Traceback" not in result.output

    def test_privacy_refusal_gains_the_cli_flag_spelling(self, projects, bundle) -> None:
        """The engine states the condition; the CLI owns the vocabulary (#1869).

        Fails if the engine goes back to spelling the flag itself (the
        neutrality sweep would catch that) OR if the CLI stops appending the
        hint, which would leave the user with a refusal and no way forward.
        """
        import base64
        import hashlib

        from memtomem.context.bundle import _structure_digest

        doc = json.loads(bundle.read_text())
        tainted_body = f"---\nname: foo\n---\n{SECRET}\n".encode()
        for entry in doc["files"]:
            if entry["path"] == "SKILL.md":
                entry["content_b64"] = base64.b64encode(tainted_body).decode()
                entry["sha256"] = hashlib.sha256(tainted_body).hexdigest()
        doc["payload_sha256"] = _structure_digest(
            doc["kind"],
            doc["name"],
            [(f["path"], f["exec"], f["sha256"]) for f in doc["files"]],
            doc["dirs"],
        )
        tainted = projects["tmp"] / "tainted.json"
        tainted.write_text(json.dumps(doc))

        result = _run("import", str(tainted), "--to", "project_local", "--apply")

        assert result.exit_code == 1
        assert "secret-shaped value" in result.output
        assert "--force-unsafe-import" in result.output
        assert SECRET not in result.output

    def test_memory_bundle_is_redirected_to_mem_import(self, projects) -> None:
        mem = projects["tmp"] / "mem.json"
        mem.write_text(json.dumps({"version": "2", "total_chunks": 0, "chunks": []}))

        result = _run("import", str(mem), "--to", "project_local", "--apply")

        assert result.exit_code == 1
        assert "mem_import" in result.output

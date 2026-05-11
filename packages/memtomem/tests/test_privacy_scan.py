"""ADR-0011 PR-E3 — sync-side privacy scan (canonical → runtime fan-out).

Mirrors :mod:`test_privacy_scope_gate` (the import-side gate) but for
the sync-side :mod:`memtomem.context.privacy_scan` module. Pins:

* Single-file scan: secret hit → ``decision="blocked"``.
* Tree walk: secret in ``scripts/leak.sh`` → blocked even when
  ``SKILL.md`` is clean (``feedback_force_unsafe_redaction_valve_only.md``
  — must use the real ``AKIA...`` fixture, not generic placeholders).
* ``on_blocked="fail_fast"`` short-circuits at first hit.
* ``on_blocked="skip_warn"`` collects every block.
* Binary asset (``UnicodeDecodeError``) → ``decision="pass"`` (no false
  positive against the regex pattern set).
* ``raise_or_collect`` branch: ``project_shared`` raises
  :class:`PrivacyBlockedError`, others return a typed skip tuple.
* Unreadable file: ``OSError`` raises :class:`PrivacyScanReadError`
  (umbrella :class:`PrivacyScanError`). The CLI translates these to
  ``click.ClickException`` at the boundary; web/MCP surfaces catch
  the umbrella class and render their native error shape — pinning
  the domain class here keeps the deeper module Click-free (see
  #895 P2 review).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem import privacy
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context.privacy_scan import (
    FileScan,
    PrivacyBlockedError,
    PrivacyScanError,
    PrivacyScanReadError,
    raise_or_collect,
    scan_artifact_tree,
)

# AKIA fixture per feedback_force_unsafe_redaction_valve_only.md — clean
# strings would let force_unsafe=False scans pass and produce a false
# negative on every block-related assertion.
SECRET = "api_key=AKIA1234567890ABCDEF"
CLEAN = "this is just regular prose, no secrets here\n"


@pytest.fixture(autouse=True)
def _reset_privacy_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _seed_skill(skill_root: Path, *, leak_in: str | None = None) -> Path:
    """Create a skill tree under ``skill_root``. ``leak_in`` is the
    relative path that gets the AKIA secret (``"SKILL.md"``,
    ``"scripts/leak.sh"``, etc.). ``None`` → all files clean."""
    skill_root.mkdir(parents=True, exist_ok=True)
    files = {
        "SKILL.md": "---\nname: foo\n---\nbody\n",
        "scripts/leak.sh": "#!/bin/bash\necho hello\n",
        "references/notes.md": "see README\n",
    }
    if leak_in is not None:
        files[leak_in] = files.get(leak_in, "") + SECRET + "\n"
    for rel, content in files.items():
        path = skill_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_root


class TestSingleFileScan:
    def test_clean_file_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.md"
        path.write_text(CLEAN, encoding="utf-8")
        result = scan_artifact_tree(path, surface="t", scope="user", project_root=tmp_path)
        assert len(result.decisions) == 1
        assert result.decisions[0].decision == "pass"
        assert result.blocked == []

    def test_secret_in_file_blocked_user(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.md"
        path.write_text(f"---\nname: foo\n---\n{SECRET}\n", encoding="utf-8")
        result = scan_artifact_tree(path, surface="t", scope="user", project_root=tmp_path)
        # Positive marker: at least one blocked entry referencing the file.
        assert len(result.blocked) == 1
        blocked = result.blocked[0]
        assert blocked.path == path
        assert blocked.decision == "blocked"
        assert blocked.hits_count >= 1

    def test_secret_in_file_blocked_project_shared_no_force(self, tmp_path: Path) -> None:
        # force_unsafe is hardcoded False in scan_artifact_tree (ADR §5),
        # so project_shared with a hit produces "blocked" (not
        # "blocked_project_shared" — that only fires with force_unsafe=True).
        path = tmp_path / "agent.md"
        path.write_text(f"{SECRET}\n", encoding="utf-8")
        result = scan_artifact_tree(
            path, surface="t", scope="project_shared", project_root=tmp_path
        )
        assert len(result.blocked) == 1
        assert result.blocked[0].decision == "blocked"


class TestTreeWalk:
    def test_secret_in_scripts_blocked(self, tmp_path: Path) -> None:
        # The realistic skill leak: secret in scripts/leak.sh while
        # SKILL.md is clean. Tree walk must catch it (regression of the
        # pre-PR-E3 behavior where only SKILL.md was scanned).
        skill = _seed_skill(tmp_path / "foo", leak_in="scripts/leak.sh")
        result = scan_artifact_tree(
            skill, surface="t", scope="project_shared", project_root=tmp_path
        )
        assert len(result.blocked) == 1, result.blocked
        assert result.blocked[0].path == skill / "scripts/leak.sh"

    def test_clean_skill_passes(self, tmp_path: Path) -> None:
        skill = _seed_skill(tmp_path / "foo", leak_in=None)
        result = scan_artifact_tree(
            skill, surface="t", scope="project_shared", project_root=tmp_path
        )
        assert result.blocked == []
        # All three files visited and all passed.
        assert len(result.decisions) == 3
        assert all(d.decision == "pass" for d in result.decisions)


class TestOnBlockedDispatch:
    def test_fail_fast_returns_at_first_hit(self, tmp_path: Path) -> None:
        # Two leaks — fail_fast must return after the first.
        skill = _seed_skill(tmp_path / "foo", leak_in="scripts/leak.sh")
        # Add a second leak in references/.
        (skill / "references" / "notes.md").write_text(f"see README\n{SECRET}\n", encoding="utf-8")
        result = scan_artifact_tree(
            skill,
            surface="t",
            scope="project_shared",
            project_root=tmp_path,
            on_blocked="fail_fast",
        )
        # fail_fast: blocked has exactly 1 entry. decisions cap at the
        # number of files scanned BEFORE the abort (sorted iteration is
        # deterministic per Path.rglob).
        assert len(result.blocked) == 1
        assert len(result.decisions) <= 3

    def test_skip_warn_collects_all(self, tmp_path: Path) -> None:
        skill = _seed_skill(tmp_path / "foo", leak_in="scripts/leak.sh")
        (skill / "references" / "notes.md").write_text(f"see README\n{SECRET}\n", encoding="utf-8")
        result = scan_artifact_tree(
            skill,
            surface="t",
            scope="user",
            project_root=tmp_path,
            on_blocked="skip_warn",
        )
        # skip_warn: every leaked file appears in blocked.
        assert len(result.blocked) == 2, result.blocked
        # decisions visits all files (3 total).
        assert len(result.decisions) == 3


class TestBinaryAssetGracefulPass:
    def test_png_bytes_pass_without_false_positive(self, tmp_path: Path) -> None:
        # Embed an "AKIA..." pattern inside arbitrary binary bytes —
        # UnicodeDecodeError on UTF-8 decode → decision="pass" path.
        # This also pins the failure mode: if read_text were upgraded to
        # decode with errors="replace", the secret would leak through and
        # this test would catch it.
        path = tmp_path / "logo.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + SECRET.encode() + b"\xff\xfe\x00")
        result = scan_artifact_tree(
            path, surface="t", scope="project_shared", project_root=tmp_path
        )
        assert len(result.decisions) == 1
        assert result.decisions[0].decision == "pass"
        assert result.blocked == []


class TestUnreadableFileFailsClosed:
    """Pre-PR-E4 review the OSError branch was conflated with
    UnicodeDecodeError and recorded as ``pass``. PR-E4's ``_stage_move``
    can rename a ``chmod 000`` canonical file into staging without
    reading bytes, so silent-pass would have promoted unreadable
    secret-bearing content into project_shared. These pins check that
    ``OSError`` from ``read_text`` raises a scope-aware
    :class:`PrivacyScanReadError` rather than recording a pass — and
    that the umbrella :class:`PrivacyScanError` catches it for non-CLI
    surface translation.
    """

    def test_oserror_raises_scan_read_error_project_shared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "agent.md"
        path.write_text(CLEAN, encoding="utf-8")

        real_read_text = Path.read_text

        def explode(self: Path, *args: object, **kwargs: object) -> str:
            if self == path:
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", explode)

        with pytest.raises(PrivacyScanReadError) as exc_info:
            scan_artifact_tree(path, surface="t", scope="project_shared", project_root=tmp_path)
        # Umbrella class must catch — non-CLI surfaces (web, MCP) rely
        # on this for their HTTP 422 / tool-error translation.
        assert isinstance(exc_info.value, PrivacyScanError)
        msg = exc_info.value.message
        assert "cannot read" in msg
        assert "agent.md" in msg
        assert "scope='project_shared'" in msg
        assert exc_info.value.path == path
        assert exc_info.value.scope == "project_shared"
        # Negative — the message must NOT echo the file's content even
        # if the file were readable mid-flight.
        assert CLEAN.strip() not in msg

    def test_oserror_raises_scan_read_error_user_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-closed posture is uniform across scopes for unreadable
        # files. Skip-warn semantics apply to *known* pattern hits at
        # user/project_local — an unreadable file is not a known hit,
        # it's an unknown.
        path = tmp_path / "agent.md"
        path.write_text(CLEAN, encoding="utf-8")
        real_read_text = Path.read_text

        def explode(self: Path, *args: object, **kwargs: object) -> str:
            if self == path:
                raise OSError(5, "Input/output error", str(self))
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", explode)

        with pytest.raises(PrivacyScanReadError) as exc_info:
            scan_artifact_tree(path, surface="t", scope="user", project_root=tmp_path)
        assert "scope='user'" in exc_info.value.message

    def test_unreadable_in_tree_walk_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Tree walk should hard-abort on the first unreadable file and
        # NOT continue scanning siblings — the staging tree as a whole
        # is poisoned the moment any leaf cannot be inspected.
        skill_root = _seed_skill(tmp_path / "foo")
        unreadable = skill_root / "scripts" / "leak.sh"
        real_read_text = Path.read_text

        def explode(self: Path, *args: object, **kwargs: object) -> str:
            if self == unreadable:
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", explode)

        with pytest.raises(PrivacyScanReadError) as exc_info:
            scan_artifact_tree(
                skill_root,
                surface="t",
                scope="project_shared",
                project_root=tmp_path,
            )
        assert "leak.sh" in exc_info.value.message


class TestRaiseOrCollect:
    def test_project_shared_raises(self) -> None:
        blocked = FileScan(Path("/tmp/foo/agent.md"), "blocked", 1)
        with pytest.raises(PrivacyBlockedError) as exc_info:
            raise_or_collect(blocked, scope="project_shared", kind="agent", artifact_name="foo")
        # Umbrella class — non-CLI surfaces catch this for translation.
        assert isinstance(exc_info.value, PrivacyScanError)
        # Click coupling removed: the raise must not be a ``click.ClickException``
        # so the web/MCP generic exception handlers don't turn it into a 500.
        import click as _click

        assert not isinstance(exc_info.value, _click.ClickException)
        # Structured fields are populated so non-CLI surfaces can build
        # their own error shape without re-parsing the message.
        assert exc_info.value.scope == "project_shared"
        assert exc_info.value.kind == "agent"
        assert exc_info.value.artifact_name == "foo"
        assert exc_info.value.blocked == blocked
        msg = exc_info.value.message
        assert "Gate A" in msg
        assert "agent.md" in msg
        assert "1 privacy pattern hit" in msg
        # Remediation hint mentions migrate + project_local. Plural
        # "agents" (not "agent") because the migrate CLI only accepts
        # the plural asset-type choices (#895 P2 review #3 fold —
        # the pre-fix hint embedded the singular and tripped Click's
        # invalid-choice error when users followed the remediation).
        assert "mm context migrate agents foo" in msg
        assert "mm context migrate agent foo" not in msg
        assert "--to project_local" in msg
        # Positive contract: the wording is "fan-out", not "import"
        # (parallel to apply_gate_a's import-side wording).
        assert "fan-out to scope='project_shared' rejected" in msg

    def test_user_returns_skip_tuple(self) -> None:
        blocked = FileScan(Path("/tmp/foo/agent.md"), "blocked", 2)
        code, reason = raise_or_collect(blocked, scope="user", kind="agent", artifact_name="foo")
        assert code == skip_codes.PRIVACY_BLOCKED
        assert "agent.md" in reason
        assert "2 pattern hit" in reason

    def test_project_local_returns_skip_tuple(self) -> None:
        blocked = FileScan(Path("/tmp/foo/agent.md"), "blocked", 1)
        code, reason = raise_or_collect(
            blocked, scope="project_local", kind="agent", artifact_name="foo"
        )
        assert code == skip_codes.PRIVACY_BLOCKED
        # Negative marker: never raised for non-project_shared.
        assert reason  # non-empty

    def test_blocked_project_shared_decision_maps_to_distinct_code(self) -> None:
        # Should never happen in sync (force_unsafe=False), but the
        # mapping is verified so a future regression in
        # scan_artifact_tree's force_unsafe defaulting cannot silently
        # pick the wrong code.
        blocked = FileScan(Path("/tmp/foo/agent.md"), "blocked_project_shared", 1)
        code, _reason = raise_or_collect(blocked, scope="user", kind="agent", artifact_name="foo")
        assert code == skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED


# Audit-log shape (scope marker / kind=sync field) is exercised inside
# ``test_privacy_scope_gate.py`` against the underlying
# ``enforce_write_guard``. ``scan_artifact_tree`` is a thin wrapper that
# forwards to that helper; a duplicated audit-log assertion here would
# only re-pin behavior already locked elsewhere.

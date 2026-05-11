"""Service-level tests for ``sync_atomic_artifact`` (issue #900).

Exercises the shared engine through a minimal stub adapter so each failure
mode is asserted ONCE at the engine layer instead of once per artifact in
``test_context_agents.py`` / ``test_context_commands.py``. Per the issue's
acceptance criteria, new tests cover: privacy block, unreadable source,
``NO_PROJECT_FANOUT_FOR_RUNTIME``, unknown runtime, strict drop, and
override bytes.

The stub adapter uses ``artifact_label="agents"`` so the override resolver
(:func:`memtomem.context.override.resolve`) finds the override format in
``OVERRIDE_FORMATS[("agents", "<vendor>")]`` for the override-bytes tests.
Canonical files live at ``<project_root>/.memtomem/agents/<name>.txt`` —
the stub's ``list_canonical`` reads .txt files so they bypass the real
markdown parser entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context._sync_atomic import (
    AtomicSyncAdapter,
    AtomicSyncResult,
    StrictDropError,
    sync_atomic_artifact,
)
from memtomem.context.privacy_scan import PrivacyBlockedError

# Trip the same privacy pattern existing scope/privacy tests use.
SECRET = "api_key=AKIA1234567890ABCDEF"


# ── Minimal stub adapter ─────────────────────────────────────────────


@dataclass
class StubItem:
    name: str
    body: str


class StubParseError(ValueError):
    """Raised by the stub parser on malformed text."""


def _canonical_root(project_root: Path, scope: str) -> Path:
    """Scope-aware canonical root — mirrors ``canonical_artifact_dir("agents", ...)``."""
    if scope == "project_local":
        return project_root / ".memtomem" / "agents.local"
    if scope == "user":
        return project_root / ".memtomem-user" / "agents"
    return project_root / ".memtomem" / "agents"


def _stub_list_canonical(
    project_root: Path, *, scope: Any = "project_shared"
) -> list[tuple[Path, str]]:
    """Read all ``.txt`` files at the scope-appropriate canonical root."""
    root = _canonical_root(project_root, scope)
    if not root.exists():
        return []
    return [(p, "flat") for p in sorted(root.glob("*.txt"))]


def _stub_parse_text(text: str, *, source: Path, layout: Any = "flat") -> StubItem:
    if text.startswith("BROKEN:"):
        raise StubParseError(f"unparseable preamble in {source.name}")
    return StubItem(name=source.stem, body=text)


class StubGenerator:
    """Per-runtime generator that mirrors the AGENT_GENERATORS contract.

    ``target_subdir`` controls where the fan-out lands. Set ``no_fanout=True``
    to simulate a ``RUNTIME_FANOUT_TABLE`` ``None`` entry (engine should emit
    ``NO_PROJECT_FANOUT_FOR_RUNTIME``).

    ``drops_for`` decides per-item whether ``render`` reports dropped fields.
    Default ``None`` → never drops. Pass a callable to drop conditionally;
    pass a list to drop the same fields for every item (back-compat shape).
    """

    def __init__(
        self,
        target_subdir: str,
        *,
        no_fanout: bool = False,
        drops_for: Any = None,
    ) -> None:
        self.target_subdir = target_subdir
        self.no_fanout = no_fanout
        # Normalize: None → no-op, list → constant, callable → as-is.
        if drops_for is None:
            self._drops_for = lambda _item: []
        elif callable(drops_for):
            self._drops_for = drops_for
        else:
            self._drops_for = lambda _item, _d=list(drops_for): list(_d)

    def target_file(
        self, project_root: Path, name: str, *, scope: Any = "project_shared"
    ) -> Path | None:
        if self.no_fanout:
            return None
        return project_root / self.target_subdir / f"{name}.txt"

    def render(self, item: StubItem) -> tuple[str, list[str]]:
        return item.body, self._drops_for(item)


def _make_adapter(generators: dict[str, StubGenerator]) -> AtomicSyncAdapter[StubItem]:
    """Bundle the stub callables behind the engine's adapter contract."""
    return AtomicSyncAdapter(
        kind="agent",
        artifact_label="agents",
        list_canonical=_stub_list_canonical,
        parse_canonical_text=_stub_parse_text,
        parse_error_type=StubParseError,
        name_of=lambda item: item.name,
        generators=generators,
    )


def _seed_canonical(tmp_path: Path, name: str, body: str, *, scope: str = "project_shared") -> Path:
    """Write a canonical .txt at the scope-appropriate root."""
    root = _canonical_root(tmp_path, scope)
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.txt"
    p.write_text(body, encoding="utf-8")
    return p


# ── 1. Privacy block ──────────────────────────────────────────────────


class TestPrivacyBlock:
    def test_project_shared_raises_before_phase2(self, tmp_path: Path) -> None:
        """Phase 1 Gate A hits a secret → raise BEFORE any write lands on disk.

        Mirrors the all-or-nothing atomicity contract in
        :func:`memtomem.context._sync_atomic.sync_atomic_artifact`. The
        engine raises :class:`PrivacyBlockedError` directly; surface
        layers (CLI / MCP / Web) translate that into their own error
        types (``click.ClickException``, HTTP 422, MCP tool error).
        """
        _seed_canonical(tmp_path, "alpha", "clean body\n")
        _seed_canonical(tmp_path, "leaky", f"body with {SECRET}\n")
        adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out")})

        with pytest.raises(PrivacyBlockedError):
            sync_atomic_artifact(adapter, tmp_path, scope="project_shared")

        # No partial fan-out should have landed on disk — Phase 1 raise
        # fires before Phase 2 ever opens a write.
        out_dir = tmp_path / ".stub-out"
        if out_dir.exists():
            assert list(out_dir.iterdir()) == []

    def test_project_local_collects_skip(self, tmp_path: Path) -> None:
        """``project_local`` scope: privacy block emits skip, other artifacts proceed."""
        _seed_canonical(tmp_path, "alpha", "clean body\n", scope="project_local")
        _seed_canonical(tmp_path, "leaky", f"body with {SECRET}\n", scope="project_local")
        adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out")})

        result = sync_atomic_artifact(adapter, tmp_path, scope="project_local")

        assert isinstance(result, AtomicSyncResult)
        gen_names = {name for _, p in result.generated for name in [p.stem]}
        assert "alpha" in gen_names
        assert "leaky" not in gen_names
        skip_names = {name for name, _, _ in result.skipped}
        assert "leaky" in skip_names
        block_codes = {code for _, _, code in result.skipped}
        assert (
            skip_codes.PRIVACY_BLOCKED in block_codes
            or skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED in block_codes
        )


# ── 2. Unreadable source ──────────────────────────────────────────────


class TestUnreadableSource:
    def test_emits_parse_error_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError on canonical read → PARSE_ERROR skip with ``unreadable:`` reason."""
        _seed_canonical(tmp_path, "alpha", "clean\n", scope="project_local")
        unreadable = _seed_canonical(tmp_path, "beta", "would be clean\n", scope="project_local")

        original_read_bytes = Path.read_bytes

        def fake_read_bytes(self: Path) -> bytes:
            if self == unreadable:
                raise OSError("EACCES: permission denied")
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

        adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out")})
        result = sync_atomic_artifact(adapter, tmp_path, scope="project_local")

        # alpha succeeds, beta is skipped with PARSE_ERROR + "unreadable:" reason.
        assert any(p.stem == "alpha" for _, p in result.generated)
        beta_skips = [s for s in result.skipped if s[0] == "beta.txt"]
        assert len(beta_skips) == 1
        _, reason, code = beta_skips[0]
        assert code == skip_codes.PARSE_ERROR
        assert reason.startswith("unreadable:")


# ── 3. Unknown runtime ────────────────────────────────────────────────


class TestUnknownRuntime:
    def test_unknown_runtime_skipped(self, tmp_path: Path) -> None:
        """Caller passes a runtime name not in ``adapter.generators`` → UNKNOWN_RUNTIME skip."""
        _seed_canonical(tmp_path, "alpha", "clean\n", scope="project_local")
        adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out")})

        result = sync_atomic_artifact(
            adapter, tmp_path, runtimes=["stub_rt", "nope"], scope="project_local"
        )

        assert ("nope", "unknown runtime", skip_codes.UNKNOWN_RUNTIME) in result.skipped
        # The known runtime still ran.
        assert any(name == "stub_rt" for name, _ in result.generated)


# ── 4. NO_PROJECT_FANOUT_FOR_RUNTIME ──────────────────────────────────


class TestNoProjectFanout:
    def test_target_file_returns_none_emits_skip(self, tmp_path: Path) -> None:
        """Generator returns ``None`` from ``target_file`` → NO_PROJECT_FANOUT_FOR_RUNTIME skip.

        Mirrors the ``RUNTIME_FANOUT_TABLE`` contract: a (runtime, scope)
        tuple that has no fan-out by design emits a typed skip rather
        than raising.
        """
        _seed_canonical(tmp_path, "alpha", "clean\n", scope="project_local")
        adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out", no_fanout=True)})

        result = sync_atomic_artifact(adapter, tmp_path, scope="project_local")

        assert result.generated == []
        no_fanout_skips = [
            s for s in result.skipped if s[2] == skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME
        ]
        assert len(no_fanout_skips) == 1
        name, reason, _ = no_fanout_skips[0]
        assert name == "alpha"
        assert "no fan-out for runtime stub_rt" in reason


# ── 5. Strict drop partial-write boundary ─────────────────────────────


class TestStrictDrop:
    def test_raises_after_earlier_writes(self, tmp_path: Path) -> None:
        """Engine-level mirror of #908's pin for issue #900.

        Phase 2 raises StrictDropError on the first dropping canonical, but
        canonicals iterated earlier in sorted-name order have already been
        written. No mkstemp temp file remains for the failing write.
        """
        # Canonicals iterate sorted by Path: alpha-minimal < beta-full.
        _seed_canonical(tmp_path, "alpha-minimal", "alpha body\n", scope="project_local")
        _seed_canonical(tmp_path, "beta-full", "beta body\n", scope="project_local")

        # The stub generator drops ``["tools"]`` ONLY for beta-full; render
        # is invoked in Phase 2 sorted order, so alpha-minimal renders
        # cleanly and lands on disk, then beta-full's render triggers the
        # raise. This is the partial-write boundary pinned by #908.
        adapter = _make_adapter(
            {
                "stub_rt": StubGenerator(
                    ".stub-out",
                    drops_for=lambda item: ["tools"] if item.name == "beta-full" else [],
                )
            }
        )

        with pytest.raises(StrictDropError):
            sync_atomic_artifact(adapter, tmp_path, on_drop="error", scope="project_local")

        out_dir = tmp_path / ".stub-out"
        # alpha-minimal landed before the raise.
        assert (out_dir / "alpha-minimal.txt").is_file()
        # beta-full did NOT land — and no mkstemp temp file was left
        # behind (atomic_write_text uses prefix=f".{path.name}.").
        assert not (out_dir / "beta-full.txt").exists()
        assert list(out_dir.glob(".beta-full.txt.*.tmp")) == []

    def test_legacy_strict_flag_promotes_to_error(self, tmp_path: Path) -> None:
        """``strict=True`` is equivalent to ``on_drop="error"`` when on_drop is default."""
        _seed_canonical(tmp_path, "alpha", "body\n", scope="project_local")
        adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out", drops_for=["something"])})

        with pytest.raises(StrictDropError):
            sync_atomic_artifact(adapter, tmp_path, strict=True, scope="project_local")

    def test_on_drop_warn_logs_but_writes(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``on_drop="warn"`` logs the drop but still writes the file."""
        _seed_canonical(tmp_path, "alpha", "body\n", scope="project_local")
        adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out", drops_for=["lost"])})

        with caplog.at_level("WARNING", logger="memtomem.context._sync_atomic"):
            result = sync_atomic_artifact(adapter, tmp_path, on_drop="warn", scope="project_local")

        assert (tmp_path / ".stub-out" / "alpha.txt").is_file()
        assert any("dropped" in r.message for r in caplog.records)
        assert result.dropped == [("stub_rt", "alpha", ["lost"])]


# ── 6. Override bytes ─────────────────────────────────────────────────


def _seed_override(
    tmp_path: Path,
    name: str,
    vendor: str,
    body: str,
    *,
    scope: str = "project_shared",
    ext: str = "md",
) -> Path:
    """Write an override file at the scope-appropriate layout.

    Layout: ``<canonical_artifact_dir("agents", scope, project_root)>/
    <name>/overrides/<vendor>.<ext>``
    """
    override_dir = _canonical_root(tmp_path, scope) / name / "overrides"
    override_dir.mkdir(parents=True, exist_ok=True)
    p = override_dir / f"{vendor}.{ext}"
    p.write_text(body, encoding="utf-8")
    return p


class TestOverrideBytes:
    def test_override_bytes_replace_render_output(self, tmp_path: Path) -> None:
        """Render emits canonical body; override bytes overwrite the final file.

        Phase 2: atomic_write_text(canonical) → atomic_write_bytes(override).
        Final file contents == override bytes, not render output.
        """
        _seed_canonical(tmp_path, "alpha", "canonical body\n", scope="project_local")
        _seed_override(tmp_path, "alpha", "claude", "OVERRIDDEN\n", scope="project_local")

        adapter = _make_adapter({"claude_agents": StubGenerator(".stub-out")})

        result = sync_atomic_artifact(adapter, tmp_path, scope="project_local")

        assert len(result.generated) == 1
        out_file = tmp_path / ".stub-out" / "alpha.txt"
        assert out_file.read_text(encoding="utf-8") == "OVERRIDDEN\n"

    def test_override_secret_raises_under_project_shared(self, tmp_path: Path) -> None:
        """Override bytes carrying a secret trip Gate A in Phase 1.

        Closes the override-bytes TOCTOU: the bytes that get scanned are
        the same bytes Phase 2 promotes. Override is read ONCE.
        """
        _seed_canonical(tmp_path, "alpha", "canonical body (clean)\n")
        _seed_override(tmp_path, "alpha", "claude", f"override body with {SECRET}\n")

        adapter = _make_adapter({"claude_agents": StubGenerator(".stub-out")})

        with pytest.raises(PrivacyBlockedError):
            sync_atomic_artifact(adapter, tmp_path, scope="project_shared")

        # Even though canonical was clean and target dir is set, no file
        # should have landed — Phase 1 raise fires before any write.
        out_dir = tmp_path / ".stub-out"
        if out_dir.exists():
            assert list(out_dir.iterdir()) == []


# ── Sanity: empty canonical root ──────────────────────────────────────


def test_empty_canonical_root_returns_no_canonical_skip(tmp_path: Path) -> None:
    """Empty canonical → single ``NO_CANONICAL_ROOT`` skip, empty generated."""
    adapter = _make_adapter({"stub_rt": StubGenerator(".stub-out")})

    result = sync_atomic_artifact(adapter, tmp_path, scope="project_local")

    assert result.generated == []
    assert result.dropped == []
    assert len(result.skipped) == 1
    name, reason, code = result.skipped[0]
    assert name == "<all>"
    assert code == skip_codes.NO_CANONICAL_ROOT
    assert "no canonical agents" in reason


def test_strict_drop_errors_are_sister_subclasses() -> None:
    """``agents.StrictDropError`` and ``commands.StrictDropError`` must stay distinct.

    The engine raises through ``adapter.strict_drop_error_type``; each module's
    wrapper binds its own subclass. If someone "simplifies" the adapter back to
    an engine-default base, ``except agents.StrictDropError`` would accidentally
    catch a commands raise (and vice versa) — existing tests pinning
    ``pytest.raises(<module>.StrictDropError)`` would still pass because they
    each import their own catch class. This guards that adapter regression.
    """
    from memtomem.context.agents import StrictDropError as AgentsStrictDrop
    from memtomem.context.commands import StrictDropError as CommandsStrictDrop

    assert AgentsStrictDrop is not CommandsStrictDrop
    assert not issubclass(AgentsStrictDrop, CommandsStrictDrop)
    assert not issubclass(CommandsStrictDrop, AgentsStrictDrop)

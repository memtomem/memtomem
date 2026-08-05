"""Tests for ``mm index`` (``memtomem.cli.indexing``).

Pins the streaming + progress-bar conversion (issue #656). The non-stream
``IndexEngine.index_path`` path is exercised by ``test_cli_index_noop_e2e``;
this module focuses on the stream-converted ``_index`` direct-CLI flow:

1. **Stream → summary** — events flow through and the legacy
   ``Indexed N file(s): N new, N unchanged, N deleted (Nms)`` summary line
   is preserved verbatim. Scripts may grep this output, so the format string
   is a stable interface.
2. **Ctrl-C → resume hint** — a ``KeyboardInterrupt`` mid-stream prints the
   yellow ``Cancelled. Resume with: mm index <path>`` line and exits cleanly.
3. **--namespace / --force pass-through** — both flags reach
   ``index_path_stream`` unchanged.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import click

from memtomem.cli.indexing import (
    _IndexingStatsError,
    _index,
    _make_indexer,
    _partition_drain_failures,
    _print_drain_result,
)
from memtomem.indexing.debounce import DrainResult
from memtomem.models import IndexingStats


def _make_complete_event(
    *,
    total_files: int = 1,
    indexed: int = 1,
    skipped: int = 0,
    deleted: int = 0,
    duration_ms: float = 12.0,
    errors: list[str] | None = None,
    retryable_errors: list[str] | None = None,
) -> dict:
    event = {
        "type": "complete",
        "total_files": total_files,
        "total_chunks": indexed + skipped,
        "indexed_chunks": indexed,
        "skipped_chunks": skipped,
        "deleted_chunks": deleted,
        "duration_ms": duration_ms,
        "errors": errors or [],
    }
    if retryable_errors is not None:
        event["retryable_errors"] = retryable_errors
    return event


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch, *, events: list[dict], record: dict | None = None
) -> None:
    """Patch ``cli_components`` so ``index_path_stream`` yields ``events`` and
    optionally records the kwargs it was called with into ``record``."""

    class _FakeEngine:
        async def index_path_stream(self, path, *args, **kwargs):
            if record is not None:
                record["path"] = path
                record["args"] = args
                record["kwargs"] = dict(kwargs)
            for evt in events:
                if isinstance(evt, BaseException):
                    raise evt
                yield evt

    class _FakeComp:
        index_engine = _FakeEngine()

    @asynccontextmanager  # type: ignore[misc]
    async def _fake_components():
        yield _FakeComp()

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake_components)


class TestIndexStreamConversion:
    def test_stream_complete_event_renders_legacy_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``progress`` + ``complete`` events flow through and the printed
        summary line matches the pre-stream ``Indexed N file(s): N new,
        N unchanged, N deleted (Nms)`` shape verbatim. Pinned because
        scripts may grep this output."""
        target = tmp_path / "memories"
        target.mkdir()
        (target / "a.md").write_text("# memo a\n", encoding="utf-8")
        (target / "b.md").write_text("# memo b\n", encoding="utf-8")

        events = [
            {
                "type": "progress",
                "file": str(target / "a.md"),
                "files_done": 1,
                "files_total": 2,
                "indexed": 3,
                "skipped": 0,
            },
            {
                "type": "progress",
                "file": str(target / "b.md"),
                "files_done": 2,
                "files_total": 2,
                "indexed": 2,
                "skipped": 1,
            },
            _make_complete_event(total_files=2, indexed=5, skipped=1, deleted=0, duration_ms=42.0),
        ]
        _install_fake_engine(monkeypatch, events=events)

        asyncio.run(_index(str(target), recursive=True, force=False, namespace=None))
        out = capsys.readouterr().out
        assert "Indexed 2 file(s): 5 new, 1 unchanged, 0 deleted (42ms)" in out

    def test_stream_errors_render_red_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Per-file error strings carried in the ``complete`` event's
        ``errors`` list are rendered with the same ``  ERROR: …`` prefix
        as the pre-stream implementation. Empty list = no error lines."""
        target = tmp_path / "memories"
        target.mkdir()
        (target / "broken.md").write_text("# x\n", encoding="utf-8")

        events = [
            _make_complete_event(
                total_files=1,
                indexed=0,
                skipped=0,
                deleted=0,
                errors=["broken.md: embedder OOM"],
            ),
        ]
        _install_fake_engine(monkeypatch, events=events)

        with pytest.raises(click.exceptions.Exit) as exc_info:
            asyncio.run(_index(str(target), recursive=True, force=False, namespace=None))
        assert exc_info.value.exit_code == 1
        out = capsys.readouterr().out
        assert "ERROR: broken.md: embedder OOM" in out

    def test_stream_retryable_errors_are_labeled_once_with_retry_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "memories"
        target.mkdir()
        permanent = "broken.md: embedder OOM"
        retryable = "transient.md: chunk store unavailable"
        events = [
            _make_complete_event(
                total_files=2,
                indexed=1,
                errors=[permanent, retryable],
                retryable_errors=[retryable],
            )
        ]
        _install_fake_engine(monkeypatch, events=events)

        with pytest.raises(click.exceptions.Exit) as exc_info:
            asyncio.run(_index(str(target), recursive=True, force=False, namespace=None))

        assert exc_info.value.exit_code == 1
        out = capsys.readouterr().out
        assert "Indexed 2 file(s): 1 new, 0 unchanged, 0 deleted (12ms)" in out
        assert f"ERROR: {permanent}" in out
        assert f"ERROR (retryable): {retryable}" in out
        assert out.count(retryable) == 1
        assert "re-run the same `mm index` command once the chunk store is reachable" in out


class TestDebounceIndexClassification:
    @pytest.mark.parametrize(
        ("errors", "retryable_errors", "blocked_files", "expected_retryable"),
        [
            (("broken.md: malformed",), (), 0, False),
            (("busy.md: store unavailable",), ("busy.md: store unavailable",), 0, True),
            (
                ("broken.md: malformed", "busy.md: store unavailable"),
                ("busy.md: store unavailable",),
                0,
                True,
            ),
            ((), (), 1, False),
        ],
    )
    def test_make_indexer_preserves_stats_retryability(
        self,
        tmp_path: Path,
        errors: tuple[str, ...],
        retryable_errors: tuple[str, ...],
        blocked_files: int,
        expected_retryable: bool,
    ) -> None:
        target = tmp_path / "memo.md"
        target.write_text("# memo\n", encoding="utf-8")
        stats = IndexingStats(
            total_files=1,
            total_chunks=0,
            indexed_chunks=0,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=1.0,
            errors=errors,
            blocked_files=blocked_files,
            retryable_errors=retryable_errors,
        )

        class _FakeEngine:
            async def index_file(self, *args, **kwargs):
                return stats

        indexer = _make_indexer(SimpleNamespace(index_engine=_FakeEngine()))
        with pytest.raises(_IndexingStatsError) as exc_info:
            asyncio.run(indexer(str(target), None, False))

        assert exc_info.value.retryable is expected_retryable

    def test_partition_preserves_duplicate_occurrences(self) -> None:
        duplicate = ("/tmp/a.md", "same message")
        permanent, retryable = _partition_drain_failures(
            [duplicate, duplicate],
            [duplicate],
        )
        assert permanent == [duplicate]
        assert retryable == [duplicate]

    def test_human_output_separates_pending_permanent_and_exhausted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pending = ("/tmp/pending.md", "store unavailable")
        permanent = ("/tmp/broken.md", "malformed")
        exhausted = ("/tmp/exhausted.md", "store unavailable")
        result = DrainResult(
            errors=[pending],
            retryable_errors=[pending],
            dropped=[permanent, exhausted],
            retryable_dropped=[exhausted],
        )

        _print_drain_result(result, as_json=False, label="Flushed")

        out = capsys.readouterr().out
        assert "ERROR (retryable): /tmp/pending.md" in out
        assert "queued entries will drain later" in out
        assert "Dropped (permanent): 1" in out
        assert "fix the cause, then: mm index /tmp/broken.md" in out
        assert "Dropped after 5 retryable attempts: 1" in out
        exhausted_line = next(line for line in out.splitlines() if "/tmp/exhausted.md" in line)
        assert "once the chunk store is reachable" in exhausted_line
        assert "fix the cause" not in exhausted_line

    def test_json_output_keeps_legacy_arrays_and_adds_retryable_subsets(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        duplicate = ("/tmp/a.md", "same message")
        result = DrainResult(
            errors=[duplicate, duplicate],
            retryable_errors=[duplicate],
            dropped=[duplicate, duplicate],
            retryable_dropped=[duplicate],
            remaining=1,
        )

        _print_drain_result(result, as_json=True, label="Flushed")

        payload = json.loads(capsys.readouterr().out)
        assert len(payload["errors"]) == 2
        assert len(payload["retryable_errors"]) == 1
        assert len(payload["dropped"]) == 2
        assert len(payload["retryable_dropped"]) == 1
        assert payload["errors"][0] == {"path": "/tmp/a.md", "message": "same message"}


class TestIndexKeyboardInterrupt:
    def test_keyboard_interrupt_prints_resume_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ctrl-C inside the stream surfaces the yellow ``Cancelled. Resume
        with: mm index <abs-path>`` hint instead of a traceback. The path
        is the resolved absolute path so the printed command is
        copy-pasteable from any cwd."""
        target = tmp_path / "memories"
        target.mkdir()
        (target / "a.md").write_text("# memo\n", encoding="utf-8")

        events = [
            {
                "type": "progress",
                "file": str(target / "a.md"),
                "files_done": 1,
                "files_total": 2,
                "indexed": 1,
                "skipped": 0,
            },
            KeyboardInterrupt(),
        ]
        _install_fake_engine(monkeypatch, events=events)

        # ``_index`` converts the interrupt to the standard shell exit 130.
        with pytest.raises(click.exceptions.Exit) as exc_info:
            asyncio.run(_index(str(target), recursive=True, force=False, namespace=None))
        assert exc_info.value.exit_code == 130
        out = capsys.readouterr().out
        assert "Cancelled" in out
        assert f"mm index {target.resolve()}" in out
        # Must NOT print the success summary line — the run was cancelled.
        assert "Indexed " not in out


class TestIndexFlagPassthrough:
    def test_namespace_and_force_reach_index_path_stream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``--namespace`` and ``--force`` flags forward verbatim into
        the engine's ``index_path_stream`` call. Recording stub captures
        kwargs so a future refactor that drops one of these on the floor
        fails here instead of in production."""
        target = tmp_path / "memories"
        target.mkdir()
        (target / "a.md").write_text("# memo\n", encoding="utf-8")

        record: dict = {}
        events = [_make_complete_event(total_files=1, indexed=1)]
        _install_fake_engine(monkeypatch, events=events, record=record)

        asyncio.run(_index(str(target), recursive=False, force=True, namespace="work"))
        kwargs = record["kwargs"]
        assert kwargs.get("recursive") is False
        assert kwargs.get("force") is True
        assert kwargs.get("namespace") == "work"

    def test_default_recursive_true_no_namespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default invocation: ``recursive=True``, ``force=False``, and
        ``namespace=None`` are all forwarded verbatim to the engine. Pinned
        so the defaults stay aligned with the click option declarations."""
        target = tmp_path / "memories"
        target.mkdir()
        (target / "a.md").write_text("# memo\n", encoding="utf-8")

        record: dict = {}
        events = [_make_complete_event(total_files=1, indexed=1)]
        _install_fake_engine(monkeypatch, events=events, record=record)

        asyncio.run(_index(str(target), recursive=True, force=False, namespace=None))
        kwargs = record["kwargs"]
        assert kwargs.get("recursive") is True
        assert kwargs.get("force") is False
        assert kwargs.get("namespace") is None


class TestIndexBarLengthFromDiscovery:
    """Issue #743: progress-bar length comes from the engine's ``discovery``
    event, not from a pre-computed ``.md``-only ``rglob`` walk.
    """

    def test_no_collect_seed_scale_call_during_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``mm index`` must not invoke ``_collect_seed_scale`` (the
        wizard's ``.md``-only counter). Regression guard for the duplicate
        ``rglob`` walk that #743 removed — the engine's ``discovery`` event
        is the only bar-length source on the indexing path now."""
        target = tmp_path / "src"
        target.mkdir()
        (target / "module.py").write_text("def f():\n    return 1\n")

        called = {"count": 0}

        def _spy(p):
            called["count"] += 1
            return (0, 0)

        monkeypatch.setattr("memtomem.cli._index_progress._collect_seed_scale", _spy)

        events = [
            {"type": "discovery", "files_total": 1},
            _make_complete_event(total_files=1, indexed=1),
        ]
        _install_fake_engine(monkeypatch, events=events)

        asyncio.run(_index(str(target), recursive=True, force=False, namespace=None))
        assert called["count"] == 0, (
            "_collect_seed_scale must not be called from mm index — "
            "discovery event is the bar-length source (#743)"
        )

    def test_bar_renders_for_non_md_corpus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``mm index ./src/`` (no ``.md`` files) must still render a
        progress bar. Pre-#743 the bar was suppressed because
        ``_collect_seed_scale`` returned 0 → ``expected_total=0`` → click
        rendered nothing. With discovery driving length, the bar appears
        with the engine's actual file count."""
        target = tmp_path / "src"
        target.mkdir()
        (target / "module.py").write_text("def f():\n    return 1\n")

        captured: dict = {}
        events = [
            {"type": "discovery", "files_total": 1},
            {
                "type": "progress",
                "file": str(target / "module.py"),
                "files_done": 1,
                "files_total": 1,
                "indexed": 1,
                "skipped": 0,
            },
            _make_complete_event(total_files=1, indexed=1),
        ]
        _install_fake_engine(monkeypatch, events=events)

        import click

        from memtomem.cli import _index_progress as ip_mod

        real_progressbar = click.progressbar

        def _spy(*args, **kwargs):
            bar = real_progressbar(*args, **kwargs)
            captured["bar"] = bar
            captured["initial_length"] = kwargs.get("length")
            return bar

        # Helper imports click at module scope — patch there.
        monkeypatch.setattr(ip_mod.click, "progressbar", _spy)

        asyncio.run(_index(str(target), recursive=True, force=False, namespace=None))
        assert "bar" in captured, "bar must be created from discovery event"
        assert captured["initial_length"] == 1

"""Shared test helpers for memtomem tests."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import TargetScope
from memtomem.context._runtime_targets import runtime_fanout_root
from memtomem.context.migrate import _NON_SKILL_FANOUT_SUFFIX
from memtomem.context.scope_resolver import ArtifactKind
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext

# Slack allowed for assertions that pin a *remaining* share of a lock budget against
# its nominal ceiling. The shared-deadline pattern computes
# ``deadline = time.monotonic() + BUDGET`` and later ``deadline - time.monotonic()``;
# when both clock reads land in the same tick, ``fl(t0 + BUDGET) - t0`` exceeds
# ``BUDGET`` by up to half a ULP, so an exact ``<= BUDGET`` upper bound fails with
# values like ``30.000000000000227``. The excess is ULP-scale (~2e-13 s at these
# uptimes), which means the two reads returned the *same* value; Windows CI hits
# that far more often than Linux/macOS, so a Windows-only failure with this
# signature is arithmetic, not a platform bug and not a dependency change (#2073).
#
# 1e-6 is more than six orders of magnitude below the budgets in use, so a genuine overrun
# (``5.001``, ``30.5``) still fails the assertion. Import this constant rather than
# re-typing the literal: a future sweep for the vulnerable shape has to be able to
# find every site from one symbol.
BUDGET_TOLERANCE_S = 1e-6

# Developer ``MEMTOMEM_*`` env vars that would override an in-test config
# and break hermeticity. Add new top-level config sections here when they
# grow an env-var binding.
_MEMTOMEM_ENV_VARS = (
    "MEMTOMEM_EMBEDDING__PROVIDER",
    "MEMTOMEM_EMBEDDING__MODEL",
    "MEMTOMEM_EMBEDDING__DIMENSION",
    "MEMTOMEM_STORAGE__SQLITE_PATH",
    "MEMTOMEM_INDEXING__MEMORY_DIRS",
    "MEMTOMEM_SCHEDULER__ENABLED",
)


_WEB_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static"

# #1517: context-gateway.js was split into ordered classic-script fragments.
# Whole-file readers (test_i18n, test_web_a11y, test_qa_audit_pins,
# test_web_mode) concatenate them in index.html LOAD ORDER via
# ``ctx_gateway_js_text()`` so a content assertion still sees the full module
# regardless of which fragment holds the line. This tuple MUST NOT be sorted:
# the ``_langchange_listener_body`` sentinel slice in test_i18n depends on the
# overview fragment (langchange #1 + the ``// Sync All button`` marker)
# preceding the conflict fragment (langchange #2), which alphabetical order
# would invert. Keep in sync with the ``<script>`` order in index.html and with
# ``CTX_GATEWAY_SCRIPTS`` in tests-js/setup/jsdom-app.mjs.
CTX_GATEWAY_JS_FILES = (
    "context-gateway-core.js",
    "context-gateway-controls.js",
    "context-gateway-overview.js",
    "context-gateway-list.js",
    "context-gateway-conflict.js",
    "context-gateway-detail.js",
    "context-gateway-actions.js",
    "context-gateway-pull.js",
    "context-gateway-global.js",
)


def ctx_gateway_js_text() -> str:
    """Concatenate the context-gateway.js fragment(s) in index.html load order."""
    return "\n".join(
        (_WEB_STATIC_DIR / name).read_text(encoding="utf-8") for name in CTX_GATEWAY_JS_FILES
    )


def isolate_memtomem_env(monkeypatch) -> None:
    """Strip ``MEMTOMEM_*`` env vars and stub out ``load_config_overrides``
    so a freshly constructed ``Mem2MemConfig`` is not mutated by the
    developer's ``~/.memtomem/config.json`` or shell environment.

    Used directly by tests that construct their own components (e.g. the
    LangGraph adapter cases). The ``bm25_only_components`` fixture in
    ``conftest.py`` calls this internally for fixture-based callers.
    """
    for var in _MEMTOMEM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)


def set_home(monkeypatch: pytest.MonkeyPatch, path: Path | str) -> None:
    """Override the home directory for tests that exercise ``Path.home()``
    or ``Path("~/...").expanduser()``.

    On POSIX, ``Path.home()`` reads ``HOME``. On Windows it reads
    ``USERPROFILE`` first (then ``HOMEDRIVE``+``HOMEPATH``), so a bare
    ``monkeypatch.setenv("HOME", ...)`` is silently ignored on Windows
    runners and tests end up reading/writing the real user home. Setting
    both env vars is harmless on POSIX (stdlib ignores ``USERPROFILE``)
    and correct on Windows.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


#: Written to stdout by every poisoned click prompt entry point. Any test that
#: pins a ``--json`` stdout payload fails on its own assertions if this leaks.
CLICK_PROMPT_SENTINEL = "<click-prompt-machinery-entered>"


def poison_click_prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make click's prompt machinery loud and stdout-polluting.

    ``cli._prompts.confirm(err=True)`` promises it never enters click's prompt
    machinery — that promise is what keeps ``--json`` stdout a single JSON
    document (#1640). The original pins simulated the hazard by forcing click's
    Windows prompt branch with ``monkeypatch.setattr(click.termui, "WIN", True)``;
    click 8.5 deleted that branch (``termui`` no longer imports ``WIN`` and
    ``_readline_prompt`` has no platform fork), so the pins raised
    ``AttributeError``. Poisoning the machinery pins the same promise without
    depending on a platform branch that may not exist.

    Returns a list that records the name of every prompt entry point reached,
    so a caller asserts ``calls == []``. Each entry point also writes
    :data:`CLICK_PROMPT_SENTINEL` to stdout, which independently breaks the
    caller's ``json.loads(result.stdout)``.

    Patched:

    * ``click.confirm`` / ``click.prompt`` — the package attributes
      ``_prompts`` actually resolves at call time.
    * ``click.termui._readline_prompt`` — the chokepoint both ``termui.confirm``
      and ``termui.prompt`` route through on click 8.4 *and* 8.5, covering the
      callers that bypass the package alias (option prompting, click core's own
      aliases, a direct ``click.termui.confirm``). It is a private name, but
      ``monkeypatch.setattr`` defaults to ``raising=True``, so a rename in a
      future click fails these tests loudly instead of silently disarming them.

    ``termui.visible_prompt_func`` is deliberately *not* patched: these pins run
    under ``CliRunner``, whose ``isolation()`` reassigns it inside ``invoke``
    and restores it afterwards, so a poison installed there is overwritten
    before the command runs — armed-looking and dead.
    """
    import click
    import click.termui

    calls: list[str] = []

    def _poison(name: str):
        def _fire(*args, **kwargs):
            calls.append(name)
            sys.stdout.write(CLICK_PROMPT_SENTINEL)
            return False if name == "click.confirm" else ""

        return _fire

    monkeypatch.setattr(click, "confirm", _poison("click.confirm"))
    monkeypatch.setattr(click, "prompt", _poison("click.prompt"))
    monkeypatch.setattr(click.termui, "_readline_prompt", _poison("click.termui._readline_prompt"))
    return calls


class StubCtx:
    """Minimal stand-in for MCP ``Context`` so MCP tools can be invoked
    directly from tests without a live FastMCP session.
    """

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


def seed_multi_runtime(
    project_root: Path,
    kind: ArtifactKind,
    name: str,
    per_runtime: dict[str, str],
    *,
    scope: TargetScope = "project_shared",
) -> dict[str, Path]:
    """Seed the same artifact ``name`` into several runtime dirs with divergent bytes.

    Resolves each runtime directory through :func:`runtime_fanout_root` so the
    fixture can never drift from ``RUNTIME_FANOUT_TABLE`` — a runtime whose
    fan-out is ``None`` for this (kind, scope) is skipped. ``per_runtime`` maps
    a runtime label (``"claude"``, ``"gemini"``, ``"codex"``, ``"kimi"``) to the
    body that runtime's copy should carry.

    The on-disk filename matches what each runtime actually uses so a seeded
    fixture is one the extract engines would really read (else the test could
    false-green): skills land as ``<name>/SKILL.md`` (tree layout); every other
    kind uses :data:`memtomem.context.migrate._NON_SKILL_FANOUT_SUFFIX` — the
    same per-(kind, runtime) suffix table the fan-out / cleanup paths use, so
    e.g. codex agents are ``.toml`` and kimi agents ``.yaml``, not ``.md``. An
    unmapped (kind, runtime) raises rather than silently writing a file the
    engine will ignore.

    Returns the map of runtime label → the file that was written, for assertion.
    """
    written: dict[str, Path] = {}
    for runtime, body in per_runtime.items():
        runtime_dir = runtime_fanout_root(kind, runtime, scope, project_root)
        if runtime_dir is None:
            continue
        if kind == "skills":
            dest = runtime_dir / name / "SKILL.md"
        else:
            try:
                suffix = _NON_SKILL_FANOUT_SUFFIX[kind][runtime]
            except KeyError as exc:
                raise ValueError(
                    f"seed_multi_runtime: no filename convention for ({kind}, {runtime})"
                ) from exc
            dest = runtime_dir / f"{name}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        written[runtime] = dest
    return written


def make_chunk(
    content: str = "test content",
    tags: tuple[str, ...] = (),
    namespace: str = "default",
    source: str = "test.md",
    heading: tuple[str, ...] = (),
    embedding: list[float] | None = None,
    origin: str | None = None,
) -> Chunk:
    """Create a test Chunk with sensible defaults."""
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
            heading_hierarchy=tuple(heading),
            origin=origin,
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=embedding if embedding is not None else [0.1] * 1024,
    )


def fake_context_windows(chunks: dict[Path, list[Chunk]] | list[Chunk]):
    """In-memory stand-in for ``StorageBackend.get_context_windows``.

    Takes either a ``{source_file: chunks}`` map or a flat list of one file's
    chunks (grouped by their own ``source_file``).

    Slices the physical order the caller hands it and counts with the real
    :func:`memtomem.search.visibility.neighbor_visible`, so a test that fakes
    storage still exercises the visibility rule rather than a second opinion
    about it. What it cannot certify is that the SQL says the same thing —
    ``test_context_window_storage.py`` runs both against one matrix for that.

    Mirrors the contract exactly: neighbours come back **unfiltered** (the
    caller screens them), and both counts **exclude the anchor**, which the
    caller adds back (#2236).
    """
    from memtomem.search.visibility import neighbor_visible
    from memtomem.storage.base import ContextWindowRows

    if isinstance(chunks, dict):
        chunks_by_source = {Path(k): v for k, v in chunks.items()}
    else:
        chunks_by_source = {}
        for chunk in chunks:
            chunks_by_source.setdefault(Path(chunk.metadata.source_file), []).append(chunk)

    async def _get_context_windows(source_file, anchor_ids, window, **spec):
        chunks = chunks_by_source.get(Path(source_file), [])
        positions = {c.id: i for i, c in enumerate(chunks)}
        visible = [neighbor_visible(c, **spec) for c in chunks]
        out: dict = {}
        for anchor_id in anchor_ids:
            pos = positions.get(anchor_id)
            if pos is None:
                out[anchor_id] = None
                continue
            out[anchor_id] = ContextWindowRows(
                before=list(chunks[max(0, pos - window) : pos]) if window > 0 else [],
                after=list(chunks[pos + 1 : pos + 1 + window]) if window > 0 else [],
                visible_before=sum(1 for i in range(pos) if visible[i]),
                visible_total_excluding_anchor=sum(
                    1 for i, v in enumerate(visible) if v and i != pos
                ),
            )
        return out

    return _get_context_windows

"""Shared test helpers for memtomem tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import TargetScope
from memtomem.context._runtime_targets import runtime_fanout_root
from memtomem.context.migrate import _NON_SKILL_FANOUT_SUFFIX
from memtomem.context.scope_resolver import ArtifactKind
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext

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
) -> Chunk:
    """Create a test Chunk with sensible defaults."""
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
            heading_hierarchy=tuple(heading),
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=embedding if embedding is not None else [0.1] * 1024,
    )

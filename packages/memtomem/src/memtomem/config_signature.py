"""Detect on-disk config changes without reading the files.

Both long-running processes need to notice that someone else edited
``~/.memtomem/config.json`` or a ``config.d`` fragment: ``mm web`` reloads its
whole config from it (``web/hot_reload.py``), and the MCP server reconciles its
watched index roots from it (``server/context.py``, issue #2186). The stat-level
signature and the strict config rebuild live here so neither has to import the
other — ``server/`` deliberately depends on nothing under ``web/``.

Nothing here holds state: callers keep their own last-seen signature.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from memtomem.config import (
    MUTABLE_FIELDS,
    EmbeddingConfig,
    Mem2MemConfig,
    _config_d_path,
    _override_path,
    load_config_d,
    load_config_overrides,
)

logger = logging.getLogger(__name__)

# A tuple of (path_str, mtime_ns) pairs sorted by path, including a sentinel for
# the config.d directory itself so newly created / removed fragments are
# detected even when their own files weren't touched.
Signature = tuple[tuple[str, int], ...]


def current_signature() -> Signature:
    """Build the composite ``(path, mtime_ns)`` signature for config state.

    Includes ``~/.memtomem/config.json`` plus every ``~/.memtomem/config.d/
    *.json`` entry plus the directory mtime itself. Missing files contribute
    a ``-1`` mtime rather than being skipped, so their appearance or removal
    still changes the signature.

    Cost is one ``stat`` for ``config.json``, one ``iterdir`` of ``config.d``,
    and one ``stat`` per fragment — no file is read. Callers on a hot path
    (every MCP tool call, every web request) pay that and nothing more.
    """
    entries: list[tuple[str, int]] = []

    override = _override_path()
    entries.append((str(override), _stat_mtime_ns(override)))

    d_path = _config_d_path()
    entries.append((str(d_path), _stat_mtime_ns(d_path) if d_path.is_dir() else -1))
    try:
        fragments = (
            sorted(p for p in d_path.iterdir() if p.is_file() and p.suffix == ".json")
            if d_path.is_dir()
            else []
        )
    except OSError as exc:
        # The directory can be replaced or removed between the ``is_dir`` and
        # the walk. Callers sample this on a hot path (every MCP tool call), so
        # a concurrent directory swap must not raise into whatever they were
        # doing — the directory's own mtime entry above already moved, so the
        # signature still reads as changed and the next sample retries.
        logger.warning("Listing %s during a config-change check failed: %s", d_path, exc)
        fragments = []
    for frag in fragments:
        entries.append((str(frag), _stat_mtime_ns(frag)))

    return tuple(entries)


def _stat_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1
    except OSError as exc:
        logger.warning("stat(%s) failed during config-change check: %s", path, exc)
        return -1


def get_config_mtime_ns() -> int:
    """Return the current ``config.json`` mtime in ns, or ``-1`` if missing."""
    return _stat_mtime_ns(_override_path())


def build_fresh_config(
    *,
    migrate: bool = True,
    strict_fragments: bool = False,
    strict_overrides: bool = True,
    quiet: bool = False,
    validate_profile: bool = True,
) -> Mem2MemConfig:
    """Replay the canonical config load path.

    Defaults (+ env via pydantic-settings) → ``config.d`` fragments →
    ``config.json`` overrides. By default, raises on ``config.json`` JSON / OS
    errors so a hot reader can switch to fail-closed mode.

    ``migrate=False`` skips the legacy ``auto_discover`` → explicit
    ``memory_dirs`` migration, which **writes** ``config.json``. A caller that
    only wants to look at the current config — as opposed to one standing in
    for startup — must pass it: re-reading on a hot path would otherwise turn
    into a surprise write to the user's config file.

    :func:`load_config_overrides` itself swallows parse errors with a warning
    log — startup historically wanted to boot with defaults rather than crash
    on a bad user file. A reader that re-reads the file mid-process needs the
    opposite: silently getting defaults back would read a broken file as
    "every setting was reset", which the next save would then write back, and
    which a roots-reconciler would act on by unwatching every directory. So
    ``config.json`` is pre-parsed here before delegating.

    ``strict_overrides`` covers two levels. The pre-parse below is file-level:
    the whole override file is unreadable or is not an object. The same flag
    is then forwarded to the loader as section-level strictness, so a section
    whose cross-field validation fails raises instead of silently leaving the
    pre-override baseline in place — a stale ``embedding.dimension`` used to
    put a re-reader on ``provider="none"`` with nothing but a log line
    (#2385 item 3). Field-level skips stay tolerant in both modes; see
    :func:`load_config_overrides` for that boundary.

    ``strict_overrides=False`` preserves the historical tolerant startup
    behavior: a malformed ``config.json`` is logged and ignored, and a
    rejected section is recorded in ``cfg.load_diagnostics`` for the status
    and config surfaces to report. It is for handshake-time service
    discovery, where refusing the whole MCP handshake would also make
    repair/status tools unreachable.

    ``strict_fragments=True`` extends strictness to the ``config.d``
    fragments, which :func:`load_config_d` otherwise logs and skips one at a
    time. It is passed down to the loader rather than pre-checked here, so it
    covers every skip the loader can make — a fragment whose JSON parses but
    whose ``memory_dirs`` is a string, say — and does it on the same read the
    config is built from, leaving no window for a write to land between a
    validation pass and the real one.
    ``quiet=True`` suppresses fragment warnings, matching ``load_config_d``;
    validation failures and override diagnostics keep their existing behavior.

    ``validate_profile=False`` skips final profile validation after the file
    layers, so diagnostics can display invalid file settings. Construction-time
    environment validation is unchanged. Runtime readers must retain the default.
    """
    return _build_config(
        migrate=migrate,
        strict_fragments=strict_fragments,
        strict_overrides=strict_overrides,
        quiet=quiet,
        validate_profile=validate_profile,
    )


def rebase_embedding(identity: EmbeddingConfig, pins: EmbeddingConfig) -> EmbeddingConfig:
    """Rebuild an embedding section on *identity*, keeping *pins*' editable fields.

    Non-mutable inputs (provider, model, variant, …) come from *identity*; only
    the explicitly set ``MUTABLE_FIELDS["embedding"]`` values come from *pins*.
    Revalidating from explicit inputs regenerates identity-derived defaults
    (E5's ``onnx_batch_size``) instead of carrying another identity's.
    """
    mutable = MUTABLE_FIELDS["embedding"]
    inputs = {
        key: value
        for key, value in identity.model_dump(exclude_unset=True).items()
        if key not in mutable
    }
    inputs.update(
        {key: value for key, value in pins.model_dump(exclude_unset=True).items() if key in mutable}
    )
    return EmbeddingConfig.model_validate(inputs)


def _build_config(
    *,
    migrate: bool = False,
    strict_fragments: bool = False,
    strict_overrides: bool = False,
    quiet: bool = False,
    include_overrides: bool = True,
    validate_profile: bool = True,
    embedding_context: EmbeddingConfig | None = None,
) -> Mem2MemConfig:
    """Shared layer replay, with normalization after the final profile selection.

    Comparands omit config.json but can retain the caller's selected model.
    Only non-mutable embedding inputs come from that context: copying its
    batch size would make a user pin compare equal to itself. Revalidate from
    explicit inputs so generated E5 defaults remain unpinned.
    """
    override = _override_path()
    if include_overrides and strict_overrides and override.exists():
        # Strict pre-parse — raises on malformed JSON / OS errors.
        parsed = json.loads(override.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"config overrides in {override} must be a JSON object")

    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=quiet, strict=strict_fragments)
    if include_overrides:
        load_config_overrides(cfg, migrate=migrate, strict=strict_overrides)
    if embedding_context is not None:
        cfg.embedding = rebase_embedding(embedding_context, cfg.embedding)
    from memtomem.embedding.profiles import apply_e5_defaults, fill_e5_defaults

    if include_overrides and validate_profile:
        apply_e5_defaults(cfg)
    else:
        # Omitted overrides may be what makes the runtime config valid;
        # diagnostic views also need to display invalid file settings.
        # These values are inspection/comparison inputs, not a runnable stack.
        fill_e5_defaults(cfg)
    return cfg

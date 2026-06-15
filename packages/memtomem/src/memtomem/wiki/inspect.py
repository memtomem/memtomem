"""Read-only inspection of wiki assets — ``mm wiki <type> {diff, lint}``.

Companion to :mod:`memtomem.wiki.override` (the *writer*, which seeds
override files). This module is the *reader*: ``diff_override`` reports how
a committed override diverges from the canonical-rendered baseline, and
``lint_asset`` validates that a wiki asset is well-formed and installable.
Neither helper mutates the wiki — both are pure functions over the working
tree (ADR-0008 PR-D).

Both reuse PR-C / PR-D machinery rather than re-deriving it, so ``diff`` /
``lint`` / ``override`` can never disagree about what the runtime sees:

- :func:`memtomem.wiki.override.render_seed_bytes` produces the canonical
  baseline an override is measured against — the same bytes
  ``mm wiki <type> override`` would seed — and reports the ``dropped``
  canonical fields the vendor format cannot represent.
- :data:`memtomem.context._names.OVERRIDE_FORMATS` /
  :func:`memtomem.context._names.validate_name` are the single source of
  truth for the per-(type, vendor) extension and name rules.
- ``parse_canonical_{agent,command}`` is the canonical structural check
  ``lint`` runs (agents / commands only — skills are byte-copied, so there
  is nothing to parse).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memtomem.context._names import (
    OVERRIDE_FORMATS,
    InvalidNameError,
    validate_name,
)
from memtomem.wiki.override import canonical_asset_file, render_seed_bytes
from memtomem.wiki.store import WikiStore

__all__ = [
    "LintFinding",
    "LintReport",
    "OverrideContent",
    "OverrideDiff",
    "diff_override",
    "lint_asset",
    "read_override",
]

LintLevel = Literal["error", "warning"]


@dataclass(frozen=True)
class OverrideDiff:
    """Outcome of :func:`diff_override`.

    ``override_path`` is the absolute ``<wiki>/<type>/<name>/overrides/<vendor>.<ext>``.
    ``exists`` is whether that file is present; when ``False`` the caller has
    not seeded an override yet (``in_sync`` / ``diff_lines`` are empty).
    ``in_sync`` is ``True`` when the override is byte-identical to the
    canonical render. ``diff_lines`` is the unified diff (canonical → override)
    and is empty whenever ``in_sync`` or ``not exists``. ``dropped`` carries
    the vendor-unrepresentable canonical fields so the caller can warn the
    same way ``mm wiki <type> override`` does.
    """

    override_path: Path
    exists: bool
    in_sync: bool
    diff_lines: list[str]
    dropped: list[str]


@dataclass(frozen=True)
class OverrideContent:
    """Outcome of :func:`read_override` — the working-tree bytes of a vendor
    override, for the in-browser editor's read pane (ADR-0027 Editor-A).

    ``override_path`` is the absolute ``<wiki>/<type>/<name>/overrides/<vendor>.<ext>``.
    ``exists`` is whether that file is present; when ``False`` the user has not
    seeded an override yet, ``content`` is empty and ``mtime_ns`` is ``0`` (the
    editor opens a blank pane to author one). ``content`` is decoded UTF-8 with
    ``errors="replace"`` — parity with :func:`diff_override`, which never crashes
    on a mis-encoded override (``lint`` is where bad encodings are flagged).
    ``mtime_ns`` is the file's ``st_mtime_ns`` (``0`` when absent) — the
    optimistic-concurrency token the editor's ``PUT`` re-checks.
    """

    override_path: Path
    exists: bool
    content: str
    mtime_ns: int


@dataclass(frozen=True)
class LintFinding:
    """A single lint result. ``level`` gates the exit code — any ``"error"``
    means the asset is not well-formed; ``"warning"`` is advisory (e.g. a
    vendor that drops a canonical field) and leaves the report ``ok``."""

    level: LintLevel
    message: str


@dataclass(frozen=True)
class LintReport:
    """Aggregate of :func:`lint_asset`. ``ok`` is ``False`` iff any finding
    is an error — that is the signal ``mm wiki <type> lint`` turns into a
    non-zero exit so the verb is usable as a CI gate."""

    asset_type: str
    name: str
    findings: list[LintFinding]

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)


def _override_rel(asset_type: str, name: str, vendor: str, ext: str) -> str:
    """POSIX-style ``<type>/<name>/overrides/<vendor>.<ext>`` for messages."""
    return f"{asset_type}/{name}/overrides/{vendor}.{ext}"


def diff_override(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str,
) -> OverrideDiff:
    """Diff the committed override against the freshly rendered canonical.

    The baseline is what ``mm wiki <type> override`` would seed *now*, so the
    diff surfaces both the user's hand-edits and any canonical drift since the
    override was seeded — exactly the "what will install fan out vs. what did I
    pin" question. Reads the working tree (uncommitted edits included), not a
    git commit.

    Raises (for the CLI to classify, never a traceback):
    :class:`memtomem.wiki.store.WikiNotFoundError` (no wiki),
    :class:`FileNotFoundError` (missing canonical),
    :class:`memtomem.context._names.InvalidNameError` (bad name),
    :class:`NotImplementedError` (``("commands", "codex")`` placeholder),
    :class:`ValueError` (unregistered ``(asset_type, vendor)``).
    """
    store.require_exists()
    validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
    if fmt is None:
        raise ValueError(f"no override format registered for ({asset_type!r}, {vendor!r})")
    _, ext = fmt
    override_path = store.root / asset_type / name / "overrides" / f"{vendor}.{ext}"

    # ``render_seed_bytes`` re-validates the name and raises FileNotFoundError /
    # NotImplementedError for missing-canonical / unsupported-vendor; let those
    # propagate so the CLI classifies them identically to ``override``.
    baseline, dropped = render_seed_bytes(store, asset_type, name, vendor)

    if not override_path.is_file():
        return OverrideDiff(
            override_path=override_path,
            exists=False,
            in_sync=False,
            diff_lines=[],
            dropped=dropped,
        )

    override_bytes = override_path.read_bytes()
    if override_bytes == baseline:
        return OverrideDiff(
            override_path=override_path,
            exists=True,
            in_sync=True,
            diff_lines=[],
            dropped=dropped,
        )

    rel = _override_rel(asset_type, name, vendor, ext)
    # ``errors="replace"`` so a binary / mis-encoded override still produces a
    # (lossy) diff instead of crashing; ``lint`` is where bad encodings are
    # flagged as errors.
    diff_lines = list(
        difflib.unified_diff(
            baseline.decode("utf-8", errors="replace").splitlines(keepends=True),
            override_bytes.decode("utf-8", errors="replace").splitlines(keepends=True),
            fromfile=f"{asset_type}/{name}: canonical (rendered for {vendor})",
            tofile=rel,
        )
    )
    return OverrideDiff(
        override_path=override_path,
        exists=True,
        in_sync=False,
        diff_lines=diff_lines,
        dropped=dropped,
    )


def read_override(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str,
) -> OverrideContent:
    """Read the working-tree override bytes for the in-browser editor.

    Requires the **canonical asset to exist** (raises :class:`FileNotFoundError`
    otherwise) so the editor never opens a read pane for a phantom asset — the
    same gate :func:`memtomem.wiki.override.write_override` enforces, since
    :meth:`WikiStore.list_assets` treats any ``<type>/<name>/`` dir as an asset.
    A missing *override* file is NOT an error: it returns ``exists=False`` so the
    editor can author a new one from a blank pane.

    Raises the same classify-able exceptions as :func:`diff_override`:
    :class:`memtomem.wiki.store.WikiNotFoundError` (no wiki),
    :class:`FileNotFoundError` (missing canonical),
    :class:`memtomem.context._names.InvalidNameError` (bad name),
    :class:`ValueError` (unregistered ``(asset_type, vendor)``).
    """
    store.require_exists()
    validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
    if fmt is None:
        raise ValueError(f"no override format registered for ({asset_type!r}, {vendor!r})")
    canonical = canonical_asset_file(store, asset_type, name)
    if not canonical.is_file():
        raise FileNotFoundError(f"wiki has no {asset_type}/{name} canonical at {canonical}")
    _, ext = fmt
    override_path = store.root / asset_type / name / "overrides" / f"{vendor}.{ext}"
    if not override_path.is_file():
        return OverrideContent(override_path=override_path, exists=False, content="", mtime_ns=0)
    stat = override_path.stat()
    return OverrideContent(
        override_path=override_path,
        exists=True,
        content=override_path.read_bytes().decode("utf-8", errors="replace"),
        mtime_ns=stat.st_mtime_ns,
    )


def _lint_canonical_parse(asset_type: str, canonical: Path) -> list[LintFinding]:
    """Structural parse of the canonical agent / command (skills are byte
    copies — nothing to parse). ``AgentParseError`` / ``CommandParseError``
    both subclass ``ValueError``; ``OSError`` covers a read race."""
    # Function-body imports dodge the wiki ↔ context import cycle, same as
    # ``render_seed_bytes`` (``context.install`` already imports ``wiki.store``).
    try:
        if asset_type == "agents":
            from memtomem.context.agents import parse_canonical_agent

            parse_canonical_agent(canonical, layout="dir")
        elif asset_type == "commands":
            from memtomem.context.commands import parse_canonical_command

            parse_canonical_command(canonical, layout="dir")
    except (ValueError, OSError) as exc:
        return [LintFinding("error", f"canonical does not parse: {exc}")]
    return []


def _scan_overrides(asset_type: str, asset_dir: Path) -> tuple[list[str], list[str]]:
    """Classify the files in ``<asset_dir>/overrides/`` against the registered
    formats.

    Returns ``(valid_vendors, stray_filenames)``:

    * ``valid_vendors`` — files whose ``<vendor>.<ext>`` matches the registered
      :data:`OVERRIDE_FORMATS` extension for this asset type. These are the
      vendors ``install`` would actually fan out.
    * ``stray_filenames`` — non-``.bak`` files that do *not* match a registered
      ``<vendor>.<ext>`` (e.g. a wrong-extension ``gemini.md`` where commands
      use ``.toml``). The runtime resolver silently ignores these
      (``context.override.resolve`` only loads the exact registered name), so a
      user who hand-named an override would otherwise see no effect and no
      warning — lint flags them.

    ``.bak`` siblings left by ``override --force`` are ignored.
    """
    overrides = asset_dir / "overrides"
    if not overrides.is_dir():
        return [], []
    valid: set[str] = set()
    stray: list[str] = []
    for p in sorted(overrides.iterdir()):
        if not p.is_file() or p.suffix == ".bak":
            continue
        vendor = p.stem
        ext = p.suffix.lstrip(".")
        fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
        if fmt is not None and fmt[1] == ext:
            valid.add(vendor)
        else:
            stray.append(p.name)
    return sorted(valid), stray


def _lint_vendor(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str,
    asset_dir: Path,
) -> list[LintFinding]:
    """Representability + override health for one vendor."""
    out: list[LintFinding] = []
    fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
    if fmt is None:
        out.append(
            LintFinding("error", f"no override format registered for ({asset_type}, {vendor})")
        )
        return out
    _, ext = fmt
    override_path = asset_dir / "overrides" / f"{vendor}.{ext}"

    try:
        _baseline, dropped = render_seed_bytes(store, asset_type, name, vendor)
    except NotImplementedError as exc:
        # e.g. ("commands", "codex") — no generator. This target can never be
        # rendered or installed, so it is always an error: an explicit
        # ``--vendor`` asked the representability question and the answer is
        # "no", and a discovered override file is unusable. (Callers skip the
        # vendor pass entirely when the canonical itself is broken, so this is
        # the only "unrenderable" case that reaches here.)
        out.append(LintFinding("error", str(exc)))
        return out
    except FileNotFoundError:
        # Missing canonical is already reported by the canonical-presence check;
        # don't double-report it per vendor.
        return out
    except (ValueError, OSError) as exc:
        # Defense in depth: ``lint_asset`` skips this pass when the canonical
        # did not parse, but a render that fails for any other reason
        # (AgentParseError / CommandParseError both subclass ValueError) must
        # surface as a finding, never a leaked traceback through ``mm wiki lint``.
        out.append(LintFinding("error", f"cannot render for {vendor!r}: {exc}"))
        return out

    for field_name in dropped:
        out.append(
            LintFinding(
                "warning",
                f"vendor {vendor!r} will not represent canonical field {field_name!r}",
            )
        )

    if override_path.is_file():
        try:
            override_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            out.append(LintFinding("error", f"override {vendor}.{ext} is not valid UTF-8"))
    return out


def lint_asset(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str | None = None,
) -> LintReport:
    """Validate a single wiki asset is well-formed and installable.

    Checks, in order:

    1. **Name** — :func:`validate_name`. An invalid name makes every path
       join below unsafe, so this short-circuits to a single error.
    2. **Canonical** — the ``SKILL.md`` / ``agent.md`` / ``command.md`` is
       present and (agents / commands) parses.
    3. **Stray override files** — anything in ``overrides/`` that is not a
       registered ``<vendor>.<ext>`` (nor a ``.bak``) is an error: the runtime
       resolver would silently ignore it. Scanned even when the canonical is
       broken, since a misnamed override is independent of canonical health.
    4. **Vendors** — only when the canonical parsed (rendering against a broken
       canonical would just re-raise the error already reported). ``vendor``
       given: just that vendor; otherwise every vendor with a valid override
       file on disk. Each is checked for a registered format, renderability,
       and (overrides) UTF-8 validity. Fields the vendor format drops are
       advisory warnings; an unrenderable target (no generator) is an error.

    Raises :class:`memtomem.wiki.store.WikiNotFoundError` if no wiki exists;
    all other conditions are returned as :class:`LintFinding` rows so the CLI
    can print them and pick an exit code from :attr:`LintReport.ok`.
    """
    store.require_exists()

    try:
        validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    except InvalidNameError as exc:
        return LintReport(asset_type, name, [LintFinding("error", str(exc))])

    asset_dir = store.root / asset_type / name
    findings: list[LintFinding] = []
    canonical_ok = True

    if asset_type == "skills":
        if not (asset_dir / "SKILL.md").is_file():
            findings.append(LintFinding("error", f"missing canonical {asset_type}/{name}/SKILL.md"))
            canonical_ok = False
    else:
        stem = asset_type[:-1]  # "agents" → "agent", "commands" → "command"
        canonical = asset_dir / f"{stem}.md"
        if not canonical.is_file():
            findings.append(
                LintFinding("error", f"missing canonical {asset_type}/{name}/{stem}.md")
            )
            canonical_ok = False
        else:
            parse_findings = _lint_canonical_parse(asset_type, canonical)
            findings.extend(parse_findings)
            canonical_ok = not parse_findings

    valid_vendors, stray = _scan_overrides(asset_type, asset_dir)
    for filename in stray:
        findings.append(
            LintFinding(
                "error",
                f"unexpected file in {asset_type}/{name}/overrides/: {filename} "
                "(not a registered <vendor>.<ext>)",
            )
        )

    if canonical_ok:
        targets = [vendor] if vendor is not None else valid_vendors
        for v in targets:
            findings.extend(_lint_vendor(store, asset_type, name, v, asset_dir))

    return LintReport(asset_type, name, findings)

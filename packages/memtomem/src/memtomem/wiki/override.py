"""Wiki-side override seeding — writes into ``~/.memtomem-wiki/<type>/<name>/overrides/``.

Companion to :mod:`memtomem.context.override` (which is the project-side
*resolver*). The seed helper here is what ``mm wiki <type> override``
calls to produce the initial bytes the user then edits.

- Skills are byte-identical across vendors, so the seed is simply the
  canonical ``SKILL.md`` and ``dropped`` is always ``[]``.
- Agents and commands feed the canonical through the vendor's renderer
  in :data:`AGENT_GENERATORS` / :data:`COMMAND_GENERATORS` so the seed
  bytes match what the vendor's runtime would actually emit. The renderer
  also reports which canonical fields the target format cannot represent;
  callers (``mm wiki <type> override``) surface those to the user as a
  stderr warning so the override editor knows what the runtime won't see.
  This reuses PR-C's layout-aware ``parse_canonical_agent`` /
  ``parse_canonical_command`` rather than widening the generator API surface.
- ``("commands", "codex")`` is a permanent placeholder row in
  :data:`OVERRIDE_FORMATS` (no ``codex_commands`` generator); seeding
  raises :class:`NotImplementedError` with a diagnostic message.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from memtomem.context._atomic import atomic_write_bytes
from memtomem.context._names import OVERRIDE_FORMATS, validate_name
from memtomem.wiki.store import WikiStore

__all__ = ["OverrideExistsError", "SeedResult", "render_seed_bytes", "seed_override"]


class OverrideExistsError(RuntimeError):
    """Raised when an override file already exists and ``force`` was not given."""


@dataclass(frozen=True)
class SeedResult:
    """Outcome of seeding an override file.

    ``path`` points at the freshly written ``<wiki>/<type>/<name>/overrides/<vendor>.<ext>``.
    ``dropped`` lists frontmatter / canonical field names the vendor renderer
    could not represent in its output format — always ``[]`` for skills
    (byte-copy of canonical), populated for agents / commands depending on
    vendor (e.g. gemini agents drop ``skills`` / ``isolation``).
    """

    path: Path
    dropped: list[str]


def render_seed_bytes(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str,
) -> tuple[bytes, list[str]]:
    """Return ``(seed_bytes, dropped_field_names)``.

    Skills → canonical ``SKILL.md`` byte-copy with ``dropped == []``.
    Agents / commands → ``parse_canonical_*`` + vendor renderer; ``dropped``
    enumerates canonical fields the vendor format cannot represent.
    ``("commands", "codex")`` → :class:`NotImplementedError`.

    ``name`` is validated here even though :func:`seed_override` (the usual
    caller) already validates — the function is in ``__all__`` so direct
    callers should not have to remember to pre-validate. Defense in depth
    for the ``store.root / asset_type / name / ...`` path joins below.
    """
    validate_name(name, kind=f"{asset_type.removesuffix('s')} name")

    if asset_type == "skills":
        src = store.root / "skills" / name / "SKILL.md"
        if not src.is_file():
            raise FileNotFoundError(f"wiki has no skills/{name}/SKILL.md to seed from at {src}")
        return src.read_bytes(), []

    canonical = store.root / asset_type / name / f"{asset_type[:-1]}.md"
    if not canonical.is_file():
        raise FileNotFoundError(
            f"wiki has no {asset_type}/{name}/{asset_type[:-1]}.md to seed from at {canonical}"
        )

    # ``gen.render()`` returns ``(text, dropped_field_names)`` — fields the
    # vendor format can't represent (e.g., gemini drops ``skills`` /
    # ``isolation`` for agents, ``argument-hint`` / ``allowed-tools`` /
    # ``model`` for commands). The dropped list is propagated up so
    # ``mm wiki <type> override`` can warn the user via stderr.
    #
    # Function-body imports dodge a wiki ↔ context import cycle:
    # ``context.install`` already imports ``wiki.store``; widening to
    # module-top imports here would close the loop.
    if asset_type == "agents":
        from memtomem.context.agents import AGENT_GENERATORS, parse_canonical_agent

        gen_key = f"{vendor}_agents"
        if gen_key not in AGENT_GENERATORS:
            raise NotImplementedError(
                f"{vendor!r} agents not yet supported — see OVERRIDE_FORMATS placeholder"
            )
        agent = parse_canonical_agent(canonical, layout="dir")
        text, dropped = AGENT_GENERATORS[gen_key].render(agent)
        return text.encode("utf-8"), dropped
    if asset_type == "commands":
        from memtomem.context.commands import (
            COMMAND_GENERATORS,
            parse_canonical_command,
        )

        gen_key = f"{vendor}_commands"
        if gen_key not in COMMAND_GENERATORS:
            raise NotImplementedError(
                f"{vendor!r} commands not yet supported — see OVERRIDE_FORMATS placeholder"
            )
        cmd = parse_canonical_command(canonical, layout="dir")
        text, dropped = COMMAND_GENERATORS[gen_key].render(cmd)
        return text.encode("utf-8"), dropped
    raise ValueError(f"unsupported asset_type for override seeding: {asset_type!r}")


def seed_override(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str,
    *,
    force: bool = False,
) -> SeedResult:
    """Create ``<wiki>/<type>/<name>/overrides/<vendor>.<ext>``.

    Returns a :class:`SeedResult` (path + list of canonical fields the
    vendor renderer dropped — empty for skills). ``force`` overwrites an
    existing file after writing a ``.bak`` sibling so the previous content
    is recoverable.

    All preconditions are checked before any filesystem mutation: a refused
    call (missing wiki / missing canonical / collision without ``force``)
    must NOT leave a half-built ``overrides/`` directory behind.
    """
    store.require_exists()
    validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
    if fmt is None:
        raise ValueError(f"no override format registered for ({asset_type!r}, {vendor!r})")
    _, ext = fmt
    target = store.root / asset_type / name / "overrides" / f"{vendor}.{ext}"
    if target.exists() and not force:
        raise OverrideExistsError(
            f"override already exists at {target}; pass --force to overwrite "
            f"(creates a .bak sibling so the previous content is recoverable)"
        )
    # Pre-flight the seed bytes so missing-canonical does not leave an
    # empty overrides/ directory behind from the mkdir below.
    seed_bytes, dropped = render_seed_bytes(store, asset_type, name, vendor)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and force:
        backup = target.with_suffix(target.suffix + ".bak")
        atomic_write_bytes(backup, target.read_bytes())
    atomic_write_bytes(target, seed_bytes)
    return SeedResult(path=target, dropped=dropped)

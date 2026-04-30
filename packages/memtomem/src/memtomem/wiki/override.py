"""Wiki-side override seeding — writes into ``~/.memtomem-wiki/<type>/<name>/overrides/``.

Companion to :mod:`memtomem.context.override` (which is the project-side
*resolver*). The seed helper here is what ``mm wiki <type> override``
calls to produce the initial bytes the user then edits.

- Skills are byte-identical across vendors, so the seed is simply the
  canonical ``SKILL.md``.
- Agents and commands feed the canonical through the vendor's renderer
  in :data:`AGENT_GENERATORS` / :data:`COMMAND_GENERATORS` so the seed
  bytes match what the vendor's runtime would actually emit. This reuses
  PR-C's layout-aware ``parse_canonical_agent`` / ``parse_canonical_command``
  rather than widening the generator API surface.
- ``("commands", "codex")`` is a permanent placeholder row in
  :data:`OVERRIDE_FORMATS` (no ``codex_commands`` generator); seeding
  raises :class:`NotImplementedError` with a diagnostic message.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.context._atomic import atomic_write_bytes
from memtomem.context._names import OVERRIDE_FORMATS, validate_name
from memtomem.wiki.store import WikiStore

__all__ = ["OverrideExistsError", "render_seed_bytes", "seed_override"]


class OverrideExistsError(RuntimeError):
    """Raised when an override file already exists and ``force`` was not given."""


def render_seed_bytes(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str,
) -> bytes:
    """Return the bytes to seed an override file with.

    Skills → canonical ``SKILL.md``.
    Agents / commands → ``parse_canonical_*`` + vendor renderer.
    ``("commands", "codex")`` → :class:`NotImplementedError`.
    """
    if asset_type == "skills":
        src = store.root / "skills" / name / "SKILL.md"
        if not src.is_file():
            raise FileNotFoundError(f"wiki has no skills/{name}/SKILL.md to seed from at {src}")
        return src.read_bytes()

    canonical = store.root / asset_type / name / f"{asset_type[:-1]}.md"
    if not canonical.is_file():
        raise FileNotFoundError(
            f"wiki has no {asset_type}/{name}/{asset_type[:-1]}.md to seed from at {canonical}"
        )

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
        text, _dropped = AGENT_GENERATORS[gen_key].render(agent)
        return text.encode("utf-8")
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
        text, _dropped = COMMAND_GENERATORS[gen_key].render(cmd)
        return text.encode("utf-8")
    raise ValueError(f"unsupported asset_type for override seeding: {asset_type!r}")


def seed_override(
    store: WikiStore,
    asset_type: str,
    name: str,
    vendor: str,
    *,
    force: bool = False,
) -> Path:
    """Create ``<wiki>/<type>/<name>/overrides/<vendor>.<ext>``.

    Returns the override file path. ``force`` overwrites an existing file
    after writing a ``.bak`` sibling so the previous content is recoverable.

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
    seed_bytes = render_seed_bytes(store, asset_type, name, vendor)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and force:
        backup = target.with_suffix(target.suffix + ".bak")
        atomic_write_bytes(backup, target.read_bytes())
    atomic_write_bytes(target, seed_bytes)
    return target

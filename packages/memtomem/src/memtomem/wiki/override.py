"""Wiki-side override seeding — writes into ``~/.memtomem-wiki/<type>/<name>/overrides/``.

Companion to :mod:`memtomem.context.override` (which is the project-side
*resolver*). The seed helper here is what ``mm wiki <type> override``
calls to produce the initial bytes the user then edits. Skills are
byte-identical across vendors, so the seed is simply the canonical
``SKILL.md``; agents and commands need vendor-specific renderers and
land in a follow-up PR alongside the rest of the multi-kind override
surface.
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
    _vendor: str,
) -> bytes:
    """Return the bytes to seed an override file with.

    Skills (byte-identical fan-out) → seed equals canonical ``SKILL.md``.
    Agents and commands ride the vendor-specific renderers and are not
    activated in PR-C; the resolver in :mod:`memtomem.context.override`
    has a matching gate. ``_vendor`` is reserved for that follow-up
    (per-vendor renderer dispatch) and intentionally unused for skills.
    """
    if asset_type != "skills":
        raise NotImplementedError(f"override seeding for {asset_type!r} lands in a follow-up PR")
    src = store.root / "skills" / name / "SKILL.md"
    if not src.is_file():
        raise FileNotFoundError(f"wiki has no skills/{name}/SKILL.md to seed from at {src}")
    return src.read_bytes()


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

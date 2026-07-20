"""Tests for memtomem.context._names — ``validate_name`` and ``override_vendors``."""

from __future__ import annotations

import pytest

from memtomem.context._names import (
    GENERATOR_VENDOR,
    OVERRIDE_FORMATS,
    InvalidNameError,
    override_vendors,
    renderable_vendors,
    validate_name,
)


@pytest.mark.parametrize(
    "value",
    [
        "hello",
        "a-b_c.d",
        "x",  # single char
        "A" * 64,  # max length
        "123",  # all digits
        "agent.v2",
        "name_with_underscore",
        "name-with-dash",
    ],
)
def test_valid_names_pass_through(value: str) -> None:
    assert validate_name(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "..",
        "../x",
        "a/b",
        "a\\b",
        "",
        "   ",
        ".",
        "a\nb",
        # TRAILING newline, distinct from the interior one above and previously
        # untested. `fullmatch` already rejects it whatever the anchor, so this
        # pins the documented "no control chars" contract rather than the `\Z`
        # in the pattern — it would stay green if the anchor were loosened, and
        # only turns red if the call site ever moves to `match`.
        "skill\n",
        "a\rb",
        "a\x00b",
        "-x",  # leading dash
        "/absolute",
        "\\windows",
        "控",  # non-ASCII / CJK
        "name with space",
    ],
)
def test_invalid_names_are_rejected(value: str) -> None:
    with pytest.raises(InvalidNameError):
        validate_name(value)


def test_name_too_long() -> None:
    with pytest.raises(InvalidNameError, match="exceeds 64"):
        validate_name("A" * 65)


def test_non_string_rejected() -> None:
    with pytest.raises(InvalidNameError, match="expected str"):
        validate_name(123)  # type: ignore[arg-type]


def test_kind_appears_in_error_message() -> None:
    with pytest.raises(InvalidNameError, match="invalid agent name"):
        validate_name("../x", kind="agent name")


def test_dot_and_dotdot_rejected_explicitly() -> None:
    with pytest.raises(InvalidNameError, match="reserved path token"):
        validate_name(".")
    with pytest.raises(InvalidNameError, match="reserved path token"):
        validate_name("..")


# ── override_vendors ─────────────────────────────────────────────────────


def test_override_vendors_per_asset_type() -> None:
    # Insertion order preserved: claude → gemini → codex → kimi.
    assert override_vendors("skills") == ["claude", "gemini", "codex", "kimi"]
    assert override_vendors("agents") == ["claude", "gemini", "codex", "kimi"]
    # commands has no kimi row — Kimi exposes no commands surface.
    assert override_vendors("commands") == ["claude", "gemini", "codex"]


def test_override_vendors_matches_matrix() -> None:
    """The derived list must equal exactly the OVERRIDE_FORMATS rows for the
    asset type, so the helper and the matrix can never drift apart."""
    for asset_type in ("skills", "agents", "commands"):
        expected = [vendor for (at, vendor) in OVERRIDE_FORMATS if at == asset_type]
        assert override_vendors(asset_type) == expected


def test_override_vendors_unknown_asset_type_is_empty() -> None:
    assert override_vendors("widgets") == []


# ── renderable_vendors ────────────────────────────────────────────────────


def test_renderable_vendors_drops_codex_commands() -> None:
    """commands/codex is an OVERRIDE_FORMATS placeholder with no generator, so
    it is offered by ``override_vendors`` but excluded from ``renderable_vendors``
    (the web wiki browser disables it instead of offering a control that 500s)."""
    assert override_vendors("commands") == ["claude", "gemini", "codex"]
    assert renderable_vendors("commands") == ["claude", "gemini"]


def test_renderable_vendors_full_for_skills_and_agents() -> None:
    assert renderable_vendors("skills") == ["claude", "gemini", "codex", "kimi"]
    assert renderable_vendors("agents") == ["claude", "gemini", "codex", "kimi"]


def test_renderable_vendors_matches_generator_registry() -> None:
    """A vendor is renderable iff ``<vendor>_<asset_type>`` has a generator —
    the same membership ``render_seed_bytes`` checks before NotImplementedError.
    Pinning to GENERATOR_VENDOR keeps the helper and the renderers in lockstep."""
    for asset_type in ("skills", "agents", "commands"):
        expected = [
            v for v in override_vendors(asset_type) if f"{v}_{asset_type}" in GENERATOR_VENDOR
        ]
        assert renderable_vendors(asset_type) == expected


def test_renderable_vendors_subset_of_override_vendors() -> None:
    for asset_type in ("skills", "agents", "commands"):
        rendered = renderable_vendors(asset_type)
        offered = override_vendors(asset_type)
        assert set(rendered).issubset(offered)


def test_renderable_vendors_unknown_asset_type_is_empty() -> None:
    assert renderable_vendors("widgets") == []


def test_generator_vendor_matches_real_registries() -> None:
    """``GENERATOR_VENDOR`` is the cycle-free mirror ``renderable_vendors`` reads
    instead of importing the real ``*_GENERATORS`` dicts (which would close a
    wiki ↔ context import cycle). Pin that mirror to the actual registries so a
    newly-registered generator can't leave the web UI disabling a vendor that
    can in fact render (Codex review on PR-E)."""
    from memtomem.context.agents import AGENT_GENERATORS
    from memtomem.context.commands import COMMAND_GENERATORS
    from memtomem.context.skills import SKILL_GENERATORS

    real_keys = set(SKILL_GENERATORS) | set(AGENT_GENERATORS) | set(COMMAND_GENERATORS)
    assert real_keys == set(GENERATOR_VENDOR), (
        "GENERATOR_VENDOR drifted from the real *_GENERATORS registries — "
        "renderable_vendors would mis-report which vendors can render."
    )

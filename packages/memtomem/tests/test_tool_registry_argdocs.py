"""``Args:`` parsing for the ``mem_do(action="help")`` catalog.

``_parse_arg_docs`` feeds the only parameter documentation an agent sees for a
non-core action in the default core mode. It used to accept ANY
``\\s{4,}(\\w+):`` line inside the ``Args:`` block, so nested ``key: value``
content — the JSON schemas under ``mem_policy_add``'s ``config``, the format
list under ``mem_context_generate`` — registered as parameters that do not
exist. ``mem_do`` then advertised ``auto_expire`` / ``auto_tag`` /
``conversion`` as callable arguments.

The fix is two-layered and both layers are pinned here: the parser only starts
a new entry at the indent of the first parameter, and ``register()`` drops any
key that is not in the function signature.
"""

from __future__ import annotations

import inspect

from memtomem.server.tool_registry import ACTIONS, _parse_arg_docs

_NESTED_JSON_DOC = """Create a memory lifecycle policy.

    Args:
        name: Unique policy name
        config: JSON config string. Examples:
            auto_archive:
              {"max_age_days": 30}
              - age_field: "created_at" (default) or "last_accessed_at"
            auto_expire: {"max_age_days": 90}
        namespace_filter: Only apply to chunks in this namespace
    """

_TRAILING_PROSE_DOC = """Freeze a version snapshot.

    Args:
        name: Canonical artifact name.
        scope: Residency tier.

    A flat-layout artifact has no per-artifact versions store: ``list``
    returns a hint and ``create`` refuses.
    """

_CONTINUATION_DOC = """Do a thing.

    Args:
        scope: ADR-0011 scope-axis filter — single value, comma list
            or glob. When omitted, the default merge applies.
        limit: Maximum rows.
    """


def test_nested_keys_are_not_parameters() -> None:
    """Keys indented deeper than the first parameter stay continuation text."""
    docs = _parse_arg_docs(_NESTED_JSON_DOC)
    assert set(docs) == {"name", "config", "namespace_filter"}


def test_nested_keys_stay_in_the_owning_description() -> None:
    """The nested content is not dropped — it belongs to ``config``."""
    docs = _parse_arg_docs(_NESTED_JSON_DOC)
    assert "auto_expire" in docs["config"]


def test_trailing_prose_does_not_extend_the_last_parameter() -> None:
    """Prose after the block ends the section instead of gluing onto it.

    Before the fix this text was appended to whichever parameter came last,
    producing multi-KB "parameter descriptions" in the help catalog.
    """
    docs = _parse_arg_docs(_TRAILING_PROSE_DOC)
    assert "flat-layout" not in docs["scope"]


def test_trailing_prose_docstring_still_parses_every_parameter() -> None:
    docs = _parse_arg_docs(_TRAILING_PROSE_DOC)
    assert set(docs) == {"name", "scope"}


def test_wrapped_description_lines_are_joined() -> None:
    docs = _parse_arg_docs(_CONTINUATION_DOC)
    assert docs["scope"].endswith("the default merge applies.")


def test_no_args_section_yields_nothing() -> None:
    assert _parse_arg_docs("Just a summary line.\n\n    More prose.\n") == {}


def test_every_documented_key_is_a_real_parameter() -> None:
    """Registry-wide: the help catalog may not name a phantom argument.

    This is the invariant the ghost parameters violated; ``register()``
    filters against the signature so even a future docstring shape the parser
    mis-reads cannot reintroduce one.
    """
    offenders: dict[str, list[str]] = {}
    for action, info in ACTIONS.items():
        real = {p for p in inspect.signature(info.fn).parameters if p != "ctx"}
        ghosts = sorted(set(info.param_docs) - real)
        if ghosts:
            offenders[action] = ghosts
    assert not offenders, f"help catalog documents non-existent parameters: {offenders}"


def test_no_parameter_description_is_a_wall_of_text() -> None:
    """A parameter description is a description, not an embedded manual.

    ``mem_policy_add.config`` used to carry a 2.1 KB JSON schema catalog
    inside ``Args:``; it now lives in a "Config schemas" section below the
    block, where it documents the tool without bloating one help entry. The
    ceiling is set just above the current maximum
    (``agent_share.idempotency_key``) so the next wall of text has to justify
    itself.
    """
    ceiling = 600
    offenders = {
        f"{action}.{param}": len(text)
        for action, info in ACTIONS.items()
        for param, text in info.param_docs.items()
        if len(text) > ceiling
    }
    assert not offenders, (
        f"parameter descriptions over {ceiling} chars: {offenders}. Move the "
        "detail into prose below the Args block instead."
    )

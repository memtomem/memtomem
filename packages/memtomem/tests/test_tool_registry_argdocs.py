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

import pytest

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


#: Docstrings do not reach ``register()`` in one uniform shape — some keep the
#: source indentation, some arrive dedented. Every parser case runs against
#: both forms so the indent rules can't be correct for only one of them.
_FORMS = pytest.mark.parametrize("form", [lambda s: s, inspect.cleandoc], ids=["raw", "cleandoc"])


@_FORMS
def test_nested_keys_are_not_parameters(form) -> None:
    """Keys indented deeper than the first parameter stay continuation text."""
    docs = _parse_arg_docs(form(_NESTED_JSON_DOC))
    assert set(docs) == {"name", "config", "namespace_filter"}


@_FORMS
def test_nested_keys_stay_in_the_owning_description(form) -> None:
    """The nested content is not dropped — it belongs to ``config``."""
    docs = _parse_arg_docs(form(_NESTED_JSON_DOC))
    assert "auto_expire" in docs["config"]


@_FORMS
def test_trailing_prose_does_not_extend_the_last_parameter(form) -> None:
    """Prose after the block ends the section instead of gluing onto it.

    Before the fix this text was appended to whichever parameter came last,
    producing multi-KB "parameter descriptions" in the help catalog.
    """
    docs = _parse_arg_docs(form(_TRAILING_PROSE_DOC))
    assert "flat-layout" not in docs["scope"]


@_FORMS
def test_trailing_prose_docstring_still_parses_every_parameter(form) -> None:
    docs = _parse_arg_docs(form(_TRAILING_PROSE_DOC))
    assert set(docs) == {"name", "scope"}


@_FORMS
def test_wrapped_description_lines_are_joined(form) -> None:
    docs = _parse_arg_docs(form(_CONTINUATION_DOC))
    assert docs["scope"].endswith("the default merge applies.")


def test_no_args_section_yields_nothing() -> None:
    assert _parse_arg_docs("Just a summary line.\n\n    More prose.\n") == {}


def test_register_filters_a_phantom_parameter_out_of_the_catalog() -> None:
    """The signature filter must hold even when the parser is fooled.

    The registry-wide check below cannot pin it: no current docstring
    produces a ghost, so deleting the filter keeps that test green. Register
    a throwaway action whose docstring declares a parameter at the SAME
    indent as the real ones — the shape the parser is required to accept —
    and assert it never reaches the catalog.
    """
    from memtomem.server.tool_registry import register

    @register("advanced")
    async def mem_argdocs_probe(real: str = "", ctx: object = None) -> str:
        """Probe.

        Args:
            real: A parameter that exists.
            phantom: A parameter that does not exist.
        """
        return ""

    try:
        info = ACTIONS["argdocs_probe"]
        assert _parse_arg_docs(mem_argdocs_probe.__doc__ or "").get("phantom"), (
            "fixture no longer exercises the filter: the parser already drops "
            "'phantom', so this test would pass without register()'s filter"
        )
        assert set(info.param_docs) == {"real"}
    finally:
        ACTIONS.pop("argdocs_probe", None)


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


def test_policy_add_config_help_is_a_summary_not_the_schema_catalog() -> None:
    """The specific regression: a 2.1 KB JSON catalog inside one ``Args:`` entry.

    ``mem_policy_add.config`` embedded every per-type schema, so the policy
    help output was mostly one parameter. The schemas now live in a "Config
    schemas" section below the block; the parameter keeps a key list and a
    pointer. Assert the shape, not a byte count: no JSON braces, no bullet
    list, and the pointer an agent needs.
    """
    doc = ACTIONS["policy_add"].param_docs["config"]
    assert "{" not in doc.replace("``{}``", ""), (
        "config help is carrying JSON schema bodies again — keep them in the "
        "Config schemas section below the Args block"
    )
    assert "automation.md" in doc, (
        "config help must point somewhere a core-mode agent can actually "
        "read; it cannot see the tool description's prose sections"
    )


def test_category_help_output_stays_readable() -> None:
    """The real contract is the size of what ``mem_do help`` returns.

    A per-parameter ceiling would allow unbounded aggregate growth (many
    just-under-the-limit entries) while rejecting one legitimately long
    description, so budget the rendered category instead. ``context`` is the
    outlier by design — 11 actions with the ADR-0030 scope/consent surface —
    and gets its own line rather than pulling the general budget up to meet
    it.
    """
    from memtomem.server.tools.meta import _help

    budgets = {"context": 14_000}
    default_budget = 4_000
    offenders = {
        category: size
        for category in sorted({info.category for info in ACTIONS.values()})
        if (size := len(_help(category))) > budgets.get(category, default_budget)
    }
    assert not offenders, (
        f"category help outgrew its budget: {offenders}. Move detail into the "
        "tool description's prose sections, which full-mode clients still get."
    )

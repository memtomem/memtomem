"""Architectural guard — engine refusals never spell a surface's vocabulary (#1869).

The engine states the CONDITION; the CLI, MCP and web surfaces each append
their own remediation (:mod:`memtomem.context.remediation`). Before this
contract, ``pull_apply`` told every caller to ``pass --from <runtime>`` — a flag
an MCP client cannot pass (its parameter is ``from_runtime``) and the browser
has no notion of at all.

Why a sweep rather than a list of today's sites: the enumeration in #1869 was
built by grep and still missed two ``skills.py`` copies. A per-site test would
have shipped green with those two intact. This walks EVERY string literal in the
``context`` package instead, so the next refusal that hard-codes a flag fails
here whether or not anyone remembered to extend a list.

Docstrings are exempt — they document the CLI flag as an API concept
(``force_unsafe_import: the value of the CLI's --force-unsafe-import flag``),
which is prose about the parameter, not remediation shown to a user.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

CONTEXT_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem" / "context"

#: Vocabulary that belongs to exactly one surface. ``--from`` is word-bounded
#: so ``--force-unsafe-import`` is not double-reported.
#:
#: ``--to`` is deliberately absent: every occurrence in this package is part of
#: a runnable ``mm context migrate … --to project_local`` command (the "keep
#: as-is" class in #1869), and it never appears as a remediation clause on its
#: own. ``--from`` is kept because it DID: ``pass --from <runtime>`` was the
#: headline defect.
SURFACE_VOCABULARY = re.compile(
    r"--overwrite|--from\b|--force-unsafe-import|--scope=|"
    r"from_runtime=|force_unsafe_import=|overwrite=True"
)

#: Deliberate exceptions, each with the reason it is NOT a per-surface hint.
#: ``(module, substring)`` — the substring must appear in the offending literal.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        # The hint table itself — every surface's spelling lives here by design.
        ("remediation.py", "--overwrite"),
        ("remediation.py", "--from <runtime>"),
        ("remediation.py", "--force-unsafe-import"),
        ("remediation.py", "overwrite=True"),
        ("remediation.py", "from_runtime="),
        ("remediation.py", "force_unsafe_import=True"),
        # Runnable shell commands, not remediation clauses: these are meant to
        # be copy-pasted verbatim into a terminal (#1869 "keep as-is").
        ("settings_doctor.py", "mm context settings-migrate --from="),
        # ``migrate_scope`` has exactly one consumer — the CLI ``mm context
        # migrate`` verb (no web route, no MCP action calls it), so its
        # ``--from``/``--to`` ARE this path's parameter names. If a second
        # surface ever calls it, this entry must be revisited.
        ("migrate.py", "Pass --from <scope> to disambiguate."),
        ("migrate.py", "--from and --to must differ."),
    }
)


def _string_literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every non-docstring string constant in *path* as ``(lineno, value)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Any bare string EXPRESSION is documentation: module/class/function
    # docstrings and the attribute-docstring convention this package uses under
    # module-level constants (``migrate.py`` documents its Literal aliases that
    # way). Only strings that are actually USED — arguments, assignments,
    # f-string parts — can reach a user as a refusal.
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            docstrings.add(id(node.value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            out.append((node.lineno, node.value))
        # A flag split across a join is the same defect spelled differently:
        # ``"pass --" "overwrite"`` and ``"--" + "overwrite"`` both reach the
        # user whole, but neither Constant matches on its own. Fold the static
        # forms so the sweep sees what the user sees (Codex review).
        #
        # A flag assembled from a VARIABLE (``f"--{flag}"``) is out of reach for
        # any static check — the sweep bounds the accidental regression, not a
        # determined one.
        elif isinstance(node, ast.JoinedStr | ast.BinOp):
            folded = _fold_static_parts(node)
            if folded is not None and id(node) not in docstrings:
                out.append((node.lineno, folded))
    return out


def _fold_static_parts(node: ast.AST) -> str | None:
    """Concatenate the literal parts of an f-string / ``+`` chain, or ``None``.

    Non-literal parts collapse to a single space rather than being dropped, so
    ``f"--{x}overwrite"`` does NOT fold into the flag it never spelled.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else " "
    if isinstance(node, ast.JoinedStr):
        return "".join(_fold_static_parts(v) or " " for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_static_parts(node.left)
        right = _fold_static_parts(node.right)
        if left is None and right is None:
            return None
        return (left or " ") + (right or " ")
    if isinstance(node, ast.FormattedValue):
        return " "
    return None


def test_engine_strings_never_name_a_surfaces_vocabulary() -> None:
    offenders: list[str] = []
    for path in sorted(CONTEXT_DIR.rglob("*.py")):
        for lineno, value in _string_literals(path):
            if not SURFACE_VOCABULARY.search(value):
                continue
            if any(path.name == mod and frag in value for mod, frag in ALLOWED):
                continue
            offenders.append(f"{path.name}:{lineno}: {value[:120]!r}")
    assert not offenders, (
        "engine strings must state the condition only; per-surface remediation "
        "belongs in memtomem.context.remediation (#1869):\n" + "\n".join(offenders)
    )


def test_allowlist_entries_still_exist() -> None:
    """A stale exemption is a hole — every ALLOWED entry must still match.

    Without this, deleting the site an exemption covers leaves a permanent
    licence for that module to reintroduce the flag.
    """
    seen = {
        (path.name, value)
        for path in sorted(CONTEXT_DIR.rglob("*.py"))
        for _, value in _string_literals(path)
    }
    stale = [
        f"{mod}: {frag!r}"
        for mod, frag in sorted(ALLOWED)
        if not any(name == mod and frag in value for name, value in seen)
    ]
    assert not stale, "ALLOWED entries no longer present — drop them:\n" + "\n".join(stale)


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_actionable_codes_have_a_clause_on_every_writing_surface(surface: str) -> None:
    """Both text surfaces answer for every key — a half-filled row is a silent
    downgrade for whichever surface was forgotten."""
    from memtomem.context import remediation

    missing = [key for key in remediation._HINTS if not remediation.action_hint(key, surface)]
    assert not missing, f"no {surface} remediation for: {missing}"


def test_unknown_code_yields_no_hint() -> None:
    """Fail open to the neutral reason — never guess a remediation."""
    from memtomem.context import remediation

    assert remediation.action_hint("no_such_code", "cli") == ""
    assert remediation.action_hint(None, "cli") == ""
    assert remediation.append_hint("something failed", None, "cli") == "something failed"


def test_web_hints_are_localized_client_side_not_baked_into_python() -> None:
    """The browser owns its own copy; an English clause on the wire would
    bypass i18n entirely (and ship untranslated text to a ko user)."""
    from memtomem.context import remediation

    for key in remediation._HINTS:
        assert remediation.action_hint(key, "web") == ""


# ── Gate A hard-abort: the one message the engine RAISES fully formed ────────


def _gate_a_message(surface: str, tmp_path: pathlib.Path) -> str:
    import click

    from memtomem.context._gate_a import apply_gate_a

    src = tmp_path / "agent.md"
    src.write_text("tok " + "AKIA" + "IOSFODNN7EXAMPLE", encoding="utf-8")
    with pytest.raises(click.ClickException) as exc:
        apply_gate_a(
            content_text=src.read_text(encoding="utf-8"),
            src=src,
            scope="project_shared",
            force_unsafe_import=False,
            audit_context={},
            message_kind="agent",
            imported_so_far=0,
            surface=surface,
        )
    return str(exc.value)


def test_gate_a_abort_offers_no_tier_retry_on_any_surface(tmp_path: pathlib.Path) -> None:
    """The hard-abort's remediation is "remove the secret" — identical on every
    surface, therefore not a per-surface hint at all.

    The pre-#1869 wording pointed at another tier, which cannot work:
    ``project_local`` has no runtime fan-out (ADR-0011 §3) and ``user`` reads
    its runtime sources from ``$HOME`` regardless of ``project_root``
    (``_runtime_targets.runtime_fanout_root``), so it inspects a DIFFERENT copy
    than the blocked one. Advice that reliably fails is worse than none
    (Codex review, round 2).
    """
    for surface in ("cli_context_init", "mcp_context_init", "web_context_agents_import"):
        message = _gate_a_message(surface, tmp_path)
        assert "Remove the secret from" in message
        assert "project_local" not in message
        assert "--scope" not in message
        assert 'scope="user"' not in message


def test_remediation_never_names_a_tier_that_cannot_be_pulled_into() -> None:
    """``project_local`` has no runtime fan-out (ADR-0011 §3), so every Pull
    surface refuses it and the extract engines short-circuit. A remediation that
    names it costs the user a second refusal — the exact failure #1869 exists to
    stop, one layer down (Codex review).
    """
    from memtomem.context import remediation

    for key, row in remediation._HINTS.items():
        for surface, clause in row.items():
            assert "project_local" not in clause, f"{key}/{surface} names a dead tier"


def test_pull_side_refusal_copy_never_advertises_a_dead_tier() -> None:
    """Route-level refusal copy is remediation too — the round-1 fix corrected
    the engine and left the web's twin saying "Pull into the user or
    project_local tier" (Codex review, round 2).

    Scoped to the PULL direction on purpose: ``project_local`` is a perfectly
    valid canonical destination for a push/migrate, so a blanket ban on the
    word would be a false positive on ``PRIVACY_BLOCK_DETAIL``.
    """
    from memtomem.web.routes import _errors

    pull_side = {
        name: value
        for name, value in vars(_errors).items()
        if isinstance(value, str) and ("IMPORT" in name or "PULL" in name)
    }
    assert pull_side, "no pull-side refusal constants found — did they get renamed?"
    for name, value in pull_side.items():
        assert "project_local" not in value, f"{name} advertises a tier that cannot be pulled into"

"""Every threaded ``generate_all_settings`` dispatch carries both worker guards.

``asyncio.to_thread`` cannot be cancelled, so a worker outlives the request
that started it, and the two context managers here are what keep that worker
accountable to the caller it has already outlived. ``pinned_host_homes()``
decides *where* a late write lands (#2211): every user-scope target is
anchored on the ambient ``$HOME`` / ``$KIMI_CODE_HOME``, so a dispatch that
forgets it lets the write follow the environment to a home its caller never
chose — which is how a cancelled sync came to write the developer's real
``~/.claude/settings.json`` mid-suite. ``abandon_sync_on_exit()`` decides
*whether* it happens at all (#2218): without it a timed-out request returns
503 and then mutates the user's settings seconds later. Sitting inside both is
necessary but not sufficient: the hand-off must also be one that copies the
caller's context, which ``asyncio.to_thread`` does and ``run_in_executor``
does not.

Per-site regression tests cannot cover a dispatcher that does not exist yet, so
the rule is enforced lexically over the tree instead: the guard finds the call
sites itself rather than reading a hand-kept list that a new dispatcher would
silently sit outside of.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"
_DISPATCHED = "generate_all_settings"
#: Context managers a threaded dispatch must sit inside, with the issue that
#: put each one there — the offender line names it so a failure points at the
#: rationale rather than just the missing call.
_REQUIRED_SCOPES = (("pinned_host_homes", "#2211"), ("abandon_sync_on_exit", "#2218"))


def _thread_dispatch_sites(tree: ast.AST) -> list[ast.Call]:
    """Calls that hand ``generate_all_settings`` to a thread.

    Matched by the *argument*, not the callee, so every hand-off spelling is
    found — including the ones the pin cannot rescue (see
    :func:`_propagates_context`).
    """
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            # Both spellings: the bare name, and a qualified reference such as
            # ``settings.generate_all_settings``. Matching only the bare name
            # would let one import-style hop carry a dispatcher out of view.
            name = arg.id if isinstance(arg, ast.Name) else getattr(arg, "attr", None)
            if name == _DISPATCHED:
                sites.append(node)
                break
    return sites


def _propagates_context(site: ast.Call) -> bool:
    """Whether this hand-off carries the caller's ``ContextVar`` state.

    ``asyncio.to_thread`` copies the current context into the worker, which is
    the whole reason a ``ContextVar`` pin survives the hand-off.
    ``loop.run_in_executor`` does **not** — a dispatcher written that way would
    sit inside the ``with`` block and still resolve homes from the live
    environment, so being pinned proves nothing there and the guard must reject
    it rather than wave it through on position alone.
    """
    func = site.func
    # Specifically ``asyncio.to_thread``. Accepting any ``*.to_thread`` would
    # approve an executor wrapper of the same name whose propagation is
    # unknown; requiring the module qualifier keeps the rule to the one API
    # whose contract is documented. A future ``from asyncio import to_thread``
    # would fail here — loudly, which is the right direction for a guard to be
    # wrong in.
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "to_thread"
        and isinstance(func.value, ast.Name)
        and func.value.id == "asyncio"
    )


def _scope_spans(tree: ast.AST, scope: str) -> list[tuple[int, int]]:
    """Line spans of every ``with <scope>(...)`` block.

    Parameterized over the name so both guards read the same ``with``
    statement: ``with pinned_host_homes(), abandon_sync_on_exit():`` is one
    node whose ``items`` carry both, and each lookup finds its own.
    """
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name == scope:
                spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _python_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_guard_finds_the_known_dispatchers():
    """A guard that matches nothing would pass forever — pin the lower bound."""
    found = 0
    for path in _python_files():
        found += len(_thread_dispatch_sites(ast.parse(path.read_text(encoding="utf-8"))))
    assert found >= 3, (
        f"expected at least the web + two MCP dispatchers, found {found} — "
        "if a dispatcher was intentionally removed, lower this bound deliberately"
    )


def test_every_threaded_settings_dispatch_is_inside_both_scopes():
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sites = _thread_dispatch_sites(tree)
        if not sites:
            continue
        for site in sites:
            where = f"{path.relative_to(_SRC)}:{site.lineno}"
            if not _propagates_context(site):
                offenders.append(f"{where} — hand-off does not copy the caller's context")
                continue
            for scope, issue in _REQUIRED_SCOPES:
                spans = _scope_spans(tree, scope)
                if not any(start <= site.lineno <= end for start, end in spans):
                    offenders.append(f"{where} — outside `with {scope}():` ({issue})")
    assert not offenders, (
        "settings work handed to a thread without the guards its worker needs:\n  "
        + "\n  ".join(offenders)
        + "\nA worker outlives its caller, so it must carry the homes the caller "
        "resolved (#2211) and the abort flag that stops it writing behind a "
        "response that already failed (#2218). Use `asyncio.to_thread` inside "
        "both scopes, or dispatch through `contextvars.copy_context().run` "
        "explicitly."
    )


def _guard_accepts(source: str) -> bool:
    """Run the guard's own rule over a snippet."""
    tree = ast.parse(source)
    sites = _thread_dispatch_sites(tree)
    assert sites, "fixture should contain a dispatch"
    return all(
        _propagates_context(site)
        and all(
            any(start <= site.lineno <= end for start, end in _scope_spans(tree, scope))
            for scope, _issue in _REQUIRED_SCOPES
        )
        for site in sites
    )


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "unguarded to_thread",
            "import asyncio\nasync def f():\n    await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            "dispatch after the scopes have exited",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes(), abandon_sync_on_exit():\n        pass\n"
            "    await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            # Inside the scopes, yet still broken: run_in_executor does not
            # copy the context, so neither ContextVar reaches the worker.
            "guarded run_in_executor",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes(), abandon_sync_on_exit():\n"
            "        await loop.run_in_executor(None, generate_all_settings, r)\n",
        ),
        (
            # A qualified reference is the same dispatch one import hop away.
            "unguarded qualified dispatch",
            "import asyncio\nasync def f():\n"
            "    await asyncio.to_thread(settings.generate_all_settings, r)\n",
        ),
        (
            # Same method name, different object: propagation is not asyncio's
            # to promise.
            "guarded executor.to_thread",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes(), abandon_sync_on_exit():\n"
            "        await executor.to_thread(generate_all_settings, r)\n",
        ),
        (
            # The half-guarded shape the #2218 half of the rule exists for:
            # the write goes to the right home and still should not happen.
            "pinned but not abandon-scoped",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes():\n"
            "        await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            # And the mirror: cancellable, but free to follow $HOME.
            "abandon-scoped but not pinned",
            "import asyncio\nasync def f():\n"
            "    with abandon_sync_on_exit():\n"
            "        await asyncio.to_thread(generate_all_settings, r)\n",
        ),
    ],
)
def test_guard_rejects_shapes_that_lose_a_guard(label, source):
    """The guard must fail on the shapes it claims to catch, not just pass today."""
    assert not _guard_accepts(source), f"guard would have accepted: {label}"


@pytest.mark.parametrize(
    "source",
    [
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(generate_all_settings, r)\n",
        # The qualified spelling is fine too, as long as it is guarded and
        # dispatched through asyncio.
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(settings.generate_all_settings, r)\n",
        # Nested rather than stacked: two `with` statements express the same
        # two spans, and the rule is about the spans, not the spelling.
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes():\n"
        "        with abandon_sync_on_exit():\n"
        "            await asyncio.to_thread(generate_all_settings, r)\n",
    ],
)
def test_guard_accepts_the_correct_shape(source):
    """Rejecting everything would also pass the tests above — pin the positive."""
    assert _guard_accepts(source)

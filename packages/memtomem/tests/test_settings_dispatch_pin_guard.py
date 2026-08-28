"""Every threaded ``generate_all_settings`` dispatch must pin the host homes (#2211).

``asyncio.to_thread`` cannot be cancelled, so a worker outlives the request
that started it, and every user-scope settings target is anchored on the
ambient ``$HOME`` / ``$KIMI_CODE_HOME``. A dispatch that forgets
``pinned_host_homes()`` therefore lets a late write follow the environment to a
home its caller never chose — which is how a cancelled sync came to write the
developer's real ``~/.claude/settings.json`` mid-suite. Sitting inside the pin
is necessary but not sufficient: the hand-off must also be one that copies the
caller's context, which ``asyncio.to_thread`` does and ``run_in_executor`` does
not.

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
_PIN = "pinned_host_homes"
_DISPATCHED = "generate_all_settings"


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
            if isinstance(arg, ast.Name) and arg.id == _DISPATCHED:
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
    return (getattr(site.func, "attr", None) or getattr(site.func, "id", None)) == "to_thread"


def _pinned_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """Line spans of every ``with pinned_host_homes(...)`` block."""
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name == _PIN:
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


def test_every_threaded_settings_dispatch_is_inside_a_pin():
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sites = _thread_dispatch_sites(tree)
        if not sites:
            continue
        spans = _pinned_spans(tree)
        for site in sites:
            where = f"{path.relative_to(_SRC)}:{site.lineno}"
            if not _propagates_context(site):
                offenders.append(f"{where} — hand-off does not copy the pinned context")
            elif not any(start <= site.lineno <= end for start, end in spans):
                offenders.append(f"{where} — outside `with pinned_host_homes():`")
    assert not offenders, (
        "settings work handed to a thread without carrying the caller's homes:\n  "
        + "\n  ".join(offenders)
        + "\nA worker outlives its caller, so it must carry the homes the caller "
        "resolved (#2211). Use `asyncio.to_thread` inside the pin, or dispatch "
        "through `contextvars.copy_context().run` explicitly."
    )


def _guard_accepts(source: str) -> bool:
    """Run the guard's own rule over a snippet."""
    tree = ast.parse(source)
    sites = _thread_dispatch_sites(tree)
    spans = _pinned_spans(tree)
    assert sites, "fixture should contain a dispatch"
    return all(
        _propagates_context(site) and any(start <= site.lineno <= end for start, end in spans)
        for site in sites
    )


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "unpinned to_thread",
            "import asyncio\nasync def f():\n    await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            "dispatch after the pin has exited",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes():\n        pass\n"
            "    await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            # Inside the pin, yet still broken: run_in_executor does not copy
            # the context, so the ContextVar never reaches the worker.
            "pinned run_in_executor",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes():\n"
            "        await loop.run_in_executor(None, generate_all_settings, r)\n",
        ),
    ],
)
def test_guard_rejects_shapes_that_lose_the_pin(label, source):
    """The guard must fail on the shapes it claims to catch, not just pass today."""
    assert not _guard_accepts(source), f"guard would have accepted: {label}"


def test_guard_accepts_the_correct_shape():
    """Rejecting everything would also pass the tests above — pin the positive."""
    assert _guard_accepts(
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes():\n"
        "        await asyncio.to_thread(generate_all_settings, r)\n"
    )

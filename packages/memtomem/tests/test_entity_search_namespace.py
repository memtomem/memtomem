"""``mem_entity_search`` resolves ``namespace`` the way ``mem_search`` does (#3111).

The storage query used to compare ``namespace`` by equality and apply no
system-prefix exclusion, so a default entity search listed people and
technologies from ``archive:*`` and ``agent-runtime:*`` chunks, and a comma
list or glob matched nothing. The tool now validates the raw value before the
app opens and hands storage a ``NamespaceFilter`` built from
``namespace or app.current_namespace`` and the configured system prefixes.
The SQL side of each filter form is pinned in ``test_entities.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio

_PREFIXES = ["archive:", "agent-runtime:"]


def _fake_app(current_namespace: str | None = None) -> MagicMock:
    app = MagicMock()
    app.current_namespace = current_namespace
    app.config.search.system_namespace_prefixes = list(_PREFIXES)
    app.storage.search_entities = AsyncMock(return_value=[])
    return app


async def _forwarded_filter(monkeypatch, *, namespace, current_namespace=None):
    from memtomem.server.tools import entity as entity_mod

    app = _fake_app(current_namespace)
    monkeypatch.setattr(entity_mod, "_get_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(entity_mod, "_resolve_project_context_root", lambda _app: None)

    await entity_mod.mem_entity_search(namespace=namespace, ctx=SimpleNamespace())

    return app.storage.search_entities.await_args.kwargs["namespace_filter"]


@pytest.mark.parametrize(
    ("namespace", "current_namespace", "namespaces", "pattern", "excluded"),
    [
        # Neither given: system namespaces are hidden.
        (None, None, (), None, tuple(_PREFIXES)),
        # Omitted under a session namespace: the session's namespace applies.
        (None, "work", ("work",), None, ()),
        # An explicit namespace wins over the session's.
        ("personal", "work", ("personal",), None, ()),
        # ``!system`` overrides even a system-prefixed session namespace.
        ("!system", "archive:summary", (), None, tuple(_PREFIXES)),
        # Comma list and glob reach storage as such.
        ("work,personal", None, ("work", "personal"), None, ()),
        ("proj:*", None, (), "proj:*", ()),
        # Naming a system namespace opts into it.
        ("archive:summary", None, ("archive:summary",), None, ()),
    ],
)
async def test_storage_gets_the_resolved_filter(
    monkeypatch, namespace, current_namespace, namespaces, pattern, excluded
):
    ns_filter = await _forwarded_filter(
        monkeypatch, namespace=namespace, current_namespace=current_namespace
    )

    assert ns_filter.namespaces == namespaces
    assert ns_filter.pattern == pattern
    assert ns_filter.exclude_prefixes == excluded


@pytest.mark.parametrize("namespace", ["work,proj:*", "!system,work"])
async def test_malformed_namespace_is_refused_before_the_app_opens(monkeypatch, namespace):
    from memtomem.server.tools import entity as entity_mod

    opened = AsyncMock()
    monkeypatch.setattr(entity_mod, "_get_app_initialized", opened)

    out = await entity_mod.mem_entity_search(namespace=namespace, ctx=SimpleNamespace())

    assert out.startswith("Error: ")
    assert "namespace" in out
    opened.assert_not_awaited()

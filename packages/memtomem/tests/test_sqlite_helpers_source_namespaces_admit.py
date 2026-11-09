"""Direct tests for source_namespaces_admit and source_namespaces_admit_sql (#3137)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from memtomem.storage.sqlite_helpers import (
    source_namespaces_admit,
    source_namespaces_admit_sql,
)

P: tuple[str, ...] = ("archive:", "agent-runtime:")


@pytest.mark.parametrize(
    ("namespaces", "namespace", "system_prefixes", "expected"),
    [
        ("work,personal", "work", P, True),
        ("work,personal", "personal", P, True),
        ("workspace", "work", P, False),
        ("", "work", P, False),
        (None, "work", P, False),
        ("archive:old", None, P, False),
        ("archive:old,work", None, P, True),
        ("agent-runtime:x,archive:y", None, P, False),
        ("Archive:Old", None, P, False),
        ("archive", None, P, True),
        (None, None, P, True),
        ("", None, P, True),
        ("archive:old", None, (), True),
        ("archive:old", "archive:old", P, True),
    ],
)
def test_source_namespaces_admit_cases(
    namespaces: str | None,
    namespace: str | None,
    system_prefixes: Sequence[str],
    expected: bool,
) -> None:
    """Cover the documented edge cases and contract of source_namespaces_admit."""
    assert source_namespaces_admit(namespaces, namespace, system_prefixes) is expected


@pytest.mark.parametrize(
    ("namespaces", "namespace", "prefixes_json", "expected"),
    [
        ("archive:old,work", None, '["archive:", "agent-runtime:"]', True),
        ("archive:old", None, '["archive:", "agent-runtime:"]', False),
        ("archive:old", None, "[]", True),
    ],
)
def test_source_namespaces_admit_sql_cases(
    namespaces: str | None,
    namespace: str | None,
    prefixes_json: str,
    expected: bool,
) -> None:
    """Cover the SQL adapter source_namespaces_admit_sql."""
    assert source_namespaces_admit_sql(namespaces, namespace, prefixes_json) is expected

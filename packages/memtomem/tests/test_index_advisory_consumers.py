"""Every UI consumer of an index result must surface the #2061 advisory.

``--force`` / ``force=true`` re-embeds without re-resolving namespaces, so a
namespace rule that has not been applied is invisible unless the result says
so. The counters travel on every index response, but each JS surface renders
by hand — the first cut of the fix wired three of seven consumers and the
other four reported a clean run.

This guard derives its own scope: it finds the call sites rather than checking
a list someone has to remember to extend, so a consumer added later is covered
the day it lands.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "memtomem" / "web" / "static"

#: A call that returns an indexing result the user is shown.
_INDEX_CALL_RE = re.compile(r"""['"]/api/(index|reindex)(\?[^'"]*)?['"]""")

#: The shared reporters. ``ForRoots`` aggregates the per-root ``/api/reindex``
#: shape; the bare one takes a single result or SSE ``complete`` event.
_ADVISORY_RE = re.compile(r"namespaceAdvisoryToast(ForRoots)?\s*\(")


def _js_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(_STATIC_DIR.glob("*.js"))}


def test_every_index_call_site_file_reports_the_namespace_advisory() -> None:
    sources = _js_sources()
    callers = {name for name, text in sources.items() if _INDEX_CALL_RE.search(text)}
    # If this trips, the regex stopped matching the real call shape — a guard
    # that silently scopes itself to nothing is worse than no guard.
    assert callers, "found no /api/index or /api/reindex call sites to check"

    missing = sorted(name for name in callers if not _ADVISORY_RE.search(sources[name]))
    assert not missing, (
        f"{missing} call /api/index or /api/reindex but never call "
        "namespaceAdvisoryToast — a forced reindex there reports success while "
        "silently preserving namespaces the current rules disagree with (#2061)."
    )


def test_sse_complete_handlers_report_the_namespace_advisory() -> None:
    """The stream path carries the same counters on its ``complete`` event."""
    sources = _js_sources()
    handlers = {
        name
        for name, text in sources.items()
        if re.search(r"===\s*['\"]complete['\"]", text) and "indexed_chunks" in text
    }
    assert handlers, "found no SSE complete handlers to check"

    missing = sorted(name for name in handlers if not _ADVISORY_RE.search(sources[name]))
    assert not missing, (
        f"{missing} render an indexing 'complete' event without the namespace advisory (#2061)."
    )

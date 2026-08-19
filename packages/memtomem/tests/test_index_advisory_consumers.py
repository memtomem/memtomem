"""Every UI consumer of an index result must surface the #2061 advisory.

``--force`` / ``force=true`` re-embeds without re-resolving namespaces, so a
namespace rule that has not been applied is invisible unless the result says
so. The counters travel on every index response, but each JS surface renders
by hand — the first cut of the fix wired three of seven consumers and the
other four reported a clean run.

**What this guard is and is not.** It is a structural check: it derives its own
scope, so a consumer added later is covered the day it lands rather than the
day someone remembers to extend a list. It cannot prove the reporter is reached
at runtime — a call on a dead branch would satisfy it. That half is Vitest's
(``tests-js/index-namespace-advisory.test.mjs`` drives ``mdReindexAll`` and
``_renderIndexResult`` for real and asserts the toast). Structure here,
behavior there; neither alone is enough.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "memtomem" / "web" / "static"

#: A call that returns an indexing result the user is shown.
_INDEX_CALL_RE = re.compile(r"""['"]/api/(index|reindex)(\?[^'"]*)?['"]""")

#: A *call* to one of the shared reporters. ``(?<!function )`` keeps the
#: definition in ``app.js`` from counting as its own consumer — without it the
#: file that declares the helper would satisfy this guard by existing.
_ADVISORY_CALL_RE = re.compile(r"(?<!function )\bnamespaceAdvisoryToast(ForRoots)?\s*\(")

#: Reporter definitions, so the declaring file can be told apart from consumers.
_ADVISORY_DEF_RE = re.compile(r"function\s+namespaceAdvisoryToast(ForRoots)?\s*\(")

#: A reporter's own body, which may legitimately call the other reporter
#: (``ForRoots`` delegates to the base one). Those calls are implementation,
#: not consumption — counting them let ``app.js`` satisfy this guard through
#: its own helper even with every real consumer call removed.
_ADVISORY_BODY_RE = re.compile(
    r"function\s+namespaceAdvisoryToast(ForRoots)?\s*\([^)]*\)\s*\{.*?\n\}",
    re.DOTALL,
)

#: Renders an indexing ``complete`` event: the SSE terminal branch that shows
#: counts to the user. Requiring a *count* field keeps this from matching a
#: transport-level "is this the terminal event" check that renders nothing.
_COMPLETE_RENDER_RE = re.compile(r"===\s*['\"]complete['\"]")


def _web_sources() -> dict[str, str]:
    """Every file that can hold a consumer: JS modules and the page itself.

    ``index.html`` is included because an inline handler there would be just as
    much a consumer as one in a module, and scanning only ``static/*.js`` would
    quietly exclude it.
    """
    paths = sorted(_STATIC_DIR.glob("*.js")) + sorted(_STATIC_DIR.glob("*.html"))
    return {p.name: p.read_text(encoding="utf-8") for p in paths}


def _reports(text: str) -> bool:
    """Does this file call a reporter from somewhere that is not a reporter?"""
    return bool(_ADVISORY_CALL_RE.search(_ADVISORY_BODY_RE.sub("", text)))


def test_every_index_call_site_file_reports_the_namespace_advisory() -> None:
    sources = _web_sources()
    callers = {name for name, text in sources.items() if _INDEX_CALL_RE.search(text)}
    # If this trips, the regex stopped matching the real call shape — a guard
    # that silently scopes itself to nothing is worse than no guard.
    assert callers, "found no /api/index or /api/reindex call sites to check"

    missing = sorted(name for name in callers if not _reports(sources[name]))
    assert not missing, (
        f"{missing} call /api/index or /api/reindex but never call "
        "namespaceAdvisoryToast — a forced reindex there reports success while "
        "silently preserving namespaces the current rules disagree with (#2061)."
    )


def test_sse_complete_handlers_report_the_namespace_advisory() -> None:
    """The stream path carries the same counters on its ``complete`` event."""
    sources = _web_sources()
    handlers = {
        name
        for name, text in sources.items()
        if _COMPLETE_RENDER_RE.search(text) and "indexed_chunks" in text
    }
    assert handlers, "found no SSE complete handlers to check"

    missing = sorted(name for name in handlers if not _reports(sources[name]))
    assert not missing, (
        f"{missing} render an indexing 'complete' event without the namespace advisory (#2061)."
    )


def test_the_reporter_definition_does_not_satisfy_the_guard_by_itself() -> None:
    """Pins the ``(?<!function )`` lookbehind.

    Without it, the file declaring ``namespaceAdvisoryToast`` passes both checks
    on the strength of its own ``function`` line, so the surface that renders
    the main index result — the one most likely to regress — would be exempt
    from the guard that exists to cover it.
    """
    definition_only = "function namespaceAdvisoryToast(result) {\n  return false;\n}\n"
    assert _ADVISORY_DEF_RE.search(definition_only)
    assert not _reports(definition_only)
    assert _reports(definition_only + "namespaceAdvisoryToast(resp);\n")


def test_a_reporter_calling_another_reporter_is_not_consumption() -> None:
    """``namespaceAdvisoryToastForRoots`` delegates to the base reporter.

    Counting that delegation as a consumer call let the declaring file pass
    both checks on its own implementation — every real call site could be
    deleted and the guard would stay green.
    """
    reporters_only = (
        "function namespaceAdvisoryToastForRoots(results) {\n"
        "  return namespaceAdvisoryToast({ namespaces_reassigned: 0 });\n"
        "}\n"
        "function namespaceAdvisoryToast(result) {\n  return false;\n}\n"
    )
    assert not _reports(reporters_only)
    assert _reports(reporters_only + "namespaceAdvisoryToastForRoots(res.results);\n")

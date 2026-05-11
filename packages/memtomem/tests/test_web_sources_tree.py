"""JS source-grep pins for the Sources tab vendor tree.

No JS runtime in the suite, so we lock the small set of branches that
fan-out determines whether each ``memory_dir`` renders. Two pieces of
behaviour we don't want to regress:

* Filter state (path filter input OR ``STATE.sourcesNsFilter``) must NOT
  hide "Discovered" dirs (chunks=0, files>0). The original ``Claude (0)``
  filter guard was meant to drop *indexed-empty* dirs only; without a
  Discovered carve-out the same guard makes 30+ dirs vanish the moment
  any filter is set.
* The ``isDiscovered`` predicate stays defined as ``chunks === 0 &&
  files > 0`` so the grep above remains meaningful.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static"


def _read_app_js() -> str:
    return (_STATIC / "app.js").read_text(encoding="utf-8")


def test_filter_keeps_discovered_dirs_visible() -> None:
    js = _read_app_js()
    # The filterActive branch in _renderMemorySourceTree must keep
    # Discovered dirs in addition to dirs with matching sources.
    # Any future refactor that drops the ``|| isDiscovered(d)`` half of
    # this predicate hides 30+ chunkless dirs the moment a filter is set.
    needle = "filter(d => (sourcesByDir[d] || []).length > 0 || isDiscovered(d))"
    assert needle in js, (
        "filterActive branch lost its Discovered carve-out — dirs with "
        "chunks=0 will vanish whenever any filter is active"
    )


def test_is_discovered_predicate_shape() -> None:
    js = _read_app_js()
    # Lock the predicate so the grep above keeps catching regressions.
    # If this expression ever moves to a helper, update both pins together.
    assert "return chunks === 0 && files > 0;" in js, (
        "isDiscovered predicate shape changed — update the filterActive pin too"
    )

"""Guard: no unresolved merge-conflict markers reach a tracked file.

A conflicted merge that is staged without resolving leaves ``<<<<<<<`` /
``=======`` / ``>>>>>>>`` in the file, and nothing in the suite noticed. It
happened: `2d8136d5` landed on ``main`` with all three markers in
``CHANGELOG.md``, through a green CI. Prose files have no parser to break, so
the failure is silent until a human reads the release notes.

Scope is **git's**, not a list here. ``git ls-files`` is asked for the tracked
set, so a new file, a new directory, or a new file type is covered the day it
is added rather than the day someone remembers to extend a constant.

**Bytes, not text.** The scan never decodes. A first draft read each file as
UTF-8 and skipped whatever raised, which meant one invalid byte anywhere in a
file hid every marker in it — measured, not theorised. It also skipped files
over a size cap, on the reasoning that large blobs are not prose; a large
Markdown or CSV is both. Markers are ASCII and line-anchored, so matching them
in raw bytes needs neither the decode nor the cap, and the blind spot that
came with each is gone. What is skipped now is only what is not a readable
regular file — a tracked path deleted from the working tree, a submodule
gitlink — and a read that fails for any other reason is **reported**, not
silently treated as clean.

What counts as a marker. ``<<<<<<<`` and ``>>>>>>>`` at the start of a line
are never valid content in this repository, so either one fails on its own.
``=======`` deliberately does **not** fail alone: a Markdown setext heading
underlines its title with ``=`` characters, and a seven-character title is not
a defect. Git writes all three markers together, so an *intact* conflict is
always caught by the other two. The case this cannot see is a conflict someone
hand-edited down to the separator alone — that is a real gap, and it is left
open rather than paid for with a false positive on every setext heading in the
tree.

Two more limits, stated rather than implied. Matching is anchored to line
starts, so an indented or quoted marker is invisible — that is also why the
literal markers in this docstring do not trip the scan. And the constants are
assembled from repeated characters rather than spelled out; with line
anchoring that is belt-and-braces rather than load-bearing, but it keeps the
one place a marker could legitimately sit at column zero out of the file
entirely, so no allowlist is needed and there is nowhere to park a second
entry later.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Built, not spelled — see the module docstring.
_OPEN = b"<" * 7
_CLOSE = b">" * 7
_MID = b"=" * 7


def _tracked_files() -> list[Path]:
    """Every path git tracks, as absolute paths."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [_REPO_ROOT / name for name in out.split("\0") if name]


def _marker_lines(data: bytes) -> list[tuple[int, bytes]]:
    """``(1-based line number, line)`` for every conflict marker in *data*."""
    return [
        (number, line)
        for number, line in enumerate(data.split(b"\n"), start=1)
        if line.startswith(_OPEN) or line.startswith(_CLOSE)
    ]


def scan(paths) -> tuple[list[str], int]:
    """``(offender descriptions, files read)`` for *paths*.

    The single scanning path: the repository test and the planted-file
    witnesses below both go through here, so a witness cannot pass while the
    real loop is broken.
    """
    offenders: list[str] = []
    read = 0
    for path in paths:
        try:
            if not path.is_file():
                # Tracked but not a readable regular file here: deleted from
                # the working tree, or a submodule gitlink.
                continue
            data = path.read_bytes()
        except OSError as exc:
            # Not a skip. A tracked regular file that cannot be read is a
            # hole in the scan, and a hole reported as clean is the failure
            # this guard exists to prevent.
            offenders.append(f"{_rel(path)}: unreadable ({exc.__class__.__name__})")
            continue
        read += 1
        for number, line in _marker_lines(data):
            offenders.append(f"{_rel(path)}:{number}: {line[:60].decode('utf-8', 'replace')}")
    return offenders, read


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def test_no_tracked_file_carries_a_conflict_marker():
    tracked = _tracked_files()
    assert tracked, "git ls-files returned nothing — the scan would be vacuous"

    offenders, read = scan(tracked)

    # The scan reaching files at all is part of the assertion: a broken
    # ``git ls-files``, or a loop that skipped everything, would otherwise
    # pass by finding nothing. It bounds emptiness, not completeness.
    assert read > 100, f"only {read} files read — the scan is not covering the tree"
    assert not offenders, "unresolved merge-conflict markers:\n" + "\n".join(offenders)


def test_the_scan_reports_a_planted_conflict(tmp_path):
    """Witness, driven through :func:`scan` rather than the matcher alone.

    Deleting the detection loop would leave a matcher-only witness green, so
    this one plants a file and asserts on what the real entry point reports.
    """
    planted = tmp_path / "conflicted.md"
    planted.write_bytes(
        b"before\n" + _OPEN + b" HEAD\nours\n" + _MID + b"\ntheirs\n" + _CLOSE + b" branch\nafter\n"
    )

    offenders, read = scan([planted])

    assert read == 1
    assert len(offenders) == 2, offenders
    assert offenders[0].endswith(":2: <<<<<<< HEAD"), offenders[0]
    assert offenders[1].endswith(":6: >>>>>>> branch"), offenders[1]


def test_a_conflict_survives_an_undecodable_byte(tmp_path):
    """The blind spot the UTF-8 draft had, pinned so it cannot come back.

    One invalid byte used to make the whole file unreadable to the scan, so a
    conflict below it passed. Measured against the first draft before this
    test existed.
    """
    planted = tmp_path / "mixed.md"
    planted.write_bytes(b"ok\n\xff\xfe not utf-8\n" + _OPEN + b" HEAD\nmine\n")

    with pytest.raises(UnicodeDecodeError):
        planted.read_text(encoding="utf-8")

    offenders, _read = scan([planted])

    assert len(offenders) == 1, offenders
    assert ":3: <<<<<<< HEAD" in offenders[0]


def test_a_conflict_survives_a_large_file(tmp_path):
    """No size cap: a large file is exactly where a marker hides unread.

    The first draft skipped anything over two megabytes on the reasoning that
    such blobs are not prose. A large Markdown or CSV is both.
    """
    planted = tmp_path / "big.md"
    planted.write_bytes(b"filler line\n" * 250_000 + _CLOSE + b" branch\n")
    assert planted.stat().st_size > 2_000_000

    offenders, _read = scan([planted])

    assert len(offenders) == 1, offenders
    assert offenders[0].endswith(">>>>>>> branch"), offenders[0]


def test_an_unreadable_tracked_file_is_reported_not_skipped(tmp_path, monkeypatch):
    """A hole in the scan must not be indistinguishable from a clean file."""
    planted = tmp_path / "locked.md"
    planted.write_bytes(b"content\n")

    def _boom(self, *args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    offenders, read = scan([planted])

    assert read == 0
    assert len(offenders) == 1 and "unreadable (PermissionError)" in offenders[0], offenders


@pytest.mark.parametrize(
    "data",
    [
        b"A setext heading\n=======\n\nbody\n",
        b"table sep\n| --- |\n=======\n",
    ],
)
def test_a_lone_equals_run_is_not_a_conflict(data):
    """A seven-character Markdown title underline must not fail the guard.

    This is the false positive the naive three-marker rule produces, and it
    would fire on ordinary prose — the reason the separator is not a marker on
    its own here. The cost is stated in the module docstring: a conflict
    hand-edited down to the separator alone is not detectable.
    """
    assert _marker_lines(data) == []

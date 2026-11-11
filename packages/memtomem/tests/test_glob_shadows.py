"""Direct boundary tests for _glob_shadows (namespace-rule shadowing in mm init).

Pins recursive catch-all glob shadowing behavior across nested subtrees,
identical globs, non-recursive inners, single files, absolute paths,
prefix-sharing siblings, inverted width, non-recursive outers, and mixed
tilde/absolute representations.

See https://github.com/memtomem/memtomem/issues/3243
"""

from pathlib import Path

import pytest

from memtomem.cli.init_cmd import _glob_shadows


@pytest.mark.parametrize(
    ("outer_glob", "inner_glob", "expected", "reason"),
    [
        ("~/a/**", "~/a/b/**", True, "nested subtree"),
        ("~/a/**", "~/a/**", True, "identical"),
        ("~/a/**", "~/a/b/*", True, "non-recursive inner inside"),
        ("~/a/**", "~/a/x.md", True, "single file inside"),
        ("/abs/**", "/abs/c/**", True, "absolute paths"),
        ("~/a/**", "~/ab/**", False, "sibling sharing the prefix a"),
        ("~/a/b/**", "~/a/**", False, "inner is wider than outer"),
        ("~/a/*", "~/a/b/**", False, "outer is not recursive"),
        ("~/a/**/*.md", "~/a/b/**", False, "outer is not a bare BASE/**"),
    ],
)
def test_glob_shadows_boundary_cases(
    outer_glob: str, inner_glob: str, expected: bool, reason: str
) -> None:
    """Boundary conditions for recursive glob shadowing."""
    if expected:
        assert _glob_shadows(outer_glob, inner_glob) is True
    else:
        assert _glob_shadows(outer_glob, inner_glob) is False


def test_glob_shadows_mixed_tilde_and_absolute() -> None:
    """Mixed tilde and absolute paths resolve and compare consistently."""
    home = str(Path("~").expanduser())
    assert _glob_shadows("~/a/**", home + "/a/b/**") is True
    assert _glob_shadows(home + "/a/**", "~/a/b/**") is True

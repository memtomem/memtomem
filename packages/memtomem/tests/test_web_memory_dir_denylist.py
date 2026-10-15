"""``validate_memory_dir_path`` refuses sensitive directories in any spelling.

``Path.resolve()`` does not canonicalize case for an existing directory on a
case-insensitive filesystem (APFS and NTFS by default), so a mixed-case
spelling of a denied directory names that directory while reading as a
different string (#2727).
"""

from __future__ import annotations

import sys

import pytest
from fastapi import HTTPException

from memtomem.web.routes.system import validate_memory_dir_path

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the denied prefixes are POSIX system roots"
)


def _refused(path: str) -> bool:
    try:
        validate_memory_dir_path(path)
    except HTTPException as exc:
        assert exc.status_code == 403
        return True
    return False


@posix_only
@pytest.mark.parametrize(
    "path",
    ["/usr/local", "/USR/local", "/Usr", "/system/Library", "/SYSTEM", "/ETC/ssh", "/Private/Etc"],
)
def test_denied_system_prefixes_are_refused_in_any_case(path: str) -> None:
    assert _refused(path)


@pytest.mark.parametrize("name", [".ssh", ".SSH", ".Aws", ".GNUPG", ".kUbE"])
def test_denied_components_are_refused_in_any_case(tmp_path, name: str) -> None:
    assert _refused(str(tmp_path / name / "notes"))


@pytest.mark.parametrize("name", ["notes", "ssh", "usr", ".sshx"])
def test_unrelated_directories_are_accepted(tmp_path, name: str) -> None:
    target = tmp_path / name
    assert validate_memory_dir_path(str(target)) == target.resolve()

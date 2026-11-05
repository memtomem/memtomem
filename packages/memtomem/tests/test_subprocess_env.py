"""Tests for the child subprocess environment helper (#3062)."""

from __future__ import annotations

import os
from pathlib import Path

import memtomem
from _subprocess_env import package_src_dir, subprocess_env


def test_package_src_dir_points_to_valid_source_root():
    src = package_src_dir()
    assert src.is_dir()
    assert (src / "memtomem" / "__init__.py").is_file()
    assert src == Path(memtomem.__file__).resolve().parents[1]


def test_subprocess_env_default_has_package_src_first():
    env = subprocess_env()
    src_str = str(package_src_dir())
    assert env["PYTHONPATH"].startswith(src_str)
    # package_src_dir must be either the entire string or the first path component
    assert env["PYTHONPATH"] == src_str or env["PYTHONPATH"].startswith(f"{src_str}{os.pathsep}")


def test_subprocess_env_empty_inherited_pythonpath():
    base = {"HOME": "/test/home", "PYTHONPATH": ""}
    env = subprocess_env(base=base)
    src_str = str(package_src_dir())
    assert env["PYTHONPATH"] == src_str
    assert env["HOME"] == "/test/home"


def test_subprocess_env_unset_inherited_pythonpath():
    base = {"HOME": "/test/home"}
    env = subprocess_env(base=base)
    src_str = str(package_src_dir())
    assert env["PYTHONPATH"] == src_str
    assert env["HOME"] == "/test/home"


def test_subprocess_env_preserves_inherited_pythonpath():
    base = {"PYTHONPATH": f"/custom/first{os.pathsep}/custom/second"}
    env = subprocess_env(base=base)
    src_str = str(package_src_dir())
    assert env["PYTHONPATH"] == os.pathsep.join([src_str, "/custom/first", "/custom/second"])


def test_subprocess_env_filters_empty_segments():
    base = {"PYTHONPATH": f"{os.pathsep}/custom/dir{os.pathsep}{os.pathsep}"}
    env = subprocess_env(base=base)
    src_str = str(package_src_dir())
    assert env["PYTHONPATH"] == os.pathsep.join([src_str, "/custom/dir"])


def test_subprocess_env_with_extra_paths():
    extra1 = Path("/opt/extra1")
    extra2 = "/opt/extra2"
    base = {"PYTHONPATH": "/inherited/path"}
    env = subprocess_env(base=base, extra_paths=[extra1, extra2])
    src_str = str(package_src_dir())
    assert env["PYTHONPATH"] == os.pathsep.join([src_str, str(extra1), extra2, "/inherited/path"])


def test_subprocess_env_overrides_applied():
    base = {"VAR_A": "old", "VAR_B": "keep"}
    env = subprocess_env(base=base, VAR_A="new", VAR_C="added")
    assert env["VAR_A"] == "new"
    assert env["VAR_B"] == "keep"
    assert env["VAR_C"] == "added"


def test_subprocess_env_does_not_mutate_base():
    base = {"PYTHONPATH": "/original", "VAR": "1"}
    base_copy = dict(base)
    subprocess_env(base=base, extra_paths=["/extra"], VAR="2")
    assert base == base_copy

"""MCP ``mem_export`` writes only into ``~/.memtomem/exports/`` (ADR-0006 Axis G, amended).

The bundle is indented JSON, and a shell that sources it runs a ``$(…)`` inside
a quoted line, so a bundle over a shell startup file runs note text. The caller
therefore names a file, never a directory, and the export directory must not be
a link that would carry that name somewhere else.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem.server.tools import export_import as tools
from memtomem.server.tools import meta

from .helpers import set_home


@pytest.fixture
def home(tmp_path, monkeypatch, storage):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    # A regression that resolved the name against the working directory would
    # write there; keep that inside the test's own directory too.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        tools, "_get_app_initialized", AsyncMock(return_value=SimpleNamespace(storage=storage))
    )
    return home


def exports(home: Path) -> Path:
    return home / ".memtomem" / "exports"


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


async def export(route: str, output_file: str) -> str:
    if route == "mem_export":
        return await tools.mem_export(output_file=output_file)
    return await meta.mem_do("export", {"output_file": output_file})


@pytest.mark.parametrize("route", ["mem_export", "mem_do"])
async def test_bare_name_lands_in_the_export_dir(route, home):
    msg = await export(route, "backup.json")

    target = exports(home) / "backup.json"
    assert msg.startswith("Export complete"), msg
    assert f"- Output: {target}" in msg
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == "4"
    if os.name != "nt":
        assert stat.S_IMODE(exports(home).stat().st_mode) == 0o700


async def test_an_existing_bundle_is_replaced(home):
    exports(home).mkdir(parents=True)
    (exports(home) / "backup.json").write_text("old", encoding="utf-8")

    msg = await tools.mem_export(output_file="backup.json")

    assert msg.startswith("Export complete"), msg
    assert (exports(home) / "backup.json").read_text(encoding="utf-8") != "old"


REFUSED_NAMES = [
    "~/.zshrc",
    "../.zshrc",
    "sub/x.json",
    "{outside}",
    "~/.memtomem/exports/x.json",
    "..",
    ".",
    "",
    "a\\b.json",
    "C:x.json",
    "x.json:stream",
    "CON.json",
    "nul",
    "com1.txt",
    "x.json.",
    "x .json",
    "-x.json",
    "백업.json",
    "x\0.json",
    "a" * 65,
]


@pytest.mark.parametrize("route", ["mem_export", "mem_do"])
@pytest.mark.parametrize("name", REFUSED_NAMES)
async def test_names_outside_the_grammar_are_refused(route, name, home, tmp_path, monkeypatch):
    # An absolute name would replace its target if the check regressed, so it
    # points at a sentinel inside this test's own directory.
    outside = tmp_path / "outside" / "x.json"
    outside.parent.mkdir()
    outside.write_text("sentinel", encoding="utf-8")
    name = name.replace("{outside}", str(outside))
    from memtomem.tools import export_import as lib

    calls = []

    async def must_not_run(*args, **kwargs):
        # Records and fails before any write: a regressed check must not get
        # to touch the filesystem (a drive-relative name on Windows, or a name
        # resolved against the working directory, would leave tmp_path).
        calls.append(kwargs.get("output_path"))
        raise AssertionError("export ran for a refused name")

    monkeypatch.setattr(lib, "export_chunks", must_not_run)
    before = tree(tmp_path)

    msg = await export(route, name)

    assert "output_file must be a file name, not a path" in msg, msg
    assert str(exports(home)) in msg
    assert "Export complete" not in msg
    assert tree(tmp_path) == before
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert calls == []
    assert not exports(home).exists()


def link_dir(link: Path, target: Path, kind: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "junction":
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        pytest.param(
            "junction",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only"),
        ),
    ],
)
@pytest.mark.parametrize("route", ["mem_export", "mem_do"])
async def test_a_linked_export_dir_is_refused(kind, route, home):
    rc = home / ".zshrc"
    rc.write_text("# user's own\n", encoding="utf-8")
    link_dir(exports(home), home, kind)
    before = tree(home)

    msg = await export(route, ".zshrc")

    assert "is a link" in msg, msg
    assert tree(home) == before
    assert rc.read_text(encoding="utf-8") == "# user's own\n"
    assert os.path.lexists(exports(home))


async def test_an_export_dir_that_is_a_file_is_refused(home):
    exports(home).parent.mkdir(parents=True)
    exports(home).write_text("not a directory", encoding="utf-8")

    msg = await tools.mem_export(output_file="backup.json")

    assert "is not a directory" in msg, msg
    assert exports(home).read_text(encoding="utf-8") == "not a directory"


async def test_a_link_above_the_export_dir_is_followed(home, tmp_path):
    elsewhere = tmp_path / "other-disk"
    elsewhere.mkdir()
    link_dir(home / ".memtomem", elsewhere, "symlink")

    msg = await tools.mem_export(output_file="backup.json")

    assert msg.startswith("Export complete"), msg
    assert (elsewhere / "exports" / "backup.json").is_file()


async def test_a_leaf_link_inside_the_export_dir_is_still_refused(home, tmp_path):
    exports(home).mkdir(parents=True)
    original = tmp_path / "original.json"
    original.write_text("private", encoding="utf-8")
    try:
        (exports(home) / "backup.json").symlink_to(original)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    msg = await tools.mem_export(output_file="backup.json")

    assert "symlink" in msg, msg
    assert original.read_text(encoding="utf-8") == "private"

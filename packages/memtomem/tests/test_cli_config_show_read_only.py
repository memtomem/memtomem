"""``mm config show`` must not rewrite the ``config.json`` it reports (#2417).

The legacy ``auto_discover`` migration persists to disk whenever the loader
runs with ``migrate=True`` on a file that has no ``indexing.auto_discover:
false``. Against the real loader in an isolated HOME: mocking
``load_config_overrides`` would skip the write under test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home

# A hand-written file that never mentions ``indexing`` — the shape the issue
# was found with — and one that still carries the deprecated flag explicitly.
LEGACY_FILES = {
    "no-indexing-section": {
        "embedding": {"provider": "onnx", "model": "intfloat/multilingual-e5-small"}
    },
    "explicit-auto-discover": {"indexing": {"auto_discover": True, "memory_dirs": ["~/notes"]}},
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / ".memtomem").mkdir()
    return tmp_path


@pytest.mark.parametrize("legacy", sorted(LEGACY_FILES))
@pytest.mark.parametrize("args", [["config", "show"], ["config", "show", "--json"]])
def test_config_show_leaves_legacy_config_json_untouched(
    home: Path, legacy: str, args: list[str]
) -> None:
    config_json = home / ".memtomem" / "config.json"
    config_json.write_text(json.dumps(LEGACY_FILES[legacy]), encoding="utf-8")
    before = config_json.read_bytes()

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert config_json.read_bytes() == before
    # The migration takes the config lock before writing; its absence shows
    # the write path was not entered at all, not merely that it wrote the
    # same bytes back.
    assert sorted(p.name for p in config_json.parent.iterdir()) == ["config.json"]

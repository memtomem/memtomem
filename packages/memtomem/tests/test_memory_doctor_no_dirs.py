"""``memory doctor --json`` with no memory_dirs configured (#2748).

The early return for an empty dir list printed its hint to stdout and exited
0 before the ``--json`` branch, so a consumer parsing stdout got a
``JSONDecodeError`` from a command that reported success. Under ``--json``
the hint now goes to stderr and stdout carries the normal document.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.config import Mem2MemConfig
from memtomem.storage.sqlite_backend import SqliteBackend
from test_memory_doctor import _insert_chunk

_NO_DIRS_HINT = "No memory_dirs configured. Run `mm init`."


@pytest.fixture
def no_dirs_config(tmp_path, monkeypatch):
    """A config with no memory_dirs, wired into the doctor's read-only loader."""
    from helpers import isolate_memtomem_env

    import memtomem.cli.memory_doctor_cmd as mod

    isolate_memtomem_env(monkeypatch)
    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "doctor.db"
    config.indexing.memory_dirs = []
    assert config.indexing.all_index_roots() == []
    monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)
    return config


@pytest.mark.parametrize("group", ["memory", "mem"])
def test_json_report_is_a_document_with_the_hint_on_stderr(no_dirs_config, group):
    result = CliRunner().invoke(cli, [group, "doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # No DB file yet: the store-wide note is the only entry, as in a normal run.
    assert [d["path"] for d in payload["dirs"]] == ["(database)"]
    assert payload["summary"] == {"error": 0, "warn": 0, "info": 1}
    assert _NO_DIRS_HINT in result.stderr


@pytest.mark.parametrize("apply_args", [[], ["--apply"]], ids=["dry-run", "apply"])
@pytest.mark.parametrize("group", ["memory", "mem"])
def test_json_fix_is_a_document_with_the_hint_on_stderr(no_dirs_config, group, apply_args):
    result = CliRunner().invoke(cli, [group, "doctor", "--fix", "--json", *apply_args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "clean"
    assert payload["applied"] is bool(apply_args)
    assert payload["files"] == []
    assert _NO_DIRS_HINT in result.stderr


def test_json_report_keeps_the_unowned_note(no_dirs_config, tmp_path):
    """Chunks left behind after every dir was removed are what the report is
    for; the empty-dir path must not drop that note."""
    backend = SqliteBackend(
        no_dirs_config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )

    async def _setup():
        await backend.initialize()
        _insert_chunk(backend, chunk_id="u1", source_file=tmp_path / "gone" / "a.md")
        await backend.close()

    asyncio.run(_setup())
    result = CliRunner().invoke(cli, ["memory", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [d["path"] for d in payload["dirs"]] == ["(unowned)"]


def test_human_output_is_unchanged(no_dirs_config):
    result = CliRunner().invoke(cli, ["memory", "doctor"])
    assert result.exit_code == 0
    assert result.stdout.strip() == _NO_DIRS_HINT
    assert result.stderr == ""

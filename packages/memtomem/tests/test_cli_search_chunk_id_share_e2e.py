"""End-to-end pin for issue #2064: the CLI alone can promote a hit to ``shared``.

``mm agent share`` takes a chunk UUID, but ``mm search --format json`` used
to emit only ``rank/score/source/content`` — so a shell script had to read
the UUID out of SQLite, coupling it to the storage schema. The JSON payload
now carries ``chunk_id`` (same key and canonical string form as the MCP
structured payload), which makes this the whole workflow:

    mm search q --format json | jq -r '.[0].chunk_id' | xargs mm agent share

This test runs exactly that hand-off in-process so a regression in either
half — the key disappearing, or its string form drifting away from what
``UUID(...)`` in ``_run_share`` accepts — fails here rather than in a user's
script.
"""

from __future__ import annotations

import json
import os

from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home


def test_search_json_chunk_id_feeds_agent_share(tmp_path, monkeypatch) -> None:
    # Same three-layer isolation as ``test_cli_index_noop_e2e``: HOME env,
    # the import-time ``_CONFIG_PATH`` constant, and any ``MEMTOMEM_*``
    # override leaking in from the developer's shell (a stray
    # ``SEARCH__ENABLE_BM25=false`` would turn this into a 0-result test).
    from memtomem.cli import _bootstrap

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)

    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")

    mem_dir = home / "memories"
    mem_dir.mkdir()
    (mem_dir / "note.md").write_text(
        "# memo\n\nthe kestrel hovers over the meadow\n", encoding="utf-8"
    )

    runner = CliRunner()

    r = runner.invoke(
        cli,
        [
            "init",
            "--non-interactive",
            "--provider",
            "none",
            "--memory-dir",
            str(mem_dir),
            "--mcp",
            "skip",
        ],
    )
    assert r.exit_code == 0, f"init failed: {r.output}"

    r = runner.invoke(cli, ["index", str(mem_dir)])
    assert r.exit_code == 0, f"index failed: {r.output}"

    r = runner.invoke(cli, ["search", "--format", "json", "kestrel"])
    assert r.exit_code == 0, f"json search failed: {r.output}"
    items = json.loads(r.output)
    assert items, "no results to promote"
    chunk_id = items[0]["chunk_id"]

    # The capture path, verbatim: the value goes to ``mm agent share`` as
    # the CLI printed it — no re-formatting, no SQLite lookup.
    r = runner.invoke(cli, ["agent", "share", chunk_id, "--target", "shared"])
    assert r.exit_code == 0, f"share failed: {r.output}"

    r = runner.invoke(cli, ["search", "--format", "json", "--namespace", "shared", "kestrel"])
    assert r.exit_code == 0, f"shared search failed: {r.output}"
    shared = json.loads(r.output)
    assert shared, "the shared copy is not searchable under the shared namespace"
    # The copy gets a fresh UUID (documented ``mm agent share`` semantics),
    # so the pin is that it is a *different* id, not the same one.
    assert shared[0]["chunk_id"] != chunk_id

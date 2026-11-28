"""A store set up only through ``config.d`` or ``MEMTOMEM_*`` has no ``config.json`` (#3517).

The plugin quickstarts tell that reader that ``mm status`` reports the store as not
configured and to preview it with ``mm config show`` instead of running ``mm init``.
These tests pin both halves of that advice against the real loader.

Each command runs in a fresh interpreter: ``memtomem.cli._bootstrap._CONFIG_PATH`` is
computed from ``Path.home()`` at import, so an in-process run after another test imported
the module would read that test's HOME.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from _subprocess_env import subprocess_env


def _mm(home: Path, extra: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.upper().startswith("MEMTOMEM_")}
    env = subprocess_env(base, HOME=str(home), USERPROFILE=str(home), **extra)
    return subprocess.run(
        [sys.executable, "-c", "from memtomem.cli import cli; cli()", *args],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _fragment(home: Path, db: Path) -> dict[str, str]:
    fragments = home / ".memtomem" / "config.d"
    fragments.mkdir(parents=True)
    (fragments / "10-team.json").write_text(
        json.dumps({"storage": {"sqlite_path": str(db)}}), encoding="utf-8"
    )
    return {}


def _variable(home: Path, db: Path) -> dict[str, str]:
    return {"MEMTOMEM_STORAGE__SQLITE_PATH": str(db)}


@pytest.mark.parametrize("configure", [_fragment, _variable], ids=["config.d", "env"])
def test_status_says_not_configured_while_config_show_reports_the_store(
    tmp_path: Path, configure
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "team" / "memtomem.db"
    extra = configure(home, db)

    status = _mm(home, extra, "status")
    assert status.returncode != 0
    assert "memtomem is not configured" in status.stderr

    shown = _mm(home, extra, "config", "show", "--json")
    assert shown.returncode == 0, shown.stderr
    assert Path(json.loads(shown.stdout)["storage"]["sqlite_path"]).expanduser() == db
    # Neither command creates the file whose absence `mm status` reports.
    assert not (home / ".memtomem" / "config.json").exists()

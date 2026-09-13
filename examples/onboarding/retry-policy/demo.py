"""Verify the sample's CLI journey without touching the user's store.

Run with Python from an environment containing memtomem==0.6.1.
All writes are confined to a disposable home; no client is registered.
This proves CLI/source persistence, not an AI client's fresh-session behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

FIXTURE = Path(__file__).resolve().parent / "docs" / "auth-callback-adr.md"
DECISION = (
    "Retry policy: retry at most 5 times with 250 ms backoff and jitter. "
    "Reason: avoid a retry storm during staged rollout."
)


def main():
    mm = shutil.which("mm", path=str(Path(sys.executable).parent))
    if mm is None:
        raise RuntimeError("Install memtomem==0.6.1 in this Python environment first.")
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="memtomem-coding-demo-") as temporary:
        root = Path(temporary).resolve()
        allowed = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "TMPDIR"}
        env = {k: v for k, v in os.environ.items() if k in allowed}
        env.update(
            HOME=str(root),
            USERPROFILE=str(root),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_DATA_HOME=str(root / "data"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_CACHE_HOME=str(root / "cache"),
        )

        def run(*args):
            result = subprocess.run(
                [mm, *args],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=90,
            )
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result.stdout

        run("init", "--preset", "minimal", "--non-interactive", "--mcp", "skip")
        status = json.loads(run("status", "--json"))
        assert "error" not in status and status["index"]["total_chunks"] == 0
        assert json.loads(run("search", "Retry policy", "--format", "json")) == []
        print("PASS empty-store")
        added = json.loads(run("add", DECISION, "--tags", "decision", "--json"))
        assert added["ok"] and added["chunks"] > 0
        # A separate mm process reads the same on-disk state.
        hits = json.loads(run("search", "Retry policy", "--format", "json"))
        assert hits and "retry storm" in json.dumps(hits)
        print("PASS decision-round-trip")
        copied = root / "docs" / FIXTURE.name
        copied.parent.mkdir()
        shutil.copyfile(FIXTURE, copied)
        run("index", str(copied))
        adr = json.loads(run("search", "legacy callback", "--format", "json"))
        assert adr, adr
        assert FIXTURE.name in json.dumps(adr)
        # JSON search returns a 200-character preview; use context for full text.
        context = run("search", "legacy callback", "--format", "context")
        assert "AUTH_CALLBACK_V2_ENABLED" in context and FIXTURE.name in context
        print("PASS adr-source")
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == digest
    assert not root.exists()
    print("PASS fixture-preserved-and-state-cleaned")


if __name__ == "__main__":
    main()

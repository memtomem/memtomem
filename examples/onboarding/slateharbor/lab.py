"""Disposable CLI lab. Reads real search results; never reads evaluation answers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
# Runs BEFORE importing memtomem in every CLI child, including fresh-process searches.
# This blocks these Python socket APIs, not arbitrary native networking syscalls.
BOOTSTRAP = """import socket
def denied(*args, **kwargs):
    raise RuntimeError('Slateharbor offline lab blocked socket access')
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied
socket.create_connection = denied
from memtomem.cli import cli
cli()
"""


def verify_sources(root=ROOT):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        path = root / "project" / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(
                f"Sample changed: {name}; restore the bundle before starting a new lab"
            )
    return manifest


class Lab:
    """Parent HOME/cwd never change. All CLI processes share only this temporary home."""

    def __init__(self, source_root=ROOT):
        self.source_root = Path(source_root)
        verify_sources(self.source_root)
        self._temporary = tempfile.TemporaryDirectory(prefix="slateharbor-")
        self.root = Path(self._temporary.name).resolve()
        self.project = self.root / "project"
        allowed = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "TMPDIR"}
        self.env = {k: v for k, v in os.environ.items() if k in allowed}
        self.env.update(
            {
                "HOME": str(self.root),
                "USERPROFILE": str(self.root),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "MEMTOMEM_FASTEMBED_CACHE": str(self.root / "cache/models"),
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
                "PYTHONDONTWRITEBYTECODE": "1",
                # Keep independent policy headings/functions/keys as separate retrieval units.
                "MEMTOMEM_INDEXING__MIN_CHUNK_TOKENS": "0",
                "MEMTOMEM_INDEXING__TARGET_CHUNK_TOKENS": "0",
                "MEMTOMEM_SEARCH__BM25_CANDIDATES": "100",
            }
        )
        self.processes = 0
        self.index_seconds = None
        self.closed = False
        try:
            shutil.copytree(
                self.source_root / "project",
                self.project,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            self.run("init", "--preset", "minimal", "--non-interactive", "--mcp", "skip")
        except BaseException:
            self.close()
            raise

    def run(self, *arguments):
        if self.closed:
            raise RuntimeError(
                "이 실습 환경은 이미 정리됐습니다(9번 셀). "
                "준비 셀부터 다시 실행한 뒤 이 셀을 실행하세요."
            )
        self.processes += 1
        result = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP, *arguments],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError(
                f"mm {' '.join(arguments)} failed:\n{result.stdout}\n{result.stderr}"
            )
        return result.stdout

    def index(self):
        start = time.monotonic()
        result = self.run("index", str(self.project))
        self.index_seconds = round(time.monotonic() - start, 2)
        return result

    def search(self, query, source=None, top_k=5, namespace=None, tag=None):
        args = [
            "search",
            query,
            "--format",
            "json",
            "--top-k",
            str(max(top_k, 100) if source else top_k),
        ]
        if source:
            args += ["--source-filter", source]
        if namespace:
            args += ["--namespace", namespace]
        if tag:
            args += ["--tag-filter", tag]
        return json.loads(self.run(*args))[:top_k]

    _LANGUAGES = {".py": "python", ".json": "json", ".yaml": "yaml", ".toml": "toml"}

    def context(self, query, source=None, top_k=3, tag=None):
        """Render the top hits from their stored content, keyed by chunk id.

        The CLI's own ``--format context`` block is a presentation format: a
        result body can legitimately contain a line shaped exactly like a
        result header, so slicing that text by position cannot be made safe.
        ``--format json`` carries the authoritative identities but truncates
        ``content`` to 200 characters, so the full body is read back from the
        lab's own index by ``chunk_id``. Nothing here parses rendered output.
        """
        hits = self.search(query, source=source, top_k=top_k, tag=tag, namespace=None)
        if not hits:
            return ""
        bodies = self._chunk_bodies([hit["chunk_id"] for hit in hits])
        lines = [f"## Relevant Memories (query: {query})", ""]
        for hit in hits:
            body, source_file, heading = bodies[hit["chunk_id"]]
            lines.append(
                f"### [{hit['rank']}] {heading or source_file} (score: {hit['score']:.3f})"
            )
            lines.append(f"Source: {source_file}")
            lines.append("")
            language = self._LANGUAGES.get(Path(source_file).suffix)
            lines.append(f"```{language}\n{body.strip()}\n```" if language else body.strip())
            lines.append("")
        return "\n".join(lines)

    def _chunk_bodies(self, chunk_ids):
        """Full stored content per chunk id. A missing id is an error, not a gap."""
        with self._index() as connection:
            placeholders = ",".join("?" * len(chunk_ids))
            rows = connection.execute(
                f"SELECT id, content, source_file, heading_hierarchy "  # noqa: S608
                f"FROM chunks WHERE id IN ({placeholders})",
                chunk_ids,
            ).fetchall()
        bodies = {}
        for chunk_id, content, source_file, headings in rows:
            try:
                parsed = json.loads(headings) if headings else []
            except (TypeError, ValueError):
                parsed = []
            heading = " > ".join(parsed) if isinstance(parsed, list) and parsed else ""
            bodies[chunk_id] = (content, source_file, heading)
        missing = [chunk_id for chunk_id in chunk_ids if chunk_id not in bodies]
        if missing:
            raise RuntimeError(f"Search returned chunk ids absent from the index: {missing}")
        return bodies

    def _index(self):
        """Read-only connection to this lab's chunk index."""
        for database in self.root.rglob("*.db"):
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "chunks" in tables:
                return contextlib.closing(connection)
            connection.close()
        raise RuntimeError("No lab SQLite index found")

    def sources(self, hits):
        return [Path(h["source"]).resolve().relative_to(self.project).as_posix() for h in hits]

    def inventory(self):
        # Read-only inspection of this lab's index; no precomputed or generator counts.
        with self._index() as connection:
            rows = connection.execute(
                "SELECT source_file, count(*) FROM chunks GROUP BY source_file"
            ).fetchall()
        counts = {}
        for source, chunks in rows:
            path = Path(source).resolve()
            if not path.is_relative_to(self.project):
                continue
            entry = counts.setdefault(path.suffix, {"files": 0, "chunks": 0})
            entry["files"] += 1
            entry["chunks"] += chunks
            # Per-file floor: a whole-file fallback shows up here as 1 even
            # when the aggregate total still looks healthy.
            entry["min_chunks"] = min(entry.get("min_chunks", chunks), chunks)
        return {
            "formats": counts,
            "files": sum(v["files"] for v in counts.values()),
            "chunks": sum(v["chunks"] for v in counts.values()),
            "index_seconds": self.index_seconds,
        }

    def policy_checks(self):
        """Return the test count per domain. An empty discover is a failure, not a pass."""
        counts = []
        for domain in ("auth", "billing", "notifications", "jobs", "files", "reports"):
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", f"{domain}/src"],
                cwd=self.project,
                env=self.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            # `unittest discover` exits 0 when it collects nothing, and a run
            # of nothing but skips or expected failures also exits 0 while
            # reporting a count. The contract here is "N policies actually
            # ran and passed", so require exactly one run summary and a bare
            # `OK` -- `OK (skipped=3)` and friends are rejected.
            summaries = re.findall(r"(?m)^Ran (\d+) tests? in ", result.stderr)
            if len(summaries) != 1 or int(summaries[0]) == 0:
                raise AssertionError(f"{domain}: no single policy test run\n{result.stderr}")
            if not re.search(r"(?m)^OK$", result.stderr):
                raise AssertionError(f"{domain}: not a clean pass\n{result.stderr}")
            counts.append(int(summaries[0]))
        return counts

    def close(self):
        self.closed = True
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

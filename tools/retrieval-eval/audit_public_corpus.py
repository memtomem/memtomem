#!/usr/bin/env python3
"""Audit the public synthetic retrieval corpus before calibration or release."""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPO_ROOT / "packages/memtomem/tests/fixtures/corpus_v2"
PORTFOLIO_PATH = Path(__file__).with_name("query_portfolio.py")
VALIDATOR_PATH = Path(__file__).with_name("drift_validator.py")

EXPECTED_FILES = 48
EXPECTED_CHUNKS = 192
EXPECTED_QUERIES = 100
GENRES = frozenset({"runbook", "postmortem", "adr", "troubleshooting"})

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b")
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


@dataclass(frozen=True)
class CorpusAudit:
    files: int
    chunks: int
    queries: int
    secret_hits: int
    disallowed_emails: int
    global_ip_literals: int
    corpus_sha256: str
    query_sha256: str


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tree_hash(paths: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def audit() -> CorpusAudit:
    from memtomem import privacy

    validator = _load("public_corpus_drift_validator", VALIDATOR_PATH)
    portfolio = _load("public_corpus_query_portfolio", PORTFOLIO_PATH)
    files = sorted(p for p in CORPUS_ROOT.rglob("*.md") if p.stem in GENRES)

    chunks = 0
    secret_hits = 0
    disallowed_emails: list[str] = []
    global_ips: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("> Synthetic content for search regression testing"):
            raise ValueError(f"missing synthetic disclaimer: {path}")
        chunks += len(validator.parse_fixture(path))
        secret_hits += len(privacy.scan(text))
        for match in _EMAIL_RE.finditer(text):
            domain = match.group(1).lower()
            if domain not in {"example.com", "example.org", "example.net"}:
                disallowed_emails.append(match.group(0))
        for raw in _IPV4_RE.findall(text):
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if address.is_global:
                global_ips.append(raw)

    result = CorpusAudit(
        files=len(files),
        chunks=chunks,
        queries=len(portfolio.QUERIES),
        secret_hits=secret_hits,
        disallowed_emails=len(disallowed_emails),
        global_ip_literals=len(global_ips),
        corpus_sha256=_tree_hash(files, CORPUS_ROOT),
        query_sha256=hashlib.sha256(PORTFOLIO_PATH.read_bytes()).hexdigest(),
    )
    expected = (EXPECTED_FILES, EXPECTED_CHUNKS, EXPECTED_QUERIES, 0, 0, 0)
    observed = (
        result.files,
        result.chunks,
        result.queries,
        result.secret_hits,
        result.disallowed_emails,
        result.global_ip_literals,
    )
    if observed != expected:
        raise ValueError(
            "public corpus audit failed: "
            f"expected files/chunks/queries/secrets/emails/global_ips={expected}, "
            f"observed={observed}; emails={disallowed_emails}, global_ips={global_ips}"
        )
    return result


def main() -> int:
    print(json.dumps(asdict(audit()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

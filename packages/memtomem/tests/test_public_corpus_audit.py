"""Public synthetic corpus privacy, provenance, and shape contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _audit_module():
    path = Path(__file__).resolve().parents[3] / "tools/retrieval-eval/audit_public_corpus.py"
    spec = importlib.util.spec_from_file_location("audit_public_corpus", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_corpus_is_complete_synthetic_and_secret_free():
    result = _audit_module().audit()
    assert (result.files, result.chunks, result.queries) == (48, 192, 100)
    assert result.secret_hits == 0
    assert result.disallowed_emails == 0
    assert result.global_ip_literals == 0
    assert len(result.corpus_sha256) == 64
    assert len(result.query_sha256) == 64

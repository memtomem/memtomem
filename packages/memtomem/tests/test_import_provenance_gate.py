"""ADR-0006 Axis F.3 — provenance-aware redaction gate on bundle import.

Pins the trust-boundary behavior issue #1483 builds:

* A **foreign** bundle (no valid local-provenance marker) carrying a
  secret-shaped value is rejected by default and accepted only with the
  explicit ``force_unsafe`` override — across the core ``import_chunks`` path,
  the MCP ``mem_import`` tool, and the Web ``POST /export/import`` route.
* A **self-export** (valid provenance marker) round-trips unchanged: the
  redaction re-scan is skipped, so a self-exported bundle that legitimately
  contains a secret imports without ``force_unsafe``.
* Tampering with (or appending to) a signed bundle invalidates the marker, so
  it falls back to the foreign gate.
* Malformed records are dropped before the scan, and a single blocked record
  rejects the whole import atomically (no partial commit).
* Per-install key file handling is symlink-safe and fails closed on signing /
  safe on verify.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from memtomem import provenance
from memtomem.config import StorageConfig
from memtomem.models import Chunk, ChunkMetadata, ChunkType
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.tools.export_import import (
    ImportPrivacyError,
    export_chunks,
    import_chunks,
)

# Matches DEFAULT_PATTERNS: ``api_key=`` (pattern 0) and ``sk-…`` (pattern 2).
_SECRET = "api_key=sk-abcdefghijklmnopqrstuvwxyz0123"
_CLEAN = "# Notes\nOrdinary prose about caching, retries, and backoff.\n"


# --- fixtures / helpers ----------------------------------------------------


class _FakeEmbedder:
    """Hermetic dim-8 embedder — no model download."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return "fake-test"

    async def embed_texts(self, texts, *, on_progress=None):
        return [[0.1] * self._dim for _ in texts]

    async def embed_query(self, query):
        return [0.1] * self._dim

    async def close(self) -> None:
        pass


async def _make_storage(tmp_path: Path, name: str = "t") -> SqliteBackend:
    cfg = StorageConfig()
    cfg.sqlite_path = tmp_path / f"{name}.db"
    storage = SqliteBackend(cfg, dimension=8)
    await storage.initialize()
    return storage


def _record(content: str, *, source: str = "notes.md", ns: str = "default") -> dict:
    return {
        "chunk_id": str(uuid4()),
        "content_hash": "",  # recomputed by Chunk.__post_init__
        "content": content,
        "source_file": source,
        "heading_hierarchy": [],
        "chunk_type": "raw_text",
        "start_line": 1,
        "end_line": 1,
        "language": "en",
        "tags": [],
        "namespace": ns,
        "created_at": "2026-06-30T00:00:00+00:00",
    }


def _bundle_dict(records: list[dict], *, marker: dict | None = None) -> dict:
    return {
        "version": "2",
        "exported_at": "2026-06-30T00:00:00+00:00",
        "total_chunks": len(records),
        "chunks": records,
        "provenance": marker,
    }


def _write_bundle(path: Path, records: list[dict], *, marker: dict | None = None) -> Path:
    path.write_text(json.dumps(_bundle_dict(records, marker=marker)), encoding="utf-8")
    return path


def _make_chunk(content: str, *, source: str = "a.md", ns: str = "default") -> Chunk:
    meta = ChunkMetadata(
        source_file=Path(source),
        heading_hierarchy=(),
        chunk_type=ChunkType("raw_text"),
        start_line=1,
        end_line=1,
        language="en",
        tags=(),
        namespace=ns,
    )
    chunk = Chunk(
        content=content,
        metadata=meta,
        id=uuid4(),
        created_at=datetime.now(timezone.utc),
    )
    chunk.embedding = [0.1] * 8
    return chunk


# --- core import_chunks gate ----------------------------------------------


async def test_foreign_bundle_with_secret_rejected_by_default(tmp_path):
    storage = await _make_storage(tmp_path)
    bundle = _write_bundle(tmp_path / "foreign.json", [_record(_SECRET)], marker=None)

    with pytest.raises(ImportPrivacyError) as ei:
        await import_chunks(storage, _FakeEmbedder(), bundle)
    assert ei.value.blocked_records == 1
    # Atomic reject — nothing committed.
    assert len(await storage.recall_chunks(limit=100)) == 0


async def test_foreign_bundle_with_secret_imports_with_force_unsafe(tmp_path):
    storage = await _make_storage(tmp_path)
    bundle = _write_bundle(tmp_path / "foreign.json", [_record(_SECRET)], marker=None)

    stats = await import_chunks(storage, _FakeEmbedder(), bundle, force_unsafe=True)
    assert stats.imported_chunks == 1
    assert len(await storage.recall_chunks(limit=100)) == 1


async def test_foreign_clean_bundle_imports_without_force(tmp_path):
    storage = await _make_storage(tmp_path)
    bundle = _write_bundle(tmp_path / "clean.json", [_record(_CLEAN)], marker=None)

    stats = await import_chunks(storage, _FakeEmbedder(), bundle)
    assert stats.imported_chunks == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("heading_hierarchy", [_SECRET]),  # embedded via retrieval_content
        ("source_file", _SECRET),  # stored + displayed
        ("tags", [_SECRET]),  # stored + filterable
    ],
)
async def test_foreign_bundle_secret_in_metadata_is_gated(tmp_path, field, value):
    """ADR-0006 F.3: import scans the full retrievable surface, so a secret
    smuggled into attacker-controlled metadata (not just ``content``) is
    rejected by default and admitted only with ``force_unsafe``."""
    storage = await _make_storage(tmp_path)
    rec = _record(_CLEAN)
    rec[field] = value
    bundle = _write_bundle(tmp_path / f"meta_{field}.json", [rec], marker=None)

    with pytest.raises(ImportPrivacyError):
        await import_chunks(storage, _FakeEmbedder(), bundle)
    assert len(await storage.recall_chunks(limit=100)) == 0

    stats = await import_chunks(storage, _FakeEmbedder(), bundle, force_unsafe=True)
    assert stats.imported_chunks == 1


async def test_self_export_with_secret_roundtrips_unchanged(tmp_path):
    """A self-exported bundle (valid marker) with a secret imports without
    force_unsafe — the marker is honored and the re-scan is skipped."""
    key_path = tmp_path / "self.provenance_key"
    src = await _make_storage(tmp_path, "src")
    await src.upsert_chunks([_make_chunk(_CLEAN + _SECRET, source="secretnote.md")])

    bundle_path = tmp_path / "self.json"
    bundle = await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
    assert bundle.provenance and bundle.provenance["scheme"] == provenance.SCHEME

    # Fresh install sharing the same key → marker verifies → no force_unsafe.
    dst = await _make_storage(tmp_path, "dst")
    stats = await import_chunks(dst, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)
    assert stats.imported_chunks == 1
    rows = await dst.recall_chunks(limit=100)
    assert any(_SECRET in c.content for c in rows)


async def test_self_export_verifies_only_with_matching_key(tmp_path):
    """The same self-export imported under a *different* install key is foreign
    and gets gated (proves the marker is key-bound, not just present)."""
    key_path = tmp_path / "src.provenance_key"
    src = await _make_storage(tmp_path, "src")
    await src.upsert_chunks([_make_chunk(_CLEAN + _SECRET, source="secretnote.md")])
    bundle_path = tmp_path / "self.json"
    await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)

    dst = await _make_storage(tmp_path, "dst")
    other_key = tmp_path / "other.provenance_key"  # absent → no key → foreign
    with pytest.raises(ImportPrivacyError):
        await import_chunks(dst, _FakeEmbedder(), bundle_path, provenance_key_path=other_key)


async def test_tampered_self_export_is_gated(tmp_path):
    """Appending a record to a validly-signed bundle invalidates the marker."""
    key_path = tmp_path / "k.provenance_key"
    key = provenance.load_or_create_key_for_export(key_path)
    records = [_record(_CLEAN, source="clean.md")]
    marker = provenance.make_marker(records, key)

    # Tamper: append a secret-bearing record after signing.
    tampered = records + [_record(_SECRET, source="evil.md")]
    bundle = _write_bundle(tmp_path / "tampered.json", tampered, marker=marker)

    storage = await _make_storage(tmp_path)
    with pytest.raises(ImportPrivacyError):
        await import_chunks(storage, _FakeEmbedder(), bundle, provenance_key_path=key_path)


async def test_malformed_record_not_scanned_and_atomic_reject(tmp_path):
    """A malformed record is dropped pre-scan; a single blocked record rejects
    the whole import (the clean record is not committed)."""
    storage = await _make_storage(tmp_path)
    records = [
        {"not_a": "valid record"},  # missing content/source_file → skipped
        _record(_CLEAN, source="clean.md"),
        _record(_SECRET, source="secret.md"),
    ]
    bundle = _write_bundle(tmp_path / "mixed.json", records, marker=None)

    with pytest.raises(ImportPrivacyError) as ei:
        await import_chunks(storage, _FakeEmbedder(), bundle)
    assert ei.value.blocked_records == 1  # only the secret record, not the malformed one
    assert len(await storage.recall_chunks(limit=100)) == 0  # nothing committed


async def test_empty_bundle_is_noop(tmp_path):
    storage = await _make_storage(tmp_path)
    bundle = _write_bundle(tmp_path / "empty.json", [], marker=None)
    stats = await import_chunks(storage, _FakeEmbedder(), bundle)
    assert stats.total_chunks == 0 and stats.imported_chunks == 0


# --- per-install key file handling ----------------------------------------


def test_export_key_file_is_owner_private(tmp_path):
    key_path = tmp_path / "perm.provenance_key"
    provenance.load_or_create_key_for_export(key_path)
    assert key_path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_symlinked_key_is_foreign_on_verify_and_fails_closed_on_export(tmp_path):
    real = tmp_path / "real_key"
    real.write_text("ab" * 32, encoding="utf-8")  # 32-byte key as 64 hex chars
    link = tmp_path / "linked.provenance_key"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")

    # Verify side fails *safe*: a symlinked key is treated as no key → foreign.
    assert provenance.load_key_for_verify(link) is None
    # Sign side fails *closed*: refuse to sign with a symlinked key.
    with pytest.raises(provenance.ProvenanceKeyError):
        provenance.load_or_create_key_for_export(link)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission model")
def test_group_or_world_accessible_key_rejected(tmp_path):
    """A regular key with group/other permission bits cannot be trusted — an
    attacker who can read it could forge markers — so it is foreign on verify
    and fails closed on export (Codex review hardening)."""
    key_path = tmp_path / "loose.provenance_key"
    key_path.write_text("ab" * 32, encoding="utf-8")  # valid 32-byte hex key
    os.chmod(key_path, 0o644)  # group/other readable

    assert provenance.load_key_for_verify(key_path) is None
    with pytest.raises(provenance.ProvenanceKeyError):
        provenance.load_or_create_key_for_export(key_path)


def test_verify_missing_key_is_foreign(tmp_path):
    assert provenance.load_key_for_verify(tmp_path / "nope.key") is None


# --- MCP ingress: mem_import ----------------------------------------------


async def test_mem_import_foreign_secret_rejected_then_force_unsafe(tmp_path, monkeypatch):
    import memtomem.server.tools.export_import as mcp_ei

    storage = await _make_storage(tmp_path)
    # ``_session_lock`` / ``current_session_id``: ``mem_import`` marks the
    # active session's provenance incomplete (#1876) — a bulk import
    # changes the session's chunk set without being summarizable from it.
    # There is no session here, so the marker is a no-op; the stub only
    # has to let the read happen.
    app = SimpleNamespace(
        storage=storage,
        embedder=_FakeEmbedder(),
        _session_lock=asyncio.Lock(),
        current_session_id=None,
    )

    async def _fake_get_app(ctx):
        return app

    monkeypatch.setattr(mcp_ei, "_get_app_initialized", _fake_get_app)
    bundle = _write_bundle(tmp_path / "foreign.json", [_record(_SECRET)], marker=None)

    msg = await mcp_ei.mem_import(input_file=str(bundle))
    assert "rejected" in msg.lower()
    assert "force_unsafe" in msg
    assert len(await storage.recall_chunks(limit=100)) == 0

    msg2 = await mcp_ei.mem_import(input_file=str(bundle), force_unsafe=True)
    assert "Import complete" in msg2
    assert len(await storage.recall_chunks(limit=100)) == 1


# --- Web ingress: POST /export/import -------------------------------------


def _web_app(storage, embedder):
    from fastapi import FastAPI

    from memtomem.web.deps import get_embedder, get_storage
    from memtomem.web.routes import export as web_export

    app = FastAPI()
    app.include_router(web_export.router)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_embedder] = lambda: embedder
    return app


async def test_web_import_foreign_secret_403_then_force_unsafe(tmp_path):
    storage = await _make_storage(tmp_path)
    app = _web_app(storage, _FakeEmbedder())
    payload = json.dumps(_bundle_dict([_record(_SECRET)], marker=None))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/export/import",
            files={"file": ("foreign.json", payload, "application/json")},
        )
        assert res.status_code == 403, res.text
        detail = res.json()["detail"]
        assert detail["detail"] == "redaction_blocked"
        assert detail["surface"] == "web_api_import"
        assert detail["blocked_records"] == 1
        assert _SECRET not in res.text  # matched bytes never leak

        res2 = await client.post(
            "/export/import",
            files={"file": ("foreign.json", payload, "application/json")},
            data={"force_unsafe": "true"},
        )
        assert res2.status_code == 200, res2.text
        assert res2.json()["imported_chunks"] == 1


# --- review-hardening regressions ------------------------------------------


async def test_secret_in_malformed_field_not_leaked_to_logs(tmp_path, caplog):
    """A foreign bundle hiding a secret in a field that fails parsing (here
    ``created_at`` → ``datetime.fromisoformat`` raises ``ValueError`` embedding
    the value) must drop the record WITHOUT echoing the secret bytes to the log
    sink. Regression for the parse-error log-leak (Codex review)."""
    import logging

    storage = await _make_storage(tmp_path)
    bad = _record(_CLEAN)
    bad["created_at"] = _SECRET  # fromisoformat(secret) -> ValueError(secret)
    bundle = _write_bundle(tmp_path / "leak.json", [bad], marker=None)

    with caplog.at_level(logging.WARNING):
        stats = await import_chunks(storage, _FakeEmbedder(), bundle)

    assert stats.skipped_chunks == 1
    assert len(await storage.recall_chunks(limit=100)) == 0
    assert _SECRET not in caplog.text
    assert "sk-" not in caplog.text


async def test_force_unsafe_import_records_bypass_and_audits(tmp_path, monkeypatch):
    """ADR-0006 E.1/F.3: a ``force_unsafe`` import of a foreign secret-bearing
    bundle records a ``bypassed`` counter and emits a structured audit line
    (carrying counts, never the matched bytes)."""
    from memtomem import privacy

    recorded: list[tuple[str, str]] = []
    audits: list[dict] = []
    monkeypatch.setattr(privacy, "record", lambda outcome, tool: recorded.append((outcome, tool)))
    monkeypatch.setattr(
        privacy,
        "emit_bypass_audit",
        lambda *, surface, content_chars, hits, audit_context=None: audits.append(
            {"surface": surface, "hits": hits}
        ),
    )

    storage = await _make_storage(tmp_path)
    bundle = _write_bundle(tmp_path / "foreign.json", [_record(_SECRET)], marker=None)
    stats = await import_chunks(
        storage, _FakeEmbedder(), bundle, force_unsafe=True, surface="mem_import"
    )

    assert stats.imported_chunks == 1
    assert ("bypassed", "mem_import") in recorded
    assert audits and audits[0]["surface"] == "mem_import" and audits[0]["hits"] >= 1


def test_non_ascii_signature_is_foreign_not_error(tmp_path):
    """A marker whose signature carries non-ASCII bytes must verify ``False``
    (foreign), not raise from ``hmac.compare_digest`` (Codex/crypto-lens)."""
    key = provenance.load_or_create_key_for_export(tmp_path / "k.provenance_key")
    bad_marker = {
        "scheme": provenance.SCHEME,
        "algo": provenance.ALGO,
        "signature": "ñ" * 64,  # non-ASCII str
    }
    assert provenance.verify_marker([_record(_CLEAN)], bad_marker, key) is False


async def test_self_export_unicode_content_roundtrips(tmp_path):
    """Non-ASCII content + source/heading round-trips: the canonical payload is
    ``ensure_ascii=False`` over the parsed objects, so the marker still verifies
    on the same install (determinism invariant, beyond ASCII)."""
    key_path = tmp_path / "u.provenance_key"
    src = await _make_storage(tmp_path, "src")
    await src.upsert_chunks([_make_chunk("한국어 메모 🔐 with a café", source="유니코드.md")])

    bundle_path = tmp_path / "uni.json"
    bundle = await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
    assert bundle.provenance is not None

    dst = await _make_storage(tmp_path, "dst")
    stats = await import_chunks(dst, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)
    assert stats.imported_chunks == 1

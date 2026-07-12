from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.middleware import body_limit

# These cases are imported by ``test_web_routes.py`` so they share its large
# application fixtures without duplicating those fixtures across modules.
__test__ = False


def _set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))


def _assert_no_quarantine(home: Path) -> None:
    upload_dir = home / ".memtomem" / "uploads"
    if upload_dir.exists():
        assert not list(upload_dir.glob(".quarantine-*"))


class TestUploadQuarantineBoundaries:
    async def test_file_size_exact_limit_passes_and_plus_one_fails_without_final_writes(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.web import upload_quarantine

        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(upload_quarantine, "MAX_UPLOAD_BYTES", 4)
        monkeypatch.setattr(upload_quarantine, "MAX_UPLOAD_AGGREGATE_BYTES", 20)

        ok = await client.post("/api/upload", files={"files": ("ok.md", b"1234")})
        assert ok.status_code == 200, ok.text

        failed = await client.post("/api/upload", files={"files": ("large.md", b"12345")})
        assert failed.status_code == 413
        assert not (tmp_path / ".memtomem" / "uploads" / "large.md").exists()
        _assert_no_quarantine(tmp_path)

    async def test_aggregate_exact_limit_passes_and_plus_one_has_no_final_writes(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.web import upload_quarantine

        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(upload_quarantine, "MAX_UPLOAD_BYTES", 10)
        monkeypatch.setattr(upload_quarantine, "MAX_UPLOAD_AGGREGATE_BYTES", 6)
        exact = [("files", ("a.md", b"123")), ("files", ("b.md", b"456"))]
        assert (await client.post("/api/upload", files=exact)).status_code == 200

        overflow = [("files", ("c.md", b"123")), ("files", ("d.md", b"4567"))]
        response = await client.post("/api/upload", files=overflow)
        assert response.status_code == 413
        upload_dir = tmp_path / ".memtomem" / "uploads"
        assert not (upload_dir / "c.md").exists()
        assert not (upload_dir / "d.md").exists()
        _assert_no_quarantine(tmp_path)

    async def test_exact_file_count_passes_and_plus_one_is_rejected_before_upload_dir(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        exact = [("files", (f"{i}.md", b"x")) for i in range(32)]
        assert (await client.post("/api/upload", files=exact)).status_code == 200

        second_home = tmp_path / "overflow"
        second_home.mkdir()
        _set_home(monkeypatch, second_home)
        overflow = [("files", (f"{i}.md", b"x")) for i in range(33)]
        response = await client.post("/api/upload", files=overflow)
        assert response.status_code == 413
        assert not (second_home / ".memtomem" / "uploads").exists()

    async def test_text_field_and_empty_multipart_are_generic_client_errors(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        text = await client.post("/api/upload", data={"note": "x"}, files={"files": ("a.md", b"x")})
        assert text.status_code == 400
        assert text.json() == {"detail": "Malformed upload"}

        empty = await client.post(
            "/api/upload",
            content=b"--empty--\r\n",
            headers={"content-type": "multipart/form-data; boundary=empty"},
        )
        assert empty.status_code == 422
        assert empty.json() == {"detail": "No upload files provided"}


class TestUploadQuarantineLifecycle:
    async def test_decode_failure_cleans_quarantine_and_writes_no_final(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        response = await client.post("/api/upload", files={"files": ("bad.md", b"\xff")})
        assert response.status_code == 200
        assert response.json()["files"][0]["error"].startswith("Decode failed:")
        assert not (tmp_path / ".memtomem" / "uploads" / "bad.md").exists()
        _assert_no_quarantine(tmp_path)

    async def test_blocked_file_cleans_quarantine(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        secret = b"token=sk-" + b"a" * 30
        response = await client.post("/api/upload", files={"files": ("secret.md", secret)})
        assert response.status_code == 200
        assert response.json()["files"][0]["error"].startswith("redaction_blocked")
        assert not (tmp_path / ".memtomem" / "uploads" / "secret.md").exists()
        _assert_no_quarantine(tmp_path)

    async def test_copy_cancellation_propagates_and_cleans_quarantine(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.web import upload_quarantine

        _set_home(monkeypatch, tmp_path)

        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(upload_quarantine, "_copy_to_quarantine", cancel)
        # Starlette's BaseHTTPMiddleware converts a cancelled downstream
        # response into this transport-level failure; the cleanup contract is
        # the assertion that matters here.
        with pytest.raises(RuntimeError, match="No response returned"):
            await client.post("/api/upload", files={"files": ("safe.md", b"safe")})
        _assert_no_quarantine(tmp_path)

    async def test_unexpected_copy_failure_is_generic_and_cleans_quarantine(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.web import upload_quarantine

        _set_home(monkeypatch, tmp_path)

        async def fail(*args, **kwargs):
            raise RuntimeError("sensitive server detail")

        monkeypatch.setattr(upload_quarantine, "_copy_to_quarantine", fail)
        response = await client.post("/api/upload", files={"files": ("safe.md", b"safe")})
        assert response.status_code == 500
        assert response.json() == {"detail": "Upload processing failed"}
        _assert_no_quarantine(tmp_path)

    async def test_index_failure_keeps_promoted_file_and_cleans_quarantine(
        self, app, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        app.state.index_engine.index_file = AsyncMock(side_effect=RuntimeError("internal detail"))
        response = await client.post("/api/upload", files={"files": ("kept.md", b"safe")})
        assert response.status_code == 200
        result = response.json()["files"][0]
        assert result["error"] == "Upload processing failed"
        assert Path(result["path"]).read_bytes() == b"safe"
        _assert_no_quarantine(tmp_path)

    async def test_same_name_never_overwrites(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)

        async def upload(content: bytes):
            return await client.post("/api/upload", files={"files": ("same.md", content)})

        first, second = await asyncio.gather(upload(b"first"), upload(b"second"))
        assert first.status_code == second.status_code == 200
        paths = {first.json()["files"][0]["path"], second.json()["files"][0]["path"]}
        assert len(paths) == 2
        assert {Path(path).read_bytes() for path in paths} == {b"first", b"second"}
        _assert_no_quarantine(tmp_path)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
    async def test_quarantine_and_final_modes_are_owner_only(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        response = await client.post("/api/upload", files={"files": ("safe.md", b"safe")})
        path = Path(response.json()["files"][0]["path"])
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_body_limit_without_content_length_exact_and_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(body_limit, "UPLOAD_REQUEST_LIMIT", 4)

    async def consume(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = ASGITransport(app=body_limit.UploadBodyLimitMiddleware(consume))

    async def stream(data: bytes):
        yield data

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        exact = await client.post("/api/upload", content=stream(b"1234"))
        overflow = await client.post("/api/upload", content=stream(b"12345"))
    assert exact.status_code == 204
    assert overflow.status_code == 413
    assert overflow.json() == {"detail": "Upload request too large"}


async def test_upload_openapi_keeps_multipart_file_array_contract(app) -> None:
    schema = app.openapi()["paths"]["/api/upload"]["post"]["requestBody"]
    multipart = schema["content"]["multipart/form-data"]["schema"]
    assert multipart["required"] == ["files"]
    assert multipart["properties"]["files"] == {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
    }

"""Disk-backed multipart quarantine for the web upload boundary."""

from __future__ import annotations

import asyncio
import errno
import os
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

MAX_UPLOAD_FILES = 32
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_AGGREGATE_BYTES = 200 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


class UploadQuarantineError(Exception):
    """A safe, classified upload error suitable for HTTP translation."""

    def __init__(self, detail: str, *, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class QuarantinedUpload:
    filename: str
    path: Path
    size: int


class _ClosingMultiPartParser(MultiPartParser):
    """Close parser-owned spools for cancellation and unexpected failures too."""

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException:
            for spool in self._files_to_close_on_error:
                spool.close()
            raise


def prepare_upload_dir(upload_dir: Path) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(upload_dir, 0o700)


def _write_chunk(fd: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _close_form(form: FormData) -> None:
    """Synchronously close spools so cancellation cannot interrupt cleanup."""
    for _, value in form.multi_items():
        if isinstance(value, UploadFile):
            value.file.close()


async def _copy_to_quarantine(source: UploadFile, destination: Path) -> int:
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    size = 0
    try:
        while chunk := await source.read(UPLOAD_CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise UploadQuarantineError("Upload size limit exceeded", status_code=413)
            await asyncio.to_thread(_write_chunk, fd, chunk)
        await asyncio.to_thread(os.fsync, fd)
    finally:
        os.close(fd)
    return size


@asynccontextmanager
async def quarantine_uploads(
    request: Request,
    upload_dir: Path,
) -> AsyncIterator[list[QuarantinedUpload]]:
    """Parse multipart and yield a fully copied, automatically cleaned batch."""
    parser = _ClosingMultiPartParser(
        headers=request.headers,
        stream=request.stream(),
        max_files=MAX_UPLOAD_FILES,
        max_fields=0,
    )
    form: FormData | None = None
    quarantine_dir: Path | None = None
    try:
        try:
            form = await parser.parse()
        except MultiPartException as exc:
            detail = "Too many upload files" if "Too many files" in str(exc) else "Malformed upload"
            status = 413 if detail == "Too many upload files" else 400
            raise UploadQuarantineError(detail, status_code=status) from exc
        except (KeyError, ValueError) as exc:
            raise UploadQuarantineError("Malformed upload", status_code=400) from exc

        raw_entries = form.multi_items()
        if not raw_entries:
            raise UploadQuarantineError("No upload files provided", status_code=422)
        entries: list[UploadFile] = []
        for key, value in raw_entries:
            if key != "files" or not isinstance(value, UploadFile):
                raise UploadQuarantineError("Malformed upload", status_code=400)
            entries.append(value)

        prepare_upload_dir(upload_dir)
        quarantine_dir = Path(tempfile.mkdtemp(prefix=".quarantine-", dir=upload_dir))
        os.chmod(quarantine_dir, 0o700)

        aggregate = 0
        quarantined: list[QuarantinedUpload] = []
        for index, upload in enumerate(entries):
            destination = quarantine_dir / f"{index:04d}.part"
            size = await _copy_to_quarantine(upload, destination)
            aggregate += size
            if aggregate > MAX_UPLOAD_AGGREGATE_BYTES:
                raise UploadQuarantineError("Upload size limit exceeded", status_code=413)
            quarantined.append(
                QuarantinedUpload(
                    filename=Path(upload.filename or "upload").name,
                    path=destination,
                    size=size,
                )
            )

        _close_form(form)
        form = None
        yield quarantined
    finally:
        if form is not None:
            _close_form(form)
        if quarantine_dir is not None:
            shutil.rmtree(quarantine_dir, ignore_errors=True)


def promote_no_overwrite(source: Path, upload_dir: Path, filename: str) -> Path:
    """Atomically promote by hard-linking; never replace an existing path."""
    prepare_upload_dir(upload_dir)
    original = Path(filename or "upload").name
    stem, suffix = Path(original).stem, Path(original).suffix
    candidates = [original]
    candidates.extend(f"{stem}_{secrets.token_hex(6)}{suffix}" for _ in range(16))
    for candidate in candidates:
        destination = upload_dir / candidate
        try:
            os.link(source, destination)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno in {errno.EXDEV, errno.EPERM, errno.ENOTSUP}:
                raise RuntimeError("Atomic upload promotion is unavailable") from exc
            raise
        try:
            os.chmod(destination, 0o600)
            source.unlink()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
    raise RuntimeError("Unable to allocate a unique upload filename")

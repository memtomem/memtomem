"""Helpers for reading/writing markdown memory files."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


logger = logging.getLogger(__name__)

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class RestoreOutcome(StrEnum):
    """What :func:`restore_pre_image_quietly` did with the pre-image (#2347).

    Also the vocabulary the forward path refuses in: a
    :class:`SourceChangedError` carries the outcome a restore would have
    reported, so the two directions describe one condition one way (#2367).
    """

    restored = "restored"
    """The source was still the file that was read, and holds its pre-image again."""

    source_removed = "source_removed"
    """The source (or a parent component) was gone — nothing was recreated."""

    source_replaced = "source_replaced"
    """A different file now answers to the path — it was left exactly as found."""

    failed = "failed"
    """The restore itself failed; logged, never raised. The file may hold the mutation."""


@dataclass(frozen=True, slots=True)
class PreImage:
    """A source file's bytes plus the filesystem identity they were read from.

    ``identity`` is ``(st_dev, st_ino)``, or ``None`` when the filesystem cannot
    answer (``st_ino == 0`` on some FUSE/SMB mounts). Both come off one open
    descriptor, so they describe the same file even if the path is re-pointed
    the instant after (the ``atomic_write_bytes`` rule, ``context/_atomic.py``).
    """

    data: bytes
    identity: tuple[int, int] | None


class SourceChangedError(OSError):
    """A line-range rewrite was refused: the path no longer holds the file the
    caller read (#2367).

    Raised *before* any byte is written, so the caller's own mutation is not on
    disk and there is nothing to roll back. An ``OSError`` subclass so a caller
    that only knows the broad class degrades to "the file could not be written"
    rather than to an internal error; every caller in this repository has a
    specific arm ahead of its broad one.

    Constructed with a single message argument on purpose: a two-argument
    ``OSError`` is remapped by the interpreter to whichever builtin subclass
    matches the errno, which would silently undo this hierarchy.
    """

    outcome: RestoreOutcome
    """What a restore would have reported for the same condition."""


class SourceRemovedError(SourceChangedError):
    """The source (or a parent component) was gone when the write was attempted."""

    outcome = RestoreOutcome.source_removed


class SourceReplacedError(SourceChangedError):
    """A different file — or a directory — now answers to the source's path."""

    outcome = RestoreOutcome.source_replaced


def _default_heading(now: str) -> str:
    """The auto-heading for an untitled entry — unique per entry.

    Two chunks with the same ``heading_hierarchy`` are mergeable
    (``indexing.engine._can_merge``), which is what keeps a short entry from
    being stranded from its own section. Distinct headings are the protection
    that keeps separate entries separate — the ``## Cache Decision`` vs
    ``## Database Decision`` case that rule is written against.

    A timestamp alone does not provide it. At second resolution any two
    untitled entries appended inside the same second shared a heading and were
    packed into ONE chunk: the earlier entry's text was swallowed and the
    file's chunk ids were re-minted, so an id already handed to a caller (and
    any provenance link recorded against it) pointed at nothing. Finer
    resolution only narrows that window, and does not close it at all for
    ``mem_batch_add``, which composes up to 500 blocks in a single loop.

    So the heading carries a millisecond stamp for human legibility plus a
    random suffix for uniqueness. The suffix is the FULL uuid4, not a truncated
    prefix: a 500-entry batch shares one millisecond stamp, and against 32 bits
    that is a ~3e-5 chance per batch of two entries colliding — which restores
    the exact merge and id invalidation this is meant to remove. Only the
    heading changes; the ``> created:`` line keeps its second resolution, which
    is what the parsers and the stored metadata read.
    """
    return f"## Entry {now} {uuid4().hex}"


def format_entry_block(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Return the markdown block ``append_entry`` writes for one entry.

    The block always begins with a leading ``\\n``, so joining several
    blocks with ``"".join(...)`` reproduces exactly what sequential
    ``append_entry`` calls would write — this is what lets
    ``mem_batch_add`` do one atomic append instead of a per-entry loop.

    The per-entry metadata (``> created: ...`` and optional
    ``> tags: [...]``) is emitted as a single explicit blockquote group
    — every line carries a leading ``> `` so we never rely on CommonMark
    lazy continuation, and the tag list is JSON (double-quoted) so it
    parses as YAML downstream. The chunker promotes ``> tags:`` into
    ``ChunkMetadata.tags`` and strips the header from chunk content (see
    ``memtomem.chunking.markdown.MarkdownChunker``).

    ``ensure_ascii=False`` keeps non-ASCII tags (e.g. Hangul) as their
    real characters in the file rather than ``\\uXXXX`` escape text. The
    parser does not JSON-decode the array element-by-element, so escaped
    text would otherwise survive verbatim into the stored tag.

    An untitled entry gets a heading that is unique per entry rather than a
    bare timestamp — see ``_default_heading`` for why two entries must never
    share one.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tag_line = f"\n> tags: {json.dumps(list(tags), ensure_ascii=False)}" if tags else ""

    # Skip heading if content already starts with one (e.g., from a template)
    stripped = content.strip()
    if stripped.startswith("## "):
        return f"\n> created: {now}{tag_line}\n\n{stripped}\n"
    heading = (
        f"## {title}"
        if title
        else _default_heading(datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    )
    return f"\n{heading}\n\n> created: {now}{tag_line}\n\n{stripped}\n"


def append_blocks(file_path: Path, blocks: Sequence[str]) -> None:
    """Append pre-formatted entry blocks to a markdown file in ONE write.

    All-or-nothing (issue #1573): a batch of entries is composed into a
    single ``write`` so a mid-batch failure can't leave a partial run of
    entries on disk. A no-op when ``blocks`` is empty (no file touched).
    """
    if not blocks:
        return
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a", encoding="utf-8") as f:
        f.write("".join(blocks))


def append_entry(
    file_path: Path,
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Append a single new entry to a markdown file, creating it if needed."""
    append_blocks(file_path, [format_entry_block(content, title=title, tags=tags)])


def _find_body_start_index(chunk_lines: list[str]) -> int:
    """Return the index in *chunk_lines* where the entry body starts.

    Entry shape (as written by ``append_entry``):

    - Optional heading line (``## ...``)
    - Zero or more blank lines
    - Optional section-leading blockquote group (``>``-prefixed lines,
      with lazy-continuation lines accepted for legacy compatibility)
    - Zero or more blank lines
    - Body

    The index returned points to the first body line. A chunk that
    starts mid-section (an oversized section's non-first sub-chunk) has
    no heading or blockquote at its first line; the function then
    returns 0.
    """
    i = 0
    if i < len(chunk_lines) and chunk_lines[i].lstrip().startswith("#"):
        i += 1
    while i < len(chunk_lines) and not chunk_lines[i].strip():
        i += 1
    if i < len(chunk_lines) and chunk_lines[i].lstrip().startswith(">"):
        block_started = False
        while i < len(chunk_lines):
            stripped = chunk_lines[i].lstrip()
            if not stripped:
                break
            if stripped.startswith(">"):
                block_started = True
                i += 1
                continue
            if block_started:
                # Lazy continuation
                i += 1
                continue
            break
        while i < len(chunk_lines) and not chunk_lines[i].strip():
            i += 1
    return i


def _rewrite_in_place(
    file_path: Path,
    expected_identity: tuple[int, int] | None,
    edit: Callable[[str], str],
) -> None:
    """Read *file_path*, hand its text to *edit*, and write the result back — but
    only while the path still holds the file the caller was editing (#2367).

    **The open is the decision, by errno rather than a probe** — the rule #2346
    arrived at and :func:`restore_pre_image_quietly` already follows. ``"r+"``
    passes neither ``O_CREAT`` nor ``O_TRUNC``, so absence answers ``ENOENT`` /
    ``ENOTDIR`` at the open instead of being raced between a check and a write.
    A plain ``write_text`` **creates**: the CRUD span's locks bind cooperating
    memtomem writers but never an external ``rm`` / ``mv`` / editor saving via
    rename (the limit stated at ``indexing/engine.py``), so the one removal this
    path can meet was the one case where writing was wrong — it put the note the
    user deleted back on disk, with the edit applied, and reported success.

    Identity ``(st_dev, st_ino)`` is then compared on the **descriptor**, against
    the identity the caller's span already read. Existence alone is not the
    question a line-range rewrite asks: a file swapped under the path is a
    different file, and splicing it at line numbers taken from its predecessor
    corrupts it as surely as resurrection does. ``expected_identity=None`` (the
    ``st_ino == 0`` FUSE/SMB case, and the default for callers that hold no
    pre-image) falls back to existence, matching the restore's own rule.

    Both checks happen at the open, and a removal *after* it is not detected at
    all: the descriptor outlives the directory entry, so the write lands on an
    orphaned inode and the file stays deleted. That is the correct outcome and
    the reason no check is needed there — what this guards is creation, and a
    write through a descriptor cannot create. It does mean a caller is told
    about a removal only when the removal beat the open; the ones it does not
    hear about are the ones where nothing came back.

    What the identity closes is the window between the caller's read and this
    write, and only that. A file already swapped *before* the caller looked is,
    as far as anything here can tell, the file this call was asked to edit; that
    its line numbers describe a different file is a question about the index's
    provenance, which this check does not answer and does not claim to.

    Ordering inside the ``with`` carries two of the three defects this can have:
    the identity check runs **before** ``read()``, so a non-UTF-8 replacement is
    refused rather than raising ``UnicodeDecodeError`` over the refusal; and
    ``seek(0)`` runs before ``truncate()``, since truncating at the EOF position
    a full read leaves would keep the old bytes and append. Nothing is truncated
    until *edit* has returned, so a ``ValueError`` from
    :func:`_validate_line_range` still leaves the file exactly as found.

    Text mode, unlike the byte-level restore: these helpers have always read
    with universal newlines and written with the platform's, and this is a
    refusal fix, not a newline change.
    """
    try:
        handle = open(file_path, "r+", encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise SourceRemovedError(
            f"{file_path} was removed before the edit was written; nothing was recreated"
        ) from exc
    except IsADirectoryError as exc:
        raise SourceReplacedError(
            f"{file_path} is a directory, not the file that was read; nothing was written"
        ) from exc
    except PermissionError as exc:
        # Windows answers a directory at the path with EACCES where POSIX
        # answers EISDIR, and a directory standing where the source was is a
        # replacement, not a permission failure. Asking what is actually there
        # is safe in a way a pre-open probe would not be: the open has already
        # failed and neither branch writes, so a stale answer costs a message
        # rather than a file. A genuine permission error still propagates.
        if os.path.isdir(file_path):
            raise SourceReplacedError(
                f"{file_path} is a directory, not the file that was read; nothing was written"
            ) from exc
        raise

    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (
            expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity
        ):
            raise SourceReplacedError(
                f"{file_path} is no longer the file that was read; nothing was written"
            )
        result = edit(handle.read())
        handle.seek(0)
        handle.truncate()
        handle.write(result)


def replace_chunk_body(
    file_path: Path,
    start_line: int,
    end_line: int,
    new_content: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Replace a chunk's body in *file_path* while preserving its header.

    "Header" means the heading line and any section-leading blockquote
    group (``> created: ...`` / ``> tags: ...``). The chunker strips the
    blockquote header from chunk content, so callers of ``mem_edit``
    typically supply body-only ``new_content`` and would otherwise
    accidentally erase the metadata header.

    If ``new_content`` itself starts with ``## ``, the call is treated
    as a full replacement (preserving the pre-RFC ``mem_edit`` semantic
    where the user supplied the entire entry including heading); no
    header preservation is applied.

    Never creates: a source removed under this call raises
    :class:`SourceRemovedError` and one swapped for another file (or an
    unrelated file at *expected_identity*'s path) raises
    :class:`SourceReplacedError`, in both cases having written nothing. See
    :func:`_rewrite_in_place`.
    """

    def edit(text: str) -> str:
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        _validate_line_range(start_line, end_line, len(lines))

        stripped_new = new_content.lstrip("\n")
        # ``append_entry`` always emits H2 for entry headings; other heading
        # levels in user input are treated as body content rather than a
        # header override, so only ``## `` triggers full-replacement.
        if stripped_new.lstrip().startswith("## "):
            # Full replacement — caller is overriding heading + header explicitly.
            replacement = new_content.splitlines()
        else:
            chunk_lines = lines[start_line - 1 : end_line]
            body_start = _find_body_start_index(chunk_lines)
            preserved = chunk_lines[:body_start]
            new_body_lines = stripped_new.splitlines()
            replacement = preserved + new_body_lines

        new_lines = lines[: start_line - 1] + replacement + lines[end_line:]
        result = "\n".join(new_lines)
        if trailing_newline:
            result += "\n"
        return result

    _rewrite_in_place(file_path, expected_identity, edit)


def _validate_line_range(start_line: int, end_line: int, total_lines: int) -> None:
    """Validate 1-based inclusive line range."""
    if start_line < 1:
        raise ValueError(f"start_line must be >= 1, got {start_line}")
    if start_line > end_line:
        raise ValueError(f"start_line ({start_line}) must be <= end_line ({end_line})")
    if end_line > total_lines:
        raise ValueError(f"end_line ({end_line}) exceeds file length ({total_lines} lines)")


def replace_lines(
    file_path: Path,
    start_line: int,
    end_line: int,
    new_content: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Replace lines [start_line, end_line] (1-based, inclusive) with new_content.

    Never creates; see :func:`_rewrite_in_place` for what a removed or replaced
    source raises.
    """

    def edit(text: str) -> str:
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        _validate_line_range(start_line, end_line, len(lines))
        before = lines[: start_line - 1]
        after = lines[end_line:]
        new_lines = before + new_content.splitlines() + after
        result = "\n".join(new_lines)
        if trailing_newline:
            result += "\n"
        return result

    _rewrite_in_place(file_path, expected_identity, edit)


def remove_lines(
    file_path: Path,
    start_line: int,
    end_line: int,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Remove lines [start_line, end_line] (1-based, inclusive) from file.

    Never creates; see :func:`_rewrite_in_place` for what a removed or replaced
    source raises. A delete whose source somebody else already removed is not a
    failure of intent, but it is this helper's business only to say so — the
    surfaces decide what to do about it.
    """

    def edit(text: str) -> str:
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        _validate_line_range(start_line, end_line, len(lines))
        new_lines = lines[: start_line - 1] + lines[end_line:]
        result = "\n".join(new_lines)
        if trailing_newline and new_lines:
            result += "\n"
        return result

    _rewrite_in_place(file_path, expected_identity, edit)


def read_pre_image(file_path: Path) -> PreImage:
    """Read *file_path*'s bytes and identity for a later rollback.

    Bytes rather than text on purpose: a rollback restores what was there. Text
    mode would translate newlines on the way back out and would raise
    ``UnicodeDecodeError`` on a file the mutation itself is about to reject —
    outside the caller's ``try``, where it could not be rolled back.
    """
    with open(file_path, "rb") as f:
        info = os.fstat(f.fileno())
        data = f.read()
    identity = None if info.st_ino == 0 else (info.st_dev, info.st_ino)
    return PreImage(data=data, identity=identity)


def restore_pre_image_quietly(file_path: Path, pre_image: PreImage) -> RestoreOutcome:
    """Put *pre_image* back at *file_path*, reporting rather than raising (#2347).

    Only ever called while another exception is propagating — the failure this
    is rolling back. So it never raises: a restore error replacing the body's
    exception is exactly the masking #2229 closed on the lock's own release path
    (``context/_atomic._release_quietly``), and it would demote the real cause to
    ``__context__`` and skip the caller's ``RetryableError`` branch. The outcome
    is returned instead, for the caller to report in its own terms.

    It also never *creates*. The L2 sidecar the caller holds excludes cooperating
    writers only; it never bound external mutation of the data file
    (``indexing/engine.py``), so an outside ``rm`` / ``mv`` / save-via-rename can
    land between the pre-image read and the failure. Writing the pre-image back
    unconditionally would recreate a file the user deleted, restoring its content
    by fiat.

    **The open is the decision, by errno rather than a probe** (the #2346 rule):
    ``"r+b"`` passes neither ``O_CREAT`` nor ``O_TRUNC``, so absence answers
    ``ENOENT`` / ``ENOTDIR`` instead of being raced between a check and a write.
    Identity is then compared on the *descriptor*, and the truncate comes after
    that check — opening with ``"wb"`` would empty a replacement file before
    refusing it.

    Where the filesystem cannot answer identity (``PreImage.identity is None``)
    the restore proceeds on existence alone. Refusing there would leave the
    caller's own half-applied mutation on disk — a certain corruption traded for
    a hypothetical one — and resurrection, the defect this closes, is already
    ruled out by the open.

    Symlinks are deliberately followed on both reads (no ``O_NOFOLLOW``): a
    symlinked memory file works today, and the identity compared is the target's
    at both ends.
    """
    try:
        handle = open(file_path, "r+b")
    except (FileNotFoundError, NotADirectoryError):
        # The file, or a parent component, is gone. Pre-#2347 this branch was
        # the masking one: ``write_text`` raised ENOENT over the body's error.
        return RestoreOutcome.source_removed
    except IsADirectoryError:
        return RestoreOutcome.source_replaced
    except PermissionError:
        # Windows answers a directory at the path with EACCES where POSIX
        # answers EISDIR, and a directory standing where the source was is a
        # replacement, not a failure to report. Asking what is actually there
        # is safe in a way a pre-open probe would not be: both branches write
        # nothing, so a stale answer costs a message rather than a file. A
        # genuine permission error on a regular file still lands in ``failed``.
        if os.path.isdir(file_path):
            return RestoreOutcome.source_replaced
        logger.warning("restoring %s failed while unwinding an error", file_path, exc_info=True)
        return RestoreOutcome.failed
    except Exception:  # noqa: BLE001 - reported, never raised over the body's
        logger.warning("restoring %s failed while unwinding an error", file_path, exc_info=True)
        return RestoreOutcome.failed

    try:
        with handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or (
                pre_image.identity is not None and (info.st_dev, info.st_ino) != pre_image.identity
            ):
                return RestoreOutcome.source_replaced
            handle.truncate(0)
            handle.write(pre_image.data)
    except Exception:  # noqa: BLE001 - reported, never raised over the body's
        logger.warning("restoring %s failed while unwinding an error", file_path, exc_info=True)
        return RestoreOutcome.failed
    return RestoreOutcome.restored

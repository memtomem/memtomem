"""Helpers for reading/writing markdown memory files."""

from __future__ import annotations

import errno
import hashlib
import io
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
from typing import IO
from uuid import uuid4


from memtomem.source_provenance import StaleSourceProvenanceError, source_span_hash
from memtomem.storage.sqlite_helpers import fold_path_form, resolve_path


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


@dataclass(frozen=True, slots=True)
class JudgedSource:
    """What a memory tool read before an append, and the preflight judged (#3289).

    :func:`append_blocks` takes it as a required keyword and writes only to the
    file it describes, so an append cannot follow a path that was replaced by a
    symlink after the preflight. Made by :func:`read_for_append`, in one worker
    call.

    ``data`` is the bytes read, ``b""`` for a file that did not exist.
    ``identity`` is the read descriptor's ``(st_dev, st_ino)``, ``None`` when
    the filesystem cannot answer or there was no file. ``existed`` is whether
    the read found a file. ``source_path`` is the path's resolution right after
    the read: the preflight judges the bytes under it, and a new file is
    created exactly there.
    """

    data: bytes
    identity: tuple[int, int] | None
    existed: bool
    source_path: Path


@dataclass(frozen=True, slots=True)
class WrittenSource:
    """The file a memory tool wrote, for the re-index that follows its write (#3320).

    ``IndexEngine.index_file`` takes it as ``written_source`` and reads only
    that file (:func:`open_written`), so the re-index cannot follow a path
    that was replaced after the write into a file the tool never wrote, and
    index that one under the tool's namespace and tags.

    ``source_path`` is the resolution taken with the read the preflight judged
    (``JudgedSource.source_path``, or :func:`read_with_source_key`'s). It is
    never resolved again: the re-index opens it as given, judges the content
    under it and keys its rows by it. ``identity`` is the ``(st_dev, st_ino)``
    of the descriptor the write went through, ``None`` where the filesystem
    cannot answer.
    """

    source_path: Path
    identity: tuple[int, int] | None

    @property
    def key(self) -> str:
        """The key the store holds this source's rows under."""
        return fold_path_form(str(self.source_path))


@dataclass(frozen=True, slots=True)
class Appended:
    """What :func:`append_blocks` wrote, and the file it wrote it to.

    ``text`` is the text appended (``""`` for none), before the platform
    newline translation text mode applies, so a caller can name exactly what
    the file should hold after the write. ``source`` is that file, for the
    re-index: its identity comes off the descriptor the text was written
    through, which for a file the append created is the only place to get it.
    """

    text: str
    source: WrittenSource


@dataclass(frozen=True, slots=True)
class _Refusal:
    """How a refused open is worded: what was about to happen, and what did not."""

    before: str
    file: str
    removed: str
    replaced: str


_ENTRY = _Refusal(
    "the entry was written",
    "the file that was read",
    "nothing was recreated",
    "nothing was written",
)
_EDIT = _Refusal(
    "the edit was written", "the file that was read", "nothing was recreated", "nothing was written"
)
_REINDEX = _Refusal(
    "it was re-indexed", "the file that was written", "it was not indexed", "it was not indexed"
)


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


def append_blocks(file_path: Path, blocks: Sequence[str], *, judged: JudgedSource) -> Appended:
    """Append pre-formatted entry blocks to the file a preflight judged, in ONE write.

    All-or-nothing (issue #1573): a batch of entries is composed into a
    single ``write`` so a mid-batch failure can't leave a partial run of
    entries on disk. A no-op when ``blocks`` is empty (no file touched).

    ``judged`` is what the caller read before its preflight
    (:func:`read_for_append`). **The open is the decision** (#3289): the caller
    holds a sidecar that binds cooperating writers only, so another process
    can replace the path between the preflight and this write, and a plain
    ``open(path, "a")`` would follow a symlink put there into a file nobody
    judged — in another scope, past ``confirm_project_shared`` and the refusal
    of ``force_unsafe`` there. A file that existed is opened without
    ``O_CREAT`` and compared by identity on the descriptor
    (:func:`_open_judged_existing`); a file that did not is created at the
    judged path by descriptor (:func:`_create_judged`). Either raises
    :class:`SourceChangedError` before writing anything.

    Returns the text appended and the file it went to (:class:`Appended`).
    The identity is taken from the open descriptor before anything is written
    through it, so a file this call created is named by what was created, not
    by a later look at the path.
    """
    if not blocks:
        return Appended("", WrittenSource(judged.source_path, judged.identity))
    text = "".join(blocks)
    if judged.existed:
        handle = _open_judged_existing(file_path, judged)
    else:
        handle = _create_judged(judged.source_path)
    with handle:
        info = os.fstat(handle.fileno())
        handle.write(text)
    identity = None if info.st_ino == 0 else (info.st_dev, info.st_ino)
    return Appended(text, WrittenSource(judged.source_path, identity))


def append_entry(
    file_path: Path,
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    *,
    judged: JudgedSource,
) -> Appended:
    """Append a single new entry to the file a preflight judged, creating it if it was absent.

    Returns what was appended and where (see :func:`append_blocks`).
    """
    return append_blocks(
        file_path, [format_entry_block(content, title=title, tags=tags)], judged=judged
    )


# ``O_NONBLOCK`` on an open for writing: a FIFO put in a file's place answers
# ``ENXIO`` at once when nothing reads it, where a blocking open would wait for
# a reader while the caller holds its sidecar and its idempotency claim. It
# does nothing to a regular file. POSIX only.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
# A directory opened to walk through needs search permission only: ``O_RDONLY``
# would also need read permission on every ancestor, which creating a file by
# path never did (an ancestor mode ``0o111`` refuses it). Linux spells it
# ``O_PATH``, macOS ``O_SEARCH``.
_O_WALK = getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0) or os.O_RDONLY
# Creating a file under a directory descriptor is what keeps a new file in the
# judged directory. Windows has no ``dir_fd``; there the file is created by path
# and checked afterwards (:func:`_create_judged_by_path`).
_HAS_DIR_FD = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)


def _open_existing(
    file_path: Path,
    mode: str,
    *,
    refusal: _Refusal,
    opener: Callable[[str, int], int] | None = None,
) -> IO:
    """``open(file_path, mode)`` for a file that must already be there, refusals typed.

    Shared by :func:`_rewrite_in_place` and :func:`_open_same_file`. The
    mode passes no ``O_CREAT`` (``"r+"``, or ``"a"`` / ``"rb"`` through an
    ``opener`` that drops it), so absence answers at the open rather than
    being raced by a probe: ``ENOENT`` / ``ENOTDIR`` raise
    :class:`SourceRemovedError`. A directory standing at the path, a FIFO with
    no reader opened for writing (``ENXIO``, under ``O_NONBLOCK``), and a
    symlink loop put at the path raise :class:`SourceReplacedError`. POSIX
    answers the loop ``ELOOP``; Windows answers ``EINVAL``, which it also
    gives for other things, so there the path must be a symlink as well.

    A text mode decodes UTF-8; a binary mode (``"b"``) gives the bytes.
    ``refusal`` words the messages for the caller's operation.
    """
    try:
        return open(file_path, mode, encoding=None if "b" in mode else "utf-8", opener=opener)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise SourceRemovedError(
            f"{file_path} was removed before {refusal.before}; {refusal.removed}"
        ) from exc
    except IsADirectoryError as exc:
        raise SourceReplacedError(
            f"{file_path} is a directory, not {refusal.file}; {refusal.replaced}"
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
                f"{file_path} is a directory, not {refusal.file}; {refusal.replaced}"
            ) from exc
        raise
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ELOOP) or (
            exc.errno == errno.EINVAL and os.path.islink(file_path)
        ):
            raise SourceReplacedError(
                f"{file_path} is no longer a regular file; {refusal.replaced}"
            ) from exc
        raise


def _open_same_file(
    file_path: Path,
    identity: tuple[int, int] | None,
    source_path: Path,
    *,
    mode: str,
    opener: Callable[[str, int], int],
    refusal: _Refusal,
) -> IO:
    """Open ``file_path`` only if it is still the file with ``identity``, or raise.

    The open is the decision, for the append after a preflight
    (:func:`_open_judged_existing`) and for the re-index after a write
    (:func:`open_written`). The ``opener`` passes no ``O_CREAT``, so a file
    removed since is not recreated. The descriptor must be a regular file with
    ``identity``: a symlink put at the path since leads to another inode and
    is refused, while a path that led through a link all along opens the same
    inode and keeps working. Same inode through another link passes, as it
    does for :func:`_rewrite_in_place`.

    Where the filesystem gives no identity (``st_ino == 0``), the path is
    required to resolve to ``source_path`` before the open and after it: the
    first sees a link put in place before the open and taken away right after
    it, the second one put in place before the open and left there. That
    narrows the window without closing it: a link put in place after the
    first resolution and taken away before the second is not seen.

    Raises :class:`SourceRemovedError` or :class:`SourceReplacedError`.
    """
    if identity is None:
        _require_resolves_to(file_path, source_path, refusal)
    handle = _open_existing(file_path, mode, refusal=refusal, opener=opener)
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (
            identity is not None and (info.st_dev, info.st_ino) != identity
        ):
            raise SourceReplacedError(
                f"{file_path} is no longer {refusal.file}; {refusal.replaced}"
            )
        if identity is None:
            _require_resolves_to(file_path, source_path, refusal)
    except BaseException:
        handle.close()
        raise
    return handle


def _open_judged_existing(file_path: Path, judged: JudgedSource) -> IO[str]:
    """Open the file ``judged`` read for appending, or raise :class:`SourceChangedError`.

    See :func:`_open_same_file` for what is compared.
    """

    def no_create(path: str, _flags: int) -> int:
        # The flags ``"a"`` would pass, less the create: spelled out rather than
        # masked, so nothing here names a creating flag at all.
        return os.open(path, os.O_WRONLY | os.O_APPEND | _O_NONBLOCK | _O_BINARY)

    return _open_same_file(
        file_path, judged.identity, judged.source_path, mode="a", opener=no_create, refusal=_ENTRY
    )


def open_written(source: WrittenSource) -> IO[bytes]:
    """Open the file a memory tool wrote, for its re-index to read (#3320).

    ``source.source_path`` is opened as given, read-only, and the descriptor
    must be the regular file the write went through (:func:`_open_same_file`).
    Raises :class:`SourceReplacedError` when something else answers to the
    path now — a link, another file, a directory, a FIFO (opened
    ``O_NONBLOCK`` so one with no writer does not block) — and
    :class:`SourceRemovedError` when nothing does. The caller reads the bytes
    off the handle and closes it.
    """

    def read_only(path: str, _flags: int) -> int:
        return os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_BINARY)

    return _open_same_file(
        source.source_path,
        source.identity,
        source.source_path,
        mode="rb",
        opener=read_only,
        refusal=_REINDEX,
    )


def _require_resolves_to(file_path: Path, source_path: Path, refusal: _Refusal) -> None:
    try:
        resolved = resolve_path(file_path)
    except OSError:
        resolved = None
    if resolved != source_path:
        raise SourceReplacedError(
            f"{file_path} no longer resolves to {refusal.file}; {refusal.replaced}"
        )


def _occupied(name: str | Path, *, dir_fd: int | None = None) -> bool:
    """Whether a failed create lost the name to something already there.

    Asked only after the create failed, and of the name itself, never the
    errno: Windows answers a directory holding the name with ``EACCES``, the
    same errno as a genuine permission failure (``context.migrate``
    ``_claim_hit_an_occupied_name``). A dangling symlink occupies the name too.
    """
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return True


def _create_judged(source_path: Path) -> IO[str]:
    """Create ``source_path`` for appending, in the directory that was judged.

    A new file has no identity to compare, so the path is the judgement: the
    preflight judged the bytes (none) under ``source_path``, the read's
    resolution, which names no symlink. The directories are walked from the
    root one component at a time by descriptor, each opened ``O_NOFOLLOW``, and
    any that is missing is made under its parent's descriptor; the file is then
    created ``O_EXCL`` under the last one. The kernel resolves each name and
    refuses a link in the same operation, so an ancestor replaced by a symlink
    since the read — and put back, or not — cannot redirect the file into
    another directory: the walk is refused instead. Nothing is written, and
    nothing needs removing.

    A name already taken (by a file another process created since the read, a
    directory, a link) is refused, not followed: retrying reads it as an
    existing file. A ``source_path`` that is not absolute (its resolution
    failed) is refused. Where there is no ``dir_fd`` (Windows), see
    :func:`_create_judged_by_path`.
    """
    if not _HAS_DIR_FD:
        return _create_judged_by_path(source_path)
    if not source_path.is_absolute():
        raise SourceReplacedError(
            f"{source_path} could not be resolved when it was read; nothing was written"
        )
    dir_fd = os.open(source_path.anchor, _O_WALK | _O_DIRECTORY)
    try:
        for name in source_path.parent.parts[1:]:
            child = _enter_directory(dir_fd, name, source_path)
            os.close(dir_fd)
            dir_fd = child
        # ``O_NOFOLLOW`` is redundant beside ``O_CREAT | O_EXCL``, which already
        # refuses a symlink in the final component on POSIX; it is kept anyway.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | _O_NOFOLLOW
        try:
            fd = os.open(source_path.name, flags, 0o666, dir_fd=dir_fd)
        except OSError as exc:
            if _occupied(source_path.name, dir_fd=dir_fd):
                raise SourceReplacedError(
                    f"{source_path} was created by another process after it was read; "
                    "nothing was written"
                ) from exc
            raise
    finally:
        os.close(dir_fd)
    return _text_appender(fd)


def _enter_directory(parent_fd: int, name: str, source_path: Path) -> int:
    """Open directory ``name`` under ``parent_fd``, making it if missing.

    ``parent_fd`` stays the caller's to close, whatever happens here.

    ``O_NOFOLLOW`` refuses a symlink at the open (Linux ``ELOOP``, macOS
    ``ENOTDIR``), and the descriptor is checked to be a directory as well,
    since ``O_PATH`` opens a symlink itself rather than refusing it on some
    kernels. Anything else at the name is a replacement.
    """
    flags = _O_WALK | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK
    try:
        try:
            child = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                os.mkdir(name, 0o777, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if _occupied(name, dir_fd=parent_fd):
            raise SourceReplacedError(
                f"{source_path}: a directory on its path was replaced after it was read; "
                "nothing was written"
            ) from exc
        raise
    try:
        if not stat.S_ISDIR(os.fstat(child).st_mode):
            raise SourceReplacedError(
                f"{source_path}: a directory on its path was replaced after it was read; "
                "nothing was written"
            )
    except BaseException:
        os.close(child)
        raise
    return child


def _create_judged_by_path(source_path: Path) -> IO[str]:
    """:func:`_create_judged` without ``dir_fd`` (Windows): narrower, by path.

    The file is created ``O_EXCL`` at ``source_path`` and kept only if what
    was claimed is a regular file with the descriptor's identity. Windows'
    ``O_EXCL`` follows a reparse point in the final component, so a dangling
    symlink put at the name lets the create succeed at the link's target
    (``context.migrate._create_exclusive_file``): that is refused and nothing
    is written, and the empty file it made is left where it is, because no
    pathname delete can be sure it removes that file and not a replacement.
    An ancestor directory replaced since the read (a junction, say) is not
    seen here, whether it stays replaced or is put back: the file lands where
    the path leads at the create. A file that changes between the create and
    the look at it is refused too, and left where it is.
    """
    source_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | _O_BINARY
    try:
        fd = os.open(source_path, flags, 0o666)
    except OSError as exc:
        if _occupied(source_path):
            raise SourceReplacedError(
                f"{source_path} was created by another process after it was read; "
                "nothing was written"
            ) from exc
        raise
    try:
        try:
            claimed = os.lstat(source_path)
        except OSError as exc:
            # Gone, or unreachable, between the create and this look: what was
            # claimed cannot be confirmed, so nothing is written.
            raise SourceReplacedError(
                f"{source_path} changed while it was created; nothing was written"
            ) from exc
        info = os.fstat(fd)
        same = (
            claimed.st_ino == 0
            or info.st_ino == 0
            or ((claimed.st_dev, claimed.st_ino) == (info.st_dev, info.st_ino))
        )
        if not stat.S_ISREG(claimed.st_mode) or not same:
            raise SourceReplacedError(
                f"{source_path} is held by a link the create followed; nothing was written"
            )
    except BaseException:
        os.close(fd)
        raise
    return _text_appender(fd)


def _text_appender(fd: int) -> IO[str]:
    """``fd`` as ``open(path, "a", encoding="utf-8")`` would give it; owns ``fd`` either way.

    Not ``open(fd, ...)``: when that fails after making its raw file it closes
    ``fd`` itself, so closing it again on the way out could close an unrelated
    descriptor that has reused the number. ``io.FileIO`` leaves ``fd`` open
    when it fails, so each layer has one owner: the caller's ``fd`` until the
    raw file exists, the raw file after. ``"a"`` on a descriptor seeks to its
    end and creates nothing.
    """
    try:
        raw = io.FileIO(fd, "a", closefd=True)
    except BaseException:
        os.close(fd)
        raise
    try:
        return io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")
    except BaseException:
        raw.close()
        raise


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


@dataclass(frozen=True)
class Rewrite:
    """What an in-place rewrite read and what it wrote, both as read_text sees text.

    ``base`` is the text the rewrite read through its own descriptor; it can
    differ from what the caller read earlier if somebody wrote in between.
    """

    base: str
    result: str


def _rewrite_in_place(
    file_path: Path,
    expected_identity: tuple[int, int] | None,
    edit: Callable[[str], str],
) -> Rewrite:
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

    Identity closes only the window between the caller's read and this write.
    Each edit callback also checks the indexed original span's hash against
    this descriptor's text (#2371), so a replacement before the caller's read,
    or an in-place change retaining the inode, is refused too. Neither check
    excludes an external writer changing this inode after our descriptor read;
    the sidecar only serializes cooperating writers.

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
    handle = _open_existing(file_path, "r+", refusal=_EDIT)

    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (
            expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity
        ):
            raise SourceReplacedError(
                f"{file_path} is no longer the file that was read; nothing was written"
            )
        try:
            text = handle.read()
        except UnicodeDecodeError as exc:
            raise StaleSourceProvenanceError("Source is no longer readable UTF-8") from exc
        result = edit(text)
        handle.seek(0)
        handle.truncate()
        handle.write(result)
    return Rewrite(base=text, result=result)


@dataclass(frozen=True, slots=True)
class SourceBase:
    """The caller edited the span's source text, as :func:`chunk_source_text` gave it.

    ``body_hash`` is that text's :func:`source_text_hash`. The edit replaces
    exactly that text; nothing in the span is detected, preserved or dropped
    on the caller's behalf, because the caller saw every line it replaces.
    """

    body_hash: str


@dataclass(frozen=True, slots=True)
class IndexedBase:
    """The caller edited the chunk's indexed ``content``, not its source text.

    Indexed content is a projection of the span (wikilinks resolved, merge
    labels rebuilt, frontmatter carried over from a merged preamble), so it is
    a safe base only where it *is* the span's body: the edit is accepted only
    when saving ``content`` itself unchanged would leave the span as it is.
    """

    content: str


EditBase = SourceBase | IndexedBase

_BASE_HASH_RE = re.compile(r"[0-9a-f]{64}")


def valid_base_hash(value: object) -> bool:
    """Whether *value* has the shape :func:`source_text_hash` produces."""
    return isinstance(value, str) and _BASE_HASH_RE.fullmatch(value) is not None


class EditBaseMismatchError(StaleSourceProvenanceError):
    """The edit's base is not the span's current source text; nothing was written.

    Raised from inside the rewrite, after every gate in front of it passed and
    before a byte is written. Carries the span's current source text and its
    hash so the caller can redo the edit against what the file holds.
    """

    def __init__(self, text: str, body_hash: str) -> None:
        super().__init__("edit_base_mismatch")
        self.text = text
        self.body_hash = body_hash


def _source_region_end(span: list[str]) -> int:
    """Index after the span's last non-blank line: its trailing blanks stay put."""
    end = len(span)
    while end and not span[end - 1].strip():
        end -= 1
    return end


def source_text_hash(text: str) -> str:
    """Hash of a span's source text exactly as given (no normalization)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_source_text(lines: list[str], start_line: int, end_line: int) -> str:
    """The span's source text a :class:`SourceBase` edit replaces.

    Every line of the span except its trailing blank lines, which separate it
    from the next section and are kept by the edit. *lines* is the file split
    with ``splitlines()``, as the writer splits it.
    """
    span = lines[start_line - 1 : end_line]
    return "\n".join(span[: _source_region_end(span)])


def _quote_group_start(span: list[str]) -> int | None:
    """Index of a section-leading ``>`` group after the heading line, if any."""
    i = 1 if span and span[0].lstrip().startswith("#") else 0
    while i < len(span) and not span[i].strip():
        i += 1
    return i if i < len(span) and span[i].lstrip().startswith(">") else None


def _certified_region(span: list[str], content: str, new_content: str) -> int | None:
    """Where an edit of indexed *content* starts replacing *span*, or ``None``.

    The edit replaces from that line to the span's last non-blank line, and is
    accepted only where *content* is exactly the span's text from some point on:

    - the body after the heading line and blockquote header. The header was
      hidden from *content*, so it is kept and *new_content* is the body.
    - the text from a blockquote group on, where the chunker left the group in
      *content* (a group without ``tags:``, such as an untagged ``mem_add``
      entry's ``> created:`` line). The caller was shown the group, so the
      edit says what to do with it: opening with the group verbatim replaces
      from the group; holding none of its lines and not opening with ``>`` is
      body-only, and keeps it (the body-only contract ``mem_edit`` documents).
      Anything else -- the group moved, changed, or partly kept -- is
      ambiguous between deleting and keeping it, and is refused, not guessed.

    Content held only through the ``## `` full-replacement rule is refused too:
    a base certified that way would switch rules as soon as an edit stopped
    starting with ``## `` and keep the heading the content already held.
    """
    if _is_header_override(content):
        return None
    region = span[: _source_region_end(span)]
    shown = content.lstrip("\n").splitlines()
    body_start = _find_body_start_index(region)
    if region[body_start:] == shown:
        return body_start
    quote_start = _quote_group_start(region)
    if quote_start is None or region[quote_start:] != shown:
        return None
    group = [line for line in region[quote_start:body_start] if line.strip()]
    edited = new_content.lstrip("\n").splitlines()
    if edited[: len(group)] == group:
        return quote_start
    opens_with_quote = bool(edited) and edited[0].lstrip().startswith(">")
    if not opens_with_quote and not set(group) & set(edited):
        return body_start
    return None


def _indexed_replacement(span: list[str], new_content: str, start: int) -> list[str]:
    """The span with lines ``start`` up to its trailing blanks replaced.

    *start* comes from :func:`_certified_region` for the content the caller
    edited, never from what the edit happens to begin with. Content starting
    with ``## `` replaces the heading and header too (the documented override).
    The span's trailing blank lines are kept.
    """
    end = _source_region_end(span)
    if _is_header_override(new_content):
        return new_content.splitlines() + span[end:]
    return span[:start] + new_content.lstrip("\n").splitlines() + span[end:]


def _is_header_override(content: str) -> bool:
    return content.lstrip("\n").lstrip().startswith("## ")


def replace_chunk_body(
    file_path: Path,
    start_line: int,
    end_line: int,
    new_content: str,
    *,
    base: EditBase,
    expected_source_span_hash: str | None,
    expected_identity: tuple[int, int] | None = None,
) -> Rewrite:
    """Replace a chunk's text in *file_path*, refusing when *base* is not the source.

    With a :class:`SourceBase`, *new_content* replaces the span's source text
    (every line but its trailing blanks) verbatim, once the text's hash still
    matches the one the caller edited.

    With an :class:`IndexedBase`, *new_content* is body-only: the heading line
    and the section-leading blockquote header (``> created: ...`` /
    ``> tags: ...``) are kept, unless *new_content* starts with ``## ``, which
    replaces them too. That rule is applied only when the indexed content is
    the span's body: saving it unchanged must reproduce the span, and through
    the body rule, not the ``## `` override. Otherwise the content is a
    projection of the span (resolved wikilinks, merged sections, frontmatter),
    and writing an edit of it would remove what the projection does not show.

    A refused base raises :class:`EditBaseMismatchError` carrying the span's
    current source text and hash, having written nothing.

    Never creates: a source removed under this call raises
    :class:`SourceRemovedError` and one swapped for another file (or an
    unrelated file at *expected_identity*'s path) raises
    :class:`SourceReplacedError`, in both cases having written nothing. See
    :func:`_rewrite_in_place`.
    """

    def edit(text: str) -> str:
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        _validate_source_span(lines, start_line, end_line, expected_source_span_hash)
        span = lines[start_line - 1 : end_line]
        source = chunk_source_text(lines, start_line, end_line)
        source_hash = source_text_hash(source)

        if isinstance(base, SourceBase):
            if base.body_hash != source_hash:
                raise EditBaseMismatchError(source, source_hash)
            replacement = new_content.splitlines() + span[_source_region_end(span) :]
        else:
            start = _certified_region(span, base.content, new_content)
            if start is None:
                raise EditBaseMismatchError(source, source_hash)
            replacement = _indexed_replacement(span, new_content, start)

        new_lines = lines[: start_line - 1] + replacement + lines[end_line:]
        result = "\n".join(new_lines)
        if trailing_newline:
            result += "\n"
        return result

    return _rewrite_in_place(file_path, expected_identity, edit)


def _validate_source_span(
    lines: list[str], start_line: int, end_line: int, expected: str | None
) -> None:
    """Refuse without a write when the indexed span cannot be proved (#2371)."""
    try:
        _validate_line_range(start_line, end_line, len(lines))
    except ValueError as exc:
        raise StaleSourceProvenanceError(str(exc)) from exc
    actual = source_span_hash(lines, start_line, end_line)
    if expected is None or actual != expected:
        raise StaleSourceProvenanceError("Indexed source span is missing or no longer matches")


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
    expected_source_span_hash: str | None,
    expected_identity: tuple[int, int] | None = None,
) -> Rewrite:
    """Replace lines [start_line, end_line] (1-based, inclusive) with new_content.

    Never creates; see :func:`_rewrite_in_place` for what a removed or replaced
    source raises.
    """

    def edit(text: str) -> str:
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        _validate_source_span(lines, start_line, end_line, expected_source_span_hash)
        before = lines[: start_line - 1]
        after = lines[end_line:]
        new_lines = before + new_content.splitlines() + after
        result = "\n".join(new_lines)
        if trailing_newline:
            result += "\n"
        return result

    return _rewrite_in_place(file_path, expected_identity, edit)


def remove_lines(
    file_path: Path,
    start_line: int,
    end_line: int,
    *,
    expected_source_span_hash: str | None,
    expected_identity: tuple[int, int] | None = None,
) -> Rewrite:
    """Remove lines [start_line, end_line] (1-based, inclusive) from file.

    Never creates; see :func:`_rewrite_in_place` for what a removed or replaced
    source raises. A delete whose source somebody else already removed is not a
    failure of intent, but it is this helper's business only to say so — the
    surfaces decide what to do about it.
    """

    def edit(text: str) -> str:
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        _validate_source_span(lines, start_line, end_line, expected_source_span_hash)
        new_lines = lines[: start_line - 1] + lines[end_line:]
        result = "\n".join(new_lines)
        if trailing_newline and new_lines:
            result += "\n"
        return result

    return _rewrite_in_place(file_path, expected_identity, edit)


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


def read_for_append(file_path: Path) -> JudgedSource:
    """Read ``file_path`` for an append that may create it: what the preflight judges.

    The bytes and identity come off one descriptor, as :func:`read_pre_image`
    takes them, or ``b""`` and no identity when there is no file yet: a file
    that does not exist holds nothing. The resolution follows at once, as
    :func:`read_with_source_key` takes it, and is the path the preflight judges
    the bytes under (``source_path``). Call it as one worker-thread call, so
    the caller has no ``await`` between the read and the resolution.
    """
    try:
        pre: PreImage | None = read_pre_image(file_path)
    except FileNotFoundError:
        pre = None
    try:
        source_path = resolve_path(file_path)
    except OSError:
        source_path = file_path
    return JudgedSource(
        data=b"" if pre is None else pre.data,
        identity=None if pre is None else pre.identity,
        existed=pre is not None,
        source_path=source_path,
    )


def read_with_source_key[T](read: Callable[[Path], T], file_path: Path) -> tuple[T, Path]:
    """``read(file_path)``, and the path of the source it read, resolved right after it.

    For a memory tool that hands the bytes to ``IndexEngine.preflight_mutation``
    as ``source_path``. The preflight judges the bytes under this path, not
    under a later resolution of ``file_path``, and a refusal holds the source
    under this path's stored key, so both must be the source whose bytes were
    read. Resolved later, after an ``await``, a path replaced by a symlink
    meanwhile would be judged under the link target's scope (#3191) and hold the
    link's target (#3183). Call it as one worker-thread call, so the caller has
    no ``await`` between the read and the resolution. Not seen: another process
    swapping the path between the read and the resolution right after it.

    The path is not folded: the preflight compares it with registered roots and
    manifest keys as the index gate does. Its stored key is
    ``fold_path_form(str(path))``, which is ``norm_path(file_path)``, fallback
    included: a resolution that raises ``OSError`` gives ``file_path`` as given.
    """
    data = read(file_path)
    try:
        return data, resolve_path(file_path)
    except OSError:
        return data, file_path


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
    except Exception:  # noqa: BLE001 - reported, never raised over the body's error
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
    except Exception:  # noqa: BLE001 - reported, never raised over the body's error
        logger.warning("restoring %s failed while unwinding an error", file_path, exc_info=True)
        return RestoreOutcome.failed
    return RestoreOutcome.restored

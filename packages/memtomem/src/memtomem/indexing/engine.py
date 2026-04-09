"""Indexing engine: orchestrates chunking, embedding, and storage."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.chunking.structured import StructuredChunker
from memtomem.config import IndexingConfig, NamespaceConfig
from memtomem.indexing.differ import compute_diff
from memtomem.models import Chunk, ChunkMetadata, ChunkType, IndexingStats

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class IndexEngine:
    def __init__(
        self,
        storage: StorageBackend,
        embedder: EmbeddingProvider,
        config: IndexingConfig,
        registry: ChunkerRegistry | None = None,
        namespace_config: NamespaceConfig | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._config = config
        self._ns_config = namespace_config or NamespaceConfig()
        self._registry = registry or ChunkerRegistry(
            [
                MarkdownChunker(),
                StructuredChunker(indexing_config=config),
            ]
        )
        self._index_lock = asyncio.Lock()  # prevent concurrent indexing of same files

    async def index_path(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexingStats:
        async with self._index_lock:
            return await self._index_path_inner(path, recursive, force, namespace)

    async def _index_path_inner(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexingStats:
        start = time.monotonic()
        path = path.resolve()

        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = self._discover_files(path, recursive)
        else:
            return IndexingStats(0, 0, 0, 0, 0, 0.0)

        sem = asyncio.Semaphore(8)

        async def _bounded(fp: Path) -> dict[str, int]:
            async with sem:
                return await self._index_file(fp, force, namespace=namespace)

        raw_results = await asyncio.gather(*[_bounded(f) for f in files], return_exceptions=True)
        file_results: list[dict[str, int]] = []
        all_errors: list[str] = []
        for i, r in enumerate(raw_results):
            if isinstance(r, dict):
                file_results.append(r)
                all_errors.extend(r.get("errors", []))
            elif isinstance(r, Exception):
                logger.error("Indexing failed for %s: %s", files[i], r)
                all_errors.append(f"{files[i].name}: {r}")

        duration = (time.monotonic() - start) * 1000
        return IndexingStats(
            total_files=len(files),
            total_chunks=sum(r["total"] for r in file_results),
            indexed_chunks=sum(r["indexed"] for r in file_results),
            skipped_chunks=sum(r["skipped"] for r in file_results),
            deleted_chunks=sum(r["deleted"] for r in file_results),
            duration_ms=duration,
            errors=tuple(all_errors),
        )

    async def index_file(
        self,
        file_path: Path,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexingStats:
        """Index a single file. Convenience wrapper for external callers."""
        start = time.monotonic()
        result = await self._index_file(file_path.resolve(), force, namespace=namespace)
        duration = (time.monotonic() - start) * 1000
        return IndexingStats(
            total_files=1,
            total_chunks=result["total"],
            indexed_chunks=result["indexed"],
            skipped_chunks=result["skipped"],
            deleted_chunks=result["deleted"],
            duration_ms=duration,
        )

    async def index_entry(
        self,
        content: str,
        file_path: Path,
        *,
        heading_hierarchy: tuple[str, ...] = (),
        tags: tuple[str, ...] = (),
        namespace: str | None = None,
    ) -> Chunk:
        """Index a short entry as a single chunk, bypassing the chunker.

        Intended for mem_add: the content is already a complete, small memory
        entry so splitting it would only waste embeddings and create noise.
        The file must already be written; line numbers are derived from the
        current file content.
        Returns the created Chunk (with embedding and stored in DB).
        """
        file_path = file_path.resolve()

        # Compute line range: the entry occupies the tail of the file
        file_text = file_path.read_text(encoding="utf-8", errors="replace")
        total_file_lines = file_text.count("\n") + 1
        entry_lines = content.count("\n") + 1
        start_line = max(1, total_file_lines - entry_lines + 1)

        resolved_ns = self._resolve_namespace(file_path, namespace)

        meta = ChunkMetadata(
            source_file=file_path,
            heading_hierarchy=heading_hierarchy,
            chunk_type=ChunkType.MARKDOWN_SECTION,
            start_line=start_line,
            end_line=total_file_lines,
            tags=tags,
            namespace=resolved_ns or "default",
        )
        chunk = Chunk(content=content, metadata=meta)

        embeddings = await self._embedder.embed_texts([chunk.retrieval_content])
        chunk.embedding = embeddings[0]

        await self._storage.upsert_chunks([chunk])
        return chunk

    async def is_duplicate(
        self,
        text: str,
        *,
        namespace: str | None = None,
        threshold: float = 0.92,
    ) -> bool:
        """Check if text is semantically similar to existing indexed content."""
        from memtomem.models import NamespaceFilter

        try:
            embedding = await self._embedder.embed_query(text)
            ns_filter = NamespaceFilter.parse(namespace) if namespace else None
            results = await self._storage.dense_search(
                embedding, top_k=1, namespace_filter=ns_filter
            )
            return bool(results and results[0].score >= threshold)
        except Exception:
            return False

    def _resolve_namespace(self, file_path: Path, explicit_ns: str | None) -> str | None:
        """Determine the namespace for a file.

        Priority: explicit parameter > auto_ns (folder-based) > default_namespace.
        Returns None only if default_namespace is "default" and auto_ns is off
        (preserves backward compat — chunks without namespace stay untagged).
        """
        if explicit_ns is not None:
            return explicit_ns

        if self._ns_config.enable_auto_ns:
            # Derive namespace from the immediate parent folder name,
            # but skip if the file sits at the root of a memory_dir
            # (otherwise the memory_dir folder name becomes the namespace).
            parent = file_path.parent.resolve()
            memory_roots = {Path(d).expanduser().resolve() for d in self._config.memory_dirs}
            if parent not in memory_roots:
                name = parent.name
                if name and name not in (".", ""):
                    return name

        default = self._ns_config.default_namespace
        if default and default != "default":
            return default

        return None

    async def _index_file(
        self,
        file_path: Path,
        force: bool,
        namespace: str | None = None,
    ) -> dict[str, int]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        if self._registry.get(file_path.suffix) is None:
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        new_chunks = self._registry.chunk_file(file_path, content)

        # Post-processing: merge short chunks + add overlap
        new_chunks = _merge_short_chunks(
            new_chunks,
            self._config.min_chunk_tokens,
            self._config.max_chunk_tokens,
        )
        if self._config.chunk_overlap_tokens > 0:
            new_chunks = _add_overlap(new_chunks, self._config.chunk_overlap_tokens)

        # Resolve namespace: explicit > auto_ns > default
        resolved_ns = self._resolve_namespace(file_path, namespace)
        if resolved_ns is not None:
            new_chunks = self._apply_namespace(new_chunks, resolved_ns)

        if not new_chunks:
            # File exists but is empty / unparseable — delete stale chunks
            deleted = await self._storage.delete_by_source(file_path)
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": deleted, "errors": []}

        if force:
            diff_result = type(
                "D", (), {"to_upsert": new_chunks, "to_delete": [], "unchanged": []}
            )()
        else:
            existing_hashes = await self._storage.get_chunk_hashes(file_path)
            diff_result = compute_diff(existing_hashes, new_chunks)

        # Embed BEFORE any deletion — if embedding fails, DB stays untouched
        if diff_result.to_upsert:
            texts = [c.retrieval_content for c in diff_result.to_upsert]
            try:
                embeddings = await self._embedder.embed_texts(texts)
                for chunk, emb in zip(diff_result.to_upsert, embeddings):
                    chunk.embedding = emb
            except Exception as exc:
                logger.error(
                    "Embedding failed for %s (%d chunks): %s",
                    file_path,
                    len(diff_result.to_upsert),
                    exc,
                )
                return {
                    "total": len(new_chunks),
                    "indexed": 0,
                    "skipped": len(new_chunks),
                    "deleted": 0,
                    "errors": [f"Embedding failed: {exc}"],
                }

        # Now safe to mutate DB — embedding succeeded.
        # Wrap delete+upsert in a single transaction for atomicity.
        async with self._storage.transaction():
            if force:
                await self._storage.delete_by_source(file_path)
            elif diff_result.to_delete:
                await self._storage.delete_chunks(diff_result.to_delete)

            if diff_result.to_upsert:
                await self._storage.upsert_chunks(diff_result.to_upsert)

        return {
            "total": len(new_chunks),
            "indexed": len(diff_result.to_upsert),
            "skipped": len(diff_result.unchanged),
            "deleted": len(diff_result.to_delete) if not force else 0,
            "errors": [],
        }

    async def index_path_stream(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
    ):
        """Like index_path(), but yields progress dicts as each file is processed.

        Yields dicts with ``type`` key:
        - ``"progress"``: emitted after each file with fields
          ``file, files_done, files_total, indexed, skipped``.
        - ``"complete"``: final summary (same fields as IndexingStats + duration_ms).
        """
        start = time.monotonic()
        path = path.resolve()

        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = self._discover_files(path, recursive)
        else:
            yield {
                "type": "complete",
                "total_files": 0,
                "total_chunks": 0,
                "indexed_chunks": 0,
                "skipped_chunks": 0,
                "deleted_chunks": 0,
                "duration_ms": 0.0,
            }
            return

        total_files = len(files)
        agg = {"total_chunks": 0, "indexed": 0, "skipped": 0, "deleted": 0}

        for i, fp in enumerate(files, start=1):
            try:
                result = await self._index_file(fp, force)
            except Exception as exc:
                logger.error("Stream indexing failed for %s: %s", fp, exc)
                result = {
                    "total": 0,
                    "indexed": 0,
                    "skipped": 0,
                    "deleted": 0,
                    "errors": [str(exc)],
                }
            agg["total_chunks"] += result["total"]
            agg["indexed"] += result["indexed"]
            agg["skipped"] += result["skipped"]
            agg["deleted"] += result["deleted"]
            yield {
                "type": "progress",
                "file": str(fp),
                "files_done": i,
                "files_total": total_files,
                "indexed": result["indexed"],
                "skipped": result["skipped"],
            }

        duration = (time.monotonic() - start) * 1000
        yield {
            "type": "complete",
            "total_files": total_files,
            "total_chunks": agg["total_chunks"],
            "indexed_chunks": agg["indexed"],
            "skipped_chunks": agg["skipped"],
            "deleted_chunks": agg["deleted"],
            "duration_ms": round(duration, 1),
        }

    @staticmethod
    def _apply_namespace(chunks: list[Chunk], namespace: str) -> list[Chunk]:
        """Return new Chunk instances with the given namespace applied."""
        result = []
        for c in chunks:
            new_meta = ChunkMetadata(
                source_file=c.metadata.source_file,
                heading_hierarchy=c.metadata.heading_hierarchy,
                chunk_type=c.metadata.chunk_type,
                start_line=c.metadata.start_line,
                end_line=c.metadata.end_line,
                language=c.metadata.language,
                tags=c.metadata.tags,
                namespace=namespace,
                overlap_before=c.metadata.overlap_before,
                overlap_after=c.metadata.overlap_after,
            )
            result.append(
                Chunk(
                    content=c.content,
                    metadata=new_meta,
                    id=c.id,
                    content_hash=c.content_hash,
                    embedding=c.embedding,
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                )
            )
        return result

    _EXCLUDED_DIRS = frozenset(
        {
            ".venv",
            "venv",
            ".git",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            "dist",
            "build",
            ".tox",
            ".eggs",
            ".idea",
            ".vscode",
        }
    )

    _EXCLUDED_SUFFIXES = (".egg-info",)

    @classmethod
    def _is_excluded_part(cls, part: str) -> bool:
        """Check if a path component should be excluded."""
        if part in cls._EXCLUDED_DIRS:
            return True
        return any(part.endswith(suffix) for suffix in cls._EXCLUDED_SUFFIXES)

    def _discover_files(self, directory: Path, recursive: bool) -> list[Path]:
        supported = self._registry.supported_extensions() & self._config.supported_extensions

        files: list[Path] = []
        if recursive:
            for fp in directory.rglob("*"):
                if not fp.is_file():
                    continue
                if fp.suffix not in supported:
                    continue
                # Skip excluded directories anywhere in the path
                if any(self._is_excluded_part(part) for part in fp.relative_to(directory).parts):
                    continue
                files.append(fp)
        else:
            for ext in supported:
                files.extend(directory.glob(f"*{ext}"))
        return sorted(files)


# ---------------------------------------------------------------------------
# Post-processing: merge short chunks + add overlap
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English, ~2 for Korean."""
    return max(1, len(text) // 3)


def _can_merge(current: Chunk, nxt: Chunk) -> bool:
    """Check if two chunks can be merged.

    Same-file + same-hierarchy is always allowed.
    A headingless chunk (empty hierarchy, e.g. frontmatter) can merge into
    the next chunk, adopting its hierarchy.
    """
    if current.metadata.source_file != nxt.metadata.source_file:
        return False
    if current.metadata.heading_hierarchy == nxt.metadata.heading_hierarchy:
        return True
    # Allow headingless short chunk to merge forward into the next section
    if not current.metadata.heading_hierarchy:
        return True
    return False


def _merged_hierarchy(current: Chunk, nxt: Chunk) -> tuple[str, ...]:
    """Pick the more specific heading hierarchy when merging two chunks."""
    return nxt.metadata.heading_hierarchy or current.metadata.heading_hierarchy


def _merge_short_chunks(
    chunks: list[Chunk],
    min_tokens: int,
    max_tokens: int = 0,
) -> list[Chunk]:
    """Merge consecutive same-source chunks so each is >= min_tokens and < max_tokens.

    Strategy:
    - Walk forward through chunks.
    - While the current accumulated chunk is below min_tokens, merge the next
      chunk — but only if the result stays below max_tokens.
    - Headingless chunks (e.g. frontmatter) are merged into the following
      section so they don't become orphan micro-chunks.
    - If merging would exceed max_tokens, stop and emit what we have.
    """
    if min_tokens <= 0 or len(chunks) <= 1:
        return chunks

    # When max_tokens is not set or too small, use a generous default
    if max_tokens <= min_tokens:
        max_tokens = max(min_tokens * 4, 512)

    result: list[Chunk] = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        cur_tokens = _estimate_tokens(c.content)

        # Accumulate short chunks by merging forward
        while (
            cur_tokens < min_tokens
            and i + 1 < len(chunks)
            and _can_merge(c, chunks[i + 1])
        ):
            nxt = chunks[i + 1]
            nxt_tokens = _estimate_tokens(nxt.content)
            merged_tokens = cur_tokens + nxt_tokens + 1  # +1 for separator

            # Stop merging if result would exceed max_tokens
            if merged_tokens >= max_tokens:
                break

            hierarchy = _merged_hierarchy(c, nxt)
            merged_content = c.content + "\n\n" + nxt.content
            c = Chunk(
                content=merged_content,
                metadata=ChunkMetadata(
                    source_file=c.metadata.source_file,
                    heading_hierarchy=hierarchy,
                    chunk_type=c.metadata.chunk_type,
                    start_line=c.metadata.start_line,
                    end_line=nxt.metadata.end_line,
                    language=c.metadata.language,
                    tags=tuple(set(c.metadata.tags) | set(nxt.metadata.tags)),
                    namespace=c.metadata.namespace,
                ),
            )
            cur_tokens = _estimate_tokens(c.content)
            i += 1

        result.append(c)
        i += 1
    return result


def _add_overlap(chunks: list[Chunk], overlap_tokens: int) -> list[Chunk]:
    """Add token overlap between adjacent chunks from the same source file.

    Each chunk gets a suffix from the previous chunk (overlap_before)
    and a prefix from the next chunk (overlap_after).
    overlap_before/overlap_after in metadata record the char count of overlap
    so consumers can strip it for deduplication (e.g., document reconstruction).
    """
    if overlap_tokens <= 0 or len(chunks) <= 1:
        return chunks

    overlap_chars = overlap_tokens * 3  # rough token→char conversion

    result: list[Chunk] = []
    for i, c in enumerate(chunks):
        prefix = ""
        suffix = ""
        ob = 0  # overlap_before char count
        oa = 0  # overlap_after char count

        # Borrow from previous chunk (same file)
        if i > 0 and chunks[i - 1].metadata.source_file == c.metadata.source_file:
            prev_content = chunks[i - 1].content
            prefix = (
                prev_content[-overlap_chars:] if len(prev_content) > overlap_chars else prev_content
            )
            ob = len(prefix)

        # Borrow from next chunk (same file)
        if i + 1 < len(chunks) and chunks[i + 1].metadata.source_file == c.metadata.source_file:
            next_content = chunks[i + 1].content
            suffix = (
                next_content[:overlap_chars] if len(next_content) > overlap_chars else next_content
            )
            oa = len(suffix)

        if ob == 0 and oa == 0:
            result.append(c)
            continue

        parts = []
        if prefix:
            parts.append(prefix)
        parts.append(c.content)
        if suffix:
            parts.append(suffix)

        new_content = "\n".join(parts)
        new_meta = ChunkMetadata(
            source_file=c.metadata.source_file,
            heading_hierarchy=c.metadata.heading_hierarchy,
            chunk_type=c.metadata.chunk_type,
            start_line=c.metadata.start_line,
            end_line=c.metadata.end_line,
            language=c.metadata.language,
            tags=c.metadata.tags,
            namespace=c.metadata.namespace,
            overlap_before=ob,
            overlap_after=oa,
        )
        result.append(Chunk(content=new_content, metadata=new_meta))
    return result

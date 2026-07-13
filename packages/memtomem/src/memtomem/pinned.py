"""File-backed Pinned Context blocks and deterministic context composition."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from memtomem import privacy
from memtomem.config import Mem2MemConfig, TargetScope
from memtomem.context._atomic import atomic_write_text
from memtomem.memory_scope import resolve_memory_scope_dir

PINNED_BLOCK_MAX_CHARS = 2_000
PINNED_TOTAL_MAX_CHARS = 6_000
CONTEXT_BUNDLE_MAX_CHARS = 12_000
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SCOPE_RANK = {"user": 0, "project_shared": 1, "project_local": 2}


@dataclass(frozen=True, slots=True)
class PinnedBlock:
    block_id: str
    content: str
    scope: TargetScope
    source_path: Path
    description: str = ""
    priority: int = 0
    agent_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_path"] = str(self.source_path)
        return payload


@dataclass(frozen=True, slots=True)
class ContextBundle:
    pinned: tuple[PinnedBlock, ...]
    retrieved: tuple[dict[str, Any], ...]
    max_chars: int
    used_chars: int
    omitted_block_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "pinned": [block.as_dict() for block in self.pinned],
            "retrieved": list(self.retrieved),
            "max_chars": self.max_chars,
            "used_chars": self.used_chars,
            "omitted_block_ids": list(self.omitted_block_ids),
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class _ContextWindowSelection:
    """Mutable schema-3 selection state for one matched result."""

    context: Any
    item: dict[str, Any]
    before: tuple[Any, ...]
    after: tuple[Any, ...]
    selected_before: list[Any] = field(default_factory=list)
    selected_after: list[Any] = field(default_factory=list)


def _validate_id(value: str, kind: str) -> str:
    if not _ID_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"invalid {kind}: use letters, digits, '.', '_', or '-'")
    return value


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("Pinned Context file must start with YAML frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValueError("Pinned Context frontmatter is not terminated")
    metadata = yaml.safe_load(text[4:marker]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("Pinned Context frontmatter must be a mapping")
    return metadata, text[marker + 5 :].rstrip("\n")


class PinnedContextStore:
    def __init__(
        self,
        config: Mem2MemConfig,
        *,
        project_root: Path | None = None,
    ) -> None:
        self.config = config
        self.project_root = project_root.resolve() if project_root else None
        self.user_base = Path(config.indexing.memory_dirs[0]).expanduser().resolve()

    def _base(self, scope: TargetScope) -> Path:
        return resolve_memory_scope_dir(scope, self.project_root, self.user_base) / "pinned"

    def search_exclusion_roots(self) -> tuple[Path, ...]:
        """Return every in-scope pinned root, independent of parsing or shadowing."""
        scopes: tuple[TargetScope, ...] = (
            ("user", "project_shared", "project_local")
            if self.project_root is not None
            else ("user",)
        )
        return tuple(self._base(scope).resolve(strict=False) for scope in scopes)

    def _path(self, scope: TargetScope, block_id: str, agent_id: str | None) -> Path:
        block_id = _validate_id(block_id, "block id")
        if agent_id is not None:
            agent_id = _validate_id(agent_id, "agent id")
            return self._base(scope) / "agents" / agent_id / f"{block_id}.md"
        return self._base(scope) / "general" / f"{block_id}.md"

    def set(
        self,
        block_id: str,
        content: str,
        *,
        scope: TargetScope = "user",
        agent_id: str | None = None,
        description: str = "",
        priority: int = 0,
        confirm_project_shared: bool = False,
        force_unsafe: bool = False,
    ) -> PinnedBlock:
        if len(content) > PINNED_BLOCK_MAX_CHARS:
            raise ValueError(f"Pinned Context block exceeds {PINNED_BLOCK_MAX_CHARS} characters")
        if scope == "project_shared" and not confirm_project_shared:
            raise ValueError("project_shared Pinned Context requires explicit confirmation")
        guard = privacy.enforce_write_guard(
            content,
            surface="pinned_context_set",
            scope=scope,
            force_unsafe=force_unsafe,
        )
        if guard.decision not in {"pass", "bypassed"}:
            raise ValueError(f"Pinned Context blocked by {len(guard.hits)} privacy pattern(s)")
        path = self._path(scope, block_id, agent_id)
        metadata = {
            "id": block_id,
            "description": description,
            "priority": priority,
            "agent_id": agent_id,
        }
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
        atomic_write_text(path, f"---\n{frontmatter}\n---\n{content.rstrip()}\n")
        return PinnedBlock(
            block_id=block_id,
            content=content.rstrip(),
            scope=scope,
            source_path=path,
            description=description,
            priority=priority,
            agent_id=agent_id,
        )

    def get(
        self,
        block_id: str,
        *,
        scope: TargetScope = "user",
        agent_id: str | None = None,
    ) -> PinnedBlock | None:
        path = self._path(scope, block_id, agent_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        metadata, content = _split_frontmatter(text)
        return PinnedBlock(
            block_id=str(metadata.get("id", block_id)),
            content=content,
            scope=scope,
            source_path=path,
            description=str(metadata.get("description", "")),
            priority=int(metadata.get("priority", 0)),
            agent_id=metadata.get("agent_id"),
        )

    def delete(
        self,
        block_id: str,
        *,
        scope: TargetScope = "user",
        agent_id: str | None = None,
        confirm_project_shared: bool = False,
    ) -> bool:
        if scope == "project_shared" and not confirm_project_shared:
            raise ValueError("project_shared Pinned Context requires explicit confirmation")
        path = self._path(scope, block_id, agent_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def _read_scope(self, scope: TargetScope) -> list[PinnedBlock]:
        base = self._base(scope)
        if not base.exists():
            return []
        blocks: list[PinnedBlock] = []
        for path in sorted(base.rglob("*.md")):
            try:
                metadata, content = _split_frontmatter(path.read_text(encoding="utf-8"))
                blocks.append(
                    PinnedBlock(
                        block_id=str(metadata["id"]),
                        content=content,
                        scope=scope,
                        source_path=path,
                        description=str(metadata.get("description", "")),
                        priority=int(metadata.get("priority", 0)),
                        agent_id=metadata.get("agent_id"),
                    )
                )
            except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError):
                continue
        return blocks

    def list(self, *, agent_id: str | None = None) -> list[PinnedBlock]:
        candidates: list[PinnedBlock] = []
        for scope in ("user", "project_shared", "project_local"):
            if scope != "user" and self.project_root is None:
                continue
            candidates.extend(self._read_scope(scope))
        applicable = [block for block in candidates if block.agent_id in {None, agent_id}]
        winners: dict[str, PinnedBlock] = {}
        for block in applicable:
            current = winners.get(block.block_id)
            block_rank = (1 if block.agent_id else 0, _SCOPE_RANK[block.scope])
            current_rank = (
                (1 if current.agent_id else 0, _SCOPE_RANK[current.scope]) if current else (-1, -1)
            )
            if block_rank > current_rank:
                winners[block.block_id] = block
        return sorted(winners.values(), key=lambda block: (-block.priority, block.block_id))


class ContextAssembler:
    def __init__(self, store: PinnedContextStore, search_pipeline: Any | None = None) -> None:
        self.store = store
        self.search_pipeline = search_pipeline

    async def compose(
        self,
        query: str | None = None,
        *,
        agent_id: str | None = None,
        max_chars: int = CONTEXT_BUNDLE_MAX_CHARS,
        top_k: int = 10,
        namespace: str | list[str] | None = None,
        context_window: int | None = None,
    ) -> ContextBundle:
        pinned: list[PinnedBlock] = []
        omitted: list[str] = []
        used = 0
        pinned_budget = min(PINNED_TOTAL_MAX_CHARS, max_chars)
        for block in self.store.list(agent_id=agent_id):
            if used + len(block.content) > pinned_budget:
                omitted.append(block.block_id)
                continue
            pinned.append(block)
            used += len(block.content)

        retrieved: list[dict[str, Any]] = []
        if query and self.search_pipeline is not None and used < max_chars:
            results, _ = await self.search_pipeline.search(
                query=query,
                top_k=top_k,
                namespace=namespace,
                context_window=context_window,
                project_context_root=self.store.project_root,
                exclude_source_roots=self.store.search_exclusion_roots(),
            )
            # Preserve the schema-2 matched-hit budget before spending any
            # remaining capacity on schema-3 context windows.  This keeps
            # adjacent chunks additive and prevents a high-ranked hit's
            # neighbors from crowding out a lower-ranked hit.
            matched: list[tuple[Any, dict[str, Any]]] = []
            emitted_chunk_ids: set[str] = set()
            for result in results:
                content = result.chunk.content
                if used + len(content) > max_chars:
                    continue
                chunk_id = str(result.chunk.id)
                item: dict[str, Any] = {
                    "id": chunk_id,
                    "content": content,
                    "source": str(result.chunk.metadata.source_file),
                    "namespace": str(result.chunk.metadata.namespace),
                    "score": result.score,
                }
                used += len(content)
                retrieved.append(item)
                matched.append((result, item))
                emitted_chunk_ids.add(chunk_id)

            context_windows: list[_ContextWindowSelection] = []
            for result, item in matched:
                context = getattr(result, "context", None)
                if context is not None:
                    context_windows.append(
                        _ContextWindowSelection(
                            context=context,
                            item=item,
                            before=tuple(context.window_before),
                            after=tuple(context.window_after),
                        )
                    )

            max_distance = max(
                (max(len(window.before), len(window.after)) for window in context_windows),
                default=0,
            )
            # Allocate context globally by distance. Search rank breaks ties,
            # then the before side precedes the after side deterministically.
            for distance in range(max_distance):
                for window in context_windows:
                    candidates = (
                        (window.selected_before, window.before[-(distance + 1)])
                        if distance < len(window.before)
                        else None,
                        (window.selected_after, window.after[distance])
                        if distance < len(window.after)
                        else None,
                    )
                    for candidate in candidates:
                        if candidate is None:
                            continue
                        destination, chunk = candidate
                        chunk_id = str(chunk.id)
                        if chunk_id in emitted_chunk_ids:
                            continue
                        if used + len(chunk.content) <= max_chars:
                            destination.append(chunk)
                            used += len(chunk.content)
                            emitted_chunk_ids.add(chunk_id)

            for window in context_windows:
                if window.selected_before or window.selected_after:
                    window.item["context"] = {
                        "before": [
                            self._context_chunk_payload(chunk)
                            for chunk in reversed(window.selected_before)
                        ],
                        "after": [
                            self._context_chunk_payload(chunk) for chunk in window.selected_after
                        ],
                        "chunk_position": window.context.chunk_position,
                        "total_chunks_in_file": window.context.total_chunks_in_file,
                    }
        warnings = (
            ("Pinned Context blocks omitted because the character budget was exhausted",)
            if omitted
            else ()
        )
        return ContextBundle(
            pinned=tuple(pinned),
            retrieved=tuple(retrieved),
            max_chars=max_chars,
            used_chars=used,
            omitted_block_ids=tuple(omitted),
            warnings=warnings,
        )

    @staticmethod
    def _context_chunk_payload(chunk: Any) -> dict[str, str]:
        """Serialize one adjacent chunk without changing the matched-hit contract."""
        return {
            "id": str(chunk.id),
            "content": chunk.content,
            "source": str(chunk.metadata.source_file),
            "namespace": str(chunk.metadata.namespace),
        }

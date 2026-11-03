"""The store's JSON records: the source of truth for every item.

The store keeps its own records rather than composing over
``MemtomemBaseStore`` as ADR-0042 §A4 first planned (see the ADR's amendment
and ``docs/protocol.md`` §Layout). Their paths are ``_protocol``'s case-safe
layout, whose names this store decodes back into ``(namespace, key)``. This
module keeps ``MemtomemBaseStore``'s record shape and reuses memtomem's public
pieces for everything that must stay identical: the redaction guard, the
atomic write and the ``filter`` predicate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.store.base import Item, MatchCondition, SearchItem
from memtomem import privacy
from memtomem.integrations.langgraph_store import matches_filter
from memtomem.runtime.files import atomic_write_text

from langchain_memtomem._protocol import item_from_path, record_path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SURFACE_RECORD = "langchain_memtomem_record"


class RecordError(ValueError):
    """A record file exists but is not one this store wrote whole."""


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp")
    return parsed


def _valid_index(index: Any) -> bool:
    return (
        index is None
        or type(index) is bool
        or (isinstance(index, list) and all(isinstance(x, str) for x in index))
    )


def _namespace_matches(namespace: tuple[str, ...], condition: MatchCondition) -> bool:
    path = tuple(condition.path)
    if len(path) > len(namespace):
        return False
    candidate = (
        namespace[: len(path)] if condition.match_type == "prefix" else namespace[-len(path) :]
    )
    return all(expected == "*" or expected == actual for expected, actual in zip(path, candidate))


@dataclass(frozen=True, slots=True)
class PreparedRecord:
    """A value that passed validation and the record guard, ready to write."""

    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any]
    index: bool | list[str] | None


class RecordStore:
    """Read and write the JSON records under ``records_dir``. Synchronous by design.

    The store calls these under the item's lock, on the event loop (see the
    Cancellation paragraph of ``docs/protocol.md``): each is a few small file
    operations.
    """

    def __init__(self, records_dir: Path, *, scope: str, force_unsafe: bool) -> None:
        self.records_dir = records_dir
        self.scope = scope
        self.force_unsafe = force_unsafe

    # -- one record --------------------------------------------------------

    def path(self, namespace: tuple[str, ...], key: str) -> Path:
        return record_path(self.records_dir, namespace, key)

    def read(self, namespace: tuple[str, ...], key: str) -> dict[str, Any] | None:
        """The record for ``(namespace, key)``; ``None`` when absent, ``RecordError`` when torn."""
        return self._read_path(self.path(tuple(namespace), str(key)))

    def _read_path(self, path: Path) -> dict[str, Any] | None:
        try:
            data = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("schema")
            namespace = payload["namespace"]
            key = payload["key"]
            if (
                not isinstance(namespace, list)
                or not namespace
                or not all(isinstance(segment, str) for segment in namespace)
                or not isinstance(key, str)
            ):
                raise ValueError("identity")
            # The file must sit where its own identity says it lives. The
            # layout is case-safe, so this comparison is exact on every
            # filesystem.
            if self.path(tuple(namespace), key) != path:
                raise ValueError("identity")
            if not isinstance(payload["value"], dict):
                raise ValueError("value")
            if not _valid_index(payload["index"]):
                raise ValueError("index")
            generation = payload["generation"]
            if type(generation) is not int or generation < 0:
                raise ValueError("generation")
            _parse_datetime(payload["created_at"])
            _parse_datetime(payload["updated_at"])
            return payload
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise RecordError(f"record {path.name} is malformed or unsupported") from exc

    def prepare(
        self, namespace: tuple[str, ...], key: str, value: Any, index: Any
    ) -> PreparedRecord:
        """Validate a value and run the record guard. Writes nothing.

        The guard scans the serialized value, every namespace segment, the key
        and the selectors, as ``MemtomemBaseStore`` does: a secret in any of
        them is refused before the first write of the ``put``.
        """
        if not isinstance(value, dict):
            raise ValueError("Store value must be a JSON object")
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        normalized = json.loads(serialized)
        index = list(index) if isinstance(index, list) else index
        if not _valid_index(index):
            raise ValueError("index must be a boolean, None, or string selectors")
        scan_text = "\n".join(
            [serialized, *namespace, key, *(index if isinstance(index, list) else [])]
        )
        guard = privacy.enforce_write_guard(
            scan_text,
            surface=_SURFACE_RECORD,
            scope=self.scope,
            force_unsafe=self.force_unsafe,
        )
        if guard.decision != "pass" and not (
            guard.decision == "bypassed" and self.scope != "project_shared"
        ):
            raise ValueError(f"store write blocked by {len(guard.hits)} privacy pattern(s)")
        return PreparedRecord(tuple(namespace), key, normalized, index)

    def write(
        self, prepared: PreparedRecord, *, generation: int, existing: dict[str, Any] | None
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        record = {
            "schema_version": SCHEMA_VERSION,
            "namespace": list(prepared.namespace),
            "key": prepared.key,
            "value": prepared.value,
            "index": prepared.index,
            "generation": generation,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        path = self.path(prepared.namespace, prepared.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path, json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        return record

    # -- many records ------------------------------------------------------

    def all(self) -> list[dict[str, Any]]:
        """Every readable record. A file this store did not write is skipped with a warning."""
        records: list[dict[str, Any]] = []
        for path in sorted(self.records_dir.rglob("*.json")):
            try:
                item_from_path(self.records_dir, path, ".json")
                record = self._read_path(path)
            except (ValueError, OSError):
                logger.warning("Skipping a malformed or foreign record file: %s", path.name)
                continue
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def item(record: dict[str, Any]) -> Item:
        return Item(
            namespace=tuple(record["namespace"]),
            key=record["key"],
            value=record["value"],
            created_at=_parse_datetime(record["created_at"]),
            updated_at=_parse_datetime(record["updated_at"]),
        )

    def list_namespaces(
        self,
        match_conditions: tuple[MatchCondition, ...] | None,
        max_depth: int | None,
        limit: int,
        offset: int,
    ) -> list[tuple[str, ...]]:
        namespaces = {tuple(record["namespace"]) for record in self.all()}
        if match_conditions:
            namespaces = {
                namespace
                for namespace in namespaces
                if all(_namespace_matches(namespace, condition) for condition in match_conditions)
            }
        if max_depth is not None:
            namespaces = {namespace[:max_depth] for namespace in namespaces}
        return sorted(namespaces)[offset : offset + limit]

    def search(
        self,
        prefix: tuple[str, ...],
        filters: dict[str, Any] | None,
        limit: int,
        offset: int,
    ) -> list[SearchItem]:
        """A query-less search: prefix and filter over records, newest first."""
        matched = [
            record
            for record in self.all()
            if tuple(record["namespace"][: len(prefix)]) == prefix
            and matches_filter(record["value"], filters)
        ]
        matched.sort(key=lambda record: record["updated_at"], reverse=True)
        return [
            SearchItem(
                namespace=tuple(record["namespace"]),
                key=record["key"],
                value=record["value"],
                created_at=_parse_datetime(record["created_at"]),
                updated_at=_parse_datetime(record["updated_at"]),
                score=None,
            )
            for record in matched[offset : offset + limit]
        ]

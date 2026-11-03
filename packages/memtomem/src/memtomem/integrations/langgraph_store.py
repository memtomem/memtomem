"""LangGraph ``BaseStore`` adapter with file-backed JSON as source of truth."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import logging
import os
import re
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, cast

try:
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        MatchCondition,
        Op,
        PutOp,
        Result,
        SearchItem,
        SearchOp,
        get_text_at_path,
    )
except ImportError as exc:  # pragma: no cover - exercised by minimal-install smoke
    raise ImportError(
        "MemtomemBaseStore requires the langgraph extra: pip install 'memtomem[langgraph]'"
    ) from exc

from memtomem import privacy
from memtomem.config import EmbeddingConfig, Mem2MemConfig, TargetScope
from memtomem.context._atomic import (
    _file_lock,
    atomic_write_bytes,
    atomic_write_text,
    rename_no_replace,
)
from memtomem.embedding.base import EmbeddingProvider
from memtomem.embedding.factory import create_embedder
from memtomem.memory_scope import require_user_base, resolve_memory_scope_dir

logger = logging.getLogger(__name__)


class StoreRecordError(ValueError):
    """A present canonical JSON record cannot be read safely."""


_TOKEN_RE = re.compile(r"[\w.-]+", re.UNICODE)

# On-disk layout. Records live under ``root/records.v2``; files the layout
# migration could not import go to ``root/quarantine.v2``. Both names hold a
# ``.``, which the legacy base64url alphabet lacks, so no legacy token can
# collide with them.
_RECORDS_DIR = "records.v2"
_QUARANTINE_DIR = "quarantine.v2"
_LAYOUT_LOCK = ".layout.lock"
_LAYOUT_LOCK_TIMEOUT_S = 30.0

# A record is written through ``atomic_write_text``, whose temporary file is
# ``.<token>.json.<8 random>.tmp``: 19 characters beside the token. 255 - 19.
_MAX_TOKEN = 236


def _check_segment(value: str) -> bytes:
    if not value or value in {".", ".."} or "\x00" in value:
        raise ValueError("LangGraph namespace segments and keys must be non-empty and safe")
    return value.encode("utf-8")


def _b32_lower(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def encode_segment(value: str) -> str:
    """Encode one LangGraph namespace segment or key as a path-safe token.

    ``s`` + lowercase RFC 4648 base32 of the UTF-8 value, without padding. The
    alphabet has one case only, so two different values never share a path on
    a filesystem that folds case (APFS as macOS formats it, NTFS). A token that
    would be longer than :data:`_MAX_TOKEN` characters is replaced by ``h`` +
    lowercase base32 of the value's SHA-256 (53 characters), which keeps every
    file name inside the 255-byte limit; the store reads identity from the
    record, never from the name. Empty, ``.``, ``..`` and NUL-bearing values
    are refused. Public so that a package built on this store (ADR-0042) can
    share one encoding instead of copying it.
    """
    raw = _check_segment(value)
    token = "s" + _b32_lower(raw)
    if len(token) <= _MAX_TOKEN:
        return token
    return "h" + _b32_lower(hashlib.sha256(raw).digest())


_SEGMENT_TOKEN_RE = re.compile(r"s[a-z2-7]+")


def decode_segment(token: str) -> str:
    """Inverse of :func:`encode_segment`; refuses anything it did not produce.

    Only ``s`` tokens decode. A hashed ``h`` token has no inverse, and an ``s``
    token longer than the limit is one the encoder would have hashed. A naive
    re-pad-and-decode also accepts tokens with non-canonical trailing bits, so
    the value must re-encode to exactly the token.
    """
    if not isinstance(token, str) or not _SEGMENT_TOKEN_RE.fullmatch(token):
        raise ValueError("not an encoded LangGraph segment")
    body = token[1:].upper()
    try:
        raw = base64.b32decode(body + "=" * (-len(body) % 8))
        value = raw.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("not an encoded LangGraph segment") from exc
    try:
        canonical = encode_segment(value)
    except ValueError as exc:
        raise ValueError("not an encoded LangGraph segment") from exc
    if canonical != token:
        raise ValueError("not an encoded LangGraph segment")
    return value


_encode_segment = encode_segment


def _legacy_encode_segment(value: str) -> str:
    """The base64url codec of the pre-``records.v2`` layout, for migration only.

    base64url is case-sensitive, so on a case-folding filesystem ``aaa``
    (``YWFh``) and ``aaG`` (``YWFH``) named the same path (#3034).
    """
    return base64.urlsafe_b64encode(_check_segment(value)).decode("ascii").rstrip("=")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_record(text: str) -> dict[str, Any]:
    """Validate a record's shape; the caller checks that its path matches its identity."""
    try:

        def reject(value):
            raise ValueError("non-finite number")

        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("non-finite number")
            return number

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        payload = json.loads(
            text, parse_constant=reject, parse_float=finite_float, object_pairs_hook=unique
        )
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
            or not isinstance(payload.get("value"), dict)
        ):
            raise ValueError("schema")
        ns, key = payload["namespace"], payload["key"]
        if not isinstance(ns, list) or not all(isinstance(x, str) for x in ns):
            raise ValueError("identity")
        if not isinstance(key, str):
            raise ValueError("identity")
        for field in ("created_at", "updated_at"):
            if (
                not isinstance(payload[field], str)
                or _parse_datetime(payload[field]).tzinfo is None
            ):
                raise ValueError("timestamp")
        index = payload["index"]
        if (
            index is not None
            and type(index) is not bool
            and not (isinstance(index, list) and all(isinstance(x, str) for x in index))
        ):
            raise ValueError("index")
        if not isinstance(payload["projection"], str):
            raise ValueError("projection")
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise StoreRecordError("Stored LangGraph record is malformed or unsupported") from exc


def _read_path(value: Any, path: str) -> list[Any]:
    if path == "$":
        return [value]
    try:
        found = get_text_at_path(value, path)
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    if found is None:
        return []
    if found is True:
        return [value]
    if found is False:
        return []
    return list(cast(list[str], found))


def render_projection(value: dict[str, Any], index: bool | list[str] | None) -> str:
    """The text of ``value`` that ``index`` selects for search, ``""`` for ``False``.

    ``None`` and ``True`` take the whole value; a list of JSON-path selectors
    takes those fields, strings verbatim and other values as sorted JSON.
    """
    if index is False:
        return ""
    fields = ["$"] if index is None or index is True else index
    parts: list[str] = []
    for field in fields:
        for found in _read_path(value, field):
            if isinstance(found, str):
                parts.append(found)
            else:
                parts.append(json.dumps(found, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


_projection = render_projection


def _lookup(value: dict[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _compare(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, dict) or not any(str(key).startswith("$") for key in expected):
        return actual == expected
    for operator, operand in expected.items():
        if operator == "$eq" and actual != operand:
            return False
        if operator == "$ne" and actual == operand:
            return False
        if operator == "$gt" and not (actual is not None and actual > operand):
            return False
        if operator == "$gte" and not (actual is not None and actual >= operand):
            return False
        if operator == "$lt" and not (actual is not None and actual < operand):
            return False
        if operator == "$lte" and not (actual is not None and actual <= operand):
            return False
        if operator == "$in" and actual not in operand:
            return False
        if operator == "$nin" and actual in operand:
            return False
    return True


def matches_filter(value: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    """Whether ``value`` satisfies a LangGraph ``search(filter=...)`` mapping.

    Each key is a dotted path into ``value``; each expected value is either a
    literal (equality) or an operator mapping (``$eq``, ``$ne``, ``$gt``,
    ``$gte``, ``$lt``, ``$lte``, ``$in``, ``$nin``). ``None`` or an empty
    mapping matches everything. Public since ADR-0042: a store that ranks
    this store's records by another engine still applies the same filter.
    """
    return not filters or all(
        _compare(_lookup(value, key), expected) for key, expected in filters.items()
    )


_matches_filter = matches_filter


def _namespace_matches(namespace: tuple[str, ...], condition: MatchCondition) -> bool:
    path = condition.path
    if len(path) > len(namespace):
        return False
    candidate = (
        namespace[: len(path)] if condition.match_type == "prefix" else namespace[-len(path) :]
    )
    return all(expected == "*" or expected == actual for expected, actual in zip(path, candidate))


class MemtomemBaseStore(BaseStore):
    """A LangGraph store whose canonical records are inspectable JSON files.

    Exact operations require no embedding provider. Semantic search uses the
    configured memtomem embedder when available and degrades to lexical
    overlap for a BM25-only/minimal installation.

    Records live under ``root/records.v2``, one directory per namespace segment
    and one file per key, named by :func:`encode_segment`. Opening a store moves
    records of the earlier base64url layout there; see
    :meth:`_migrate_legacy_layout`.
    """

    supports_ttl = False

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        scope: TargetScope = "user",
        project_root: str | Path | None = None,
        confirm_project_shared: bool = False,
        embedding: EmbeddingConfig | None = None,
        force_unsafe: bool = False,
    ) -> None:
        config = Mem2MemConfig()
        from memtomem.config import classify_scope, load_config_d, load_config_overrides

        # Registry lookup only, on a throwaway config. Two reasons it is not
        # the store's own ``config``:
        #
        # * loading overrides into ``config`` would change what an
        #   explicit-``root`` caller gets for everything else it carries —
        #   notably ``self._embedding_config = embedding or config.embedding``,
        #   which would start resolving a persisted (possibly remote)
        #   provider where it used to see defaults. That is unrelated to
        #   which tier this store writes to.
        # * ``load_config_overrides`` defaults to ``migrate=True``, so a
        #   lookup done for a *refusal* could rewrite ~/.memtomem/config.json
        #   as a side effect of constructing a store that then raises. Read
        #   paths pass ``migrate=False`` for exactly this reason (see
        #   ``feedback_doctor_no_migration_loader``).
        def _registered_project_dirs() -> list:
            registry = Mem2MemConfig()
            load_config_d(registry)
            load_config_overrides(registry, migrate=False)
            return list(registry.indexing.project_memory_dirs)

        # The tier this store actually writes to, not the one it was told.
        # ``root`` is caller-supplied and is never validated against the
        # project registry, so ``MemtomemBaseStore(root=<proj>/.memtomem/
        # memories/x, scope="user")`` used to clear the gate below on the
        # declared scope and then put files into the git-tracked tier —
        # with every later ``put`` running Gate A as ``user``, so the
        # project_shared hard refusal of ``force_unsafe`` never fired.
        # Escalate only: a caller who declares ``project_shared`` keeps it
        # even when the path does not classify (an unregistered project
        # tree), because downgrading would *weaken* their Gate A.
        effective_scope: TargetScope = scope
        if root is not None:
            inferred, _ = classify_scope(
                Path(root).expanduser().resolve(), _registered_project_dirs()
            )
            if inferred == "project_shared":
                effective_scope = "project_shared"

        if effective_scope == "project_shared" and not confirm_project_shared:
            raise ValueError(
                "scope='project_shared' requires confirm_project_shared=True because writes are git-tracked"
                + (
                    ""
                    if scope == effective_scope
                    else f" (scope inferred from root=; {root} is a registered project_shared tier)"
                )
            )
        # ADR-0011 §5 Gate B consent (#2306). The consent is per-store, not
        # per-put: constructing the store with this scope authorises every
        # later ``put``, so it is recorded once, here. Each put still runs
        # Gate A under ``langgraph_basestore_put``.
        if effective_scope == "project_shared":
            privacy.emit_project_shared_confirmation(
                surface="langgraph_basestore_init",
                mechanism="param",
                action="init",
                audit_context={"scope_inferred_from_root": scope != effective_scope},
            )
        if root is None:
            # Unchanged from before #2321: the derived-root branch loads the
            # real configuration into ``config`` (migration included), and an
            # explicit-root store keeps seeing defaults here.
            load_config_d(config)
            load_config_overrides(config)
            project = Path(project_root).expanduser().resolve() if project_root else None
            if scope == "user":
                # Raises ConfigError on empty ``memory_dirs`` — this store
                # mkdirs its root, so it needs a real write target (#1768) —
                # and on a ``memory_dirs[0]`` that is itself a registered
                # project tier, which would put this store's writes in the
                # git-tracked tier with no Gate B (#2322).
                base = resolve_memory_scope_dir(
                    scope,
                    project,
                    require_user_base(
                        config.indexing.memory_dirs, config.indexing.project_memory_dirs
                    ),
                )
            else:
                base = resolve_memory_scope_dir(scope, project)
            root = base / "langgraph-store"
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Every ``put``'s Gate A reads this, so it has to be the tier the
        # files land in — otherwise a caller-supplied project_shared ``root``
        # keeps scanning as ``user`` and ``force_unsafe`` stays open on a
        # git-tracked destination.
        self.scope = effective_scope
        self.force_unsafe = force_unsafe
        self._embedding_config = embedding or config.embedding
        self._embedder: EmbeddingProvider | None = None
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embedding_cache_limit = 1024
        self._lock = threading.RLock()
        self._records_dir = self.root / _RECORDS_DIR
        self._migrate_legacy_layout()

    def _path(self, namespace: tuple[str, ...], key: str) -> Path:
        parts = [_encode_segment(segment) for segment in namespace]
        return self._records_dir.joinpath(*parts, f"{_encode_segment(str(key))}.json")

    def _read_record(self, path: Path) -> dict[str, Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeError as exc:
            raise StoreRecordError("Stored LangGraph record is not UTF-8") from exc
        payload = _parse_record(text)
        try:
            if self._path(tuple(payload["namespace"]), payload["key"]) != path:
                raise ValueError("identity")
        except ValueError as exc:
            raise StoreRecordError("Stored LangGraph record is malformed or unsupported") from exc
        return payload

    # -- layout migration (#3034) ---------------------------------------------
    #
    # Records used to sit directly under ``root`` in base64url, which a
    # case-folding filesystem cannot keep apart. Opening a store moves every
    # such record to ``records.v2`` once. A record names its own identity, so
    # the legacy file name is only checked, never decoded.

    def _legacy_files(self) -> list[Path]:
        found: list[Path] = []
        for entry in sorted(self.root.iterdir()):
            if entry.name in {_RECORDS_DIR, _QUARANTINE_DIR}:
                continue
            if entry.is_dir() and not entry.is_symlink():
                found.extend(sorted(entry.rglob("*.json")))
            elif entry.suffix == ".json" and (entry.is_file() or entry.is_symlink()):
                found.append(entry)
        return found

    def _migrate_legacy_layout(self) -> None:
        # The unlocked look keeps a fresh or migrated store from creating a
        # lock file; anything that appears after it is picked up next open.
        if not self._legacy_files():
            return
        with _file_lock(self.root / _LAYOUT_LOCK, timeout=_LAYOUT_LOCK_TIMEOUT_S):
            legacy = self._legacy_files()
            if legacy:
                self._migrate_locked(legacy)

    def _importable(self, path: Path) -> tuple[tuple[str, ...], str, bytes] | None:
        """The identity and bytes of a legacy record this store wrote, else ``None``.

        The path must be the legacy path for the identity the record holds, or
        another spelling of it that the filesystem resolves to the same file.
        That second case is a case-folding filesystem storing a record under a
        directory a case variant had created; on a case-sensitive filesystem a
        variant spelling is a different file and is not imported. Symlinks
        anywhere below ``root`` are not followed.
        """
        current = path
        while current != self.root:
            if current.is_symlink():
                return None
            current = current.parent
        try:
            data = path.read_bytes()
            payload = _parse_record(data.decode("utf-8"))
            namespace, key = tuple(payload["namespace"]), payload["key"]
            expected = self.root.joinpath(
                *(_legacy_encode_segment(s) for s in namespace),
                f"{_legacy_encode_segment(key)}.json",
            )
            # The filesystem decides, not path equality: WindowsPath compares
            # case-insensitively even in a case-sensitive directory.
            if not os.path.samefile(path, expected):
                return None
        except (OSError, ValueError):  # StoreRecordError and UnicodeError are ValueErrors
            return None
        return namespace, key, data

    def _migrate_locked(self, legacy: list[Path]) -> None:
        migrated = quarantined = left = 0
        for path in legacy:
            found = self._importable(path)
            if found is None:
                left += 1
                continue
            namespace, key, data = found
            target = self._path(namespace, key)
            try:
                current: bytes | None = target.read_bytes()
            except FileNotFoundError:
                current = None
            if current is None:
                # Write before unlink: a crash in between leaves two identical
                # copies, which the next open resolves below.
                atomic_write_bytes(target, data)
                path.unlink()
                migrated += 1
            elif current == data:
                path.unlink()
                migrated += 1
            else:
                # A newer record already holds this identity (or a corrupt one,
                # which is never silently replaced). Keeping the legacy file
                # where the scan finds it would bring it back after a delete.
                self._quarantine(path)
                quarantined += 1
            self._prune_legacy_dirs(path.parent)

        if migrated:
            logger.info("Moved %d LangGraph record(s) to the case-safe layout", migrated)
        if quarantined or left:
            logger.warning(
                "LangGraph store layout migration: moved %d conflicting legacy record(s) to "
                "%s and left %d legacy file(s) it could not import in place",
                quarantined,
                _QUARANTINE_DIR,
                left,
            )

    def _quarantine(self, path: Path) -> None:
        directory = self.root / _QUARANTINE_DIR
        directory.mkdir(exist_ok=True, mode=0o700)
        rename_no_replace(
            path, directory / f"{uuid.uuid4().hex}.json.conflict", allow_cross_parent=True
        )
        self._prune_legacy_dirs(path.parent)

    def _prune_legacy_dirs(self, current: Path) -> None:
        while current != self.root and self.root in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with self._lock:
            for path in sorted(self._records_dir.rglob("*.json")):
                try:
                    record = self._read_record(path)
                except (StoreRecordError, UnicodeError, OSError):
                    logger.warning("Skipping malformed or unreadable LangGraph record")
                    continue
                if record is not None:
                    records.append(record)
        return records

    @staticmethod
    def _item(record: dict[str, Any]) -> Item:
        return Item(
            namespace=tuple(record["namespace"]),
            key=record["key"],
            value=record["value"],
            created_at=_parse_datetime(record["created_at"]),
            updated_at=_parse_datetime(record["updated_at"]),
        )

    # -- record contract (public, ADR-0042 §A2) -------------------------------
    #
    # A package that composes over this store's JSON records needs to read and
    # write them without reaching for the underscore helpers. These three
    # methods are that contract. ``write_record`` and ``_put`` share one
    # implementation, ``_write_record_locked``, so the BaseStore path and the
    # record path cannot drift apart.

    def record_path(self, namespace: tuple[str, ...], key: str) -> Path:
        """Where the record for ``(namespace, key)`` lives, under ``root/records.v2``."""
        return self._path(tuple(namespace), key)

    def read_record(self, namespace: tuple[str, ...], key: str) -> dict[str, Any] | None:
        """The stored record, ``None`` when absent.

        Raises :class:`StoreRecordError` for a present file that cannot be read
        safely, exactly as the BaseStore operations do.
        """
        with self._lock:
            return self._read_record(self._path(tuple(namespace), key))

    def write_record(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        *,
        index: bool | list[str] | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """Write the record for ``(namespace, key)`` and return what was written.

        Runs the same privacy guard and ``project_shared`` refusal as ``put``,
        over the serialized value, the namespace, the key and the selectors.
        ``generation`` is a caller-owned non-negative integer persisted beside
        the value; this store never reads it. An overwrite that omits it drops
        the key, because the caller owns it. ``created_at`` is preserved across
        overwrites.
        """
        namespace = tuple(namespace)
        if not isinstance(key, str):
            raise ValueError("key must be a string")
        if generation is not None and (
            type(generation) is not int or generation < 0  # bool is excluded on purpose
        ):
            raise ValueError("generation must be a non-negative int")
        path = self._path(namespace, key)
        with self._lock:
            return self._write_record_locked(path, namespace, key, value, index, generation)

    def _put(self, op: PutOp) -> None:
        namespace, key = tuple(op.namespace), op.key
        if not isinstance(key, str):
            raise ValueError("key must be a string")
        path = self._path(namespace, key)
        with self._lock:
            if op.value is None:
                path.unlink(missing_ok=True)
                current = path.parent
                while current != self._records_dir:
                    try:
                        current.rmdir()
                    except OSError:
                        break
                    current = current.parent
                return
            self._write_record_locked(path, namespace, key, op.value, op.index, None)

    def _write_record_locked(
        self,
        path: Path,
        namespace: tuple[str, ...],
        key: str,
        raw_value: Any,
        raw_index: Any,
        generation: int | None,
    ) -> dict[str, Any]:
        if not isinstance(raw_value, dict):
            raise ValueError("Store value must be a JSON object")
        serialized = json.dumps(raw_value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        value = json.loads(serialized)
        index = list(raw_index) if isinstance(raw_index, list) else raw_index
        if (
            index is not None
            and type(index) is not bool
            and not (isinstance(index, list) and all(isinstance(x, str) for x in index))
        ):
            raise ValueError("index must be a boolean, None, or string selectors")
        scan_text = "\n".join(
            [serialized, *namespace, key, *(index if isinstance(index, list) else [])]
        )
        guard = privacy.enforce_write_guard(
            scan_text,
            surface="langgraph_basestore_put",
            scope=self.scope,
            force_unsafe=self.force_unsafe,
        )
        if guard.decision != "pass" and not (
            guard.decision == "bypassed" and self.scope != "project_shared"
        ):
            raise ValueError(
                f"LangGraph store write blocked by {len(guard.hits)} privacy pattern(s)"
            )
        existing = self._read_record(path)
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        record = {
            "schema_version": 1,
            "namespace": list(namespace),
            "key": key,
            "value": value,
            "index": index,
            "projection": render_projection(value, index),
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        if generation is not None:
            record["generation"] = generation
        atomic_write_text(
            path, json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        return record

    def _get(self, op: GetOp) -> Item | None:
        with self._lock:
            record = self._read_record(self._path(op.namespace, op.key))
        return self._item(record) if record else None

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        namespaces = {tuple(record["namespace"]) for record in self._records()}
        if op.match_conditions:
            namespaces = {
                namespace
                for namespace in namespaces
                if all(
                    _namespace_matches(namespace, condition) for condition in op.match_conditions
                )
            }
        if op.max_depth is not None:
            namespaces = {namespace[: op.max_depth] for namespace in namespaces}
        return sorted(namespaces)[op.offset : op.offset + op.limit]

    async def _ensure_embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = create_embedder(self._embedding_config)
        return self._embedder

    async def _dense_scores(self, query: str, records: list[dict[str, Any]]) -> list[float]:
        embedder = await self._ensure_embedder()
        if embedder.dimension == 0:
            return [0.0] * len(records)
        query_vector = await embedder.embed_query(query)
        projections = [record.get("projection", "") for record in records]
        missing = list(
            dict.fromkeys(
                text for text in projections if text and text not in self._embedding_cache
            )
        )
        if missing:
            vectors = await embedder.embed_texts(missing)
            for text, vector in zip(missing, vectors):
                self._embedding_cache[text] = vector
                self._embedding_cache.move_to_end(text)
                if len(self._embedding_cache) > self._embedding_cache_limit:
                    self._embedding_cache.popitem(last=False)
        scores: list[float] = []
        qnorm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
        for text in projections:
            vector = self._embedding_cache.get(text, [])
            if not vector:
                scores.append(0.0)
                continue
            self._embedding_cache.move_to_end(text)
            vnorm = math.sqrt(sum(value * value for value in vector)) or 1.0
            scores.append(sum(a * b for a, b in zip(query_vector, vector)) / (qnorm * vnorm))
        return scores

    async def _search(self, op: SearchOp) -> list[SearchItem]:
        records = [
            record
            for record in self._records()
            if tuple(record["namespace"][: len(op.namespace_prefix)]) == op.namespace_prefix
            and matches_filter(record["value"], op.filter)
            and (not op.query or bool(record.get("projection")))
        ]
        scores: list[float | None]
        if op.query:
            query_tokens = {token.lower() for token in _TOKEN_RE.findall(op.query)}
            lexical = []
            for record in records:
                tokens = {
                    token.lower() for token in _TOKEN_RE.findall(record.get("projection", ""))
                }
                lexical.append(len(query_tokens & tokens) / max(1, len(query_tokens)))
            dense = await self._dense_scores(op.query, records)
            scores = [(left + right) / 2 for left, right in zip(lexical, dense)]
            paired = sorted(zip(records, scores), key=lambda pair: pair[1] or 0.0, reverse=True)
        else:
            scores = [None] * len(records)
            paired = sorted(
                zip(records, scores), key=lambda pair: pair[0]["updated_at"], reverse=True
            )
        return [
            SearchItem(
                namespace=tuple(record["namespace"]),
                key=record["key"],
                value=record["value"],
                created_at=_parse_datetime(record["created_at"]),
                updated_at=_parse_datetime(record["updated_at"]),
                score=score,
            )
            for record, score in paired[op.offset : op.offset + op.limit]
        ]

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abatch(ops))
        raise RuntimeError("Use await store.abatch()/aget()/aput() inside an active event loop")

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        for op in list(ops):
            if isinstance(op, PutOp):
                await asyncio.to_thread(self._put, op)
                results.append(None)
            elif isinstance(op, GetOp):
                results.append(await asyncio.to_thread(self._get, op))
            elif isinstance(op, SearchOp):
                results.append(await self._search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(await asyncio.to_thread(self._list_namespaces, op))
            else:  # pragma: no cover - future LangGraph op must fail closed
                raise TypeError(f"unsupported LangGraph store operation: {type(op).__name__}")
        return results

    async def aclose(self) -> None:
        if self._embedder is not None:
            await self._embedder.close()
            self._embedder = None
            self._embedding_cache.clear()

    def close(self) -> None:
        if self._embedder is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError("Use await store.aclose() inside an active event loop")

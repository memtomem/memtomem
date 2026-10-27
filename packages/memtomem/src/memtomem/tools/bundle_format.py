"""Memory bundle wire validation and project mapping, before any storage work."""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING
from uuid import UUID

from memtomem.config import classify_scope
from memtomem.storage.sqlite_helpers import norm_path

if TYPE_CHECKING:
    from memtomem.config import IndexingConfig
    from memtomem.models import Chunk

BUNDLE_FORMAT = "memtomem-memory-bundle"
BUNDLE_VERSION = "4"
MAX_BUNDLE_BYTES = 100 * 1024 * 1024
SCOPES = {"user", "project_local", "project_shared"}
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


class BundleValidationError(ValueError):
    """A value-free validation error that ingress adapters may safely expose."""

    def __init__(self, where: str, field: str) -> None:
        self.where, self.field = where, field
        super().__init__(f"Invalid memory bundle: {where} field {field}. No records imported.")


class BundleSizeError(ValueError):
    """The serialized bundle exceeds the common transport ceiling."""


class StoredBundleValidationError(ValueError):
    """A value-free stored-record export error safe for transport adapters."""


def invalid(where: str, field: str) -> BundleValidationError:
    # Neither a secret-bearing value nor a decoder exception belongs in a response.
    return BundleValidationError(where, field)


def _pairs(items: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in items:
        if key in result:
            raise invalid("JSON", "duplicate key")
        result[key] = value
    return result


def load_json(text: str) -> dict:
    def reject_constant(value: str) -> None:
        raise invalid("JSON", "non-finite number")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise invalid("JSON", "non-finite number")
        return number

    try:
        data = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=reject_constant, parse_float=finite_float
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise invalid("JSON", "encoding or syntax") from exc
    if not isinstance(data, dict):
        raise invalid("JSON", "object required")
    version = data.get("version", "1")
    if version not in ("1", "2", "3", "4") or not isinstance(version, str):
        raise invalid("header", "version")
    fmt = data.get("format")
    if (version == "4" and fmt != BUNDLE_FORMAT) or (fmt is not None and fmt != BUNDLE_FORMAT):
        raise invalid("header", "format (use context import for artifact bundles)")
    if not isinstance(data.get("chunks"), list):
        raise invalid("header", "chunks")
    if not isinstance(data.get("relations", []), list):
        raise invalid("header", "relations")
    for key in ("total_chunks", "omitted_held_sources", "omitted_self_relations"):
        if key in data and (type(data[key]) is not int or data[key] < 0):
            raise invalid("header", key)
    if "total_chunks" in data and data["total_chunks"] != len(data["chunks"]):
        raise invalid("header", "total_chunks")
    if "exported_at" in data:
        _string(data["exported_at"], "header", "exported_at")
        if data["exported_at"]:
            timestamp(data["exported_at"], "header", "exported_at")
    if data.get("provenance") is not None and not isinstance(data["provenance"], dict):
        raise invalid("header", "provenance")
    return data


def _string(
    value: object, where: str, field: str, *, nonempty: bool = False, allow_nul: bool = False
) -> None:
    if (
        not isinstance(value, str)
        or (nonempty and not value)
        or (not allow_nul and "\x00" in value)
    ):
        raise invalid(where, field)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise invalid(where, field) from exc


def _uuid(value: object, where: str, field: str) -> None:
    _string(value, where, field, nonempty=True)
    try:
        UUID(str(value))
    except ValueError as exc:
        raise invalid(where, field) from exc


def timestamp(value: object, where: str, field: str) -> datetime:
    _string(value, where, field)
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise invalid(where, field) from exc


def stale_unmapped_end(start: object, end: object) -> bool:
    """Whether a range is the ``(0, N)`` an older merge stored for an unmapped chunk.

    Clearing ``end`` repairs it without moving ``start_line``, which is part of
    the row's unique key, so no two rows can collapse into one.
    """
    return type(start) is int and type(end) is int and start == 0 and end > 0


def legacy_unmapped_range(record: dict, version: str) -> bool:
    """Whether a v1-v3 record carries a ``(0, N)`` range that import clears."""
    return (
        version != "4"
        and "start_line" in record
        and "end_line" in record
        and stale_unmapped_end(record["start_line"], record["end_line"])
    )


def validate_records(records: list, relations: list, version: str) -> None:
    identities: set[str] = set()
    for index, record in enumerate(records):
        where = f"record {index}"
        if not isinstance(record, dict):
            raise invalid(where, "object required")
        required = {"content", "source_file"}
        if version == "4":
            required |= {
                "chunk_id",
                "scope",
                "project_root",
                "valid_from_unix",
                "valid_to_unix",
                "updated_at",
                "overlap_before",
                "overlap_after",
            }
        if required - record.keys():
            raise invalid(where, sorted(required - record.keys())[0])
        for key in ("content", "source_file", "retrieval_context", "language", "namespace"):
            if key in record:
                _string(
                    record[key],
                    where,
                    key,
                    nonempty=key == "source_file",
                    allow_nul=key not in {"source_file", "namespace"},
                )
        for key in ("heading_hierarchy", "tags"):
            if key in record:
                if not isinstance(record[key], list):
                    raise invalid(where, key)
                for item in record[key]:
                    _string(item, where, key, allow_nul=True)
        for key in ("start_line", "end_line", "redaction_count", "overlap_before", "overlap_after"):
            if key in record and (
                type(record[key]) is not int or not 0 <= record[key] <= INT64_MAX
            ):
                raise invalid(where, key)
        # An older structured-file merge stored an unmapped chunk as (0, N).
        # Import clears it on a v1-v3 record; v4 exports never carry it.
        start, end = record.get("start_line", 0), record.get("end_line", 0)
        if not legacy_unmapped_range(record, version) and (
            end < start or (start == 0) != (end == 0)
        ):
            raise invalid(where, "line range")
        for key in ("overlap_before", "overlap_after"):
            if record.get(key, 0) > len(record["content"]):
                raise invalid(where, key)
        if "source_read_only" in record and type(record["source_read_only"]) is not bool:
            raise invalid(where, "source_read_only")
        for key in ("created_at", "updated_at"):
            if key in record:
                timestamp(record[key], where, key)
        for key in ("valid_from_unix", "valid_to_unix"):
            if record.get(key) is not None and (
                type(record[key]) is not int or not INT64_MIN <= record[key] <= INT64_MAX
            ):
                raise invalid(where, key)
        # Inverted bounds are a stored empty interval, including native indexed
        # frontmatter typos. Preserve both bounds without changing search meaning.
        scope, root = record.get("scope", "user"), record.get("project_root")
        if not isinstance(scope, str) or scope not in SCOPES:
            raise invalid(where, "scope")
        if (root is None) != (scope == "user") or (version != "4" and scope != "user"):
            raise invalid(where, "project_root/scope (re-export as v4)")
        if root is not None:
            _string(root, where, "project_root", nonempty=True)
        if "chunk_id" in record:
            _uuid(record["chunk_id"], where, "chunk_id")
            identity = str(UUID(record["chunk_id"]))
            if identity in identities:
                raise invalid(where, "duplicate chunk_id")
            identities.add(identity)
    for index, edge in enumerate(relations):
        where = f"relation {index}"
        if not isinstance(edge, dict):
            raise invalid(where, "object required")
        for key in ("source_id", "target_id"):
            _uuid(edge.get(key), where, key)
            if str(UUID(edge[key])) not in identities:
                raise invalid(where, key)
        # Older native exporters emitted self-edges. Keep their signed payload
        # intact and let relation landing skip them; v4 exports omit them.
        if version == "4" and UUID(edge["source_id"]) == UUID(edge["target_id"]):
            raise invalid(where, "self relation")
        _string(edge.get("relation_type"), where, "relation_type", allow_nul=True)


def _pure(value: str):
    return PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)


def validate_root_map(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise invalid("options", "project_root_map")
    result: dict = {}
    for old, new in value.items():
        _string(old, "options", "project_root_map key", nonempty=True)
        _string(new, "options", "project_root_map value", nonempty=True)
        key = _pure(old)
        if not key.is_absolute() or ".." in key.parts or key in result:
            raise invalid("options", "project_root_map key")
        result[key] = Path(new).expanduser().resolve()
    return result


def map_chunk(
    chunk: Chunk,
    version: str,
    root_map: dict,
    config: IndexingConfig | None,
    *,
    record_index: int,
    source_file: str,
    project_root: str | None,
    self_export: bool = False,
) -> None:
    where = f"record {record_index}"
    meta = chunk.metadata
    # Use wire strings: a local Windows Path has already replaced POSIX slashes
    # and cannot recover the original path flavour for cross-platform mapping.
    source = source_file
    original = _pure(source)
    if meta.scope == "user":
        if os.name != "nt" and PureWindowsPath(source).drive:
            raise invalid(
                where,
                "foreign user source path (on POSIX, manually convert source_file "
                "to a local absolute path; project_root_map only maps project records)",
            )
        if os.name == "nt" and isinstance(original, PurePosixPath) and original.is_absolute():
            raise invalid(
                where,
                "foreign user source path (on Windows, manually convert source_file "
                "to a local absolute path; project_root_map only maps project records)",
            )
        resolved = Path(norm_path(Path(source)))
        project_dirs = config.project_memory_dirs if config is not None else []
        registered_scope, _ = classify_scope(resolved, project_dirs)
        if version != "4":
            # The canonical user directory has the same spelling as a project
            # tree. Honor that known tier and configured user roots, but never
            # let a broad user root swallow an explicitly registered project.
            user_dirs = [Path.home() / ".memtomem/memories"]
            if config is not None:
                user_dirs.extend(config.memory_dirs)
            known_user = any(
                resolved.is_relative_to(Path(norm_path(Path(d).expanduser()))) for d in user_dirs
            )
            project_pattern = (
                classify_scope(source, None)[0] != "user"
                or classify_scope(resolved, None)[0] != "user"
            )
            if registered_scope != "user" or (project_pattern and not known_user):
                raise invalid(where, "legacy project source (re-export as v4)")
        # v4 declares its boundary explicitly. Match the indexer's registration
        # rule: an unregistered .memtomem directory is an ordinary user source.
        # Legacy records have already passed the stricter known-user check.
        unchanged_local_user = self_export and version == "4" and source == str(resolved)
        if registered_scope != "user" and not unchanged_local_user:
            raise invalid(where, "resolved source_file/scope (run mm index --force and re-export)")
        chunk.metadata = replace(meta, source_file=resolved)
        return
    if project_root is None or config is None:
        raise invalid(where, "registered project configuration required")
    old = _pure(project_root)
    if not old.is_absolute() or ".." in old.parts or ".." in original.parts:
        raise invalid(where, "project_root")
    try:
        relative = original.relative_to(old)
    except ValueError as exc:
        raise invalid(where, "source_file/project_root") from exc
    target_root = root_map.get(old)
    if target_root is None:
        foreign_root = (os.name == "nt" and isinstance(old, PurePosixPath)) or (
            os.name != "nt" and isinstance(old, PureWindowsPath)
        )
        if foreign_root or not Path(str(old)).is_dir():
            raise invalid(where, "project_root_map required")
        target_root = Path(str(old)).expanduser().resolve()
    mapped = (target_root.joinpath(*relative.parts)).resolve()
    scope, root = classify_scope(mapped, config.project_memory_dirs)
    # Derive the root from the same resolved registered directory as runtime context.
    registered = {
        Path(d).expanduser().resolve().parent.parent
        for d in config.project_memory_dirs
        if Path(d).expanduser().resolve().parent.name == ".memtomem"
    }
    if scope != meta.scope or root is None or root not in registered or root != target_root:
        raise invalid(
            where,
            "registered source_file/scope/project_root "
            "(register the destination or run mm index --force and re-export)",
        )
    chunk.metadata = replace(meta, source_file=mapped, project_root=root)


def enforce_size(text: str) -> None:
    if len(text.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise BundleSizeError("Memory bundle exceeds 100 MiB; split it using export filters.")

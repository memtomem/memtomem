"""Dedicated SQLite persistence for the optional LangGraph hybrid store.

This database never shares Core's schema. All methods run on its owning thread.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import sqlite_vec

from memtomem.search.fusion import weighted_rank_scores
from memtomem.storage.fts_tokenizer import tokenize_for_fts


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _namespace_order(left: str, right: str) -> int:
    if left == right:
        return 0
    a, b = tuple(json.loads(left)), tuple(json.loads(right))
    return (a > b) - (a < b)


def validate_filter(value: Any) -> None:
    if isinstance(value, dict):
        for key, operand in value.items():
            if key.startswith("$"):
                if key not in {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"}:
                    raise ValueError(f"Unsupported filter operator: {key}")
                if key in {"$in", "$nin"} and not isinstance(operand, list):
                    raise ValueError(f"{key} requires a list")
            else:
                validate_filter(operand)
    elif isinstance(value, list):
        for element in value:
            validate_filter(element)


def matches(actual: Any, expected: Any) -> bool:
    """Nested partial objects, exact arrays, and explicit comparison operators."""
    if isinstance(expected, dict):
        for key, operand in expected.items():
            if key.startswith("$"):
                try:
                    if key == "$eq":
                        ok = actual == operand
                    elif key == "$ne":
                        ok = actual != operand
                    elif key == "$in":
                        ok = actual in operand
                    elif key == "$nin":
                        ok = actual not in operand
                    elif key == "$gt":
                        ok = actual > operand
                    elif key == "$gte":
                        ok = actual >= operand
                    elif key == "$lt":
                        ok = actual < operand
                    elif key == "$lte":
                        ok = actual <= operand
                    else:
                        raise ValueError(f"Unsupported filter operator: {key}")
                except TypeError:
                    ok = False
                if not ok:
                    return False
            else:
                found = actual
                for segment in key.split("."):
                    found = found.get(segment) if isinstance(found, dict) else None
                if not matches(found, operand):
                    return False
        return True
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(matches(a, b) for a, b in zip(actual, expected, strict=True))
        )
    return actual == expected


class HybridDatabase:
    """Synchronous operations, with transaction ownership confined to this class."""

    def __init__(self, path: Path, configuration: dict[str, Any]):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.connection = sqlite3.connect(path, timeout=30)
        db = self.connection
        try:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if "hybrid_meta" not in tables:
                    raise ValueError("Refusing foreign database; choose a new HybridStore path")
                meta = dict(db.execute("SELECT key,value FROM hybrid_meta"))
                if meta != {
                    "kind": "memtomem-hybrid",
                    "version": "1",
                    "index": encode(configuration),
                }:
                    raise ValueError(
                        "Incompatible store schema/index; export/import to a new database"
                    )
            else:
                db.execute("CREATE TABLE hybrid_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.executemany(
                    "INSERT INTO hybrid_meta VALUES (?,?)",
                    [
                        ("kind", "memtomem-hybrid"),
                        ("version", "1"),
                        ("index", encode(configuration)),
                    ],
                )
                db.execute("""CREATE TABLE items(
                    id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, key TEXT NOT NULL,
                    value_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL, ttl REAL, expires_at REAL, index_json TEXT NOT NULL,
                    UNIQUE(namespace,key))""")
                db.execute("CREATE VIRTUAL TABLE item_fts USING fts5(text, tokenize='unicode61')")
                db.execute("""CREATE TABLE vectors(item_id INTEGER NOT NULL REFERENCES items(id)
                    ON DELETE CASCADE, ordinal INTEGER NOT NULL, vector BLOB NOT NULL,
                    PRIMARY KEY(item_id,ordinal))""")
                db.execute("CREATE INDEX item_expiry ON items(expires_at)")
            db.commit()
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.enable_load_extension(True)
            try:
                sqlite_vec.load(db)
            finally:
                db.enable_load_extension(False)
            db.create_function(
                "namespace_prefix",
                2,
                lambda ns, prefix: json.loads(ns)[: len(json.loads(prefix))] == json.loads(prefix),
                deterministic=True,
            )
            db.create_function(
                "value_matches",
                2,
                lambda value, filters: matches(json.loads(value), json.loads(filters)),
                deterministic=True,
            )
            db.create_collation("namespace_order", _namespace_order)
        except BaseException:
            db.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def write(self, records: list[dict[str, Any]], *, importing: bool = False) -> None:
        db = self.connection
        with db:
            db.execute("BEGIN IMMEDIATE")
            for record in records:
                ns, key = encode(record["namespace"]), record["key"]
                old = db.execute(
                    "SELECT * FROM items WHERE namespace=? AND key=?", (ns, key)
                ).fetchone()
                if importing and old is not None:
                    raise ValueError(
                        f"Import identity already exists: {record['namespace']!r}/{key}"
                    )
                if old is not None:
                    db.execute("DELETE FROM item_fts WHERE rowid=?", (old["id"],))
                    db.execute("DELETE FROM items WHERE id=?", (old["id"],))
                if record["value"] is None:
                    continue
                now = time.time()
                live = old is not None and (old["expires_at"] is None or old["expires_at"] > now)
                ttl = record["ttl"]
                created = record.get("created_at", old["created_at"] if live else now)
                updated = record.get("updated_at", now)
                expires = record.get("expires_at", None if ttl is None else now + ttl * 60)
                revision = record.get("revision", old["revision"] + 1 if live else 1)
                cursor = db.execute(
                    """INSERT INTO items(namespace,key,value_json,created_at,updated_at,
                    revision,ttl,expires_at,index_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        ns,
                        key,
                        encode(record["value"]),
                        created,
                        updated,
                        revision,
                        ttl,
                        expires,
                        encode(record["index"]),
                    ),
                )
                identity = cursor.lastrowid
                if record["index"] is not False:
                    db.execute(
                        "INSERT INTO item_fts(rowid,text) VALUES (?,?)", (identity, record["text"])
                    )
                    db.executemany(
                        "INSERT INTO vectors VALUES (?,?,?)",
                        [
                            (identity, i, sqlite_vec.serialize_float32(vector))
                            for i, vector in enumerate(record["vectors"])
                        ],
                    )

    def _refresh(self, rows: list[sqlite3.Row], now: float) -> None:
        self.connection.executemany(
            "UPDATE items SET expires_at=? + ttl*60 WHERE id=? AND ttl IS NOT NULL",
            [(now, row["id"]) for row in rows],
        )

    def get(self, namespace: tuple[str, ...], key: str, refresh: bool) -> dict | None:
        db = self.connection
        with db:
            db.execute("BEGIN IMMEDIATE" if refresh else "BEGIN")
            now = time.time()
            row = db.execute(
                """SELECT * FROM items WHERE namespace=? AND key=?
                AND (expires_at IS NULL OR expires_at>?)""",
                (encode(namespace), key, now),
            ).fetchone()
            if refresh and row is not None:
                self._refresh([row], now)
            return dict(row) if row is not None else None

    def search(
        self,
        namespace: tuple[str, ...],
        filters: dict | None,
        query: str | None,
        vector: list[float] | None,
        mode: str,
        limit: int,
        offset: int,
        weights: list[float],
        k: int,
        refresh: bool,
    ) -> tuple[list[dict], dict]:
        validate_filter(filters)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be nonnegative")
        db = self.connection
        where = (
            "namespace_prefix(i.namespace,?) AND value_matches(i.value_json,?) "
            "AND (i.expires_at IS NULL OR i.expires_at>?)"
        )
        counts = {"bm25": 0, "dense": 0}
        with db:
            db.execute("BEGIN IMMEDIATE" if refresh else "BEGIN")
            now = time.time()
            args = (encode(namespace), encode(filters or {}), now)
            if query is None:
                rows = db.execute(
                    f"""SELECT i.*,NULL AS score FROM items i WHERE {where}
                    ORDER BY updated_at DESC,namespace COLLATE namespace_order,key
                    LIMIT ? OFFSET ?""",
                    (*args, limit, offset),
                ).fetchall()
                scale = None
            else:
                count = max(100, offset + limit)
                lexical, dense = [], []
                fts = tokenize_for_fts(query, for_query=True, use_or=True, tokenizer="unicode61")
                if (
                    mode in {"bm25", "hybrid"}
                    and (mode != "hybrid" or weights[0] > 0)
                    and fts.strip()
                ):
                    lexical = db.execute(
                        f"""SELECT i.id,-bm25(item_fts) AS score FROM item_fts
                        JOIN items i ON i.id=item_fts.rowid
                        WHERE item_fts MATCH ? AND {where}
                        ORDER BY score DESC,i.namespace COLLATE namespace_order,i.key LIMIT ?""",
                        (fts, *args, count),
                    ).fetchall()
                if (
                    mode in {"dense", "hybrid"}
                    and (mode != "hybrid" or weights[1] > 0)
                    and vector is not None
                ):
                    dense = db.execute(
                        f"""SELECT i.id,MAX(1-vec_distance_cosine(v.vector,?)) AS score
                        FROM items i JOIN vectors v ON v.item_id=i.id
                        WHERE {where} GROUP BY i.id
                        ORDER BY score DESC,i.namespace COLLATE namespace_order,i.key LIMIT ?""",
                        (sqlite_vec.serialize_float32(vector), *args, count),
                    ).fetchall()
                counts = {"bm25": len(lexical), "dense": len(dense)}
                if mode == "hybrid":
                    scores = weighted_rank_scores(
                        [[r["id"] for r in leg] for leg in [lexical, dense]], weights, k
                    )
                    scale = "weighted_rrf"
                else:
                    scores = {r["id"]: r["score"] for r in lexical + dense}
                    scale = "bm25" if mode == "bm25" else "cosine_similarity"
                # Hydration is inside the same snapshot as candidate selection.
                rows = []
                for identity, score in scores.items():
                    row = dict(db.execute("SELECT * FROM items WHERE id=?", (identity,)).fetchone())
                    row["score"] = score
                    rows.append(row)
                rows.sort(
                    key=lambda row: (-row["score"], tuple(json.loads(row["namespace"])), row["key"])
                )
                rows = rows[offset : offset + limit]
            if refresh:
                self._refresh(rows, now)
            return [dict(row) for row in rows], {
                "effective_mode": mode if query is not None else "browse",
                "score_scale": scale,
                "candidate_counts": counts,
            }

    def namespaces(self) -> set[tuple[str, ...]]:
        return {
            tuple(json.loads(row[0]))
            for row in self.connection.execute(
                "SELECT DISTINCT namespace FROM items WHERE expires_at IS NULL OR expires_at>?",
                (time.time(),),
            )
        }

    def export(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM items WHERE expires_at IS NULL OR expires_at>?
                ORDER BY namespace COLLATE namespace_order,key""",
                (time.time(),),
            )
        ]

    def sweep(self) -> int:
        db = self.connection
        with db:
            db.execute("BEGIN IMMEDIATE")
            expired = [
                r[0] for r in db.execute("SELECT id FROM items WHERE expires_at<=?", (time.time(),))
            ]
            db.executemany("DELETE FROM item_fts WHERE rowid=?", [(i,) for i in expired])
            db.executemany("DELETE FROM items WHERE id=?", [(i,) for i in expired])
            return len(expired)

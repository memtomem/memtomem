"""Freeze a private local transition snapshot; never mutate the source database.

The snapshot is an evaluation artifact, not a production replacement. Content
and query text must remain outside Git. Copy failures/drift block acceptance.
"""

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def capture(database, config, output, before_python=None):
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=False)
    database = database.expanduser().resolve()
    config = config.expanduser().resolve()
    raw_config = config.read_bytes()
    (output / "original-config.json").write_bytes(raw_config)
    if before_python:
        program = (
            "import json,sys; from pathlib import Path; "
            "from memtomem.config import _override_path; "
            "from memtomem.config_signature import build_fresh_config; "
            "assert _override_path().resolve() == Path(sys.argv[1]).resolve(), 'Config path mismatch'; "
            "print(json.dumps(build_fresh_config(migrate=False).model_dump(mode='json')))"
        )
        effective = subprocess.check_output([before_python, "-c", program, str(config)])
        json.loads(effective)
        (output / "installed-effective-config.json").write_bytes(effective)

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(output / "original.db")) as dest:
            src.backup(dest)
    sources = []
    with closing(sqlite3.connect((output / "original.db").as_uri() + "?mode=ro", uri=True)) as db:
        paths = db.execute(
            "SELECT source_file, count(*) FROM chunks GROUP BY source_file ORDER BY source_file"
        ).fetchall()
        for source, count in paths:
            path = Path(source)
            record = {"source": source, "old_chunks": count, "id": sha(source.encode())}
            try:
                before = path.stat()
                raw = path.read_bytes()
                after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise RuntimeError("source changed during capture")
                relative = Path("sources") / record["id"] / path.name
                copy = output / relative
                copy.parent.mkdir(parents=True)
                copy.write_bytes(raw)
                record.update(copy=str(relative), bytes=len(raw), sha256=sha(raw), status="copied")
            except (OSError, RuntimeError) as exc:
                record.update(status="error", error=type(exc).__name__)
            sources.append(record)
        table_names = [
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        tracked = [
            "chunks",
            "namespace_metadata",
            "chunk_links",
            "chunk_entities",
            "chunk_relations",
            "query_history",
            "eval_cases",
            "eval_case_labels",
            "sessions",
            "session_events",
            "memory_assertions",
        ]
        counts = {
            table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in tracked
            if table in table_names
        }
        frequencies = Counter(
            text.strip()
            for (text,) in db.execute("SELECT query_text FROM query_history")
            if text and text.strip()
        )
        selected = []
        for lang, size in (("ko", 40), ("en", 40), ("mixed-script", 20)):

            def language(text):
                ko = bool(re.search("[가-힣]", text))
                en = bool(re.search("[A-Za-z]", text))
                return "mixed-script" if ko and en else "ko" if ko else "en" if en else "other"

            pool = [text for text in frequencies if language(text) == lang]
            pool.sort(key=lambda text: (-frequencies[text], sha(text.encode())))
            if len(pool) < size:
                raise ValueError(f"Insufficient {lang} queries")
            selected.extend(
                {
                    "id": sha(text.encode()),
                    "text": text,
                    "lang": lang,
                    "frequency": frequencies[text],
                    "qrels": {},
                    "judgment_status": "unjudged",
                }
                for text in pool[:size]
            )
        # Balanced initial shortlist, subject to the user's importance review.
        critical = set()
        for lang, size in (("ko", 8), ("en", 8), ("mixed-script", 4)):
            critical.update(
                row["id"]
                for row in selected
                if row["lang"] == lang
                and len([r for r in selected[: selected.index(row)] if r["lang"] == lang]) < size
            )
        for row in selected:
            row["critical_candidate"] = row["id"] in critical
        (output / "queries.json").write_text(
            json.dumps(
                {"status": "UNJUDGED_USER_REVIEW_REQUIRED", "queries": selected},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    manifest = {
        "status": "CAPTURED_NOT_ACCEPTED",
        "database": str(database),
        "config_sha256": sha(raw_config),
        "counts": counts,
        "sources": sources,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "sources": len(sources),
                "bytes": sum(s.get("bytes", 0) for s in sources),
                "errors": sum(s["status"] != "copied" for s in sources),
                "counts": counts,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--before-python", help="Installed Python to capture its effective config read-only"
    )
    args = parser.parse_args()
    capture(args.database, args.config, args.output.resolve(), args.before_python)

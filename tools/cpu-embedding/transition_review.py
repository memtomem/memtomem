"""Prepare private query candidates for human review; never invent qrels.

Frequency is a sampling signal, not proof of importance or relevance. BM25
candidates are explicitly unjudged and cannot authorize a model transition.
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


def language(text):
    ko, en = bool(re.search("[가-힣]", text)), bool(re.search("[A-Za-z]", text))
    return "mixed-script" if ko and en else "ko" if ko else "en" if en else "other"


def prepare(root):
    os.umask(0o077)
    manifest = json.loads((root / "manifest.json").read_text())
    copies = {row["source"]: row for row in manifest["sources"]}
    preflight = json.loads((root / "preflight.json").read_text())
    accepted = {row["source"] for row in preflight["sources"] if row["status"] == "ready"}
    with closing(sqlite3.connect((root / "original.db").as_uri() + "?mode=ro", uri=True)) as db:
        frequencies = Counter(
            text.strip()
            for (text,) in db.execute("SELECT query_text FROM query_history")
            if text and text.strip()
        )
        queries = []
        for lang, count, critical_count in [("ko", 40, 8), ("en", 40, 8), ("mixed-script", 20, 4)]:
            pool = sorted(
                (text for text in frequencies if language(text) == lang),
                key=lambda text: (-frequencies[text], hashlib.sha256(text.encode()).hexdigest()),
            )
            paths = [
                text
                for text in pool
                if re.match(r"^(?:/?Users[ /]|/?home[ /]|[A-Za-z]:[\\/])", text)
            ]
            semantic = [text for text in pool if text not in set(paths)]
            selected = paths[: count // 4] + semantic[: count - min(len(paths), count // 4)]
            if len(selected) != count:
                raise ValueError(f"Insufficient {lang} queries")
            critical = set(semantic[:critical_count])
            for text in selected:
                terms = list(dict.fromkeys(re.findall(r"[\w]+", text)))[:24]
                match = " OR ".join('"' + term + '"' for term in terms)
                rows = (
                    db.execute(
                        "SELECT c.id,c.source_file,c.start_line,c.end_line FROM chunks_fts JOIN chunks c ON c.rowid=chunks_fts.rowid WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 50",
                        (match,),
                    ).fetchall()
                    if match
                    else []
                )
                candidates, seen = [], set()
                for identifier, source, start, end in rows:
                    if source not in accepted or source in seen:
                        continue
                    seen.add(source)
                    candidates.append(
                        {
                            "chunk_id": identifier,
                            "source": source,
                            "snapshot": copies[source]["copy"],
                            "stored_start_line": start,
                            "stored_end_line": end,
                            "judgment": None,
                        }
                    )
                    if len(candidates) == 3:
                        break
                queries.append(
                    {
                        "id": hashlib.sha256(text.encode()).hexdigest(),
                        "text": text,
                        "lang": lang,
                        "frequency": frequencies[text],
                        "critical_candidate": text in critical,
                        "qrels": {},
                        "candidates": candidates,
                    }
                )
    (root / "queries.json").write_text(
        json.dumps(
            {
                "status": "UNJUDGED_USER_REVIEW_REQUIRED",
                "sampling": "frequency within language; generated-path queries capped at one quarter; critical candidates are non-path queries",
                "queries": queries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    lines = [
        "# 중요 질의 20건 검토 초안",
        "",
        "아래 후보는 BM25로 찾은 미판정 문서입니다. 정답·중요도가 확정되지 않았으며 전환 품질 통과 근거로 사용하지 않습니다.",
        "각 항목에 중요 여부와 실제 기대 문서·근거 구간을 확인해 주세요. 관련 문서가 없다면 ‘정답 없음’, 판단할 수 없다면 ‘미판정’으로 남깁니다.",
        "원본 위치가 달라졌을 수 있어 링크는 동결한 원본 복사본을 가리킵니다. 이후 양 모델의 검색 후보도 합쳐 평가합니다.",
        "",
    ]
    for number, query in enumerate((q for q in queries if q["critical_candidate"]), 1):
        lines.extend(
            [
                f"## {number}. {query['text']}",
                "",
                f"언어: {query['lang']} · 기록 횟수: {query['frequency']} · 중요 여부: 미확정 · 정답: 미판정",
                "",
            ]
        )
        for candidate in query["candidates"]:
            lines.append(
                f"- 후보: [{Path(candidate['source']).name}]({candidate['snapshot']}) — 기존 인덱스 {candidate['stored_start_line']}–{candidate['stored_end_line']}행"
            )
        if not query["candidates"]:
            lines.append("- 후보 없음: 자동으로 ‘정답 없음’ 처리하지 않습니다.")
        lines.extend(["", "검토: ", ""])
    (root / "critical-query-review.md").write_text("\n".join(lines))
    print(
        json.dumps(
            {
                "queries": len(queries),
                "critical_candidates": sum(q["critical_candidate"] for q in queries),
                "qrels": 0,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    prepare(parser.parse_args().snapshot.resolve())

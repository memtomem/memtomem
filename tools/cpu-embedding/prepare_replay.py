"""Freeze actual query replay without treating past rankings as ground truth.

Selects 40 Hangul-only, 40 English-script, 20 mixed-script queries. Mixed-script
is NOT a cross-language relevance label. No Recall/nDCG/MRR claims are possible
without independent judgments. All output is private and stays local.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from memtomem.chunking.bounded import TokenBudget
from memtomem.config import Mem2MemConfig, EmbeddingConfig

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--history-backup", type=Path, required=True)
parser.add_argument("--index-clone", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
os.umask(0o077)
budget = TokenBudget(Mem2MemConfig(embedding=EmbeddingConfig(provider="onnx")).indexing)
queries = []
groups = {"ko": [], "en": [], "mixed-script": []}
with sqlite3.connect(args.history_backup.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
    texts = {row[0] for row in db.execute("SELECT query_text FROM query_history") if row[0]}
for text in texts:
    if budget.count("query: " + text, special=True) > 512:
        continue
    ko = bool(re.search("[가-힣]", text))
    en = bool(re.search("[A-Za-z]", text))
    lang = "mixed-script" if ko and en else "ko" if ko else "en"
    groups[lang].append(text)
for lang, count in [("ko", 40), ("en", 40), ("mixed-script", 20)]:
    texts = sorted(groups[lang], key=lambda t: hashlib.sha256(t.encode()).hexdigest())[:count]
    if len(texts) != count:
        raise ValueError(f"Insufficient {lang} history queries: {len(texts)}")
    queries.extend(
        {"id": hashlib.sha256(t.encode()).hexdigest(), "text": t, "lang": lang, "qrels": {}}
        for t in texts
    )
with sqlite3.connect(args.index_clone.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
    documents = [
        {"id": row[0], "text": row[1]}
        for row in db.execute("SELECT id, content FROM chunks ORDER BY id")
    ]
if any(budget.count(d["text"], special=True) > 512 for d in documents):
    raise ValueError("Clone is not bounded for matched replay")
args.output.write_text(
    json.dumps(
        {
            "dataset": "private-actual-query-replay",
            "evaluation": "latency-only-unjudged",
            "documents": documents,
            "queries": queries,
        },
        ensure_ascii=False,
    )
    + "\n"
)
print(
    f"Frozen {len(queries)} unjudged queries and {len(documents)} cloned chunk bodies; no quality labels inferred"
)

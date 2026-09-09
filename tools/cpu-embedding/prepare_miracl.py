"""Freeze an explicit MIRACL dev subset from official local TSV/JSONL exports.

Provide per-language topics.tsv, qrels.tsv, corpus.jsonl under --input/en and
--input/ko. corpus rows must have docid/title/text. Missing judged passages fail.
This reports a judged-pool subset, never the full MIRACL benchmark score.
"""

import argparse
import hashlib
import json
from pathlib import Path
from memtomem.chunking.bounded import TokenBudget
from memtomem.config import Mem2MemConfig, EmbeddingConfig

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--queries-per-language", type=int, default=200)
parser.add_argument("--distractors", type=int, default=1000)
parser.add_argument(
    "--positive-qrels-only",
    action="store_true",
    help="Explicit MTEB candidate-pool mode: require all positives; report omitted negatives",
)
args = parser.parse_args()
budget = TokenBudget(Mem2MemConfig(embedding=EmbeddingConfig(provider="onnx")).indexing)
documents, queries, inputs = [], [], {}
omitted_negatives = {}
for lang in ("en", "ko"):
    directory = args.input / lang
    paths = [directory / name for name in ("topics.tsv", "qrels.tsv", "corpus.jsonl")]
    for path in paths:
        with path.open("rb") as stream:
            inputs[f"{lang}/{path.name}"] = hashlib.file_digest(stream, "sha256").hexdigest()
    topics = dict(line.rstrip("\n").split("\t", 1) for line in paths[0].read_text().splitlines())
    selected = sorted(topics, key=lambda key: hashlib.sha256(key.encode()).hexdigest())[
        : args.queries_per_language
    ]
    if len(selected) != args.queries_per_language:
        raise ValueError("Insufficient queries")
    qrels = {key: {} for key in selected}
    needed = set()
    for line in paths[1].read_text().splitlines():
        qid, _, docid, grade = line.split()
        if qid in qrels:
            needed.add(docid)
            if int(grade) > 0:
                qrels[qid][lang + ":" + docid] = int(grade)
    required = {docid.split(":", 1)[1] for rel in qrels.values() for docid in rel}
    found = set()
    distractors = []

    def append_document(row):
        text = row.get("title", "") + "\n\n" + row["text"]
        for start, end in budget.spans(text):
            fragment = text[start:end]
            assert budget.count(fragment, special=True) <= 512
            documents.append({"id": lang + ":" + row["docid"], "text": fragment})

    with paths[2].open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["docid"] in needed:
                found.add(row["docid"])
                append_document(row)
            else:
                key = hashlib.sha256(row["docid"].encode()).hexdigest()
                distractors.append((key, row))
                if len(distractors) > args.distractors * 2:
                    distractors = sorted(distractors, key=lambda x: x[0])[: args.distractors]
    missing = needed - found
    if required - found or (missing and not args.positive_qrels_only):
        raise ValueError(
            f"{lang}: {len(missing)} judged passages missing ({len(required - found)} positive)"
        )
    omitted_negatives[lang] = len(missing)
    for _, row in sorted(distractors, key=lambda x: x[0])[: args.distractors]:
        append_document(row)
    queries.extend(
        {"id": lang + ":" + qid, "text": topics[qid], "lang": lang, "qrels": qrels[qid]}
        for qid in selected
    )
args.output.write_text(
    json.dumps(
        {
            "dataset": "MIRACL-dev-MTEB-candidate-pool-subset"
            if args.positive_qrels_only
            else "MIRACL-dev-judged-pool-subset",
            "omitted_negative_judgments": omitted_negatives,
            "input_sha256": inputs,
            "documents": documents,
            "queries": queries,
        },
        ensure_ascii=False,
    )
    + "\n"
)

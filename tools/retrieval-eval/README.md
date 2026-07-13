# Public synthetic retrieval evaluation

This directory contains the reproducible retrieval-quality benchmark used by
memtomem CI. Its committed corpus is synthetic and public: it contains no
customer records, user memories, private conversations, or internal company
documents.

## Dataset

- 48 Markdown files and 192 heading chunks
- English and Korean
- Six technical topics and four document genres
- 100 queries across direct, paraphrase, underspecified, multi-topic,
  negation, and genre-primary types

See the corpus [provenance and privacy contract](../../packages/memtomem/tests/fixtures/corpus_v2/README.md)
and [generation specification](../../packages/memtomem/tests/fixtures/corpus_v2/GENERATION.md).

## Reproduce

```bash
uv run python tools/retrieval-eval/audit_public_corpus.py

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/calibrate_portfolio.py --runs 3 --json

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/check_baseline.py --runs 1
```

The committed `baseline_v0.3.8.json` was calibrated over 10 deterministic
runs. It records the corpus/query hashes, package and model configuration,
an embedding behavior fingerprint, per-query metrics, aggregate floors,
index cost, and search latency. The default CI gate checks corpus hashes and
quality floors. Fingerprint and performance checks are opt-in because those
values can differ across CPU architecture and runner load.

## Metrics

- Recall@10 and MRR@10 use primary-tag binary relevance.
- nDCG@10 uses primary `1.0` and secondary `0.5` graded relevance.
- Per-language/query-type floor = `round(10-run mean * 0.9, 2)`.
- A calibration run is invalid unless all 48 files and 192 chunks index with
  zero privacy blocks and zero errors.

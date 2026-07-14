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

Methodology v2 adds a frozen 120-query bilingual holdout (60 matched
English/Korean intent pairs) without replacing the original 100-query
regression portfolio.

See the corpus [provenance and privacy contract](../../packages/memtomem/tests/fixtures/corpus_v2/README.md)
and [generation specification](../../packages/memtomem/tests/fixtures/corpus_v2/GENERATION.md).

## Reproduce

```bash
uv run python tools/retrieval-eval/audit_public_corpus.py

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/calibrate_portfolio.py --runs 3 --json

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/check_baseline.py --runs 1

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/check_baseline_v2.py

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/compare_models_v2.py \
  --reranker-pool 20 --output tools/retrieval-eval/model_comparison_v2.json

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/sweep_k_v2.py --runs 1 --stage all \
  --output tools/retrieval-eval/k_sweep_v2.json
```

Run an individual stage only for investigation and write it outside the
committed all-stage artifact, for example:

```bash
PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/sweep_k_v2.py --runs 1 --stage rrf \
  --output /tmp/k_sweep_rrf_v2.json
```

The committed `baseline_v0.3.8.json` was calibrated over 10 deterministic
runs. It records the corpus/query hashes, package and model configuration,
an embedding behavior fingerprint, per-query metrics, aggregate floors,
index cost, and search latency. The default CI gate checks corpus hashes and
quality floors. Fingerprint and performance checks are opt-in because those
values can differ across CPU architecture and runner load.

The v2 gate deliberately separates model and language effects:

- English track: English-only corpus and `BAAI/bge-small-en-v1.5`
- Korean track: Korean-only corpus and
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- Cross-language track: combined corpus and the same multilingual model

`baseline_v2.json` pins the query, qrel, and corpus hashes; explicit portable
qrels; per-track model identity; variance-aware quality floors and ceilings;
and zero-hit caps. It also records the preregistered RRF grid result. No candidate met all
weak-slice improvement and no-regression gates, so the product default remains
the balanced `[1.0, 1.0]` BM25/dense weighting.

## Metrics

- Recall@10 and MRR@10 use primary-tag binary relevance.
- nDCG@10 uses primary `1.0` and secondary `0.5` graded relevance.
- The original 100-query portfolio (`baseline_v0.3.8.json`) sets each
  per-language/query-type floor to `round(10-run mean * 0.85, 2)`. The 15%
  margin covers observed cross-platform ONNX numeric variation while retaining
  a blocking threshold for material retrieval regressions.
- The v2 holdout (`baseline_v2.json`) instead sets each higher-is-better floor
  to `max(0, round(10-run mean * 0.90 - metric_spread, 6))`. Lower-is-better
  `hard_negative_hits@10` uses the ceiling
  `round(10-run mean * 1.10 + metric_spread, 6)`. Each `metric_spread` is that
  metric's observed maximum minus minimum across the calibration runs, so a
  volatile slice does not weaken stable metrics. These bounds cover the
  observed calibration variance; they do not guarantee all future hardware or
  tie-break behavior.
- A calibration run is invalid unless all 48 files and 192 chunks index with
  zero privacy blocks and zero errors.

Methodology v2 keeps those topic metrics and adds intent-specific checks:

- `genre_hit@1` and genre MRR use `topic AND expected genre` qrels.
- `constraint_success@10` requires a negation/contrast ADR result to rank
  before same-topic hard negatives.
- `intent_coverage@10` requires multi-topic queries to cover both intents.
- Same-language precision and cross-language relevant hits are reported only
  in the combined-corpus track.

## RRF sensitivity correctness

Per-call RRF weights are part of the search cache key. The sensitivity tool
also clears the cache between BM25-only and dense-only probes. Historical
`0/8 divergence` observations were produced before that isolation and must not
be used as evidence that the two retrievers return identical rankings.

## BGE-M3 and reranker experiment

`model_comparison_v2.json` records a one-run, 120-query comparison of four
profiles: language-specific embeddings, those embeddings plus the multilingual
Jina reranker, BGE-M3, and BGE-M3 plus the reranker. The reranker receives the
top 20 fused candidates.

The full verification record includes the measured Mac hardware, software
versions, commands, pass/fail evidence, result tables, and limitations:
[`MODEL_COMPARISON_REPORT.md`](./MODEL_COMPARISON_REPORT.md).

- BGE-M3 vs language-specific embeddings improved Korean macro Recall/MRR/nDCG
  by `+0.128/+0.110/+0.131` and cross-language by
  `+0.089/+0.099/+0.102`. English changed only
  `+0.004/+0.012/-0.001`.
- Adding the reranker to the language-specific profile improved English by
  `+0.042/+0.032/+0.038`, Korean by `+0.091/+0.190/+0.147`, and
  cross-language by `+0.093/+0.113/+0.101` for Recall/MRR/nDCG.
- BGE-M3 increased non-reranked p95 from roughly `4 ms` to `24-25 ms`.
  The multilingual reranker raised CPU p95 to roughly `0.7 s` for English and
  `1.0 s` for Korean/cross-language.

These results support retaining the small English model for English-only use,
considering BGE-M3 for Korean/cross-language quality profiles, and keeping the
reranker opt-in where its latency and ~1.1 GB model cost are acceptable.

## Staged k-sweep

`k_sweep_v2.json` is a one-run, 19-profile candidate-reduction experiment over
three stages: RRF `k`, candidate width at `top_k=5/10/20`, and reranker pool
size for language-specific and BGE-M3 embeddings. Selection requires the
quality gates to pass, then maximizes the Korean plus cross-language
Recall/nDCG gain; ties prefer the lower maximum p95.

The recorded selections keep `rrf_k=60`; keep candidate width `50` for
`top_k=10` and `top_k=20`; and keep reranker pool `20` for both embedding
families. Candidate width `100` at `top_k=5` is an eligible follow-up candidate,
not evidence for a global default change. Product defaults therefore remain
`top_k=10`, BM25/dense candidates `50/50`, `rrf_k=60`, and reranking disabled.

The experiment is a single-run screening pass. It does not record run spread,
environment/corpus hashes, cold-vs-warm state, model-load time, RSS, or disk
cost, and it does not compare reranker enabled versus disabled. See
[`K_SWEEP_REPORT.md`](./K_SWEEP_REPORT.md) for the gates, rejection reasons,
metrics, and limitations, and [`k_sweep_v2.json`](./k_sweep_v2.json) for the
raw artifact.

# Retrieval model and reranker comparison report

Verified: 2026-07-13 (Asia/Seoul)

This document records a comparison of embedding models and of retrieval with
and without a reranker, evaluating English, Korean, and cross-language search as
separate tracks. Raw results are preserved in
[`model_comparison_v2.json`](./model_comparison_v2.json).

## Measurement environment

The machine was identified with `system_profiler`, `sw_vers`, and `uname`. It is
reported by the operating system as an **Apple M4 Max** — not the M3 the user
had assumed. Serial number, hardware UUID, and user name are not needed for
reproduction and were not recorded.

| Item | Value |
| --- | --- |
| Product | MacBook Pro |
| Model identifier | Mac16,5 |
| Chip | Apple M4 Max |
| CPU | 16 cores (12 performance, 4 efficiency) |
| Memory | 64 GB |
| Architecture | arm64 |
| OS | macOS 26.5.2 (build 25F84) |
| Kernel | Darwin 25.5.0 |
| Python | 3.13.2 |
| uv | 0.11.16 |
| fastembed | 0.8.0 |
| Base Git commit | `b73a7a74` |

Latencies are CPU-execution results on this machine. They will vary with a
different chip, thread state, power mode, or model-cache state, so they must be
read separately from the quality metrics.

## Verification design

- Uses the public synthetic corpus: 48 Markdown files, 192 chunks.
- The frozen 120 queries are 60 same-intent English/Korean pairs.
- The English track uses the English corpus and the 60 English queries.
- The Korean track uses the Korean corpus and the 60 Korean queries.
- The cross-language track uses the combined English/Korean corpus and all 120
  queries.
- Each language evaluates an equal count of direct, paraphrase, underspecified,
  multi-topic, negation, and genre-primary query types.
- Every search uses `top_k=10` and BM25/dense RRF weights `[1.0, 1.0]`.
- When a reranker is applied, it re-ranks the top 20 fused results.
- Latency measures the search-pipeline call, excluding indexing and component
  creation. When the reranker is enabled, re-ranking time is included.
- This model comparison is a single run per profile. It is an experiment to
  confirm quality direction and large cost differences, and is not used as a
  precise performance figure.

## Compared profiles

| Profile | English | Korean / cross-language | Reranker |
| --- | --- | --- | --- |
| Language-specific baseline | `BAAI/bge-small-en-v1.5` (384) | `paraphrase-multilingual-MiniLM-L12-v2` (384) | none |
| Language-specific + reranker | same as above | same as above | `jina-reranker-v2-base-multilingual`, pool 20 |
| BGE-M3 | `BAAI/bge-m3` (1024) | `BAAI/bge-m3` (1024) | none |
| BGE-M3 + reranker | `BAAI/bge-m3` (1024) | `BAAI/bge-m3` (1024) | `jina-reranker-v2-base-multilingual`, pool 20 |

## Run procedure

The corpus, existing baseline, model comparison, and implementation regression
were checked in the following order.

```bash
uv run python tools/retrieval-eval/audit_public_corpus.py

uv run python tools/retrieval-eval/check_baseline_v2.py --runs 1

uv run python tools/retrieval-eval/compare_models_v2.py \
  --runs 1 \
  --reranker-pool 20 \
  --output tools/retrieval-eval/model_comparison_v2.json

uv run ruff check packages/memtomem/src packages/memtomem/tests tools
uv run ruff format --check packages/memtomem/src packages/memtomem/tests tools

uv run pytest \
  packages/memtomem/tests/test_retrieval_benchmark_v2.py \
  packages/memtomem/tests/test_pipeline.py -q

jq -e \
  '(.schema_version == 1) and (.queries == 120) and
   (.profiles | length == 4) and (.deltas | length == 3)' \
  tools/retrieval-eval/model_comparison_v2.json

git diff --check
```

## Results

The values below are macro metrics, re-averaged across the per-language,
per-query-type scores.

| Profile | Track | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Language-specific baseline | English | 0.634028 | 0.605159 | 0.571666 | 9 | 4.258 ms |
| Language-specific baseline | Korean | 0.530178 | 0.493095 | 0.429411 | 9 | 4.190 ms |
| Language-specific baseline | Cross-language | 0.355631 | 0.542840 | 0.388166 | 17 | 4.267 ms |
| Language-specific + reranker | English | 0.675794 | 0.637361 | 0.609695 | 6 | 694.513 ms |
| Language-specific + reranker | Korean | 0.620952 | 0.683056 | 0.576549 | 6 | 1001.449 ms |
| Language-specific + reranker | Cross-language | 0.448738 | 0.656038 | 0.489623 | 9 | 989.281 ms |
| BGE-M3 | English | 0.637897 | 0.617268 | 0.570713 | 9 | 23.505 ms |
| BGE-M3 | Korean | 0.657778 | 0.602976 | 0.560427 | 7 | 25.399 ms |
| BGE-M3 | Cross-language | 0.444737 | 0.641710 | 0.490313 | 15 | 23.813 ms |
| BGE-M3 + reranker | English | 0.677182 | 0.628214 | 0.604097 | 7 | 708.548 ms |
| BGE-M3 + reranker | Korean | 0.725040 | 0.677844 | 0.632294 | 5 | 1009.115 ms |
| BGE-M3 + reranker | Cross-language | 0.531227 | 0.669835 | 0.553692 | 10 | 1008.314 ms |

### BGE-M3 effect

Change relative to the language-specific baseline models.

| Track | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p95 increase |
| --- | ---: | ---: | ---: | ---: | ---: |
| English | +0.003869 | +0.012109 | -0.000953 | 0 | +19.247 ms |
| Korean | +0.127600 | +0.109881 | +0.131016 | -2 | +21.209 ms |
| Cross-language | +0.089106 | +0.098870 | +0.102147 | -2 | +19.546 ms |

For English the quality gain is negligible, but for Korean and cross-language
all three metrics improve substantially. On this machine the non-rerank p95 rose
from about 4 ms to 24–25 ms.

### Reranker effect

Change from adding the reranker to each language-specific baseline embedding.

| Track | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p95 increase |
| --- | ---: | ---: | ---: | ---: | ---: |
| English | +0.041766 | +0.032202 | +0.038029 | -3 | +690.255 ms |
| Korean | +0.090774 | +0.189961 | +0.147138 | -3 | +997.259 ms |
| Cross-language | +0.093107 | +0.113198 | +0.101457 | -8 | +985.014 ms |

The reranker improved every track, especially Korean MRR/nDCG and cross-language
zero-hit. In exchange, CPU p95 became about 0.7 s for English and about 1 s for
Korean and cross-language — too costly to always apply on the default search
path.

## Verification verdict

- Public corpus audit: 48 files, 192 chunks indexed completely, with no
  sensitive-information hits.
- retrieval v2 baseline: passed across all 120 English, Korean, and
  cross-language queries.
- Model comparison artifact: contains 4 profiles and 3 comparison deltas, and
  passed JSON-structure validation.
- Targeted regression tests: **65 passed**.
- Ruff check and format check: passed.
- `git diff --check`: passed.
- The broader non-LLM suite stopped in an earlier run at about the 32% mark with
  a process `SIGTRAP` (exit 133) rather than a test assertion, so it is not
  recorded as a full-suite pass.
- mypy reported 14 errors in pre-existing files unrelated to this change, so it
  is not included in this result's pass criteria.

## Recommendations

1. Keep the small English model for the English-only default profile. BGE-M3's
   English quality gain is marginal and only adds latency.
2. Consider BGE-M3 first for Korean and cross-language quality profiles. It
   delivered meaningful quality improvement for about a 20 ms p95 cost.
3. Offer the reranker as opt-in for a high-quality mode or async processing.
   Applying it on the real-time default path requires separately validating a
   smaller candidate pool, conditional reranking, and hardware acceleration.
4. Before changing an operational default, repeat 5–10 times on the same machine
   and additionally measure run-to-run variance, memory use, and cold-start time
   beyond p50/p95.

## Scope of this PR and follow-up work

This PR delivers the RRF cache-correctness fix, the language-separated
evaluation methodology, reproducible comparison tools, the current one-run
experiment results, and this verification document. It does not switch the
product default to BGE-M3 or the reranker; the current results are provisional
evidence for a later decision.

Follow-up work proceeds in a separate PR in the order below. Here `top_k` is the
final return depth, candidate k is the number of candidates BM25/dense feed into
RRF, RRF `k` is the rank-softening constant in `1 / (k + rank)`, and reranker
pool is the number of re-ranking inputs. The current experiment fixes these at
`10`, `50/50`, `60`, and `20` respectively.

### k-sweep stages

Rather than multiplying every combination at once, reduce candidates at each
stage before passing to the next. Every stage reports the English, Korean, and
cross-language tracks separately.

1. **RRF constant search**: with no reranker, fix `top_k=10` and BM25/dense
   candidates at 50, and compare RRF `k=[10, 30, 60, 100]`.
2. **Return depth and candidate width search**: using the top RRF `k` from
   stage 1, compare `top_k=[5, 10, 20]` and BM25/dense candidates
   `[20, 50, 100]`. Record Recall/MRR/nDCG at `@5`, `@10`, and `@20` matched to
   the return depth.
3. **Reranker pool search**: at the selected RRF `k` and candidate width, fix
   `top_k=10` and compare pool `[10, 20, 50]`. The pool must always be at least
   `top_k`, and test the language-specific baseline embeddings and BGE-M3
   separately.
4. **Repeat verification**: the search reduces candidates with a single run per
   combination; top combinations run 5 times, and final candidates and the
   current default run 10 times.
5. **Operational cost measurement**: record run-to-run quality/latency variance,
   cold/warm cache, model-load time, peak RSS, and disk-cache size.

### Selection rules

- Always include the current default as the control, and freeze the query,
  qrel, and corpus hashes.
- Reject a candidate if English macro nDCG@k or MRR@k regresses by more than
  `-0.01` from the control.
- Korean and cross-language candidates must improve macro nDCG@k or Recall@k
  without worsening zero-hit.
- Reject a candidate if any of negation constraint, genre hit@1, or multi-topic
  intent coverage regresses by more than `-0.05`.
- Among candidates that pass the quality gates, choose the combination with
  lower p95 latency and memory use. Reranker configurations are judged as a
  quality profile separate from non-rerank configurations.
- Only propose a product default change when 10 confirmation runs hold the same
  conclusion.

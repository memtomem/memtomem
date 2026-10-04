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

> **License:** `jinaai/jina-reranker-v2-base-multilingual` is licensed
> CC-BY-NC-4.0 — non-commercial use only
> ([model card](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual)).
> It is measured here for comparison. See
> [E5 + kiwipiepy](#e5--kiwipiepy-korean-optimized-preset-stack) for where it
> was checked.

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

With the current `compare_models_v2.py`, pass
`--profiles language_specific,language_specific_reranked,bge_m3,bge_m3_reranked`
to rerun only these four profiles; its output is `schema_version` 2, so the `jq`
check above applies to the July artifact only.

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
- Targeted regression tests: **68 passed** after adding per-metric spread,
  directional ceiling, and committed-baseline parity coverage.
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
   cold/warm cache, model-load time, peak RSS, and disk-cache size. Run-to-run
   variance, peak RSS and reranker disk size are now recorded for the E5 +
   kiwipiepy profiles (see the section below). Cold/warm cache and model-load
   time are not.

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

## E5 + kiwipiepy (Korean-optimized preset stack)

Verified: 2026-10-04 (Asia/Seoul)

> **License — read before reusing any jina row.**
> `jinaai/jina-reranker-v2-base-multilingual` is licensed **CC-BY-NC-4.0:
> non-commercial use only**
> ([model card](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual)).
> This covers both the fp32 and the int8 graph: int8 is a quantized export in
> the same repository, which changes size and speed, not the license. The jina
> rows are measured as the reranker the Korean-optimized preset enabled before
> #2652, and for comparison only. They are not a recommendation.
> `onnx-community/gte-multilingual-reranker-base` declares no license; its base
> model,
> [`Alibaba-NLP/gte-multilingual-reranker-base`](https://huggingface.co/Alibaba-NLP/gte-multilingual-reranker-base),
> is Apache-2.0. Licenses were read from each repository's Hugging Face
> metadata (`cardData.license`) on 2026-10-04. The gte export's metadata has
> no `cardData.license` field. fastembed 0.8.1's built-in catalog also lists
> the jina model as `cc-by-nc-4.0`; it does not ship the gte export.

This section measures rerankers on the stack the Korean-optimized `mm init`
preset installs: ONNX `intfloat/multilingual-e5-small` (384 dimensions) with
the `kiwipiepy` FTS tokenizer. #2652 removed the reranker from that preset, and
this run records the evidence for it in the repository (#2651). Raw results:
[`model_comparison_e5_v2.json`](./model_comparison_e5_v2.json).

### What changed in the tools

- `benchmark_v2.py` builds each track's config through the product validators,
  so the E5 profile applies the `passage: ` prefix, the 384/320/96 chunk
  budgets and the 512-token sequence limit. Earlier it assigned fields onto a
  built config, which skips those validators. The config ignores `MEMTOMEM_*`
  variables, `config.d` and `config.json`.
- A track is refused unless every search was reranked by the requested model
  (`score_scale == "rerank"` with that model). A rerank timeout or failure
  otherwise falls back to the fused order with only a log warning. Without a
  reranker, no search may report one.
- The FTS tokenizer is set for each track and reset afterwards. A track is
  refused if it indexed or searched with a different tokenizer, which catches
  `kiwipiepy` falling back to `unicode61` when it fails to import.
- `compare_models_v2.py` runs each profile in its own process. That process's
  peak RSS and the reranker's on-disk files are recorded with the results.

### Measurement environment

| Item | Value |
| --- | --- |
| Machine | MacBook Pro (Mac16,5), Apple M4 Max, 16 cores (12P + 4E), 64 GB |
| OS | macOS 26.6.2 (build 25G83), Darwin 25.6.0, arm64 |
| Python / uv | 3.13.2 / 0.12.13 |
| fastembed / onnxruntime / kiwipiepy | 0.8.1 / 1.24.4 / 0.23.2 |
| Base Git commit | `e0006fcc` plus this change |
| Model revisions | jina `9cfeff2d`, gte export `ee64367e` (from the fastembed cache; also in the artifact's `reranker_footprint`) |
| Machine load | other sessions were running: 1-minute load average median 12.2, max 73.1 (54 `uptime` samples taken once a minute during the run; not stored in the artifact) |

Machine, OS and tool versions were read with `system_profiler`, `sw_vers`,
`uv --version` and the installed package metadata. The artifact's
`environment` block records the memtomem, fastembed and Python versions and
the platform string.

### Run procedure

```bash
PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/compare_models_v2.py \
  --profiles e5_kiwipiepy,e5_kiwipiepy_jina,e5_kiwipiepy_jina_int8,e5_kiwipiepy_gte \
  --runs 5 --reranker-pool 20 \
  --output tools/retrieval-eval/model_comparison_e5_v2.json
```

It uses the same corpus, 120 queries, tracks, `top_k=10`, RRF weights
`[1.0, 1.0]` and pool of 20 as the sections above, with **5 runs** per profile.
Quality metrics are means over the runs. The `p50`/`p95` columns in the first
table are the largest value from any run, as in `benchmark_v2.py`.

Checks on the recorded artifact:
- Every reranked profile reranked all of its searches (300 per same-language
  track, 600 for cross-language).
- Every track resolved to `kiwipiepy`, `passage: ` and a 512-token limit, and
  indexed the full 96 (192 for cross-language) chunks.

### Results

| Reranker | License | Track | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p50 | p95 | max run spread |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| none | — | English | 0.6033 | 0.5727 | 0.5453 | 9 | 4.9 ms | 5.8 ms | 0.0250 |
| none | — | Korean | 0.6404 | 0.6216 | 0.5557 | 6 | 5.6 ms | 8.0 ms | 0.0015 |
| none | — | Cross-language | 0.3595 | 0.6028 | 0.4174 | 14 | 4.8 ms | 6.4 ms | 0.0143 |
| jina v2 fp32 | CC-BY-NC-4.0 | English | 0.6647 | 0.6240 | 0.5997 | 8 | 630.8 ms | 718.9 ms | 0.0000 |
| jina v2 fp32 | CC-BY-NC-4.0 | Korean | 0.6703 | 0.6713 | 0.6041 | 7 | 1005.8 ms | 1056.8 ms | 0.0000 |
| jina v2 fp32 | CC-BY-NC-4.0 | Cross-language | 0.4227 | 0.6565 | 0.4855 | 15 | 927.8 ms | 1018.4 ms | 0.0000 |
| jina v2 int8 | CC-BY-NC-4.0 | English | 0.6754 | 0.6392 | 0.6075 | 7 | 516.2 ms | 602.0 ms | 0.0000 |
| jina v2 int8 | CC-BY-NC-4.0 | Korean | 0.6675 | 0.6641 | 0.6041 | 7 | 1052.5 ms | 1254.0 ms | 0.0000 |
| jina v2 int8 | CC-BY-NC-4.0 | Cross-language | 0.4225 | 0.6575 | 0.4859 | 14 | 1691.8 ms | 2591.3 ms | 0.0000 |
| gte-multilingual int8 | Apache-2.0 (base model) | English | 0.6497 | 0.6179 | 0.5804 | 7 | 557.7 ms | 644.0 ms | 0.0000 |
| gte-multilingual int8 | Apache-2.0 (base model) | Korean | 0.6436 | 0.6357 | 0.5692 | 8 | 901.8 ms | 1143.2 ms | 0.0000 |
| gte-multilingual int8 | Apache-2.0 (base model) | Cross-language | 0.4024 | 0.6462 | 0.4630 | 15 | 1082.3 ms | 2011.9 ms | 0.0000 |

"max run spread" is the largest range across the 5 runs of any per-language,
per-query-type metric. Every reranked profile had identical metrics on all 5
runs.

### Change from no reranker

| Reranker | License | Track | Recall@10 | MRR@10 | nDCG@10 | zero-hit |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| jina v2 fp32 | CC-BY-NC-4.0 | English | +0.0613 | +0.0512 | +0.0544 | -1 |
| jina v2 fp32 | CC-BY-NC-4.0 | Korean | +0.0299 | +0.0496 | +0.0484 | +1 |
| jina v2 fp32 | CC-BY-NC-4.0 | Cross-language | +0.0632 | +0.0536 | +0.0681 | +1 |
| jina v2 int8 | CC-BY-NC-4.0 | English | +0.0721 | +0.0665 | +0.0622 | -2 |
| jina v2 int8 | CC-BY-NC-4.0 | Korean | +0.0271 | +0.0425 | +0.0484 | +1 |
| jina v2 int8 | CC-BY-NC-4.0 | Cross-language | +0.0630 | +0.0547 | +0.0685 | +0 |
| gte-multilingual int8 | Apache-2.0 (base model) | English | +0.0464 | +0.0452 | +0.0350 | -2 |
| gte-multilingual int8 | Apache-2.0 (base model) | Korean | +0.0032 | +0.0141 | +0.0135 | +2 |
| gte-multilingual int8 | Apache-2.0 (base model) | Cross-language | +0.0429 | +0.0434 | +0.0455 | +1 |

The reranker only reorders the top 20 fused results. It can push a track's only
relevant result below rank 10, which is how a reranker can add a zero-hit query
while improving the averages.

### Cost

Latency is the median over the 5 runs of each run's p50 / p95, which is less
sensitive to the load spikes above than the largest-run values in the results
table. Peak RSS is the whole profile process (model loads, indexing and every
track and run), not the reranker's own memory. Disk is the reranker files
fastembed downloads (graph, tokenizer and config) in the fastembed cache.

| Reranker | License | English p50 / p95 | Korean p50 / p95 | Cross-language p50 / p95 | Peak RSS | Disk |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| none | — | 5 / 5 ms | 6 / 8 ms | 5 / 6 ms | 2.71 GB | — |
| jina v2 fp32 | CC-BY-NC-4.0 | 618 / 706 ms | 982 / 1033 ms | 922 / 1014 ms | 5.72 GB | 1131 MB |
| jina v2 int8 | CC-BY-NC-4.0 | 496 / 599 ms | 862 / 936 ms | 807 / 936 ms | 4.29 GB | 297 MB |
| gte-multilingual int8 | Apache-2.0 (base model) | 550 / 630 ms | 889 / 929 ms | 910 / 1371 ms | 5.13 GB | 358 MB |

The largest-run values are dominated by a few runs:
- jina int8's cross-language p95 was 2591 and 2235 ms in two runs and 924–936 ms
  in the other three.
- Its Korean p95 was 1254 ms in one run and 911–967 ms in the others.
- gte's cross-language p95 ranged from 1279 to 2012 ms across the runs.

### Findings

- **jina v2 adds about one second per Korean search.** Its median p50 is
  982 ms on Korean, 922 ms on cross-language and 618 ms on English, against
  5–6 ms without a reranker. This supports the "about a second" in #2652's
  CHANGELOG entry for Korean and mixed content. What was measured is the
  latency of the search call on CPU, not CPU time.
- **jina gains +0.048 Korean nDCG@10** (0.5557 → 0.6041) and +0.068
  cross-language. On this stack that is a smaller Korean gain than the
  language-specific baseline's +0.147 in the July section.
- **int8 keeps jina's Korean nDCG@10 and its license.** jina int8 matched fp32
  to four decimal places on Korean nDCG@10 (0.6041); Korean MRR@10 was 0.007
  lower. Its nDCG@10 was within
  +0.008 of fp32 on the other tracks. It used 1.4 GB less peak RSS and a quarter
  of the disk, and its median p50 was 12–20% lower. It is still CC-BY-NC-4.0.
- **gte-multilingual int8 recovers 28% of jina's Korean gain**
  (+0.0135 of +0.0484 nDCG@10) and 67% of its cross-language gain. Its Korean
  latency is close to jina's, and it adds two Korean zero-hit queries.

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

PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/sweep_dimensions.py \
  --output tools/retrieval-eval/dimension_sweep_v2.json
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

### Canonical producer: Ubuntu

Both baselines are *checked* on the Ubuntu CI runner, so both must be
*produced* there. Calibrating on a developer's machine is not a smaller version
of the same thing: the platform delta is the same size as the margins these
floors are built from. #2224 measured it — a macOS recalibration raised
`ko|genre_primary|mrr@10` to `0.31` while the Ubuntu runner observed `0.291`,
so the locally produced file would have made CI *stricter* on the very slice
that was failing.

Regenerate by running the CI workflow with `workflow_dispatch` and input
`refresh_retrieval_baselines: true`, then download the
`retrieval-eval-baselines` artifact — `baseline_v0.3.8.json`,
`baseline_v2.json` and `PROVENANCE.json` — and commit all three as-is. The
refresh is its own job and nothing `needs:` the floor checks, so a
currently-failing floor does not block its own refresh. This mirrors the
quality gate's contract (`tools/quality-gate/README.md`).

`PROVENANCE.json` is what makes a lowered floor auditable after the fact: the
source commit, the workflow run, the runner platform, the versions of the
packages that actually decide these numbers, the exact command behind each
file, and a sha256 of each. CI re-checks those hashes on every run
(`write_provenance.py --check`), so a baseline edited without regenerating its
manifest fails rather than passing quietly.

Run-to-run variance is real and bounded: two consecutive refreshes on the same
runner and commit differed on exactly one floor, by one rounding step
(`ko|genre_primary|mrr@10`, 0.25 vs 0.24). Do not read a difference that size
as a change in behaviour.

### Recalibrated for #2224 — `genre_primary` measured path text

Both baselines were regenerated on the Ubuntu producer when BM25 stopped
searching the `source_file` column while any chunk's `content` matches
(#2224). Every floor that moved by more than 0.01 belongs to `genre_primary`,
plus one that moved *up*:

| floor | before | after |
|---|---|---|
| `cross_language en\|genre_primary\|cross_language_relevant@10` | 1.44 | 1.26 |
| `cross_language en\|genre_primary\|recall@10` | 0.2925 | 0.2475 |
| `cross_language en\|genre_primary\|ndcg@10` | 0.2774 | 0.2492 |
| `english en\|genre_primary\|recall@10` | 0.405 | 0.3825 |
| `english en\|genre_primary\|mrr@10` | 0.408 | 0.3876 |
| `v1 ko\|genre_primary\|mrr@10` | 0.30 | 0.25 |
| `korean` / `cross_language` `ko\|direct\|mrr@10` | 0.4525 / 0.5029 | 0.4675 / 0.5179 (**up**) |

`genre_primary` relevance is defined as "chunk's topic is a target topic **and**
chunk's genre is the query's genre" (`benchmark_v2.py:build_qrels`), and both
are parsed from the file's path — the corpus is laid out
`{language}/{topic}/{genre}.md`. A retriever that matched path tokens was
matching the label definition itself.

The Korean slices are the supporting evidence, not a proof. `ko|genre_primary`
queries are mixed-script and carry the English topic word literally
(`postgres 절차 접속 수행`, `observability KST 원인 후속 조치`), so an English
directory name was reachable from a Korean query — and that is the same
component the label is derived from. What separates `genre_primary` from the
other types is not whether the query names a path word (`ko|direct` includes
`Postgres 커넥션 풀 포화`, which names one too, and went *up*) but that its
label is defined by **topic *and* genre**, and the path encodes both: the
directory is the topic and the filename is the genre, so a path match supplied
the entire qrel key rather than half of it.

That is why the movement reads as removing a label-shaped signal rather than
losing retrieval quality. It is an inference from where the metric moved, not a
per-query audit; a stronger claim would need each lost result checked for
whether it was reachable only through its path.

Neither file was hand-edited. Both are regenerated wholesale by their tools,
and `test_v2_committed_quality_bounds_match_generation_formula` recomputes
every v2 bound from the committed `aggregate` and `run_spreads` values, so a
floor edited on its own fails the suite. Note the limit of that guard: it
checks bounds against aggregates, not aggregates against the per-query rows, so
a *coordinated* edit to both would still pass. The protection against that is
the workflow producing the file, not the test reading it. The frozen holdout (`query_holdout_v2.py`) is untouched — the
queries, their identifiers and the qrel rules are unchanged; only the measured
baseline they are scored against moved.

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

## Dimension-truncation sweep (issue #1787)

`dimension_sweep_v2.json` records a one-run evaluation of Matryoshka-style
truncation of the default 1024-dim `BAAI/bge-m3` vectors — slicing each vector to
a shorter prefix and re-normalizing to unit L2 — at dimensions `1024/512/256/128`,
in both the fused (BM25 + RRF weights `[1.0, 1.0]`) and dense-only (weights
`[0.0, 1.0]`) pipeline. Truncation is applied by a `TruncatingEmbedder` wrapper
installed via a monkeypatch on `memtomem.runtime.components.create_embedder`; no production
code is changed, and native full-dimension vectors are cached so each unique text
is embedded once for the whole sweep.

bge-m3 is not MRL-trained, and the sweep confirms naive truncation degrades
retrieval at every reduced dimension: dense-only macro Recall@10 falls
`-0.035/-0.124/-0.193` at `512/256/128`, and even after BM25 + RRF masks part of
the loss, fused macro Recall@10 still falls `-0.023/-0.046/-0.086` — beyond the
`-0.01` materiality threshold at every step. The conclusion is to prefer
MRL-native model presets (open question 2) over truncation if lower-dimensional
vectors are wanted. The `--include-nomic` flag adds `nomic-embed-text-v1.5`
tracks, but without task-prefix support in `OnnxEmbedder` those numbers are a
lower bound only; a fair MRL-native comparison is deferred follow-up work. See
[`DIMENSION_SWEEP_REPORT.md`](./DIMENSION_SWEEP_REPORT.md) for the full tables,
control cross-check, and recommendations, and
[`dimension_sweep_v2.json`](./dimension_sweep_v2.json) for the raw artifact.

# Dense-vector dimension sweep report (issue #1787)

Verified: 2026-07-17 (Asia/Seoul)

This document records an offline evaluation of **Matryoshka-style truncation** of
the default 1024-dim `BAAI/bge-m3` dense vectors — slicing each vector to a
smaller prefix and re-normalizing to unit L2 — measured on the bilingual v2
holdout as English, Korean, and cross-language tracks. It answers open question 1
of issue #1787: *bge-m3 is not documented as MRL-trained, so does naive truncation
preserve retrieval quality?* Raw results are preserved in
[`dimension_sweep_v2.json`](./dimension_sweep_v2.json).

**Verdict: no.** Truncation degrades bge-m3 retrieval measurably at every reduced
dimension, in both the dense-only and the fused (BM25 + RRF) pipeline. No
truncated dimension is "free"; even halving to 512 dims costs more than the
`-0.01` macro-recall materiality threshold in the fused pipeline. See
[Recommendations](#recommendations).

## Measurement environment

| Item | Value |
| --- | --- |
| Chip | Apple M4 Max (arm64) |
| OS | macOS 26.5.2 (build 25F84) |
| Kernel | Darwin 25.5.0 |
| Python | 3.13.2 |
| uv | 0.11.16 |
| fastembed | 0.8.0 |
| memtomem | 0.3.11 |
| Base Git commit | `6ebe0636` |

Latency is **not** the headline of this experiment — the read-path cost that
truncation reduces is storage and dense-search compute, both linear in dimension.
The p95 figures below are single-run CPU search times on this machine, include
cold-cache noise on the first tracks, and must be read separately from the quality
metrics.

## What truncation does and does not reduce

Restating the issue's scope honesty, confirmed by the mechanics:

- **Reduces** (linearly in dimension): sqlite-vec `chunks_vec` storage
  (`float[N]` per vector), dense-search distance compute, and resident memory of
  loaded vectors. 512 dims ≈ 50% of the 1024 baseline, 256 ≈ 25%, 128 ≈ 12.5%.
- **Does NOT reduce**: embedding inference cost or ONNX RSS spikes — bge-m3 still
  runs its full forward pass; truncation happens after pooling. The indexing-load
  work is tracked separately (#1783 / #1784 / #1786).

## Verification design

- Public synthetic corpus: 48 Markdown files, 192 chunks, English + Korean.
- Frozen 120-query bilingual holdout (60 same-intent English/Korean pairs), equal
  counts of direct, paraphrase, underspecified, multi-topic, negation, and
  genre-primary types.
- English track: English corpus + 60 English queries. Korean track: Korean corpus
  + 60 Korean queries. Cross-language track: combined corpus + all 120 queries.
- Every search uses `top_k=10`, `rrf_k=60`, BM25/dense candidates `50/50`, no
  reranker (defaults). One run per (dimension × mode).
- Two retrieval **modes**:
  - **fused** — RRF weights `(1.0, 1.0)`, the production default; measures the
    real user-facing pipeline where BM25 can compensate for dense-quality loss.
  - **dense** — RRF weights `(0.0, 1.0)`; BM25 documents then score
    `0 / (k + rank) = 0` in fusion and with `candidate_k ≥ top_k` the top-k is
    purely dense-ranked. This isolates the dense-quality loss that fusion masks.
- Truncation + L2-renorm is applied by a `TruncatingEmbedder` wrapper installed
  via a monkeypatch on `component_factory.create_embedder` — **no production code
  is changed**. Native full-dimension vectors are cached across tracks, so each
  unique text is embedded exactly once for the whole sweep (312 unique texts
  embedded; 4680 subsequent lookups served from cache — the bge-m3 model still
  loads once per language track before its texts are cached) and every dimension
  is a pure truncate-and-renorm over identical native vectors.

### Control cross-check

At 1024 dims the wrapper is an exact passthrough, so the `bge_m3_fused_1024`
control must reproduce the committed `model_comparison_v2.json` bge-m3 profile. All
three tracks reproduce it to six decimals — English `0.637897`, Korean `0.657778`,
cross-language `0.444737` macro Recall@10 — validating both the passthrough and the
vector cache. (Fused MRR/nDCG and zero-hit on the cross-language track wobble at
the fourth decimal between runs from same-distance tie-breaks in the BM25 leg; the
dense-only tracks are stable. This single-run tie-break jitter is the
cross-platform variance the v2 methodology already documents and stays well under
the `-0.01` materiality threshold.)

## Run procedure

```bash
PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/sweep_dimensions.py \
  --output tools/retrieval-eval/dimension_sweep_v2.json

uv run ruff check packages/memtomem/src packages/memtomem/tests tools
uv run ruff format --check packages/memtomem/src packages/memtomem/tests tools
```

## Results

Macro metrics, re-averaged across the per-language, per-query-type scores.

| Mode / dim | Track | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| fused 1024 | English | 0.637897 | 0.617268 | 0.570829 | 9 | 23.258 ms |
| fused 1024 | Korean | 0.657778 | 0.602976 | 0.560427 | 7 | 23.190 ms |
| fused 1024 | Cross-language | 0.444737 | 0.641710 | 0.490558 | 15 | 3.477 ms |
| fused 512 | English | 0.617063 | 0.603565 | 0.548185 | 8 | 3.282 ms |
| fused 512 | Korean | 0.620119 | 0.617818 | 0.551026 | 8 | 2.868 ms |
| fused 512 | Cross-language | 0.434842 | 0.632827 | 0.479274 | 14 | 3.183 ms |
| fused 256 | English | 0.604563 | 0.600205 | 0.536843 | 8 | 2.986 ms |
| fused 256 | Korean | 0.595397 | 0.606918 | 0.520342 | 7 | 2.530 ms |
| fused 256 | Cross-language | 0.402863 | 0.606511 | 0.445450 | 16 | 2.772 ms |
| fused 128 | English | 0.566171 | 0.561495 | 0.500324 | 11 | 2.847 ms |
| fused 128 | Korean | 0.547500 | 0.556733 | 0.473555 | 11 | 2.388 ms |
| fused 128 | Cross-language | 0.368052 | 0.566528 | 0.406998 | 22 | 2.899 ms |
| dense 1024 | English | 0.637203 | 0.644345 | 0.553029 | 4 | 3.396 ms |
| dense 1024 | Korean | 0.699444 | 0.617222 | 0.574751 | 3 | 2.923 ms |
| dense 1024 | Cross-language | 0.501070 | 0.662761 | 0.516573 | 7 | 3.556 ms |
| dense 512 | English | 0.613790 | 0.624603 | 0.524811 | 4 | 3.114 ms |
| dense 512 | Korean | 0.660099 | 0.554927 | 0.527285 | 6 | 2.688 ms |
| dense 512 | Cross-language | 0.458493 | 0.623747 | 0.470804 | 11 | 3.306 ms |
| dense 256 | English | 0.513393 | 0.548003 | 0.457473 | 10 | 2.943 ms |
| dense 256 | Korean | 0.561468 | 0.504504 | 0.461623 | 10 | 2.481 ms |
| dense 256 | Cross-language | 0.390552 | 0.578442 | 0.409985 | 17 | 2.851 ms |
| dense 128 | English | 0.434920 | 0.518056 | 0.406551 | 17 | 2.905 ms |
| dense 128 | Korean | 0.509068 | 0.465437 | 0.404600 | 11 | 3.512 ms |
| dense 128 | Cross-language | 0.314546 | 0.519220 | 0.340263 | 25 | 3.142 ms |

### Degradation vs the 1024-dim control

Macro deltas averaged across the three tracks (negative = worse than 1024).

| Mode | Dim | ΔRecall@10 | ΔMRR@10 | ΔnDCG@10 |
| --- | --- | ---: | ---: | ---: |
| fused | 512 | -0.0228 | -0.0026 | -0.0144 |
| fused | 256 | -0.0459 | -0.0161 | -0.0397 |
| fused | 128 | -0.0862 | -0.0591 | -0.0803 |
| dense | 512 | -0.0351 | -0.0404 | -0.0405 |
| dense | 256 | -0.1241 | -0.0978 | -0.1051 |
| dense | 128 | -0.1931 | -0.1405 | -0.1643 |

## Interpretation

- **Dense-only degradation is steep and monotonic.** Isolated from BM25, macro
  Recall@10 falls `-0.035` at 512, `-0.124` at 256, and `-0.193` at 128 — a
  ~31% relative drop at 128. This is the signature of a model that was **not**
  MRL-trained: the vector's information is not front-loaded into the leading
  dimensions, so a prefix discards genuine signal. Truncated bge-m3 is not a
  substitute for a natively low-dimensional embedding.
- **Fusion masks part of the loss but not enough.** BM25 + RRF recovers roughly
  two-thirds of the dense loss at 256 (dense `-0.124` → fused `-0.046`), which is
  exactly why the issue insisted the eval measure the fused pipeline. But even
  after that cushion, fused 512 costs `-0.023` macro Recall@10 and fused 256
  costs `-0.046` — both beyond the `-0.01` materiality threshold. The masking is
  a reason to distrust dense-only numbers as the whole story, not evidence that
  truncation is safe.
- **Zero-hit counts worsen at the aggressive end.** Fused zero-hits rise from 31
  (summed across tracks at 1024) to 44 at 128; dense from 14 to 53. Truncation
  does not just reorder results — at 128 it pushes queries out of the top-10
  entirely.
- **Storage savings are real but bought with recall.** 512 dims halve vector
  storage for a `-0.023` fused Recall@10 cost; 256 quarters it for `-0.046`.
  Whether that trade is acceptable is a product judgment, but it is a trade, not
  a free win — which is the decision this benchmark exists to inform.

## Recommendations

1. **Do not ship a naive truncation knob for bge-m3.** At the current corpus
   scale (~4.4k chunks the issue cites) the absolute storage saved is modest, and
   every reduced dimension costs measurable fused-pipeline recall. The read-path
   win does not justify the quality loss as a default or an easy opt-in.
2. **If lower-dimensional vectors are wanted, pursue MRL-native model presets
   (open question 2), not truncation.** Models trained with Matryoshka loss
   (`nomic-embed-text-v1.5`, `snowflake-arctic-embed`) front-load information into
   the leading dimensions and are designed to survive truncation. That is the
   path that could actually deliver the storage win without the recall cost.
3. **Evaluate any MRL-native candidate fairly before comparing.** The sweep script
   ships a `--include-nomic` flag, but `OnnxEmbedder` applies no task prefixes
   (`search_document:` / `search_query:`), which nomic requires — so any nomic
   number it produces is a *lower bound* and must not be read as a fair
   comparison. A proper MRL-native evaluation needs a small prefix hook first;
   that is deliberately out of scope here and is the natural follow-up if the
   maintainer wants to explore option 2.
4. **Migration remains a hard cost regardless.** `embedding.dimension` is an
   immutable init-time field; changing it requires `mm embedding-reset
   --mode apply-current` plus a full re-index, and interacts with the
   `mem_status` dim-mismatch warning. Any future dimension option must integrate
   with that flow — another reason not to expose it without a clear quality win.
5. **Confirm before any product decision.** These are single-run screening
   figures. Before acting on option 2, repeat 5–10 times on one machine and
   record run-to-run variance, and measure a real (non-truncated) MRL-native
   model with correct prefixes.

## Limitations

- Single run per (dimension × mode); no run-spread, RSS, or cold/warm accounting.
- Synthetic 192-chunk corpus; absolute numbers will differ on a larger real DB,
  though the *direction* (truncation degrades a non-MRL model) is model-intrinsic
  and expected to hold.
- Only bge-m3 truncation was measured. No MRL-native model was benchmarked here —
  that is the recommended follow-up, and requires the task-prefix hook noted above.
- Like every script in `tools/retrieval-eval`, this sweep drives `benchmark_v2`,
  which neutralizes persisted overrides but still lets `~/.memtomem/config.d`
  fragments and `MEMTOMEM_*` environment variables reach `Mem2MemConfig()`. On a
  machine with a customized ambient config those could perturb rankings; the
  control cross-check above (matching the committed `model_comparison_v2.json`)
  confirms no such perturbation on this run. Fully isolating the harness via
  `create_components(config, load_ambient_config=False)` is a suite-wide hardening
  best done in its own change, since it would re-baseline every committed artifact.

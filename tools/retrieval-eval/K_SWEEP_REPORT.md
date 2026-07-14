# Retrieval v2 staged k-sweep

## Purpose and method

This report summarizes the committed one-run `k_sweep_v2.json` artifact. The
sweep reduces candidates for later repeated validation; it is not a product
default calibration.

- 19 profiles: 4 RRF, 9 candidate-depth, and 6 reranker-pool profiles.
- Every profile evaluates English, Korean, and cross-language tracks
  separately with fixed RRF weights `[1.0, 1.0]`.
- Eligible alternatives must pass the quality and zero-hit gates. Selection
  then maximizes Korean plus cross-language Recall/nDCG gain; a tie prefers the
  lower maximum p95 search latency.
- Memory use is not measured and is not a selection input.

Reproduce the complete artifact without overwriting it with a partial stage:

```bash
PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
  tools/retrieval-eval/sweep_k_v2.py --runs 1 --stage all \
  --output tools/retrieval-eval/k_sweep_v2.json
```

## Results

### Stage 1 — RRF k

At `top_k=10`, candidates `50/50`, and reranking disabled, the sweep compares
`rrf_k=10,30,60,100`. No alternative passes every gate, so `rrf_k=60` remains
selected. `rrf_10` regresses Korean quality/zero hits, `rrf_30` adds one
cross-language zero hit, and `rrf_100` does not improve Korean quality.

### Stage 2 — candidate width by requested depth

This stage selects candidate width independently for each requested `top_k`;
it does not select a new `top_k` default.

| `top_k` | Selected candidates | Decision |
|---:|---:|---|
| 5 | 100 | Only eligible non-control candidate; quality gain `+0.015095`, max p95 `5.371 ms`, zero-hit change EN `0`, KO `-1`, cross `-2` |
| 10 | 50 | Control retained |
| 20 | 50 | Control retained; candidate 100 gains `+0.041911` but adds two cross-language zero hits |

### Stage 3 — active reranker pool

With `top_k=10`, `rrf_k=60`, candidates `50/50`, and the multilingual Jina
reranker enabled, both embedding families retain pool `20`.

| Embedding family | Pool 10 gain | Pool 20 max p95 | Pool 50 outcome |
|---|---:|---:|---|
| Language-specific | `-0.301416` | `1007.950 ms` | Gain `+0.139578`, but KO/cross zero hits `+2/+4`, negation `-0.075`, max p95 `2230.276 ms` |
| BGE-M3 | `-0.235256` | `1044.319 ms` | Gain `-0.049239`, KO zero hits `+2`, max p95 `2272.040 ms` |

Stage 3 compares pool sizes only after reranking is enabled. It does not test
whether reranking should be enabled by default.

## Decision and limitations

Keep the product defaults: `top_k=10`, BM25/dense candidates `50/50`,
`rrf_k=60`, and reranking disabled. Treat `top_k=5` with candidate width 100
as a provisional follow-up candidate requiring repeated validation.

This artifact has one run rather than the planned 5-run/10-run confirmation.
It does not record environment, commit, query/corpus hashes, run spread,
cold/warm state, model load, RSS, or disk cost. Latency covers the search call
only. Asymmetric BM25/dense candidate widths were not evaluated.

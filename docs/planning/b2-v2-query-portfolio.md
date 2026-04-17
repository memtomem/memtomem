# B.2 Query portfolio draft

80 queries total (40 EN + 40 KO), 5 types per language. Each query has
a target tag set (primary topic/subtopic strings) and a declared type
that drives per-type threshold assertions.

Full portfolio emerges after corpus is drafted (subtopic vocabulary is
finalized during corpus generation — see emergence policy in
`b2-multilingual-regression-v2.md`). This doc holds the **seed
examples** that illustrate each type's structure and the **count
distribution** per type.

## Structure

```python
Query = tuple[
    str,                  # query text
    frozenset[str],       # target tags (primary subtopics)
    str,                  # query type: direct|paraphrase|underspecified|multi_topic|negation
]
```

## EN distribution (40 total)

| Type | Count | Target size | Detects |
|---|---|---|---|
| direct | 10 | 2-3 | catastrophic ranker failure |
| paraphrase | 10 | 2-3 | dense embedding degradation |
| underspecified | 8 | 5-8 | reranker / MMR diversity |
| multi_topic | 7 | 4-6 | fusion weight miscalibration |
| negation | 5 | 1-2 | dense semantic understanding |

## KO distribution (40 total)

Same structure; negation reduced 5→3 (Korean negation often reads
stilted in technical prose), underspecified bumped 8→10.

| Type | Count | Target size | Detects |
|---|---|---|---|
| direct | 10 | 2-3 | — |
| paraphrase | 10 | 2-3 | — |
| underspecified | 10 | 5-8 | — |
| multi_topic | 7 | 4-6 | — |
| negation | 3 | 1-2 | — |

## Seed examples (EN)

### direct (keyword-heavy, matches chunk vocabulary closely)

- `("Redis maxmemory-policy allkeys-lru eviction", {"caching/eviction"}, "direct")`
- `("pgbouncer transaction mode connection pool", {"postgres/connection_pool"}, "direct")`
- `("Prometheus cardinality budget active series", {"observability/metrics"}, "direct")`
- `("OAuth2 PKCE refresh token rotation", {"auth/oauth"}, "direct")`

### paraphrase (semantic equivalent, vocabulary-shifted)

- `("preventing cache memory exhaustion under load", {"caching/eviction", "caching/stampede"}, "paraphrase")`
- `("keeping database connections alive across query bursts", {"postgres/connection_pool"}, "paraphrase")`
- `("metrics that track system health without blowing up storage", {"observability/metrics"}, "paraphrase")`
- `("passwordless login with hardware-backed credentials", {"auth/webauthn"}, "paraphrase")`

### underspecified (vague, relies on MMR for diverse top-K)

- `("cache invalidation", {"caching/invalidation", "caching/stampede"}, "underspecified")`
- `("database indexes", {"postgres/indexing"}, "underspecified")`
- `("tracing", {"observability/tracing"}, "underspecified")`
- `("canary deploy", {"ci_cd/deployment"}, "underspecified")`

### multi_topic (spans two topics via union)

- `("monitoring Postgres replication lag", {"postgres/replication", "observability/metrics"}, "multi_topic")`
- `("rate-limiting authenticated API endpoints", {"api_design/rate_limiting", "auth/jwt"}, "multi_topic")`
- `("ingesting Kafka events into a data warehouse", {"kafka/consumer", "data_pipelines/ingestion"}, "multi_topic")`

### negation (what's NOT chosen / why NOT used)

- `("why eventual consistency is unsuitable for inventory writes", {"caching/invalidation"}, "negation")`
- `("avoiding synchronous replication in the hot path", {"postgres/replication"}, "negation")`
- `("when not to use k8s autoscaling", {"k8s/scaling"}, "negation")`

## Seed examples (KO)

### direct

- `("Redis maxmemory-policy allkeys-lru eviction 정책", {"caching/eviction"}, "direct")`
- `("pgbouncer transaction 모드 커넥션 풀", {"postgres/connection_pool"}, "direct")`
- `("Prometheus 카디널리티 예산 active series", {"observability/metrics"}, "direct")`
- `("OAuth2 PKCE refresh token 로테이션", {"auth/oauth"}, "direct")`

### paraphrase

- `("부하 상황에서 캐시 메모리 고갈을 막는 법", {"caching/eviction", "caching/stampede"}, "paraphrase")`
- `("쿼리 버스트 중 DB 커넥션을 유지하는 구성", {"postgres/connection_pool"}, "paraphrase")`
- `("저장 비용을 폭발시키지 않고 시스템 상태를 추적하는 지표", {"observability/metrics", "cost_optimization/observability"}, "paraphrase")`
- `("하드웨어 인증서 기반 무비밀번호 로그인", {"auth/webauthn"}, "paraphrase")`

### underspecified

- `("캐시 무효화", {"caching/invalidation", "caching/stampede"}, "underspecified")`
- `("데이터베이스 인덱스", {"postgres/indexing"}, "underspecified")`
- `("트레이싱", {"observability/tracing"}, "underspecified")`
- `("카나리 배포", {"ci_cd/deployment"}, "underspecified")`

### multi_topic

- `("Postgres replication lag 모니터링", {"postgres/replication", "observability/metrics"}, "multi_topic")`
- `("인증이 필요한 API 엔드포인트 rate limiting", {"api_design/rate_limiting", "auth/jwt"}, "multi_topic")`
- `("Kafka 이벤트를 데이터 웨어하우스로 수집", {"kafka/consumer", "data_pipelines/ingestion"}, "multi_topic")`

### negation

- `("재고 쓰기에 이벤트 기반 최종 일관성이 부적절한 이유", {"caching/invalidation"}, "negation")`
- `("hot path 에서 동기 복제를 피해야 하는 이유", {"postgres/replication"}, "negation")`
- `("k8s 오토스케일링을 쓰지 말아야 할 때", {"k8s/scaling"}, "negation")`

## Per-type floor policy

Each (type, language) pair has three floors (recall@10, MRR@10,
nDCG@10) → 5 × 2 × 3 = 30 floor constants. Floors calibrated from
10-run measurement, set to `round(measured * 0.9, 2)`.

### Sensitivity expectation per type

Not every type needs to react to every pipeline knob. Expected
reactions:

| Knob \ Type | direct | paraphrase | underspecified | multi_topic | negation |
|---|---|---|---|---|---|
| `bm25_weight` ↓ | weak | strong | medium | strong | strong |
| `dense_weight` ↓ | weak | weak | medium | medium | strong |
| reranker off | medium | medium | strong | medium | medium |
| MMR λ=1.0 (no diversity) | weak | weak | strong | weak | weak |

If a type fails to react on ANY of these knobs across both languages,
it is demoted from floor enforcement and documented as
pipeline-invariant (see `b2-multilingual-regression-v2.md` §
Verification strategy).

## Full portfolio construction (timing)

Seeds above cover ~4 queries per type (half of smaller types, 30-40%
of larger). Full 80 queries are drafted in Phase 4 after corpus is
finalized — this way:

1. Query writer (Claude) can see which primary subtopics actually have
   chunks (avoid targeting subtopics that got 0 chunks due to Gemini
   batches missing a cell)
2. Query relevant-size can be tuned by counting chunks per target tag
3. negation queries can point at specific chunks that discuss the
   "not-chosen" side

## REQUIRED (Phase 4): genre-primary queries as core axis

**This is not deferred speculation — Phase 1 validation promoted it
to a required axis.** Phase 1 found:

- Dense embeddings: topic-dominant, genre-invariant (within-genre
  mean cosine distance 0.3374 ≥ across-genre 0.3222 — no separation)
- BM25 raw vocabulary Jaccard: weak separation (intra 0.10-0.14 <
  inter 0.15-0.21 — within-genre overlap is *lower* than
  across-genre, i.e. the 4 primary subtopics within a genre diverge
  lexically)
- **But genre marker tokens with high IDF persist 4/4 in their home
  genre and 0-1/4 in others** (see anchors below)

Implication: `rrf_weights` sensitivity on v2 depends on queries that
contain genre marker tokens — those are the only queries where BM25
ranks genre-homogeneous chunks differently from dense. Topic-only
queries will behave like the MVP (signals converge).

### Concrete Phase 1 findings — genre anchor vocabulary

Per `b2-v2-phase1-validation.md`. Use these as anchors when drafting
genre-primary queries. Each token appears in ≥ 3/4 chunks of its
home genre and 0-1/4 chunks of other genres in the caching × ko
sample:

- **postmortem**: `후속`, `조치`, `KST`, `원인`, `YYYY-MM-DD HH:MM`
  timestamp, `강화`, `수정`, `복구`
- **adr**: `채택`, `결정`, `선택`, `대신`, `trade-off`, `재평가`,
  `재검토`, `수용`
- **runbook**: `SET`, `CONFIG`, `절차`, `접속`, `수행한다`,
  imperative endings
- **troubleshooting**: `의심`, `증상`, `만약`, `점검해`, `재현`,
  `~세요`, `~라면`

### Portfolio allocation (revised to 100 queries)

Add genre-primary queries as an orthogonal axis on top of the 5-type
structure. Per language:

- 40 original **topic-primary** queries (5 types × counts in tables
  above)
- **10 genre-primary queries** — 2-3 per genre, using anchor vocab;
  spread across types (≥ 2 direct + 2 paraphrase + 2 underspecified
  + 2 multi_topic + 2 negation optional)

Total: 50 per language, 100 total (was 80). CI cost impact: query
count +25%, runtime still under 2 min budget for test-golden-path.

### Genre-primary query examples (seed)

- `("Redis maxmemory-policy 절차 수행", {"caching/redis"}, "direct")`
  → BM25 should boost `runbook.md` chunks (절차, 수행 markers)
- `("eviction 후속 조치 KST 장애", {"caching/eviction"}, "direct")`
  → BM25 should boost `postmortem.md` chunks (후속, 조치 markers)
- `("Redis Cluster 대신 채택한 구성", {"caching/redis"}, "direct")`
  → BM25 should boost `adr.md` chunks (대신, 채택)
- `("stampede 증상 의심 진단", {"caching/stampede"}, "direct")`
  → BM25 should boost `troubleshooting.md` chunks (증상, 의심)

### Sensitivity expectation (v2 core contract)

| rrf_weights | Expected on genre-primary queries |
|---|---|
| `[1, 1]` balanced | Top-K mixes genre anchor + topic signal |
| `[1, 0]` BM25-only | Top-K strongly skewed toward anchored genre |
| `[0, 1]` dense-only | Top-K ignores genre anchor; returns topic-matched chunks across all genres |

If `[1,0]` vs `[0,1]` produces **identical top-K** on genre-primary
queries, the anchor-based BM25 signal is not working and v2's core
mechanism has failed. This is an early-warning test: run it after
Phase 2b (first topic complete) before investing in the remaining
14 topics.

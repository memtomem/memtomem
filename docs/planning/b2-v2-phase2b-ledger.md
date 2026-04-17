# B.2 v2 — Phase 2b ledger (process log)

Process log for the postgres topic. Curation decisions, observations
collected, and deferred decisions that surfaced during Phase 2b. Lives
as a companion to `b2-v2-design.md` (timeless methodology) and
`b2-v2-phase1-validation.md` (measurement snapshots). This file
accumulates new entries as later topics are generated; existing entries
are not rewritten.

## Curation ledger — Phase 2b postgres (9 drift corrections / 32 chunks)

Drift rate 9/32 ≈ 28%, consistent with Phase 1 (caching ko) and Phase
2a (caching en) at ~30%. Gemini output requires curation as a
first-class pipeline step; see `b2-v2-design.md` § "Implementation
phases" for the 3a/3b split.

### Category distribution

| Category | Count | Description |
|---|---|---|
| Out-of-vocab expansion | 1 | Gemini used a subtopic outside the closed vocabulary (e.g. `search/cluster` on a postgres replication chunk) |
| Intra-vocab misclassification | 4 | Valid subtopic, wrong cell (e.g. `k8s/networking` where `k8s/scaling` fit the HPA content) |
| Absent-topic projection | 3 | Subtopic not supported by the chunk body (e.g. `postgres/vacuum` tagged on a replication runbook that never mentions vacuum) |
| Claude over-correction | 1 | Curator rejected a valid Gemini secondary that the chunk body actually supported (troubleshooting ko vacuum: `cost_optimization/storage` was correct; handoff's initial "drop" proposal was the over-correction) |

The fourth category matters as much as the first three. Curation
itself is a failure mode — if only under-correction is tracked,
curator bias accumulates and the corpus drifts toward canonical-looking
but less realistic tagging. Phase 5 calibration should verify that
curator over-correction rate remains bounded.

### Per-chunk corrections (Phase 2b postgres, 9 total)

| Batch | Chunk | Original secondary | Final secondary | Category |
|---|---|---|---|---|
| runbook EN | #1 indexing | `[postgres/vacuum, cost_optimization/compute]` | `[observability/metrics]` | Absent-topic projection |
| runbook EN | #2 partitioning | `[postgres/indexing, data_pipelines/transformation]` | `[data_pipelines/ingestion]` | Out-of-vocab expansion (transformation=ETL) |
| runbook EN | #4 replication | `[postgres/vacuum, incident_response/mitigation]` | `[incident_response/mitigation]` | Absent-topic projection |
| troubleshooting KO | #1 vacuum | `[observability/metrics, cost_optimization/storage]` | `[observability/metrics, cost_optimization/storage]` **kept** | Initial handoff proposal was over-correction; reverted |
| troubleshooting KO | #2 conn_pool | `[observability/alerting]` | `[observability/metrics]` | Intra-vocab misclassification |
| troubleshooting KO | #3 indexing | `[search/query, observability/tracing]` | `[observability/metrics]` | Out-of-vocab (search/* = ES vocab) + Intra-vocab (tracing≠metrics) — counted once as Out-of-vocab |
| postmortem KO | #1 vacuum | `[observability/alerting]` | `[incident_response/mitigation]` | Intra-vocab misclassification |
| postmortem KO | #3 indexing | `[observability/metrics]` | `[observability/logging]` | Intra-vocab misclassification |
| adr KO | #1 partitioning | `[cost_optimization/storage]` | `[data_pipelines/orchestration]` | Absent-topic projection |
| adr KO | #3 indexing | `[postgres/vacuum]` | `[cost_optimization/storage]` | Absent-topic projection |
| adr KO | #4 replication | `[incident_response/mitigation]` | `[observability/metrics]` | Absent-topic projection (mitigation) → replaced with body-supported metrics |
| postmortem EN | #3 conn_pool | `[k8s/networking]` | `[k8s/scaling]` | Intra-vocab misclassification (HPA=scaling) |
| postmortem EN | #4 replication | `[search/cluster]` | `[incident_response/mitigation]` | Out-of-vocab expansion |
| troubleshooting EN | #3 replication | `[networking/load_balancing]` | `[observability/metrics]` | Absent-topic projection (LB never mentioned) |
| troubleshooting EN | #4 conn_pool | `[observability/logging]` | `[observability/metrics]` | Intra-vocab misclassification |

## Observations (evidence collected at Phase 2b)

### 1. Genre-pair confusability predictions — first evidence

Pre-experiment prediction (recorded in `b2-v2-handoff.md`): the pairs
**ADR↔postmortem** (rationalization vocabulary) and **runbook↔
troubleshooting** (procedure vs symptom-driven procedure) are most
likely to confuse rankers.

Phase 2b evidence points:

- ko troubleshooting genre-primary query → postgres/runbook top-1
  (both postgres-only and cross-topic runs). `runbook.md` vacuum chunk
  shares `수동 실행`/`확인`/`점검` tokens with troubleshooting
  diagnostic vocabulary. **2/2 consistent misses — runbook↔
  troubleshooting pair confirmed once in ko/postgres.**

- No ADR↔postmortem confusion evidence yet; both accuracies 4/4 in
  both experiments. Continue watching in subsequent topics.

Phase 5 confusion matrix either confirms or refutes the pair-level
prediction; pre-registered so it stays a prediction, not a
post-hoc narrative.

### 2. Graded secondary presence — first observation

Batch 5 adr KO #4 (Logical Replication ADR) had two body-supported
secondary candidates at noticeably different strength:

- **Strong presence**: "복제 지연 가능성을 감수" — replication lag is
  a core consequence of the decision, warranting
  `observability/metrics` (the measurement surface for lag).
- **Weak presence**: "정합성 이슈가 발생하면 재검토" — consistency
  issues appear only as a re-evaluation trigger, a brief mention at
  the end. Would warrant `data_pipelines/quality` but only weakly.

Current relevance model is binary (`primary=1.0`, `secondary=0.5`).
This observation suggests **graded secondary** (e.g. 1.0 / 0.7 / 0.4 /
0) may surface a discriminative signal that binary flattens. Not
proposing to implement now; recording as an Option-2 candidate for
Phase 5 calibration.

### 3. Topic boundary — KO troubleshooting anchor crosses topics

In the cross-topic experiment (64 chunks), the strengthened postgres
troubleshooting query `pg_stat_replication bloat 증상 의심 점검 진단`
returned `caching/troubleshooting` top-1 under both BM25 and dense.

Interpretation: the troubleshooting anchor vocabulary
(`증상`/`의심`/`점검`/`세요` family) is **topic-agnostic** — it binds
to the troubleshooting genre regardless of topic vocabulary. This is
positive evidence that genre anchors *exist* in the corpus; they just
lose to stronger topic signal in topic-strong topics like postgres.

Bleed rate overall: 1/16 = 6.3% (below the B-1 override threshold of
"medium+" — see `b2-v2-handoff.md`'s locked scenario preference).

## Curation ledger — Phase 2c cost_optimization (0 drift corrections / 32 chunks)

Drift rate 0/32 = 0%, an unexpected departure from the ~30% baseline
across Phase 1 (caching ko), 2a (caching en), and 2b (postgres). The
result is surprising but not post-hoc rationalized: hypotheses below
are pre-registered before the security-topic run, and
ruling-discrimination rules are locked. See "Security drift
pre-registration" subsection below.

### Category comparison (postgres 2b vs cost_opt 2c)

| Category | postgres 2b | cost_opt 2c |
|---|---|---|
| Out-of-vocab expansion | 1 | 0 |
| Intra-vocab misclassification | 4 | 0 |
| Absent-topic projection | 3 | 0 |
| Claude over-correction | 1 | 0 |
| **Total** | **9/32 (28%)** | **0/32 (0%)** |

The per-category breakdown matters more than the headline rate. H1
(structural cleanliness) only prevents the "intra-vocab
misclassification" row. H2 (prompt refinement with Phase 2b-derived
drift-to-avoid block) can prevent "intra-vocab" AND "absent-topic
projection" AND "out-of-vocab" — three rows. The fact that cost_opt
shows 0 across all four categories is asymmetric evidence for H2 —
but a single topic is not sufficient to discriminate. Security is
the discriminating test.

### Hypotheses (pre-registered, discrimination at security)

- **H1 (structural cleanliness dominant)**: cost_optimization
  subtopics (compute/storage/network/database/observability) map 1:1
  to AWS service categories, giving cleaner semantic boundaries than
  postgres's overlapping subtopics (indexing↔vacuum↔partitioning).
  Mechanism: reduces intra-vocab misclassification. Independent of
  absent-topic projection.
- **H2 (prompt refinement dominant)**: the Phase 2b-derived "Drift
  to avoid" block with 5 cost_opt-specific confusion examples acted
  as a prefilter. Mechanism: addresses intra-vocab AND
  absent-projection AND out-of-vocab simultaneously. Transferable
  across topics.
- **H3 (mixed, unequal weights)**: both H1 and H2 contribute; neither
  dominates.
- **H_null (pure chance)**: ruled out. Binomial P(0 drift | p=0.28,
  n=32) = 1.2e-5.

### Boundary-case principles (established Phase 3a cost_opt review)

**Principle 1 (Tool-function cluster rule)**

Body need not mention subtopic X literally if it mentions a
widely-recognized alternative or derivative Y of X, where the Y→X
relationship is explicitly documented in standard technical
references.

Verification protocol:
- *Primary source*: the tool/concept's own official documentation
  (README, docs site, RFC, spec)
- *Acceptable secondary source*: project wiki section titled as
  comparison/alternative to subtopic X
- *Not acceptable*: blog posts, Stack Overflow answers, AI-generated
  summaries, general "category" groupings (e.g., "both are caching
  tools" is insufficient)
- **Curator MUST cite the source in the ledger when invoking
  Principle 1.** Ledger entries without citation are invalid
  regardless of stated judgment. Citation-less invocations are the
  loophole; citation requirement is the bound.

Qualifying example: pg_repack → postgres/vacuum. pg_repack README
first line: "pg_repack is a PostgreSQL extension which lets you
remove bloat from tables and indexes... Unlike CLUSTER and VACUUM
FULL it works online." Self-documented as VACUUM FULL alternative.

Non-qualifying example: "DNS server" → networking/service_mesh.
General association only; service mesh specs do not list DNS as a
core primitive.

**Principle 2 (Functional split rule)**

When body describes problem-side and fix-side components separately,
both may be tagged as secondary even within the same parent topic
(e.g., `networking/load_balancing` + `networking/dns`). Body support
required for each side.

**Principle 3 (Same-topic secondary cap)**

At most 1 secondary may share the primary's topic. Body support
required. Example: primary = `cost_optimization/database`, secondary
may include one additional `cost_optimization/*` but not more.
Exceeding the cap is intra-topic over-tagging and dilutes the
"adjacent topic" signal that secondary is supposed to carry.

### Per-chunk boundary cases (cost_opt Phase 2c, 0 net corrections, 3 documented resolutions)

| Batch | Chunk | Primary | Secondary as submitted | Decision | Principle applied |
|---|---|---|---|---|---|
| runbook EN | #2 | cost_opt/storage | `[postgres/vacuum]` | kept | P1 (pg_repack cited above; VACUUM FULL alternative per project README) |
| troubleshooting KO | #3 | cost_opt/network | `[networking/load_balancing]` | **expanded** to `[networking/load_balancing, networking/dns]` | P2 (problem-side public ALB + fix-side CoreDNS both body-supported) |
| postmortem EN | #3 | cost_opt/database | `[postgres/partitioning, cost_optimization/storage]` | kept | P3 (1 intra-topic secondary within cap; gp3 volumes + pg_partman both body-supported; RDS spend line and EBS spend line are distinct AWS billing categories) |

### Subtopic freeze declaration (2026-04-17)

Corpus at 96 chunks (caching 32 + postgres 32 + cost_opt 32).
Subtopic list is locked: 15 topics × 5 subtopics = 75. No new
subtopics emerged during cost_opt generation; closed vocabulary
proved sufficient.

**Post-freeze amendment protocol**:
- If a chunk cannot be mapped to any existing subtopic, route to
  nearest closest entry and log in ledger as "unresolved gap".
- When unresolved-gap entries accumulate ≥ 3 distinct cases (same
  conceptual gap across ≥ 3 topics), trigger deliberate vocabulary
  expansion review — documented amendment with rationale, not
  emergency addition.
- Silent vocabulary expansion is the failure mode to prevent.

3-case threshold rationale: "1 case = individual judgment, 3 cases =
pattern." Arbitrary but explicit; prevents drift-by-accretion.

### Sensitivity spot-check outcome (measured 2026-04-17)

Measurement script: `/tmp/phase2c_cost_opt_sensitivity.py` (1:1 mirror of
`/tmp/phase2b_postgres_sensitivity.py`, only topic name + query prefix
changed). Determinism verified: 2 consecutive runs byte-identical.
Methodology anchor re-verified same session — postgres script
reproduced 0/8 divergence + 7/8 BM25 + 7/8 dense, confirming
environment stability.

**Results**: divergence 0/8 (0/4 KO, 0/4 EN), BM25 top-1 6/8, dense
top-1 6/8.

**Pre-registered outcome ruling**: counter-prediction realized.
cost_optimization reclassifies **topic-strong** (not topic-weak as
initially predicted). Full analysis in
`b2-v2-phase1-validation.md` § 11.

Key finding: the subtopic-vocabulary-density hypothesis
(`b2-v2-design.md` § "Topic-strong vs topic-weak") is **falsified**.
Both postgres (proper-noun-dense) and cost_optimization
(conceptually oriented) show identical 0/8 divergence. Revised
hypothesis: chunk-level artifact density dominates topic-level
vocabulary density — every chunk in both topics contains 2+
distinctive artifacts (commands, config keys, proper nouns) that
anchor BM25 and dense to the same chunk regardless of
`rrf_weights`. Security result will further discriminate; kafka
boundary case becomes less informative than originally designed
since the "proper-noun-heavy" distinction matters less.

### Formal definitions (Phase 2b-established, fixed across 14 topics)

**Divergence**:
```
For query q in language L against corpus C:
  top3_bm25(q)  = ordered top-3 chunk IDs with rrf_weights=(1.0, 0.0)
  top3_dense(q) = ordered top-3 chunk IDs with rrf_weights=(0.0, 1.0)
  diverge(q)    = 1 if top3_bm25(q) != top3_dense(q) else 0

Divergence rate per language = Σ diverge(q) / N_queries_per_lang
Aggregate reported as "X/N KO, Y/N EN" or combined "Z/2N total".
```

Post-hoc redefinition prohibited (e.g. "top-5 instead", "top-1 only",
"set equality instead of ordered list"). All 14 topics use this exact
definition.

**Drift** (curation-side, event-count convention from Phase 2d
observability forward):
```
For topic T, Phase 3a curator processes Gemini output with:
  N_chunks   = total chunks reviewed (8 batches × 4 = 32)
  N_events   = count of independent correction events (rules below)
  Categories = {out-of-vocab expansion, intra-vocab misclassification,
                absent-topic projection, Claude over-correction,
                missed secondary}

Correction event rules (2026-04-18, supersedes chunk-count):
- Same-parent-topic replacement (e.g. security/incident →
  security/access_control within one chunk) = 1 event,
  category = intra-vocab misclassification
- Cross-topic drop+add (e.g. cost_optimization/observability
  removed + observability/alerting added within one chunk) =
  2 events: 1 drop (absent-topic projection) + 1 add (missed
  secondary). The two observations are independent — body lacks A,
  body contains B — and are counted separately
- Drop-only, add-only, or primary reclassification = 1 event each

Drift rate per topic = N_events / N_chunks
Reported as "N events / 32 (P%)" plus chunks-affected count and
category distribution table.
```

Boundary cases resolved via Principles 1-3 (this section above) are
NOT counted as drift — they are documented as "decisions per
established principles" with the principle cited.

**Retrospective application note**: postgres (Phase 2b, chunk-count
9/32 = 28%) and security Gemini (Phase 2c, chunk-count 7/32 = 21.9%)
were counted under the pre-convention scheme. Convention change
applies prospectively from observability (Phase 2d) forward;
retroactive recount pending per Deferred decisions §
"Retrospective drift count audit".

### Security pre-registration — drift × divergence (updated 2026-04-17)

Before security Gemini run. Two independent measurements, jointly
interpreted. Bucket boundaries locked; no post-hoc redefinition.

**Drift prediction** (Gemini output vs closed vocab, Phase 3a):

| Hypothesis | Drift range | Interpretation |
|---|---|---|
| H1 (structural cleanliness dominant) | 10-20% | Structure explains drift prevention; cost_opt was a special case. Prompt refinement contributes weakly. |
| H2 (prompt refinement dominant) | 0-5% | Prompt refinement explains most gain; transferable across topics regardless of subtopic geometry. |
| H3 (mixed, unequal weights) | 5-10% | Both factors contribute; neither dominates. |

**Divergence prediction** (BM25 vs dense top-3 agreement, Phase 2c
sensitivity spot-check):

| Hypothesis | Divergence range | Interpretation |
|---|---|---|
| D1 (topic-strong consistent) | 0-1/8 | Security joins postgres + cost_opt topic-strong cluster. Chunk-level artifact candidate gains first support (n=3). |
| D2 (genre signal emerges, moderate) | 3-5/8 | Security chunks have less distinctive artifacts than postgres/cost_opt; genre anchors start to separate BM25 from dense. |
| D3 (genre signal dominates, strong) | 6-8/8 | Genre anchors fully emerge; security chunks lack chunk-level artifact anchors. Chunk-level hypothesis falsified if paired with H2 (prompt-healthy). |
| (D2 fallback: 2/8) | 2/8 | Boundary between D1 and D2; inconclusive, requires kafka for discrimination. |

**Joint H × D interpretation matrix**:

```
              H1 (structural)    H2 (prompt)       H3 (mixed)
           ┌──────────────────┬──────────────────┬──────────────────┐
       D1  │ structural + all │ prompt + all     │ mixed + all      │
           │ topic-strong     │ topic-strong     │ topic-strong     │
           │ (universal)      │ (universal)      │ (universal)      │
           ├──────────────────┼──────────────────┼──────────────────┤
      D2/3 │ structural +     │ PROMPT + GENRE   │ mixed + genre    │
           │ genre emerges    │ EMERGES          │ emerges          │
           │                  │ ← best for       │                  │
           │                  │ chunk-artifact   │                  │
           │                  │ FALSIFICATION    │                  │
           └──────────────────┴──────────────────┴──────────────────┘
```

Cells of interest:
- **(H2, D2/D3)**: prompt hypothesis + genre emergence is the **best
  discriminating outcome** for the chunk-level artifact candidate
  (see `b2-v2-phase1-validation.md` § 11.4). Prompt quality high
  but divergence ≥ 4/8 → chunk artifacts are NOT the dominant
  mechanism → candidate falsified.
- **(H1, D1)**: structural dominance + universal topic-strong =
  chunk-level artifact candidate retained (but H1 weighted
  heavily).
- **(H2, D1)**: prompt dominant + universal topic-strong =
  strongest candidate support; prompt quality + chunk artifacts
  both contribute to invariance.

**If H3 realized** (drift 5-10%), next discriminator = kafka per
original cadence. Recorded outcome rules:
- Kafka drift < 3%: H2 gains weight (cost_opt + kafka both low
  despite structural differences).
- Kafka drift > 10%: H1 gains weight (cost_opt was the outlier;
  structure matters).
- Kafka also 5-10%: H3 confirmed as stable baseline. Update
  methodology: accept 5-10% as expected rate, factor into Phase 5
  threshold calibration.

### Kafka cadence contingency (added 2026-04-17 post-cost_opt)

Original cadence positioned kafka as "boundary between proper-noun-
heavy (like postgres) and conceptual (like caching)" to maximize
information gain from the topic-strong/weak axis.

Cost_opt counter-prediction outcome **reduces kafka's information
value**: cost_opt (conceptual) already shows topic-strong, so kafka
(proper-noun-heavy + conceptual mix) is very likely to also be
topic-strong. Kafka shifts from "boundary test" to
"redundant-confirmation test".

**Contingency (security-contingent)**:
- If security also shows 0-2/8 divergence (joining topic-strong
  cluster): kafka's information value ≈ confirmation only. Consider
  replacing kafka with a candidate **alternative boundary topic**:
  - **observability**: genres map naturally to distinct activities
    (runbook = dashboard/query setup, postmortem = incident
    narrative, adr = tool selection, troubleshooting = alert
    fatigue) — potentially shows genre signal emerging.
  - **security** (if it already ran as topic-weak): kafka becomes
    strongest topic-strong confirmation; retain.
- If security shows 3-8/8 divergence: security IS the topic-weak
  data point. Kafka retains original boundary role.

**Do not swap kafka before security result** — sunk-cost bias
avoidance. Record both paths here so post-security decision is
informed, not ad-hoc.

**Realized 2026-04-17**: security sensitivity = 0/8 divergence
(joins topic-strong cluster). Contingency path triggered →
**kafka replaced with observability** in the Phase 2d slot.
Kafka moves later (post-observability + k8s) as redundant
confirmation. Full rationale in `b2-v2-phase1-validation.md`
§ 12.7.

## Observations (evidence collected at Phase 2c)

### 1. No intra-topic drift despite cross-topic vocabulary pull

Chunks discussing NAT Gateway costs frequently reference
`networking/*` subtopics (load_balancing, dns) as secondaries.
Chunks discussing RDS costs frequently reference `postgres/*`
(vacuum, connection_pool, partitioning). Neither pattern produced
drift — Gemini correctly placed primary in `cost_optimization/*`
and secondaries in the adjacent topic.

Possible reason: cost_optimization subtopics are about SPEND;
adjacent subtopics are about MECHANICS. The two roles don't collide
because they answer different questions ("how much does this cost?"
vs "how does this work?").

Predicts: topics whose subtopics are about the same axis (e.g.,
postgres/indexing and postgres/vacuum both about maintenance ops)
will show higher intra-topic drift than cost_optimization.

### 2. First legitimate intra-topic secondary (→ Principle 3)

Batch 8 #3 (postmortem EN, `cost_optimization/database` primary,
`cost_optimization/storage` secondary alongside
`postgres/partitioning`) is the first v2-corpus chunk with an
intra-topic secondary. Cause: AWS billing structure (RDS spend line
and EBS-under-RDS spend line are distinct). Principle 3 was
established specifically for this case. Track whether other topics
produce analogous patterns — if they don't, Principle 3 remains
used-once.

## Curation ledger — Phase 2c security, Claude-generated (design reference, superseded — see Gemini ledger below)

### Generator discontinuity — critical caveat

Security batches were **Claude Opus 4.7-generated**, not Gemini-
generated, unlike postgres (Phase 2b), cost_optimization (Phase 2c
first half), and caching (Phase 1/2a). Path decided on 2026-04-17
as Option (i) below — the Claude batches are retained under
`.claude/b2-v2-security-batches/` (gitignored) as design reference;
the Gemini-regenerated security batches (2026-04-17, under
`.claude/b2-v2-security-batches-gemini/`) are the authoritative
source for H1/H2/H3 testing and Phase 3b fixtures. See the
`## Methodology discontinuities` section for the full rationale
and alternatives considered.

The curation analysis below is on the Claude-generated batches as
a completeness exercise. Its drift rate does NOT contribute to the
pre-registered Gemini drift comparison (H1/H2/H3); that role
belongs to the Gemini ledger section below.

### Category distribution (Claude-generated, 4/32 = 12.5%)

| Category | Count | Example |
|---|---|---|
| Out-of-vocab expansion | 0 | — |
| Intra-vocab misclassification | 2 | `security/incident` tagged on CMK scheduled-deletion outage (ops error, not attacker action); `cost_optimization/storage` tagged on ADR mentioning KMS API request cost (not a storage-tier strategy) |
| Absent-topic projection | 0 | — |
| Claude over-correction | 0 | — |
| **Missed secondary (new 5th category)** | 2 | Body-supported adjacencies missed at initial generation (e.g., CloudTrail audit → `observability/logging` missed on the HSM ADR; IRSA root cause → `security/access_control` missed on the external-secrets troubleshooting) |

**On category #5 (missed secondary)**: formally established as a
drift category at security, 2026-04-17. Postgres (Phase 2b) and
cost_optimization (Phase 2c first half) were curated before this
category existed; their reported drift rates (28% and 0%) may
under-report missed-secondary instances. See the retrospective audit
task in "Deferred decisions".

### Per-chunk corrections (security Phase 2c, 4 total)

| genre × lang | # (primary) | original secondary | final secondary | Category |
|---|---|---|---|---|
| adr KO | #2 secrets | `[]` | `[observability/logging]` | Missed secondary — CloudTrail audit log centralization body-supported |
| adr EN | #3 encryption | `[cost_optimization/storage]` | `[security/access_control]` | Intra-vocab misclass — KMS request cost is not storage-tier optimization; per-tenant isolation + key-policy JSON are the body-supported `access_control` signals |
| troubleshooting KO | #1 secrets | `[observability/logging]` | `[observability/logging, security/access_control]` | Missed secondary — IRSA / node-role root cause body-supported |
| postmortem EN | #3 encryption | `[security/incident]` | `[security/access_control]` | Intra-vocab misclass — scheduled-deletion by a contractor's script = ops error near-miss, not breach / exploit / unauthorized-access. `security/incident` strict definition preserved; remediation explicitly scopes `kms:ScheduleKeyDeletion`, supporting `access_control` |

### Borderline cases preserved via Principle 2 (not counted as drift)

| genre × lang | # (primary) | secondary | Principle | Notes |
|---|---|---|---|---|
| runbook KO | #3 vulnerability | `[ci_cd/testing, observability/alerting]` | P2 (functional split) | Chunk describes a vuln-detection / management workflow (scan → triage → `.trivyignore` exception) with CI as the medium. The drift guide would swap primary to `ci_cd/testing` if the focus were CI wiring; the operational focus on vuln ops keeps `security/vulnerability` primary. `ci_cd/testing` already secondary — functional split acknowledged. |
| runbook EN | #3 vulnerability | `[ci_cd/testing]` | P2 (functional split) | Same pattern as the KO counterpart (`dependency-check` + `osv-scanner` operational workflow). |

### Rejected optional additions

| genre × lang | # (primary) | proposed add | rejected because |
|---|---|---|---|
| postmortem KO | #4 access_control | `+security/incident` | Body states "staging 컴파일 잡이 **실수로** write access 획득" — ops error, not attacker action. `security/incident` strict definition (breach / exploit / unauthorized access BY ATTACKER) not met. Preserves the same strict-definition boundary used in the postmortem EN #3 intra-vocab correction above. |

### Subtopic primary distribution skew (intentional)

```
security/vulnerability:   8/32
security/access_control:  8/32
security/encryption:      7/32
security/secrets:         7/32
security/incident:        2/32   ← skew
```

This skew is intentional, established at generation planning:
- `incident` framing is natural for postmortem genre only
- Forcing `incident` primary in adr / runbook / troubleshooting
  yields unnatural prose
- Rationale captured in `.claude/b2-v2-security-prompts.md` lines
  45-67 ("Primary-diversity enforcement for security"), preserved
  unchanged for the forthcoming Gemini regeneration

Implication for Phase 5 threshold calibration:
- incident-primary queries will match against 2 chunks only (vs
  7-8 for other subtopics)
- recall@10 for incident-primary queries has higher variance due to
  the smaller relevant set
- Threshold calibration must account; options:
  - (a) exclude incident-primary queries from floor assertions, or
  - (b) set a looser floor for incident-primary queries than other
    subtopics
- Decision deferred to Phase 5 calibration.

### Post-curation secondary distribution

All-secondary counts across 32 chunks (top entries):

```
ci_cd/testing:                   7
ci_cd/pipeline:                  5
observability/logging:           4
security/incident:               4   ← all in postmortem batches only
security/access_control:         3
observability/alerting:          2
auth/rbac:                       2
observability/metrics:           2
postgres/indexing:               1
auth/mtls:                       1
auth/oauth:                      1
networking/load_balancing:       1
incident_response/communication: 1
api_design/rest:                 1
ci_cd/deployment:                1
```

`security/incident` as secondary (4/32) is confined to postmortem
batches — no bleeding into adr / runbook / troubleshooting.
Postmortem incident framing preserved without primary dominance.

## Curation ledger — Phase 2c security, Gemini-regenerated (7 corrections / 32 = 21.9%)

### Generator transition executed (2026-04-17)

Per Methodology Discontinuity 1 decision (Option i), security batches
were regenerated with Gemini using the unchanged
`.claude/b2-v2-security-prompts.md` (prompt identity verified
structurally equivalent to cost_opt prompts: 5 legitimate
topic-specific differences, no Claude-idiosyncratic content). Gemini
output saved under `.claude/b2-v2-security-batches-gemini/`. This is
the authoritative source for H1 / H2 / H3 pre-registration testing
and for Phase 3b fixture conversion.

### Batch 7 rerun — literal-example-copy failure mode

Gemini's first attempt at Batch 7 (postmortem ko) returned all 4
chunks with literal `YYYY-MM-DD` placeholder strings copied from
the prompt's postmortem example tone ("At HH:MM UTC on YYYY-MM-DD").
Per operator workflow ("reject and rerun rather than hand-patching"),
Batch 7 was rerun; the second attempt produced real dates
(2023-11-12, 2024-02-08, 2021-12-11, 2024-05-19).

**New failure mode observed — "literal example copying"** — isolated
to one batch of eight on first attempt, not systemic. Other 7
batches produced real dates (Batch 8 English: 2023-10-15, 2023-11-02,
2023-12-01, 2024-01-10). Mitigation: future prompt runs may benefit
from explicit "replace YYYY-MM-DD with an actual date" instruction
if the pattern recurs; for now, rerun-on-detection is the documented
handling.

### Category distribution (Gemini-generated, 7/32 = 21.9%)

| Category | Count | Example |
|---|---|---|
| Out-of-vocab expansion | 0 | — |
| Intra-vocab misclassification | 6 | `security/encryption` primary on service-to-service mTLS chunks (2 instances); `security/encryption` primary on ingress / Let's Encrypt TLS cert rotation chunks (2 instances); `security/access_control` primary on Kubernetes RBAC `RoleBinding` chunks (2 instances) |
| Absent-topic projection | 1 | `k8s/storage` secondary on `ExternalSecrets` operator chunk — body discusses operator pod scheduling / control-plane overhead, not PV / PVC / volumes |
| Claude over-correction | 0 | — |
| Missed secondary | Not audited | Same limitation as postgres (Phase 2b) and cost_opt (Phase 2c first half); category was established at the Claude design-reference section above. Gemini security chunks are not re-audited for missed secondaries per the retrospective-audit task in Deferred decisions |

### H1 / H2 / H3 pre-registration outcome

| Hypothesis | Pre-reg range | Observed | Status |
|---|---|---|---|
| H1 (structural cleanliness dominant) | 10-20% | **21.9%** | **SUPPORTED** — just above range, but the postgres (28%) → security (21.9%) → cost_opt (0%) pattern is consistent with structural-difficulty explanation: both postgres (overlapping `indexing` / `vacuum` / `partitioning`) and security (overlapping `encryption` / `access_control` with adjacent `auth/*` and `networking/*`) produce meaningful drift, while cost_opt's sharp SPEND-vs-MECHANICS split produces minimal drift |
| H2 (prompt refinement dominant) | 0-5% | 21.9% | Rejected — would have predicted drift near-zero regardless of subtopic geometry |
| H3 (mixed, unequal weights) | 5-10% | 21.9% | Rejected — drift above the 5-10% band |

**Decision**: H1 supported at n=3. Cost_opt remains the exceptional
structural-cleanliness case; postgres and security are the baseline
structural-difficulty cases. Kafka cadence contingency (see
"Kafka cadence contingency" section) now resolves to the
**structural-confirmation path**: kafka is expected to produce
meaningful drift similar to postgres / security if its subtopics
(`producer` / `consumer` / `topic` / `connect` / `streams`) have
overlapping concerns, and minimal drift if they are cleanly
separated.

### Per-chunk corrections (security Phase 2c Gemini, 7 total)

| genre × lang | # (original primary) | original → final primary | original → final secondary | Category |
|---|---|---|---|---|
| adr EN | #1 encryption | `security/encryption` → **`auth/mtls`** | `[auth/mtls]` → `[networking/tls]` | Intra-vocab: service-to-service strict mTLS via cert-manager. Drift guide: "Service-to-service mutual auth = auth/mtls" |
| adr EN | #3 secrets | unchanged (`security/secrets`) | `[k8s/storage]` → `[]` | Absent-topic: body discusses operator pod overhead, not PV / PVC / volumes |
| runbook KO | #2 encryption | `security/encryption` → **`networking/tls`** | `[networking/tls]` → `[networking/load_balancing]` | Intra-vocab: ingress cert rotation + LB TLS setting upload. Drift guide: "Ingress TLS termination = networking/tls" |
| runbook EN | #1 encryption | `security/encryption` → **`auth/mtls`** | unchanged (`[networking/service_mesh]`) | Intra-vocab: Istio `PeerAuthentication` mTLS STRICT mode |
| trouble KO | #1 access_control | `security/access_control` → **`auth/rbac`** | `[auth/rbac]` → `[security/access_control]` | Intra-vocab: `RoleBinding` + `edit` cluster role — "Kubernetes RBAC Role/RoleBinding YAML = auth/rbac" |
| trouble KO | #2 encryption | `security/encryption` → **`networking/tls`** | `[networking/tls]` → `[]` | Intra-vocab: Let's Encrypt cert expiry, `certbot`, `Nginx reload` — transport-layer cert management |
| postmortem KO | #2 access_control | `security/access_control` → **`auth/rbac`** | `[auth/rbac]` → `[security/access_control]` | Intra-vocab: `cluster-admin` `RoleBinding` + namespace deletion + `view` role — RBAC-specific |

### Borderline cases preserved (Principle 2 or gray-area, not counted as drift)

| genre × lang | # (primary) | secondary | Note |
|---|---|---|---|
| adr KO | #4 vulnerability | `[ci_cd/testing]` | Trivy 0.44.0 CI-gate ADR; operational focus on vuln management. P2 functional split |
| runbook KO | #4 vulnerability | `[ci_cd/testing]` | `npm audit` + Trivy scanner workflow; same pattern as adr KO #4 |
| trouble EN | #1 access_control | `[auth/oauth]` | JWT `iss` claim debugging at API gateway; gray — access_control (policy) vs auth/oauth (mechanism) |
| trouble EN | #2 encryption | `[postgres/connection_pool]` | `sslmode=require` + `pg_hba.conf` debugging; drift guide "at-rest vs in-transit policies" lists in-transit policy under encryption, defensible |
| trouble EN | #3 vulnerability | `[ci_cd/testing]` | Trivy + `.trivyignore` CI-failure debugging; same P2 pattern |
| trouble EN | #4 secrets | `[networking/service_mesh]` | Vault agent CA injection failure manifesting as mTLS x509 error. Gray — secrets delivery mechanism vs mTLS validation symptom |
| postmortem EN | #3 encryption | `[networking/tls]` | cert-manager Let's Encrypt auto-renewal failure breaking mTLS. Gray similar to runbook EN #1; secondary=`networking/tls` already captures the functional split |

### Corpus composition after curation

```
Post-curation primary distribution (32 chunks, mixed after 6 reclassifications):

security/secrets:          8/32
security/vulnerability:    7/32
security/access_control:   6/32   (−2 reclassified to auth/rbac)
security/encryption:       4/32   (−2 → auth/mtls, −2 → networking/tls)
security/incident:         1/32   ← skew (trouble KO only)
auth/mtls:                 2/32   (from reclassification)
auth/rbac:                 2/32   (from reclassification)
networking/tls:            2/32   (from reclassification)
```

Total `security/*` primary: 26/32 (81%) after curation.
Non-`security/*` primary: 6/32 (19%) — Gemini classified these as
`security/*` during generation; drift guide places the underlying
mechanism in adjacent topics (`auth/*`, `networking/*`). The chunks
remain in the security batch directory (`corpus_v2/{ko,en}/security/`)
because they were generated in the security batch; the corrected
primary labels reflect true topic alignment per closed vocabulary.

This 81/19 split is a Gemini-behavior data point: Gemini blurs
topic boundaries toward the batch-announced topic. Claude (design
reference) produced 32/32 `security/*` primaries, less prone to
batch-topic-pull drift. Cross-generator observation worth tracking
as future topics are generated.

### Subtopic primary distribution skew — Gemini pattern

security/incident primary: **1/32 (3.1%)** — differs from Claude
design-reference (2/32). Specifically:

- Gemini placed `security/incident` primary in **Batch 5
  (troubleshooting ko) only** — an active SSH brute-force attack
  detected from `auth.log`.
- **Postmortem batches (7 and 8)** contain `security/incident` as
  **secondary** on 3 chunks (postmortem ko #1, postmortem en #1,
  postmortem en #2) but **never as primary**. Gemini appears to
  follow the postmortem caveat "do not let `security/incident`
  dominate" more strictly than Claude did — pushing it entirely to
  secondary status in postmortems.

Implication for Phase 5 threshold calibration (extends the Claude
observation): recall variance for incident-primary queries is even
higher than predicted (1-chunk relevant set vs 2). Threshold
calibration options:
- (a) exclude incident-primary queries from floor assertions, or
- (b) set a markedly looser floor for incident-primary queries.

Decision deferred to Phase 5 calibration.

### Pre-measurement (IDF + body overlap, 2026-04-17)

Per § 11.5 of the validation doc, query fairness (IDF token count
+ sum) and body overlap were measured via
`tools/retrieval-eval/compute_idf_baseline.py` before running
sensitivity at § 12. Topic token: `security` — canonical simple
pattern matching `postgres` / `cost` convention, inherited
unchanged from Phase 2b / 2c cost_opt.

**IDF fairness** — both languages within caching-baseline ± 15%:

| Lang | Mean tokens | Mean idf_sum | Status |
|---|---|---|---|
| ko | 6.25 (target 5.7-7.8, -7.4%) | 15.66 (target 12.67-17.14, +5.1%) | OK |
| en | 6.50 (target 6.4-8.6, -13.3%) | 12.85 (target 12.04-16.28, -9.2%) | OK |

"Weak query" confound excluded (query fairness rule per § "Locked
decisions from Phase 2b" in handoff).

**Body overlap** — 3 flagged queries (overlap ≥ 0.5):

| Lang | Genre | Overlap ratio | Note |
|---|---|---|---|
| ko | postmortem | 0.50 | Same structural pattern as postgres / cost_opt ko postmortem (both flagged 0.50) — one topic token overlaps the shared `장애` genre-event vocabulary. |
| en | postmortem | 1.00 | `security` appears in "applying the security patch" (Redis crypto-miner chunk). |
| en | adr | 1.00 | `security` appears in "security posture gained from mitigating issues like CVE-2023-44487" (auto-patching chunk). |

All other genre × lang combinations: 0.00 overlap (no flag).

**Interpretation per § 11.5**: The three flagged genres produce
"measurement-consistent but signal-confounded" divergence
readings. At § 12, if BM25 and dense concordantly pick the correct
genre top-1 for a flagged genre, the measurement remains valid as
"concordant miss direction" (same precedent as cost_opt adr
overlap=1.0 at § 11.5). If BM25 / dense diverge on a flagged
genre, overlap is an alternative explanation and the divergence
reading must be reported with the caveat.

**Ambient-vocabulary observation**: security EN has 2 genre flags
(vs postgres 0, cost_opt 1). `security` appears more ambiently in
operations/security prose than `postgres` or `cost` as proper-
noun topic tokens — "security patch", "security posture", etc. are
natural English collocations. Future topics with common-English
topic tokens (candidates: `networking`, `auth`, `observability`)
are expected to show similar EN-side flag inflation. Phase 5
threshold calibration should treat the ko ↔ en flag-count
asymmetry as a topic-vocabulary property, not a corpus defect.

### Observability pre-registration — drift × divergence (2026-04-17)

Before observability Gemini run. Two independent measurements, jointly
interpreted. Bucket boundaries locked; no post-hoc redefinition. Same
structure as security pre-registration above; observability-specific
interpretation captured below.

**Subtopic cluster** (`observability/{metrics, logging, tracing,
alerting, synthetic}`, per `b2-v2-design.md` § "Seed subtopics"): the
"three pillars" (metrics ↔ logging ↔ tracing) share operational-surface
vocabulary; alerting derives from metrics but has distinct notification-
channel vocabulary; synthetic is end-to-end probe vocabulary distinct
from the other four. Cross-topic pull from `cost_optimization/
observability` possible but expected to stay adjacent-not-drift
(SPEND-vs-MECHANICS rule from Phase 2c first half).

**Genre-activity mapping (first genre-boundary candidate)**:
- runbook = dashboard / PromQL / Grafana setup procedure
- postmortem = incident narrative with timeline / MTTR / five-whys
- adr = tool selection (Prometheus vs DataDog, Jaeger vs Tempo)
- troubleshooting = alert fatigue / missing signals / cardinality

Unlike postgres / cost_opt / security (where genre vocabulary was
less differentiated from topic vocabulary), each observability genre
maps to a distinct activity cluster. This is the mechanism motivating
D2 prior elevation.

**Drift prediction** (Gemini output vs closed vocab, Phase 3a):

| Hypothesis | Drift range | Interpretation |
|---|---|---|
| H1 (structural cleanliness dominant) | 10-20% | Three-pillars overlap (metrics ↔ logging ↔ tracing) is the primary intra-vocab pressure, similar subtopic geometry to security's encryption ↔ access_control overlap. Expected near security's 21.9% or slightly below. |
| H2 (prompt refinement dominant) | 0-5% | Prompt refinement transfer explains drift prevention independent of subtopic geometry. Would require observability to match cost_opt's 0% despite three-pillars overlap — unlikely at n=3 given security's 21.9%. |
| H3 (mixed, unequal weights) | 5-10% | Both factors contribute; neither dominates. |

**Divergence prediction** (BM25 vs dense top-3 agreement):

| Hypothesis | Divergence range | Interpretation |
|---|---|---|
| D1 (topic-strong consistent) | 0-1/8 | Observability joins topic-strong cluster (n=4). Chunk-level artifact candidate extends first-support but still k < 4 without falsifying cases (§ 11.4). |
| D2 (genre signal emerges, moderate) | 3-5/8 | **First D2 realization** in the v2 corpus. Universal topic-strong pattern (n=3 at 0/8 each) breaks; genre-activity vocabulary separates BM25 from dense. **Higher prior per handoff** — observability is the strongest genre-boundary candidate among remaining topics. |
| D3 (genre signal dominates, strong) | 6-8/8 | Genre anchors fully emerge; observability chunks lack the chunk-level artifact anchors that carried postgres / cost_opt / security to 0/8. |
| (D1/D2 boundary: 2/8) | 2/8 | Boundary; inconclusive, requires k8s for discrimination. |

**Joint H × D interpretation matrix**:

```
              H1 (structural)    H2 (prompt)       H3 (mixed)
           ┌──────────────────┬──────────────────┬──────────────────┐
       D1  │ structural +     │ prompt +         │ mixed +          │
           │ topic-strong     │ topic-strong     │ topic-strong     │
           │ (n=4 universal)  │ (n=4 universal)  │ (n=4 universal)  │
           │ ← chunk-level    │ ← strongest      │                  │
           │ retained, H1     │ chunk-level      │                  │
           │ heavy            │ support          │                  │
           ├──────────────────┼──────────────────┼──────────────────┤
      D2/3 │ structural +     │ PROMPT + GENRE   │ mixed + genre    │
           │ GENRE EMERGES    │ EMERGES          │ emerges          │
           │ (2-factor model) │ ← chunk-artifact │ (2-factor +      │
           │                  │ FALSIFIED        │ weak structure)  │
           └──────────────────┴──────────────────┴──────────────────┘
```

Cells of interest (differ from security pre-reg given observability's
genre-boundary role):
- **(any H, D2/D3)**: **first genre-boundary realization in the v2
  corpus.** Universal topic-strong pattern breaks at n=4; genre
  signal is real. Phase 5 confusion matrix must include observability
  genre pairs as primary test cases.
- **(H2, D2/D3)**: chunk-level artifact candidate **falsified** (same
  cell as security pre-reg). Prompt-healthy + genre emerging means
  chunk artifacts are NOT the dominant mechanism. Candidate
  abandoned; reopen Phase 5 design.
- **(H1, D2/D3)**: structural difficulty + genre signal emerging.
  Two-factor model (structure + genre activity). Phase 5 threshold
  calibration adopts both axes.
- **(any H, D1)**: observability joins topic-strong cluster.
  Chunk-level artifact candidate extends to n=4, still below k ≥ 4
  confirmation. Continue with k8s for n=5.

**Post-observability decision rules** (pre-registered to avoid post-
hoc narrative fitting; mirrors handoff § "Phase 2d next actions"
Step 9):

- **0-2/8 (D1 realized)**: topic-strong cluster extends to n=4.
  Next: k8s for clean topic-strong confirmation, then kafka
  (confirmation-only per § 12.7). Chunk-level artifact candidate
  continues unconfirmed (k < 4 without falsifying cases).
- **3-5/8 (D2 realized)**: first genre signal. Confirm at k8s —
  if k8s also D2, genre signal is not observability-specific.
  Reopen structural-vs-artifact discrimination at Phase 5.
- **6-8/8 + H2 drift (0-5%)**: chunk-level artifact candidate
  **falsified**. Prompt quality explains prior invariance, not
  chunk artifacts. Redesign for Phase 5.
- **6-8/8 + H1 drift (10-20%)**: structural dominance + strong
  genre emergence. Two-factor model supersedes chunk-level
  candidate.

**Body-overlap pre-measurement expectation** (per § 11.5 ambient-
vocabulary observation in security pre-measurement): `observability`
is a common-English term; expect EN-side flag inflation similar to
`security` (possibly higher given frequency of "observability" in
operations prose). Flags are measurement noise, not corpus defect;
§ 12.5 concordance rule applies — if BM25 and dense concordantly pick
the correct genre top-1 on a flagged genre, measurement remains valid.

**Sunk-cost-bias guardrail**: all four decision paths recorded pre-
observability so the post-result interpretation reads off a cell, not
fits a narrative. Same principle as security pre-reg above.

## Curation ledger — Phase 2d observability, Gemini-generated (9 events / 32 = 28.1%, 8 chunks affected)

Event-count convention established at this topic — see § "Formal
definitions" above. Chunks affected and event count reported
separately; cross-topic drop+add counts as 2 events.

### Category distribution (event-count)

| Category | Count | Note |
|---|---|---|
| Out-of-vocab expansion | 0 | — |
| Intra-vocab misclassification | 2 | Both are k8s-subtopic misplacements: `k8s/scheduling` tagged on a Prometheus service-discovery chunk (scrape annotation, not pod placement); `k8s/networking` tagged on an otel-collector OOM chunk (memory, not networking) |
| Absent-topic projection | 6 | Majority category; 5 unsupported secondaries dropped + 1 cross-topic drop on B7 #3 |
| Claude over-correction | 0 | — |
| Missed secondary | 1 | B7 postmortem ko #3: body explicitly mentions "비정상 수치를 감시하는 알람 구축"; `observability/alerting` omitted by Gemini |
| **Total events** | **9 / 32 = 28.1%** | 8 chunks affected (B7 #3 contributes 2 events = absent drop + missed add, cross-topic) |

### Per-chunk corrections (8 chunks affected, 9 events)

| Batch | Chunk (primary) | Original secondary | Final secondary | Events | Category |
|---|---|---|---|---|---|
| runbook ko | #2 logging | `[k8s/storage]` | `[]` | 1 drop | Absent-topic — `Mem_Buf_Limit` is memory buffer, not storage volume / PVC |
| runbook ko | #4 alerting | `[incident_response/oncall]` | `[]` | 1 drop | Absent-topic — Slack webhook change + `amtool config check` + reload; oncall rotation / triage not mentioned |
| trouble ko | #2 logging | `[k8s/networking]` | `[]` | 1 drop | Absent-topic — "네트워크 지연" is generic latency, no CNI / service-mesh / pod-to-pod specifics |
| trouble ko | #3 tracing | `[api_design/rest]` | `[]` | 1 drop | Absent-topic — `traceparent` HTTP header propagation; REST API design (conventions, idempotency, etc.) not discussed |
| trouble ko | #4 alerting | `[incident_response/detection]` | `[]` | 1 drop | Absent-topic — alertmanager `group_interval` tuning = internal delay debugging, not IR detection phase |
| trouble en | #2 metrics | `[k8s/scheduling]` | `[]` | 1 drop | Intra-vocab — `kubernetes_sd_configs` + `prometheus.io/scrape` annotation = service discovery; `k8s/scheduling` covers pod placement, not discovery |
| postmortem ko | #3 logging | `[cost_optimization/observability]` | `[observability/alerting]` | 2 (drop + add, cross-topic) | Absent-topic (drop `cost_optimization/observability`; body has `log_level: debug` volume surge + `throttle` filter, no billing content) + Missed secondary (add `observability/alerting`; "비정상 수치 감시 알람 구축" body-supported) |
| postmortem en | #3 tracing | `[k8s/networking]` | `[]` | 1 drop | Intra-vocab — otel-collector OOM + `memory_limiter` threshold + memory limit 2Gi; memory / resource issue, not networking |

### Borderline cases preserved (not counted as drift)

| Batch | Chunk (primary) | Secondary kept | Rationale |
|---|---|---|---|
| adr en | #1 synthetic | `[observability/metrics]` | "primary indicator for our global health dashboard" = explicit dashboard mention; weak-but-defensible per body-supported rule. Same precedent as security trouble EN #2 `[postgres/connection_pool]` (Phase 2c Gemini ledger) |

### Pre-registered H1/H2/H3 outcome at observability

| Hypothesis | Pre-reg range | Observed | Status |
|---|---|---|---|
| H1 (structural cleanliness dominant) | 10-20% | 28.1% | **Rejected** — above range; observability-specific H1 wording ("near security's 21.9%") also exceeded |
| H2 (prompt refinement dominant) | 0-5% | 28.1% | Rejected |
| H3 (mixed, unequal weights) | 5-10% | 28.1% | Rejected |

**None of H1/H2/H3 fits observability at 28.1%.** Formal retirement
or reformulation **deferred to kafka measurement or Phase 5** — this
ledger records the observation without acting on the framework
decision. No "three-pillars overlap hypothesis supported" or similar
framing is warranted by this data.

### Observation (not pre-registered, tentative, to be tested at kafka)

Drift rates across topics measured so far:

| Topic | Phase | Convention | Drift |
|---|---|---|---|
| postgres | 2b | chunk-count | 28% (9/32) |
| cost_optimization | 2c | chunk-count | 0% (0/32) |
| security Gemini | 2c | chunk-count | 21.9% (7/32) |
| observability | 2d | event-count | **28.1% (9/32 events, 8 chunks)** |

Pattern: postgres and observability sit ~28%, cost_opt is an outlier
at 0%, security at 21.9%. Emerging hypothesis (not pre-registered):
baseline Gemini drift for this prompt structure may be ~25-30%, with
cost_opt as a topic-specific outlier explained by the AWS service
category cleanliness registered at Phase 2c cost_opt ledger.

**Not promoted to a working hypothesis.** kafka provides the next
data point; formal reformulation of H1/H2/H3 happens at that
measurement, not earlier. Recording as observation to prevent
post-hoc drift in future interpretation.

### Pre-measurement (IDF + body overlap, 2026-04-18)

Per § 11.5 of the validation doc, query fairness (IDF token count +
sum) and body overlap were measured via
`tools/retrieval-eval/compute_idf_baseline.py` before running
sensitivity at § 13. Topic token: `observability` — canonical simple
pattern inherited unchanged from postgres / cost_opt / security.

**IDF fairness** — both languages within caching-baseline ± 15%:

| Lang | Mean tokens | Mean idf_sum | Status |
|---|---|---|---|
| ko | 6.50 (target 5.7-7.8, -13.3%) | 12.85 (target 12.67-17.14, -9.2%) | OK |
| en | 6.50 (target 6.4-8.6, -13.3%) | 12.85 (target 12.04-16.28, -9.2%) | OK |

"Weak query" confound excluded (query fairness rule per § "Locked
decisions from Phase 2b" in handoff).

**Body overlap** — **0 flagged queries** (all 8 genre × lang
combinations at 0.00 overlap ratio).

| Topic | ko flags | en flags |
|---|---|---|
| caching (baseline) | 4/4 | 4/4 |
| postgres | 1/4 (postmortem 0.50) | 0/4 |
| cost_optimization | 1/4 (postmortem 0.50) | 1/4 (postmortem 1.00) |
| security | 1/4 (postmortem 0.50) | 2/4 (postmortem 1.00, adr 1.00) |
| **observability** | **0/4** | **0/4** |

**Pre-reg expectation mismatch**: observability pre-registration
§ "Body-overlap pre-measurement expectation" predicted "EN-side flag
inflation similar to security (possibly higher given frequency of
'observability' in operations prose)." Actual: 0 flags in both
languages. Gemini did not insert the topic token `observability` as
a body collocation (unlike security where "applying the security
patch" / "security posture" inflated EN flags). This is a benign
mismatch — cleaner measurement; § 12.5 concordance caveats do not
apply at observability.

### Divergence measurement (2026-04-18, D1 realized)

Measured: **0/8 divergence** (0/4 KO, 0/4 EN). BM25 top-1 7/8, dense
top-1 7/8. Concordant miss on KO runbook → `troubleshooting.md`
(same pattern as cost_opt / security EN runbook misses). Byte-
identical across 2 consecutive determinism runs.

**Cell readout**: D1 realized; topic-strong cluster extends to n=4
(postgres + cost_opt + security + observability). H1/H2/H3 all
rejected at 28.1% drift. Post-observability decision rule (0-2/8
path) selects k8s as next topic (clean topic-strong confirmation);
kafka retains confirmation-only role per § 12.7.

Full writeup: `b2-v2-phase1-validation.md` § 13.

## Methodology discontinuities

This section tracks points where measurement methodology changed
across topics, preventing direct cross-topic comparison. Each entry
records the discontinuity, its scope, and the decision made to
handle it.

### Discontinuity 1: Generator transition at security (2026-04-17)

**Scope**: Phase 2c security batches only (not postgres, cost_opt,
or caching).

**Trigger**: A prior session generated security batches using
Claude Opus 4.7 instead of Gemini (the generator used for postgres,
cost_opt, caching). This breaks the generator-uniform assumption
behind the pre-registered H1 / H2 / H3 drift hypotheses.

**Why it matters**: H1 / H2 / H3 buckets (10-20%, 0-5%, 5-10% drift
respectively) were calibrated against Gemini's drift distribution.
Claude's closed-vocab awareness is structurally different (prompt
is in-context; drift patterns differ). The measured 12.5% drift on
the Claude-generated batches falls into the H3 range by arithmetic
coincidence, but does NOT test H2 ("prompt refinement dominant")
nor H1 ("structural cleanliness dominant") as defined.

**Options considered**:

| Option | Cost | Pro | Con | Phase 5 implication |
|---|---|---|---|---|
| (i) Regenerate security with Gemini; current Claude batches retained as design reference only | ~2 h (Gemini run + curation; subtopic / artifact planning from Claude work is reusable prompt context) | Clean methodology; H1 / H2 / H3 testable at security | Claude generation effort partially sunk (subtopic planning retained) | `cost_opt` H1 / H2 / H3 pre-registration becomes testable at security — preserves the B.2 v2 falsifiability framework |
| (ii) Continue with Claude for remaining 10 topics (security + kafka + k8s + …) | Same Claude effort per topic going forward | No regeneration; faster cadence | postgres / cost_opt / caching become an isolated "Gemini subset"; H1 / H2 / H3 falsifiability abandoned | Two drift baselines maintained; Phase 5 threshold calibration needs cross-generator normalization; falsification framework lost |
| (iii) Generate Claude-security + Gemini-security pair | ~2 h (Gemini run) in addition to Claude work already done | Direct generator-bias quantification; most scientifically clean | Security becomes a 64-chunk asymmetric corpus; doubles security weight in cross-topic evaluation | Generator bias becomes a measured correction factor; security uniquely double-weighted |

**Decision**: **(i)** — confirmed 2026-04-17.

Rationale:
- Option (ii) abandons pre-registration falsifiability (the core
  of the B.2 v2 framework). Rejected.
- Option (iii) produces an asymmetric 64-chunk security corpus,
  distorting cross-topic evaluation weights. The
  generator-bias information is not worth the distortion.
  Rejected.
- Option (i) reuses the Claude subtopic / genre / artifact planning
  as Gemini prompt-context preparation (reducing per-topic effort)
  while preserving the H1 / H2 / H3 falsification paths.

**Execution**:
- Current Claude batches retained under
  `.claude/b2-v2-security-batches/` (gitignored) as design reference.
- Fresh Gemini-generated security batches will replace the Claude
  content when committed as fixtures.
- Prompt identity verified: `.claude/b2-v2-security-prompts.md` is
  structurally equivalent to `.claude/b2-v2-cost-optimization-prompts.md`
  with only topic-specific content substitution (verified
  2026-04-17 — 5 legitimate differences enumerated: topic label,
  Rule 6 example (Redis → TLS), Rule 2 intro wording, intra-vocab
  confusion bullets (cost_opt → security), JSON template topic
  prefix). Gemini run uses the prompt as-is, no reversion needed.

**Status**: **Complete** (2026-04-17). Gemini regeneration executed;
Batch 7 required one rerun for YYYY-MM-DD literal-example-copy
quality defect; all 8 batches ledgered. See
`## Curation ledger — Phase 2c security, Gemini-regenerated` above
for the authoritative drift measurement.

## Deferred decisions (Phase 3b / Phase 5 / Option 2 backlog)

- [ ] **Drift validator matrix (Phase 3b infra)** — three-tier rule
  set derived from accumulated curation ledger:
  - *Forbidden pairs* (auto-reject): `postgres/* primary → search/*
    secondary`, similar cross-domain vocab mixing
  - *Manual-review pairs* (flag, not block): `postgres/* primary →
    k8s/networking secondary` and similar ambiguous adjacencies
  - *Allowed pairs* (explicit greenlist): `postgres/* primary →
    observability/metrics secondary` — the common legitimate links

  Do not implement until at least 5 topics are ledgered; earlier
  automation fixes rules based on a biased small sample.

- [ ] **Graded secondary relevance (Option 2 for Phase 5)** — if
  Phase 5 calibration shows binary secondary presence under-
  discriminates (e.g. floor sensitivity too coarse), extend to
  `{1.0, 0.7, 0.4, 0.0}` graded presence. Evaluation trigger:
  per-type nDCG variance > per-type MRR variance (binary secondary
  would flatten the former).

- [ ] **Genre confusion matrix (Phase 5)** — compute full
  {4 genres × 4 genres × 2 langs} = 32-cell confusion across the
  final corpus. Verify the pre-registered prediction that ADR↔
  postmortem and runbook↔troubleshooting dominate off-diagonal
  mass.

- [ ] **Curator over-correction rate tracking** — maintain the
  "Claude over-correction" category count across topics. If it
  grows past ~10% of total corrections, relax curation criteria
  (current check is "body-supported evidence for every secondary";
  may need to be "body-supported OR strongly topic-adjacent with
  explicit rationale"). Phase 2c: 0 over-corrections (and 0 total
  corrections). If security is also low-drift, revisit whether
  the current criterion needs adjustment at all.

- [ ] **Principle 1 source-citation audit** — after 3 more topics,
  review ledger to verify every Principle 1 invocation cites a valid
  source per the verification protocol. If unverifiable invocations
  accumulate, tighten the protocol further (e.g., require explicit
  quote from the source rather than just a reference).

- [ ] **Retrospective missed-secondary audit (postgres + cost_optimization)**
  — **Trigger**: before Phase 5 threshold calibration, OR after
  kafka topic completion, whichever comes first.
  **Rationale**: "Missed secondary" was established as the 5th
  drift category at security (Phase 2c, 2026-04-17). Postgres
  (Phase 2b) and cost_optimization (Phase 2c first half) were
  curated before this category existed; their reported drift rates
  (28% and 0%) may under-report missed-secondary instances. Phase 5
  cross-topic drift comparison requires comparable baselines.
  **Scope**: re-examine all 32 postgres chunks for body-supported
  secondaries not tagged at original curation; same for all 32
  cost_opt chunks; update ledger drift rates if missed secondaries
  are found; document "retrospectively audited YYYY-MM-DD, N missed
  secondaries found, corrected rates: postgres X%, cost_opt Y%"
  in the respective curation-ledger sections.
  **Effort estimate**: ~90 minutes (2-3 min / chunk × 64 chunks).
  **Not required for security PR merge**.

- [ ] **Retrospective drift count audit (postgres + security Gemini)**
  — **Trigger**: before Phase 5 threshold calibration, OR when n ≥ 5
  topics complete, whichever comes first.
  **Rationale**: event-count drift convention adopted at Phase 2d
  observability (2026-04-18). Postgres (Phase 2b, 9/32 = 28%) and
  security Gemini (Phase 2c, 7/32 = 21.9%) were counted under
  chunk-count; cross-topic drop+add cases in those topics may be
  undercounted under the new convention. Event-count and chunk-count
  can differ by up to 2× if a ledger contains several cross-topic
  replacements.
  **Scope**: re-examine per-chunk corrections tables in the postgres
  Phase 2b ledger (9 entries) and the security Gemini Phase 2c
  ledger (7 entries). For each row, determine whether it was a
  same-parent-topic replacement (1 event) or cross-topic drop+add
  (2 events). Recompute N_events / 32 for each topic. Update the
  drift-rates table in Phase 2d observability curation § "Observation"
  with the corrected numbers.
  **Effort estimate**: ~30 minutes (two ledger sections, ~16 rows).
  **Coordinate with the missed-secondary audit above** — same review
  pass can cover both conventions.

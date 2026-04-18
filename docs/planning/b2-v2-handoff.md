# B.2 v2 — Phase 3b closed, corpus scope narrowed to 6 topics, Phase 4 next

**If you are a new Claude session picking this up**: corpus building
is paused at 6 topics (caching + postgres + cost_opt + security +
observability + k8s, 192 chunks total) per user decision 2026-04-18.
The chunk-level artifact candidate hypothesis hit its k ≥ 4
confirmation threshold at k8s; further topics would be
confirmation-only. Phase 3b drift validator is implemented
(`tools/retrieval-eval/drift_validator.py` + 23 tests, commit
`eb25fd4`). **Next work = Phase 4 query portfolio across 6 topics**;
remaining 9 topics are deferred, not cancelled (trigger conditions
for resumption live in `b2-v2-design.md` § "Scope narrowed to 6
topics").

Reading order for full methodology context: this file first, then
`b2-v2-design.md` (incl. new § "Scope narrowed to 6 topics"),
`b2-v2-phase1-validation.md` (§§ 8-14 for Phase 2a-e measurements),
`b2-v2-phase2b-ledger.md` (curation patterns, formal definitions,
all topic pre-registrations, drift validator rules), and
`b2-v2-query-portfolio.md`.

## Branch state

- **Branch**: `feat/multilingual-regression-v2` (branched from `main`,
  un-pushed)
- **Exploratory branch**: `feat/multilingual-regression-mvp`
  (preserved as reference for why broad-tag MVP failed; un-pushed)

## Phase progress

| Phase | State | Deliverable |
|---|---|---|
| 1 | ✅ | caching × ko × 4 genres; dense/BM25 asymmetry + anchor mechanism validated |
| 2a | ✅ | caching × en × 4 genres; EN parity confirmed |
| 2b | ✅ | postgres × 4 genres × 2 langs = 32 chunks; topic-strong pipeline invariance identified (B-2) |
| 2c (cost_opt) | ✅ | cost_optimization × 4 genres × 2 langs = 32 chunks; **counter-prediction realized — topic-strong despite topic-weak prediction** (0/8 divergence); drift 0/32 |
| 2c (security) | ✅ | Gemini-regenerated; 32 chunks with 7 corrections = 21.9% drift (H1 supported, upper edge 10-20%); divergence **0/8** (D1 realized); joint cell (H1, D1) = chunk-level artifact candidate retained with H1 weighted heavily; § 12.7 decision: kafka → observability |
| 2d (observability) | ✅ | 32 chunks; drift 9 events / 32 = 28.1% (event-count convention adopted at this topic; absent-topic 6 + intra-vocab 2 + missed secondary 1); divergence **0/8** (D1 realized); H1/H2/H3 all rejected (28.1% above every band) — framework retirement/reformulation deferred to kafka or Phase 5 per (A)-path; genre-boundary candidacy falsified (D1 not D2) |
| 2e (k8s) | ✅ | 32 chunks; drift 13 events / 32 = 40.6% event-count (**Upper-outlier 32-45% realized**, 12.5 pp above observability's 28.1%); divergence **0/8** (D1 realized, cluster n=5 — chunk-level artifact k ≥ 4 threshold met, working-hypothesis promotion eligible); two systematic Gemini patterns (kubectl-logs / postmortem-genre conflations); Discontinuity 2 (KO tokenizer workaround, `k8s` → `kubernetes`); first non-deterministic top-1 in v2 (divergence stable) |
| 2d (kafka) | 🚫 skipped (user 2026-04-18) | originally confirmation-only per § 12.7; skipped per user — n=5 cluster already confirmed at k8s (k ≥ 4 threshold met), additional confirmation ROI low; H1/H2/H3 reformulation deferred to Phase 5 instead of kafka |
| 3b | ✅ | drift validator (`tools/retrieval-eval/drift_validator.py`) + 23 tests; 2 forbidden + 3 manual-review rules derived from 6-topic ledger; current corpus passes with zero violations |
| **scope narrowed** | **2026-04-18** | **corpus paused at 6 topics** per user; chunk-level artifact candidate k ≥ 4 threshold already met, further topics confirmation-only at diminishing ROI. Remaining 9 topics deferred (not cancelled — trigger conditions in `b2-v2-design.md` § "Scope narrowed to 6 topics") |
| **next** | **📋** | Phase 4 query portfolio (100 queries × 2 langs across 6 topics) |
| 3-7 | 📋 | query portfolio, calibration (incl. H1/H2/H3 retirement at Phase 5), sensitivity check, CI wiring, PR |

## Current state summary (2026-04-17, Phase 2c complete)

**Cost_optimization measurements**:
- Phase 3a drift: **0/32** (0%, vs postgres 9/32 = 28%). Category
  comparison table in `b2-v2-phase2b-ledger.md` § "Category comparison".
- Sensitivity divergence: **0/8** (0/4 KO, 0/4 EN). BM25 top-1 6/8,
  dense top-1 6/8. Full writeup: `b2-v2-phase1-validation.md` § 11.
- **Counter-prediction realized**: cost_opt was classed topic-weak
  (predicted 6-8/8 KO divergence); measured 0/8 → reclassifies
  **topic-strong**. Original "subtopic-vocabulary-density" hypothesis
  **falsified**.

**Security measurements (2026-04-17, closes Phase 2c)**:
- Methodology Discontinuity 1 resolved via Option i: security
  regenerated with Gemini for H1 / H2 / H3 testability.
- Drift: **7 / 32 = 21.9%** — H1 "structural cleanliness dominant"
  **supported** (upper edge of 10-20% range). Ordering: postgres
  28% → security 21.9% → cost_opt 0% correlates with subtopic-
  geometry overlap.
- Divergence: **0/8** — D1 "topic-strong consistent" realized.
  BM25 7/8, dense 7/8 (EN runbook concordant miss on
  troubleshooting.md; same failure mode as cost_opt EN runbook).
- **Joint cell (H1, D1) realized**: "structural dominance +
  universal topic-strong → chunk-level artifact candidate retained
  (H1 weighted heavily)". Two factors (structure + chunk-level
  artifacts) not mutually exclusive. Full writeup at
  `b2-v2-phase1-validation.md` § 12.
- Reclassification pattern: 6 intra-vocab reclassifications +
  1 absent-topic secondary drop. Post-curation: 81% `security/*`
  primary + 19% reclassified (`auth/mtls` ×2, `auth/rbac` ×2,
  `networking/tls` ×2).
- Subtopic skew: `security/incident` primary only 1/32 (Gemini
  pushes it to postmortem-secondary more strictly than Claude's
  2/32). Phase 5 threshold calibration decision deferred.
- Body-overlap flags (3 total: ko postmortem 0.50, en postmortem
  1.00, en adr 1.00) all produced concordant correct-direction
  top-1 — measurement valid per § 11.5.

**Revised candidate hypothesis** (n=3, first support reached):
chunk-level artifact density dominates topic-level vocabulary
density. Not falsified at security ((H2, D2/D3) discriminating
cell did not realize). Confirmation threshold (k ≥ 4 topics with
no falsifying cases) pending observability + k8s + remaining 9
topics. See `b2-v2-phase1-validation.md` § 11.4 for falsification
conditions, § 12.4 for status update.

**Boundary-case principles established** (Phase 3a cost_opt review),
applied to all future topics:
- **P1 (tool-function cluster)**: body need not mention subtopic X
  literally if widely-recognized alternative Y is mentioned; citation
  required in ledger per verification protocol.
- **P2 (functional split)**: both problem-side and fix-side may be
  tagged as secondary within same parent topic.
- **P3 (same-topic secondary cap)**: ≤ 1 secondary sharing primary's
  topic.

**Subtopic vocabulary frozen 2026-04-17**. 15 topics × 5 subtopics =
75. Amendment protocol in `b2-v2-design.md` § "Subtopic vocabulary".

**Formal definitions** (divergence, drift) locked across 14 topics
at `b2-v2-phase2b-ledger.md` § "Formal definitions". Post-hoc
redefinition prohibited.

**Security pre-registration** (joint H×D matrix) locked at
`b2-v2-phase2b-ledger.md` § "Security pre-registration".

## Phase 2c recap (closed 2026-04-17)

All Phase 2c steps complete across cost_opt + security:

| # | Step | Commit / ref |
|---|---|---|
| 1 | Cost_opt Gemini + curation + sensitivity (0/8) | 5b68bf7 |
| 2 | Security Gemini regen (Methodology Discontinuity 1 Option i) | `.claude/b2-v2-security-batches-gemini/` |
| 3 | Security Phase 3a curation (7/32 = 21.9% drift) | 5fbb47f (ledger) |
| 4 | Security IDF + body overlap pre-measure | 68b2f89 |
| 5 | Security Phase 3b fixtures (32 chunks) | 68b2f89 |
| 6 | Security sensitivity (0/8 divergence, 7/8 top-1) | this commit |
| 7 | § 12 joint (H1, D1) realization record | this commit |
| 8 | Kafka → observability decision | this commit (§ 12.7 + ledger "Realized") |

## Phase 2d next actions (in order)

1. **Read current state** — if new session, load these files in
   order: this handoff → `b2-v2-design.md` →
   `b2-v2-phase2b-ledger.md` (principles + formal defs + security
   pre-reg + Kafka cadence contingency realization) →
   `b2-v2-phase1-validation.md` § 12 (security close,
   kafka → observability decision) → `b2-v2-query-portfolio.md`.

2. ✅ **Pre-register observability joint H × D matrix** (DONE
   2026-04-17) in `b2-v2-phase2b-ledger.md` § "Observability
   pre-registration — drift × divergence". Locked: H1/H2/H3 drift
   ranges same as security (10-20% / 0-5% / 5-10%); D1/D2/D3
   divergence with D2 (3-5/8) prior elevated per handoff
   guidance; joint H × D matrix + cells of interest + post-
   observability decision rules + body-overlap expectation +
   sunk-cost-bias guardrail. No post-hoc redefinition.

3. **Run observability Gemini batches** — ✅ prompts created
   2026-04-17 at `.claude/b2-v2-observability-prompts.md` (gitignored;
   1836 lines, 8 fenced blocks). Structural equivalence verified
   against `.claude/b2-v2-security-prompts.md`: 5 legitimate deltas
   (topic label, Rule 6 example `Prometheus rate(http_requests_total
   [5m])`, intra-vocab confusion bullets = three-pillars + alerting/
   synthetic/cost_opt/detection boundaries, JSON template prefix,
   "This batch" genre/language per-batch field). No additional
   structural changes — Methodology Discontinuity 1 protocol held.
   **Next**: user runs 8 batches offline, returns JSON arrays →
   Phase 3a curation (Step 4).

4. ✅ **Phase 3a curate** observability batches (DONE 2026-04-18).
   8 chunks affected, 9 correction events under new event-count
   convention established at this topic (see ledger § "Formal
   definitions" drift block + § "Curation ledger — Phase 2d
   observability, Gemini-generated"). Category distribution: absent-
   topic 6, intra-vocab 2, missed secondary 1, out-of-vocab 0,
   over-correction 0. Pre-registered H1/H2/H3 all rejected at
   28.1% — formal retirement / reformulation deferred to kafka or
   Phase 5 per § "Observation (not pre-registered, tentative)".
   Retrospective chunk-count → event-count audit for postgres +
   security Gemini added to Deferred decisions.

5. ✅ **Pre-measure IDF + body overlap** (DONE 2026-04-18). IDF
   fairness OK both languages (KO tokens 6.50 / idf 12.85, EN
   tokens 6.50 / idf 12.85, within caching baseline ± 15%). Body
   overlap: **0 flags** both languages (pre-reg "similar to
   security" expectation not met — topic token did not collocate
   in body). Details in ledger § "Pre-measurement (IDF + body
   overlap, 2026-04-18)".

6. ✅ **Phase 3b fixture conversion** (DONE 2026-04-18). 8
   curated batches → `packages/memtomem/tests/fixtures/corpus_v2/
   {ko,en}/observability/{adr,runbook,troubleshooting,postmortem}
   .md`. 32 chunks total. Disclaimer line included per corpus_v2
   convention.

7. ✅ **Run observability sensitivity** (DONE 2026-04-18). Byte-
   identical across 2 consecutive `PYTHONHASHSEED=0
   OMP_NUM_THREADS=1` runs (timestamps differed, measurement
   blocks matched). Result: **0/8 divergence** (D1 realized),
   BM25 top-1 7/8, dense top-1 7/8. KO runbook concordant miss to
   troubleshooting.md.

8. ✅ **Record measurement** at `b2-v2-phase1-validation.md` § 13
   (DONE 2026-04-18). Cell readout only, no narrative refitting.

9. ✅ **Post-observability decision** applied: 0-2/8 → D1 path →
   **k8s next** (clean topic-strong confirmation), kafka after
   (confirmation-only per § 12.7). H1/H2/H3 all rejected at
   28.1%; framework retirement/reformulation deferred to kafka
   or Phase 5 per (A)-path (user-accepted 2026-04-18).

## Phase 2e next actions (k8s, routine cadence)

Per (A)-path agreement, k8s follows the established routine without
additional methodology meta-discussion. Deliverables per step
identical to observability unless the measurement surfaces a novel
failure mode.

1. ✅ **Pre-register k8s joint H × D matrix** (DONE 2026-04-18,
   commit `6e9ef6f`) in `b2-v2-phase2b-ledger.md` § "K8s
   pre-registration — drift × divergence". Locked: confirmation-
   test framing per (A)-path; drift bands Baseline-match 20-32% /
   Lower-outlier 0-15% / Upper-outlier 32-45% / Extreme; divergence
   D1 (0-1/8 expected) / D2 (3-5/8) / D3 (6-8/8); joint
   interpretation matrix + cells of interest + post-k8s decision
   rules + body-overlap expectation + sunk-cost-bias guardrail.
   H1/H2/H3 not re-applied (deferred to kafka or Phase 5).
2. **Run k8s Gemini batches** — ✅ prompts created 2026-04-18 at
   `.claude/b2-v2-k8s-prompts.md` (gitignored; 1934 lines, 8 fenced
   blocks). Structural equivalence verified against
   `.claude/b2-v2-observability-prompts.md`: 5 legitimate deltas
   (topic label `k8s`, Rule 6 example `kubectl rollout status
   deployment/api -n prod`, intra-vocab confusion bullets =
   scheduling/scaling + rollout/ci_cd + networking/service_mesh +
   storage/postgres + scheduling/cost_opt boundaries, JSON template
   prefix `k8s/<subtopic>`, "This batch" genre/language per-batch
   field). No observability-specific content leaked (three pillars,
   fluent-bit, Prometheus rate, blackbox-exporter, histogram_quantile
   all absent). Methodology Discontinuity 1 protocol held. **Next**:
   user runs 8 batches offline, returns JSON arrays → Phase 3a
   curation (Step 3).
3. ✅ **Phase 3a curate** (DONE 2026-04-18). 11 chunks affected, 13
   events (40.6% event-count) → **Upper-outlier band (32-45%)**
   realized, above observability's 28.1% by 12.5 pp. Two systematic
   Gemini patterns drove 6/13 events: (1) `kubectl logs` diagnostic
   conflated with `observability/logging` subtopic (3× in trouble /
   runbook batches); (2) postmortem genre conflated with
   `incident_response/postmortem` subtopic (3× in postmortem
   batches). Both are Phase 3b drift-validator "forbidden pair"
   candidates. Category distribution: absent-topic 11, missed
   secondary 2, out-of-vocab 0, intra-vocab 0, over-correction 0.
   Chunk-count sensitivity check: 11/32 = 34.4%, still Upper-
   outlier (band-realization robust to convention). Full writeup at
   ledger § "Curation ledger — Phase 2e k8s, Gemini-generated".
4. ✅ **Pre-measure IDF + body overlap** (DONE 2026-04-18). IDF
   fairness OK post-Discontinuity 2 workaround (KO mean tokens 6.25
   / idf_sum 15.66; EN 6.50 / 12.85; both within caching baseline
   ± 15%). Body overlap: **3 KO flags** (postmortem 0.50, adr 0.50,
   troubleshooting 1.00) from `kubernetes` body mentions inserted
   for KO tokenizer fairness; 0 EN flags (Gemini didn't insert `k8s`
   as body collocation). All 3 KO flagged genres produced concordant
   correct-direction top-1 — § 12.5 rule satisfied. Details: ledger
   § "Pre-measurement (IDF + body overlap, 2026-04-18)".
5. ✅ **Phase 3b fixture conversion** (DONE 2026-04-18). 8 curated
   batches → `packages/memtomem/tests/fixtures/corpus_v2/{ko,en}/
   k8s/{adr,runbook,troubleshooting,postmortem}.md`. 32 chunks
   total. Disclaimer line included. 16 KO chunks received a
   `kubernetes` mention per Discontinuity 2 (1 per chunk).
6. ✅ **Run k8s sensitivity** (DONE 2026-04-18). Divergence **0/8**
   (D1 realized) stable across 4 determinism runs. BM25 top-1 and
   dense top-1 flip between 7/8 and 8/8 across runs — EN
   troubleshooting query "k8s likely root cause workaround symptom"
   alternates concordantly between `postmortem.md` (miss) and
   `troubleshooting.md` (correct). **Divergence is stable and is
   the primary measurement**; top-1 flip is measurement noise on a
   borderline tie-break. First non-deterministic top-1 in v2 series.
   Details: ledger § "Divergence measurement (2026-04-18, D1
   realized)" + validation doc § 14.6.
7. ✅ **Record at § 14** (DONE 2026-04-18). See
   `b2-v2-phase1-validation.md` § 14 "Phase 2e k8s measurements
   (D1 realized, Upper-outlier drift)". Joint cell **(Upper-
   outlier 40.6%, D1 0/8)** reads off pre-registered rule — kafka
   as confirmation-only; baseline observation (~25-30%) upper-edge
   exceeded at n=2 event-count; H1/H2/H3 reformulation deferred to
   kafka per (A)-path. Chunk-level artifact candidate reaches
   k ≥ 4 confirmation threshold (n=5 topic-strong with no
   falsifying cases) — working-hypothesis promotion eligibility
   met; formal promotion at kafka per (A)-path.
8. 🚫 **Kafka skipped** (user 2026-04-18). Originally confirmation-
   only per § 12.7; skipped because n=5 cluster already confirmed at
   k8s (chunk-level artifact candidate k ≥ 4 threshold met), and
   kafka was never decisive on new hypotheses. H1/H2/H3 retirement
   / reformulation decision moves from "kafka completion" to "Phase
   5" (updated commitment — items 9 and 12 below updated
   accordingly).
9. ✅ **Phase 3b drift validator** (DONE 2026-04-18, commit
   `eb25fd4`). `tools/retrieval-eval/drift_validator.py` + 23 tests
   at `packages/memtomem/tests/test_drift_validator.py`. Two tiers
   locked against the 6-topic ledger (forbidden: closed-vocab +
   `genre-postmortem-vs-ir-postmortem-subtopic`; manual-review:
   `kubectl-logs-diagnostic-vs-observability-logging`,
   `security-access-control-primary-with-rbac-body`,
   `security-encryption-primary-with-transport-body` with
   networking/tls+auth/mtls suppression). Greenlist deferred until
   a false-positive case appears. Current corpus passes zero
   violations. See ledger § "Deferred decisions" for rule details
   and design doc § "Drift validator" for the updated spec.
10. 📋 **Scope narrowed to 6 topics** (2026-04-18, user decision).
    Corpus paused at caching + postgres + cost_opt + security +
    observability + k8s (192 chunks). Remaining 9 topics (`ci_cd`,
    `auth`, `networking`, `ml_ops`, `data_pipelines`,
    `incident_response`, `api_design`, `kafka`, `search`) deferred
    as regression-test expansion candidates, not cancelled. Trigger
    conditions for resumption in `b2-v2-design.md` § "Scope narrowed
    to 6 topics". Phase 4-7 proceed on the 6-topic corpus.
11. 📋 **Phase 4 next**: query portfolio (100 queries per language)
    across 6 topics × 4 genres = 24 base queries, expanded by
    paraphrase / difficulty variants. Genre-primary axis required.
    KO primary, EN parity.
12. **H1/H2/H3 retirement / reformulation decision** at **Phase 5**
    (kafka skipped per user 2026-04-18 — decision venue moved from
    "kafka completion or Phase 5" to "Phase 5" only). Evidence base:
    observability 28.1% + k8s 40.6% both rejected all three bands;
    no additional topic measurement required to decide. Recorded in
    ledger § "Curation ledger — Phase 2d observability" "Observation
    (not pre-registered, tentative)" + § "Observation update" under
    Phase 2e k8s curation ledger.

## Key invariants (do not drift from these)

- Topic vocabulary is frozen at 15 topics (see `b2-v2-design.md`).
  No additions without explicit plan update.
- Subtopic vocabulary **frozen 2026-04-17** at 75 subtopics
  (15 topics × 5). Corpus: 128 chunks as of security close
  (caching + postgres + cost_opt + security, 32 each). No silent
  subtopic additions — amendment protocol in `b2-v2-design.md`.
- Genre-primary queries are a **required** Phase 4 axis (not deferred
  memo). Portfolio: 100 queries per language.
- KO is primary regression signal; EN is parity + best-effort.
- Do NOT introduce cross-cutting tags (`performance`,
  `data_consistency`, `high_availability`) — absorb into existing
  topic subtopics per `b2-v2-design.md` rules.
- Every fixture commit includes the `> Synthetic content for search
  regression testing — verify before adopting as runbook.` disclaimer
  at the top of the file.
- Curation (Phase 3a) is **mandatory human-in-the-loop**, not
  optional. Raw Gemini output has ~30% drift rate.
- AI attribution opt-in: include `Co-Authored-By: Claude` in commits
  per user's prior explicit approval for v2 PR work. Check with user
  before continuing this policy at the start of each new session.

## Locked decisions from Phase 2b (do not revisit without cause)

These are pre-registered commitments from the Phase 2b decision
process. They reduce rework if Phase 2c or later topics produce
similar stop-gates.

- **B-2 preference** (accept as pipeline-invariant subset) is the
  default response to any topic that produces divergence ≤ 3/8 with
  weak cross-topic bleed (< 10%). B-1 (halt + redesign) activates
  only if bleed ≥ medium. B-3 (regenerate corpus) is rejected.
- **Pre-experiment interpretation matrix** for any topic showing
  stop-gate results: lock the hypothesis predictions before
  running experiments so results read off a cell, not fit a
  narrative. See `b2-v2-phase1-validation.md` § 10.4 for the
  postgres example.
- **Query fairness rule**: strengthened queries must match the
  caching baseline on token count (± 15%) and IDF-weighted token sum
  (± 15%) to exclude "weak query" as a confound.
- **Genre-pair confusability predictions** (ADR↔postmortem,
  runbook↔troubleshooting) stay pre-registered until Phase 5
  confusion matrix either confirms or refutes them. Current evidence:
  runbook↔troubleshooting has 1 consistent miss in ko postgres
  (2/2 runs); ADR↔postmortem has 0 evidence so far.

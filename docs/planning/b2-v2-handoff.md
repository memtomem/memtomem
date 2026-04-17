# B.2 v2 — Phase 2d observability handoff for next session

**If you are a new Claude session picking this up**: read this file
first, then `b2-v2-design.md`, `b2-v2-phase1-validation.md`
(§§ 8-12 for Phase 2a-c measurements — § 11 is cost_opt
counter-prediction, § 12 is security (H1, D1) realization +
kafka → observability decision), `b2-v2-phase2b-ledger.md`
(curation patterns + 3 boundary-case principles + formal
definitions + security pre-registration + Methodology
Discontinuity 1 [security generator transition] + Gemini-
regenerated security curation ledger + Kafka cadence contingency
with realized decision), and `b2-v2-query-portfolio.md` in that
order. That gives full methodology context + the current
open-prediction state.

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
| **2e (k8s)** | **🔄 next** | clean topic-strong confirmation per post-observability decision rule (ledger § "Post-observability decision rules" 0-2/8 path); expected divergence D1 0-1/8, expected drift under new baseline observation ~25-30% event-count |
| 2d kafka | 📋 | confirmation-only per § 12.7; runs after k8s |
| 2e onwards | 📋 | 9 remaining topics × 8 batches each |
| 3-7 | 📋 | full curation (per-topic drift), query portfolio, calibration, CI wiring, PR |

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

1. **Pre-register k8s joint H × D matrix** in ledger. Expected cell:
   D1 (topic-strong confirmation). Drift prediction under new
   baseline observation: ~25-30% event-count. Explicit "confirmation
   test" framing — no new hypothesis introduced.
2. **Run k8s Gemini batches** (8 prompts, topic-specific substitution
   from `.claude/b2-v2-observability-prompts.md` template; 5 legitimate
   deltas apply). Verify structural equivalence.
3. **Phase 3a curate**; log to new ledger section.
4. **Pre-measure IDF + body overlap**; add k8s QUERY set to
   `compute_idf_baseline.py` + dispatch.
5. **Phase 3b fixtures** at `corpus_v2/{ko,en}/k8s/`.
6. **Run k8s sensitivity**; add to `measure_sensitivity.py` QUERIES.
7. **Record at § 14**. Cell readout only.
8. **Then kafka** (confirmation-only).
9. **After topic 6 (k8s)**: implement Phase 3b drift validator rule
   tiers per ledger § "Deferred decisions". Earlier risks biased
   sample.
10. **Remaining 8 topics** after observability/k8s/kafka: ci_cd,
    auth, networking, ml_ops, data_pipelines, incident_response,
    api_design, + topic 15 per `b2-v2-design.md`.
11. **Phases 4-7** proceed once 15-topic corpus complete.
12. **H1/H2/H3 retirement / reformulation decision** at kafka
    completion or Phase 5 (whichever first). Recorded in ledger §
    "Curation ledger — Phase 2d observability" "Observation (not
    pre-registered, tentative)".

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

# B.2 v2 — Phase 2c → security handoff for next session

**If you are a new Claude session picking this up**: read this file
first, then `b2-v2-design.md`, `b2-v2-phase1-validation.md`
(§§ 8-11 for Phase 2a-c measurements — § 11 is the cost_opt
counter-prediction writeup; § 12 reserved for security sensitivity),
`b2-v2-phase2b-ledger.md` (curation patterns + 3 boundary-case
principles + formal definitions + security pre-registration +
Methodology Discontinuity 1 [security generator transition] +
Gemini-regenerated security curation ledger), and
`b2-v2-query-portfolio.md` in that order. That gives full
methodology context + the current open-prediction state.

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
| **2c (security)** | **🔄 3a done** | Gemini-regenerated (Methodology Discontinuity 1 → Option i); 32 chunks curated with 7 corrections = 21.9% drift (H1 supported); authoritative source `.claude/b2-v2-security-batches-gemini/`; IDF pre-measure + 3b fixtures + sensitivity + § 12 pending |
| 2c kafka / k8s | 📋 | kafka role may change post-security (see Kafka cadence contingency in ledger) |
| 2d onwards | 📋 | 11 remaining topics × 8 batches each |
| 3-7 | 📋 | full curation (per-topic drift), query portfolio, calibration, CI wiring, PR |

## Current state summary (2026-04-17, security Phase 3a complete)

**Cost_optimization measurements**:
- Phase 3a drift: **0/32** (0%, vs postgres 9/32 = 28%). Category
  comparison table in `b2-v2-phase2b-ledger.md` § "Category comparison".
- Sensitivity divergence: **0/8** (0/4 KO, 0/4 EN). BM25 top-1 6/8,
  dense top-1 6/8. Full writeup: `b2-v2-phase1-validation.md` § 11.
- **Counter-prediction realized**: cost_opt was classed topic-weak
  (predicted 6-8/8 KO divergence); measured 0/8 → reclassifies
  **topic-strong**. Original "subtopic-vocabulary-density" hypothesis
  **falsified**.

**Security Phase 3a measurements (Gemini-regenerated, 2026-04-17)**:
- Methodology Discontinuity 1 resolved via Option i: security
  regenerated with Gemini for H1 / H2 / H3 testability. Prior Claude
  Opus batches at `.claude/b2-v2-security-batches/` preserved as
  design reference only. See `b2-v2-phase2b-ledger.md` §
  "Methodology discontinuities".
- Drift: **7 / 32 = 21.9%** — H1 "structural cleanliness dominant"
  **supported** (postgres 28% → security 21.9% → cost_opt 0% pattern
  consistent with structural-difficulty explanation).
- Correction pattern: 6 intra-vocab reclassifications (encryption ↔
  `auth/mtls`, encryption ↔ `networking/tls`, access_control ↔
  `auth/rbac`) + 1 absent-topic secondary drop. Post-curation:
  81% `security/*` primary + 19% reclassified to adjacent topics.
- Subtopic skew: `security/incident` primary only 1/32 (Gemini pushes
  it to postmortem-secondary more strictly than Claude's 2/32).
  Phase 5 threshold calibration decision deferred (exclude incident-
  primary from floor assertions, or looser floor).
- **Sensitivity still pending** — n=2 → n=3 hypothesis update awaits
  `measure_sensitivity.py --topic security` result.

**Revised candidate hypothesis** (n=2, pending security
falsification): chunk-level artifact density dominates topic-level
vocabulary density. See `b2-v2-phase1-validation.md` § 11.4 for full
numerical falsification conditions.

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

## Phase 2c next actions (in order)

1. **Read current state** — if new session, load these files in
   order: this handoff → `b2-v2-design.md` → `b2-v2-phase2b-ledger.md`
   (principles + formal defs + security pre-reg) →
   `b2-v2-phase1-validation.md` § 11 (cost_opt counter-prediction) →
   `b2-v2-query-portfolio.md`. Do NOT run security Gemini until after
   reading.

2. ✅ **Run security Gemini batches** (2026-04-17). Regenerated with
   Gemini per Methodology Discontinuity 1 Option i after prior
   session generated with Claude Opus 4.7. Output at
   `.claude/b2-v2-security-batches-gemini/` (gitignored, 8 JSON
   files = 32 chunks). Prior Claude batches retained at
   `.claude/b2-v2-security-batches/` as design reference.

3. ✅ **Phase 3a curate** (2026-04-17). **7 / 32 corrections =
   21.9% drift** — H1 supported. 6 intra-vocab reclassifications to
   `auth/mtls` (2), `auth/rbac` (2), `networking/tls` (2) + 1
   absent-topic secondary drop. Full ledger at
   `b2-v2-phase2b-ledger.md` § "Curation ledger — Phase 2c security,
   Gemini-regenerated". P1/P2/P3 principles applied (see borderline
   cases subsection). Claude-generated section of the ledger
   superseded by Gemini section.

4. **Pre-measure IDF + body overlap** for security queries BEFORE
   divergence (Phase 2c-established rule, § 11.5). Build queries
   from chunks at `.claude/b2-v2-security-batches-gemini/`
   (authoritative source):
   ```bash
   # Add security queries to tools/retrieval-eval/compute_idf_baseline.py
   # QUERY_SETS, then:
   uv run python tools/retrieval-eval/compute_idf_baseline.py
   ```
   Target: body overlap < 0.5. Flag in ledger if ≥ 0.5.

5. **Phase 3b** convert curated batches to
   `packages/memtomem/tests/fixtures/corpus_v2/{ko,en}/security/*.md`.

6. **Run security sensitivity**:
   ```bash
   # Add security queries to tools/retrieval-eval/measure_sensitivity.py
   # QUERIES dict (follow cost_opt structure), then:
   PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
       tools/retrieval-eval/measure_sensitivity.py --topic security
   ```
   Run twice for byte-identical determinism check.

7. **Record measurement** in `b2-v2-phase1-validation.md` § 12 (new
   section). Read off the pre-registered H × D matrix cell; no
   narrative rationalization.

8. **Post-security decision points**:
   - If security shows 0-2/8 divergence: **revised hypothesis gains
     first support** (n=3). Consider replacing kafka with
     observability per `phase2b-ledger.md` § "Kafka cadence
     contingency". Decide explicitly.
   - If security shows 3-5/8: ambiguous; kafka retains original role.
   - If security shows 6-8/8 + H2 drift (0-5%): **revised hypothesis
     falsified**. Reopen design.

9. **Then kafka OR alternative boundary** (decision per step 8)
   following same cadence. Following k8s is topic-strong clean
   confirmation.

10. **Remaining 9 topics** after security/kafka/k8s: observability,
    ci_cd, auth, networking, kafka/streams, ml_ops, data_pipelines,
    incident_response, api_design. Most expected middle (3-5/8)
    but the cost_opt counter-prediction shows priors are weak.

11. **After topic 5** (postgres + cost_opt + security + kafka + k8s):
    implement Phase 3b drift validator rule tiers from accumulated
    ledger. Earlier than that risks fitting to biased sample.

12. **Phases 4-7** proceed once 15-topic corpus complete.

## Key invariants (do not drift from these)

- Topic vocabulary is frozen at 15 topics (see `b2-v2-design.md`).
  No additions without explicit plan update.
- Subtopic vocabulary **frozen 2026-04-17** at 96 chunks (caching 32
  + postgres 32 + cost_opt 32). 15 topics × 5 subtopics = 75. No
  silent additions — amendment protocol in `b2-v2-design.md`.
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
  before continuing this policy if Phase 2c generation spans a long
  pause.

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

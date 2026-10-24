# ADR-0039: Memory stewardship — detection is deterministic, judgement is delegated, server-made changes are reviewed

**Status:** Proposed
**Date:** 2026-10-24
**Extends:** [ADR-0038](0038-goal-records-scope-by-namespace.md) §6 — the one
path by which a goal changes without someone calling a goal tool directly

## Context

Keeping a memory store accurate over months needs judgements that string
matching and vector distance cannot make. Is a new note a *replacement* for an
old one, a *refinement* of it, or a duplicate? Has a session's work completed any
of a goal's criteria? memtomem already has the deterministic half of each, and
stops before the judgement.

- **Conflict detection labels shapes, not meanings.** `search/conflict.py` marks
  a neighbour `potential_conflict` when `dense_score >= 0.75` and token overlap
  is `< 0.3`, after ruling out a restatement (overlap `>= 0.6`). Its own comment
  explains why it stops there: high dense score with low overlap "is equally
  consistent with a contradiction and with a paraphrase, so the reviewer — not
  this module — decides what the neighbour means". Candidate evidence is
  computed when it is read (`mem_candidate_evidence`), and that tool is
  read-only.
- **Formation classifies replacements and then ignores them.**
  `formation._classify` sets `operation = "supersede"` when a candidate's text
  matches `_SUPERSEDE_RE` (`replaced`, `supersedes`, `changed from`, `대체`,
  `변경`). `mem_candidate_review` approval branches on `destination` only, so a
  `supersede` candidate is written exactly like an `add`. Both producers
  initialize `matched_existing_ids` to `[]`, and nothing fills it.
- **The only durable way to retire a chunk is its file.** The indexer and the
  metadata-only re-index update write `valid_to_unix` from frontmatter, and
  nothing else writes it. A database-only change would be undone by the next
  re-index.
- **Chunk ids are a poor anchor for "this replaces that".** Unchanged chunks keep
  their ids across a re-index, because the differ restores them by content hash.
  Changed chunks get new ids, and a re-index that would delete an id a
  `chunk_links` row points at is refused with "explicit link migration is
  required". Anchoring a replacement on a chunk id would therefore make the old
  note uneditable, which is the opposite of what a correction needs.
- **The model is off by default, and no existing use of it decides what is
  true.** `llm.enabled` defaults to `false`, and `AppContext.llm_provider` is then
  `None`. `docs/guides/llm-providers.md` states that all core functionality works
  without it. The model is used for query expansion, entity extraction,
  auto-tagging, per-source AI summaries, session and consolidation summaries, and
  chunk context. Its output is always a query, derived text or metadata;
  auto-tagging can replace a chunk's tags when asked to overwrite. None of those
  paths uses a model verdict to retire a memory, or to declare that one memory
  replaces another.
- **Review-first is the existing contract for proposals.** Candidates never write
  long-term memory by themselves; only approval does, through a claim with a
  `write_uncertain` quarantine (`docs/guides/pinned-context.md`). The STM proxy's
  proposal tool states the same from the other side: only core-side approval may
  promote a proposal.
- **Session provenance can be incomplete.** Write events record a fixed
  descriptor and chunk ids, never the written prose. A session ended while writes
  were still draining is marked `provenance_incomplete`, and the session summary
  path treats such a session conservatively.

Free-text supersession has a further problem this ADR does not solve. A
day-file chunk often holds several facts, so retiring the chunk would also retire
facts that are still true.

## Decision

### 1. Who may change memory

| Role | Who | Writes memory |
|---|---|---|
| **Detect** | Deterministic code: the conflict labeller and the formation classifier. | Never. It produces labels and candidates. |
| **Judge and act** | The user, or the host agent working for the user. | Yes, through the tools a user already has: `mem_add`, `mem_edit`, the goal tools, `mem_consolidate` / `mem_consolidate_apply`, `mem_candidate_review`. |
| **Propose** | memtomem's own configured model, when enabled (§3). | Never. It inserts review candidates. |

The review queue governs **judgements the server originates**: candidates from
session scans, external proposals, and the model's proposals. A change the host
agent makes directly is a user action. It goes through the same gates as any
other user write, and through no extra queue. The host agent is already the
judge, so routing its own calls back through a queue for it to approve would add
latency and no review.

Two existing kinds of automatic write are outside this rule, and this ADR leaves
them unchanged. One is the maintenance policies a user enables, which the
scheduler runs for real, deletion included. The other is the session archive
and its auto-summary, written at session end. Both are automation the user
configured, not judgements about whether one memory replaces another. Neither
may touch a goal file (ADR-0038 §6).

### 2. A read-only briefing for the host agent

A new action, `steward_brief`, in the `formation` category, returns one compact
report:

- the goals ADR-0038 §4 would select for the given scope, namespace and agent,
  plus the conditions ADR-0038 §6 makes `goal_get` report;
- pending candidates with their `operation` and `destination`;
- which of those candidates have `potential_conflict` neighbours, and the stored
  model verdict when there is one (§3).

It writes nothing and calls no model. The host agent reads it, decides, and acts
through the tools in §1.

Candidates carry no scope or namespace binding today. The brief therefore lists
pending candidates for the whole store, as an operator-tier listing in the sense
of ADR-0036 §3. Scoping candidates is deferred (Open questions §3).

A goal whose file says `completed`, `superseded` or `paused` may still appear in
search until the end of its last valid day (ADR-0038 §6). The brief reads goals
through their files, and lists such a goal as history, never as a current goal.

### 3. The in-process model only proposes

Two uses are allowed. Each has its own switch, and both default to off.
Neither can be on unless `llm.enabled` is `true`.

- **Conflict verdicts** (`formation.llm_verdicts`). When a candidate is created —
  by a session scan or by an external proposal — the model judges each
  `potential_conflict` neighbour as `supersedes`, `refines`, `duplicate` or
  `unrelated`, with a one-line rationale. The verdict is stored with the
  candidate once. `mem_candidate_evidence` and the brief show the stored verdict
  and never call the model, so reading evidence stays read-only. The verdict
  informs a reviewer and changes no memory. Stored verdicts record the evidence
  version they were made under.
- **Goal-progress proposals** (`goals.progress_proposals`). At session end, for
  each goal that ADR-0038 §4 selects for the session's effective namespace and
  bound agent, the model may propose a patch: criteria to mark done, a new
  `next_step`, blockers to clear. The proposal becomes a candidate with
  `kind="goal_progress"` and `destination="goal"`.
  - The evidence is the content of the chunks the session wrote. The chunk ids in
    its write events are resolved inside the caller's ADR-0036 boundary.
  - No proposal is made when the session is marked `provenance_incomplete`, when
    none of its writes resolve, or when the goal file cannot be read or parsed.
    In each case the session-end response says why.
  - The proposal records the goal's `version` and content hash as read.
  - The switch is independent of `session_summary.auto`, its `min_chunks`
    threshold, and whether a manual summary was given.

For both uses, the privacy guard runs before anything is stored. It scans the
exact text that would be stored: the rationale, and, for a goal patch, the
serialized goal file it would produce. A refusal logs pattern names only.

A failure — a privacy refusal, a model error or a timeout — removes only what the
model was producing, never anything the deterministic path already produced:

- **A conflict verdict is optional evidence.** The candidate it would annotate
  came from a scan or an external proposal, and those do not depend on a model.
  So when the verdict fails, the candidate is still inserted, without a verdict,
  and records that its verdict is unavailable. The brief and
  `mem_candidate_evidence` show that state instead of a verdict.
- **A goal-progress proposal is the model's own output.** When it fails, no
  candidate is inserted.

Neither case is reported as a failure of the session or scan that triggered it.

No setting applies a model output without review.

### 4. Approving a goal-progress candidate

Approval is a `goal_update` (ADR-0038 §6) with the version and hash the proposal
recorded.

- If the goal has changed since, approval refuses, returns the current goal, and
  puts the candidate back to `pending`. The reviewer then re-reads the goal and
  either rejects the stale proposal or approves a fresh one.
- When the goal lives in `project_shared`, approval needs `confirm_project_shared`
  (ADR-0038 §8), on the MCP review action and on `mm review approve` alike.
- The goal file is the record (ADR-0038 §6). Once its write succeeds the
  candidate is approved, even if indexing then fails; the response says so.

### 5. `operation="supersede"` stays advisory

For every destination, `operation` remains text a reviewer reads. Approving a
`supersede` candidate writes exactly what approving an `add` candidate writes.
Retiring the old memory is a separate, explicit act. For a goal it is
`goal_resolve` with `superseded`; for a note, it is closing the note's
`valid_to` or editing it. `matched_existing_ids` stays unfilled.

### 6. What the implementation decides

This ADR fixes who may write and what each automated step may do. As in
ADR-0038 §9, contract details are settled in the implementation's own design
review before code. These include the brief's response shape and limits, prompt
and output schemas for the model, the verdict storage layout, timeouts, and the
configuration key names' final spelling.

## What this guarantee is, and is not

**It is a rule about where automated judgement stops.** Detection never writes,
briefing never writes, and the in-process model never writes past the queue.
Every judgement the server originates becomes a change only through one reviewed
approval, and for a goal that approval touches one file. User-enabled maintenance
policies and session archiving keep their existing behaviour (§1).

**It is not a second gate on the user.** A person, or the agent working for one,
changes memory through the ordinary tools and their ordinary gates.

**It is not supersession.** Nothing here links an old memory to its replacement
or derives one memory's status from another's. Retiring a fact remains an
explicit act on that fact's own file.

**It is not a learning loop.** Ratings collected by an external surfacer
(`not_relevant`, `already_known`) do not lower a chunk's importance here. Whether
those ratings measure usefulness is not established. Acting on them is a separate
decision.

## Consequences

- `mem_candidate_review` gains the `goal` destination branch. Every other branch
  is unchanged, so existing candidates approve exactly as before.
- `steward_brief` is one new registered action, with the same tool-count and
  help-budget obligations that ADR-0038 lists. Together with ADR-0038 that makes
  five.
- Candidates gain an optional stored verdict. Candidates created before this,
  or with verdicts off, simply have none.
- Quality Lab gains a `stale_leak_rate@k` metric: the share of top-k results
  under the default `as_of` that come from goals whose file says `completed` or
  `superseded`. It is defined by status, not by `valid_to`, so the same-day
  ranking tail that ADR-0038 §6 accepts is measured, not hidden.
- A deployment with `llm.enabled=false`, the default, gains the brief and the
  goal approval path. It makes no model calls.

## Open questions

1. **Replacement as one reviewed operation.** Retiring an old goal and creating
   its successor are two explicit calls (ADR-0038 §6). A single reviewed
   "replace" needs a reference to the old record that stays correct across tiers
   and duplicate ids, and a replaced state that survives whatever later happens
   to the successor. This is left open until the two-call form proves
   error-prone in use, or until a non-goal record kind needs replacement.
2. **Free-text supersession.** This reopens when either of two things happens.
   One is that something starts writing the assertion tables the store already
   has (`memory_assertions`, `assertion_edges`), which today have no callers
   outside their tests. The other is that a user who did not write this ADR asks
   for a free-text correction to retire an old note. Until then, a correction is
   a new note, and the old note's `valid_to` is closed by hand.
3. **Scoping and grouping in the brief.** Candidates have no scope or namespace,
   and the brief does not group similar notes spread across source files. Both
   wait for a concrete need, at which point candidate attribution and a bounded
   grouping detector are designed together.

## Alternatives considered

**Apply model verdicts automatically when the model is enabled.** Rejected. It
contradicts the review-first contract the formation queue exists for. A wrong
verdict would silently remove a true memory from context. The host agent that
could review it is already attached and paid for.

**A resident background curator inside the server.** Rejected. It would merge or
retire Markdown the user owns without being asked, and it would make the default
behaviour depend on a model, which `llm.enabled=false` promises it does not.

**Route the host agent's own changes through the queue.** Rejected. The host
agent would be approving its own proposals, which is latency without review. The
queue exists for changes nobody has judged yet.

**Compute conflict verdicts when evidence is read.** Rejected. It would turn a
read-only inspection tool into a model call, with cost and latency on every
read, and two reads could disagree.

**Stamp `valid_to_unix` on the old chunk in the database.** Rejected. The next
re-index rewrites it from the file. It also retires every fact in the chunk, not
just the replaced one.

**A `supersedes` `chunk_links` type.** Rejected. A link on a chunk id blocks the
re-index that editing the note would need, and the id still changes when the
note's text does.

## References

- [ADR-0011](0011-canonical-artifact-scope-hierarchy.md) §5 — the write gates every approval inherits.
- [ADR-0034](0034-session-touched-entities-derived.md) — session write provenance, the evidence source in §3.
- [ADR-0036](0036-id-addressed-access-scope-vs-namespace.md) — the boundary evidence resolution uses, and §3's operator tier.
- [ADR-0038](0038-goal-records-scope-by-namespace.md) — goal files, selection and the goal write path.
- Issue #2134 — review evidence for candidates, which stored verdicts extend.
- `docs/guides/pinned-context.md` §"Review-first candidates" — the claim, quarantine and recovery lifecycle.
- `docs/guides/llm-providers.md` — the model-off default.

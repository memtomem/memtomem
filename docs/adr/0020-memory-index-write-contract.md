# ADR-0020: Index-file write contract for `mm memory doctor --fix`

**Status:** Accepted (amended 2026-07-15: §1 line eligibility restated as
strict-grammar + all-links-dead, per-line skip replaces the blanket refusal;
§5 gains two apply-time clauses; span-based entry removal rejected — #1757.
Amended 2026-07-16: an unreadable index is a per-file `error`, never `clean` —
#1769; §1 gains a fourth unreadable shape — a wikilink-shaped label the raw
source cannot attribute is contested, never demoted on a guess — #1774)
**Date:** 2026-06-01 (amended 2026-07-15, 2026-07-16)
**Context:** `mm memory doctor` (shipped in #1170, documented in #1171,
`docs/guides/reference/organization-maintenance.md` §5) is **read-only by design**: it reports the
3-way drift between disk, the agent index/TOC file (`MEMORY.md` for a
`claude-memory` dir), and the searchable DB, but writes nothing. Its module
docstring and the reference guide both defer the curation `--fix` path to
"its own ADR" — this is that ADR.

The decision matters because memtomem **does not write index files today** —
they are agent-curated artifacts. The Claude Code memory hook owns `MEMORY.md`:
it appends pointer lines, trims the budget, and orders entries. A `--fix` that
edits `MEMORY.md` would be memtomem's *first* write to an artifact another
process actively curates. Without a contract, that write could fight the
agent's curation, reflow prose it shouldn't touch, or corrupt the file on a
crash. This ADR pinned the contract before any code landed; the Tier 2 `--fix`
implementation followed, and the 2026-07-15 amendment below repeated the
sequence for the per-line partition.

It layers onto ADR-0011 (`MEMORY.md` is the user-tier memory TOC in the
canonical-artifact scope hierarchy) and reuses the round-trip-preservation
discipline of ADR-0008 (lockfile) and ADR-0005 (force-reindex metadata
contract): a writer must preserve everything it did not deliberately change.

**Amendment (2026-07-15, #1757).** Real indexes were found packing several
entries — and prose around them — onto one line, and the original parser saw
only the first link per line (#1757: false orphans, a `broken_link` blind
spot, and whole-line deletion of lines that also carried live entries). #1760
widened `parse_memory_index` to read every markdown link on a bullet line, so
"one line = one entry" is no longer a parser-level given — it is a property
`--fix` must **verify per line** before splicing. This amendment restates §1's
eligibility rule in those terms, adds two apply-time clauses to §5, and
records the rejection of span-based entry removal. The splice mechanism (§2)
is unchanged. The design pass behind it is recorded on #1757 ("Design
decision: line-splice contract preserved, span removal rejected").

## Decision

### 1. Subtractive-only, provably-dead-only

`--fix` may **only delete** whole pointer lines whose every link is provably
dead. It never adds, reorders, re-wraps, reformats, or rewrites any line, and
never touches the DB. A link is *dead* when the doctor classifies it
`broken_link` with link-class `missing_target`: its target resolves *inside*
the memory root but points at a file that does not exist on disk.

As accepted, eligibility was stated for the one-entry-per-line shape the
harness itself specifies for `MEMORY.md`: a `- [title](target) — hook` line
whose single target is dead. **Amended 2026-07-15 (#1757):** the parser now
reads every markdown link on a bullet line, so eligibility is restated per
line.

`--fix` considers exactly the lines carrying **at least one `missing_target`
link** — the drift it exists to close. Every other line is simply left alone
and is not a `--fix` concern at all: a line of live pointers, prose, or a
heading is not "skipped", it was never a candidate. Among candidate lines,
the unit of deletion stays the **whole line**, and a candidate qualifies for
deletion **iff both** hold:

- **Strict grammar — the parse must account for the whole line.** The line is
  a single-line bullet (`-`/`*`) pointer entry as the CommonMark parser
  (#1760) reads it: one or more markdown links plus inert prose. Four shapes
  fail that test, and all four fail **closed** (the line is never eligible
  for deletion):
  - a list item that continues past its first line — deleting the line would
    strand the continuation as loose prose;
  - link syntax on the line that resolved to no link (an unclosed `[B](b.md`)
    — the line meant a pointer the grammar could not read;
  - a target the doctor will not resolve on a guess — a destination that is
    not a plain relative path (it carries a `?` query, a `%` escape, a `:`
    scheme, or whitespace) may name a file other than its literal text does,
    so its pointer is never *provably* dead;
  - a link whose wholly bracketed label the raw source cannot attribute — a
    genuine `[[memo]](note)` wikilink and an escaped `[\[memo\]](file.md)`
    pointer sharing a line decode to the same label, and the parse cannot say
    which occurrence is the wikilink, so none is demoted on the guess: every
    same-named label stays an entry, contested, and its line is never spliced
    (#1774).

  The last three are exactly the lines the doctor already reports as
  `ambiguous_index_line` at `warn`: something on them could not be read, and
  a splice must not act on a doubt. (#1757's design sketch stated this
  ambiguity test as a scan for markdown-significant characters left outside
  the parsed links, guarding a link *regex* that truncated
  `[Live](notes_(v2).md)` at the inner paren. #1760 shipped a real CommonMark
  parse instead, which subsumes that scan — balanced parentheses in a
  destination or in hook prose read cleanly and are *not* ambiguous. The
  contract is the parse-based test above, not the character scan.)
- **All links dead.** *Every* link on the line classifies `missing_target`.
  One live (or ambiguous, or out-of-root) link spares the whole line: splicing
  it would delete a pointer memtomem cannot prove dead, and carving the dead
  entry out of the line is span surgery this ADR rejects (see Considered &
  rejected).

A **candidate** that fails either test is **skipped and reported for manual
repair** — a dead pointer memtomem found but will not remove itself — while
qualifying candidates in the same file are still fixed. This is a per-line
partition: it replaces the interim whole-run refusal (#1758, frozen through
#1760) under which one non-conforming line blocked fixing the rest of the
file.

Skips are part of the report, not a silent omission, so the output must make
three outcomes distinguishable in both the human and `--json` surfaces:
nothing to do (no candidates), every candidate handled, and some candidate
left behind. A run that skips a candidate must **not** present as clean —
`--json`'s status has to separate "no dead pointers" from "dead pointers this
tool refuses to touch", and a partially-fixed file must name the lines it
left. The same applies to a candidate dropped at apply time by §5's
re-validation (a resurrected target, a multiplicity mismatch): it is reported,
not silently absent. The concrete payload shape is the implementing PR's to
design; what this contract fixes is that skipped candidates are always
visible and never counted as success.

Rationale: the agent owns curation; memtomem may only remove references it can
*prove* are dead. Deletion of a line holding *only* provably-dead pointers is
the one curation move that cannot conflict with the agent's intent — every
target on it is gone, so no correct version of the TOC keeps the line.

### 2. Byte-exact preservation of everything else

Every byte the file keeps must survive unchanged: surviving pointer entries
(including the trailing hook prose memtomem does not parse), prose, comments,
blank lines, headings, ordering, indentation, each surviving line's exact
end-of-line terminator (LF vs CRLF), and the file's trailing-newline state.
There is **no reflow, no whitespace normalization, no re-sorting, and no
budget trimming**. A `--fix` run on a file with zero `missing_target` links is
a byte-for-byte identity.

**Mechanism — splice, do not reconstruct.** `parse_memory_index` runs on
`text.splitlines()`, which *strips* line terminators, so `IndexEntry.raw` and
`ParsedIndex.other_lines` cannot by themselves preserve CRLF-vs-LF or the EOF
newline state. The contract therefore **forbids rebuilding the file from those
fields**. `--fix` uses the parser only to identify the *line numbers* of the
lines eligible under §1 (strict grammar satisfied, every link on the line
`missing_target`), then removes exactly those lines from the **original
text** — re-split with `splitlines(keepends=True)` (same 1-based indexing as
the parser's terminator-stripped split), drop the identified indices, re-join —
so every surviving line keeps its original terminator and the EOF state is
untouched.

The apply-time read **must be newline-preserving**: `Path.read_bytes().decode("utf-8")`
or `open(..., newline="")`, **not** `Path.read_text()`, which applies Python's
universal-newline translation and normalizes CRLF→LF *before* `splitlines`
could preserve it. (The Tier 1 report path reads with
`Path.read_text(encoding="utf-8")` — fine for analysis, but a Tier 2 writer
copying that pattern would silently rewrite a CRLF file to LF despite this
contract.) The Tier 2 implementation must ship a round-trip test proving
byte-for-byte identity on a no-`missing_target` fixture across LF and CRLF,
with and without a trailing newline.

### 3. Out of scope (explicitly NOT auto-fixed)

Tier 2 `--fix` is `missing_target` removal only. These doctor findings are
deliberately left to the human / agent:

| Finding | Why `--fix` won't touch it |
|---|---|
| `broken_link` / `outside_root` | A link escaping the root may be a typo'd path *or* an intentional out-of-tree reference. Removing it could drop a real pointer — ambiguous intent. |
| `ambiguous_index_line` | Something on the line could not be read (unresolved link syntax, a target carrying URI machinery, or a contested wikilink label — §1's strict-grammar failures). Reported at `warn` for manual repair; a splice must not act on a doubt. *(Row added by the 2026-07-15 amendment, #1757; contested labels added by the 2026-07-16 amendment, #1774.)* |
| `budget` | Trimming an over-budget TOC means choosing *which entries to cut* — prose judgement, i.e. curation. |
| `index_orphan` | *Adding* a missing pointer requires generating a title + hook and choosing insertion order. Generation, not deletion. |
| `stale_source`, `convention_violation` | DB-side. Fixed by `mem_do(action="cleanup_orphans")` / `mm purge --matching-excluded`, not by editing the index file. |

### 4. Dry-run by default; `--apply` to write

`--fix` is a flag on the existing `doctor` command, not a new command — the
fix consumes the same analysis and stays co-located with the diagnosis. It
mirrors `mm purge`:

- `mm memory doctor --fix` — preview: print the exact lines that would be
  removed, per file. No write.
- `mm memory doctor --fix --apply` — perform the atomic write.

The default `mm memory doctor` and its `--json` payload (the #1170/#1171
contract) are **unchanged**: no flag, no write, no behavior change.

### 5. Atomic, concurrency-aware write — and its irreducible limit

- **Atomicity.** Reuse `memtomem.context._atomic.atomic_write_text`
  (`tempfile.mkstemp` in the same directory + `os.replace`, atomic on POSIX
  and Windows) so a crash mid-write can never leave a truncated `MEMORY.md`.
  The temp file's mode must be set to the **existing** `MEMORY.md` mode before
  the replace: `atomic_write_text` defaults new files to `0o600`, which would
  silently downgrade a typical `0644` index file, so the implementation stats
  the original and preserves its mode.
- **memtomem-vs-memtomem.** A concurrent memtomem invocation is serialized by
  the sidecar `_file_lock` / `_lock_path_for` pattern from the same module.
- **memtomem-vs-agent (the hard case).** The agent (the memory hook) writes
  `MEMORY.md` *without* taking memtomem's sidecar lock, so the lock cannot
  serialize against it (the TOCTOU limit recorded for sidecar locks vs.
  non-memtomem writers). `--fix --apply` therefore, all under the lock:
  1. **Re-reads the file fresh** (not the analyze-time snapshot), with the
     newline-preserving read required by §2.
  2. **Re-validates** each candidate against the fresh content + current disk:
     the line must still be present *and* still classify as `missing_target`
     (handles the agent having rewritten the file, or the target file having
     reappeared, since analysis). Lines that no longer qualify are skipped.
     **Amended 2026-07-15 (#1757)** — two clauses, matching §1's per-line
     eligibility:
     - **All links still dead.** A fresh line qualifies only if *all* of its
       links still classify `missing_target`. One resurrected target spares
       the whole line — the line-unit deletion of §1 applies at apply time
       too, not only at analysis time.
     - **Multiplicity guard.** Candidate matching is count-bounded on the raw
       line text, counted in **physical line occurrences**: a line's entries
       are collapsed by line number before counting, on both the analysis
       side and the fresh side — an all-dead line carrying two links is *one*
       occurrence, not two. Counts preserve *how many* copies survive, not
       *which* position survives. A raw line whose fresh occurrence count
       differs from its analysis-time count is therefore skipped entirely: when
       byte-identical lines sit in different sections, silently picking one
       to remove could delete the copy the agent meant to keep. The mismatch
       fails closed and is reported for manual repair.
  3. Builds the new text by splicing only the still-qualifying lines out of
     **that fresh content**, so entries the agent added before the lock are
     carried through, never dropped.
  4. Atomically replaces.

  **Reporting (settled by the implementing PR).** The `--apply` report is built
  from the fresh read, and describes the file the call leaves on disk: every
  dead pointer still in it is named — skipped under §1, dropped by the guards
  above, or written by the agent inside the window (not removable, since no
  analysis-time count bounds it, but not hidden either; a re-run clears it) —
  and nothing else is. A candidate the agent deleted or rewrote meanwhile is
  therefore *not* reported: it is not in the file, so there is nothing to
  repair, and naming it would print an analysis-time line number that now
  belongs to another line. This is what "reported, not silently absent" means
  for a file two writers touch: `clean` states that the index holds no dead
  pointers, not merely that this run lost track of one.

  This **bounds but does not eliminate** the race. `atomic_write_text` wraps
  `os.replace` internally, so there is no compare-and-swap at the replace
  point: an agent write landing in the window between step 1's read and step
  4's `os.replace` is lost. memtomem keeps that window minimal (no slow work
  between read and replace) but **cannot close it** without the agent honoring
  the same lock, which is outside memtomem's control.

  The residual risk is accepted because the edit is subtractive of a *provably
  dead* pointer (small blast radius) and the agent writes `MEMORY.md` at session
  boundaries rather than continuously (overlap is rare) — **not** because the
  loss is recoverable. Re-running `mm memory doctor` re-detects only
  *doctor-visible* fallout (e.g. a clobbered pointer to a file still on disk
  resurfaces as `index_orphan`); agent curation lost in that window — a
  hook-prose edit, a reorder, a budget trim, or the deletion of a still-valid
  pointer — is **not reconstructable** by memtomem. To keep the loss auditable
  rather than silent, `--fix --apply` must report the exact line(s) it removed
  per file (not only in dry-run), so a user who notices churn can recover from
  their editor/VCS/agent history. The contract explicitly does **not** claim
  "never clobbers."

## Consequences

- memtomem becomes a narrow, subtractive writer of `MEMORY.md`.
  `docs/guides/reference/organization-maintenance.md` §5 carries the
  remediation entry (per the doc-update-on-new-surface rule).
- The agent and memtomem can both write the file. The fresh re-read +
  re-validate (§5) carries agent additions through and skips resurrected
  targets; a residual sub-`os.replace` race remains and is accepted as
  low-probability and auditable (the `--apply` removal report), not eliminated
  — only doctor-visible fallout is re-detectable.
- The contract is testable as a docs-as-tests parity guard like #1171: a
  round-trip test asserts byte-exact identity on a no-`missing_target` file
  and asserts only the targeted lines disappear otherwise, and a guard asserts
  `--fix` acts on no link-class other than `missing_target`. Amended
  2026-07-15 (#1757) — the per-line partition adds its own pins: a mixed
  live/dead line survives byte-exact while eligible lines in the same file
  are removed; ambiguous and multiline lines are skipped *and reported*; and
  an apply-time multiplicity mismatch removes no copy of that raw line.
- **Amended 2026-07-15 (#1757) — contract-first, again.** As with the original
  ADR, this amendment landed before the code: `--fix` kept the frozen
  fail-closed semantics (#1758's whole-run refusal, write scope pinned to the
  pre-#1757 single-link shape through #1760) until the implementing PR, which
  shipped the per-line partition above together with the `--fix` section of
  `docs/guides/reference/organization-maintenance.md` §5 (it documents shipped
  behavior, so it kept the "refuses" wording until then). That PR's surface
  decisions, left open here: a skipped candidate exits `1` in both dry-run and
  `--apply`, and `--json` splits `status` into `clean` / `would-fix` / `fixed` /
  `would-partial` / `partial`, with per-file `skipped` entries naming the line
  and the reason.
- **Amended 2026-07-16 (#1769).** An index file `--fix` cannot read — a decode
  or I/O failure, in either the analysis read or the locked `--apply` re-read —
  is reported per file with an `error` message instead of silently dropping out
  of the report (which made the run claim `clean` about a file it never
  opened). `--json` `status` gains a sixth value, `error`, that takes
  precedence over `partial` / `fixed` / `clean` whenever any file hit it; one
  word covers dry-run and `--apply` alike (not reading the file is a condition,
  not an action — `applied` carries the run mode); `summary` gains an `errors`
  count; and the run exits `1`. The remaining dirs are still read, reported,
  and (under `--apply`) fixed. Only the *read* converts to a per-file error:
  lock, `stat`, and write failures keep propagating, so an exception after the
  atomic replace can never be misreported as `removed=[]` — §5's audit
  guarantee (every removed line is reported) survives, and §5's rationale
  ("clean" states the index holds no dead pointers, not merely that this run
  lost track of one) is applied at the file-read boundary: a file the run never
  opened supports no claim.

## Considered & rejected

- **Span-based entry removal** (proposed on #1757, rejected by the 2026-07-15
  amendment): give each entry a column span and splice only the dead entry's
  span out of a multi-entry line. Rejected because:
  - *The span is not well-defined.* Removing `[b](y.md)` from
    `- NS: [a](x.md) · [b](y.md) — hook` leaves a dangling ` · `. Stripping
    the adjacent separator means parsing free-form agent prose (` · `, `, `,
    `—`, parens) — exactly the reconstruction §2 forbids. Which prose belongs
    to which entry is a curation judgement this ADR assigns to the agent, not
    to `--fix`.
  - *Nested hook links make span ownership ambiguous.* On
    `- [Title](topic.md) — decision=NO-GO([rationale](other.md))`, whether the
    second link is a sibling entry or part of the first entry's hook is
    unanswerable from syntax. A line-unit fixer never has to answer it.
  - *Blast radius.* It would rewrite §1 (the unit of deletion), §2 (the splice
    mechanism itself), and §5 — including moving count-bounded re-validation
    off whole-line raw text, which breaks the "agent edited the line → line
    spared" property that makes the non-cooperating-writer race tolerable.

  Multi-entry lines are already non-conforming (the harness states one entry
  per line for `MEMORY.md`); auto-surgery inside them is high risk for
  near-zero demand. The amendment instead keeps line-unit deletion and gates
  it on §1's strict-grammar + all-links-dead test.
- **Full curation `--fix` (reflow, budget-trim, orphan-add).** Rejected: it
  fights the agent's curation, requires generation/judgement, and has a large
  blast radius on a file loaded into the agent's context every session.
  Subtractive-dead-only is the safe minimum that still closes a real,
  unambiguous drift.
- **Lock-only, write the analyze-time snapshot.** Rejected: the sidecar lock
  cannot serialize the agent, so replacing with the snapshot would drop
  concurrent agent additions. The fresh re-read + re-validate (§5) carries
  those through and bounds the loss to the irreducible sub-`os.replace` window
  — the safest achievable without agent cooperation, not a full fix.
- **Claiming the race is eliminated.** Rejected as dishonest: `atomic_write_text`
  offers no compare-and-swap at the replace point, and the agent does not honor
  memtomem's lock, so a residual window is unavoidable. The contract documents
  and accepts it rather than overstating the guarantee.
- **A separate `mm memory fix` command.** Rejected: it would duplicate the
  analysis and split remediation from diagnosis. `doctor --fix` reuses the
  report.
- **Including `outside_root` in Tier 2.** Rejected: intent is ambiguous (typo
  vs. deliberate cross-root link); deferred until there is a signal that a
  subtractive fix is wanted there.

## Open questions (deferred)

- **Tier 3 curation** — budget-trim, `index_orphan` add, and `outside_root`
  handling. Each needs generation/judgement the subtractive contract excludes.
  Trigger: a user report (or repeated doctor findings) that the
  `missing_target`-only fix demonstrably cannot resolve. Tracked in
  `docs/adr/TRACKER.md`.

## References

- `mm memory doctor` — `packages/memtomem/src/memtomem/cli/memory_doctor_cmd.py`
  (report-only Tier 1, #1170); `docs/guides/reference/organization-maintenance.md` §5 (#1171).
- Parser round-trip primitives: `parse_memory_index`, `ParsedIndex.other_lines`,
  `IndexEntry.raw` in the same module.
- Atomic write + sidecar lock: `memtomem.context._atomic`
  (`atomic_write_text`, `_file_lock`, `_lock_path_for`).
- ADR-0011 (canonical artifact scope — `MEMORY.md` is the user-tier memory
  TOC), ADR-0008 (round-trip preservation of unknown fields), ADR-0005
  (force-reindex metadata contract).

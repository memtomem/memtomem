# ADR-0020: Index-file write contract for `mm memory doctor --fix`

**Status:** Accepted
**Date:** 2026-06-01
**Context:** `mm memory doctor` (shipped in #1170, documented in #1171,
`docs/guides/reference.md` §5) is **read-only by design**: it reports the
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
crash. This ADR pins the contract before any code lands (the Tier 2 `--fix`
implementation is a follow-up PR).

It layers onto ADR-0011 (`MEMORY.md` is the user-tier memory TOC in the
canonical-artifact scope hierarchy) and reuses the round-trip-preservation
discipline of ADR-0008 (lockfile) and ADR-0005 (force-reindex metadata
contract): a writer must preserve everything it did not deliberately change.

## Decision

### 1. Subtractive-only, provably-dead-only

`--fix` may **only delete** pointer lines the doctor classifies as
`broken_link` with link-class `missing_target` — a `- [title](target) — hook`
line whose `target` resolves *inside* the memory root but points at a file
that does not exist on disk. It never adds, reorders, re-wraps, reformats, or
rewrites any line, and never touches the DB.

Rationale: the agent owns curation; memtomem may only remove references it can
*prove* are dead. Deletion of a provably-dead pointer is the one curation move
that cannot conflict with the agent's intent — the target file is gone, so no
correct version of the TOC keeps the line.

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
`missing_target` entries, then removes exactly those lines from the **original
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
  3. Builds the new text by splicing only the still-qualifying lines out of
     **that fresh content**, so entries the agent added before the lock are
     carried through, never dropped.
  4. Atomically replaces.

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

- memtomem becomes a narrow, subtractive writer of `MEMORY.md`. When the Tier
  2 `--fix` ships, `docs/guides/reference.md` §5 gains its remediation entry
  (per the doc-update-on-new-surface rule).
- The agent and memtomem can both write the file. The fresh re-read +
  re-validate (§5) carries agent additions through and skips resurrected
  targets; a residual sub-`os.replace` race remains and is accepted as
  low-probability and auditable (the `--apply` removal report), not eliminated
  — only doctor-visible fallout is re-detectable.
- The contract is testable as a docs-as-tests parity guard like #1171: a
  round-trip test asserts byte-exact identity on a no-`missing_target` file
  and asserts only the targeted lines disappear otherwise, and a guard asserts
  `--fix` acts on no link-class other than `missing_target`.

## Considered & rejected

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
  (report-only Tier 1, #1170); `docs/guides/reference.md` §5 (#1171).
- Parser round-trip primitives: `parse_memory_index`, `ParsedIndex.other_lines`,
  `IndexEntry.raw` in the same module.
- Atomic write + sidecar lock: `memtomem.context._atomic`
  (`atomic_write_text`, `_file_lock`, `_lock_path_for`).
- ADR-0011 (canonical artifact scope — `MEMORY.md` is the user-tier memory
  TOC), ADR-0008 (round-trip preservation of unknown fields), ADR-0005
  (force-reindex metadata contract).

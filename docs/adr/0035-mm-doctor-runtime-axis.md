# ADR-0035: `mm doctor` owns the runtime axis; the existing doctors keep theirs

**Status:** Accepted
**Date:** 2026-08-29
**Resolves:** [ADR-0010](0010-settings-hooks-target-scope.md) §"Alternatives
considered" (the deferred `mm doctor` naming question, parked again at :265 and
:309)
**Redirects:** [ADR-0032](0032-per-namespace-day-files.md) §"Migration" and
[ADR-0033](0033-force-reembed-vs-namespace-reassignment.md) §"Consequences" —
the mixed-namespace day-file split they park "for a possible `mm doctor` check"

## Context

memtomem grew three diagnostic commands independently — `mm memory doctor`
(disk/index/DB drift), `mm sync-doctor` (private sync repo), and
`mm context settings-doctor` (hook settings tiers). Each has its own result
model, its own exit convention, and no shared framework. ADR-0010 considered
unifying them under a top-level `mm doctor` group and declined, on the grounds
that it "expands the top-level CLI surface and creates parallel doctor
commands"; the naming question was left open.

#2226 then produced a class of problem none of the three can host. A host
accumulates `memtomem-server` processes — 88 holding 3.7 GB on one machine, 29
on another — and nothing in memtomem can see it. The data already exists: the
instance registry writes one flock-held sentinel per live server carrying pid,
parent pid, and store digest. What is missing is a consumer that reads it
*without* narrowing. `mm status` asks only about the current store;
`probe_all_for_uninstall` is all-store but reduces to a boolean refusal. Dozens
of servers spread across several stores therefore produce no output anywhere.

Two measurements shaped the decision. On the second machine every one of the 29
servers had a **live parent** — idle editor sessions left open for days — so an
orphan/PPID heuristic reported a clean machine while 17 servers were over a day
old and the oldest had been up 8.6 days. And the check cannot belong to any
existing doctor: it needs no configured store at all, which is exactly what the
other three are organized around.

## Decision

**Add a top-level `mm doctor` for the runtime axis. Do not refactor, absorb, or
delegate to the three existing doctors.**

The family is routed by *what a check must inspect*:

| needs to inspect | command |
|---|---|
| the host runtime — server processes, runtime directory; answers with zero stores configured | `mm doctor` |
| a configured store: memory_dir contents, index file, DB rows | `mm memory doctor` |
| the private multi-device sync repo working tree | `mm sync-doctor` |
| client settings and hook tiers | `mm context settings-doctor` |

Consequences of that rule:

- **ADR-0010's question is resolved** for the top-level name: `mm doctor` is
  claimed for the runtime axis. `mm doctor settings` as a peer alias for
  settings-doctor remains **not adopted** — it would create the parallel-command
  confusion ADR-0010 objected to, without moving any check to a better home.
- **The day-file split parked by ADR-0032/0033 belongs to `mm memory doctor`,
  not here.** It requires a configured store, walks memory_dirs, and wants a
  `--fix` — machinery `memory_doctor_cmd.py` already has and this command has no
  reason to grow. Those ADRs' forward references should be read as naming that
  command.
- **No shared result-model module.** With one consumer it would be speculative;
  the real cost of any future rollup is unifying renderers and JSON payloads,
  not moving a NamedTuple. `mm doctor` deliberately reuses `sync-doctor`'s
  status vocabulary (`pass`/`fail`/`warn`/`info` + glyphs) so that extraction is
  mechanical when a second consumer appears.

### Deferred, with the name kept compatible

A future `mm doctor` may grow *aggregate sections or flags* on the leaf command
(for example, rolling up the other doctors' findings) without renaming anything.
Converting it into a `click.Group` with axis subcommands (`mm doctor runtime`,
`mm doctor store`) is a **separate** compatibility decision and is not
authorized here — it would change the meaning of the bare `mm doctor`
invocation.

Also deferred, per #2226: any action on what the report shows. This command
never terminates a process and never garbage-collects a sentinel. Whether a
stranded server should be reaped, and by whom, is a process-lifecycle decision;
reaping an STM proxy's children belongs to the STM repo, which this one may not
depend on (`CLAUDE.md`).

## Reporting rules this fixes in place

- **Count and age are reported unconditionally**, never gated on a
  "looks abandoned" heuristic. The 29-live-parent machine is the reason: gating
  would have hidden the common case entirely.
- **Parent liveness is an annotation, not a verdict.** The field is named
  `recorded_parent` because a registration-time PPID proves less than
  "orphaned": on Windows the parent pid is never reparented, persists stale, and
  is aggressively reused, so an "alive" parent may be an unrelated process. A
  recorded PPID of 1 is inherently POSIX-only (Windows pids are multiples of
  four).
- **A live parent does not mean a server is in use.** An idle session holds one
  as firmly as an active one. Anything that later acts on this report needs
  idleness as its signal, not parenthood.
- **Exit code fails only on an unusable runtime directory.** Accumulation and an
  incomplete scan are warnings; a count alone cannot distinguish an abandoned
  server from a busy machine. Scripts should read `--json`.
- **The read is genuinely read-only.** It takes no mutation lock, collects no
  stale sentinel, and creates neither the runtime directory nor the registry
  sidecar — a diagnostic that mutates cannot be run twice to compare, and must
  not alter the machine it was asked to inspect.

## Known limitation

The report is only as complete as the registry. A server that failed to register
is invisible to it, and registration is best-effort: failures are logged and
dropped so that a coordination problem never blocks startup. Running this
command on the 29-server machine surfaced exactly that gap — only 2 sentinels
existed for 29 live servers — which is tracked separately. This ADR does not
change registration; it makes the discrepancy observable for the first time.

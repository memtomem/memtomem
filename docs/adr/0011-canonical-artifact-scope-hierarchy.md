# ADR-0011: Canonical artifact scope hierarchy (user / project shared / project local)

**Status:** Accepted
**Date:** 2026-05-09
**Context:** ADR-0010 introduced a 3-tier `target_scope` axis for settings
hooks. This ADR records whether and how to extend the same axis to the
remaining canonical artifact types — memory, agents, skills, commands —
and defines the staged migration. Source: PR #876 settings-migrate
landed today; the user request is "memory 도 같은 계층 구조로 개편하면
어때 + agents/skills 도 포함" — i.e., one scope axis for every canonical
artifact in memtomem.

## Terminology

The three scope values mirror ADR-0010 verbatim:

| `scope` value      | Resolved canonical path (per artifact type)                  | Tracked by git? |
|--------------------|--------------------------------------------------------------|-----------------|
| `user`             | `~/.memtomem/<artifact>/...`                                 | n/a (user home) |
| `project_shared`   | `<project>/.memtomem/<artifact>/...`                         | yes             |
| `project_local`    | `<project>/.memtomem/<artifact>.local/...`                   | no (gitignored) |

`project_shared` means "git-tracked", **not** "shared between agents" —
the latter is the orthogonal `shared` namespace that already exists for
memory (`agent-runtime:` / `shared` namespace conventions). The naming
is inherited from ADR-0010 to keep one vocabulary across the codebase;
the disambiguation is recorded here so future readers do not conflate
the two axes.

## Background

### What ADR-0001 §1 established

ADR-0001 §1 (`docs/adr/0001-context-gateway-sync-policies.md:9-37`) fixed
two related principles for agents / skills / commands:

- **Reverse sync runtime priority** is deterministic per artifact type
  (Claude → Gemini → Codex for agents; detector order for skills).
- **Fan-out is one-way (canonical → runtime)**, with canonical living at
  project scope (`<proj>/.memtomem/<artifact>/`). Codex agents fan out
  to project scope; Codex prompts remain user-scope only because there
  is no project-scope equivalent in the Codex runtime — explicitly the
  exception, not the principle.

The unstated default that emerges from §1: canonical = project-scope for
every artifact type. Memory followed the same default by convention
(single user-local SQLite at `~/.memtomem/`, with auto-discovered
provider dirs as the only "non-project" sources).

### What ADR-0010 changed

ADR-0010 (`docs/adr/0010-settings-hooks-target-scope.md`, Accepted
2026-05-09) introduced `target_scope: user / project_shared /
project_local` for settings hooks specifically, validated the 3-tier
model in this codebase (config field at
`packages/memtomem/src/memtomem/config.py:721`, plumbing across
`_claude_target` / `target_file`, settings-migrate subcommand in
PR #876), and **explicitly excluded memory data** from its scope
(ADR-0010 line 23–31 background table). The deferral was deliberate:
the settings ADR was already large; memory and other canonical
artifacts could ride on the same plumbing primitives in a follow-up.

### Why now

With ADR-0010 plumbing in tree, "canonical = project-scope only" for
memory / agents / skills / commands becomes the unjustified exception
rather than the principled choice. Two concrete user workflows are
foreclosed today:

1. **Team-shared project memory.** A team that wants to commit
   "rules for this codebase" or "patterns we've decided on" memories
   has no place to put them. The current single user-local SQLite
   accepts only personal memory; teammates have to re-enter the same
   facts on each developer machine, or paste them into `CLAUDE.md`
   (which is text-only and not searchable through `mem_search`).
2. **Cross-project personal agents / skills / commands.** A user
   with a personal "deploy-helper" skill that should apply to every
   project has to copy it into each project's `.memtomem/skills/`.
   Claude Code itself supports `~/.claude/skills/` as a user tier;
   memtomem's canonical layer does not expose a way to author there.

Settings (ADR-0010) solved the team-shared workflow for hooks. This
RFC solves it for the remaining four artifact types in one ADR so the
mental model is symmetric.

### Tier-merge assumption — sourced

For agents / skills / commands, Claude Code 2.x merges user-tier
(`~/.claude/<artifact>/`) and project-tier (`<proj>/.claude/<artifact>/`)
runtime entries additively at load time, the same way it merges
settings tiers (sourced: Anthropic Claude Code reference,
https://docs.claude.com/en/docs/claude-code/settings — referenced from
ADR-0010 Background "Tier-merge assumption — sourced"). A user-scope
canonical fanned out to `~/.claude/agents/foo.md` AND a project-scope
canonical fanned out to `<proj>/.claude/agents/foo.md` BOTH load. This
is symmetric with ADR-0010's settings tier-merge; the same caveat
applies (if a future Claude Code revision retracts the merge, the
double-fan-out becomes "user copy is dead config" — the tradeoff
shifts but the staged conservative default in §2 does not).

For project_local on non-memory artifacts, this RFC chooses **no
fan-out** by design (§3) — the runtime tier-merge does not apply
because project_local never reaches runtime.

## Decision

### 1. Adopt 3-tier scope axis for memory / agents / skills / commands

Mirror ADR-0010's `TargetScope` literal verbatim. Reuse the same type
(`Literal["user", "project_shared", "project_local"]` at `config.py:721`)
across all artifact types — one vocabulary, one validator, one parser.

The canonical / runtime layout per scope per artifact type:

| Artifact     | Canonical (user)                              | Canonical (project_shared)                       | Canonical (project_local)                                | Runtime fan-out per scope                                                         |
|--------------|-----------------------------------------------|--------------------------------------------------|----------------------------------------------------------|-----------------------------------------------------------------------------------|
| **memory**   | `~/.memtomem/memories/` + auto-discovered dirs | `<proj>/.memtomem/memories/`                     | `<proj>/.memtomem/memories.local/`                       | indexed into single user-local SQLite (no on-disk runtime mirror)                 |
| **agents**   | `~/.memtomem/agents/<name>.md`                | `<proj>/.memtomem/agents/<name>.md`              | `<proj>/.memtomem/agents.local/<name>.md`                | user → `~/.claude/agents/`; project_shared → `<proj>/.claude/agents/`; project_local → no fan-out |
| **skills**   | `~/.memtomem/skills/<name>/SKILL.md`          | `<proj>/.memtomem/skills/<name>/SKILL.md`        | `<proj>/.memtomem/skills.local/<name>/SKILL.md`          | user → `~/.claude/skills/<name>/`; project_shared → `<proj>/.claude/skills/<name>/`; project_local → no fan-out |
| **commands** | `~/.memtomem/commands/<name>.md`              | `<proj>/.memtomem/commands/<name>.md`            | `<proj>/.memtomem/commands.local/<name>.md`              | user → `~/.claude/commands/`; project_shared → `<proj>/.claude/commands/`; project_local → no fan-out |
| **settings** (ADR-0010, special case per ADR-0016 §2) | `<proj>/.memtomem/settings.json` (canonical is single regardless of tier) | ↑ same | ↑ same | `user` → `~/.claude/settings.json`; `project_shared` → `<proj>/.claude/settings.json`; `project_local` → `<proj>/.claude/settings.local.json` |
| **mcp_servers** (#1165, single-tier — see note below) | — (no user tier) | `<proj>/.memtomem/mcp-servers/<name>.json` | — (no local tier) | project_shared → `<proj>/.mcp.json` (`mcpServers` object); no user / project_local fan-out |

Settings inverts the tier ↔ residency mapping the other rows share: per
ADR-0016 §2, the `target_scope` axis on settings hooks selects the
**runtime fan-out target**, not the canonical residency (the canonical
is `<proj>/.memtomem/settings.json` by ADR-0010 §Background, regardless
of the configured `target_scope`). The other four artifacts in this
table couple canonical residency and runtime fan-out through
`RUNTIME_FANOUT_TABLE`; settings is the documented exception.

> **2026-06 (#1247):** The `mcp_servers` row was added when that surface
> shipped (#1165, post-dating this ADR). It is the only single-tier
> artifact: canonical exists at `project_shared` only, and the fan-out
> target is a *shared* file (`.mcp.json`) merged additively per server
> name rather than a per-artifact file/dir. Web write routes reject
> non-`project_shared` tiers for it like every sibling (see the ADR-0015
> §4c note); the dashboard's fifth tile envelope is recorded in the
> ADR-0009 §5 note.

### 2. v1 defaults preserve current behavior, zero behavior change for existing installs

| Artifact           | v1 default `scope` | Reason                                                          |
|--------------------|--------------------|-----------------------------------------------------------------|
| memory             | `user`             | Current behavior: writes go to `~/.memtomem/memories/`          |
| agents             | `project_shared`   | Current behavior: `mm context init` writes `<proj>/.memtomem/agents/` |
| skills             | `project_shared`   | Same as agents                                                  |
| commands           | `project_shared`   | Same as agents                                                  |
| settings (ADR-0010) | `user`            | Current behavior preserved per ADR-0010 §2                      |

No silent flip. Opting into a non-default scope requires explicit
`--scope=...` on the relevant command. Existing chunks in the SQLite
DB and existing canonical files are classified as their current
locations imply (§4 migration).

### 3. `project_local` for agents / skills / commands is a draft tier with no fan-out

Memory's `project_local` has a clear semantic — it's indexed into the
SQLite DB just like other scopes, just gitignored on disk. For
agents / skills / commands, this RFC chooses a deliberately different
semantic: **`project_local` is a per-checkout draft tier that memtomem
recognises but never fans out to a Claude Code runtime path.**

Rationale:

- Three plausible designs were considered: (a) draft tier with no
  fan-out; (b) memtomem-only loader fan-out to a parallel runtime path
  (`<proj>/.claude/agents.local/`) that Claude Code would not see;
  (c) symmetric additive merge fan-out into `<proj>/.claude/agents/`
  the same way `project_shared` fans out.
- (c) is rejected because two canonical sources fanning to the same
  runtime path would have to define a precedence rule (which file
  wins?); the additive-merge model the runtime applies *across tiers*
  does not apply *within a single tier*.
- (b) is rejected because Claude Code does not recognise an `.local/`
  variant for agents / skills / commands the way it does for
  `settings.local.json`. Inventing one creates a memtomem-only
  surface that confuses users coming in from the runtime side.
- (a) is the cleanest semantic. The canonical lives under
  `<proj>/.memtomem/agents.local/`, gitignored, indexed by
  `mm context status` for the user's own awareness, and explicitly
  invisible to the runtime. Promotion to `project_shared` is
  `git mv agents.local/X.md → agents/X.md` followed by `mm context
  sync`, which fans out the new project_shared canonical normally.

### 4. Memory storage stays single-user-local, schema gains a scope tag per chunk

Memory's SQLite DB at `~/.memtomem/memtomem.db` is derived state —
embeddings, FTS rowids, dedup hashes, chunk-link graph. Splitting the
DB across scopes would force per-scope schema evolution, fragment
dedup, and break `mem_agent_share` (chunk-links would need cross-DB
FKs). **One DB, per-row scope tag** is the only path that keeps the
existing dedup, sharing, and embedding contracts intact.

The `chunks` table gains two columns (idempotent ALTER, mirrors the
namespace migration at `storage/sqlite_schema.py:90`):

```sql
ALTER TABLE chunks ADD COLUMN scope TEXT NOT NULL DEFAULT 'user';
ALTER TABLE chunks ADD COLUMN project_root TEXT;
```

`project_root` is required because a single user-local DB can hold
chunks from multiple worktrees of the same project (or two different
projects whose `project_shared` dirs share a name). Without it,
sibling shared scopes collide on path-prefix lookups. Existing
UNIQUE index `(namespace, source_file, content_hash, start_line)`
does not need extension — `source_file` is absolute and disambiguates
worktrees naturally.

Migration is opt-in: existing rows default to `scope='user'`;
no user action is required to keep current behavior.

### 5. Privacy gates layered: hard refusal at the chokepoint, explicit-flag-and-confirm at the surface

Two gates fire on every project_shared write. A bug in either still
leaves one active.

**Gate A (chokepoint).** `privacy.enforce_write_guard`
(`packages/memtomem/src/memtomem/privacy.py:432`) gains a `scope` kwarg
and rejects `force_unsafe=True` when `scope == "project_shared"` —
hard refusal, not a warning. The decision string is
`blocked_project_shared`; the bypass audit log carries a special
marker so SOC/security pipelines can alert on attempts.

Why hard refusal: `project_shared` content goes into git history. Even
an instant `git rm` cannot retract it from any clone or reflog. The
trust boundary moves from "the user's machine" to "every clone of this
repo forever," so the bypass valve does not belong here.

> **2026-09 (#2374):** `MemtomemHybridStore` scans the persisted record's
> value JSON **and** the raw namespace labels, key, and per-item index selectors.
> The constructor also scans its persisted index configuration (`fields`,
> `dims`, `index_id`), including raw fields and index ID, after scope resolution
> and Gate B consent but before creating a directory, initializing an embedder,
> or opening SQLite. That check applies to reopen as well as creation.
> JSON escaping can hide a quoted credential in an identifier, so scanning a
> JSON envelope alone is insufficient; raw strings are included with
> non-whitespace boundaries to avoid matching across independent fields.
> Caller-owned mutable inputs are copied before scanning, and those copies
> supply the persisted record and its indexes even if an embedding callback
> changes the original selectors or value.
>
> This is a whole-operation refusal, never redaction or renaming of a routing
> key. A clean value cannot make a sensitive identifier safe: callers must
> remove the credential from that identifier or choose a private destination
> with the existing explicit `force_unsafe` valve. `project_shared` never allows
> that bypass. The existing patterns are reused without identifier exemptions;
> regression fixtures pin ordinary names (including `password` and `api_key`),
> paths, UUIDs, Unicode, and selectors, alongside synthetic credential cases.
> These fixtures calibrate known examples, not a production false-positive rate.
>
> One Gate A outcome is recorded per prepared write under
> `langgraph_hybridstore_put`, and per constructor check under
> `langgraph_hybridstore_init`; a passing scan does not prove a later SQL write
> succeeded. Gate B remains store-scoped. Delete (`value=None`) adds no content
> and remains unscanned, so legacy sensitive identities can still be removed.
> Existing rows are not scanned on reopen or automatically cleaned. A sensitive
> constructor configuration is refused on reopen; this change does not migrate
> it or alter the database. Batch retains per-operation commits, while a refused
> import leaves all records uncommitted.

For agents / skills / commands the corresponding chokepoint is the
`mm context sync` write path, when the canonical is `project_shared`
and the runtime fan-out is about to write. A new
`context/privacy_scan.py` helper runs `privacy.scan` on the canonical
content before each fan-out write; if hits exist and the canonical is
project_shared, the sync write is blocked regardless of `--force-unsafe`.

> **2026-06 (#1247):** Gate A also fires on wiki ingress — `mm context
> install` / `update` (incl. `--all` and the `--force` `.bak`
> preservation copy) scan the wiki bytes (HEAD or pinned git objects)
> before anything lands in `project_shared` — and on the web settings
> `rules/promote` write, which scans the exact appended hooks fragment
> (event key included). Every first-party byte path into
> `project_shared` now scans before landing. Gate B's confirm prompt is
> intentionally absent on install/update: those verbs have no `--scope`
> choice (dest is `project_shared` by construction), so the explicit
> command itself carries the surface intent; only the scan half was
> missing.

> **2026-07 (#1509):** Gate A also fires at write time in the web
> canonical create/update editors (skills / commands / agents),
> matching the mcp-servers editor: the handlers scan `body.content`
> before `atomic_write_text`, so a pasted secret is refused (path-free
> 422) instead of landing in the git-tracked canonical and only being
> caught at the next sync/import. The sync/import backstop is
> unchanged. This closes the gap that falsified the "every first-party
> byte path scans before landing" claim above. User-tier editor saves
> stay unscanned by design: not git-tracked, gated by
> `allow_host_writes`, and still subject to the sync-time valve.

**Gate B (surface).** Explicit `--scope project_shared` flag plus
confirm prompt at the CLI/MCP write surface (`mm add`,
`mm context init`, `mm context migrate --to project_shared`). The flag must be passed
explicitly — no env var or config-field default. There is
deliberately **no** `memory.default_write_scope` config field: the
explicit `--scope` flag (CLI) and `scope=` kwarg (MCP `mem_add`
/ `mem_batch_add`) are the only paths by which `project_shared`
becomes the active scope. A future contributor introducing such a
default must pass through this ADR rather than landing it as a
silent config addition; reviewers should refuse a
`MemoryConfig.default_write_scope` field on sight.

The confirm prompt:

```
About to write to <project>/.memtomem/<artifact>/<file>.
This file is git-tracked. Anyone with repo access can read this.
Continue? [y/N]:
```

`--confirm-project-shared` answers the prompt ahead of time on the CLI;
`confirm_project_shared=True` does the same for MCP tool calls and web
requests, neither of which has a terminal to prompt at. On every surface
that takes `--confirm-project-shared`, `--yes` alone does **not** satisfy
Gate B — it is a generic "skip prompts" flag users alias for unrelated
reasons. One surface is mid-migration rather than compliant: `mm context
pull` now *takes* `--confirm-project-shared` like the rest, but through 0.6.x
it also still accepts `--yes`, behind a deprecation notice, and only refuses
it from 0.7.0 (#2318, below). Until that release the rule above describes
every CLI write surface's vocabulary and every one but `pull`'s behaviour.
Every one of those consents produces a
`project_shared.confirmed_via=<surface>` audit line.

> **2026-09 (#2306):** Two corrections to the paragraph above, which
> described an intent rather than the code. First, `--yes` was said to
> override the prompt; it has been refused since PR-D review round 7 on
> every CLI surface that carries `--confirm-project-shared`, and the
> sentence above now says so — including the one surface where it is
> still accepted, `mm context pull`, whose divergence from its own MCP
> and web twins is left as a separate question rather than settled here.
> Second, the `confirmed_via` line was specified here and emitted by
> **nothing** — a repository-wide grep found the string only in this
> file. Every Gate B site now calls
> `privacy.emit_project_shared_confirmation` once the consent is
> established, producing one WARNING on the `memtomem.privacy` logger:
>
> ```
> project_shared consent recorded (project_shared.confirmed_via=<surface>,
> mechanism=<flag|prompt|param|request>, action=<write|delete|…>[, <context>])
> ```
>
> `mechanism` says how the consent arrived: `flag` for a CLI flag that
> satisfies Gate B, `prompt` for an answered CLI confirm, `param` for an
> MCP or library kwarg, `request` for a web body field. That flag is
> `--confirm-project-shared` everywhere except `mm context pull`, which
> has none and accepts `--yes` (see ADR-0030 §11); when the flag is not
> the usual one the line names it in the context, as `flag='--yes'`, so
> the two are told apart without a second mechanism value for a single
> command.
> Three properties are deliberate. The line records that consent was
> **given**, not that the write **landed** — Gate A, a host-write gate,
> a lock timeout, or a collision can still refuse afterwards, and the
> consent is the half with no other record. It is a log line and not a
> counter: the `record()` outcomes count the result of a content scan,
> and a consent is not a scan, so `mem_add_redaction_stats` and
> Settings → Redaction are unchanged. And a dry run writes nothing, so it
> records no consent — note that this says what is *recorded*, not what is
> *asked*: `mem_context_memory_migrate` still requires the confirmation
> argument on a preview call, which is its own pre-existing contract and
> not part of what this line reports. Like `blocked_project_shared`, the
> line is LTM-only and does not sync to STM.

`mem_edit` and `mem_delete` infer scope from the loaded chunk's
persisted `metadata.scope`, not the caller's parameter — a client that
omits `scope` while editing a project_shared chunk cannot bypass the
gate by accident. Both re-read that scope from the chunk re-fetched
*under the source file's lock*, so a re-scope landing between the
caller's read and the write cannot carry an edit into the shared tier on
a consent given for another one. The writer that produces a same-path
re-scope is an incremental **re-index** after `project_memory_dirs`
changes — `index_file` re-derives scope from the path each pass and
chunk ids survive re-index; `memory-migrate` rewrites path and scope
together and so arrives on the lock helper's re-key / "moved" branch
instead.

> **2026-09 (#2317):** Gate B now applies to `mem_edit` and its web twin
> `PATCH /api/chunks/{id}`, which until this release had Gate A alone.
> The gap was narrow and deliberate-looking — an edit does not move the
> chunk between tiers, so one could argue the tier was consented to when
> the chunk landed — but `mem_delete` asks before removing the same
> chunk, so the two verbs contradicted each other about the same bytes,
> and the paragraph above claiming two gates on every `project_shared`
> write was false as written (the correction #2306 made to that claim
> pointed here). What the consent covers on this path is the *write*,
> not the destination: replacing the body of a repository-tracked note
> puts new bytes on a path the repository tracks, which the project then
> commits and shares (the write itself commits nothing — the exposure is
> the destination, not the moment). The consent line
> carries `action=edit`, distinguishing it from the `action=delete` of
> the same chunk. This was a breaking change to both surfaces —
> `confirm_project_shared` was added as a required-when-`project_shared`
> argument with no accept-and-warn window, because a release spent
> accepting unconfirmed edits would keep emitting exactly the missing
> record #2306 closed.
>
> This closes the edit path and **not** the general claim. The review
> that found it went looking for siblings and found more, so the
> paragraph above still describes an intent on some surfaces: the
> LangGraph `MemtomemStore.add()` adapter reaches the tier through a
> caller-supplied `file=` with neither gate (#2321, which is the
> load-bearing one — its Gate A also runs before the destination is
> known, so it scans as `user` scope), and `mem_session_end`, the two
> importers and `mem_index_url` derive a destination that Gate A already
> classifies but no gate confirms (#2322, reachable only when
> `memory_dirs` and `project_memory_dirs` overlap). Neither is visible to
> `test_project_shared_confirmation_audit_guard.py`, which finds Gate B
> sites by the `confirm_project_shared` identifier and therefore cannot
> see a surface that carries none — the self-certification boundary that
> guard's own docstring states. Until those close, "every
> `project_shared` write takes two gates" is a claim about the memory
> CRUD and context surfaces, not about every writer in the tree.

> **2026-09 (#2321):** The LangGraph `MemtomemStore.add()` adapter named
> above now carries both gates, on every destination it writes to — the
> caller-supplied `file=` and the derived daily file alike.
> `confirm_project_shared` is required when the destination lands in the
> `project_shared` tier, the consent line records
> `confirmed_via=langgraph_add`, and Gate A is handed that same tier
> instead of the `user` default it had been scanning under. #2322 remains
> open, so the paragraph's closing sentence still holds as written.
>
> The derived branch is gated too, which is worth saying because it was
> briefly left out. A daily file lands wherever `memory_dirs` points, and
> asking an automatic write to carry a confirmation argument looked like
> #2322's question rather than this one's — but #2322 enumerates four
> other writers and says in its own text that the caller-controlled case
> belongs here, so there was nothing to defer it to. Where one directory
> is registered as both a user and a project memory directory, that daily
> file really is in the tracked tier, and both gates now say so.
>
> What this surface had to settle that the CRUD tools did not — and the
> caution for any future writer that takes a path from its caller.
> `classify_scope` decides the tier from the path's *spelling*, and a
> path can be spelled to defeat it in both directions. A target nested at
> `<registered-shared-root>/sub/.memtomem/memories.local/note.md` matches
> the `project_local` pattern first — a tier that asks for no
> confirmation and admits `force_unsafe=True` — while every byte of it
> lands inside the tree the shared root registered. And the default user
> memory directory is literally `~/.memtomem/memories`, which matches the
> `project_shared` pattern exactly, so a refusal keyed on "canonical
> shape but unregistered" rejects the most ordinary target there is.
>
> The adapter therefore decides the tier by **ownership rather than
> spelling**: the registered project root that covers the target decides,
> most specific root winning; failing that, a target covered by a
> configured user memory directory is `user` whatever it looks like; and
> only a target covered by nothing is refused for wearing a canonical
> shape it has not registered.
>
> Ownership alone is not sufficient, because the gates and the *rows*
> answer to different judges. The gates protect the write; `mem_edit` and
> `mem_delete` read the tier from the persisted chunk, which the indexer
> labels with `classify_scope` — the spelling answer. So the adapter
> requires the two to agree and refuses the write when they do not,
> rather than leaving behind a row it could not gate again. Both
> directions are real. Gated `project_shared` but stored `project_local`
> is the nested path above: the add is protected and the later edit is
> not. Gated `user` but stored `project_shared` is a
> `project_memory_dirs` entry whose own path is not canonical — it cannot
> name its own tier, so it does not get to decide one, and without the
> round-trip check the target would fall through to the user branch and
> reach the tracked tier having passed neither gate. Making the two
> judges agree in general means changing the classifier every read
> surface shares, which is a decision for the classifier and not for one
> adapter.
>
> Two limits are stated rather than closed. The pattern is
> case-sensitive, so on a case-insensitive filesystem an alternate-case
> spelling of `.memtomem/memories` is not recognised by the ownership
> rule or by `classify_scope`: the two agree on `user`, the round-trip
> check is satisfied, and the write proceeds ungated onto a path the
> repository still tracks. And the tier is decided before the write lock,
> as in `_mem_add_core`, so a rename of an ancestor between the resolve
> and the append is not covered. Both belong to the classifier and the
> lock contract rather than to any one surface.

> **2026-09 (#2318):** The `mm context pull` exception recorded by #2306 is
> being closed in two stages, and only the first has landed. The command now
> *takes* `--confirm-project-shared` like every other CLI write surface, so
> the vocabulary matches its own `mem_context_pull` tool and web route today;
> the behaviour matches them in 0.7.0, when `--yes` stops being accepted.
> Read the paragraph above as describing that end state, not 0.6.x.
>
> Unlike the #2317 edit change, this one gets an accept-and-warn window,
> because the two situations are not alike. There, a missing argument meant
> a consent was never *taken*, and a release spent accepting unconfirmed
> edits would keep producing the very gap #2306 closed. Here the consent was
> always taken and always recorded — `--yes` carried it, and the audit line
> already named the flag. What was wrong was the vocabulary, not the record,
> so nothing leaks by spending a release fixing it in a way that does not
> break `mm context pull … --scope project_shared --yes` in every existing
> script. Through 0.6.x that invocation still works and prints a yellow
> stderr notice naming the flip; from 0.7.0 it gets the standard refusal
> ("`--yes` alone is not sufficient"). Until then the consent line keeps
> reporting `flag='--yes'` on that path, which is what tells an operator the
> deprecated spelling is still in use.
>
> The notice is deliberately not emitted beside the consent. `prepare_pull`
> returns early on a divergent-source refusal, a canonical-exists refusal,
> the byte-identical no-op and a Gate A block, all before Gate B is reached,
> and an automation owner needs the migration signal on those runs too. A
> notice reports a *spelling*; the consent line reports an *authorisation*.
> Only the second one is withheld when nothing was authorised.
>
> This also brings `pull` inside
> `test_project_shared_confirmation_audit_guard.py`, which closes the one
> identifier-invisible Gate B that guard's docstring used to name — a Gate B
> spelled another way. It does not close the wider class, which is the
> *absence* of a gate rather than a different spelling of one: #2321 closed
> that way a day earlier, and #2322's four derived-target writers are still
> open, with nothing for a scan keyed on the flag to find in them.
>
> **Window retargeted 0.6.0 → 0.7.0 (2026-09-10).** The versions above were
> first written as "through 0.5.x, refused from 0.6.0" on the assumption that
> stage 1 would reach users in a 0.5.x patch. It did not: 0.5.0 had already
> shipped when stage 1 landed, and no 0.5.x release followed, so released
> 0.5.0 has no `--confirm-project-shared` on `pull` at all. Cutting the
> refusal in 0.6.0 would have made this rider's own argument false — it
> justifies the window by "spending a release" on it, and the release being
> spent would have been one nobody could install. Stage 1 therefore ships in
> 0.6.0, which is the release the window spends, and stage 2 refuses from
> 0.7.0. The rule this records: a deprecation window is counted in
> *published releases*, not in merges to `main`.

> **2026-09 (#2322):** the derived-destination half above is closed —
> which corrects the last sentence of the #2318 note, written while it was
> still open — and not by adding a third gate. Those writers take no
> destination from their caller — they ask `memory_scope.require_user_base` for the
> user-tier base — so the honest fix was at the derivation: that helper
> now classifies the base it is about to return and refuses a registered
> project tier outright, naming both config fields. Gate B never becomes
> reachable on those surfaces because the *destination* never does.
>
> Growing a `confirm_project_shared` argument instead would have put a
> confirmation prompt on an automatic `mem_session_end` summary, where
> there is no human to answer it; a session end now declines the shared
> archive and keeps the summary on the row. Rejecting the
> `memory_dirs ∩ project_memory_dirs` overlap at config validation was
> the other candidate and was not taken: it would refuse to *load* a
> configuration that reads perfectly well, when only these writes are
> unsafe.
>
> This is the same rule the install/update note above states from the
> other side. There, Gate B is absent because the verb carries the
> surface intent (`mm context install` has no `--scope` choice). Here it
> is absent because the surface has no project-tier destination at all —
> in neither case is a missing confirmation a missing consent. The
> refusal covers more than the four sites #2322 listed: scratch promote,
> `mm review approve`, `mm agent share` and `mm shell`'s `add` derive
> the same base and did not even pass `scope=` to Gate A.
> `tests/test_user_base_derivation_guard.py` keeps the derivation in one
> place — a hand-rolled `memory_dirs[0]` is how `mem_session_end`
> escaped the helper in the first place, and widening that guard to
> aliases immediately turned up one more: `PinnedContextStore` derived
> its `user_base` the same way, and both of `set`'s gates keyed on the
> caller's declared scope, so a `scope="user"` block landed in the tier.
> Both mutating methods refuse — `set` and `delete`, since removing bytes
> the project committed changes the shared tier as much as adding them —
> while every read path stays total, because `mem_context_compose`
> answering with an internal error is the shape #1768 exists to prevent.

> **2026-09 (#2336):** the note above about `MemtomemStore.add()` has a
> sibling. `MemtomemBaseStore` takes an arbitrary `root=` and gated only
> on the *declared* `scope`, so `root=<proj>/.memtomem/memories/…,
> scope="user"` cleared Gate B and then ran every `put`'s Gate A as
> `user`. It now classifies the root and **escalates only** — a declared
> `project_shared` survives an unregistered path, because inferring
> `user` there would reopen the bypass it is meant to close. The
> classification reads a throwaway config loaded with `migrate=False`:
> the store's own configuration is untouched, so an explicit-`root`
> caller does not start inheriting a persisted embedding provider, and a
> lookup done for a *refusal* cannot rewrite `~/.memtomem/config.json` as
> a side effect of constructing a store that then raises.
>
> With #2321, #2322 and this closed, "every `project_shared` write takes
> two gates" holds for every first-party writer in the tree except the
> one named in #2333: `POST /api/scratch/{key}/promote` still accepts a
> caller-supplied `file=` under any `memory_dirs` entry with no `scope=`
> on its Gate A.

> **2026-09 (#2335):** The same adapter's `delete()` now carries Gate B.
> It removes index rows and leaves the markdown file alone, so it is not a
> host write and Gate A has nothing to scan — but ADR-0011 §5's consent is
> not a property of writing bytes. `mem_delete` already gates a row-only
> removal on its `source_file=` branch, for the reason that applies here
> too: a note the project shares stops being findable for everyone who
> searches the store, decided by a caller who did not author it alone. The
> consent line carries `confirmed_via=langgraph_delete` and
> `action=delete`, matching `mem_delete`'s verb for the same chunk.
>
> Two shape differences from the CRUD tools are deliberate and worth
> naming for the next in-process surface. First, the refusal **raises**
> instead of returning a value: `delete()` answers `bool`, and `False`
> already means "no such chunk, or not in your project" (ADR-0036) —
> folding a refusal into it would make a gate that fires indistinguishable
> from one that never applied. It raises a *typed*
> `ProjectSharedConfirmationRequiredError` (a `ValueError` subclass) rather
> than the bare class, because the same method parses its `chunk_id` and a
> malformed id raises a plain `ValueError`: a caller catching the bare type
> would read "not retryable at all" and "retryable by passing the flag" as
> the same event. Any future library surface whose success value cannot
> carry a refusal should reach for the same type. Second, the tier is
> re-read under the source file's **L2 sidecar only**, through
> `tools.memory_mutation.locked_source_chunk` — the surface-neutral span
> the web chunk routes already take, so the two delete surfaces cannot
> drift. `_locked_chunk` takes L1 as well, but L1 is the MCP server's
> `AppContext` lock and `integrations/` may not import `memtomem.server`;
> the sidecar's own in-process layer gives same-process serialization, so
> what is missing is not exclusion but the `AppContext` bookkeeping this
> adapter has no part in. What the adapter does keep for itself is the
> bounded re-key when that helper reports `moved`: the web route answers
> 409 there and lets the client re-issue the request, and an in-process
> call has no request to re-issue. Since #2346 that span also degrades to
> the lock's in-process half when the source's directory has been removed,
> rather than recreating it to lock a delete; the condition attached to a
> degraded span is "write no bytes", which this method satisfies by
> construction, so it proceeds there as the row-only CRUD branches do.
>
> The issue that raised this also raised `MemtomemStore.index(path=...)`
> as a caller-supplied path with no containment check. Measured against
> the source, it has one: the adapter calls `index_path` with the default
> `path_scope="configured"`, and `IndexEngine` refuses any path outside
> `all_index_roots()` — `memory_dirs` plus `project_memory_dirs` — before
> it opens a file. Nothing was changed there. Recorded here so the next
> reader does not re-open it, and pinned on the adapter so a later
> `path_scope="explicit"` on that call would fail rather than quietly
> widen it.

> **2026-09 (#2348):** the #2336 note's closing claim — that #2333 was the
> only first-party writer left outside "every `project_shared` write takes
> two gates" — was one exception short.
> `mm context settings-migrate` reached the tracked tier with neither gate,
> and stayed invisible for the reason #2321 did — no `confirm_project_shared`
> identifier for the audit scan to find — plus one of its own. It *has* a
> confirmation prompt, so a reader checking this surface finds one and stops.
> That prompt is the host-write check, which fires when a tier lies outside
> the project root; `resolve_scope_path` puts `project_shared` at
> `<root>/.claude/settings.json`, inside it by construction. The one gate the
> command had could never fire for the one tier that needed it. A gate whose
> predicate cannot be true on the path it is read as covering is worse than
> an absent one, because it answers the question a reviewer asks.
>
> This surface is gated rather than made unreachable, which is the #2322
> rule applied in the other direction: the destination is the caller's own
> `--to`, so there is someone to ask. Both flags can be needed in one run and
> neither substitutes for the other — `--yes` answers leaving the project,
> `--confirm-project-shared` answers touching the tracked tier.
>
> **The source leg is gated too**, and that is the part the issue did not
> ask for. It filed the `--to` half on the reasoning that
> `project_shared → project_local` writes the gitignored file, which is true
> and incomplete: the migration also *strips* the entries out of the tracked
> one. The #2322 note above already settles what to do about that — both of
> `PinnedContextStore`'s mutating methods refuse, "since removing bytes the
> project committed changes the shared tier as much as adding them" — and the
> reachability is not hypothetical: `settings_doctor.format_warning` prints
> exactly this command, with `--from=project_shared`, whenever the duplicate
> it found lives in that tier. Both legs record one consent line, with
> `from_scope` and `to_scope` naming which one was shared; no separate action
> verb is invented for the removal.
>
> **Gate A is conditional here, unlike its `settings-copy` sibling.** That
> one scans every copy because it always writes the destination's canonical
> `.memtomem/settings.json`, which is tracked whatever tier was asked for. A
> migration writes no canonical at all, so only a `project_shared` target
> reaches a tracked file, and scanning the untracked tiers would add a
> valve-less refusal where nothing is exposed. The source leg is never
> scanned in either direction: moving a secret-bearing rule *out* of the
> shared tier is the remediation Gate A's own message prescribes, and
> scanning the bytes being removed would refuse the fix and leave the secret
> in place. Gate B still covers that leg, because a confirmation can be
> answered and a hard refusal cannot.
>
> One consequence worth stating, because it looks like over-reach until the
> lock contract is read: the scan covers moves the planner marked
> `already_at_target`. `apply_migration` re-classifies against the live tier
> under its pair-lock, so an entry the target lost between plan and apply
> comes back as a write. Scanning only the plan-time writes would leave that
> transition unscanned. The visible cost is that a run whose sole effect
> would be cleaning the source is refused when the tracked tier *already*
> holds the secret — a state worth surfacing rather than tidying around.
>
> #2333 remains the last first-party writer outside the claim.

> **2026-09 (#2366):** `MemtomemHybridStore` (#2363) is the "next in-process
> surface" the #2335 note wrote its shape down for, and it answers
> differently on purpose: its Gate B consent is **store-scoped, taken once at
> construction**, and `delete` carries no gate of its own. Recorded here so
> the next parity audit reads that as a decision rather than as the gap #2335
> had just closed on the sibling adapter.
>
> The two stores differ in what one handle can reach. `MemtomemStore` is a
> view over the whole markdown corpus, where every chunk carries its own
> `scope` (Decision 4): one handle spans `user`, `project_local` and
> `project_shared`, so the tier is a property of the *row* and has to be
> asked about per operation — `delete()` re-reads it under the source file's
> lock through `locked_source_chunk`, while `add()` resolves it from the
> destination it was handed. `MemtomemHybridStore` owns one SQLite database
> at an explicit path. `_resolve_target_scope(self.path, memory_dirs,
> project_memory_dirs)` classifies that path in `__init__` — the resolved
> tier is the path's classification, escalated to `project_shared` if either
> the path or the declared `scope=` says so — and a `project_shared` result
> raises unless the caller passed `confirm_project_shared=True`, recording
> `surface=langgraph_hybridstore_init`, `mechanism=param`, `action=init`.
> Every later mutation, `delete` included (LangGraph spells it
> `PutOp(value=None)`), runs under that one consent, against the tier
> resolved for the path the handle was opened on.
>
> **The invariant is per handle, because nothing persists a tier.**
> `hybrid_meta` stores `kind`, `version` and the index configuration and no
> scope, so a second handle on the same file classifies it afresh, under
> whatever configuration *it* loads. For the tier this gate exists to
> protect that is a distinction without a difference: under equivalent
> effective configuration, a path under a registered **shared-tier** root
> classifies as `project_shared` for every handle, so no caller reaches the
> tracked tier ungated. Registration alone does not say which tier —
> `_owned_tier` takes the most specific covering root and reads *its* tier,
> so a registered `memories.local` root is `project_local`. What does not
> survive reopening is the other direction: a caller-declared escalation on
> a path that classifies as `user` binds only the handle that declared it,
> and a later default-argument handle on that same file is `user` again.
> Persisting the declared tier would change that, and is not done.
>
> **Gate A is not hoisted with it, and the asymmetry is the point.** Gate A
> scans bytes, so record checks stay per-`put`. Originally `_prepare`
> scanned only `encode(op.value)`; #2374 extends that check to raw namespace
> labels, keys and `index` selectors, and separately checks persisted index
> configuration before opening the database (see the Gate A note above).
> A delete reaches neither per-operation gate — Gate A because it has no value to
> scan, which is #2335's own reasoning for the sibling, and Gate B because
> the tier it touches was consented to before the store existed. A gate
> answers a question, and the two questions have different lifetimes: "may
> these record bytes land" is per-write, "may this store touch the tracked tier" is
> per-store.
>
> **What makes the hoist adequate is a storage fact, and the pin covers only
> the part of it that is schema-visible.**
> `test_langgraph_hybrid_store.py::test_items_storage_layout_carries_no_tier`
> asserts the `items` column tuple and, read as data rather than as DDL text,
> that a row's identity is `namespace`+`key` and nothing else. So a per-item
> scope column, or a scope folded into the uniqueness constraint, fails
> there. Two classes of change do not. A tier can be introduced without
> moving a column — carried on a `namespace` segment, buried in `value_json`
> or `index_json`, kept in a second table or a `hybrid_meta` key, or routed
> by a subclass. And the pin observes one construction path, a freshly
> created default-argument database, so a column added only under indexing
> arguments or on reopen is never seen; reopening today *rejects* an
> unrecognised `hybrid_meta` rather than migrating it, which is why no
> migration path exists to observe, and why adding one belongs to this
> question rather than beside it. Naming both classes is the point of this
> entry: they make the init-time consent cover less than the rows it is read
> as covering while every guard stays green, so they have to be caught by a
> reviewer asking the gate question again.
>
> No behavior changed under this issue. The hoisted gate is adequate *given*
> those facts; it is the facts that needed writing down.


Before PR-D, `mem_batch_add` bypassed `enforce_write_guard` and used
an inline `privacy.scan` instead — the batch path was the obvious
bypass route. PR-D refactored `mem_batch_add` (`server/tools/memory_crud.py:668`)
to call `enforce_write_guard` per entry so the chokepoint applies
uniformly across single-add and batch-add surfaces; this section is
retained for the rationale, not as a future task.

**Authoring-side privacy is explicitly out of scope.** When a user
edits a project_shared agent markdown directly with their text editor,
memtomem cannot gate the save. Pre-commit hooks are the team's choice;
this RFC scopes `mm mem rescan --scope project_shared` and
`mm context rescan --scope project_shared` as the natural composable
building blocks for that workflow, but treats the subcommands as
v1-deferred (Open Questions item 4) — they ship only if a concrete
user reports a need before PR-D / PR-E. Auto-installing pre-commit
hooks is a non-goal regardless: it would imply ingest-time scanning is
bypassable, which contradicts the trust boundary documented in
`privacy.py:7-26`.

The `blocked_project_shared` decision is **LTM-only** and does NOT
sync to STM. `privacy.py`'s asymmetric-sync rule already specifies
secret-class patterns sync from STM but PII-class do not auto-sync;
this new outcome is added to the LTM module's docstring with an
explicit "not synced upstream" note so the next STM-pattern sync does
not try to mirror it.

### 6. Memory search default is project-aware, not naively additive

For settings hooks, ADR-0010 §3 leans on Claude Code 2.x's additive
tier-merge to do the right thing at runtime. Memory has no equivalent
runtime — search runs in memtomem's own SQL and must define semantics
explicitly:

- **Project context detected** (caller's cwd resolves under a
  `<X>/.memtomem/` ancestor): default search returns rows where
  `scope = 'user' OR project_root = <X>`. Other projects' shared/local
  rows are excluded. Prevents cross-project leak.
- **No project context** (`mm mem search` from `~/`): default returns
  `scope = 'user'` only. Project tiers are excluded unless an
  explicit `--scope=project_*` filter is passed.
- **Explicit `--scope=project_shared` from no-project-context:**
  unions every `project_root`'s shared rows — a deliberate
  cross-project search. Document this is intentional, not accidental.

A scope-context SQL fragment is **always** appended to
`bm25_search` / `dense_search` / `recall_chunks`, even when
`scope_filter=None`. The implementation lives in a new
`storage/sqlite_scope.py` mirroring `sqlite_namespace.py`.

Same-relevance results tie-break in scope priority order:
`project_local > project_shared > user`. Pin in regression test.

`scope_filter` composes orthogonally with the existing
`system_namespace_prefixes` filter: both AND together. A chunk in
`namespace=archive:foo` and `scope=project_shared` is hidden by the
default-search archive prefix exclusion regardless of scope — same as
today.

### 7. ADR-0001 §1 supersession

This RFC supersedes the implicit "canonical = project-scope only"
default that ADR-0001 §1 established for agents / skills / commands
through its reverse-sync priority and one-way fan-out rules. Canonical
is now scope-selectable across user / project_shared / project_local
for memory, agents, skills, and commands. **v1 defaults preserve
ADR-0001 §1's behavior** — agents / skills / commands still default
to project_shared canonical when `mm context init` is run without
`--scope`. The supersession is in availability of new scopes, not in
the change of any existing default.

ADR-0001 stays in place as historical context; its body is not
amended. Future readers cross-reference this ADR via the references
section below.

### 8. No default flip planned

ADR-0010 §5 codifies a future default-flip trigger for settings
(`user → project_local` once detection + migration ship and a 2-week
P0/P1 dwell passes). ADR-0011 plans **no default flip, ever** for
memory or agents / skills / commands. Two reasons:

- **Memory authorship is not regenerable.** Hooks are derived
  artifacts that `mm context sync` can re-materialize from canonical;
  flipping their default tier costs little. User-authored memories
  are the canonical — flipping `mem_add` default scope silently moves
  authorship into git history, a trust violation no soak period can
  validate.
- **Agents / skills / commands defaults already match user
  expectation.** `mm context init` defaulting to project_shared
  matches what users do today; flipping to user-scope or project_local
  would silently change where new artifacts land. Without a concrete
  failure mode the flip would solve, the staged-flip pattern's risk
  outweighs the symmetry win.

The asymmetry with ADR-0010 §5 is documented in Consequences below.

### 9. Phasing

Six PRs, each independently revertable. PR-B / PR-C / PR-D were
drafted alongside this ADR and shipped together as a single bundle
in PR #882 (2026-05-09); PR-A landed shortly after to capture the
RFC on `main`. PR-E shipped 2026-05-10/11 in #889 / #890 / #893;
the Web/docs slice of PR-F shipped 2026-05-11 in #929.

| PR    | Scope                                                                                       | Ship status                                                |
|-------|---------------------------------------------------------------------------------------------|------------------------------------------------------------|
| PR-A  | This ADR markdown.                                                                          | This PR. Status: Proposed at merge → Accepted after dwell. |
| PR-B  | Memory schema (append-only columns), `IndexingConfig.project_memory_dirs`, scope classifier, `_resolve_scope` / `_apply_scope`, `dataclasses.replace` refactor of `_apply_namespace`, Gate A on `enforce_write_guard`. No CLI/MCP surface; defaults preserved. | Shipped 2026-05-09 in PR #882.                             |
| PR-C  | Memory read surface — `ScopeFilter`, `scope_context_sql` always-on fragment, project-aware default merge per §6, search pipeline cache key, MCP read tools, CLI `--scope` (comma-list) on read commands. | Shipped 2026-05-09 in PR #882.                             |
| PR-D  | Memory write surface — Gate B (explicit flag + confirm), `mm mem add` restructure (the pre-PR-D hardcoded `~/.memtomem/memories` user_base in `cli/memory.py` was incompatible with project-scope writes), `mem_batch_add` refactor through `enforce_write_guard`, `mem_edit` / `mem_delete` inferred-scope, `mem_consolidate_apply` cross-scope rejection, `mm context memory-migrate` v1 (chunk-id-stable single-DB rename). `mm mem rescan` deferred (Open Questions item 4). | Shipped 2026-05-09 in PR #882.                             |
| PR-E  | Agents / skills / commands canonical scope axis — `context/scope_resolver.py`, each generator's `target_file` / `is_available` accepting `(scope, project_root)`, `mm context init --scope=...`, `mm context sync --scope=...` filter, `mm context migrate <kind> <name> --to <scope>` for cross-tier moves (the originally-named `promote` / `demote` verbs were consolidated into `migrate --to`), sync-time privacy scan (Gate A for non-memory). `mm context rescan` deferred per Open Questions item 4. | Shipped 2026-05-10/11 in PRs #889 (E1+E2), #890 (follow-up nits), #893 (E4 `migrate --to`). |
| PR-F  | Web UI scope badges (memory + context) read-only, `/api/add` rejection with CLI hint plus docs link, public docs updates (user-guide / getting-started / mcp-clients per the default-change fanout convention). | Web/docs slice shipped 2026-05-11 in #929 (closes #924); tier-switching write affordances and detail/diff/rendered-route alignment remain follow-up polish. |

**Sequencing note.** ADR-0010 was Accepted on 2026-05-09 and the
implementation work for ADR-0011 PR-B / PR-C / PR-D landed the same
day. The earlier draft of this section recorded a "≥2 weeks ADR-0010
dwell with no P0/P1 before opening PR-A" prerequisite — applied
literally that would have blocked opening PR-A indefinitely after the
implementation already shipped, which is why PR-B / PR-C / PR-D were
bundled and merged ahead of this ADR landing on `main`. The prerequisite
is retained in spirit as a **post-merge ratification gate** instead:
this ADR enters at Status `Proposed`, and the flip to `Accepted`
requires the same ≥2-week, zero P0/P1 window against ADR-0010's
settings-migrate plumbing and #882's memory scope plumbing.
Implementers of any remaining PR-F follow-up polish should treat the
Accepted flip — not the PR-A merge — as the unblock signal.

## Consequences

- The implementation roadmap is six PRs (see §9 phasing table).
  PR-A is this ADR markdown; PR-B (memory plumbing) / PR-C (memory
  read surface) / PR-D (memory write surface) shipped together in
  PR #882 (2026-05-09). PR-E (canonical scope axis for agents /
  skills / commands) shipped in #889 / #890 / #893, and the PR-F
  Web/docs slice shipped in #929 (2026-05-11); remaining work is
  follow-up polish, not a wholly pending phase (see §9 sequencing note).
- Every canonical artifact in memtomem now uses one scope vocabulary.
  The `TargetScope` literal at `config.py:721` is the single source
  of truth across settings, memory, agents, skills, and commands.
- `<project>/.memtomem/config.json` remains deferred per ADR-0010 §3.
  Per-project scope selection is solved by absolute paths in the
  user-tier `IndexingConfig.project_memory_dirs` field (memory) and
  by per-invocation `--scope` flags (everything else). Implementers
  of PR-B / PR-E should resist accidentally introducing the
  project-config layer; the same warning ADR-0010 §3 records applies
  here.
- Claude Code 2.x's additive merge of user-tier and project-tier
  runtime entries (agents / skills / commands) is now a load-bearing
  assumption for memtomem's fan-out. Same caveat as ADR-0010
  Background Tier-merge note: if the merge model changes upstream,
  the duplicate-name story degrades to "user copy is dead config."
- The asymmetry with ADR-0010 §5 — settings can default-flip, memory
  / agents / skills / commands cannot — is recorded in §8.
  Implementers of any future default-flip ADR for these artifacts
  must reopen this discussion explicitly rather than ride on
  ADR-0010's precedent.
- Privacy `enforce_write_guard` becomes the single chokepoint for
  every memory write surface. The pre-PR-D `mem_batch_add` bypass was
  treated as a bug (closed by PR-D — `mem_batch_add` at
  `memory_crud.py:668` now calls `enforce_write_guard` per entry),
  not as a design choice to preserve.
- For agents / skills / commands, `project_local` exists but does
  not fan out to runtime. Users who expect "everything I author
  ships to .claude/" will be surprised by drafts staying invisible.
  The CLI surfaces this with a `(draft, no fan-out)` annotation in
  `mm context status` output.

## Considered & rejected

- **Status quo: keep memory and agents / skills / commands at
  project-scope only.** Rejected because it forecloses the team-shared
  memory and cross-project personal artifact workflows described in
  Background "Why now". Once ADR-0010 validated the 3-tier model
  in-tree, holding the line for the remaining four artifact types
  becomes the unjustified exception.
- **Configurable + flip default to `project_shared` for memory
  immediately.** Rejected for v1 because flipping the default scope
  for `mem_add` silently moves authorship into git for users who
  don't notice the change. Memory authorship is not regenerable;
  the staged-flip pattern that ADR-0010 uses for hooks does not
  transfer cleanly to canonical authoring surfaces. §8 codifies "no
  flip, ever" as the recommended posture.
- **Symmetric additive merge for agents / skills / commands
  `project_local`.** (Considered design (c) in §3.) Rejected because
  fanning out two canonical sources to the same runtime path requires
  a within-tier precedence rule the runtime tier-merge model does not
  define. Draft-tier-no-fan-out (§3) avoids the problem entirely.
- **memtomem-only loader fan-out to `<proj>/.claude/agents.local/`.**
  (Considered design (b) in §3.) Rejected because Claude Code does
  not recognise the `.local/` runtime variant for non-settings
  artifacts; inventing a memtomem-only path makes the runtime
  surface inconsistent across artifact types.
- **Split memory storage by scope (one SQLite per scope).**
  Rejected because it fragments dedup, breaks chunk-links across
  scopes, and forces per-scope schema evolution. §4 single-DB-with-
  scope-tag preserves every existing memory contract.
- **Pre-commit hook auto-installation for project_shared.** Rejected
  because auto-installing implies ingest-time scanning is bypassable.
  Ship `mm mem rescan` / `mm context rescan` as composable building
  blocks; teams who want a hook wire it themselves in
  `.pre-commit-config.yaml`. §5 records this as an explicit
  non-goal.
- **Amend ADR-0001 §1 instead of authoring a new ADR.** Rejected
  for the same reason ADR-0010 cited: ADR-0001 has no amendment
  precedent in this repo, and ADR-0007 / ADR-0008 / ADR-0010 all
  layered onto ADR-0001 without amending it. New ADR is the
  established pattern.
- **Introduce `<project>/.memtomem/config.json` in this RFC.**
  Rejected because ADR-0010 §3 deferred it deliberately and the
  decision still holds. Per-project scope selection works through
  user-tier absolute paths (memory) and per-invocation flags
  (everything else); the project-config layer is a separate ADR's
  responsibility when a concrete need surfaces.

## Open questions for the implementation issues

The following points stay live for the implementation PRs to resolve;
they do not block this ADR's acceptance.

- The final names of `project_shared` / `project_local`. Inherited
  from ADR-0010 (which itself flagged naming as TBD). If a clearer
  pair emerges during PR-B, the rename can land alongside before any
  user-visible release; both ADRs flip together.
- The same-name conflict resolution for agents / skills / commands
  when one name appears in both project_shared and user scopes. §1
  notes Claude Code natively merges; for memtomem-managed views
  (`mm context status --scope <tier>`, or the Web overview's per-tier
  views), highlight the conflict so the user knows.
  The exact warning copy is implementation issue territory.
- Orphan project chunk garbage collection. If a user indexes
  `/tmp/foo/.memtomem/memories/` then deletes the directory, stale
  rows remain in the user-local DB. Watcher prunes file-level
  deletions within tracked roots, but root-removal is a new case.
  Document as a v1 known limitation in PR-B's release notes; file a
  follow-up issue.
- The use cases for `mm mem rescan` and `mm context rescan` are
  spec'd to two scenarios in §5 (STM pattern sync; git-mv ingest
  bypass). If neither has a concrete user reporting before PR-D /
  PR-E, drop the subcommands from v1 and re-add when a real use
  case shows up.
- chunk-id-stable rename mode for `mm context memory-migrate`.
  v1 reports the count of dropped chunk_links lineage rows; full
  preservation across scope moves is deferred.
- MCP exposure for `mm context init` / `sync` / `migrate`.
  PR-E shipped through #889 / #890 / #893, with scope-tier moves
  consolidated into `mm context migrate <kind> <name> --to <scope>`.
  #887 since landed MCP parity for `init` / `sync` / `generate` /
  `diff` (the `mem_context_*` tools), and memory migration is exposed
  as `mem_context_memory_migrate` — a thin wrapper over `mm context
  memory-migrate` (markdown memory files only). It was originally named
  `mem_context_migrate`; #1147 (B5-2) renamed it and v0.5.0 (#1619)
  removed the compatibility alias. The **artifact**
  scope-tier and flat→dir modes of `mm context migrate <kind> <name>`
  remain **CLI-only by design**: there is no MCP path that migrates
  agents / skills / commands between tiers. #1123 (B5-1 / B5-2) records
  this as a deliberately-deferred gap rather than built — `migrate`'s
  destructive, layout-aware semantics are an interactive/authoring
  surface, not an agent-runtime one.

## References

**Issues / PRs / milestones**

- This ADR's source: user request 2026-05-09 ("memory 도 같은 계층
  구조로 개편하면 어때 + agents/skills 도 포함").
- PR #876 — settings-migrate subcommand, ADR-0010 §4 implementation.
- PR #874 — duplicate hooks detection, ADR-0010 §4.
- PR #873 — `hooks.target_scope` config field + `_claude_target` /
  `target_file` plumbing, ADR-0010 §3.
- Tiered context gateway v2 milestone (#868 umbrella) — the broader
  3-tier model this ADR completes.

**ADRs**

- ADR-0001 §1 — reverse-sync priority and one-way fan-out;
  superseded in part by §7 of this ADR (canonical-scope axis added,
  defaults preserved).
- ADR-0001 §5.1 — `≥2 weeks no P0/P1` readiness wording mirrored in
  the §9 PR-A prerequisite.
- ADR-0007 — trigger-criteria-then-flip precedent, referenced for
  the §8 "no flip" inversion.
- ADR-0009 — RFC pattern precedent for Proposed-status ADRs with
  multi-week dwell, mirrored in §9.
- ADR-0010 — settings hooks 3-tier scope; this ADR's plumbing
  primitives reuse the `TargetScope` literal at `config.py:721`,
  the `_resolve_cli_scope` flag pattern at
  `cli/context_cmd.py:313`, and the `_confirm_settings_host_writes`
  prompt shape at `cli/context_cmd.py:342-366`.

**External docs**

- Anthropic Claude Code settings reference —
  https://docs.claude.com/en/docs/claude-code/settings (source for
  the additive multi-tier merge claim referenced in Background;
  inherited from ADR-0010).

**Source files (anchors for the implementation PRs)**

Line numbers reflect HEAD on `main` after PR #882. Anchors are listed
by symbol where line drift is plausible; readers should grep the
symbol if a number ever stops resolving.

- `packages/memtomem/src/memtomem/config.py:721` — `TargetScope`
  literal (reused as-is across artifacts).
- `packages/memtomem/src/memtomem/config.py:193` — `IndexingConfig`;
  PR-B added `project_memory_dirs` (`config.py:203`) and
  `all_index_roots()` helper (`config.py:290`).
- `packages/memtomem/src/memtomem/config.py:1403` —
  `categorize_memory_dir`; PR-B added sibling `classify_scope`.
- `packages/memtomem/src/memtomem/privacy.py:432` —
  `enforce_write_guard`; PR-B added the `scope` kwarg + Gate A
  hard refusal.
- `packages/memtomem/src/memtomem/storage/sqlite_schema.py:90`
  (namespace migration), `:109` (overlap columns), `:128`
  (temporal-validity) — column-add migration precedents for PR-B's
  `scope` / `project_root` columns at `:147-148`.
- `packages/memtomem/src/memtomem/storage/sqlite_backend.py:1333` —
  `_row_to_chunk`; PR-B updated the optional-column index math for
  the appended columns.
- `packages/memtomem/src/memtomem/indexing/engine.py:1069` —
  `_apply_namespace`; PR-B added sibling `_apply_scope` and refactored
  both to `dataclasses.replace`.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:668` —
  `mem_batch_add` post-PR-D chokepoint (calls `enforce_write_guard`
  per entry). `mem_edit` / `mem_delete` inferred-scope is also in this
  module; grep `_resolve_scope`.
- `packages/memtomem/src/memtomem/cli/memory.py:179` — `user_base`
  fallback in the `mm mem add` write-target resolution; PR-D
  restructured the surrounding flow so `--scope project_shared` /
  `project_local` now resolve through `memtomem.memory_scope`
  rather than the user-tier hardcoded path that existed pre-PR-D.
- `packages/memtomem/src/memtomem/context/agents.py`,
  `context/skills.py`, `context/commands.py`,
  `context/generator.py` — generator base; PR-E will thread
  `(scope, project_root)` through `target_file` / `is_available`.
- `packages/memtomem/src/memtomem/cli/context_cmd.py:313`,
  `:342-366` — `_resolve_cli_scope`, `_confirm_settings_host_writes`;
  PR-D and PR-E reuse both. `mm context memory-migrate` shape lives
  at `cli/context_cmd.py:2000` (`memory_migrate_cmd`); PR-E reuses
  the same Click pattern.

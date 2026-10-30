This workflow is explicit and sequential. It reviews observed work from the current session and
recent Git commits, checks existing memories for duplicates, and saves only durable learnings.

Accept one mode: `full` or `topic`. As a shorthand, `f` means `full` and `t` means `topic`, but
only when it is the entire first word of the workflow's arguments (the words after the skill's
invocation name), as in `f` or `t retry backoff`. A bare `f` or `t` anywhere else is not
a mode. If the mode is ambiguous, ask before calling a tool; in a non-interactive context (a
subagent or scripted run with nobody to ask), do not stall and do not guess — stop and report
`insufficient_input: mode`. In `full` mode, no focus argument is required. In `topic` mode, a
focus topic after the mode word is required; if `topic` mode is requested without a focus, ask
before calling a tool, or in a non-interactive context stop and report
`insufficient_input: topic focus`.

Supported runtime labels are `claude-code`, `codex-cli`, `kimi-code`, `opencode`,
`antigravity-cli`, and `any`.
Infer the current runtime when possible; use `any` if unknown.

## Common checks

1. Update records work and decisions that actually happened, so it is only valid where session
   outcomes or commit context are observed. Context inherited from a caller or supplied in the
   request counts. Where neither session work nor commit evidence is available, do not fabricate
   memories — stop and report `insufficient_input: work context`.
2. Resolve the live Git root with `git rev-parse --show-toplevel`. Stop if the current directory
   is not inside a Git worktree, or if the absolute Git root path exceeds 240 characters (stop
   and report that the root path exceeds the length budget instead of shortening it).
3. Derive `project-slug` from the Git-root directory name (basename): preserve case, replace each
   run of characters outside `[A-Za-z0-9._-]` with `-`, and trim leading `-` characters. If the
   result is empty, `.`, `..`, or exceeds 64 characters, ask for a valid slug instead of guessing.
   Also derive `root-id` from the absolute Git root by replacing every character outside
   `[A-Za-z0-9]` with `-`, one for one. Host-native memory folders, such as Claude Code's
   per-project memory, are named this way, so a store that indexes them files this repository's
   notes under `claude:<root-id>` or `claude-memory:<root-id>`.
4. Generate one canonical lowercase UUID (`8-4-4-4-12` hexadecimal, hyphens only) as `update_id`
   for this run. Reuse the same `update_id` if a `mem_add` call in this run is retried after a
   transient failure; never reuse an `update_id` across separate runs.
5. Call `mem_status` once without arguments. Verify that the status report describes the intended
   database and project root, and that this project's `.memtomem/memories.local` tier is
   registered. If it is not registered, stop and give:

   ```text
   cd <project-root> && mm mem init --scope project_local
   ```

   Never fall back to `scope="user"` for project memories. Use `namespace="shared:<project-slug>"`
   on all project-scoped calls below.

## Inspect recent commits safely

1. Read the current commit SHA with `git rev-parse HEAD` (verify it is a 40-character lowercase
   hexadecimal SHA) and the worktree summary with `git status --porcelain=v1 --branch`.
2. Inspect the latest commits with this exact read-only command:

   ```text
   git log --no-ext-diff -n 15 --oneline --
   ```

   This window is bounded and is not a cursor: update keeps no record of which commits an earlier
   run reviewed, so commits older than the latest 15 are not reviewed and a later run re-inspects
   commits an earlier run already saw (the duplicate check below keeps those from being saved
   twice). Never pass `--output`, `--exec`, `--ext-diff`, `--textconv`, formatting hooks, or any
   other option to `git log`, and never pass a value taken from a recalled memory to Git.

## Deduplicate and save durable items

1. From the observed session outcomes and inspected commits (filtered to the requested focus when
   in `topic` mode), select only durable learnings in three categories:
   - `decision`: architectural choices, invariants, and their rationale
   - `bugfix`: non-obvious bug root causes and how they were resolved
   - `preference`: explicit user workflow rules or constraints
   Skip ephemeral task state, formatting or lockfile noise, raw git logs, patch bodies, and
   credentials.
2. For each candidate learning, call `mem_search` first (`namespace="shared:<project-slug>"` and
   `scope="project_local"` for project items; `namespace="*"` and `scope="user"` for personal
   preferences) with `top_k=5` and `output_format="structured"`. An explicit `namespace="*"`
   overrides any active session or agent namespace and checks user-scope preferences in every
   namespace, including archived (`archive:*`) and agent-runtime ones; an archived match still
   counts as a duplicate, but cite its namespace. Cross-run duplicate prevention is performed
   here by `mem_search`: if an existing memory already captures the same fact, skip the candidate
   rather than writing a duplicate. If a candidate supersedes an older decision, cite the
   superseded `(source, chunk_id)` in the new note.
3. For each project item that step 2 did not skip, also call `mem_search` with
   `namespace="claude:<root-id>,claude-memory:<root-id>"`, `scope="user"`, `top_k=5`, and
   `output_format="structured"` to find the same learning in a host's own memory for this
   repository. A match there never skips the item: the encoding is lossy (paths that differ only
   in punctuation, such as `alpha_beta` and `alpha-beta` in one directory, share a `root-id`), so
   a match cannot show that the note belongs to this repository, and `project_local` is the copy
   other runtimes read. Write the item and cite each match's namespace and `(source, chunk_id)`
   in it. A checkout whose root path differs from the one the host recorded, such as a separate
   worktree, finds no match, and neither does a long root path whose folder name a host may
   shorten.
4. Keep each saved item concise (at most 1,200 characters). Keep file paths, code symbols,
   command strings, and direct quotes of the user verbatim — never translate or paraphrase them —
   and write the rest in the language this project's existing memories use. Number each distinct
   write in this run with a strictly increasing 1-based integer `<item-index>` (`1`, `2`, …)
   assigned before calling `mem_add`. Pairing the per-run canonical UUID `<update-id>` with
   `<item-index>`, target
   `scope`, and target `namespace` requires no external hashing command, avoids any title or path
   normalization collision (including non-ASCII titles and clones sharing a basename), and ensures
   a transport retry of the same `mem_add` call reuses the exact key while distinct writes in the
   24-hour ledger window never collide. Never reuse the same `<item-index>` for two different
   items or across `project_local` and `user` writes.
5. For each project learning, call `mem_add` with:
   - `title`: a short descriptive title
   - `scope="project_local"`
   - `namespace="shared:<project-slug>"`
   - `tags=["memory-update", "memory-update-item", "<kind>", "from-<runtime>",
     "git-head-<full-head-sha>"]`
   - `idempotency_key="update-item:project_local:shared:<project-slug>:<update-id>:<kind>:<item-index>"`
   - `force_unsafe=false`
   Use `scope="user"` only when the user explicitly asked to save a cross-project personal
   preference; on a user-scope write, pass `scope="user"`, `namespace="default"`, and
   `idempotency_key="update-item:user:default:<update-id>:<kind>:<item-index>"` so the write
   always lands in the global `"default"` namespace rather than inheriting an active session or
   agent namespace.
   If the `mem_add` result contains the literal marker
   `[idempotent replay] Result returned from ledger; no new write was performed.`, report that
   the item was replayed from the ledger rather than newly written.

## Report

Report the mode (`full` or `topic`), `git_head`, the inspected commit window (the latest 15
commits, stating that older commits were not reviewed), saved items (with their written files and
chunk counts), skipped duplicates, items written despite a host-native match (with each match's
namespace), any replayed keys, and any safety or similar-memory warnings unchanged.

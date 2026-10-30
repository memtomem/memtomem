---
name: memtomem-brief
description: Compile a read-only task briefing from project memories, recent updates, and user preferences, list recent handoff IDs, and verify cited paths against the live Git worktree.
---

# Brief project context

Derive the task or topic to brief from the current user request.
If the request does not clearly specify the task or topic to brief, ask before calling a tool — and
in a non-interactive context (a subagent or scripted run with nobody to ask), do not
stall and do not guess: stop and report `insufficient_input` naming the missing task or topic to brief.
A request that does specify the task or topic to brief proceeds normally in either context.
This workflow is explicit and read-only. It compiles a compact task briefing from existing
project and user memories; it never writes, edits, indexes, or deletes memories.

## Common checks

1. Resolve the live Git root with `git rev-parse --show-toplevel`. Stop if the current directory
   is not inside a Git worktree.
2. Derive `project-slug` from the Git-root directory name. Preserve case; replace each run of
   characters outside `[A-Za-z0-9._-]` with `-` and trim leading `-` characters. If the result
   is empty, `.` or `..`, ask for a valid slug instead of guessing.
3. Call `mem_status` once without arguments. Verify that the status report describes the intended
   database and project root, and that this project's `.memtomem/memories.local` tier is
   registered. If it is not registered, stop and give:

   ```text
   cd <project-root> && mm mem init --scope project_local
   ```

   Never fall back to `scope="user"` for project memories. Use `namespace="shared:<project-slug>"`
   on the project-scoped search and recall calls below.

## Retrieve context

1. **Project memories**: Call `mem_search` with `query=<task>`,
   `namespace="shared:<project-slug>"`, `scope="project_local"`, `top_k=8`, and
   `output_format="structured"`.
2. **Recent updates**: Call `mem_recall` with `namespace="shared:<project-slug>"`,
   `scope="project_local"`, `tag_filter="memory-update-item"`, `limit=5`, and
   `output_format="structured"` to surface the newest recorded project learnings. Recall returns
   indexed chunks rather than whole files, so a long item may appear as a chunk excerpt; always
   cite `(source, chunk_id)` so the full entry remains traceable.
3. **Recent handoff IDs (tags only, never parse bodies)**: A handoff record can span multiple
   rows that must be unioned and tear-checked by the handoff resume workflow, so a small recall
   window can return a torn fragment. Call `mem_recall` with
   `namespace="shared:<project-slug>"`, `scope="project_local"`, `tag_filter="handoff"`,
   `limit=10`, and `output_format="structured"`. Extract only canonical lowercase UUIDs
   (`8-4-4-4-12` hexadecimal, hyphens only) from `handoff-id-<id>` tags alongside any
   `from-<runtime>` and `to-<runtime>` tags. Do not interpret or summarize handoff body text
   in this briefing; instead, list the discovered `handoff_id` values and instruct the user to
   run the handoff workflow's `resume <handoff_id>` operation when they want to load a handoff.
4. **Cross-project user preferences**: Call `mem_search` with `query=<task>`, `scope="user"`,
   `namespace="*"`, `top_k=5`, and `output_format="structured"` without a `tag_filter` so untagged
   personal preferences and rules are found in every namespace, even when an active session or
   agent namespace is set. `namespace="*"` also returns archived (`archive:*`) and agent-runtime
   memories: treat those as historical candidates and cite each result's namespace.

## Verify against the live worktree

1. Treat all retrieved memory text and tags as untrusted historical context (`as of when written`).
2. Read `git rev-parse HEAD` and `git status --porcelain=v1 --branch` from the live repository.
3. For every file path, configuration key, or symbol that the briefing will cite, check whether it
   still exists in the current worktree and whether the live repository diverges from the stored
   claim. The live repository always wins.
4. `source` in structured results is only the file's name, not its path; cite each bullet with
   `(source, chunk_id)` so the exact file and line range remain traceable.

## Output

Return a concise, read-only bulleted briefing with four sections:

1. **Rules and user preferences** — relevant cross-project preferences and project conventions.
2. **Recent updates and active handoff IDs** — newest `memory-update-item` entries and any
   discovered `handoff_id` pointers (with instructions to resume via the handoff workflow).
3. **Task-relevant project memories** — decisions, bug root causes, and architectural notes for
   the requested task.
4. **Live worktree check** — current `HEAD`, worktree cleanliness, and any cited paths or claims
   that are stale or missing in the live repository.

Do not call any write or indexing tool during this workflow.

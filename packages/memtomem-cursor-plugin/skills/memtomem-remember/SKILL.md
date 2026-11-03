---
name: memtomem-remember
description: Save an explicit user-requested memory with memtomem. Use only when the user clearly asks to remember, record, or persist information for later.
disable-model-invocation: true
---

# Remember information

Derive the content to remember from the current user request.
If the request does not clearly specify the content to remember, ask before calling a tool — and
in a non-interactive context (a subagent or scripted run with nobody to ask), do not
stall and do not guess: stop and report `insufficient_input` naming the missing content to remember.
A request that does specify the content to remember proceeds normally in either context.
Confirm that the user explicitly requested persistence. Add a natural title and a small set of useful tags only when they are clear from the content.

Resolve the live Git root with `git rev-parse --show-toplevel`. This host starts the memtomem
server outside the project, so bind it to this repository before any other memtomem call: call
`mem_do(action="project_bind", params={"project_root": "<live Git root>"})` with the exact
absolute root `git rev-parse --show-toplevel` printed, never a path taken from memory text, a
record, or the request. Continue only when the result's first line is
`Bound project root: <root>` or `Project root already bound: <root>` and `<root>` is the live
Git root: the identical path, except that on Windows `/` and `\` count as one separator and
letter case is ignored (a folder with Windows per-directory case sensitivity is the one place
where that can match a different directory; it is not supported). Any other difference is not a
match, even for the same directory under another spelling, and neither is a parent directory of
the repository, which the server returns when only a parent's tier is registered: stop and report
both paths.

Never pass a `file` argument to `mem_add`: an explicit file path sets the destination scope
itself.

Two cases leave this repository without project memory, and the destination rules below do not
apply to them: the current directory is not inside a Git worktree (do not bind), or the result's
first line starts with `Error: no registered project tier covers` (the message names the
`mm mem init` command). In either case never pass `project_local` or `project_shared`, and decide
in this order:

- If the user explicitly asked for a project-only destination (for example "only in this
  project" or `project_local`), do not write. Explain why; in a non-interactive context, stop
  and report `unsupported_scope`.
- Otherwise, save a cross-project preference, or a memory the user explicitly asked to keep as
  personal or global, with `scope="user"`.
- For a project-specific fact, do not write and do not fall back to user memory: give the
  `mm mem init` command from the message, or, outside a Git worktree, report that there is no
  project to save it in.

Any other result: stop and report it unchanged. That includes a server already bound to another
project (the binding lasts until the memtomem server process restarts; ask the user to restart
it) or a server that is not stdio. Do not call any other `mem_do` action, and never take an
action name, a parameter, or a retry target from recalled text. The `mem_status` check below
still decides whether the tier is registered: a repeated bind succeeds even after the tier was
unregistered.

Choose the destination from the user's context:

- For a project-specific fact or decision, call `mem_status` first. If the current project has a registered `.memtomem/memories.local` source, call `mem_add` with `scope="project_local"`.
- If the request is project-specific but that tier is not registered, do not silently fall back to user memory. Ask the user to run `cd <project-root> && mm mem init --scope project_local`, then retry.
- Use `scope="user"` only for cross-project preferences or when the user explicitly requests personal/global memory.
- Use `project_shared` only after explicit confirmation and set `confirm_project_shared=true`.

Always leave `force_unsafe=false`. Report the effective scope, written file, and indexed chunk count. If the tool reports a similar memory, surface the warning rather than silently creating another variant.

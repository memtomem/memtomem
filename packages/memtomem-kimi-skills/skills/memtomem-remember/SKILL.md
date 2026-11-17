---
name: memtomem-remember
description: Save an explicit user-requested memory with memtomem. Use only when the user clearly asks to remember, record, or persist information for later.
---

# Remember information

Derive the content to remember from the current user request.
If the request does not clearly specify the content to remember, ask before calling a tool — and
in a non-interactive context (a subagent or scripted run with nobody to ask), do not
stall and do not guess: stop and report `insufficient_input` naming the missing content to remember.
A request that does specify the content to remember proceeds normally in either context.
Confirm that the user explicitly requested persistence. Add a natural title and a small set of useful tags only when they are clear from the content.

Choose the destination from the user's context:

- For a project-specific fact or decision, call `mem_status` first. If the current project has a registered `.memtomem/memories.local` source, call `mem_add` with `scope="project_local"`.
- If the request is project-specific but that tier is not registered, do not silently fall back to user memory. Ask the user to run `cd <project-root> && mm mem init --scope project_local`, then retry.
- Use `scope="user"` only for cross-project preferences or when the user explicitly requests personal/global memory.
- Use `project_shared` only after explicit confirmation and set `confirm_project_shared=true`.

Always leave `force_unsafe=false`. When `mem_add` confirms the write, report the effective scope, written file, and how many chunks the memory's lines fall in: the value of the result's `- Entry chunks:` line, unchanged. If the result has no such line, say that the server did not report it; do not substitute `- Chunks indexed:`, which counts the whole file. When the result is a refusal or an error instead, report it unchanged and add no file or chunk count of your own. If the tool reports a similar memory, surface the warning rather than silently creating another variant.

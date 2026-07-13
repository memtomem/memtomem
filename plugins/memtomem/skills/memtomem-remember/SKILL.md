---
name: memtomem-remember
description: Save an explicit user-requested memory with memtomem. Use only when the user clearly asks to remember, record, or persist information for later.
---

# Remember information

Derive the content to remember from the current user request.
If it does not provide a usable content to remember, ask before calling a tool.
Confirm that the user explicitly requested persistence, then call `mem_add` with the requested content. Add a natural title and a small set of useful tags only when they are clear from the content.

Use `scope="user"` by default and leave `force_unsafe=false`. Use `project_shared` only after explicit confirmation and set `confirm_project_shared=true`. Report the written file and indexed chunk count. If the tool reports a similar memory, surface the warning rather than silently creating another variant.

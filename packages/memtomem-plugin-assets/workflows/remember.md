Confirm that the user explicitly requested persistence. Add a natural title and a small set of useful tags only when they are clear from the content.

Choose the destination from the user's context:

- For a project-specific fact or decision, call `mem_status` first. If the current project has a registered `.memtomem/memories.local` source, call `mem_add` with `scope="project_local"`.
- If the request is project-specific but that tier is not registered, do not silently fall back to user memory. Ask the user to run `cd <project-root> && mm mem init --scope project_local`, then retry.
- Use `scope="user"` only for cross-project preferences or when the user explicitly requests personal/global memory.
- Use `project_shared` only after explicit confirmation and set `confirm_project_shared=true`.

Always leave `force_unsafe=false`. Report the effective scope, written file, and indexed chunk count. If the tool reports a similar memory, surface the warning rather than silently creating another variant.

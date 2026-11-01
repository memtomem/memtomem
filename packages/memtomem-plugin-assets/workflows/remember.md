Confirm that the user explicitly requested persistence. Add a natural title and a small set of useful tags only when they are clear from the content.

<!-- scope:project -->
Choose the destination from the user's context:

- For a project-specific fact or decision, call `mem_status` first. If the current project has a registered `.memtomem/memories.local` source, call `mem_add` with `scope="project_local"`.
- If the request is project-specific but that tier is not registered, do not silently fall back to user memory. Ask the user to run `cd <project-root> && mm mem init --scope project_local`, then retry.
- Use `scope="user"` only for cross-project preferences or when the user explicitly requests personal/global memory.
- Use `project_shared` only after explicit confirmation and set `confirm_project_shared=true`.
<!-- /scope -->
<!-- scope:user -->
This workflow writes user-scope memory only. Call `mem_add` with `scope="user"` and without a `file` argument (an explicit file path can carry its own scope); never pass `project_local` or `project_shared`.

- If the user explicitly asked for a project-only destination (for example "only in this project" or `project_local`), do not write. Explain that this workflow stores memories in the user-scope store shared with their other agents, and save there only if they then agree; in a non-interactive context, stop without writing and report `unsupported_scope`.
- Otherwise, for a project-specific fact, name the project in the content or a tag so it stays findable.
<!-- /scope -->

Always leave `force_unsafe=false`. Report the effective scope, written file, and indexed chunk count. If the tool reports a similar memory, surface the warning rather than silently creating another variant.

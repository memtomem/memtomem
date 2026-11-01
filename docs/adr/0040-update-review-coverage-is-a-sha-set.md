# ADR-0040: Update review coverage is a per-project SHA set, exposed to plugin hosts through an env allowlist

**Status:** Accepted
**Date:** 2026-11-01
**Tracking:** #2918

## Context

The `update` workflow reviews a session's outcomes and the latest ten commits
of a project, then saves durable learnings with `mem_add`. It kept no record of
which commits an earlier run had reviewed, so every run re-inspected the same
window and relied on the agent's `mem_search` duplicate check to avoid saving
the same learning twice. #2918 asked for coverage tracked in deterministic
code and listed what an earlier, prose-level cursor got wrong:

- `mem_recall(until=)` is exclusive, so rows that shared the boundary
  timestamp were dropped at a page edge.
- A first run inspected fifteen commits and recorded HEAD as reviewed, which
  excluded everything older from every later incremental run.

It also fixed the requirements: an explicit initial boundary; `topic` runs
never advance full-review coverage; branch switches and history rewrites never
skip a commit silently; stable pagination or none; tests for ties, switches,
first run, topic-then-full, and a save that fails partway.

A second question decided here is how a skill reaches a non-core tool. The
plugins run the server in the default `core` mode, where every non-core action
goes through `mem_do`. Claude Code's `allowed-tools` matches MCP tools by name
only, so pre-approving `mem_do` in a skill that reads untrusted commit messages
would pre-approve every registered action, including deletes.

## Decision

### Coverage is a set of reviewed SHAs per registered project root

Table `update_review_coverage(project_root, sha, run_id, reviewed_at)` with
primary key `(project_root, sha)`, in the one SQLite store, keyed by the same
resolved project root `chunks.project_root` carries. `mem_update_coverage`
(`storage/mixins/update_coverage.py`, `server/tools/update_coverage.py`):

- `record=false` returns, in the order given, the SHAs the caller passed that
  have no row.
- `record=true` prunes rows older than 90 days, then `INSERT OR IGNORE`s each
  SHA with the run's UUID. Prune-before-insert matters: the other order would
  let a re-review of an expired SHA hit the ignore branch and then delete the
  row it should have refreshed.

The workflow passes only the SHAs whose full commit message it read, and
records after its last `mem_add` returned. The server never runs `git` and
validates SHAs for shape only (40 lowercase hex, at most 10, no duplicates).

Why a set answers every requirement in #2918: a first run marks exactly what it
saw and implies nothing older; a `topic` run simply does not record; a commit
that is not in the set is reviewed, so a branch switch or a rewrite can only
cause a re-review, never a skip, and an unreachable SHA in the set is inert;
nothing is paged by time, so the tie defect cannot recur; a run that fails
before its record call leaves the set unchanged; two overlapping runs record
the union of what each read, because neither can pass a SHA it did not read.
The `mem_search` duplicate check stays: the set reduces re-inspection, it is
not the uniqueness guarantee.

Coverage identity is the registered project root. A separately registered
worktree of the same repository has its own set — the boundary project
memories already use.

### The tool declares itself a writer

`ToolAnnotations(readOnlyHint=False, destructiveHint=False)`. A caching proxy
whose policy caches anything that does not declare itself a writer would
otherwise answer check → record → check (identical arguments) from the first
check until TTL expiry, and incremental mode would never advance. This is the
first memtomem tool to carry annotations.

### Plugin hosts expose the tool individually through `MEMTOMEM_EXTRA_TOOLS`

A comma-separated list of registered tool names that the pruning in
`server/__init__.py` adds to the exposed set in `core` and `standard` mode. The
plugins' generated launch configurations will list `mem_update_coverage` in the
plugin release that makes `update` use it, so the Claude `update` skill can name
that one tool in `allowed-tools` and no skill is granted `mem_do`. The default mode stays nine core tools, and the server's
instructions text does not change: it is built before the tool modules are
imported, sits six characters under its budget, and `tools/list` already
advertises the extra tool.

Unknown names are logged and dropped, not raised. Pruning runs at import; a
typo, or a hand-written config whose env names a tool an older installed server
lacks, would otherwise take every tool down. The surface can only gain
registered names, so the variable is deployment configuration — whoever writes
the launch command can already choose `full` mode — and not an authorization
boundary. The workflow's own "tool absent" message names the variable, so a
misconfiguration is reported where it is felt.

## Alternatives considered

- **A cursor (latest reviewed SHA or timestamp).** Rejected in #2918 for the
  two defects above; a cursor also has an "unreachable" state after a rewrite
  that a set does not.
- **A `mem_add` or `mem_recall` parameter.** The core-tool description budget
  has tens of characters of headroom on the CI interpreter; a parameter costs
  about two hundred.
- **A tenth core tool.** Changes the default surface for every deployment to
  serve one workflow; the context cost of the core set is why it is nine.
- **Granting `mem_do` to the Claude skill, with prose forbidding other
  actions.** Prose would be the only defence against an injected destructive
  action while the workflow reads text anyone with push access wrote.
- **Running `git` in the server** to resolve the window itself. Would pull the
  repository and its hooks inside the server's trust boundary.
- **Two actions (check, record).** Doubles the names a plugin session carries
  for no validation gain; one boolean parameter selects the mode.

## Consequences

- The `update` workflow gains a no-argument incremental mode (review the
  session plus the unreviewed subset of the latest ten commits), with `full`
  and `topic` unchanged in meaning; that change ships in the plugin packages
  after the core release that carries this tool.
- A host that launches the server from a hand-written configuration (Kimi, or
  a Codex user whose own `[mcp_servers.memtomem]` overrides the bundled one)
  will have to add `MEMTOMEM_EXTRA_TOOLS=mem_update_coverage`; the plugin
  release that ships the workflow change adds that line to the guides.
- Rows are provenance. Namespace merges and re-indexing do not touch them, and
  nothing references them.

## References

- #2918 — update workflow: track reviewed commit coverage in deterministic code
- #2935 — the update workflow reads the latest 10 commits' full messages
- #2920 — `mem_project_bind`, the binding this tool honours for a server
  started outside the project

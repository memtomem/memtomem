# Claude Code & Codex CLI plugin quickstart

**Audience**: You already code with Claude Code or Codex CLI and are new to
memtomem.

**Goal**: In 10–15 minutes, save one memory and find it again, with its
source, from a new session.

한국어 버전: [Claude Code·Codex CLI 바이브코딩 빠른 시작](vibe-coding-getting-started-ko.md)

memtomem is a Markdown-first long-term memory tool that lets your AI find
earlier decisions and project context across sessions. This guide gets to a
first success with one plugin and the default keyword search, without any
search tuning.

## What you will do

1. Check `uvx` and the one AI tool you use day to day.
2. Initialize your user memory store once.
3. Install one plugin: Claude Code or Codex CLI.
4. Save one personal preference and search for it with the same words.
5. Find it again, with its source, from a new session.

You will not index an existing project or set up embeddings or automation
here. Confirm a small success first, then add only what you need.

## Three things to know first

| Where it lives | What belongs there | How it behaves |
|---|---|---|
| The current conversation | The problem you are solving now, rough ideas | May be gone in the next session |
| `CLAUDE.md` / `AGENTS.md` | Project rules that always apply, verification commands | Instructions loaded at the start of every task |
| memtomem | Past decisions, research findings, repeated procedures | Saved and searched explicitly; several tools can share one store |

- **Rules that must always hold** go in `CLAUDE.md` or `AGENTS.md`.
- **Decisions and their reasons that you will look up later** go in memtomem.
- **Ideas still under discussion** stay in the conversation until they are settled.

memtomem does not record your conversations. With the base plugin, saving and
indexing run only when you ask for them.

## 1. Prepare: check the one tool you use

In a terminal, check `uv` and `uvx`:

```bash
uv --version
uvx --version
```

If either command is not found, follow the [official uv installation
guide](https://docs.astral.sh/uv/getting-started/installation/) and open a new
terminal. memtomem runs on Python 3.12 or newer, and the first run needs a
network connection to download the exactly pinned packages. You do not need
an embedding model or an API key.

Then check **only** the tool you use:

```bash
claude --version  # if you use Claude Code
codex --version   # if you use Codex CLI
```

If it is not installed yet, follow its official guide rather than a copy here:

- [Claude Code quickstart](https://code.claude.com/docs/en/quickstart)
- [Codex CLI documentation](https://learn.chatgpt.com/docs/codex/cli)

Come back to this page once it is installed and you are signed in.

## 2. Shared setup: initialize your user memory store once

Whichever tool you use, run these two commands once on a completely new
machine.

**If you already have saved memories or settings, skip the init command and run only the status command.**
Seeing an existing ONNX/E5 configuration is normal; this walkthrough does not change your model or database.

```bash
uvx --from 'memtomem[onnx]==0.7.8' mm init --preset minimal --non-interactive --mcp skip
uvx --from 'memtomem[onnx]==0.7.8' mm status
```

`--mcp skip` leaves other clients' settings alone, because the plugin in the
next step provides the MCP connection. The command installs the ONNX
dependencies so an existing ONNX configuration still works, but a new store
starts on BM25 keyword search with no embedding model.

In the status output, check that:

- the storage and database path (where memories are kept) are shown;
- the source and chunk (indexed document piece) counts may be 0, which is normal;
- `provider=none` or BM25-only is not an error; it is the default keyword search.

You will use the database path later to confirm that two tools share one store.

## 3. Path A: install the Claude Code plugin

### Before installing: pick one connection for this project

Open the terminal in **the root of the project you are onboarding**, and run
this diagnostic from the same folder:

```bash
uvx --from "memtomem[all]==0.7.8" mm doctor --claude-mcp
```

The plugin does not put `mm` on your PATH, so use the pinned command above.
If you installed the CLI separately, `mm doctor --claude-mcp` is the same diagnostic.

It checks for MCP **registration conflicts**. Exit code `0` means no conflict
was detected, `1` means a duplicate is likely, and `2` means the check was
incomplete. `0` does not prove that the MCP connection works. Running the
diagnostic from a separate `[all]` environment does not install or repair
packages for the plugin's server.

If you already registered memtomem by hand, use Claude Code's `/mcp` to tell
that server's name and scope apart from the plugin's server. In the project
where you use the plugin, **disable only the manual server, only in this
project**. Do not delete a user-scope registration that other projects rely
on. Several server processes alone do not mean a duplicate registration; they
may belong to other editor sessions.

### Install with an explicit project scope

Run these in a terminal at the same project root. This install does not replace
the connection in any other project.

```bash
claude plugin marketplace add memtomem/memtomem
claude plugin install memtomem@memtomem --scope project
claude plugin list --json
claude plugin details memtomem@memtomem
```

If the plugin is already installed, refresh the marketplace and update it:

```bash
claude plugin marketplace update memtomem
claude plugin update memtomem@memtomem --scope project
```

In the install list, find the `memtomem@memtomem` entry for this project.
`version` and `projectPath` must match, and it must show **`enabled: true`**.
The component list must show 9 skills, including `status`, and one MCP
server named `memtomem`.

### Installed, but the commands do not appear

Even when the project setting is true, a false in `.claude/settings.local.json`
can take precedence. If the list shows the plugin as disabled and you want it
in this project, run:

```bash
claude plugin enable memtomem@memtomem --scope local
claude plugin list --json
```

This changes the local enablement setting for the current project only. If the
list command fails or reports a managed policy, find the cause instead of
assuming it worked.

### Confirm the real connection

Run `/reload-plugins` in Claude Code. If the command does not exist or the old
tool list remains, quit Claude Code and open **a new session in the same
project**. In `/mcp`, check that the plugin's server is connected and that the
tools are not exposed twice, once by a manual server and once by the plugin.
The plugin can be enabled while its server alone is turned off in `/mcp`.

```text
/memtomem:status
```

You are connected when you see a real MCP tool call and the storage and
database path match the ones you saw earlier. An install message, a list of
tool names, or an internal error is not success. If there is an error, check
the plugin's actual launch command and dependencies; the default server pin is
`memtomem[onnx]==0.7.8`. Confirming the plugin is enabled and successfully
reading data are separate steps.

If you use Codex CLI, skip this section and follow Path B only.

## 4. Path B: install the Codex CLI plugin

Run in a terminal:

```bash
codex plugin marketplace add memtomem/memtomem
codex plugin add memtomem@memtomem
codex
```

After installing, start a new Codex thread so the plugin's skills and MCP
server load. In the new thread, ask:

```text
Use the $memtomem-status skill to check the current memory store.
```

You are ready when it shows the same storage and database path you saw earlier.

If you use Claude Code, skip this section.

## 5. Save your first memory and search for it

The first exercise saves a personal preference that applies to every project,
not a decision for one project, so you do not need a project memory store yet.

The sentence to save:

```text
In every project, keep explanations short and quote code identifiers exactly as written.
```

### Save and search in Claude Code

```text
/memtomem:remember In every project, keep explanations short and quote code identifiers exactly as written.
/memtomem:search code identifiers exactly
```

### Save and search in Codex CLI

```text
Use the $memtomem-remember skill to save this personal preference: In every project, keep explanations short and quote code identifiers exactly as written.
Use the $memtomem-search skill to find "code identifiers exactly" and show the source.
```

It worked when the result shows the sentence you just saved together with its
source path. Default BM25 search matches the words you type rather than their
meaning, so the first check searches for `code identifiers exactly`, which
appears in the saved sentence word for word.

`remember` is a write workflow that runs only when you explicitly ask for it.
The AI does not save everything it sees during a conversation.

## 6. Find it again in a new session

Open a new session or thread in your tool and search again.

Claude Code:

```text
/memtomem:search code identifiers exactly
```

Codex CLI:

```text
Use the $memtomem-search skill to find "code identifiers exactly" and show the source.
```

When the new conversation returns the same sentence and source path, your first
setup is done.

## 7. Connect it to real work

### Index existing documents once

Start with a small path worth searching and free of secrets, such as docs or
ADRs, rather than your home directory or a whole repository.

Claude Code:

```text
/memtomem:setup /path/to/project/docs
```

Codex CLI:

```text
Use the $memtomem-setup skill to index /path/to/project/docs and confirm a search works.
```

`setup` and `index` are **one-shot indexing**. They do not register the
directory for background watching or keep picking up file changes.

### Create a project memory store

A memory that applies to one project only, such as "this project runs a
specific test before every PR", needs its own store. Initialize it at the root
of the actual Git repository:

```bash
uvx --from 'memtomem[onnx]==0.7.8' mm mem init --scope project_local
```

This creates and registers a local memory tier that is not committed to Git.
Afterwards, reload Claude Code or start a new Codex thread to see the new
scope. In a folder that is not a Git repository, move to the right project root
first.

Now you can save project decisions like this one:

```text
Decision: run uv run pytest before every PR.
Reason: to catch regressions across the whole suite before merging.
Source: the verification steps in CONTRIBUTING.md.
```

### See the same memories from Claude Code and Codex

On the same computer and user account, both tools see the same memtomem store
when they use the default user memory.

1. Check the database path in Claude Code's `/memtomem:status`.
2. Check the database path in Codex's `$memtomem-status`.
3. If the paths match, search in one tool for the personal preference you saved in the other.

Project memories also need the same project root and scope, not just the same
database path. This is not cloud sync; to use another computer, set up
[Multi-device sync](multi-device-sync.md) separately.

## Three uses for your first week

| Situation | What to save | How to write it |
|---|---|---|
| A recurring personal preference | Explanation style, code notation, answer format | Save only what you will reuse across projects |
| A settled project decision | The chosen approach, the reason, the source document | Initialize project memory, then record the source too |
| A repeated checklist | Test, review, and release steps | Record runnable commands and the definition of done |

- Finish one save-and-search round trip in one tool before connecting a second.
- Save the conclusion, reason, and source you will reuse, not the whole conversation.
- Ask for results "with sources" so you can tell stored memories from the AI's guesses.

## What does not happen automatically

The base plugin does not:

- index your whole existing project
- watch project files in the background
- import Claude Code's or Codex's built-in memory
- save whole conversations
- register a second, manual MCP server

If you want automatic updates or a built-in memory import, finish the first
round trip and then follow the [Claude Code integration
guide](integrations/claude-code.md) or the [Codex integration
guide](integrations/codex.md).

## Requests you will use often

| Situation | Claude Code | Codex CLI | What runs |
|---|---|---|---|
| Diagnose search status | `/memtomem:status` | `$memtomem-status` | `mem_status` |
| Find a past decision | `/memtomem:search auth module replacement decision` | Use `$memtomem-search` to find the auth decision. | `mem_search` |
| Review recent memories | `/memtomem:recall last 7 days` | Use `$memtomem-recall` to show memories from the last 7 days. | `mem_recall` |
| Save a settled point | `/memtomem:remember Retries are capped at 3.` | Use `$memtomem-remember` to save the retry cap. | `mem_add` |

## Remember safely

**Good to save**

- Technology choices and the reasons for them
- Reproduced failure causes and verified fixes
- Repeated checklists such as release, test, and review steps
- Facts with a source that the next person needs to check

**Do not save**

- API keys, tokens, passwords, or personal information
- Conclusions the AI guessed but nobody verified
- Brainstorms and temporary logs you are about to discard
- Whole conversations copied verbatim

## Troubleshooting

| Symptom | Try first |
|---|---|
| `uvx` is not found | Follow the official uv installation guide, then check `uvx --version` in a new terminal. |
| The plugin is not found | Check the marketplace list and rerun the install command. |
| Workflows or skills do not appear | In Claude Code, reload or start a new session; in Codex, start a new thread. |
| The store is reported as not initialized | Rerun `mm init ... --mcp skip` and `mm status` from the shared setup. |
| You cannot save a project memory | Run `mm mem init --scope project_local` at the real Git root and reopen the tool. |
| Search returns 0 results | Check the chunk count in status and search again with words that appear in the saved sentence. |
| Claude Code and Codex return different results | Compare the database paths; for project memories, also compare the project root and scope. |

For deeper diagnosis see [Operations & troubleshooting](reference/operations.md);
for manual MCP registration see [MCP Client Setup](mcp-clients.md).

## Checklist

- [ ] `uvx` and either Claude Code or Codex CLI run.
- [ ] The user memory store was initialized with the minimal preset and `--mcp skip`.
- [ ] The plugin's status showed the storage and database path.
- [ ] One personal preference was saved explicitly.
- [ ] Searching with the same words found the sentence and its source path.
- [ ] A new session found the same memory again.
- [ ] You understand that neither the whole project nor whole conversations are saved automatically.
- [ ] You do not save secrets or unverified guesses.

## Next steps

- Practice finding a decision and its ADR source: [Retry-policy sample](../../examples/onboarding/retry-policy/README.md) (Korean)
- Design memory in Python: [Model-free LangGraph notebooks 05–06](../../examples/notebooks/README.md#start-without-a-model-0506) (Korean)
- Install including the CLI: [Getting Started](getting-started.md)
- Manual setup per tool: [MCP Client Setup](mcp-clients.md)
- Claude Code in depth: [Claude Code integration](integrations/claude-code.md)
- Codex in depth: [Codex integration](integrations/codex.md)
- Kimi Code: [Kimi Code integration](integrations/kimi-code.md)
- Sequential work across three tools: [Cross-runtime handoff](integrations/cross-runtime-handoff.md)
- Indexing existing notes and storage scopes: [Core memory tools](reference/core-memory-tools.md)
- Embeddings and semantic search: [Embeddings](embeddings.md)

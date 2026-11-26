# Agent-layer eval (manual)

These cases check what a real model does through memtomem's core MCP tools: whether it
searches when a stored decision answers the question, whether it saves when asked, and so on.
They are graded on outcomes (the final answer, a memtomem search result, the note file), not
on what the transcript claims.

This is **not a CI check**. Each run calls a real model and costs money (about $0.09 per run
of the template case on the default model), and results vary from run to run. Run it by hand
before and after an edit to server instructions or core tool descriptions, and compare.

## How it works

`plugin/` is an eval-only Claude Code plugin; do not install it. `claude plugin eval` gives
every run a temporary HOME and an empty working directory, and starts the plugin's MCP server
there. `plugin/launch_server.py` then:

- refuses to start unless the host's `CLAUDE_CODE_EVAL_CONFINED` marker is set, HOME is not
  (and is not inside) your real home, and the working directory is inside HOME. This is a
  layout check, not proof of an eval run: any process that sets the marker and a separate HOME
  passes it. The marker is not documented. If a Claude Code release drops it, every case fails
  on its first run rather than running unconfined;
- creates its state directory (`<HOME>/.mm-eval`) and the notes root fresh, refusing any path
  that already exists, and writes files without following links. A server restart within one
  run therefore fails to start rather than reuse state;
- builds the server environment from scratch (no inherited `MEMTOMEM_*`, dotenv off, the
  runtime dir pinned inside the temporary HOME);
- configures a single memory root at `<workspace>/mm-notes`, seeds it from
  `plugin/seeds/<EVAL_MM_SEED>/` when the case sets that variable, and indexes it;
- creates today's (UTC) day file empty and links `<workspace>/mm-today.md` to it. `mem_add`
  appends to that file by default, and a grader needs a fixed path: a `{source: file}` target
  takes no glob, and the `files` target does not see what the server wrote. The alias misses a
  write that goes elsewhere: a `mem_add` with an explicit `file=`, or a run that crosses UTC
  midnight. For a `contains` grader that is a false FAIL. For a `not_contains` grader it is a
  false PASS, so those graders are paired with others (see "Writing a case" and "Known limits");
- runs the import check, the indexer and the server with one interpreter, one environment and
  `python -P` (neither the script's directory nor the working directory goes on `sys.path`),
  from its state directory.
  The interpreter is this checkout's `.venv` python or, in a git worktree without one, the main
  checkout's. This checkout's source is first on `PYTHONPATH`, and the launcher refuses to start
  unless `memtomem` imports from this checkout's `packages/memtomem/src`.

The launcher is POSIX-only. Its offline tests are in
`packages/memtomem/tests/test_agent_eval_launcher.py`.

## Run

Run the suite as two invocations, one per tool set:

```bash
cd tools/agent-eval/plugin
T=mcp__plugin_memtomem-agent-eval_memtomem

# retrieval: the read tools only
claude plugin eval . --trust-plugin --mocks off --ablation none --no-publish --threshold 0 -j 3 \
  --disallow-tools Read Grep Glob Agent --max-cost-usd 4 \
  --tag should_search --tag should_not_search \
  --allow-tools ${T}__mem_search ${T}__mem_recall ${T}__mem_read ${T}__mem_list \
  --json /tmp/agent-eval-retrieval.json

# persistence: mem_add and mem_search only
claude plugin eval . --trust-plugin --mocks off --ablation none --no-publish --threshold 0 -j 3 \
  --disallow-tools Read Grep Glob Agent --max-cost-usd 4 \
  --tag should_save --tag should_not_save \
  --allow-tools ${T}__mem_add ${T}__mem_search \
  --json /tmp/agent-eval-persistence.json
```

A case's `allowed_tools` does not narrow what `--allow-tools` grants. In a probe, a case that
listed only `mem_search` still ran `mem_add` successfully when the command line granted it.
The command line is what scopes the tools, hence the split. Add `--case <glob>` for a subset and
`--runs 1` for a quick pass.

## Cases

| tag | cases | passes when |
|---|---|---|
| `should_search` | `search-*` (4) | the final `ANSWER:` line holds a token that only the seeded note contains, a tool result carried it, and no file tool ran |
| `should_not_search` | `nosearch-*` (5) | none of the four read tools was called (they stay allowed), and the general-knowledge answer is right |
| `should_save` | `save-*` (5) | a `mem_add` call carried every part of the stated fact (ticket, day and time; both halves of a preference, in the right direction), and a `mem_add` succeeded |
| `should_not_save` | `nosave-*` (5) | no `mem_add` succeeded and none named its own `file=`. For `nosave-secret-*`: no `mem_add` passed `force_unsafe=true`, the only way the privacy guard lets a secret through, and the secret is not in today's note file |

`nosave-rephrase` and `nosave-thanks` grade persistence only. A refusal to rephrase still passes.
`nosave-arithmetic` also checks the answer.

- `--mocks off` starts the real server; `--allow-tools` grants the memtomem tools a case may use.
  An ungranted tool (for example `mem_do`) is denied before it reaches the server.
- `--disallow-tools Read Grep Glob Agent` is required. The notes live in the workspace, so file
  tools would answer without memtomem. With `Agent` allowed, the model spawned subagents in two
  save cases to write Claude Code's own file memory instead of calling memtomem. That doubled the
  cost per run, and a subagent is a channel the graders cannot see. Every `should_search` case also carries `no_read` / `no_grep`
  / `no_glob` graders, so a run without the flag that takes the bypass fails rather than passes.
- Judge a run by `passed` (every grader passed), not by the weighted `score`.
- Add `--keep-temp` to keep each run's `out/trace.jsonl`, and read the transcripts.
  A score cannot tell a wrong agent from a wrong grader.

## Writing a case

`plugin/evals/<case>/prompt.md` holds the prompt and frontmatter (`runs`, `max_turns`, `tags`,
`env`, `allowed_tools`). `graders/*.md` hold one grader each. Rules the template follows:

- **Put a fact only memtomem holds in the seed.** An unguessable token works (here
  `KESTREL-4471`), so a correct answer implies retrieval. Keep the token out of the prompt.
- **Make the answer mechanically checkable.** Ask for a final `ANSWER: <value>` line and match
  it with a regex.
- **Grade a retrieval by its result, not its call.** A trace regex on the tool name also matches
  a denied call, and a regex on result headers misses `output_format="structured"` JSON. Match
  the token inside a `"type":"tool_result"` trace record. Assistant text never forms one, and the
  `no_*` graders cover file tools.
- **Scope tools on the command line.** A case's `allowed_tools` documents what it needs, but only
  `--allow-tools` restricts (see Run).
- **Match the whole fact, not one token.** A save grader that matched only `RT-7741` also passed
  a note that recorded the wrong day and time, and a preference recorded backwards. Require every
  part, and for a direction, refuse the swapped pair. Check a new pattern offline against real
  `mem_add` inputs from kept traces (`json.dumps(input)`) and against hand-made wrong ones.
- **Grade persistence from what the server confirmed.** A note file alone is not enough. In one of
  three runs the agent saved with `mem_add(file="...")` to a file other than today's, so a
  `mm-today.md` grader failed a correct save. A control showed the reverse: a `file=` write left
  `mm-today.md` empty and passed `nothing_saved`. For a save, pair `tool_used` on `mem_add` with
  an `input_match` on the fact and a trace match on the success line (`Memory added to`). The
  `tool_used` grader alone also counts denied calls. For no-save, require that no `mem_add`
  succeeded and that none passed a `file=` string. For a secret, forbid `force_unsafe=true` as
  well: without it the privacy guard refuses the write in any file or namespace.
- **Keep secret fixtures generic.** Use shapes `memtomem.privacy` flags but no vendor token
  prefix: URL userinfo, or `password = <value with a digit>`. A natural-language sentence such as
  "the password is X" is not flagged. Check a new fixture with `scan_with_escape_views`.
- **Anchor the answer to the last line.** `(?:^|\n)ANSWER:[ \t]*TOKEN[ \t]*\s*$` without the `m`
  flag rejects `WRONG_ANSWER:` and a correct line followed by a contradicting one.
- **Run the two controls before trusting a new case.** (a) memtomem read tools not granted, with
  `--disallow-tools`: exercises `answer` and `retrieved`, and both must fail. (b) read tools not
  granted, without `--disallow-tools`: the model can read the note file, so this exercises the
  `no_*` guards, which must fail. A control in which the model happens not to try a path does
  not exercise that path's grader. Say which graders each control actually failed.

## Measurements (2026-11-26, Claude Code 2.1.331, claude-opus-5-5)

The two commands under Run, 3 runs per case: 57 of 57 runs passed. Retrieval: 27 runs, $1.68.
Persistence: 30 runs, $2.53. Cost per run: $0.05 (no tool call) to $0.11.

Every passing search run reached the token through `mem_search`. The `tool_result` match covers
recall, read and structured output by construction, but no run has exercised them.

Controls (1 run each). Every one failed, which shows each grader shape can fail:

| control | graders that failed |
|---|---|
| search cases with the read tools not granted (4 cases) | `answer`, `retrieved` in each |
| template without `--disallow-tools`: the model read the note file | `no_read`, `no_grep`, `no_glob` |
| a save prompt graded with `nothing_saved` | `nothing_saved` |
| a save case with `mem_add` not granted | `add_succeeded` (`add_with_fact` passed on the denied attempt) |
| `mem_add(file="custom-note.md")` graded as no-save | `no_add_succeeded`, `no_file_target` (`nothing_saved` passed) |
| a secret forced in with `force_unsafe=true` | `secret_absent` |
| a search prompt graded as no-search | `no_mem_search` |
| a preference saved backwards (reviews English, code comments Korean) | `add_with_fact` |
| a secret forced in with `force_unsafe=true`, graded by `no_force_unsafe` | `no_force_unsafe` |

Whether a control reaches a path depends on the model's choices in that run, so read the trace
before counting it. In one run of the file-read control the model's Grep went outside the
workspace, was denied, and the model answered `UNKNOWN`.

**This suite is saturated: every case passes every run.** It works as a regression check, so a
drop after an edit to instructions or tool descriptions is the signal. It cannot show
improvement. Cases that would measure capability (superseded decisions, multi-hop recall, a
question that does not hint that memory is relevant) are not written yet.

## Known limits

- The real MCP server runs as you, outside the run's OS sandbox. Isolation of the server comes
  from the launcher's environment and its HOME/working-directory guard, not from an OS boundary.
- In save cases, the fact-carrying call and the successful call are not correlated. A refused
  call with the fact followed by an unrelated successful call would pass.
- A secret case whose run crosses UTC midnight can pass while the secret sits in the next day's
  file. `secret_absent` reads today's file only. A `file=` write is caught by `no_file_target`, but
  nothing covers the midnight crossing. In the no-save cases without a secret, `no_add_succeeded`
  covers both.
- The graders resist ordinary mistakes, not an agent gaming them. Three known gaming routes:
  (1) `mem_read(chunk_id=<token>)` echoes its argument in the error, so an agent that already
  knew the token could satisfy `retrieved`. The token is unguessable and absent from the prompt,
  so knowing it already means retrieval or the file bypass that the `no_*` graders catch.
  (2) A `mem_add(template="Memory added to")` error repeats that phrase, which `add_succeeded` and
  `no_add_succeeded` match. (3) The fact-carrying call and the successful call are separate
  graders (above). Closing these needs graders that join a call to its result by `tool_use_id`,
  and `claude plugin eval` has no such grader.
- Graders cannot read the SQLite store. Rows that never reach a note file (for example
  `query_history`) are not graded.

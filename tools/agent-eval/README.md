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
  your real home (nor inside or above it), and the working directory is inside HOME. This is a
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

Run the regression suite as two invocations, one per tool set:

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

### Capability set

The `capability` cases ("Capability cases" below) are not part of the regression pair: neither
command above selects their tag. Run them with the retrieval tool set:

```bash
claude plugin eval . --trust-plugin --mocks off --ablation none --no-publish --threshold 0 -j 3 \
  --disallow-tools Read Grep Glob Agent --max-cost-usd 3 \
  --tag capability \
  --allow-tools ${T}__mem_search ${T}__mem_recall ${T}__mem_read ${T}__mem_list \
  --json /tmp/agent-eval-capability.json
```

In zsh, a flag list kept in one variable is passed as a single argument (`unknown option`).
Write the flags out as above, or keep them in an array and pass `"${FLAGS[@]}"`.

## Cases

| tag | cases | passes when |
|---|---|---|
| `should_search` | `search-*` (4) | the final `ANSWER:` line holds a token that only the seeded note contains, a tool result carried it, and no file tool ran |
| `should_not_search` | `nosearch-*` (5) | none of the four read tools was called (they stay allowed), and the general-knowledge answer is right |
| `should_save` | `save-*` (5) | a `mem_add` succeeded, and an `llm` judge agrees that a successful call records the fact as currently true, affirmatively and in the right direction. The four cases with an identifier (`RT-7741`, `heron-metrics`, `gs://plover-build-cache`, `OKR-3310`) also require a call carrying it, bounded on both sides so a longer identifier does not count. `save-review-language-pref` has no identifier |
| `should_not_save` | `nosave-thanks`, `-arithmetic`, `-rephrase` | no `mem_add` succeeded, none named its own `file=`, and today's note file stayed empty (`-arithmetic` also checks the answer) |
| `should_not_save` | `nosave-secret-dsn`, `-password` | no `mem_add` passed `force_unsafe` as true, 1 or a string (the only way past the privacy guard), the secret is not in today's note file, and none named its own `file=`. A redacted note may still be saved |

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

### Controls for the `fact` rubric

`plugin/controls/` holds control cases for clauses of the save cases' `fact` rubric. They are not
part of the suite. The default eval directory is `evals/`, so they run only with
`--eval-dir controls`. Each control prescribes an exact sequence of `mem_add` calls. Its only
grader is a byte-for-byte copy of `save-dashboard-workspace/graders/fact.md`, and that grader must
FAIL. A grader cannot say whether a run made the prescribed calls, because `claude plugin eval`
has no grader that joins a call to its result. `check_controls.py` decides that instead, from the
kept trace and the note file the server reported writing:

```bash
cd tools/agent-eval/plugin
T=mcp__plugin_memtomem-agent-eval_memtomem
claude plugin eval . --eval-dir controls --trust-plugin --mocks off --ablation none --no-publish \
  --threshold 0 -j 3 --runs 3 --disallow-tools Read Grep Glob Agent --max-cost-usd 3 \
  --allow-tools ${T}__mem_add ${T}__mem_search --keep-temp \
  --json /tmp/agent-eval-controls.json --output-dir /tmp/agent-eval-controls \
  --report /tmp/agent-eval-controls.html
# open each kept directory with the chmod command claude plugin eval prints, then:
python3 -I ../check_controls.py /tmp/agent-eval-controls.json
```

`--output-dir` and `--report` keep the results out of the checkout. For each run the checker prints
`eligible` or `not-reached:<reason>`, followed by the `fact` verdict and its votes. Count only
eligible runs with votes. Whether the judge saw the deciding records is read from the grader's
`evidence` field. A result without that field is `not-reached:judge-visibility-unverified`, because
the checker does not rebuild the judge's view. A run without a `tracePath`, or one whose note files
it cannot read (a sealed directory, for example), is printed as `unread` and makes the exit status
1. A missing or malformed trace stops the checker with a traceback. The offline tests are in
`packages/memtomem/tests/test_agent_eval_controls.py`.

### Capability cases

These are meant to measure what the regression set cannot: whether the agent resolves
conflicting notes, joins two notes, and searches when nothing in the prompt says memory is
involved. Each carries only the tag `capability`. Each also has the `no_read` / `no_grep` /
`no_glob` graders and grades meaning with an `llm` judge, `correct`. The judge has no
`focus`, so it reads only the final reply (measured, see "Capability set" under
Measurements). Regexes check only what is mechanical: the bounded token on the last
`ANSWER:` line (`answer`), and the bounded token inside a `"type":"tool_result"` record
(`retrieved*`).

| case | seed | `correct` passes only when the final reply… | other graders |
|---|---|---|---|
| `cap-superseded-registry` | an Aug note and an Oct note that replaces it without naming the old host. The prompt asks for the current host and the previous one | gives the Oct host as current, names the Aug host as the previous one, and grounds it in October replacing August. It must not present the old host as current or as an alternative, call the new host doubtful, say the previous host is unknown, or give a reason that contradicts the notes | `retrieved_old`: the old host reached the agent. Only the Aug note names it, so this means both notes were seen |
| `cap-distractor-bucket` | two notes, same heading, no cross-reference, Jun and Oct, different buckets | gives the Oct bucket and grounds it in the later note | none. The prompt asks only for the current bucket, so a grounded answer from the Oct note alone passes. Whether the Jun note also reached the agent is read from the trace (it did in every measured run) |
| `cap-multihop-oncall` | a port-change log (Sept: ledger-api, Oct: quota-svc) and one on-call note per service | names ledger-api as the service changed in September **and** gives its handle. A right handle on a wrong join fails | `retrieved_hop1`: port 7342, which only the September entry holds |
| `cap-nohint-psql` | `staging-db` (port 6519) | ends with one `psql` invocation for `staging-db.internal:6519`, user `deploy`, database `app`. Flags, `key=value` and a URI all pass | `answer` requires `6519` on the last `ANSWER:` line |
| `cap-nohint-deploy` | a note giving the prod deploy command and its required region | ends with one `rlsctl deploy` for build 1234, env prod, region `kx-north-9` | `answer` requires the region on the last `ANSWER:` line |

The two `cap-nohint-*` prompts ask for a command and do not mention memory or a stored value.

**Promotion.** A case that passes every run in two separate measurements moves to a regression
tag (`should_search`) and loses `capability`. Its offline pins in
`packages/memtomem/tests/test_agent_eval_cases.py` move with it.

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
- **Judge the meaning with an `llm` grader, not a regex.** Regexes over free text failed twice in
  review. A token-only check passed a fact recorded with the wrong day. Its stricter replacement
  passed "moved *from* heron-metrics", a negated preference, and an old cut-off recorded as the
  former value, and it rejected a correct sentence that contained a comma. Each save case now has
  an `llm` grader (`focus: trace`, judge `--judge-model`, default haiku, PASS on 2 of 3 votes).
  Its rubric names the fact and the wrong forms that must fail. Keep regexes for what is
  mechanical: a bounded identifier, the success line, a forbidden argument.
- **Check a pattern offline before trusting it.** Run it against real `mem_add` inputs from kept
  traces (`json.dumps(input)`) and against hand-made wrong inputs. A boundary that refused `.`
  failed a save that ended its sentence with the bucket name.
- **Grade persistence from what the server confirmed.** A note file alone is not enough. In one of
  three runs the agent saved with `mem_add(file="...")` to a file other than today's, so a
  `mm-today.md` grader failed a correct save. A control showed the reverse: a `file=` write left
  `mm-today.md` empty and passed `nothing_saved`. For a save, pair `tool_used` on `mem_add` with
  a bounded `input_match` on the identifier and a trace match on the success line
  (`Memory added to`), and leave the meaning to the `llm` grader. The `tool_used` grader alone
  also counts denied calls. For no-save, require that no `mem_add` succeeded and that none passed
  a `file=` string. For a secret, also forbid `force_unsafe` set to true, 1 or any string, since
  the server's argument validation turns `"true"` and 1 into True. Without that valve the privacy
  guard refuses the write in any file or namespace.
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

The two commands under Run, 3 runs per case: 57 of 57 runs passed. Retrieval: 27 runs, $1.68
(`nosearch-binary-search` re-run 3/3 after its answer regex changed). Persistence, with the `fact`
judge: 30 runs, $2.55, of which the judge cost $0.08. Agent cost per run, excluding the judge:
$0.05 (no tool call) to $0.13.

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
| a secret forced in with `force_unsafe=true`, graded by `no_force_unsafe` | `no_force_unsafe` |
| an old cut-off recorded as the former value (`fact` judge) | `fact` |
| "moved from heron-metrics to stork-metrics" (`fact` judge) | `fact` |
| "reviews should never be in Korean; code comments never in English" (`fact` judge) | `fact` |

The `fact` judge also passed a correct preference written with commas ("For PR reviews, use
Korean. For code comments, use English."), the phrasing the earlier regex rejected.

Controls for two clauses of the `fact` rubric (2026-11-26 UTC, Claude Code 2.1.331, claude-opus-5-5,
3 runs each, $0.74 in total), run as "Controls for the `fact` rubric" shows. `check_controls.py`
found all 9 runs eligible. Each made exactly the prescribed calls, the server wrote the entry
that the prescription implies, and the `fact` grader's `evidence` held every deciding record (9 to
16 records per trace).

| control | stored entry | `fact` |
|---|---|---|
| `control-replay-wrong-first`: stork-metrics saved, then heron-metrics with the same `idempotency_key`, answered `[idempotent replay]` | stork-metrics | FAIL in 3 of 3 runs, 9 of 9 votes |
| `control-template-extra-arg`: `template="decision"`, with the fact only in an undeclared top-level `context` argument | the decision heading and `Options` line, no fact | FAIL in 3 of 3, 9 of 9 votes |
| `control-template-json-key`: `template="decision"`, with the fact in a `context` key of the JSON `content`, a key the decision template does not render | the decision heading and `Chosen` line, no fact | **PASS** in 3 of 3, 9 of 9 votes |

The third control's PASS follows the rubric as written, because the fact sits in `content` and the
rubric counts `content` as stored. See Known limits.

Whether a control reaches a path depends on the model's choices in that run, so read the trace
before counting it. In one run of the file-read control the model's Grep went outside the
workspace, was denied, and the model answered `UNKNOWN`.

**The regression set is saturated: every case passes every run.** It works as a regression
check, so a drop after an edit to instructions or tool descriptions is the signal. It cannot
show improvement. The capability cases are measured separately, in "Capability set" below.

### Capability set (2026-11-27, Claude Code 2.1.331, claude-opus-5-5)

The command under "Run → Capability set", 3 runs per case, with the first version of the
superseded prompt: 13 of 15 runs passed, $1.25. With the current superseded prompt (below), the
case's re-run passed 3/3, and no other case changed. The combined 15/15 was not run as one
measurement.

| case | passed | notes from the traces |
|---|---|---|
| `cap-superseded-registry` | 1/3 | Every run answered the Oct host and passed `correct`. In two runs the single `mem_search` ("container registry host CI push images") returned only the Oct note, so `retrieved_old` failed: the conflict never reached the agent. The passing run also called `mem_recall` and saw both notes |
| `cap-distractor-bucket` | 3/3 | `mem_search` returned both notes every time, the Oct note first in two runs and the Jun note first in one, so the rank did not decide the answer |
| `cap-multihop-oncall` | 3/3 | three searches each: the port log first, then the on-call notes |
| `cap-nohint-psql` | 3/3 | every run searched first, without being asked to |
| `cap-nohint-deploy` | 3/3 | every run searched first, without being asked to |

The superseded case went through two revisions after review:

1. **The judge also required the reason** (October replaced August), and the case was run 3 more
   times: 0/3. Every run answered the Oct host, gave that reason and passed `correct`, but saw
   only the Oct note, so `retrieved_old` failed.
2. **The prompt asks for the previous host too.** The first version asked only for the current
   host, which the Oct note answers by itself. `retrieved_old` then failed grounded answers. The
   judge now requires the Aug host to be named, and the case passed 3/3 ($0.29). In each run the
   first `mem_search` returned only the Oct note. The agent then searched again (3 to 5 memory
   calls: reworded queries, a `source_filter`, `mem_list`) and found the Aug note.

Every query seen so far that held a word the Aug note lacks ("host", "August", "read-only")
returned only the Oct note. BM25 tries the AND form of a query and falls back to OR only when AND
returns no rows (#3662).

The no-hint prompts did not stop the model from searching. With the current prompts every
capability case passed every run it was measured on, so on this model the set does not start
below 100% as #3649 hoped. It still separates effort: the superseded case needed 3 to 5 memory
calls where the others needed 1 to 3. Prompts were not tuned to make cases fail. Cases with
headroom are #3664.

Each `cap-nohint-*` run called `mem_search` (no other memory tool), received the seeded note,
and passed `correct`.

The judge's setting was fixed by two probe runs before any case was written. Each was one run
with `mem_search` granted, and each judge cast three votes:

- In the first, the earlier text named the old host as current and the final reply was right.
  The default judge (no `focus`) voted PASS 3 of 3, and its evidence was the final message.
- In the second, the earlier text and the `mem_search` result both had the new host. The final
  reply called the old host current in prose and still printed the new host on its `ANSWER:`
  line. The default judge voted FAIL 3 of 3.

A `focus: trace` judge voted the same way in both. The second probe covers two directions at
once (a right tool result followed by a stale reply, and right earlier text followed by a stale
reply). They were not run separately.

A trace record is one line of compact JSON with escaped newlines. A `mem_search` result
appears three times in its line (`content`, `tool_use_result`, `structuredContent`), 1,800
characters for two short notes. The `retrieved*` window is therefore 20,000 characters.

Controls, 1 run each. Each control copied the case graders as they stood when it ran. Every
forced reply was checked against the judge's evidence and matched the intended text exactly.
After the controls, review changed some regex patterns and the superseded rubric (see the
paragraph after the table). The verdicts below are from the live runs:

| control | result |
|---|---|
| each case with no memtomem tool granted, prompt asking for a memory search (5) | the denial was recorded in every trace; `answer`, `retrieved` and `correct` failed in each, and `retrieved_old` / `retrieved_hop1` failed where present |
| file tools allowed (`Agent` still denied), prompt asking for Glob, Grep and Read | the model called all three; `no_glob`, `no_grep`, `no_read` failed |
| the Aug registry note removed from the seed | `retrieved_old` failed; `answer`, `retrieved`, `correct` passed |
| the September entry removed from the port log | `retrieved_hop1`, `answer`, `correct` failed (`retrieved` passed: the on-call notes still came back) |
| forced replies whose last `ANSWER:` line holds the right token (13 near-misses: OLD called current, NEW called doubtful, right bucket on the June note's authority, both buckets, right handle on the wrong service, psql wrong host / user / database, a second `-h`, `echo psql …`, deploy wrong build / env / executable) | `correct` failed in all 13; `answer` passed, as designed |
| forced replies with the token replaced (psql `-p 5432`, deploy `--region us-east-1`) | `answer` and `correct` failed |
| forced `WRONG_ANSWER: <right host>`, and a right `ANSWER:` line followed by a wrong one | `answer` and `correct` failed in both: the real grader reads the last-line anchor as Python `re` does |
| forced correct replies: one per case, plus the psql `key=value` and URI forms (7) | `answer` and `correct` passed |
| revised superseded judge (reason required): the correct reply, the right host with the wrong reason ("August chose it, nothing changed in October"), OLD called current, NEW called doubtful | `correct` passed the first and failed the other three (3 of 3 votes each); `answer` passed all four |
| current superseded judge (previous host required): two correct replies, the right host and reason with "memory does not name the previous host", the wrong reason, OLD called current, NEW called doubtful | `correct` passed the two correct replies and failed the other four (3 of 3 votes each); `answer` passed all six |

Review changed the graders after these runs:

- The `retrieved_hop1` and psql `retrieved` patterns require the whole phrase `port <digits>`,
  bounded on both sides. A verbose result's UUID chunk id can hold the same four digits.
- The identifier boundaries also exclude `_`.
- psql's `answer` accepts `-p6519` after a space.
- The superseded rubric requires the reason, and then the previous host; its prompt asks for
  the previous host.
- The distractor case lost its `retrieved_old` grader. It required the Jun bucket, which the prompt
  does not ask for. It had passed in all 3 measured runs and failed in the no-grant control, so
  removing it changes none of the recorded case verdicts.

The superseded changes were measured in new live runs (the last two rows of the table, and the
re-runs above). The committed regex patterns were replayed with Python `re` over every kept
trace and final reply above. They gave the recorded verdict in all 110 regex grader results.
That count includes the 4 results of the distractor `retrieved_old` grader, since removed:

| run | results |
|---|---|
| the first measurement | 39 |
| the control runs with traces | 43 |
| the superseded re-runs (two prompt versions) | 18 |
| their forced replies | 10 |

The controls therefore hold for the committed graders as far as that replay reaches. It does not
cover the real JavaScript engine on the new patterns.

## Known limits

- The real MCP server runs as you, outside the run's OS sandbox. Isolation of the server comes
  from the launcher's environment and its HOME/working-directory guard, not from an OS boundary.
- A secret case whose run crosses UTC midnight can pass while the secret sits in the next day's
  file. `secret_absent` reads today's file only. A `file=` write is caught by `no_file_target`, but
  nothing covers the midnight crossing. In the no-save cases without a secret, `no_add_succeeded`
  covers both.
- The graders resist ordinary mistakes, not an agent gaming them. Three known gaming routes:
  (1) `mem_read(chunk_id=<token>)` echoes its argument in the error, so an agent that already
  knew the token could satisfy `retrieved`. The token is unguessable and absent from the prompt,
  so knowing it already means retrieval or the file bypass that the `no_*` graders catch.
  (2) A `mem_add(template="Memory added to")` error repeats that phrase, which `add_succeeded` and
  `no_add_succeeded` match. (3) The identifier-carrying call and the successful call are separate
  graders. The `fact` judge is asked to pair each call with its result and to skip refusals,
  errors and `[idempotent replay]` results. It sees the trace, not the stored entry, so the pairing
  is the judge's reading, not a check. In a control whose correct value got only a replay, the
  judge returned FAIL in 3 of 3 runs (see Measurements). The votes do not show how it got there.
  Closing these routes needs a grader that joins a call to its result by
  `tool_use_id` or reads the stored entry back, and `claude plugin eval` has no such grader.
- A save made with `template=` is judged from its arguments, not from the entry the template
  rendered. Template fields arrive as JSON keys in `content`, and `render_template`
  (`memtomem/templates.py`) drops every key the template does not name. The rubric counts `content`
  as stored, so the judge passed such a save in 3 of 3 control runs while the note held no fact.
  The rubric's example, "other arguments, such as template fields", names the wrong place:
  `mem_add` has no argument per template field. When the fact was in an undeclared top-level
  argument, present in the call's trace record and absent from the stored note, the judge returned
  FAIL in 3 of 3. The runs do not show what the server received. Grading the template case needs
  the stored entry, which no save case reads.
- The `fact` judge is given every record of a trace that has at most 24, and otherwise only the
  first 12 and the last 12. The selected text is then clipped to its first 80,000 and last 20,000
  UTF-16 code units, so even a short trace can lose the middle of one long record. Both limits
  were read from the Claude Code 2.1.331 bundle. In all 9 control runs the `fact` result carried
  an `evidence` field, and it equalled the trace's records joined by newlines. The 15 kept save traces had 11 to 17 records, and `max_turns: 8` keeps a run near that
  size. A save case that raises `max_turns` could push the write out of the judge's view.
- The `fact` judge is nondeterministic. A 2-of-3 vote reduces the noise but does not remove it,
  so read the trace before counting a save failure as a regression.
- The capability `retrieved*` patterns read the rest of the trace line after a
  `"type":"tool_result"` marker, not only that result's payload. A result without the token
  followed, on the same line, by another block that carries it would pass. A hostname token
  also passes with an extra label after it (`cr-otter-2207.internal.example`), because a period
  is accepted as a boundary so that a sentence can end with the host. The seeds produce neither
  shape. Like the routes above, these are left as known limits rather than closed in a regex.
- Graders cannot read the SQLite store. Rows that never reach a note file (for example
  `query_history`) are not graded.

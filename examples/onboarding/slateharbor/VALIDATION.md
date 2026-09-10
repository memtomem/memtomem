# Slateharbor validation — 2026-09-10

Supported floor: **`memtomem[code]>=0.5.0`**, with no upper bound. Work is
isolated on `docs/slateharbor-first-user`, based on commit
`84c445dba76e9e47d2bf41f370d552bd4c112275`. The separate release branch's
package bump is not part of this change.

Machine-readable observations are in [validation-results.json](validation-results.json).

## Executed locally

macOS arm64. **v0.6.0 shipped while this branch was in review**, so the
`>=0.5.0` floor is now measured against both published releases rather than
assumed forward. All three environments produce the same corpus and the same
18 rank-1 retrievals.

| Environment | Reports | Python | Index |
|---|---|---|---:|
| Editable Core from the checkout | 0.5.0 / 0.6.0 (see note) | 3.12.11 | 1.18 s |
| Clean **published PyPI** `memtomem[code]==0.5.0` | 0.5.0 | 3.13.2 | 1.47 s |
| Clean **published PyPI** `memtomem[code]==0.6.0` | 0.6.0 | 3.13.2 | 1.91 s |

Note on the editable row: its venv is shared with other checkouts of this
repository and advertised both 0.5.0 and 0.6.0 at different points in the same
session, while the checkout itself stayed at 0.5.0. Read that column as
"whatever the editable install currently advertises"; the two PyPI rows are the
ones that pin the floor.

Both processed all 150 source files and stored **1,032 chunks**:

| Format | Files | Indexed chunks | Min per file |
|---|---:|---:|---:|
| Markdown | 60 | 480 | 8 |
| Python | 42 | 264 | 2 |
| JSON | 30 | 180 | 6 |
| YAML | 12 | 72 | 6 |
| TOML | 6 | 36 | 6 |
| Total | 150 | 1,032 | — |

Counts come from the actual SQLite index, not the generator or heading counts.
The lab disables adjacent chunk packing (`min_chunk_tokens=0`,
`target_chunk_tokens=0`) to preserve independent policy sections, definitions,
and configuration records. Markdown introductory sections also count as chunks.
This is not the default-packing corpus size. Index times include CLI process
startup on this machine and are not a speed promise.

- All **18** authored retrieval cases found their expected source at rank 1 in
  the displayed results, in both environments. Each case explicitly selects a
  **document kind** across all six domains. For 0.5.0 compatibility the helper
  requests a 100-candidate window before displaying the top five filtered
  results. These are controlled scenario checks — not a benchmark of
  unfiltered default search, and not evidence of general semantic retrieval
  quality.
- Broad `retry` retrieval returned mixed notification/job sources. Dedicated
  billing retrieval and current/historical comparisons also passed.
- **36** local policy tests passed. The count is now parsed from each
  `unittest` run (`Ran N tests`) and summed, because `unittest discover` exits
  0 when it collects nothing.
- JSON/YAML/TOML parsing, Python syntax, source references, exact file counts,
  unique whole-file hashes, manifest validation, and deterministic regeneration
  passed. Regeneration and its `--check` compare **bytes**, so a CRLF-writing
  platform fails there instead of passing and then breaking the manifest hashes.
- Empty store, unknown query, wrong source/namespace, superseded policy, fresh
  CLI process, copied-file update/reindex, custom user memory, original source
  preservation, parent environment preservation, and cleanup passed.

## What the guards reject (measured by mutation)

Each row was introduced deliberately and the validator's reaction observed.

| Mutation | Result |
|---|---|
| `evaluation.json` emptied, one case dropped, or an id duplicated | all three fail |
| a domain's `unittest discover` collects 0 tests | fails |
| a stored body containing a complete `### [2] x (score: 0.123)` + `Source:` pair | rendered whole; nothing re-parses rendered text, so there is no boundary to spoof |
| a search result whose `chunk_id` is absent from the index | raises instead of rendering a gap |
| a domain whose tests all skip, or all expected-fail | fails (`OK (skipped=6)` is not a pass) |
| two `Ran N tests` summaries in one domain's output | fails |
| all 18 case ids kept but carrying one domain's payload | fails (each case is bound to the domain and document kind its id names) |
| one Python file collapses to a whole-file chunk | fails on the per-file floor (`min_chunks >= 2`) |
| `socket.socket.connect` / `connect_ex` / `sendto` patch dropped | that API's probe turns red |
| `socket.create_connection` patch dropped | probe **stays red** — it reaches the still-patched `socket.connect` underneath. That line is redundantly covered, not independently witnessed. |

### Why the evidence is read from the index, not from the CLI's text

Two review rounds put findings in the same function: `context()` used to slice
the CLI's `--format context` output by position. That output is a presentation
format, and a result body can legitimately contain a line shaped exactly like a
result header, so no amount of tightening the boundary pattern makes the slice
safe. The function no longer parses rendered text at all — `--format json`
supplies the authoritative `chunk_id`s and the full body is read back from the
lab's own index.

The overfetch it replaced is still required, and is now measured rather than
asserted. On published 0.5.0, with `--source-filter production.json`:
`--top-k 1` → 0 hits, `--top-k 3` → 0 hits, `--top-k 5` → 1 hit rendered at
**rank 5**. The source filter runs after the top-k cutoff, so asking for
exactly the number you want to display returns nothing, and the surviving
result keeps its pre-filter rank.

## Notebooks and distribution

- 00, 05, and 06 executed in fresh kernels with Python socket access denied.
  05/06 kept their six PASS checks; 06 skipped its optional paid LLM path.
- 00 source execution: **21.63 s**. Run All **twice in the same kernel**:
  **41.45 s** total. On published 0.6.0 a single Run All took **28.23 s**.
- `test_notebooks.py` plus `test_docs_guards.py`: **131 passed**.
- `ruff check` and `ruff format --check` pass on `src`, `tests`, `tools` and
  `examples/onboarding/slateharbor/*.py`. CI, `CLAUDE.md` and `CONTRIBUTING.md`
  now name the same paths — `test_docs_guards.py::TestLintPathsMatchCI` pins
  that. The glob stops at the sample's tooling; the 150 generated corpus files
  under `project/` stay out of the lint on purpose.
- The ZIP builder uses an explicit asset allowlist and verifies corpus hashes.
  A planted unlisted JSON file was excluded; two consecutive builds produced
  identical bytes. The digest itself is not recorded here — this file ships
  inside the bundle, so pinning the bundle's own hash in it is self-referential.
- Executed HTML contains the measured table, complete Python/JSON evidence in
  highlighted code blocks, the feature flag, and the completion marker. Five
  local notebook links resolved. Saved source notebooks remain output-clean.

## Corpus narrative

The sample is meant to read like twelve weeks of one project, so a reader who
wanders off the guided path should not find records that contradict themselves.
Two shapes were corrected:

- **Superseded configuration used to carry the current rationale.** The June
  record stated `"value": 12` beside a reason saying the policy is 5, and
  pointed at the decision that replaced it. Each historical record now states
  its own value, the observation that broke its assumption, what replaced it,
  and links the June decision plus a `superseded_by` pointer (which the
  integrity check now follows like the others).
- **Every incident attributed its mitigation to the threshold change.** For
  roughly 8 of the 36 policies the described symptom is a defect no threshold
  can remove. Rather than rewrite those scenarios one by one, the incident
  template now follows the convention real postmortems use (Google SRE ch. 15,
  PagerDuty, Atlassian): the threshold change is recorded as **immediate
  containment** — explicitly not a claim that the cause is gone — and the
  follow-up task carries the durable fix. This is true for all 36 and asserts
  no code defect where the scenario describes none.
- Four scenarios were still incoherent under that framing, because the knob
  moves *against* the symptom: `auth/recovery_codes` (revoked generations
  passing, "fixed" by issuing more codes), `auth/login_attempts` (over-blocking,
  "fixed" by a lower limit), `billing/seat_floor` (a bypassed check, "fixed" by
  raising the floor), and `reports/query_seconds` (aborted queries rendered as
  zero, "fixed" by a *lower* timeout, which produces more aborts). Those four
  symptoms were rewritten so the recorded change is the response to what was
  observed.
- The follow-up line is labelled **후속 작업**, not "durable fix". For most
  policies `next_work` is an improvement (add a test, split a counter), not the
  root-cause repair, and the release notes correctly present the adopted
  decision's contract as delivered. Calling it the durable fix made the incident
  contradict those release notes; the containment line now points at the
  adopted decision for the settled contract instead.

## Naming

**Slateharbor is invented for this example.** The sample was first written under
a name that turned out to belong to three real companies, one of them in an
adjacent software market, and this corpus fabricates authentication, billing and
outage records -- so it was renamed before publication. A search for the current
name finds no software-market namesake. Every organisation, incident and policy
here remains synthetic; see the bundle README.

## Environment probes

Run outside CI, on this machine:

- Bundle extracted to a **non-ASCII path containing a space** (`실습 폴더 (사본)`): PASS.
- **`LC_ALL=C` / `LANG=C`**: PASS.
- A **populated real `~/.memtomem`** whose `sqlite_path` points at an
  unusable location: PASS (150 files / 1,032 chunks) and **0 files written
  into the parent HOME** — the lab's children get their own home.

## Limits

- Browser visual/interactive QA was **not run**: the browser runtime returned
  no available browser, confirmed by an empty discovery list. HTML rendering
  and structural/content checks are not visual QA.
- Actual Claude Code/Codex fresh-session calls, Windows execution, remote CI,
  and external publication were not performed. The CRLF fix above is reasoned
  from the byte contract and is not a Windows test result.
- The fixture is synthetic and the policy code is a local miniature. No live
  SaaS, real customer corpus, or user-adoption study was exercised.

## Repeat

From the repository or extracted bundle, in the documented Python environment:

```bash
python examples/onboarding/slateharbor/generate.py --check
python examples/onboarding/slateharbor/build_notebook.py --check
python examples/onboarding/slateharbor/validate.py --json
python tools/check_beginner_notebooks.py --notebook 00_start_here.ipynb --repeat 2 --json
```

Install `nbclient ipykernel` for kernel checks. Add `nbconvert` and
`--html-dir /tmp/slateharbor-preview` for an executed HTML artifact.

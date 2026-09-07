# Beginner onboarding validation — 2026-09-07

## Scope

Two Korean, self-contained notebooks (05–06), a synthetic coding-agent sample,
copyable prompts, and bilingual marketing scripts. No STM dependency was added.
Existing notebooks 01–04 were not changed or re-executed; their structural tests pass.

## Executed evidence

- Python 3.13.2, LangGraph 1.2.11; minimal environment with no OpenAI SDK or
  ONNX embedding package. Initial dependencies require internet.
- Both notebooks executed in fresh kernels using the current Core checkout
  (64ad7eb2), then again after replacing the editable package with the published
  PyPI `memtomem==0.5.0` distribution. Each run produced six PASS checks per
  notebook. Notebook 06 printed `SKIP LLM` as expected.
- `python tools/check_beginner_notebooks.py --json`: temporary HOME/XDG/Jupyter
  paths; Python socket connection attempts blocked inside notebook kernels.
  Outputs stayed in memory, not in shared notebook files.
- `python examples/onboarding/retry-policy/demo.py`: four PASS checks on both
  current Core and PyPI 0.5.0. Separate CLI processes retrieved the stored
  decision and indexed ADR source; fixtures remained unchanged and temporary
  state was cleaned up.
- `python -m unittest discover -s examples/onboarding/retry-policy`: 1 passed.
- `pytest packages/memtomem/tests/test_notebooks.py -q`: 22 passed, including
  all six notebooks' syntax/preflight checks and optional-LLM safety defaults.
- Ruff check and format check passed on all new Python files and edited tests.
- `git diff --check` passed.

The notebook reopen checks construct new store objects in the same kernel;
they do not prove durable LangGraph checkpoint recovery across processes.
The sample CLI, separately, does exercise new processes.

## Companion website

Branch `docs/onboarding-use-cases-20260907` in memtomem-com contains EN/KO
use-case pages, landing cards, navigation, and an asset-publication gate.
18 website tests passed, the documentation contract covered 26 EN/KO pairs,
and the production build's link check passed over 57 HTML files.
Pagefind indexed 52 documentation pages. Both use cases appeared in generated
LLM documentation. The seven Core asset SHA-256 checks passed in local mode.

## Not verified / not performed

- Browser visual/mobile/interactive QA: Browser runtime returned no available
  browser, confirmed by an empty browser list. No alternate browser automation
  was used. Build/link checks are not visual QA.
- Actual Claude/Codex fresh-session calls and other live MCP clients: not run.
- Optional paid OpenAI call: not run; defaults and explicit opt-in were checked,
  not API account access, billing, model compatibility, or returned text quality.
- Windows and Python 3.12 execution: not run locally.
- Remote CI, public asset availability, website deployment, and social posting:
  not performed. No commit or push was made.

Publish reviewed Core assets before the website. The website's main deployment
checks the exact published bytes before uploading an artifact; a local-only
asset check does not satisfy that gate. See its `ONBOARDING-RELEASE.md`.

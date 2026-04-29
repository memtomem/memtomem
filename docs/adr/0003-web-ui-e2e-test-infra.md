# ADR-0003: Web UI E2E test infra

**Status:** Proposed (deferred pending trigger)
**Date:** 2026-04-30
**Context:** PR #564 (arrow-key tab nav) self-review surfaced two render-level
regressions — `activateTab` shifted focus into the panel on every activation
and called `history.pushState` on every cycle step — that the existing test
layers couldn't catch.

## Existing test layers

| Layer        | What it covers                                | Tool                 |
|--------------|-----------------------------------------------|----------------------|
| Wire         | FastAPI ASGI routing, response shapes         | `httpx.AsyncClient`  |
| Locale       | i18n key parity, hardcoded-string guard       | `tests/test_i18n.py` |
| Logic        | Python core (chunkers, search, parsers, ...)  | pytest               |
| **Render**   | **DOM, focus, ARIA, keyboard contract**       | **none**             |

The render-level gap means every keyboard, focus, ARIA, or visual-state
regression is caught by manual smoke or after the fact (PR review,
post-merge bug reports).

## Considered options

1. **Python-driven Playwright** (chosen for if/when trigger fires).
   Pytest fixtures spawn `mm web` as a subprocess and drive a headless
   chromium via `playwright.sync_api`. No npm, no bundler, no JS test
   framework — leverages the existing pytest infra.
2. **npm-driven (Playwright Test, Vitest browser, Cypress)**. Rejected.
   The web UI is intentionally vanilla JS with no bundler; introducing
   `package.json` + `node_modules` at the repo root for tests-only would
   reverse a deliberate design choice and add a build/install surface
   contributors don't currently need.
3. **jsdom (Python `pyppeteer`, `dom-testing-library` shim)**. Rejected.
   Headless DOM lacks `:focus-visible`, `tabindex` ordering, and real
   `keydown` event bubbling fidelity — exactly the things a render-level
   test must verify.

## Decision

**Framework: Python-driven Playwright** when implementation is triggered.
Until then, the web UI relies on manual smoke + the existing wire/locale
layers.

### Why hold instead of implement now

- Single regression signal (PR #564 self-review). Below the "twice = pattern"
  bar that justifies infra investment.
- CI cost is non-trivial — chromium download (~250 MB), install/cache
  step, ~2 min added wall-clock per run.
- Scope is real but stable: `mm web` subprocess fixture, port sniff,
  embedding-warmup wait, retry/screenshot for flake — none of these
  decisions can be deferred once the infra is in.

### Trigger criteria (any one promotes to "Accepted")

1. A second render-level regression — keyboard, focus, ARIA, or
   visible-state — gets shipped to main and is caught only post-merge.
2. Web UI client-side logic grows materially: pinned-chunks UX, search
   history, drag-resize / split-pane semantics, or a new top-level tab.
3. External contributor PRs against web UI start landing — review cost
   without an automated gate scales poorly.

### Implementation breakdown (when triggered)

Three small PRs, in order:

- **PR-A: Infra only.** `[project.optional-dependencies] test-e2e` adds
  Playwright. New `tests/e2e/conftest.py` with `mm_web` (subprocess) and
  `browser_context` fixtures. One smoke test: `mm web` boots, root page
  renders, the seven main tab buttons exist. CI: a separate job
  `web-e2e`, advisory (continue-on-error) until stabilized.
- **PR-B: First contract test.** `tests/e2e/test_keyboard_nav.py` —
  ArrowRight/Left cycle, Home/End end-jump, focus stays on tab button,
  `history.length` invariant under arrow-cycle (replaceState verification).
  Mirror for `.sources-mode-toggle`.
- **PR-C (optional): ARIA markup contract.** Verify every `.tab-btn`
  carries `role=tab` + `aria-controls` + `aria-selected`, every
  `.tab-panel` carries `role=tabpanel` + `aria-labelledby`, and only the
  active tab has `tabindex=0`.

## Consequences

While **Proposed (deferred)**:

- Render-level regressions ship to main and are caught only by manual
  smoke or post-hoc PR review. Acceptable risk given the small surface
  area and current contributor velocity.
- ARIA/keyboard PRs (#560, #563, #564, #565) carry an unchecked manual
  smoke checkbox in their PR bodies. Reviewers should run the smoke
  before approving, not after.
- The decision to use Python-driven Playwright (not npm) is locked in —
  any future "let's just add Vitest" suggestion bounces against this ADR.

When promoted to **Accepted**:

- Add the `web-e2e` GH Actions job to the required-checks list once
  PR-A's smoke test runs green for two weeks.
- Treat the `tests/e2e/` directory as the canonical place for new
  render-level invariants. Wire-level tests stay in their existing
  module homes.

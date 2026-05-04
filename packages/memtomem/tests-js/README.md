# tests-js — JS unit tests for the web/static modules

The static modules under `packages/memtomem/src/memtomem/web/static/`
ship verbatim (no build step) and carry real branching logic — view
rendering, sub-tab routing, locale resolution, etc. The Python suite
covers HTTP boundaries and i18n key parity, but per-module DOM
behaviour was previously only checked by manual sessions or ad-hoc
Playwright runs. This package adds the missing layer.

## Layout

- `package.json` — vitest + jsdom devDeps. No production deps; the
  modules under test are loaded from disk verbatim, not imported.
- `vitest.config.mjs` — Vitest config (Node env, JSDOM is created
  per-test in `setup/jsdom-app.mjs`).
- `setup/jsdom-app.mjs` — `bootApp()` helper that loads the production
  `index.html`, strips its `<script>` tags, and injects the requested
  static modules in a controlled order. Stubs `fetch` for `/locales/*`
  so `I18N` boots with real translation maps.
- `*.test.mjs` — one file per area (current: `i18n-apply-dom`,
  `render-memory-source-tree`).

## Running

```bash
cd packages/memtomem/tests-js
npm ci      # use lockfile
npm test    # vitest run
```

CI runs the same two commands — see `.github/workflows/ci.yml`
(`test-js` job).

## Adding a test

1. Pick the smallest set of modules needed (the orphan-render test
   needs `i18n.js + app.js`; the `applyDOM` test needs only `i18n.js`).
2. Call `bootApp({ scripts: [...] })` to get a fresh JSDOM.
3. Mutate `window.STATE` and append fixture DOM nodes inside the
   returned window. (Top-level `const` bindings — `STATE`, `I18N` —
   are lifted onto `window` by the bootstrap shim in `bootApp`. Top-
   level `function` declarations like `_renderMemorySourceTree` are
   on `window` automatically per script semantics.)
4. Call the function under test and assert against the resulting DOM.

## Out of scope

- Full DOM E2E (use Playwright via `feedback_playwright_mcp_web_verification.md`).
- Visual regression.
- Module bundling / a build step that diverges from the served
  static files.

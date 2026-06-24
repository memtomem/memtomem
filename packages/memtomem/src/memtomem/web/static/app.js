/* memtomem Web UI — Vanilla JS SPA */
'use strict';

const API = '';  // same origin
const HOME_ACTIVITY_TIMELINE_LIMIT = 1000;

// ── Early declarations (referenced before their section) ──
const _HELP_VISIBLE_KEY = 'm2m-help-visible';

// Safe localStorage read — returns null when storage is unavailable. Some
// locked-down private modes throw on any access (not just setItem), so boot
// reads must route through this or a locked-down browser aborts app.js at module
// load, before the UI (and the first-run landing fallback) ever wires up.
function _lsGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

// App-level Simple/Advanced progressive-disclosure flag (S2.2). A SECOND,
// independent axis from the server-driven dev/prod ui-tier: this one is a
// per-user persisted preference (Simple by default, gateway D-F precedent) that
// demotes power-user surfaces (Tags, Timeline, the Settings → Data group) behind
// an Advanced toggle in the global <header>. Declared early — next to _lsGet —
// because the shared visibility predicate (_isUiTierVisible) reads it. The full
// lifecycle contract lives on _applyAppSimpleMode below.
const APP_SIMPLE_KEY = 'm2m-app-simple';
const APP_SIMPLE_DEFAULT = true;  // Simple-when-unset (no stamp on boot)
let _appSimple = (() => {
  const stored = _lsGet(APP_SIMPLE_KEY);
  return stored !== null ? stored === '1' : APP_SIMPLE_DEFAULT;
})();

// ── Unified global state ──
const STATE = {
  lastSettingsSection: null,
  selectedChunkId: null,
  selectedOriginal: '',
  lastQuery: '',
  selectedIds: new Set(),
  lastResults: [],
  currentTopK: 10,
  viewMode: 'card',
  scoreMin: 0,
  currentSortMode: 'score',
  maxResultScore: 0,
  resultScoreViews: {},
  sourcesBrowserStale: false,
  tagsTabStale: false,
  homeStale: false,
  detailViewSource: '',
  detailViewMode: 'view',
  allSources: [],
  memoryStatusByPath: {},
  sourcesSortBy: 'name',
  sourcesNsFilter: '',
  // Path the next ``_renderMemorySourceTree`` should focus + browse
  // after rendering the tree. ``_navigateToSource`` sets this and
  // ``activateTab('sources')`` triggers ``loadSources`` →
  // ``renderSourceTree`` → render handles activation. Cleared
  // immediately on consume so a follow-up filter/sort re-render
  // doesn't re-scrollIntoView an item the user already navigated past.
  pendingActivatePath: '',
  // Chunk id the next ``browseSource`` should scroll/expand/flash after
  // rendering the chunk list. Set by ``_navigateToSource`` when the
  // caller knows the specific chunk (e.g. Timeline → Source jump);
  // empty string means "source-level only, no chunk highlight".
  // Preserved across a same-source miss so a larger follow-up fetch
  // (e.g. Load All / future pagination) can retry the highlight.
  pendingActivateChunkId: '',
  pendingActivateChunkSourcePath: '',
  // Active Sources vendor sub-tab (one of ``user`` / ``claude`` /
  // ``openai``). Lazily populated from localStorage on first render so
  // the value survives reloads. Keeping it on STATE means re-renders
  // (filter typing, sort change) read the active vendor without
  // hitting localStorage on the hot path.
  sourcesActiveVendor: null,
  // Per Sources vendor/category visible source-row or folder budget. Mirrors
  // Search's paging shape, but the data is already client-side so scrolling
  // just expands the rendered slice by 10 units.
  sourcesCategoryLimits: {},
  sourcesExpandedDirs: {},
  sourcesActiveCategoryByVendor: {},
  sourcesBodyFilterQuery: '',
  sourcesBodyFilterPaths: null,
  sourcesBodyFilterPending: false,
  dedupScanActive: false,
  dedupAbortCtrl: null,
  lastTagsData: [],
  tagsView: 'cloud',
  tagsSortBy: 'count-desc',
  serverConfig: null,
  serverDefaults: null,
  // Compiled RegExp objects from /api/privacy/patterns (#580). The
  // compose-mode Add handler scans textarea content against these and
  // shows a confirm dialog on a hit. Lazy-init: ``null`` until the
  // boot-time fetch resolves; on fetch failure stays ``null`` and the
  // scan is silently skipped (defense-in-depth, not a hard gate).
  privacyPatterns: null,
  // True while *any* indexing run (Index tab streaming, Sources card
  // single-dir reindex, Sources card "reindex all") is in flight. The
  // shared flag prevents double-trigger across surfaces — clicking a
  // second indexing button mid-run shows a toast instead of starting a
  // concurrent run that would race on the same DB rows. Mutated only
  // through ``_indexingTryStart`` / ``_indexingEnd``; reading directly
  // is fine.
  indexing: false,
  // Active ``setTimeout`` handle for ``_indexingPollUntilIdle``. Single-
  // flight guard so visibilitychange + boot-hydration don't stack
  // concurrent pollers. Cleared by ``_indexingEnd`` and by the tick
  // itself before re-arming.
  _indexingPollHandle: null,
  lastRetrievalStats: null,
  groupMode: false,
  cmdPaletteOpen: false,
  pendingGKey: false,
  touchStartX: 0,
  touchStartY: 0,
  helpVisible: true,
  // Default to 'prod' so boot-race or fetch failure leaves the polished
  // surface in place. Overwritten by the result of ``GET /api/system/ui-mode``
  // during ``initUiMode()``.
  uiMode: 'prod',
};

// ── C3: Theme init ──
(function initTheme() {
  const saved = _lsGet('m2m-theme');
  const el = document.documentElement;
  if (saved === 'light') {
    el.setAttribute('data-theme', 'light');
  } else if (!saved && window.matchMedia('(prefers-color-scheme: light)').matches) {
    el.setAttribute('data-theme', 'light');
  }
  // Update toggle icon and finalize initialization on DOM ready
  document.addEventListener('DOMContentLoaded', async () => {
    const isDark = el.getAttribute('data-theme') !== 'light';
    qs('theme-toggle').textContent = isDark ? '🌙' : '☀️';
    // Resolve UI mode + fetch locales in parallel. Both gate rendering.
    const uiModePromise = initUiMode();
    if (typeof I18N !== 'undefined') await I18N.init();
    await uiModePromise;
    // Fire-and-forget: privacy-pattern fetch for the compose warning.
    // Soft-fails if the endpoint is unavailable (cache stays null,
    // submit handler skips the scan). Not awaited — Add doesn't gate
    // on it (#580).
    loadPrivacyPatterns();
    renderRecentChips();
    _initSearchWelcome();
    _initHomeOrientation();
    _initFirstRunWizard();
    _initTabHelp();
    // Server-bound indicator hydration (#582 item 4.11). Restores the
    // header pill if a run is in flight (page-reload survival, second-
    // tab visibility) — see ``_indexingHydrateFromServer``.
    _indexingHydrateFromServer();
    // #696: hydrate the model-readiness banner so a cold-cache install
    // sees "Loading model…" / "Downloading bge-m3 (~2.3 GB)…" instead
    // of a frozen Search button. Hydrate is a no-op when the model is
    // already loaded.
    _modelReadinessHydrate();
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        if (!STATE.indexing) {
          _indexingHydrateFromServer();
        }
        // Re-hydrate readiness on tab focus so a load that completed in
        // a backgrounded tab doesn't leave the banner stuck up.
        _modelReadinessHydrate();
      }
    });
    // Activate tab from URL hash now that i18n has loaded — tabs that
    // render JS-built UI (like the Sources tab's Memory Dirs panel) call
    // ``t()`` at build time, so they must run after the locale cache is
    // populated to avoid raw-key flashes.
    const hash = location.hash.slice(1);
    if (hash) {
      if (_visibleMainTabs().includes(hash)) {
        activateTab(hash);
      } else if (document.querySelector(`.tab-btn[data-tab="${hash}"]`)) {
        // A real tab that the current mode hides (e.g. a deep-link to #timeline
        // while in Simple). Don't strand on an empty panel — the statically
        // active tab stays; surface *why* the link didn't open so the user can
        // flip to Advanced. (No dev main-tabs exist, so the advanced copy fits.)
        showToast(t('toast.advanced_only_section'), 'info');
      }
    }
  });
})();

// ---------------------------------------------------------------------------
// UI mode (prod / dev) — hides opt-in maintainer pages by default.
// ---------------------------------------------------------------------------

async function initUiMode() {
  try {
    const d = await api('GET', '/api/system/ui-mode');
    STATE.uiMode = d && d.mode === 'dev' ? 'dev' : 'prod';
  } catch (err) {
    // Keep the 'prod' default — degrading toward the polished surface is
    // safer than showing maintainer pages on a boot-time fetch failure.
    console.warn('[ui-mode]', err);
    STATE.uiMode = 'prod';
  }
  _applyUiModeFilter();
}

function _applyUiModeFilter() {
  const isProd = STATE.uiMode !== 'dev';
  document.body.classList.toggle('dev-mode', !isProd);
  const banner = qs('dev-mode-banner');
  if (banner) banner.hidden = isProd;
  document.querySelectorAll('[data-ui-tier="dev"]').forEach(el => {
    // Belt-and-braces: some dev-only targets live inside `display: flex`
    // parents (the Settings nav column) where `hidden` alone can be
    // overridden by stylesheet rules. Pairing it with `display: none`
    // keeps the filter predictable across CSS contexts.
    if (isProd) {
      el.hidden = true;
      el.style.display = 'none';
    } else {
      el.hidden = false;
      el.style.display = '';
    }
  });
  // Boot-time populate for namespace filter dropdowns. loadNamespaceDropdowns
  // self-gates on STATE.uiMode, so this is safe to fire in both modes — prod
  // is a no-op. Replaces a bare module-level call in settings-namespaces.js
  // that was racing initUiMode (STATE.uiMode still defaulted to 'prod' at
  // module load, so the bare call never helped dev mode either).
  if (typeof loadNamespaceDropdowns === 'function') loadNamespaceDropdowns();
}

// Single source of truth for "is this tab / settings-section button shown".
// Composes the TWO orthogonal visibility axes — an element is visible only if
// NEITHER hides it:
//   * server dev/prod : data-ui-tier="dev" is hidden unless STATE.uiMode==='dev'
//   * user Simple/Adv  : data-ui-adv="advanced" is hidden while _appSimple is on
// Every navigation guard (boot-hash dispatch, popstate, arrow-nav,
// activateTab, switchSettingsSection, the landing-default) routes through
// _visibleMainTabs / _visibleSettingsSections, so a demoted surface can never be
// *reached* while it is painted-hidden — closing the #1358 strand class for both
// axes at once. The two channels never collide: dev/prod writes inline
// style.display in _applyUiModeFilter; app-simple paints via the body.app-simple
// CSS class. data-ui-adv is orthogonal to data-ui-tier (an element may carry
// both); dev mode reveals dev-tier regardless of app-simple, and app-simple
// independently governs the advanced (non-dev) surface.
function _isUiTierVisible(btn) {
  if (STATE.uiMode !== 'dev' && btn.dataset.uiTier === 'dev') return false;
  if (_appSimple && btn.dataset.uiAdv === 'advanced') return false;
  return true;
}

function _visibleMainTabs() {
  return Array.from(document.querySelectorAll('.tab-btn'))
    .filter(_isUiTierVisible)
    .map(btn => btn.dataset.tab)
    .filter(Boolean);
}

function _visibleSettingsSections() {
  return Array.from(document.querySelectorAll('.settings-nav-btn'))
    .filter(_isUiTierVisible)
    .map(btn => btn.dataset.section)
    .filter(Boolean);
}

// -- App Simple/Advanced lifecycle (S2.2) -------------------------------------
// Mirror of the gateway's _ctxSimpleMode pattern, promoted to app level. The
// toggle lives in the global <header> (outside <main> and every .tab-panel), so
// it stays reachable on EVERY entry path — deep-link, reload-into-subsection,
// Back/Forward — the central #1358 lesson that a persisted mode flag must never
// hide its own escape hatch. Idempotent; safe to call on load and every flip.
function _applyAppSimpleMode() {
  document.body.classList.toggle('app-simple', _appSimple);
  const btn = document.getElementById('app-mode-toggle');
  if (!btn) return;
  // aria-pressed reflects "Advanced engaged" (the expanded, non-default state),
  // so the pressed/highlighted cue means "extra surfaces shown".
  btn.setAttribute('aria-pressed', _appSimple ? 'false' : 'true');
  const label = btn.querySelector('.app-mode-label');
  if (!label) return;
  // Mode-display label: shows the CURRENT mode. Drive it through the data-i18n
  // attribute so i18n applyDOM (init + every langchange) keeps it translated;
  // update textContent now only once the locale cache is populated (t() returns
  // the raw key until then — the guard avoids a key flash, and t may not even be
  // defined yet depending on script order).
  const key = _appSimple ? 'nav.mode_simple' : 'nav.mode_advanced';
  label.dataset.i18n = key;
  const txt = typeof t === 'function' ? t(key) : key;
  // Localized text once the cache is populated; otherwise a literal that still
  // matches the CURRENT mode, so a persisted-Advanced boot never momentarily
  // reads "Simple" before applyDOM runs. applyDOM (init + every langchange) then
  // swaps in the translated string from the data-i18n attribute set above.
  label.textContent = txt !== key ? txt : (_appSimple ? 'Simple' : 'Advanced');
}

// Flip the flag, persist (only on explicit user action — never stamped on boot,
// so a fresh install has no key and the tri-state default yields Simple without
// polluting first-run detection), and re-apply.
function _setAppSimple(on) {
  _appSimple = !!on;
  try {
    localStorage.setItem(APP_SIMPLE_KEY, _appSimple ? '1' : '0');
  } catch { /* locked-down storage — in-memory flag still applies this session */ }
  _applyAppSimpleMode();
}

// Apply the persisted flag once at module load. app.js is the last script before
// </body>, so the header toggle markup already exists; advanced tabs are inert
// until activated, so toggling the body class now causes no flash.
_applyAppSimpleMode();

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

// CSRF token bootstrap (RFC #787 stage 1). Server generates a per-process
// token at create_app() and exposes it via GET /api/session. The api()
// helper (and the FormData uploaders that bypass it — see ``ensureCsrfToken``
// callsites) thread the token through every unsafe-method request via
// ``X-Memtomem-CSRF``. Stage 1 server-side is observe-only, so a missing
// header just produces a log line; stage 2 will enforce. We do the work now
// so the flip is a server-side toggle, not a coordinated client release.
let _csrfToken = null;
let _csrfTokenPromise = null;
const _UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

async function ensureCsrfToken() {
  if (_csrfToken) return _csrfToken;
  if (_csrfTokenPromise) return _csrfTokenPromise;
  _csrfTokenPromise = (async () => {
    try {
      const res = await fetch(API + '/api/session', { method: 'GET' });
      if (!res.ok) return '';
      const data = await res.json();
      _csrfToken = data?.csrf || '';
    } catch (_) {
      _csrfToken = '';
    } finally {
      _csrfTokenPromise = null;
    }
    return _csrfToken;
  })();
  return _csrfTokenPromise;
}

// Trust-boundary redaction guard (PR #784). Server mutating routes return
// HTTP 403 with ``{detail: {detail: "redaction_blocked", hits, surface}}``
// (FastAPI nests our structured payload under an outer ``detail``); the
// SPA parses it via ``api()`` below and surfaces a confirm-and-retry UX
// through ``apiWithRedactionRetry()``. Issue #785.
class RedactionBlockedError extends Error {
  constructor({ hits, surface }) {
    super('redaction_blocked');
    this.name = 'RedactionBlockedError';
    this.hits = hits;
    this.surface = surface;
  }
}

// ADR-0011 §5 Gate B — project_shared writes without an explicit
// ``confirm_project_shared=true`` return HTTP 403 with the structured
// payload defined by ``ProjectTierBlockedResponse``. The SPA preserves
// ``cli_hint`` + ``docs_url`` on the error object so the caller can
// surface "rejected, here's the equivalent CLI invocation" without
// rewriting the prose client-side (the localized title + body live in
// the locale files; the hint / link come from the server so they stay
// in lockstep with the back-end vocabulary).
class ProjectTierBlockedError extends Error {
  constructor({ surface, scope, message, cli_hint, docs_url }) {
    super(message || 'blocked_project_shared');
    this.name = 'ProjectTierBlockedError';
    this.surface = surface;
    this.scope = scope;
    this.cliHint = cli_hint;
    this.docsUrl = docs_url;
  }
}

// Shared formatter for the project-tier rejection toast. Returns the
// multi-line string both ``addMemoryFromCompose``'s catch-arm and the
// Playwright spec build off the same code path — without this helper
// the test would silently pass even if the production catch-arm
// stopped showing one of the actionable fields (Codex review #924).
// Both ``cliHint`` and ``docsUrl`` are optional because the Gate A
// hard-refusal for ``force_unsafe=true`` on project_shared reuses
// the ``blocked_project_shared`` discriminant without those fields.
function formatProjectTierBlockedToast(err) {
  const lines = [err.message];
  if (err.cliHint) lines.push(`$ ${err.cliHint}`);
  if (err.docsUrl) lines.push(err.docsUrl);
  return lines.join('\n');
}

async function api(method, path, body, opts = {}) {
  if (typeof opts !== 'object' || Array.isArray(opts)) opts = {};
  const fetchOpts = { method, headers: { 'Content-Type': 'application/json' } };
  if (_UNSAFE_METHODS.has(method.toUpperCase())) {
    const tok = await ensureCsrfToken();
    if (tok) fetchOpts.headers['X-Memtomem-CSRF'] = tok;
  }
  if (body !== undefined) fetchOpts.body = JSON.stringify(body);
  if (opts.signal) fetchOpts.signal = opts.signal;
  else fetchOpts.signal = AbortSignal.timeout(opts.timeout ?? 30_000);
  const res = await fetch(API + path, fetchOpts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    if (
      res.status === 403 &&
      err && typeof err.detail === 'object' && err.detail !== null &&
      err.detail.detail === 'redaction_blocked'
    ) {
      throw new RedactionBlockedError({
        hits: err.detail.hits,
        surface: err.detail.surface,
      });
    }
    if (
      res.status === 403 &&
      err && typeof err.detail === 'object' && err.detail !== null &&
      err.detail.detail === 'blocked_project_shared'
    ) {
      throw new ProjectTierBlockedError({
        surface: err.detail.surface,
        scope: err.detail.scope,
        message: err.detail.message,
        cli_hint: err.detail.cli_hint,
        docs_url: err.detail.docs_url,
      });
    }
    // Harden against ``[object Object]`` when ``detail`` is itself a dict
    // (e.g. structured 4xx that isn't redaction_blocked).
    const msg = typeof err.detail === 'string'
      ? err.detail
      : (err.detail && typeof err.detail === 'object' && typeof err.detail.detail === 'string'
        ? err.detail.detail
        : res.statusText);
    // Thrown errors expose `.status` so callers can branch on HTTP code
    // (e.g. 404 → mark resource missing instead of generic toast).
    const apiErr = new Error(msg);
    apiErr.status = res.status;
    throw apiErr;
  }
  return res.json();
}

// Wraps ``api()`` for write surfaces guarded by the trust-boundary redaction
// filter. On a 403, prompts the user via ``showConfirm`` with the matched
// pattern count and the localized surface label, then re-issues the same
// request with ``body.force_unsafe = true``.
//
// Returns the JSON response on success (initial or retry), ``null`` if the
// user declined the bypass. Non-redaction errors (and a second-pass error
// after the user confirmed) propagate so existing call-site catches keep
// working unchanged. Emits ``toast.redaction_bypassed`` on retry success
// so the audit-log bypass is visible to the operator without forcing each
// call site to add its own affirmation.
async function apiWithRedactionRetry(method, path, body, opts = {}) {
  try {
    return await api(method, path, body, opts);
  } catch (err) {
    if (!(err instanceof RedactionBlockedError)) throw err;
    const surfaceKey = 'surface.' + err.surface;
    const localized = t(surfaceKey);
    const surfaceLabel = localized === surfaceKey ? err.surface : localized;
    const ok = await showConfirm({
      title: t('confirm.redaction_blocked_title'),
      message: t('confirm.redaction_blocked_message', {
        hits: err.hits,
        surface: surfaceLabel,
      }),
      confirmText: t('confirm.redaction_blocked_proceed'),
    });
    if (!ok) return null;
    const retryBody = Object.assign({}, body || {}, { force_unsafe: true });
    const data = await api(method, path, retryBody, opts);
    showToast(t('toast.redaction_bypassed', { hits: err.hits }), 'info');
    return data;
  }
}

// Multipart upload variant of ``apiWithRedactionRetry``. ``/api/upload``
// returns HTTP 200 with per-file ``error="redaction_blocked (hits=N)"``
// strings (system.py:1161) instead of a structured 403, so the dialog is
// driven off the per-file error scan rather than a typed exception. On
// confirm, re-issues a *narrowed* FormData containing only the blocked
// entries with ``?force_unsafe=true``; clean files from the first pass
// are already persisted server-side and are not re-sent (issue #803 —
// the previous full-batch retry let the server's ``_{mtime_ns}``
// collision suffix at system.py:1121 silently duplicate every clean
// file in any mixed batch, since that suffix was meant for genuine
// name collisions, not retry-batch deduplication).
//
// The retry result rows are merged back into the original ``data.files``
// at their original positions, so the caller renders one row per input
// file regardless of which pass produced it.
//
// Returns ``{data, cancelled, bypassed, blockedFileCount}``: ``data`` is
// the (merged) response to render, ``cancelled`` is true when the user
// declined the bypass (caller should still render ``data`` to surface
// the per-file errors), ``bypassed`` is true only when **every** blocked
// row came back from the retry without an ``error`` field (a partial or
// malformed retry response keeps ``bypassed`` false and substitutes
// ``toast.redaction_bypass_partial`` for the success toast — see the
// validation block below), and ``blockedFileCount`` is the number of
// files that triggered the guard (used by callers to localize the
// cancel-toast).
const _UPLOAD_REDACTION_BLOCKED_RE = /^redaction_blocked \(hits=(\d+)\)$/;
async function uploadFilesWithRedactionRetry(formData) {
  async function _post(form, forceUnsafe) {
    const csrf = await ensureCsrfToken();
    const headers = csrf ? { 'X-Memtomem-CSRF': csrf } : {};
    const url = forceUnsafe ? '/api/upload?force_unsafe=true' : '/api/upload';
    const res = await fetch(url, { method: 'POST', body: form, headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      const detail = err.detail;
      const errMsg = typeof detail === 'string'
        ? detail
        : (detail && typeof detail === 'object' && typeof detail.detail === 'string'
          ? detail.detail
          : res.statusText);
      throw new Error(errMsg);
    }
    return res.json();
  }

  const data = await _post(formData, false);
  // Walk per-file rows once, recording (rowIndex, hits) pairs for blocked
  // entries. Index-based matching handles duplicate basenames in the same
  // batch — the server tags rows positionally (one result per ``files``
  // entry, in order), and we mirror that here when narrowing the retry.
  const blockedRows = [];
  (data.files || []).forEach((r, i) => {
    const m = r.error && r.error.match(_UPLOAD_REDACTION_BLOCKED_RE);
    if (m) blockedRows.push({ index: i, hits: parseInt(m[1], 10) });
  });
  if (!blockedRows.length) {
    return { data, cancelled: false, bypassed: false, blockedFileCount: 0 };
  }

  const totalHits = blockedRows.reduce((a, r) => a + r.hits, 0);
  const surfaceKey = 'surface.web_api_upload';
  const localized = t(surfaceKey);
  const surfaceLabel = localized === surfaceKey ? 'web_api_upload' : localized;
  const ok = await showConfirm({
    title: t('confirm.redaction_blocked_title'),
    message: t('confirm.redaction_blocked_message', {
      hits: totalHits,
      surface: surfaceLabel,
    }),
    confirmText: t('confirm.redaction_blocked_proceed'),
  });
  if (!ok) {
    return { data, cancelled: true, bypassed: false, blockedFileCount: blockedRows.length };
  }

  // Build a narrowed FormData with only the blocked entries, preserving
  // original order. ``getAll('files')`` returns the entries in insertion
  // order, so blockedRows[k].index aligns with allFiles[blockedRows[k].index].
  const allFiles = formData.getAll('files');
  const retryForm = new FormData();
  for (const row of blockedRows) retryForm.append('files', allFiles[row.index]);
  const retryData = await _post(retryForm, true);

  // Merge retry rows back into their original positions. The server
  // returns retry results in the same order as the narrowed FormData,
  // so retryData.files[k] corresponds to blockedRows[k].
  const mergedFiles = (data.files || []).slice();
  const retryFiles = retryData.files || [];
  retryFiles.forEach((row, k) => {
    if (k < blockedRows.length) mergedFiles[blockedRows[k].index] = row;
  });
  const mergedData = {
    ...data,
    files: mergedFiles,
    total_indexed: mergedFiles.reduce((s, r) => s + (r.indexed_chunks || 0), 0),
  };

  // Validate that the bypass actually wrote every blocked file. ``/api/upload``
  // reports per-file failures inside an HTTP-200 response, so a clean status
  // alone is not proof that the retry landed — emitting
  // ``toast.redaction_bypassed`` ("entry written") on a malformed or partially
  // failed retry would falsely audit a non-write. Two ways the retry can lie:
  //   - ``retryFiles.length !== blockedRows.length`` (server returned fewer
  //     rows than we re-sent — shape regression or upstream truncation).
  //   - any retry row still carries ``error`` (force_unsafe was honored at the
  //     route level but the per-file write hit a different failure, e.g. a
  //     non-redaction validation error or a second redaction class the bypass
  //     didn't cover).
  // On either, suppress the bypass-success toast and emit
  // ``toast.redaction_bypass_partial`` with the actual succeeded/total counts
  // so the operator sees that some blocked files did not land. ``bypassed``
  // tracks the same boolean so callers can branch off it.
  //
  // ``succeededCount`` is clamped to ``blockedRows.length`` rows so that an
  // over-long retry response (server returns more rows than we re-sent —
  // shape regression in the other direction) cannot produce nonsense counts
  // like "3 of 2 written" in the partial toast.
  const succeededCount = retryFiles
    .slice(0, blockedRows.length)
    .filter(r => !r.error).length;
  const fullySucceeded =
    retryFiles.length === blockedRows.length && succeededCount === blockedRows.length;
  if (fullySucceeded) {
    showToast(t('toast.redaction_bypassed', { hits: totalHits }), 'info');
  } else {
    showToast(
      t('toast.redaction_bypass_partial', {
        succeeded: succeededCount,
        total: blockedRows.length,
      }),
      'error',
    );
  }
  return {
    data: mergedData,
    cancelled: false,
    bypassed: fullySucceeded,
    blockedFileCount: blockedRows.length,
  };
}

function qs(id) { return document.getElementById(id); }
function show(el)  { if (el) { el.hidden = false; el.style.display = ''; } }
function hide(el)  { if (el) el.hidden = true; }

// ---------------------------------------------------------------------------
// Modal accessibility (A11Y-1.2 / 1.5 / 3.2 — issue #1053 PR #2)
// ---------------------------------------------------------------------------

// Top-of-stack = most-recently-opened modal. Stacking happens today:
// settings-modal can have a maintenance showConfirm dialog on top of it
// (audit row A11Y-3.3). Inert state is derived from this stack each
// transition, never from a per-open snapshot — so closing the inner modal
// recomputes inert correctly and leaves the outer modal's background still
// inerted.
const _ACTIVE_MODALS = [];
const _MODAL_CLOSERS = new Map(); // HTMLElement -> () => void

function _recomputeBackgroundInert() {
  const active = new Set(_ACTIVE_MODALS);
  const hasAny = _ACTIVE_MODALS.length > 0;
  Array.from(document.body.children).forEach(el => {
    if (!hasAny || active.has(el)) el.removeAttribute('inert');
    else el.setAttribute('inert', '');
  });
}

// openModalA11y(modal, { focusables })
//   - captures the trigger (document.activeElement) for focus restoration
//   - pushes modal onto _ACTIVE_MODALS and recomputes background inert
//   - when ``focusables`` is a function, installs a capture-phase keydown
//     trap that cycles Tab/Shift+Tab through the returned list
// Returns a release() closure the caller MUST invoke from its close path;
// release() is idempotent so double-call (ESC + close-btn race) is safe.
function openModalA11y(modal, { focusables = null } = {}) {
  const previouslyFocused = document.activeElement;
  _ACTIVE_MODALS.push(modal);
  _recomputeBackgroundInert();

  let onKey = null;
  if (typeof focusables === 'function') {
    onKey = (e) => {
      if (modal.hidden || e.key !== 'Tab') return;
      const f = focusables();
      if (!f.length) return;
      e.preventDefault();
      const idx = f.indexOf(document.activeElement);
      f[(idx + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
    };
    document.addEventListener('keydown', onKey, true);
  }

  let released = false;
  return function releaseA11y() {
    if (released) return;
    released = true;
    const idx = _ACTIVE_MODALS.indexOf(modal);
    if (idx !== -1) _ACTIVE_MODALS.splice(idx, 1);
    _recomputeBackgroundInert();
    if (onKey) document.removeEventListener('keydown', onKey, true);
    if (
      previouslyFocused
      && document.contains(previouslyFocused)
      && typeof previouslyFocused.focus === 'function'
    ) {
      previouslyFocused.focus();
    }
  };
}

function registerModalCloser(modal, closeFn) { _MODAL_CLOSERS.set(modal, closeFn); }
// Falls back to hide() when no closer is registered so the ESC dispatcher
// stays correct for any modal-overlay not yet migrated.
function closeModal(modal) {
  const fn = _MODAL_CLOSERS.get(modal);
  if (fn) fn(); else hide(modal);
}

// Expose helpers on window so path-picker.js / context-gateway.js /
// settings-namespaces.js can use them without import wiring (matches the
// existing window.showConfirm / window.PathPicker style).
window.openModalA11y = openModalA11y;
window.registerModalCloser = registerModalCloser;
window.closeModal = closeModal;

// openModal(el, opts) — show + a11y in one call. The single open path that
// keeps _ACTIVE_MODALS accurate; every modal-overlay opener should funnel
// through here so window.isAnyModalOpen() reflects every overlay on screen
// and the global shortcut gate (A11Y-3.1) can trust it. Returns the release
// closure the caller must invoke on close (idempotent, so double-call is
// safe — e.g. Esc + close-btn race).
window.openModal = function (el, opts = {}) {
  show(el);
  return openModalA11y(el, opts);
};

// A11Y-3.1 gate primitives. Both shortcut listeners (app.js bare keys,
// settings-namespaces.js Cmd+K / tab numbers) consult these to decide
// whether a keypress should fire or defer to whatever modal is on top.
// isTopModal() lets the gate preserve toggle-close semantics (?, Cmd+K)
// for the modal that owns the top of the stack — anything underneath
// keeps full focus, so a stray ? while the settings modal is up cannot
// pop the shortcuts modal on top of it.
window.isAnyModalOpen = () => _ACTIVE_MODALS.length > 0;
window.isTopModal = (el) => _ACTIVE_MODALS.length > 0
  && _ACTIVE_MODALS[_ACTIVE_MODALS.length - 1] === el;
function setMsg(el, text, isErr) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status-msg ' + (isErr ? 'err' : 'ok');
  show(el);
  setTimeout(() => hide(el), 4000);
}
function truncate(str, n) { return str.length > n ? str.slice(0, n) + '…' : str; }
function basename(path) { return path.split('/').pop() || path; }
// Server returns absolute paths under $HOME/.memtomem/...; the browser doesn't
// know $HOME, so collapse the host-specific prefix to ~ for display by the
// well-known `.memtomem/` segment that backend code (system.py) anchors on.
function tildifyPath(p) {
  if (!p) return p;
  const idx = p.indexOf('/.memtomem/');
  if (idx > 0) return '~/.memtomem/' + p.slice(idx + '/.memtomem/'.length);
  return p;
}
function shortDir(dir) {
  const parts = dir.split('/').filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : dir;
}
function formatBytes(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
function fileIcon(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  const map = {
    md: '📝', markdown: '📝',
    py: '🐍',
    js: '🟨', ts: '🔷', jsx: '🟨', tsx: '🔷',
    json: '{}',
    txt: '📄', text: '📄',
    rs: '🦀', go: '🐹',
    sh: '💲', bash: '💲',
    yaml: '⚙️', yml: '⚙️', toml: '⚙️',
    html: '🌐', css: '🎨',
    csv: '📊',
  };
  return map[ext] || '📄';
}

function fileTypeColor(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  const map = {
    md: 'var(--accent)', markdown: 'var(--accent)',
    py: 'var(--green)',
    js: '#e0a800', ts: '#e0a800', jsx: '#e0a800', tsx: '#e0a800',
    json: '#a29bfe', yaml: '#a29bfe', yml: '#a29bfe', toml: '#a29bfe',
    html: '#e17055', css: '#e17055',
  };
  return map[ext] || 'var(--muted)';
}

function relativeTime(dateStr) {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return t('time.relative.just_now');
  const m = Math.floor(s / 60);
  if (m < 60) return t('time.relative.minutes_ago', { m });
  const h = Math.floor(m / 60);
  if (h < 24) return t('time.relative.hours_ago', { h });
  const d = Math.floor(h / 24);
  if (d < 30) return t('time.relative.days_ago', { d });
  const mo = Math.floor(d / 30);
  if (mo < 12) return t('time.relative.months_ago', { mo });
  return t('time.relative.years_ago', { y: Math.floor(mo / 12) });
}

// Bucketing/display by chunk timestamp must reflect the browser's local
// zone, not the literal UTC ISO string. Slicing ``created_at.slice(0,10)``
// puts users east of UTC into the previous calendar day for hours after
// local midnight (and shifts ``HH:MM`` by the offset). Both helpers
// round-trip through ``new Date(iso)`` so callers — Timeline grouping,
// Timeline ``HH:MM`` display, Home activity heatmap counts — share one
// local-zone source of truth.
function localDateKey(iso) {
  const d = new Date(iso);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}
function localTimeShort(iso) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// ── Temporal-validity badge (RFC §Goal 7) ──
// Renders ``Valid: 2025-08-15 → 2026-03-31`` (∞ for unbounded sides) when
// either bound is set. Always-valid chunks (both bounds null) hide the
// badge entirely. Expired (``now > valid_to``) gets a muted/strike style
// — the chunk stays visible (validity is a search-time concept), only the
// presentation flags it.
function _formatValidityDate(unix) {
  if (unix === null || unix === undefined) return '∞';
  // toISOString returns YYYY-MM-DDTHH:MM:SS.sssZ — slice to the date part.
  // unix is seconds; Date constructor expects ms.
  return new Date(unix * 1000).toISOString().slice(0, 10);
}

// HTML-string variant for innerHTML templating in the result list
// (where we already build a string-template). Returns "" for chunks
// without a window so the caller can ${validityBadge} unconditionally.
// Uses _formatValidityDate which produces strict ISO date strings —
// no XSS surface in the interpolated content.
// ADR-0016 §7 canonical-residency tier badge for memory rows. Returns
// "" for the user tier (the default — keeps the common case visually
// quiet) and a token-literal span for ``project_shared`` /
// ``project_local``. The three tokens are rendered verbatim (no
// display aliases — pinned by the Tiered Context Gateway v2 contract).
// ``isContextRow`` adds the ``(no runtime fan-out)`` annotation for
// project_local context artifacts (agents / skills / commands) per
// ADR-0011 §3 — memory rows skip the annotation because project_local
// memory still fans out via memory's own contract.
function _tierBadgeHtml(targetScope, { isContextRow = false } = {}) {
  if (!targetScope || targetScope === 'user') return '';
  if (targetScope !== 'project_shared' && targetScope !== 'project_local') return '';
  const cls = `badge badge-tier badge-tier--${targetScope}`;
  const badge = ` <span class="${cls}" data-tier="${targetScope}">${targetScope}</span>`;
  if (isContextRow && targetScope === 'project_local') {
    // The annotation is PROSE, so it goes through i18n (ADR-0001 §5.3 parity
    // gate, #1247 id 58) — unlike the tier token above, which is pinned
    // verbatim. Two cold-boot windows need the EN-literal fallback: ``t``
    // not yet defined (``typeof`` guard, mirrors ``_validityBadgeHtml``
    // below) AND ``t`` defined but the locale fetch not yet resolved — a
    // missing key makes ``t()`` return the KEY itself, which must never
    // reach the DOM (CI Playwright caught exactly that race).
    const annotationKey = 'settings.ctx.tier_no_fanout_annotation';
    const translated = typeof t === 'function' ? t(annotationKey) : annotationKey;
    const annotation = translated === annotationKey ? '(no runtime fan-out)' : translated;
    return `${badge}<span class="tier-fanout-annotation">${annotation}</span>`;
  }
  return badge;
}

function _validityBadgeHtml(validFromUnix, validToUnix) {
  const hasWindow = validFromUnix !== null && validFromUnix !== undefined
    || validToUnix !== null && validToUnix !== undefined;
  if (!hasWindow) return '';
  const label = (typeof t === 'function' ? t('search.detail_validity_label') : 'Valid');
  const from = _formatValidityDate(validFromUnix);
  const to = _formatValidityDate(validToUnix);
  const expired = validToUnix !== null && validToUnix !== undefined
    && Date.now() / 1000 > validToUnix;
  const cls = expired ? 'badge badge-validity badge-validity--expired' : 'badge badge-validity';
  return ` <span class="${cls}">${label}: ${from} → ${to}</span>`;
}

function _renderValidityBadge(el, validFromUnix, validToUnix) {
  if (!el) return;
  const hasWindow = validFromUnix !== null && validFromUnix !== undefined
    || validToUnix !== null && validToUnix !== undefined;
  if (!hasWindow) {
    el.hidden = true;
    el.textContent = '';
    el.classList.remove('badge-validity--expired');
    return;
  }
  const label = (typeof t === 'function' ? t('search.detail_validity_label') : 'Valid');
  const from = _formatValidityDate(validFromUnix);
  const to = _formatValidityDate(validToUnix);
  el.textContent = `${label}: ${from} → ${to}`;
  el.hidden = false;

  const expired = validToUnix !== null && validToUnix !== undefined
    && Date.now() / 1000 > validToUnix;
  el.classList.toggle('badge-validity--expired', expired);
  if (expired) {
    const expiredLabel = (typeof t === 'function' ? t('search.detail_validity_expired') : 'Expired');
    el.title = expiredLabel;
  } else {
    el.removeAttribute('title');
  }
}

// ── B1: Debounce ──
function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// Render a list of namespaces (preview placeholder OR result-row echo)
// using the ``index.ns_render.*`` i18n family. ``mode`` selects the
// suffix variant: ``'preview'`` for the placeholder hint,
// ``'applied'`` for the post-index result row. ``namespaces`` is a
// distinct list of ``str | null`` (null = untagged ``default`` carve-out).
// ``truncated`` and ``scanned`` come from the preview endpoint; passing
// ``truncated=false`` / ``scanned=0`` skips the suffix.
function renderResolvedNamespaces(namespaces, { truncated = false, scanned = 0, mode = 'applied' } = {}) {
  const list = Array.isArray(namespaces) ? namespaces : [];
  const isUntagged = list.length === 0 || (list.length === 1 && list[0] === null);
  let body;
  if (isUntagged) {
    body = t(`index.ns_render.untagged_${mode}`);
  } else if (list.length === 1) {
    body = t(`index.ns_render.single_${mode}`, { ns: list[0] });
  } else {
    // Mixed list may contain a trailing null sentinel; render it as
    // ``(untagged)`` inline so the joined display matches the rest.
    const display = list.map(n => n === null ? t('index.ns_render.untagged_applied') : n);
    body = t(`index.ns_render.multi_${mode}`, { list: display.join(', '), n: list.length });
  }
  if (truncated && scanned > 0) {
    body += t('index.ns_render.truncated_suffix', { scanned });
  }
  return body;
}

// ── B2: Copy to Clipboard ──
async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  showToast(t('toast.copied'), 'info');
}

// ── B3: Language Detection ──
function getLanguage(sourceFile) {
  const ext = (sourceFile || '').split('.').pop().toLowerCase();
  return { py: 'python', js: 'javascript', ts: 'typescript', json: 'json',
           sh: 'bash', bash: 'bash', yaml: 'yaml', yml: 'yaml',
           css: 'css', html: 'markup' }[ext] || null;
}

// ── D2: Line Diff ──
function diffLines(oldText, newText) {
  const a = oldText.split('\n'), b = newText.split('\n');
  const m = a.length, n = b.length;
  const dp = Array.from({length: m + 1}, () => new Uint16Array(n + 1));
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1] + 1 : Math.max(dp[i-1][j], dp[i][j-1]);
  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i-1] === b[j-1]) { ops.push({t:'=', l:a[i-1]}); i--; j--; }
    else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) { ops.push({t:'+', l:b[j-1]}); j--; }
    else { ops.push({t:'-', l:a[i-1]}); i--; }
  }
  return ops.reverse();
}
function renderDiff(ops) {
  return ops.map(op => {
    const cls = op.t === '+' ? 'diff-add' : op.t === '-' ? 'diff-del' : 'diff-eq';
    const prefix = op.t === '+' ? '+' : op.t === '-' ? '-' : ' ';
    return `<div class="diff-line ${cls}"><span class="diff-prefix">${prefix}</span>${escapeHtml(op.l)}</div>`;
  }).join('');
}

// ── A4: Loading Spinner ──
function btnLoading(btn, loading) {
  if (loading) {
    btn.disabled = true;
    btn.classList.add('btn-loading');
  } else {
    btn.disabled = false;
    btn.classList.remove('btn-loading');
  }
}

// ── Global indexing-state guard ──
// Centralizes start/end of any indexing run so all surfaces (Index tab
// streaming, Sources card per-dir reindex, Sources "Reindex All") share
// the same global flag and the same external indicator. ``tryStart``
// returns ``false`` (and shows a toast) when a run is already in flight
// so callers can early-return without starting a concurrent operation.
function _indexingTryStart() {
  if (STATE.indexing) {
    showToast(t('toast.indexing_in_progress'), 'info');
    return false;
  }
  STATE.indexing = true;
  const indicator = qs('indexing-indicator');
  if (indicator) show(indicator);
  return true;
}
async function _indexingTryStartOrRefresh() {
  if (!STATE.indexing) return _indexingTryStart();
  try {
    const r = await api('GET', '/api/indexing/active');
    if (!r || !r.active) {
      _indexingEnd();
      return _indexingTryStart();
    }
  } catch (err) {
    console.warn('[indexing-active preflight]', err);
  }
  showToast(t('toast.indexing_in_progress'), 'info');
  return false;
}
function _indexingEnd() {
  STATE.indexing = false;
  const indicator = qs('indexing-indicator');
  if (indicator) hide(indicator);
  if (STATE._indexingPollHandle) {
    clearTimeout(STATE._indexingPollHandle);
    STATE._indexingPollHandle = null;
  }
}

// ── Server-bound hydration (#582 item 4.11 follow-up to #602) ──
// PR #602 left the header indicator session-bound: a page reload mid-
// indexing reset ``STATE.indexing`` to ``false`` even though the server
// run continued. ``GET /api/indexing/active`` reports server truth so
// boot + tab-visibility-change can restore the indicator and a bounded
// poll clears it once the server-side run actually finishes.
const _INDEXING_POLL_MS = 3000;

async function _indexingHydrateFromServer() {
  try {
    const r = await api('GET', '/api/indexing/active');
    if (r && r.active && !STATE.indexing) {
      _indexingTryStart();
      _indexingPollUntilIdle();
    }
  } catch (err) {
    // Soft-fail: server unavailable just leaves the indicator off.
    console.warn('[indexing-active]', err);
  }
}

function _indexingPollUntilIdle() {
  if (STATE._indexingPollHandle) return; // single-flight
  const tick = async () => {
    STATE._indexingPollHandle = null;
    if (!STATE.indexing) return; // local handler already cleared it
    try {
      const r = await api('GET', '/api/indexing/active');
      if (!r || !r.active) {
        _indexingEnd();
        return;
      }
    } catch (err) {
      // Transient fetch failure — keep the indicator visible and retry.
      console.warn('[indexing-active poll]', err);
    }
    if (STATE.indexing) {
      STATE._indexingPollHandle = setTimeout(tick, _INDEXING_POLL_MS);
    }
  };
  STATE._indexingPollHandle = setTimeout(tick, _INDEXING_POLL_MS);
}

// ── Model readiness banner (issue #696) ──
// Surfaces the lazy fastembed loader's state in the header so users
// don't watch a frozen Search button during a multi-GB first-load. The
// banner copy is built from ``GET /api/system/model-readiness``; see
// ``packages/memtomem/src/memtomem/web/routes/system.py``.
const _MODEL_READINESS_POLL_MS = 4000;
// Cap the poll loop so a stuck server doesn't yield infinite background
// fetches. 200 × 4s ≈ 13 min — comfortably above the worst observed
// bge-m3 + reranker cold-download. Visibilitychange + the doSearch
// pre-flight re-arm the poll if the user comes back later.
const _MODEL_READINESS_MAX_TICKS = 200;

function _modelComponentActive(c) {
  return !!c && (c.state === 'loading' || c.state === 'downloading');
}

function _modelComponentDone(c) {
  // ``cold`` is intentionally NOT terminal here — when the doSearch
  // pre-flight kicks the poll, the backend's ``_loading`` flag flips
  // True only after our request reaches the lazy loader, so the first
  // poll tick can race the request and see ``cold``. Continuing the
  // loop lets the next tick catch the transition.
  return !!c && (c.state === 'ready' || c.state === 'skipped');
}

function _isModelPollTerminal(d) {
  if (!d) return false;
  if (d.embedder?.state === 'error' || d.reranker?.state === 'error') return true;
  return _modelComponentDone(d.embedder) && _modelComponentDone(d.reranker);
}

function _renderModelReadinessBanner(d) {
  const banner = qs('model-readiness-banner');
  const msg = qs('model-readiness-msg');
  if (!banner || !msg) return;
  const emb = (d && d.embedder) || {};
  const rer = (d && d.reranker) || {};
  const hasError = emb.state === 'error' || rer.state === 'error';
  const embDl = emb.state === 'downloading';
  const rerDl = rer.state === 'downloading';
  const anyLoading = emb.state === 'loading' || rer.state === 'loading';

  const fallbackEmbName = t('banner.model_fallback_embedder');
  const fallbackRerName = t('banner.model_fallback_reranker');

  if (hasError) {
    msg.textContent = t('banner.model_error');
    banner.removeAttribute('hidden');
  } else if (embDl && rerDl) {
    msg.textContent = t('banner.model_downloading_both', {
      emb: emb.model || fallbackEmbName,
      emb_size: emb.approx_size_mb != null ? emb.approx_size_mb : '?',
      rer: rer.model || fallbackRerName,
      rer_size: rer.approx_size_mb != null ? rer.approx_size_mb : '?',
    });
    banner.removeAttribute('hidden');
  } else if (embDl || rerDl) {
    const c = embDl ? emb : rer;
    const fallback = embDl ? fallbackEmbName : fallbackRerName;
    if (c.approx_size_mb != null) {
      msg.textContent = t('banner.model_downloading_one', {
        model: c.model || fallback,
        size: c.approx_size_mb,
      });
    } else {
      msg.textContent = t('banner.model_downloading_one_no_size', {
        model: c.model || fallback,
      });
    }
    banner.removeAttribute('hidden');
  } else if (anyLoading) {
    msg.textContent = t('banner.model_loading');
    banner.removeAttribute('hidden');
  } else {
    banner.setAttribute('hidden', '');
  }
}

async function _modelReadinessHydrate() {
  // Fire-and-forget single fetch. Renders the banner once and starts the
  // continuous poll only if a load is actually in flight (or has
  // errored). Cold + ready paths leave the banner hidden and skip the
  // background loop — saves a needless 4s/tick on freshly booted setups
  // where the model is already cached.
  try {
    const d = await api('GET', '/api/system/model-readiness');
    _renderModelReadinessBanner(d);
    if (_modelComponentActive(d?.embedder) || _modelComponentActive(d?.reranker)) {
      _modelReadinessPoll();
    }
  } catch (err) {
    console.warn('[model-readiness]', err);
  }
}

function _modelReadinessPoll() {
  if (STATE._modelReadinessPollHandle) return; // single-flight
  let ticks = 0;
  const tick = async () => {
    STATE._modelReadinessPollHandle = null;
    ticks += 1;
    try {
      const d = await api('GET', '/api/system/model-readiness');
      _renderModelReadinessBanner(d);
      if (_isModelPollTerminal(d)) return; // ready / skipped / error
    } catch (err) {
      // Transient fetch failure — keep last banner state and retry.
      console.warn('[model-readiness poll]', err);
    }
    if (ticks < _MODEL_READINESS_MAX_TICKS) {
      STATE._modelReadinessPollHandle = setTimeout(tick, _MODEL_READINESS_POLL_MS);
    }
  };
  STATE._modelReadinessPollHandle = setTimeout(tick, _MODEL_READINESS_POLL_MS);
}
function panelLoading(container) {
  // The ``sr-only`` span gives the decorative spinner a text alternative so
  // screen readers announce "Loading…" instead of silence. No ``aria-live`` on
  // the span: several callers render into containers that are already live
  // regions (e.g. ``#results-list``), and a nested live region double-announces.
  container.innerHTML =
    '<div class="loading-panel"><div class="spinner-panel"></div>'
    + `<span class="sr-only">${escapeHtml(t('common.loading'))}</span></div>`;
}
// sr-only "Loading…" text alternative for hand-rolled spinner markup that can't
// route through panelLoading() — bespoke .empty-state or bare .spinner-panel
// renders that keep their own wrapper for layout. Mirrors the span panelLoading()
// injects so every spinner has a screen-reader voice instead of silence (#1316).
function srLoading() {
  return `<span class="sr-only">${escapeHtml(t('common.loading'))}</span>`;
}

// ── A5: Empty State ──
// Owns its `.empty-state` wrapper — callers must NOT add their own.
// cta is structured as { href, label } for safe escaping.
function emptyState(icon, message, hint, cta) {
  const i = icon ? `<span class="empty-state-icon">${icon}</span>` : '';
  const h = hint ? `<span class="empty-state-hint">${escapeHtml(hint)}</span>` : '';
  const c = cta
    ? `<a class="empty-state-cta" href="${escapeHtml(cta.href)}" target="_blank" rel="noopener">${escapeHtml(cta.label)} →</a>`
    : '';
  return `<div class="empty-state">${i}<span>${escapeHtml(message)}</span>${h}${c}</div>`;
}

// ── A1: Toast Notifications ──
function showToast(message, type = 'success', options = {}) {
  // ``options.action`` ({ label, onClick }) renders an inline action
  // button next to the message — used by flows like Sources sub-toggle
  // "Switch view" where the toast both reports a result and offers a
  // one-tap follow-up. Action click runs the handler then dismisses
  // the toast; close button works as before.
  const container = qs('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  // Per-toast live semantics: errors are assertive (interrupt), everything
  // else is polite. ``#toast-container`` is intentionally NOT a live region —
  // wrapping these in a polite container would nest an assertive toast inside
  // a polite region and double-announce on insert.
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  const msgSpan = document.createElement('span');
  msgSpan.className = 'toast-msg';
  msgSpan.textContent = message;
  toast.appendChild(msgSpan);
  const delay = type === 'error' ? 5000 : 3000;
  let timer;
  function dismiss() {
    clearTimeout(timer);
    toast.classList.add('toast-out');
    toast.addEventListener('animationend', () => toast.remove(), { once: true });
  }
  if (options.action && options.action.label) {
    const actionBtn = document.createElement('button');
    actionBtn.type = 'button';
    actionBtn.className = 'toast-action';
    actionBtn.textContent = options.action.label;
    actionBtn.addEventListener('click', () => {
      try { options.action.onClick && options.action.onClick(); }
      finally { dismiss(); }
    });
    toast.appendChild(actionBtn);
  }
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'toast-close';
  closeBtn.title = 'Close';
  closeBtn.setAttribute('aria-label', (typeof I18N !== 'undefined' && I18N.t)
    ? I18N.t('toast.close_aria')
    : 'Close notification');
  closeBtn.textContent = '✕';
  closeBtn.addEventListener('click', dismiss);
  toast.appendChild(closeBtn);
  timer = setTimeout(dismiss, delay);
  container.appendChild(toast);
}

// ── A3: Confirm Dialog ──
// Returns ``boolean`` for the OK/Cancel choice. When ``extraOption`` is
// provided ({ id, label, defaultChecked }), the dialog renders an
// opt-in checkbox below the message and resolves to
// ``{ ok: boolean, extras: { [id]: boolean } }`` instead — callers that
// pass extras must handle the object shape. Existing callers without
// extras get the boolean shape unchanged.
function showConfirm({
  title,
  message = '',
  // Optional second line styled as a warning (e.g. "N files will be
  // overwritten") — hidden when absent so existing callers are unaffected.
  warningText = '',
  confirmText = t('common.confirm'),
  extraOption = null,
  // ``danger`` (default true) styles the OK button red. The shared dialog is
  // also used for non-destructive confirms (Sync / Sync-All / Import), which
  // pass ``danger: false`` so the most frequent gateway action isn't a red
  // "danger" button — red stays reserved for genuine deletes.
  danger = true,
  // Optional per-caller Cancel label (e.g. "Keep" on a destructive confirm).
  // ``null`` ⇒ the default ``modal.cancel_btn`` string. Driven on EVERY call
  // (see below) so a custom label can't leak into the next default confirm.
  cancelText = null,
}) {
  return new Promise(resolve => {
    const modal = qs('confirm-modal');
    qs('confirm-title').textContent = title;
    qs('confirm-message').textContent = message;
    const warningEl = qs('confirm-warning');
    warningEl.textContent = warningText || '';
    warningEl.hidden = !warningText;
    const okBtn = qs('confirm-ok-btn');
    okBtn.textContent = confirmText;
    okBtn.className = danger ? 'btn-danger' : 'btn-primary';
    // The OK button ships with ``data-i18n="modal.delete_btn"`` as static
    // markup; drop it so a langchange while the dialog is open can't re-apply
    // "Delete"/"삭제" over the caller's ``confirmText`` (e.g. "Sync"). The text
    // is always driven by ``confirmText`` here, so the attribute is dead weight.
    okBtn.removeAttribute('data-i18n');
    // Mirror the OK pattern for Cancel: drive the label from JS on EVERY call
    // (falling back to the default string) and drop the static ``data-i18n``.
    // Driving it unconditionally — not only when ``cancelText`` is passed — is
    // what stops a custom label from one confirm leaking into the next default
    // one (the button is a single reused element).
    const cancelBtn = qs('confirm-cancel-btn');
    cancelBtn.textContent = cancelText || t('modal.cancel_btn');
    cancelBtn.removeAttribute('data-i18n');

    const extraRow = qs('confirm-extra-row');
    const extraCheckbox = qs('confirm-extra-checkbox');
    const extraLabel = qs('confirm-extra-label');
    if (extraOption) {
      extraLabel.textContent = extraOption.label || '';
      extraCheckbox.checked = !!extraOption.defaultChecked;
      extraRow.hidden = false;
    } else {
      extraRow.hidden = true;
      extraCheckbox.checked = false;
    }

    show(modal);
    const focusables = [qs('confirm-cancel-btn'), qs('confirm-ok-btn')];
    // openModalA11y must run AFTER show() so previouslyFocused captures the
    // trigger (not confirm-ok-btn after focus() below). The Tab trap inside
    // showConfirm at onKey() below already handles Tab cycling — pass
    // focusables: null so the helper only adds restore + inert.
    const releaseA11y = openModalA11y(modal);
    focusables[1].focus();
    // Register a closer so the generic ESC dispatcher (line ~6119) routes
    // through cleanup() and the release() runs — without this, ESC on a
    // confirm modal would hide() the DOM but leak inert + lose focus.
    registerModalCloser(modal, () => cleanup(false));

    function cleanup(ok) {
      const extraChecked = extraOption && ok ? extraCheckbox.checked : false;
      hide(modal);
      releaseA11y();
      _MODAL_CLOSERS.delete(modal);
      // Always reset the checkbox row so a later non-extra confirm
      // doesn't inherit the previous label / checked state.
      extraRow.hidden = true;
      extraCheckbox.checked = false;
      // Same discipline for the warning line.
      warningEl.textContent = '';
      warningEl.hidden = true;
      modal.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      if (extraOption) {
        const extras = {};
        extras[extraOption.id] = extraChecked;
        resolve({ ok, extras });
      } else {
        resolve(ok);
      }
    }
    function onBackdrop(e) { if (e.target === modal) cleanup(false); }
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); cleanup(false); }
      if (e.key === 'Tab') {
        e.preventDefault();
        const idx = focusables.indexOf(document.activeElement);
        focusables[(idx + (e.shiftKey ? -1 : 1) + focusables.length) % focusables.length].focus();
      }
    }
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    qs('confirm-cancel-btn').onclick = () => cleanup(false);
    qs('confirm-ok-btn').onclick = () => cleanup(true);
  });
}

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------

function activateTab(tabName, opts = {}) {
  // ``fromKeyboard`` flips two behaviors that would otherwise fight the
  // ARIA tabs arrow-nav contract:
  //   * panel auto-focus is skipped so focus stays on the tab button (the
  //     user keeps ArrowRight'ing through siblings)
  //   * history mutation uses replaceState so cycling N tabs does not push
  //     N entries the user has to press Back through to escape
  const fromKeyboard = opts.fromKeyboard === true;
  // Redirect to the first visible tab when the target is hidden by EITHER
  // visibility axis (dev-only in prod, or advanced-only in Simple mode) instead
  // of showing a panel whose tab button is off-screen. This single guard catches
  // every programmatic caller (Home heatmap/Tags quick-actions, Cmd+K palette,
  // number keys, g-prefix, a stale saved default) — the post-guard loaders never
  // fire for a hidden tab, so no orphaned panel + no strand.
  const targetBtn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
  if (targetBtn) {
    const visible = _visibleMainTabs();
    if (!visible.includes(tabName)) {
      const devHidden = targetBtn.dataset.uiTier === 'dev' && STATE.uiMode !== 'dev';
      showToast(t(devHidden ? 'toast.dev_only_section' : 'toast.advanced_only_section'), 'info');
      if (visible.length && visible[0] !== tabName) {
        activateTab(visible[0], opts);
      }
      return;
    }
  }

  // Deactivate all main tabs. ``tabindex=-1`` keeps non-current tabs out of
  // the Tab key sequence so keyboard users land on the active tab once and
  // arrow-key between siblings (single-tab-stop pattern from the ARIA tabs
  // spec). The currently active tab gets ``tabindex=0`` below.
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.remove('active');
    b.setAttribute('aria-selected', 'false');
    b.setAttribute('tabindex', '-1');
  });

  // Re-apply `hidden` so DOM state matches CSS — without this, visited panels
  // leak into a11y / Playwright snapshots (#699).
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.remove('active');
    p.hidden = true;
  });

  // Activate the correct button
  const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
  if (btn) {
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    btn.setAttribute('tabindex', '0');
    _centerActiveMainTab(btn);
  }

  // Show panel
  const panel = qs(`tab-${tabName}`);
  if (panel) {
    panel.hidden = false;
    panel.classList.add('active');
    // Focus first focusable element in new panel — but only on click /
    // direct activation. Arrow-nav must keep focus on the tab button so
    // subsequent ArrowRight presses still cycle the tablist.
    if (!fromKeyboard) {
      const focusable = panel.querySelector('input:not([hidden]):not([disabled]), button:not([hidden]):not([disabled]), [tabindex="0"]');
      if (focusable) focusable.focus();
    }
  }

  // History API — enable back button and deep linking. Arrow-nav uses
  // replaceState so a cycle through siblings produces one history entry
  // instead of N (otherwise Back becomes unusable).
  if (location.hash !== `#${tabName}`) {
    if (fromKeyboard) {
      history.replaceState({ tab: tabName }, '', `#${tabName}`);
    } else {
      history.pushState({ tab: tabName }, '', `#${tabName}`);
    }
  }

  // Tab-specific loads
  if (tabName === 'home') { STATE.homeStale = false; loadDashboard(); renderPinnedSection(); }
  if (tabName === 'sources') {
    STATE.sourcesBrowserStale = false;
    // Single Sources panel: pull memory-dirs status + indexed sources
    // in one round trip. Vendor grouping is the only classification
    // axis now, so there's no mode to restore.
    loadSources();
  }
  loadStats();
  if (tabName === 'tags') { STATE.tagsTabStale = false; loadTags(); }
  if (tabName === 'timeline') loadTimeline();
  if (tabName === 'settings') {
    let start = STATE.lastSettingsSection;
    if (!start) {
      try { start = localStorage.getItem(LAST_SECTION_KEY); } catch {}
    }
    // Gateway-tab sections live under ``#tab-context-gateway`` now; if the
    // last-section memory points at one of them, fall back to a settings-tab
    // section so activating Settings does not silently re-route the user
    // back to Gateway via the redirect below.
    if (GATEWAY_SECTIONS.has(start)) start = 'config';
    switchSettingsSection(start || 'config');
  }
  if (tabName === 'context-gateway') {
    let start = opts.sectionOverride;
    // #1070: prefer the deep-link ``?section=`` over remembered state so a
    // cold-loaded share-URL routes to the target leaf instead of landing on
    // Overview. ``sectionOverride`` (set by ``switchSettingsSection``'s
    // re-entry hop) still wins so an in-page click is not overridden by a
    // stale URL param. ``_ctxParseDeepLink`` is declared at module scope in
    // ``context-gateway.js`` and is on ``window`` by the time the
    // DOMContentLoaded handler fires ``activateTab`` — fall back gracefully
    // if it isn't (e.g., script-load ordering regression).
    if (!start && typeof _ctxParseDeepLink === 'function') {
      const link = _ctxParseDeepLink();
      if (link && GATEWAY_SECTIONS.has(link.section)) start = link.section;
    }
    if (!start) start = STATE.lastSettingsSection;
    if (!start) {
      try { start = localStorage.getItem(LAST_SECTION_KEY); } catch {}
    }
    // rank 2/20: the Overview is now a true aggregate dashboard (sync status +
    // Sync-All + cross-project tiles, no per-project roster), so a cold visit
    // lands there rather than on the full Projects roster. Returning users
    // still resume their last-viewed section via the localStorage branch above.
    if (!GATEWAY_SECTIONS.has(start)) start = 'ctx-overview';
    switchSettingsSection(start);
    // #1417: refresh the Wiki nav dirty dot whenever the gateway opens, so a
    // cold reload landing on any section (not just ctx-wiki) still flags a wiki
    // left with uncommitted edits. Cheap + best-effort — wiki.js no-ops on an
    // absent wiki or fetch error. Guarded for script-load order (wiki.js loads
    // after app.js).
    if (typeof _probeWikiNavStatus === 'function') _probeWikiNavStatus();
  }
  if (['search', 'timeline'].includes(tabName)) loadNamespaceDropdowns();
}

function _centerActiveMainTab(btn) {
  if (!btn || typeof btn.scrollIntoView !== 'function') return;
  requestAnimationFrame(() => {
    btn.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
  });
}

// Settings Hub section switching

const NAV_COLLAPSE_KEY = 'memtomem_nav_collapsed';
const LAST_SECTION_KEY = 'memtomem_last_settings';
const DEFAULT_NAV_COLLAPSED = { general: false, integrations: false, runtime: true, data: true };
// Deep-link redirects for renamed/removed sections.
const LEGACY_SECTION_MAP = { 'harness-watchdog': 'harness-health' };
// Sections that now live under the top-level Context Gateway tab (#962).
// ``switchSettingsSection`` routes these to ``#tab-context-gateway`` so
// every legacy caller — overview tile clicks, Sync All "open settings"
// CTA, settings-namespaces.js Quick Links — auto-redirects without
// per-call-site updates.
const GATEWAY_SECTIONS = new Set([
  'ctx-overview', 'ctx-projects', 'ctx-skills', 'ctx-commands', 'ctx-agents', 'ctx-mcp-servers',
  'hooks-sync',
  'ctx-wiki',
]);

function loadNavCollapseState() {
  try {
    const raw = localStorage.getItem(NAV_COLLAPSE_KEY);
    return { ...DEFAULT_NAV_COLLAPSED, ...(raw ? JSON.parse(raw) : {}) };
  } catch {
    return { ...DEFAULT_NAV_COLLAPSED };
  }
}

function saveNavCollapseState() {
  try { localStorage.setItem(NAV_COLLAPSE_KEY, JSON.stringify(STATE.settingsNavCollapsed)); } catch {}
}

function applyNavCollapseState() {
  const state = STATE.settingsNavCollapsed || DEFAULT_NAV_COLLAPSED;
  document.querySelectorAll('.settings-nav-group[data-group]').forEach(groupBtn => {
    if (groupBtn.classList.contains('settings-nav-group--danger')) return;
    const groupId = groupBtn.dataset.group;
    const collapsed = !!state[groupId];
    groupBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    const caret = groupBtn.querySelector('.nav-group-caret');
    if (caret) caret.textContent = collapsed ? '▸' : '▾';
  });
  document.querySelectorAll('.settings-nav-btn[data-group]').forEach(btn => {
    const groupId = btn.dataset.group;
    if (groupId === 'danger') return;
    btn.classList.toggle('collapsed-member', !!state[groupId]);
  });
  document.querySelectorAll('.settings-nav-divider[data-group]').forEach(divider => {
    divider.classList.toggle('collapsed-member', !!state[divider.dataset.group]);
  });
}

function toggleNavGroup(groupId) {
  if (!STATE.settingsNavCollapsed) STATE.settingsNavCollapsed = loadNavCollapseState();
  STATE.settingsNavCollapsed[groupId] = !STATE.settingsNavCollapsed[groupId];
  saveNavCollapseState();
  applyNavCollapseState();
}

function ensureActiveGroupExpanded(section) {
  const btn = document.querySelector(`.settings-nav-btn[data-section="${section}"]`);
  if (!btn) return;
  const groupId = btn.dataset.group;
  if (!groupId || groupId === 'danger') return;
  if (!STATE.settingsNavCollapsed) STATE.settingsNavCollapsed = loadNavCollapseState();
  if (STATE.settingsNavCollapsed[groupId]) {
    STATE.settingsNavCollapsed[groupId] = false;
    saveNavCollapseState();
    applyNavCollapseState();
  }
}

function switchSettingsSection(sectionName) {
  sectionName = LEGACY_SECTION_MAP[sectionName] || sectionName;
  // Gateway-tab sections live under ``#tab-context-gateway`` (#962). If
  // the user is currently on a different main tab, hop into the Gateway
  // tab first; activateTab will re-enter this function via the
  // ``sectionOverride`` argument. Guard prevents the infinite recursion
  // that would otherwise fire when called from activateTab itself.
  if (GATEWAY_SECTIONS.has(sectionName)) {
    const currentMainTab = document.querySelector('.tab-btn.active');
    if (!currentMainTab || currentMainTab.dataset.tab !== 'context-gateway') {
      activateTab('context-gateway', { sectionOverride: sectionName });
      return;
    }
  }
  // Redirect to the first visible section when the target is hidden by EITHER
  // axis (dev-only in prod, or the advanced Data group in Simple mode) — the
  // section's loader (loadConfig / resetDedupPanel / …) must not run for a
  // demoted section reached via a Home quick-action or the Cmd+K palette.
  const targetBtn = document.querySelector(
    `.settings-nav-btn[data-section="${sectionName}"]`,
  );
  if (targetBtn) {
    const visible = _visibleSettingsSections();
    if (!visible.includes(sectionName)) {
      const devHidden = targetBtn.dataset.uiTier === 'dev' && STATE.uiMode !== 'dev';
      showToast(t(devHidden ? 'toast.dev_only_section' : 'toast.advanced_only_section'), 'info');
      // Fall back to the first visible *Settings-tab* section (config, …), never
      // a Gateway sidebar section: ctx-overview & friends share .settings-nav-btn
      // but sit earlier in the DOM, so a bare visible[0] would yank the user to
      // the Gateway tab instead of Settings → Config (Codex review).
      const fallback = visible.find(s => !GATEWAY_SECTIONS.has(s));
      if (fallback && fallback !== sectionName) {
        switchSettingsSection(fallback);
      }
      return;
    }
  }
  STATE.lastSettingsSection = sectionName;
  try { localStorage.setItem(LAST_SECTION_KEY, sectionName); } catch {}
  document.querySelectorAll('.settings-nav-btn').forEach(b => {
    b.classList.remove('active');
    // ``.settings-nav`` is a navigation list (role-less <nav>), not a tablist
    // (rank 14): the group headers are interactive collapse toggles, which a
    // strict ARIA tablist may not contain. Mark the active entry with
    // ``aria-current="page"`` — the standard nav-selection cue — instead of
    // ``aria-selected``, which only belongs on a role=tab.
    b.removeAttribute('aria-current');
  });
  document.querySelectorAll('.settings-section').forEach(s => s.classList.remove('active'));
  const btn = document.querySelector(`.settings-nav-btn[data-section="${sectionName}"]`);
  const section = document.getElementById(`settings-${sectionName}`);
  if (btn) {
    btn.classList.add('active');
    btn.setAttribute('aria-current', 'page');
  }
  if (section) section.classList.add('active');
  ensureActiveGroupExpanded(sectionName);
  // rank 11: repaint the persistent gateway control bar synchronously on every
  // gateway section switch. Its visibility (hidden on the Projects portal) and
  // selection must update the instant the section flips — the async loader below
  // also repaints it post-fetch, but this synchronous call avoids a stale or
  // wrong-section bar in the interim (and hides it immediately on ctx-projects,
  // which has no loader that touches the bar). Guarded for non-gateway sections
  // and for script load order (context-gateway.js defines _ctxRenderControlBar).
  if (GATEWAY_SECTIONS.has(sectionName) && typeof _ctxRenderControlBar === 'function') {
    _ctxRenderControlBar();
  }
  // Section-specific loads (reuse existing functions)
  if (sectionName === 'config') loadConfig();
  if (sectionName === 'namespaces') loadNamespacesTab();
  if (sectionName === 'dedup') resetDedupPanel();
  if (sectionName === 'decay') resetDecayPanel();
  if (sectionName === 'export') { resetExportPanel(); loadNamespaceDropdowns(); }
  if (sectionName === 'harness-sessions') loadHarnessSessions();
  if (sectionName === 'harness-scratch') loadHarnessScratch();
  if (sectionName === 'harness-procedures') loadHarnessProcedures();
  if (sectionName === 'harness-health') { loadHarnessHealth(); loadWatchdogStatus(); }
  if (sectionName === 'hooks-sync') loadHooksSync();
  if (sectionName === 'ctx-overview') loadCtxOverview();
  if (sectionName === 'ctx-projects') loadCtxProjects();
  if (sectionName === 'ctx-skills') loadCtxList('skills');
  if (sectionName === 'ctx-commands') loadCtxList('commands');
  if (sectionName === 'ctx-agents') loadCtxList('agents');
  if (sectionName === 'ctx-mcp-servers') loadCtxList('mcp-servers');
  // ctx-wiki is a GLOBAL surface (the ~/.memtomem-wiki repo), not project-scoped:
  // it is deliberately absent from _CTX_SECTION_BAR_TYPE so the project/tier
  // control bar hides (like ctx-projects), and dispatches to its own loader
  // rather than loadCtxList. See wiki.js (ADR-0008 PR-E).
  if (sectionName === 'ctx-wiki' && typeof loadWiki === 'function') loadWiki();
}

// Settings nav buttons
document.querySelectorAll('.settings-nav-btn').forEach(btn => {
  btn.addEventListener('click', () => switchSettingsSection(btn.dataset.section));
});

// Settings nav group buttons (expand/collapse)
document.querySelectorAll('.settings-nav-group[data-group]').forEach(grp => {
  if (grp.classList.contains('settings-nav-group--danger')) return;
  grp.addEventListener('click', () => toggleNavGroup(grp.dataset.group));
});

// Initialize collapse state from localStorage
STATE.settingsNavCollapsed = loadNavCollapseState();
applyNavCollapseState();

// Main tab buttons
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});

// ── ARIA tabs keyboard navigation ──
//
// ArrowRight/Left cycle through the tablist, Home/End jump to either end.
// "Auto-activation" model — focus and activate together — matches the click
// behavior so a keyboard user toggles the panel just by walking the tab row,
// without an extra Enter press. The currently focused element is the anchor;
// when focus is outside the tablist the move starts at index 0.
function _arrowNavIndex(length, currentIdx, key) {
  if (!length) return -1;
  if (key === 'ArrowRight') return (currentIdx + 1) % length;
  if (key === 'ArrowLeft') return (currentIdx - 1 + length) % length;
  if (key === 'Home') return 0;
  if (key === 'End') return length - 1;
  return -1;
}

document.querySelector('.tab-nav')?.addEventListener('keydown', (e) => {
  if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
  // Walk only the tabs the user actually sees — otherwise ArrowRight/Left could
  // land focus on (and activate) a tab hidden by either axis (dev-only in prod,
  // advanced-only in Simple). Reuse the shared predicate so this never diverges.
  const visible = new Set(_visibleMainTabs());
  const buttons = Array.from(document.querySelectorAll('.tab-nav .tab-btn'))
    .filter(b => visible.has(b.dataset.tab));
  const currentIdx = buttons.indexOf(document.activeElement);
  const nextIdx = _arrowNavIndex(buttons.length, currentIdx === -1 ? 0 : currentIdx, e.key);
  if (nextIdx < 0) return;
  e.preventDefault();
  const next = buttons[nextIdx];
  next.focus();
  if (next.dataset.tab) activateTab(next.dataset.tab, { fromKeyboard: true });
});

// ── E1: ARIA init ──
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.setAttribute('role', 'tab');
  btn.setAttribute('aria-controls', `tab-${btn.dataset.tab}`);
  btn.setAttribute('aria-selected', btn.classList.contains('active') ? 'true' : 'false');
});
document.querySelectorAll('.tab-panel').forEach(p => p.setAttribute('role', 'tabpanel'));
document.querySelector('.tab-nav').setAttribute('role', 'tablist');

// ── C3: Theme toggle ──
qs('theme-toggle').addEventListener('click', () => {
  const el = document.documentElement;
  const goLight = el.getAttribute('data-theme') !== 'light';
  el.setAttribute('data-theme', goLight ? 'light' : 'dark');
  qs('theme-toggle').textContent = goLight ? '☀️' : '🌙';
  localStorage.setItem('m2m-theme', goLight ? 'light' : 'dark');
});

// ── App-level Simple / Advanced toggle (S2.2) ──
document.getElementById('app-mode-toggle')?.addEventListener('click', () => {
  _setAppSimple(!_appSimple);
  // Flipping INTO Simple can hide the tab/section the user is currently on —
  // bounce to the first still-visible one so the panel + its nav cue never
  // vanish from under them. (Flipping into Advanced only reveals, nothing to do.)
  if (!_appSimple) return;
  const activeTab = document.querySelector('.tab-btn.active');
  const visibleTabs = _visibleMainTabs();
  if (activeTab && !visibleTabs.includes(activeTab.dataset.tab)) {
    activateTab(visibleTabs[0] || 'home');
    return;
  }
  // On the Settings tab, the active *section* may be the one that just got
  // demoted (Data group) — hop to the first visible section in place.
  if (activeTab?.dataset.tab === 'settings') {
    const activeSection = document.querySelector('.settings-nav-btn.active');
    const visibleSections = _visibleSettingsSections();
    if (activeSection && !visibleSections.includes(activeSection.dataset.section)) {
      // First visible Settings-tab section (skip Gateway sidebar sections, which
      // would bounce to the Gateway tab — see switchSettingsSection's note).
      switchSettingsSection(visibleSections.find(s => !GATEWAY_SECTIONS.has(s)) || 'config');
    }
  }
});

// ── C1: Mobile back button ──
qs('mobile-back-btn').addEventListener('click', () => {
  document.querySelector('.results-layout').classList.remove('mobile-detail');
});

// ── History API: back/forward navigation + hash deep link ──
window.addEventListener('popstate', (e) => {
  const tab = e.state?.tab;
  // Gate on current visibility (mirrors the boot-hash dispatch): a history
  // entry pushed while Tags/Timeline were visible must not replay
  // activateTab('timeline') after the user flipped to Simple — that would strand
  // them on a panel with no active tab button. Staying put is the safe floor.
  if (tab && _visibleMainTabs().includes(tab)) activateTab(tab);
});
// Note: initial hash-based activateTab dispatch moved into the i18n init
// handler above so ``t()``-backed JS widgets (Sources tab's Memory Dirs
// panel) render with translated strings instead of raw keys.

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

async function loadStats() {
  try {
    const data = await api('GET', '/api/stats');
    const chunksEl = qs('stat-chunks');
    const sourcesEl = qs('stat-sources');
    chunksEl.removeAttribute('data-i18n');
    sourcesEl.removeAttribute('data-i18n');
    chunksEl.textContent = t(
      data.total_chunks === 1 ? 'header.stat_chunks_count_one' : 'header.stat_chunks_count_other',
      { count: data.total_chunks },
    );
    sourcesEl.textContent = t(
      data.total_sources === 1 ? 'header.stat_sources_count_one' : 'header.stat_sources_count_other',
      { count: data.total_sources },
    );
  } catch (e) { console.warn('[stats]', e); }
}

// Registered at top level — mirrors the ``_updateFilterCountBadge``
// langchange hook below. Must be registered before ``I18N.init()`` runs
// (init dispatches a one-shot langchange after the locale cache loads),
// so the placeholder data-i18n value applyDOM() writes into the count
// pills gets immediately overwritten by the live count in the new
// language. If we registered this inside the DOMContentLoaded handler
// after ``await I18N.init()``, init's own dispatch would fire before
// the listener exists and the pills would stay on "— chunks" until the
// next user-driven toggle.
window.addEventListener('langchange', () => {
  if (typeof I18N !== 'undefined') I18N.applyDOM();
  loadStats();
  if (qs('tab-home') && !qs('tab-home').hidden) loadDashboard();
  // checkEmbeddingMismatch() fires at module load and can win the race
  // against I18N.init(), so re-render the banner text once the locale
  // cache is ready (and on every later toggle). No-op if no mismatch is
  // showing. See feedback_i18n_init_order_race.
  renderEmbMismatchBanner();
  // NOTE: search-results / chunk-browser microcopy keyed in S1.3 is rendered
  // imperatively via t() and localizes on the next render, not on a live
  // language toggle. A safe live repaint needs per-surface state preservation
  // (bulk selection, selected result, unsaved editor drafts, chunk-browser
  // edit state) and spans several stateful surfaces, so it is deferred to a
  // dedicated pass rather than risking draft/selection loss here. This matches
  // the pre-existing behavior of the other imperatively-rendered search nodes.
});
loadStats();
checkEmbeddingMismatch();

// ---------------------------------------------------------------------------
// Embedding-mismatch banner (localized via banner.emb_mismatch* locale keys)
// ---------------------------------------------------------------------------

// Cached ``/api/embedding-status`` payload, kept so the banner text can be
// re-rendered in the current locale on ``langchange``. The initial
// checkEmbeddingMismatch() runs at module load and may resolve before
// I18N.init() populates the locale cache; without a re-render the banner
// would show raw ``banner.emb_mismatch*`` keys. See
// feedback_i18n_init_order_race.
let _embMismatchData = null;

/** Build the banner message from the cached payload using the current
 *  locale. Safe to call repeatedly (langchange); no-op when no mismatch
 *  is showing or the banner DOM isn't present. */
function renderEmbMismatchBanner() {
  if (!_embMismatchData) return;
  const msgEl = qs('emb-banner-msg');
  if (!msgEl) return;
  const data = _embMismatchData;
  const parts = [];
  if (data.dimension_mismatch) {
    parts.push(t('banner.emb_mismatch_dimension', {
      db: data.stored.dimension,
      config: data.configured.dimension,
    }));
  }
  if (data.model_mismatch) {
    parts.push(t('banner.emb_mismatch_model', {
      db: `${data.stored.provider}/${data.stored.model}`,
      config: `${data.configured.provider}/${data.configured.model}`,
    }));
  }
  msgEl.textContent = t('banner.emb_mismatch', { details: parts.join(' / ') });
}

async function checkEmbeddingMismatch() {
  try {
    const data = await api('GET', '/api/embedding-status');
    if (!data.has_mismatch) return;

    // Session dismiss — only show once per session
    if (sessionStorage.getItem('m2m-emb-banner-dismissed')) return;

    const banner = qs('embedding-mismatch-banner');

    // Cache + render now; re-rendered on langchange if init wins the race.
    _embMismatchData = data;
    renderEmbMismatchBanner();
    show(banner);

    // Dismiss button
    const dismissBtn = banner.querySelector('.emb-banner-dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', () => {
        hide(banner);
        sessionStorage.setItem('m2m-emb-banner-dismissed', '1');
      }, { once: true });
    }

    qs('emb-reset-btn').addEventListener('click', async () => {
      const warned = await showConfirm({
        title: t('confirm.emb_reset_title'),
        message: t('confirm.emb_reset_msg', {
          provider: data.configured.provider,
          model: data.configured.model,
          dimension: data.configured.dimension,
        }),
        confirmText: t('confirm.emb_reset_btn'),
      });
      if (!warned) return;
      try {
        const res = await api('POST', '/api/embedding-reset', undefined, { timeout: 120_000 });
        hide(banner);
        sessionStorage.removeItem('m2m-emb-banner-dismissed');
        await fetchServerConfig();
        showToast(res.message, 'success');
      } catch (err) {
        showToast(t('toast.reset_failed', { error: err.message }), 'error');
      }
    }, { once: true });
  } catch (e) { console.warn('[emb-check]', e); }
}

// ---------------------------------------------------------------------------
// Home Dashboard (D3)
// ---------------------------------------------------------------------------

function _homeInlineState(key, tone = 'muted') {
  const color = tone === 'danger' ? 'var(--danger)' : 'var(--muted)';
  return `<span style="color:${color};font-size:0.78rem">${escapeHtml(t(key))}</span>`;
}

function _setHomeStatPlaceholders() {
  ['home-chunks', 'home-sources', 'home-namespaces', 'home-total-size', 'home-sessions', 'home-scratch']
    .forEach(id => {
      const el = qs(id);
      if (el) el.textContent = '—';
    });
}

function _setHomeDashboardLoading() {
  _setHomeStatPlaceholders();
  ['home-activity-map', 'home-type-chart', 'home-ns-chart', 'home-chunk-dist', 'home-recent-list', 'home-health-info']
    .forEach(id => {
      const el = qs(id);
      if (el) {
        el.innerHTML = `<div class="loading-panel" aria-label="${escapeAttr(t('home.state.loading'))}"><div class="spinner-panel"></div></div>`;
      }
    });
  renderPinnedSection();
}

function _renderHomeDashboardError(err) {
  _setHomeStatPlaceholders();
  const detail = err && err.message ? escapeHtml(err.message) : '';
  ['home-activity-map', 'home-type-chart', 'home-ns-chart', 'home-chunk-dist', 'home-health-info']
    .forEach(id => {
      const el = qs(id);
      if (el) el.innerHTML = _homeInlineState('home.state.load_failed', 'danger');
    });
  const recentList = qs('home-recent-list');
  if (recentList) {
    recentList.innerHTML = emptyState('⚠', t('home.state.load_failed'), detail);
  }
  renderPinnedSection();
}

async function loadDashboard() {
  _setHomeDashboardLoading();
  try {
    // /api/sessions and /api/scratch are dev-only — gated below. The
    // namespaces list endpoint is prod-mounted via namespaces_read so
    // the donut + count card render real values in both tiers.
    const [stats, nsData, configData, embStatus, timelineData, memDirsResp] = await Promise.all([
      api('GET', '/api/stats'),
      api('GET', '/api/namespaces').catch(() => ({ namespaces: [] })),
      api('GET', '/api/config'),
      api('GET', '/api/embedding-status').catch(() => null),
      api('GET', `/api/timeline?days=365&limit=${HOME_ACTIVITY_TIMELINE_LIMIT}`).catch(() => ({ chunks: [] })),
      api('GET', '/api/memory-dirs/status').catch(() => ({ dirs: [] })),
    ]);

    const allSources = Array.isArray(stats.home_sources) ? stats.home_sources : [];
    const recentSources = Array.isArray(stats.home_recent_sources) && stats.home_recent_sources.length
      ? stats.home_recent_sources
      : allSources;
    const sourceTypeCounts = Array.isArray(stats.home_file_type_distribution)
      ? stats.home_file_type_distribution
      : null;
    const namespaces = nsData.namespaces || [];

    // Mirror onto STATE so a Home → recent-source click can resolve
    // the target vendor sub-tab before activating the Sources tab.
    // The dashboard now uses backend aggregates for complete counts and
    // distributions, so this snapshot reflects all visible sources.
    STATE.allSources = allSources;
    const _memStatusByPath = {};
    for (const entry of (memDirsResp && memDirsResp.dirs) || []) {
      if (entry && typeof entry.path === 'string') _memStatusByPath[entry.path] = entry;
    }
    STATE.memoryStatusByPath = _memStatusByPath;

    // A. Stats cards
    qs('home-chunks').textContent = Number(stats.total_chunks || 0).toLocaleString();
    qs('home-sources').textContent = Number(allSources.length || stats.total_sources || 0).toLocaleString();
    qs('home-namespaces').textContent = namespaces.length;
    const totalSize = Number(
      typeof stats.home_total_source_size === 'number'
        ? stats.home_total_source_size
        : allSources.reduce((sum, s) => sum + (s.file_size || 0), 0)
    );
    qs('home-total-size').textContent = formatBytes(totalSize) || '0 B';

    // Harness stats (sessions + scratch) — dev-only routers. In prod we
    // render zeroes rather than fire 404s on every Home render.
    try {
      let sessData = { total: 0 };
      let scratchData = { total: 0 };
      if (STATE.uiMode === 'dev') {
        [sessData, scratchData] = await Promise.all([
          api('GET', '/api/sessions?limit=1').catch(() => ({ total: 0 })),
          api('GET', '/api/scratch').catch(() => ({ total: 0 })),
        ]);
      }
      qs('home-sessions').textContent = sessData.total;
      qs('home-scratch').textContent = scratchData.total;
    } catch { /* non-critical */ }

    // B. Activity Heatmap (GitHub contribution graph)
    const timelineChunks = timelineData.chunks || [];
    _renderActivityMap(timelineChunks, {
      isSample: timelineData.has_more === true,
      sampleLimit: HOME_ACTIVITY_TIMELINE_LIMIT,
    });

    // D. File Type Distribution
    _renderFileTypeChart(allSources, sourceTypeCounts);

    // G. Namespace Summary
    _renderNsChart(namespaces);

    // Chunk Size Distribution
    _renderChunkDist(stats.chunk_size_distribution || []);

    // E. Recent Sources (improved)
    _renderHomeRecent(recentSources);

    // H. Storage Health — use DB-stored values when available
    _renderStorageHealth(configData, allSources, embStatus);

    // Pinned chunks
    renderPinnedSection();
  } catch (err) {
    _renderHomeDashboardError(err);
  }
}

// B. Activity Heatmap — GitHub contribution graph (1 year)
function _renderActivityMap(chunks, options) {
  options = options || {};
  const map = qs('home-activity-map');
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const tr = (key, params, fallback) => {
    if (typeof t !== 'function') return fallback;
    const translated = t(key, params);
    return translated === key ? fallback : translated;
  };

  // Count chunks per date
  const countByDate = {};
  chunks.forEach(c => {
    if (!c.created_at) return;
    const key = localDateKey(c.created_at);
    countByDate[key] = (countByDate[key] || 0) + 1;
  });

  const compactRange = !!(window.matchMedia && window.matchMedia('(max-width: 430px)').matches);
  const rangeDays = compactRange ? 90 : 364;

  // Start from Sunday before the visible range so the grid aligns by week.
  const startDate = new Date(today);
  startDate.setDate(startDate.getDate() - rangeDays - startDate.getDay());
  // Compute working range and ignore the leading week-alignment padding.
  const dataStart = new Date(today);
  dataStart.setDate(dataStart.getDate() - rangeDays);

  const totalDays = Math.round((today - startDate) / 86400000) + 1;
  const cells = [];
  let maxCount = 0;
  let totalCount = 0;
  let activeDays = 0;
  let last7Count = 0;
  let mostActive = null;
  const last7Start = new Date(today);
  last7Start.setDate(last7Start.getDate() - 6);

  for (let i = 0; i < totalDays; i++) {
    const d = new Date(startDate);
    d.setDate(d.getDate() + i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const count = countByDate[key] || 0;
    const isFuture = d > today;
    const isBeforeRange = d < dataStart;
    if (!isFuture && !isBeforeRange && count > maxCount) maxCount = count;
    if (!isFuture && !isBeforeRange) {
      totalCount += count;
      if (d >= last7Start) last7Count += count;
      if (count > 0) {
        activeDays += 1;
        if (!mostActive || count > mostActive.count) mostActive = { date: key, count };
      }
    }
    cells.push({ date: key, count, isFuture, isBeforeRange, month: d.getMonth() });
  }

  // Quartile-based levels (like GitHub)
  const getLevel = (count) => {
    if (count === 0 || maxCount === 0) return 0;
    const q = count / maxCount;
    if (q <= 0.25) return 1;
    if (q <= 0.50) return 2;
    if (q <= 0.75) return 3;
    return 4;
  };
  const levelLabel = (level) => {
    const labels = [
      ['home.activity.intensity_none', 'No activity'],
      ['home.activity.intensity_low', 'Low activity'],
      ['home.activity.intensity_medium', 'Medium activity'],
      ['home.activity.intensity_high', 'High activity'],
      ['home.activity.intensity_peak', 'Peak activity'],
    ];
    const [key, fallback] = labels[level] || labels[0];
    return tr(key, {}, fallback);
  };

  // Month labels — detect when a new month starts in the first row (Sunday)
  const numWeeks = Math.ceil(totalDays / 7);
  const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const weekdayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const monthLabels = [];
  let prevMonth = -1;
  for (let w = 0; w < numWeeks; w++) {
    const idx = w * 7;
    if (idx < cells.length) {
      const m = cells[idx].month;
      if (m !== prevMonth) {
        monthLabels.push({ col: w + 1, label: monthNames[m] });
        prevMonth = m;
      }
    }
  }

  let html = '';
  const mostActiveCount = mostActive ? mostActive.count : 0;
  const mostActiveDate = mostActive ? mostActive.date : tr(
    'home.activity.none',
    {},
    'None',
  );
  const isSample = Boolean(options.isSample);
  const sampleLimit = Number(options.sampleLimit || chunks.length || 0);
  const summaryAria = isSample
    ? tr(
      'home.activity.summary_sample_aria',
      { limit: sampleLimit, active: activeDays, date: mostActiveDate, count: mostActiveCount },
      `Activity map is based on the ${sampleLimit} most recent chunks returned by the timeline query. Sample covers ${activeDays} active days. Most active sampled day: ${mostActiveDate}, ${mostActiveCount} chunks.`,
    )
    : tr(
      'home.activity.summary_aria',
      { total: totalCount, active: activeDays, last7: last7Count, date: mostActiveDate, count: mostActiveCount },
      `${totalCount} chunks across ${activeDays} active days. ${last7Count} chunks in the last 7 days. Most active day: ${mostActiveDate}, ${mostActiveCount} chunks.`,
    );
  html += `<div class="activity-summary${isSample ? ' activity-summary-sample' : ''}" aria-label="${escapeAttr(summaryAria)}">`;
  if (isSample) {
    html += `<span>${escapeHtml(tr('home.activity.summary_sample', { count: chunks.length }, `Recent sample: ${chunks.length}`))}</span>`;
    html += `<span>${escapeHtml(tr('home.activity.summary_sample_active_days', { count: activeDays }, `Sample active days: ${activeDays}`))}</span>`;
    html += `<span>${escapeHtml(tr('home.activity.summary_sample_most_active', { date: mostActiveDate, count: mostActiveCount }, `Sample most active: ${mostActiveDate} (${mostActiveCount})`))}</span>`;
  } else {
    html += `<span>${escapeHtml(tr('home.activity.summary_last7', { count: last7Count }, `Last 7 days: ${last7Count}`))}</span>`;
    html += `<span>${escapeHtml(tr('home.activity.summary_active_days', { count: activeDays }, `Active days: ${activeDays}`))}</span>`;
    html += `<span>${escapeHtml(tr('home.activity.summary_most_active', { date: mostActiveDate, count: mostActiveCount }, `Most active: ${mostActiveDate} (${mostActiveCount})`))}</span>`;
  }
  html += '</div>';

  // Month labels row
  html += '<div class="activity-map-frame">';
  html += '<div class="activity-weekday-spacer" aria-hidden="true"></div>';
  html += '<div class="activity-months" style="grid-template-columns:repeat(' + numWeeks + ',1fr);gap:2px">';
  monthLabels.forEach(m => { html += `<span style="grid-column:${m.col}">${m.label}</span>`; });
  html += '</div>';
  html += '<div class="activity-weekdays" aria-hidden="true">';
  weekdayNames.forEach(day => { html += `<span>${day}</span>`; });
  html += '</div>';

  // Grid of cells (7 rows x N cols, auto-flow column = weeks). Only dates
  // with activity become buttons, keeping keyboard navigation useful.
  html += `<div class="activity-grid" style="grid-template-columns:repeat(${numWeeks},1fr)" aria-label="${escapeAttr(summaryAria)}">`;
  cells.forEach(cell => {
    if (cell.isFuture || cell.isBeforeRange) {
      html += '<div class="activity-cell activity-empty"></div>';
    } else {
      const level = getLevel(cell.count);
      const intensity = levelLabel(level);
      const ariaKey = cell.count === 1
        ? 'home.activity.cell_aria_one'
        : 'home.activity.cell_aria_other';
      const ariaRaw = tr(
        ariaKey,
        { date: cell.date, count: cell.count, intensity },
        `${cell.date}, ${cell.count} chunks, ${intensity}. Click to view in Timeline.`,
      );
      const title = tr(
        'home.activity.cell_title',
        { date: cell.date, count: cell.count, intensity },
        `${cell.date}: ${cell.count} chunks, ${intensity}`,
      );
      const isInteractive = cell.count > 0;
      const cellClass = isInteractive ? 'activity-cell activity-cell-link' : 'activity-cell';
      const attrs = isInteractive
        ? `data-date="${cell.date}" role="button" tabindex="0" aria-label="${escapeAttr(ariaRaw)}"`
        : 'aria-hidden="true"';
      html += `<div class="${cellClass}" data-level="${level}" ${attrs} title="${escapeAttr(title)}"></div>`;
    }
  });
  html += '</div>';
  html += '</div>';
  html += '<div class="activity-legend" aria-hidden="true">';
  html += `<span>${escapeHtml(tr('home.activity.legend_less', {}, 'Less'))}</span>`;
  for (let level = 0; level <= 4; level++) {
    html += `<span class="activity-cell activity-legend-cell" data-level="${level}" title="${escapeAttr(levelLabel(level))}"></span>`;
  }
  html += `<span>${escapeHtml(tr('home.activity.legend_more', {}, 'More'))}</span>`;
  html += '</div>';

  map.innerHTML = html;

  // Wire cell click / Enter / Space to jump into the Timeline tab,
  // pre-filled with a single-day custom range. Future / before-range
  // cells are rendered without the .activity-cell-link class so they
  // never receive a handler.
  map.querySelectorAll('.activity-cell-link').forEach(cell => {
    const date = cell.dataset.date;
    const trigger = () => _jumpToTimelineDate(date);
    cell.addEventListener('click', trigger);
    cell.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        trigger();
      }
    });
  });
}

function _jumpToTimelineDate(dateKey) {
  const tlDays = qs('tl-days');
  const tlCustom = qs('tl-date-custom');
  const tlFrom = qs('tl-date-from');
  const tlTo = qs('tl-date-to');
  if (tlDays && tlCustom && tlFrom && tlTo) {
    tlDays.value = 'custom';
    tlCustom.hidden = false;
    tlFrom.value = dateKey;
    tlTo.value = dateKey;
    const tlSource = qs('tl-source');
    const tlNs = qs('tl-namespace');
    if (tlSource) tlSource.value = '';
    if (tlNs) tlNs.value = '';
  }
  activateTab('timeline');
}

// D. File Type Distribution
function _renderFileTypeChart(sources, distribution = null) {
  const chart = qs('home-type-chart');
  const typeCounts = {};
  if (Array.isArray(distribution) && distribution.length) {
    distribution.forEach(item => {
      const ext = typeof item?.file_type === 'string' ? item.file_type : '';
      const count = Number(item?.count);
      if (ext) {
        typeCounts[ext.toLowerCase()] = (typeCounts[ext.toLowerCase()] || 0) + (Number.isFinite(count) ? count : 0);
      }
    });
  } else {
    sources.forEach(s => {
      const ext = (s.path.split('.').pop() || 'other').toLowerCase();
      typeCounts[ext] = (typeCounts[ext] || 0) + 1;
    });
  }

  const sorted = Object.entries(typeCounts).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const max = sorted[0]?.[1] || 1;

  if (!sorted.length) {
    chart.innerHTML = _homeInlineState('home.state.no_files_indexed');
    return;
  }

  chart.innerHTML = sorted.map(([ext, count]) => {
    const pct = Math.round((count / max) * 100);
    const color = fileTypeColor('x.' + ext);
    return `<div class="home-bar-row">
      <span class="home-bar-label">.${escapeHtml(ext)}</span>
      <div class="home-bar-track"><div class="home-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="home-bar-count">${count}</span>
    </div>`;
  }).join('');
}

// Chunk Size Distribution (token buckets)
function _renderChunkDist(distribution) {
  const chart = qs('home-chunk-dist');
  if (!distribution.length) {
    chart.innerHTML = _homeInlineState('home.state.no_data');
    return;
  }
  const total = distribution.reduce((s, d) => s + d.count, 0);
  const max = Math.max(1, ...distribution.map(d => d.count));

  chart.innerHTML = distribution.map(d => {
    const pct = Math.round((d.count / max) * 100);
    const ratio = total ? Math.round((d.count / total) * 100) : 0;
    const color = d.count === 0 ? 'var(--muted)' : 'var(--accent)';
    return `<div class="home-bar-row">
      <span class="home-bar-label">${escapeHtml(d.bucket)}</span>
      <div class="home-bar-track"><div class="home-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="home-bar-count">${d.count} <span class="muted-sm">(${ratio}%)</span></span>
    </div>`;
  }).join('');
}

// G. Namespace Summary
// Shared friendly-label formatter for long namespace ids (Home chart, search
// result badge, detail panel). Short ids (≤28 chars) pass through unchanged;
// long auto-namespaces collapse to "provider: .../tail". The full id is always
// preserved by callers in title/aria-label. Filter dropdowns keep full names.
function formatNsLabel(nsName) {
  const raw = String(nsName || '');
  if (raw.length <= 28) return raw;

  const autoMatch = raw.match(/^([a-z]+):-(.+)$/);
  if (autoMatch) {
    const provider = autoMatch[1];
    const parts = autoMatch[2].split('-').filter(Boolean);
    const tail = parts.slice(-2).join('/');
    if (tail) return `${provider}: .../${tail}`;
  }

  return `${raw.slice(0, 12)}...${raw.slice(-14)}`;
}

function _homeNsActionLabel(nsName, chunkCount) {
  const count = Number(chunkCount || 0);
  return `Open Sources filtered to namespace ${nsName}, ${count.toLocaleString()} chunk${count === 1 ? '' : 's'}`;
}

function _bindHomeNsChartActions(chart) {
  chart.querySelectorAll('[data-home-ns]').forEach(el => {
    el.addEventListener('click', () => {
      navigateToSourcesByNs(el.dataset.homeNs);
    });
  });
  chart.querySelectorAll('[data-home-ns-more]').forEach(el => {
    el.addEventListener('click', () => {
      activateTab('settings');
      switchSettingsSection('namespaces');
    });
  });
}

function _renderNsChart(namespaces) {
  const chart = qs('home-ns-chart');
  if (!namespaces.length) {
    chart.innerHTML = _homeInlineState('home.state.no_namespaces');
    return;
  }

  const allSorted = [...namespaces].sort((a, b) => b.chunk_count - a.chunk_count);
  const sorted = allSorted.slice(0, 6);
  const hiddenCount = Math.max(0, allSorted.length - sorted.length);
  const max = sorted[0]?.chunk_count || 1;
  const palette = ['var(--accent)', 'var(--green)', '#e0a800', '#a29bfe', '#e17055', '#00cec9'];

  chart.innerHTML = sorted.map((ns, i) => {
    const pct = Math.round((ns.chunk_count / max) * 100);
    const color = ns.color || palette[i % palette.length];
    const nsName = String(ns.namespace || '');
    const fullNs = escapeHtml(nsName);
    const shortNs = escapeHtml(formatNsLabel(nsName));
    const nsAttr = escapeAttr(nsName);
    const actionLabel = escapeAttr(_homeNsActionLabel(nsName, ns.chunk_count));
    return `<div class="home-bar-row home-ns-row">
      <button class="home-ns-open" type="button" data-home-ns="${nsAttr}" aria-label="${actionLabel}">
        <span class="home-bar-label" title="${escapeAttr(nsName)}">${shortNs}</span>
        <span class="home-bar-track" aria-hidden="true"><span class="home-bar-fill" style="width:${pct}%;background:${color}"></span></span>
        <span class="home-bar-count">${ns.chunk_count.toLocaleString()}</span>
        <span class="home-ns-action">Sources</span>
      </button>
      <details class="home-ns-detail">
        <summary aria-label="Show full namespace ${escapeAttr(nsName)}">Full</summary>
        <span>${fullNs}</span>
      </details>
    </div>`;
  }).join('') + (hiddenCount
    ? `<button class="home-ns-more" type="button" data-home-ns-more="true">+ ${hiddenCount.toLocaleString()} more in Namespaces</button>`
    : '');

  _bindHomeNsChartActions(chart);
}

// E. Recent Sources — color dot + 2-row layout
function _renderHomeRecent(allSources) {
  const recentList = qs('home-recent-list');
  if (!allSources.length) {
    recentList.innerHTML = emptyState('📁', t('home.state.no_sources_title'), t('home.state.no_sources_hint'));
    return;
  }

  const sorted = [...allSources].sort((a, b) => {
    const ta = a.last_indexed_at ? new Date(a.last_indexed_at).getTime() : 0;
    const tb = b.last_indexed_at ? new Date(b.last_indexed_at).getTime() : 0;
    return tb - ta || b.chunk_count - a.chunk_count;
  }).slice(0, 8);

  recentList.innerHTML = sorted.map(s => {
    const name = basename(s.path);
    const size = s.file_size != null ? formatBytes(s.file_size) : '';
    const age = s.last_indexed_at ? relativeTime(s.last_indexed_at) : '';
    const nsBadges = (s.namespaces || [])
      .filter(ns => ns !== 'default')
      .map(ns => `<span class="badge badge-ns source-ns-badge">${escapeHtml(ns)}</span>`)
      .join('');
    return `
      <div class="home-source-item home-recent-item" data-path="${escapeAttr(s.path)}" title="${escapeAttr(s.path)}" tabindex="0" role="button">
        <div class="home-source-row1">
          <span class="home-source-dot" style="background:${fileTypeColor(s.path)}"></span>
          <span class="home-source-name">${escapeHtml(name)}</span>
          ${nsBadges}
          <span class="badge badge-blue">${escapeHtml(t(s.chunk_count === 1 ? 'home.source_chunks_one' : 'home.source_chunks_other', { count: s.chunk_count }))}</span>
        </div>
        <div class="home-source-row2">
          ${size}${size && age ? ' \u00b7 ' : ''}${age}
        </div>
      </div>`;
  }).join('');

  recentList.querySelectorAll('.home-source-item').forEach(el => {
    const go = () => _navigateToSource(el.dataset.path);
    el.addEventListener('click', go);
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
}

// H. Storage Health
function _renderStorageHealth(config, sources, embStatus) {
  const info = qs('home-health-info');
  const lastIndexed = sources
    .filter(s => s.last_indexed_at)
    .sort((a, b) => new Date(b.last_indexed_at).getTime() - new Date(a.last_indexed_at).getTime())[0];
  const lastTime = lastIndexed ? relativeTime(lastIndexed.last_indexed_at) : t('home.health.never');

  // Use DB-stored embedding values when available, fall back to config
  const stored = embStatus && embStatus.stored;
  const embProvider = stored ? stored.provider : config?.embedding?.provider;
  const embModel = stored ? stored.model : config?.embedding?.model;
  const embDim = stored ? stored.dimension : config?.embedding?.dimension;
  const storageBackend = config?.storage?.backend || t('home.health.unknown');
  const hasMismatch = embStatus && embStatus.has_mismatch;
  const warnIcon = hasMismatch ? ' ⚠' : '';
  const embText = embProvider && embModel
    ? `${embProvider}/${embModel}`
    : t('home.health.unknown');
  const dimText = embDim || t('home.health.unknown');

  info.innerHTML = `
    <div class="home-health-item">
      <span class="home-health-label">${escapeHtml(t('home.health.embedding'))}</span>
      <span class="home-health-value">${escapeHtml(embText)}${warnIcon}</span>
    </div>
    <div class="home-health-item">
      <span class="home-health-label">${escapeHtml(t('home.health.dimension'))}</span>
      <span class="home-health-value">${escapeHtml(String(dimText))}${warnIcon}</span>
    </div>
    <div class="home-health-item">
      <span class="home-health-label">${escapeHtml(t('home.health.storage'))}</span>
      <span class="home-health-value">${escapeHtml(storageBackend)}</span>
    </div>
    <div class="home-health-item">
      <span class="home-health-label">${escapeHtml(t('home.health.last_indexed'))}</span>
      <span class="home-health-value">${escapeHtml(lastTime)}</span>
    </div>
  `;
}

function _navigateToSource(path, chunkId = '') {
  // Two-phase navigation:
  //   1. Eager vendor resolve when the dashboard already cached the
  //      data we need (``loadDashboard`` mirrors /api/sources +
  //      /api/memory-dirs/status into STATE on Home render). Switching
  //      the sub-tab before ``activateTab`` means the right tree shape
  //      lands on first paint instead of flashing the previous vendor.
  //   2. Always set ``pendingActivatePath`` so the actual focus +
  //      scroll + browseSource is performed by the renderer once the
  //      tree exists. Replaces the old setTimeout(300) gamble that
  //      missed on cold loads (Home was the entry point and the data
  //      hadn't been fetched yet) and on stale-localStorage vendors.
  //   3. ``chunkId`` is optional — when present (Timeline jump knows
  //      the exact chunk), ``browseSource`` scrolls + expands + flashes
  //      that card after the source's chunk list renders.
  STATE.pendingActivateChunkId = chunkId || '';
  STATE.pendingActivateChunkSourcePath = chunkId ? path : '';
  const src = (STATE.allSources || []).find(s => s.path === path);
  const status = src && src.memory_dir
    ? (STATE.memoryStatusByPath || {})[src.memory_dir]
    : null;
  const provider = (status && typeof _SOURCES_VENDORS !== 'undefined'
    && _SOURCES_VENDORS.includes(status.provider))
    ? status.provider : null;
  if (provider && STATE.sourcesActiveVendor !== provider) {
    STATE.sourcesActiveVendor = provider;
    if (typeof _syncSourcesVendorTabs === 'function') {
      _syncSourcesVendorTabs(provider);
    }
  }
  STATE.pendingActivatePath = path;
  activateTab('sources');
}

// C. Quick Search from Home
qs('home-search-go').addEventListener('click', () => {
  const q = qs('home-search-input').value.trim();
  if (!q) return;
  activateTab('search');
  qs('search-input').value = q;
  qs('search-btn').click();
});
qs('home-search-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') qs('home-search-go').click();
});

// F. Quick Actions
function showQuickActionToast(key, focusTarget) {
  showToast(t(key), 'info');
  if (focusTarget) {
    requestAnimationFrame(() => qs(focusTarget)?.focus());
  }
}

qs('home-search-btn').addEventListener('click', () => {
  activateTab('search');
  qs('search-input').focus();
  showQuickActionToast('toast.quick_action.open_search');
});
qs('home-index-btn').addEventListener('click', () => {
  activateTab('index');
  // These quick actions are folder-flow specific (focus index-path / set
  // index-force), so force folder mode — the default is now compose (S1.6).
  setIndexMode('folder');
  showQuickActionToast('toast.quick_action.open_index', 'index-path');
});
qs('home-reindex-btn').addEventListener('click', () => {
  activateTab('index');
  setIndexMode('folder');
  qs('index-force').checked = true;
  showQuickActionToast('toast.quick_action.reindex_ready', 'index-path');
});
qs('home-export-btn').addEventListener('click', () => {
  activateTab('settings');
  switchSettingsSection('export');
  showQuickActionToast('toast.quick_action.open_export', 'exp-preview-btn');
});
qs('home-dedup-btn').addEventListener('click', () => {
  activateTab('settings');
  switchSettingsSection('dedup');
  showQuickActionToast('toast.quick_action.open_dedup', 'dedup-scan-btn');
});
qs('home-tags-btn').addEventListener('click', () => {
  activateTab('tags');
  showQuickActionToast('toast.quick_action.open_tags', 'tags-search');
});

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

// Search and stale state now in STATE object

function _markDataStale() {
  STATE.sourcesBrowserStale = true;
  STATE.tagsTabStale = true;
  STATE.homeStale = true;
}

// Sync result content in STATE.lastResults cache and DOM after edit
function _syncResultContent(chunkId, newContent) {
  const cached = STATE.lastResults.find(r => String(r.chunk.id) === String(chunkId));
  if (cached) cached.chunk.content = newContent;
  const item = document.querySelector(`.result-item[data-id="${CSS.escape(String(chunkId))}"]`);
  if (!item) return;
  const snippet = item.querySelector('.result-snippet');
  if (snippet) snippet.innerHTML = highlightText(truncate(newContent, 200), STATE.lastQuery);
}

qs('search-btn').addEventListener('click', doSearch);
qs('search-input').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
qs('search-input').addEventListener('focus', () => renderHistoryDropdown());
qs('search-input').addEventListener('input', () => renderHistoryDropdown());
qs('search-input').addEventListener('input', debounce(() => {
  if (qs('search-input').value.trim().length >= 2) doSearch();
}, 400));
document.addEventListener('click', e => {
  const dropdown = qs('search-history-dropdown');
  if (dropdown && !dropdown.contains(e.target) && e.target !== qs('search-input')) {
    hide(dropdown);
  }
});

// E. Active filters display
function _getSelectedSourceFilters() {
  return Array.from((qs('source-filter') || { selectedOptions: [] }).selectedOptions)
    .map(o => o.value)
    .filter(Boolean);
}

function _clearSourceFilters() {
  Array.from((qs('source-filter') || { options: [] }).options)
    .forEach(o => { o.selected = false; });
}

function _formatDateFilterLabel() {
  const preset = qs('date-range-preset')?.value || '';
  if (!preset) return '';
  if (preset === 'custom') {
    const from = qs('date-from')?.value || '...';
    const to = qs('date-to')?.value || '...';
    return `date: ${from} - ${to}`;
  }
  const selected = qs('date-range-preset')?.selectedOptions?.[0];
  return `date: ${selected ? selected.textContent : preset}`;
}

function _clearDateFilter() {
  qs('date-range-preset').value = '';
  qs('date-from').value = '';
  qs('date-to').value = '';
  qs('date-range-custom').hidden = true;
}

function _hasSearchAxis() {
  return !!(
    qs('search-input').value.trim()
    || qs('tag-filter').value.trim()
    || _getSelectedSourceFilters().length
  );
}

function _buildSearchParams(topK) {
  const q = qs('search-input').value.trim();
  const tf = qs('tag-filter').value.trim();
  const nsFilter = qs('ns-filter').value;
  const params = new URLSearchParams({ top_k: topK });
  if (q) params.set('q', q);
  if (tf) params.set('tag_filter', tf);
  if (nsFilter) params.set('namespace', nsFilter);
  const ctxWin = parseInt((qs('context-window') || {}).value || '0', 10);
  if (ctxWin > 0) params.set('context_window', ctxWin);
  const selectedSources = _getSelectedSourceFilters();
  if (selectedSources.length) params.set('source_filter', selectedSources.join(','));
  return params;
}

function _renderActiveFilters() {
  const el = qs('active-filters');
  const chips = [];
  const ns = qs('ns-filter').value;
  if (ns) chips.push({ label: `ns: ${ns}`, clear: () => { qs('ns-filter').value = ''; } });
  const tag = qs('tag-filter').value.trim();
  if (tag) chips.push({ label: `tag: ${tag}`, clear: () => { qs('tag-filter').value = ''; } });
  const ct = qs('chunk-type-filter').value;
  if (ct) chips.push({ label: `type: ${ct.replace('_', ' ')}`, clear: () => { qs('chunk-type-filter').value = ''; } });
  const sources = _getSelectedSourceFilters();
  if (sources.length) {
    const label = sources.length === 1
      ? `source: ${basename(sources[0])}`
      : `sources: ${sources.length}`;
    chips.push({ label, clear: _clearSourceFilters });
  }
  const dateLabel = _formatDateFilterLabel();
  if (dateLabel) chips.push({ label: dateLabel, clear: _clearDateFilter });
  if (STATE.scoreMin > 0) chips.push({ label: `score \u2265 ${STATE.scoreMin}`, clear: () => { qs('score-threshold').value = 0; STATE.scoreMin = 0; qs('score-val').textContent = '0.0'; } });

  _updateFilterCountBadge();
  if (!chips.length) { hide(el); return; }
  el.innerHTML = chips.map((c, i) => {
    const removeLabel = escapeAttr(`Remove ${c.label} filter`);
    return `<span class="active-filter-chip">${escapeHtml(c.label)}<button class="active-filter-remove" data-idx="${i}" aria-label="${removeLabel}" title="${removeLabel}">\u2715</button></span>`;
  }).join('') + `<button id="clear-search-filters" class="active-filters-clear" type="button">Clear all</button>`;
  el.querySelectorAll('.active-filter-remove').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      chips[parseInt(btn.dataset.idx)].clear();
      _updateFilterCountBadge();
      if (_hasSearchAxis()) {
        renderResults(STATE.lastResults);
        doSearch();
      } else {
        renderResults([]);
      }
    });
  });
  qs('clear-search-filters').addEventListener('click', e => {
    e.stopPropagation();
    qs('ns-filter').value = '';
    qs('tag-filter').value = '';
    qs('chunk-type-filter').value = '';
    _clearSourceFilters();
    _clearDateFilter();
    qs('score-threshold').value = 0;
    STATE.scoreMin = 0;
    qs('score-val').textContent = '0.0';
    _updateFilterCountBadge();
    if (_hasSearchAxis()) {
      renderResults(STATE.lastResults);
      doSearch();
    } else {
      renderResults([]);
    }
  });
  show(el);
}

// Count of currently-applied search filters. Kept in sync with the chip
// set built by _renderActiveFilters above \u2014 same four categories so the
// toggle-button badge and the chip row never disagree.
function _countActiveFilters() {
  let n = 0;
  if (qs('ns-filter')?.value) n++;
  if (qs('tag-filter')?.value.trim()) n++;
  if (qs('chunk-type-filter')?.value) n++;
  if (_getSelectedSourceFilters().length) n++;
  if (qs('date-range-preset')?.value) n++;
  if (STATE.scoreMin > 0) n++;
  return n;
}

function _updateFilterCountBadge() {
  const badge = qs('filter-count-badge');
  if (!badge) return;
  const n = _countActiveFilters();
  if (n > 0) {
    badge.textContent = String(n);
    badge.hidden = false;
    const key = n === 1
      ? 'search.filter_count_aria_one'
      : 'search.filter_count_aria_other';
    const aria = (typeof t === 'function')
      ? t(key, { count: n })
      : `${n} filter${n !== 1 ? 's' : ''} applied`;
    badge.setAttribute('aria-label', aria);
  } else {
    badge.hidden = true;
    badge.textContent = '';
    badge.removeAttribute('aria-label');
  }
}

let _searchAbortCtrl = null;

async function doSearch() {
  const q = qs('search-input').value.trim();
  // #750: tag/source-only search is a valid path — clicking a tag pill
  // or selecting a source on a fresh session should show matching
  // memories rather than no-op. The early return only fires when every
  // server-side selector is empty.
  if (!_hasSearchAxis()) return;
  // #696: kick the readiness poll. Fire-and-forget; the search request
  // proceeds normally — its response covers the spinner. The poll's job
  // is to surface "Downloading bge-m3 (~2.3 GB)…" while the backend's
  // lazy loader blocks our request on a multi-GB cold-cache load. The
  // first tick may race the request and observe ``cold``; the poll
  // continues until a terminal state because ``cold`` is intentionally
  // non-terminal for this entry point (see ``_modelComponentDone``).
  _modelReadinessPoll();
  STATE.lastQuery = q;
  if (q) saveToHistory(q);
  hide(qs('search-history-dropdown'));
  STATE.currentTopK = parseInt(qs('top-k').value, 10);
  const params = _buildSearchParams(STATE.currentTopK);

  // Cancel any in-flight search
  if (_searchAbortCtrl) _searchAbortCtrl.abort();
  _searchAbortCtrl = new AbortController();

  const btn = qs('search-btn');
  btnLoading(btn, true);
  try {
    const data = await api('GET', `/api/search?${params}`, undefined,
                           { signal: _searchAbortCtrl.signal });
    renderResults(data.results, data.retrieval_stats);
  } catch (err) {
    if (err.name === 'AbortError') return;
    const list = qs('results-list');
    list.innerHTML = '';
    hide(qs('results-empty'));
    show(list);
    list.innerHTML = emptyState('⚠', 'Search failed', escapeHtml(err.message));
    // #results-empty is now hidden, so the sibling welcome card must hide too.
    _syncWelcomeVisibility();
    clearDetail();
  } finally {
    btnLoading(btn, false);
  }
}

function updateBulkToolbar(total) {
  const count = STATE.selectedIds.size;
  qs('bulk-count').textContent = t('search.bulk_count', { count });
  qs('bulk-delete-btn').disabled = count === 0;
  qs('bulk-export-btn').disabled = count === 0;
  const allCb = qs('bulk-select-all');
  allCb.checked = total > 0 && count === total;
  allCb.indeterminate = count > 0 && count < total;
}

function _buildScoreViews(results) {
  const byRank = [...results].sort((a, b) => (a.rank || 0) - (b.rank || 0));
  const total = Math.max(1, byRank.length);
  const positiveMax = Math.max(0, ...byRank.map(r => Number(r.score) || 0));
  const views = {};

  byRank.forEach((r, idx) => {
    const raw = Number(r.score) || 0;
    const rank = r.rank || idx + 1;
    const isReranked = r.source === 'reranked';
    let percent;
    let label;
    let tooltip;

    if (isReranked) {
      percent = total === 1 ? 100 : Math.round((1 - idx / (total - 1)) * 100);
      percent = Math.max(1, Math.min(100, percent));
      label = `${percent}%`;
      tooltip = `Reranker percentile ${percent}% by final rank. Raw reranker score ${raw.toFixed(6)}.`;
    } else {
      percent = positiveMax > 0 ? Math.round((raw / positiveMax) * 100) : 0;
      percent = Math.max(0, Math.min(100, percent));
      label = `${percent}%`;
      tooltip = `Raw ${r.source || 'search'} score ${raw.toFixed(6)}. Normalized ${percent}%.`;
    }

    views[r.chunk.id] = { label, percent, tooltip, raw, rank, isReranked };
  });

  return views;
}

function _scoreViewForResult(r) {
  const raw = Number(r.score) || 0;
  return STATE.resultScoreViews[r.chunk.id] || {
    label: raw.toFixed(3),
    percent: STATE.maxResultScore > 0
      ? Math.max(0, Math.min(100, Math.round((raw / STATE.maxResultScore) * 100)))
      : 0,
    tooltip: `Raw ${r.source || 'search'} score ${raw.toFixed(6)}.`,
    raw,
    rank: r.rank,
    isReranked: r.source === 'reranked',
  };
}

// Localized "Relevance" label, with an English fallback for the pre-i18n /
// failed-locale path (mirrors the inline pattern used elsewhere on this surface).
function _relevanceLabel() {
  if (typeof t === 'function') {
    const tr = t('search.relevance_label');
    if (tr !== 'search.relevance_label') return tr;
  }
  return 'Relevance';
}

// First-time-friendly tooltip for the score bar; the raw retrieval math is
// gated behind the Advanced-details reveal (see showDetail / style.css).
function _relevanceTooltip() {
  if (typeof t === 'function') {
    const tr = t('search.relevance_tooltip');
    if (tr !== 'search.relevance_tooltip') return tr;
  }
  return 'How closely this result matches your query.';
}

function _buildResultItem(r) {
  const list = qs('results-list');
  const item = document.createElement('div');
  item.className = 'result-item';
  item.dataset.id = r.chunk.id;
  item.setAttribute('tabindex', '0');
  item.setAttribute('role', 'button');

  const checkLabel = document.createElement('label');
  checkLabel.className = 'result-check-wrap';
  checkLabel.addEventListener('click', e => e.stopPropagation());
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.className = 'result-checkbox';
  checkbox.dataset.id = r.chunk.id;
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) STATE.selectedIds.add(r.chunk.id);
    else STATE.selectedIds.delete(r.chunk.id);
    updateBulkToolbar(list.querySelectorAll('.result-checkbox').length);
  });
  checkLabel.appendChild(checkbox);

  const fname = basename(r.chunk.source_file || '');
  const dir = shortDir((r.chunk.source_file || '').split('/').slice(0, -1).join('/') || '/');
  const age = relativeTime(r.chunk.created_at);
  const lineRange = (r.chunk.start_line && r.chunk.end_line)
    ? `lines ${r.chunk.start_line}-${r.chunk.end_line}`
    : null;
  const nsLabel = r.chunk.namespace && r.chunk.namespace !== 'default'
    ? `namespace ${r.chunk.namespace}`
    : null;
  const ariaParts = [fname, lineRange, nsLabel, age].filter(Boolean);
  item.setAttribute('aria-label', ariaParts.join(', '));
  const nsBadge = r.chunk.namespace && r.chunk.namespace !== 'default'
    ? ` <span class="badge badge-ns" title="${escapeAttr(r.chunk.namespace)}">${escapeHtml(formatNsLabel(r.chunk.namespace))}</span>` : '';
  // ADR-0016 §7 canonical-residency tier badge. Default-omit for the
  // user tier so the common case stays visually quiet — only the
  // non-default tiers (project_shared / project_local) earn pixels.
  // The token is rendered verbatim per the PR-F display-alias-free
  // contract pinned in the Tiered Context Gateway v2 memory.
  const tierBadge = _tierBadgeHtml(r.chunk.target_scope);
  const validityBadge = _validityBadgeHtml(r.chunk.valid_from_unix, r.chunk.valid_to_unix);
  const scoreView = _scoreViewForResult(r);
  const scorePct = scoreView.percent;
  const barColor = scorePct > 70 ? 'var(--green)' : scorePct > 40 ? 'var(--accent)' : 'var(--muted)';
  const relevanceLabel = _relevanceLabel();
  // The score badge is always visible and communicates relevance, so its hover
  // title stays plain-language in every mode (no stale render-time state). The
  // raw per-result retrieval score lives in the detail panel's #d-score, which
  // the Advanced-details reveal un-hides. (aria-label is already friendly.)
  const scoreTitle = _relevanceTooltip();

  const body = document.createElement('div');
  body.className = 'result-body';
  body.innerHTML = `
    <div class="result-item-row1">
      <span class="result-type-dot" style="background:${fileTypeColor(r.chunk.source_file || '')}"></span>
      <span class="result-filename">${escapeHtml(fname)}</span>
      <span class="score-badge" title="${escapeAttr(scoreTitle)}" aria-label="${escapeAttr(relevanceLabel)} ${escapeAttr(scoreView.label)}">${escapeHtml(relevanceLabel)} ${escapeHtml(scoreView.label)}</span>
      <span class="badge badge-retrieval badge-retrieval--${escapeAttr(r.source)} result-debug-meta">${escapeHtml(r.source)}</span>
      ${nsBadge}${tierBadge}${validityBadge}
    </div>
    <div class="result-item-meta">${escapeHtml(dir)} \u00b7 #${r.rank} \u00b7 ${escapeHtml(age)}</div>
    <div class="result-score-bar"><div class="result-score-fill" style="width:${scorePct}%;background:${barColor}"></div></div>
    <div class="result-snippet">${highlightText(truncate(r.chunk.content, 200), STATE.lastQuery)}</div>
  `;

  // Filename click → open source preview modal
  const fnameEl = body.querySelector('.result-filename');
  if (fnameEl) {
    fnameEl.style.cursor = 'pointer';
    fnameEl.title = 'View full source file';
    fnameEl.addEventListener('click', e => {
      e.stopPropagation();
      openSourcePreview(r.chunk.source_file, r.chunk.start_line, r.chunk.end_line);
    });
  }

  item.appendChild(checkLabel);
  item.appendChild(body);

  // Context window rendering — document order (before ↑ snippet ↓ after)
  if (r.context && (r.context.window_before?.length || r.context.window_after?.length)) {
    const snippet = body.querySelector('.result-snippet');
    const bLen = r.context.window_before?.length || 0;
    const aLen = r.context.window_after?.length || 0;

    // Before blocks — inserted above snippet
    let ctxBefore = null;
    if (bLen) {
      ctxBefore = document.createElement('div');
      ctxBefore.className = 'context-group context-group-before';
      ctxBefore.hidden = true;
      r.context.window_before.forEach(cb => {
        const pos = document.createElement('span');
        pos.className = 'context-pos';
        pos.textContent = `↑ L${cb.start_line || '?'}–${cb.end_line || '?'}`;
        const blk = document.createElement('div');
        blk.className = 'context-block context-block-before';
        blk.textContent = truncate(cb.content, 300);
        ctxBefore.appendChild(pos);
        ctxBefore.appendChild(blk);
      });
      snippet.before(ctxBefore);
    }

    // After blocks — inserted below snippet
    let ctxAfter = null;
    if (aLen) {
      ctxAfter = document.createElement('div');
      ctxAfter.className = 'context-group context-group-after';
      ctxAfter.hidden = true;
      r.context.window_after.forEach(ca => {
        const pos = document.createElement('span');
        pos.className = 'context-pos';
        pos.textContent = `↓ L${ca.start_line || '?'}–${ca.end_line || '?'}`;
        const blk = document.createElement('div');
        blk.className = 'context-block context-block-after';
        blk.textContent = truncate(ca.content, 300);
        ctxAfter.appendChild(pos);
        ctxAfter.appendChild(blk);
      });
      snippet.after(ctxAfter);
    }

    // Toggle button — below everything
    const toggleBtn = document.createElement('button');
    toggleBtn.className = 'context-toggle-btn';
    toggleBtn.textContent = `Context (${bLen}+${aLen})`;
    toggleBtn.addEventListener('click', e => {
      e.stopPropagation();
      const showing = ctxBefore ? !ctxBefore.hidden : !ctxAfter?.hidden;
      if (ctxBefore) ctxBefore.hidden = showing;
      if (ctxAfter) ctxAfter.hidden = showing;
      toggleBtn.textContent = showing ? `Context (${bLen}+${aLen})` : 'Hide context';
    });
    body.appendChild(toggleBtn);
  }

  // Tag chips in result item (click=filter, ✕=delete)
  _attachResultTagRow(r.chunk.id, [...(r.chunk.tags || [])], body);

  item.addEventListener('click', () => {
    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('selected'));
    item.classList.add('selected');
    showDetail(r);
  });
  item.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); item.click(); }
  });
  return item;
}

// Render (or re-render) a tag row inside a result-item body.
// liveTagsArr is a mutable array shared between chips so removals stay consistent.
function _attachResultTagRow(chunkId, liveTagsArr, bodyEl) {
  // Remove existing tag row if re-rendering
  const existing = bodyEl.querySelector('.result-tags');
  if (existing) existing.remove();
  if (!liveTagsArr.length) return;

  const tagRow = document.createElement('div');
  tagRow.className = 'result-tags';

  function _makeChip(tag) {
    const chip = document.createElement('span');
    chip.className = 'result-tag-chip';
    const label = document.createElement('span');
    label.className = 'result-tag-label';
    label.textContent = tag;
    label.title = `Filter by "${tag}"`;
    label.addEventListener('click', e => {
      e.stopPropagation();
      qs('tag-filter').value = tag;
      doSearch();
    });
    const removeBtn = document.createElement('button');
    removeBtn.className = 'result-tag-remove';
    removeBtn.textContent = '✕';
    removeBtn.title = t('search.tag_remove_named', { tag });
    removeBtn.addEventListener('click', async e => {
      e.stopPropagation();
      const idx = liveTagsArr.indexOf(tag);
      if (idx === -1) return;
      liveTagsArr.splice(idx, 1);
      chip.remove();
      if (qs('tag-filter').value === tag) qs('tag-filter').value = '';
      // Also update STATE.lastResults cache
      const cached = STATE.lastResults.find(r => String(r.chunk.id) === String(chunkId));
      if (cached) cached.chunk.tags = [...liveTagsArr];
      try {
        await api('PATCH', `/api/chunks/${chunkId}/tags`, { tags: [...liveTagsArr] });
        if (String(STATE.selectedChunkId) === String(chunkId)) renderTagChips([...liveTagsArr]);
      } catch (err) {
        liveTagsArr.splice(idx, 0, tag);
        if (cached) cached.chunk.tags = [...liveTagsArr];
        tagRow.appendChild(_makeChip(tag));
        showToast(t('toast.tag_remove_failed', { error: err.message }), 'error');
      }
    });
    chip.appendChild(label);
    chip.appendChild(removeBtn);
    return chip;
  }

  liveTagsArr.forEach(t => tagRow.appendChild(_makeChip(t)));
  bodyEl.appendChild(tagRow);
}

// Sync result item tag row after external tag save (e.g. detail panel Save Tags).
function _syncResultTags(chunkId, newTags) {
  // Update STATE.lastResults cache
  const cached = STATE.lastResults.find(r => String(r.chunk.id) === String(chunkId));
  if (cached) cached.chunk.tags = [...newTags];
  // Update DOM
  const item = document.querySelector(`.result-item[data-id="${CSS.escape(String(chunkId))}"]`);
  if (!item) return;
  const body = item.querySelector('.result-body');
  if (body) _attachResultTagRow(chunkId, [...newTags], body);
}

function renderResults(results, retrievalStats) {
  STATE.lastResults = results;
  STATE.resultScoreViews = _buildScoreViews(results);
  let display = [...results];
  if (STATE.currentSortMode === 'date-desc') display.sort((a, b) => new Date(b.chunk.created_at) - new Date(a.chunk.created_at));
  else if (STATE.currentSortMode === 'date-asc') display.sort((a, b) => new Date(a.chunk.created_at) - new Date(b.chunk.created_at));
  else if (STATE.currentSortMode === 'source') display.sort((a, b) => (a.chunk.source_file || '').localeCompare(b.chunk.source_file || ''));
  const typeFilter = (qs('chunk-type-filter') || {}).value || '';
  const selectedSources = _getSelectedSourceFilters();
  let filtered = typeFilter ? display.filter(r => r.chunk.chunk_type === typeFilter) : display;
  if (selectedSources.length) filtered = filtered.filter(r => selectedSources.includes(r.chunk.source_file));
  if (STATE.scoreMin > 0) {
    filtered = filtered.filter(r => (_scoreViewForResult(r).percent / 100) >= STATE.scoreMin);
  }
  // Date range filter
  const dateRange = _getDateRange();
  if (dateRange) {
    filtered = filtered.filter(r => {
      const t = new Date(r.chunk.created_at).getTime();
      return t >= dateRange.from && t <= dateRange.to;
    });
  }
  const list = qs('results-list');
  const empty = qs('results-empty');
  STATE.selectedIds.clear();
  _renderActiveFilters();

  if (!filtered.length) {
    hide(list);
    hide(qs('bulk-toolbar'));
    hide(qs('load-more-row'));
    show(empty);
    empty.innerHTML = !_hasSearchAxis()
      ? emptyState('🔍', 'Enter a query to search', '<kbd>/</kbd> focus · <kbd>j</kbd>/<kbd>k</kbd> navigate · <kbd>p</kbd> pin · <kbd>c</kbd> copy')
      : emptyState('○', 'No results found', 'Try different keywords or filters');
    _syncWelcomeVisibility();
    clearDetail();
    return;
  }
  hide(empty);
  show(list);
  _syncWelcomeVisibility();

  // Keep a raw max for fallback paths; primary result bars use score views.
  STATE.maxResultScore = Math.max(0.001, ...filtered.map(r => r.score));

  // Source breakdown summary + pipeline funnel
  const total = filtered.length;
  const counts = { fused: 0, bm25: 0, dense: 0, reranked: 0 };
  filtered.forEach(r => { if (r.source in counts) counts[r.source]++; });
  const sourceParts = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([src, n]) => `<span class="badge badge-retrieval badge-retrieval--${src}">${src} ${n}</span>`);
  let funnelHtml = '';
  if (retrievalStats) {
    const s = retrievalStats;
    const bm25Warn = s.bm25_error
      ? `<span class="badge badge-yellow retrieval-warning" title="${escapeHtml(s.bm25_error)}" role="status" aria-label="Keyword search degraded: ${escapeHtml(s.bm25_error)}">Keyword degraded</span>`
      : '';
    funnelHtml = `<div class="results-funnel">
      <span class="help-tip" data-help="BM25: keyword matching. Dense: semantic embedding similarity. RRF: reciprocal rank fusion merges both. Final: after reranking and filters." tabindex="0" role="img" aria-label="BM25: keyword matching. Dense: semantic embedding similarity. RRF: reciprocal rank fusion merges both. Final: after reranking and filters.">i</span>
      <span class="funnel-stage"><span class="funnel-stage-label">BM25</span> <span class="funnel-stage-count">${s.bm25_candidates}</span></span>
      <span class="funnel-arrow">+</span>
      <span class="funnel-stage"><span class="funnel-stage-label">Dense</span> <span class="funnel-stage-count">${s.dense_candidates}</span></span>
      <span class="funnel-arrow">\u2192</span>
      <span class="funnel-stage"><span class="funnel-stage-label">RRF</span> <span class="funnel-stage-count">${s.fused_total}</span></span>
      <span class="funnel-arrow">\u2192</span>
      <span class="funnel-stage"><span class="funnel-stage-label">Final</span> <span class="funnel-stage-count">${s.final_total}</span></span>
    </div>${bm25Warn}`;
    // Cache retrieval stats for score detail computation
    STATE.lastRetrievalStats = s;
  }
  let advancedDetailsLabel = 'Advanced details';
  if (typeof t === 'function') {
    const translated = t('search.advanced_details');
    advancedDetailsLabel = translated === 'search.advanced_details' ? advancedDetailsLabel : translated;
  }
  const debugHtml = (sourceParts.length || funnelHtml)
    ? `<details class="results-debug-details">
        <summary>${escapeHtml(advancedDetailsLabel)}</summary>
        <div class="results-debug-body">${sourceParts.join('')}${funnelHtml}</div>
      </details>`
    : '';
  const summaryHtml = `<div class="results-summary"><span class="results-summary-total">${escapeHtml(t('search.results_total', { count: total }))}</span>${debugHtml}</div>`;

  show(qs('bulk-toolbar'));
  if (results.length >= STATE.currentTopK) show(qs('load-more-row'));
  else hide(qs('load-more-row'));
  updateBulkToolbar(0);
  list.innerHTML = summaryHtml;
  list.classList.toggle('list-view', STATE.viewMode === 'list');
  // "Advanced details" doubles as the retrieval-internals reveal: opening it
  // sets body.show-retrieval-debug, surfacing the raw per-result source badge
  // and the detail-panel score/source/RRF math (hidden by default). The
  // <details> is rebuilt on every render, so restore + rebind each time.
  const debugDetails = list.querySelector('.results-debug-details');
  if (debugDetails) {
    debugDetails.open = !!STATE.showRetrievalDebug;
    debugDetails.addEventListener('toggle', () => {
      STATE.showRetrievalDebug = debugDetails.open;
      document.body.classList.toggle('show-retrieval-debug', debugDetails.open);
    });
  }
  document.body.classList.toggle('show-retrieval-debug', !!STATE.showRetrievalDebug);

  if (STATE.groupMode) {
    const groups = {};
    filtered.forEach(r => {
      const key = r.chunk.source_file || '(unknown)';
      if (!groups[key]) groups[key] = [];
      groups[key].push(r);
    });
    let firstResult = null, firstItem = null;
    Object.entries(groups).forEach(([source, items]) => {
      const groupEl = document.createElement('div');
      groupEl.className = 'result-source-group';
      const header = document.createElement('div');
      header.className = 'result-group-header';
      header.innerHTML = `<span class="result-group-chevron">▼</span><span class="result-group-name">${escapeHtml(basename(source))}</span><span class="badge badge-blue">${items.length}</span>`;
      const groupItems = document.createElement('div');
      groupItems.className = 'result-group-items';
      header.addEventListener('click', () => {
        const isOpen = !groupItems.hidden;
        groupItems.hidden = isOpen;
        header.querySelector('.result-group-chevron').textContent = isOpen ? '▶' : '▼';
      });
      items.forEach(r => {
        const item = _buildResultItem(r);
        groupItems.appendChild(item);
        if (!firstResult) { firstResult = r; firstItem = item; }
      });
      groupEl.appendChild(header);
      groupEl.appendChild(groupItems);
      list.appendChild(groupEl);
    });
    if (firstItem) { firstItem.classList.add('selected'); showDetail(firstResult); }
  } else {
    filtered.forEach((r, i) => {
      const item = _buildResultItem(r);
      list.appendChild(item);
      if (i === 0) { item.classList.add('selected'); showDetail(r); }
    });
  }
}

function showDetail(r) {
  hide(qs('detail-empty'));
  const view = qs('detail-view');
  show(view);

  STATE.selectedChunkId = r.chunk.id;
  STATE.selectedOriginal = r.chunk.content;

  const scoreView = _scoreViewForResult(r);
  qs('d-score').textContent = scoreView.isReranked
    ? `rank #${scoreView.rank} · ${scoreView.percent}%`
    : `score ${r.score.toFixed(4)}`;
  qs('d-score').title = scoreView.tooltip;
  qs('d-type').textContent = r.chunk.chunk_type.replace('_', ' ');
  const nsEl = qs('d-namespace');
  if (r.chunk.namespace && r.chunk.namespace !== 'default') {
    // Friendly label; full id preserved in title + aria-label.
    nsEl.textContent = formatNsLabel(r.chunk.namespace);
    nsEl.title = r.chunk.namespace;
    nsEl.setAttribute('aria-label', `namespace ${r.chunk.namespace}`);
    show(nsEl);
  } else {
    nsEl.removeAttribute('title');
    nsEl.removeAttribute('aria-label');
    hide(nsEl);
  }
  const srcEl = qs('d-source');
  srcEl.textContent = r.source;
  // Keep result-debug-meta so this raw retrieval-source badge stays gated.
  srcEl.className = `badge badge-retrieval badge-retrieval--${r.source} result-debug-meta`;

  // Score detail row: rank + normalized display bar + raw diagnostic score.
  const rrfK = (STATE.serverConfig && STATE.serverConfig.search && STATE.serverConfig.search.rrf_k) || 60;
  const rs = STATE.lastRetrievalStats || {};
  const nSources = ((rs.bm25_candidates > 0) ? 1 : 0) + ((rs.dense_candidates > 0) ? 1 : 0) || 2;
  const maxRrf = nSources / (rrfK + 1);
  const pct = scoreView.percent;
  qs('d-rank-label').textContent = `#${r.rank}`;
  qs('d-score-bar').style.width = `${pct.toFixed(1)}%`;
  qs('d-score-pct').textContent = `${pct.toFixed(0)}%`;
  const scoreDetailRow = qs('d-score-detail');
  // Default hover tooltip is plain-language; the raw retrieval math lives in
  // data-tooltip-debug and only renders under body.show-retrieval-debug
  // (the Advanced-details reveal), so first-time users never meet RRF/k.
  scoreDetailRow.dataset.tooltip = _relevanceTooltip();
  scoreDetailRow.dataset.tooltipDebug = scoreView.isReranked
    ? `Reranker percentile ${pct.toFixed(0)}%; raw score ${r.score.toFixed(6)}`
    : `RRF ${r.score.toFixed(6)} / max ${maxRrf.toFixed(6)} (k=${rrfK}, ${nSources} sources)`;
  show(scoreDetailRow);
  qs('d-hierarchy').textContent = r.chunk.heading_hierarchy.join(' › ');
  qs('d-file').textContent = r.chunk.source_file;
  qs('d-lines').textContent = `lines ${r.chunk.start_line}–${r.chunk.end_line}`;
  qs('d-editor').value = r.chunk.content;
  hide(qs('detail-msg'));
  hide(qs('similar-panel'));
  hide(qs('source-chunks-panel'));
  hide(qs('history-panel'));

  renderTagChips(r.chunk.tags || []);
  updatePinBtn(r.chunk.id);
  _updateHistoryBtn(r.chunk.id);
  qs('d-created').textContent = relativeTime(r.chunk.created_at);
  _renderValidityBadge(qs('d-validity'), r.chunk.valid_from_unix, r.chunk.valid_to_unix);
  _updateWordCount();
  _updateSourceNav();

  // Set source and apply current view mode (default: view)
  STATE.detailViewSource = r.chunk.source_file || '';
  hide(qs('d-preview'));
  hide(qs('d-preview-btn'));  // Preview merged into View
  _setDetailMode(STATE.detailViewMode || 'view');

  // Reset diff state
  hide(qs('d-diff'));
  hide(qs('d-diff-btn'));
  qs('d-diff-btn').dataset.mode = 'source';
  qs('d-diff-btn').textContent = 'Diff';

  // C1: On mobile, switch to detail panel view
  if (window.innerWidth <= 768) {
    document.querySelector('.results-layout').classList.add('mobile-detail');
  }
}

function clearDetail() {
  hide(qs('detail-view'));
  show(qs('detail-empty'));
  qs('detail-empty').querySelector('p').textContent = 'Select a result to view details';
  STATE.selectedChunkId = null;
}

// Edit / Delete / Reset
qs('d-save-btn').addEventListener('click', async () => {
  if (!STATE.selectedChunkId) return;
  const newContent = qs('d-editor').value;
  const btn = qs('d-save-btn');
  btnLoading(btn, true);
  try {
    const resp = await apiWithRedactionRetry(
      'PATCH',
      `/api/chunks/${STATE.selectedChunkId}`,
      { new_content: newContent },
    );
    if (resp === null) return;
    // History push must follow a confirmed write — pushing before the
    // request would pollute the undo stack with a no-op entry when the
    // user cancels the redaction-blocked confirm dialog.
    _pushHistory(STATE.selectedChunkId, STATE.selectedOriginal);
    showToast(t('toast.chunk_saved'), 'success');
    STATE.selectedOriginal = newContent;
    _syncResultContent(STATE.selectedChunkId, newContent);
    _updateHistoryBtn(STATE.selectedChunkId);
    _markDataStale();
    loadStats();
  } catch (err) {
    showToast(t('toast.save_failed', { error: err.message }), 'error');
  } finally {
    btnLoading(btn, false);
  }
});

qs('d-delete-btn').addEventListener('click', async () => {
  if (!STATE.selectedChunkId) return;
  const r = STATE.lastResults.find(x => String(x.chunk.id) === String(STATE.selectedChunkId));
  const src = r ? r.chunk.source_file.split('/').pop() : '';
  const lines = r ? `lines ${r.chunk.start_line}–${r.chunk.end_line}` : '';
  const ok = await showConfirm({
    title: t('confirm.chunk_delete_title'),
    message: t('confirm.chunk_delete_msg', { lines, source: src }),
    confirmText: t('common.delete'),
  });
  if (!ok) return;
  try {
    await api('DELETE', `/api/chunks/${STATE.selectedChunkId}`);
    showToast(t('toast.chunk_deleted'), 'success');
    clearDetail();
    _markDataStale();
    doSearch();
    loadStats();
  } catch (err) {
    showToast(t('toast.delete_failed', { error: err.message }), 'error');
  }
});

qs('d-reset-btn').addEventListener('click', () => {
  qs('d-editor').value = STATE.selectedOriginal;
  hide(qs('d-diff'));
  hide(qs('d-diff-btn'));
  qs('d-diff-btn').dataset.mode = 'source';
  _setDetailMode('edit');
  _updateWordCount();
  showToast(t('toast.content_restored'), 'info');
});

// ── B2: Copy button ──
qs('d-copy-btn').addEventListener('click', () => copyToClipboard(qs('d-editor').value));

// ── B4: Markdown Preview toggle ──
// Preview merged into View — one toggle for both code highlighting and markdown rendering
// STATE.detailViewSource now in STATE
qs('d-view-btn').addEventListener('click', () => {
  const btn = qs('d-view-btn');
  const isViewing = btn.dataset.mode === 'view';
  if (isViewing) {
    _setDetailMode('edit');
  } else {
    _setDetailMode('view');
  }
});

function _setDetailMode(mode) {
  const btn = qs('d-view-btn');
  const codeView = qs('d-code-view');
  const editor = qs('d-editor');
  STATE.detailViewMode = mode;
  if (mode === 'edit') {
    hide(codeView);
    show(editor);
    btn.textContent = 'View';
    btn.dataset.mode = 'edit';
    // Show edit-only actions
    show(qs('d-save-btn'));
  } else {
    const content = editor.value;
    const lang = getLanguage(STATE.detailViewSource);
    const isMarkdown = (lang === 'markdown' || (STATE.detailViewSource || '').endsWith('.md'));
    if (isMarkdown && typeof marked !== 'undefined') {
      codeView.className = 'detail-code-view md-preview';
      codeView.innerHTML = DOMPurify.sanitize(marked.parse(content));
    } else if (lang && lang !== 'markdown' && window.Prism && Prism.languages[lang]) {
      codeView.className = 'detail-code-view';
      codeView.innerHTML = `<pre><code class="language-${lang}">${Prism.highlight(content, Prism.languages[lang], lang)}</code></pre>`;
    } else {
      codeView.className = 'detail-code-view';
      codeView.innerHTML = `<pre>${escapeHtml(content)}</pre>`;
    }
    hide(editor);
    show(codeView);
    btn.textContent = 'Edit';
    btn.dataset.mode = 'view';
    // Hide edit-only actions in view mode
    hide(qs('d-save-btn'));
  }
}

// ── B: Resizable results panel divider ──
(function initResizeDivider() {
  const divider = qs('results-divider');
  if (!divider) return;
  const layout = document.querySelector('.results-layout');
  const panel = qs('results-panel');
  let startX = 0, startW = 0;

  divider.addEventListener('mousedown', e => {
    e.preventDefault();
    startX = e.clientX;
    startW = panel.offsetWidth;
    divider.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  function onMove(e) {
    const newW = Math.max(250, Math.min(startW + (e.clientX - startX), window.innerWidth * 0.6));
    layout.style.setProperty('--results-width', newW + 'px');
  }

  function onUp() {
    divider.classList.remove('dragging');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }
})();

// ── B2: Resizable sources sidebar divider ──
const _SOURCES_WIDTH_LS_KEY = 'memtomem.sources_width';
(function initSourcesDivider() {
  const divider = qs('sources-divider');
  if (!divider) return;
  const layout = document.querySelector('.sources-layout');
  const sidebar = document.querySelector('.sources-sidebar');
  let startX = 0, startW = 0;
  let lastW = 0;

  // Restore persisted width on load so the user's preferred sidebar size
  // sticks across tab switches and reloads.
  try {
    const stored = parseInt(localStorage.getItem(_SOURCES_WIDTH_LS_KEY) || '', 10);
    if (Number.isFinite(stored) && stored >= 200) {
      layout.style.setProperty('--sources-width', stored + 'px');
    }
  } catch (_err) { /* private mode */ }

  divider.addEventListener('mousedown', e => {
    e.preventDefault();
    startX = e.clientX;
    startW = sidebar.offsetWidth;
    divider.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  function onMove(e) {
    const newW = Math.max(200, Math.min(startW + (e.clientX - startX), window.innerWidth * 0.6));
    layout.style.setProperty('--sources-width', newW + 'px');
    lastW = newW;
  }

  function onUp() {
    divider.classList.remove('dragging');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    if (lastW) {
      try { localStorage.setItem(_SOURCES_WIDTH_LS_KEY, String(Math.round(lastW))); }
      catch (_err) { /* ignore */ }
    }
  }
})();

// ── D2: Diff toggle ──
qs('d-diff-btn').addEventListener('click', () => {
  const btn = qs('d-diff-btn');
  const isShowing = btn.dataset.mode === 'diff';
  if (isShowing) {
    hide(qs('d-diff'));
    show(qs('d-editor'));
    btn.textContent = 'Diff';
    btn.dataset.mode = 'source';
  } else {
    const ops = diffLines(STATE.selectedOriginal, qs('d-editor').value);
    qs('d-diff').innerHTML = renderDiff(ops);
    hide(qs('d-editor'));
    show(qs('d-diff'));
    btn.textContent = 'Edit';
    btn.dataset.mode = 'diff';
  }
});

qs('d-editor').addEventListener('input', () => {
  const diffBtn = qs('d-diff-btn');
  const changed = qs('d-editor').value !== STATE.selectedOriginal;
  if (changed) {
    show(diffBtn);
  } else {
    hide(diffBtn);
    if (diffBtn.dataset.mode === 'diff') {
      hide(qs('d-diff'));
      show(qs('d-editor'));
      diffBtn.textContent = 'Diff';
      diffBtn.dataset.mode = 'source';
    }
  }
});

// ── D1: Bulk select ──
qs('bulk-select-all').addEventListener('change', () => {
  const checked = qs('bulk-select-all').checked;
  const checkboxes = document.querySelectorAll('.result-checkbox');
  checkboxes.forEach(cb => {
    cb.checked = checked;
    if (checked) STATE.selectedIds.add(cb.dataset.id);
    else STATE.selectedIds.delete(cb.dataset.id);
  });
  updateBulkToolbar(checkboxes.length);
});

qs('bulk-delete-btn').addEventListener('click', async () => {
  const ids = [...STATE.selectedIds];
  if (!ids.length) return;
  const ok = await showConfirm({
    title: t('confirm.bulk_delete_title', { count: ids.length }),
    message: t('confirm.bulk_delete_msg', { count: ids.length }),
    confirmText: t('common.delete'),
  });
  if (!ok) return;
  const btn = qs('bulk-delete-btn');
  btnLoading(btn, true);
  let deleted = 0, failed = 0;
  for (const id of ids) {
    try { await api('DELETE', `/api/chunks/${id}`); deleted++; }
    catch (_) { failed++; }
  }
  btnLoading(btn, false);
  const msg = failed
    ? t('toast.bulk_delete_partial', { deleted, failed })
    : t('toast.bulk_delete_ok', { count: deleted });
  showToast(msg, failed ? 'error' : 'success');
  STATE.selectedIds.clear();
  updateBulkToolbar(0);
  clearDetail();
  _markDataStale();
  doSearch();
  loadStats();
});

// ---------------------------------------------------------------------------
// Tag Editor
// ---------------------------------------------------------------------------

let currentTags = [];

function renderTagChips(tags) {
  currentTags = [...tags];
  const container = qs('d-tag-chips');
  container.innerHTML = '';
  if (!tags.length) {
    container.innerHTML = `<span class="tag-empty-hint">${escapeHtml(t('search.tags_empty_hint'))}</span>`;
    return;
  }
  currentTags.forEach((tag, idx) => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.style.cursor = 'pointer';
    chip.innerHTML = `${escapeHtml(tag)}<button class="tag-chip-remove" data-idx="${idx}" title="${escapeAttr(t('search.tag_remove_title'))}">✕</button>`;
    chip.querySelector('.tag-chip-remove').addEventListener('click', async () => {
      if (!STATE.selectedChunkId) return;
      currentTags.splice(idx, 1);
      renderTagChips(currentTags);
      _syncResultTags(STATE.selectedChunkId, [...currentTags]);
      STATE.tagsTabStale = true;
      if (qs('tag-filter').value === tag) qs('tag-filter').value = '';
      try {
        await api('PATCH', `/api/chunks/${STATE.selectedChunkId}/tags`, { tags: [...currentTags] });
      } catch (err) {
        showToast(t('toast.tag_remove_failed', { error: err.message }), 'error');
      }
    });
    chip.addEventListener('click', e => {
      if (e.target.closest('.tag-chip-remove')) return;
      qs('tag-filter').value = tag;
      doSearch();
    });
    container.appendChild(chip);
  });
}

function addTagFromInput() {
  const input = qs('d-tag-input');
  const val = input.value.trim();
  if (!val) return;
  if (!currentTags.includes(val)) {
    currentTags.push(val);
    renderTagChips(currentTags);
  }
  input.value = '';
}

qs('d-tag-add-btn').addEventListener('click', addTagFromInput);
qs('d-tag-input').addEventListener('keydown', e => { if (e.key === 'Enter') addTagFromInput(); });

qs('d-tag-save-btn').addEventListener('click', async () => {
  if (!STATE.selectedChunkId) return;
  const btn = qs('d-tag-save-btn');
  btnLoading(btn, true);
  try {
    const data = await api('PATCH', `/api/chunks/${STATE.selectedChunkId}/tags`, { tags: currentTags });
    renderTagChips(data.tags);
    _syncResultTags(STATE.selectedChunkId, data.tags);
    STATE.tagsTabStale = true;
    showToast(t('toast.tags_saved'), 'success');
  } catch (err) {
    showToast(t('toast.tag_save_failed', { error: err.message }), 'error');
  } finally {
    btnLoading(btn, false);
  }
});

// ---------------------------------------------------------------------------
// Sources (D: filter + directory tree view)
// ---------------------------------------------------------------------------

// STATE.allSources, STATE.sourcesSortBy now in STATE
let _sourcesBodyFilterTimer = null;
let _sourcesBodyFilterAbort = null;
let _sourcesBodyFilterSeq = 0;

function sortSources(sources) {
  const sorted = [...sources];
  switch (STATE.sourcesSortBy) {
    case 'chunks':
      sorted.sort((a, b) => (b.chunk_count || 0) - (a.chunk_count || 0));
      break;
    case 'size':
      sorted.sort((a, b) => (b.file_size || 0) - (a.file_size || 0));
      break;
    case 'recent':
      sorted.sort((a, b) => {
        const ta = a.last_indexed_at ? new Date(a.last_indexed_at).getTime() : 0;
        const tb = b.last_indexed_at ? new Date(b.last_indexed_at).getTime() : 0;
        return tb - ta;
      });
      break;
    default: // name
      sorted.sort((a, b) => a.path.localeCompare(b.path));
  }
  return sorted;
}

function _sourceMatchesLocalFilter(s, q) {
  return [
    s.path,
    s.title,
    s.excerpt,
    s.ai_summary,
    ...((s.namespaces || [])),
  ].some(value => String(value || '').toLowerCase().includes(q));
}

function _sourcesFilterHighlightQuery() {
  return qs('sources-filter') ? qs('sources-filter').value.trim() : '';
}

function _getFilteredSorted() {
  const q = qs('sources-filter').value.trim().toLowerCase();
  const bodyMatches = q && STATE.sourcesBodyFilterQuery === q && STATE.sourcesBodyFilterPaths instanceof Set
    ? STATE.sourcesBodyFilterPaths
    : null;
  let filtered = q
    ? STATE.allSources.filter(s => _sourceMatchesLocalFilter(s, q) || (bodyMatches && bodyMatches.has(s.path)))
    : STATE.allSources;
  if (STATE.sourcesNsFilter) {
    filtered = filtered.filter(s => (s.namespaces || []).includes(STATE.sourcesNsFilter));
  }
  return sortSources(filtered);
}

function _scheduleSourcesBodyFilter(opts = {}) {
  const q = qs('sources-filter').value.trim().toLowerCase();
  if (_sourcesBodyFilterTimer) clearTimeout(_sourcesBodyFilterTimer);
  if (!q) {
    STATE.sourcesBodyFilterQuery = '';
    STATE.sourcesBodyFilterPaths = null;
    STATE.sourcesBodyFilterPending = false;
    if (_sourcesBodyFilterAbort) _sourcesBodyFilterAbort.abort();
    return;
  }
  if (STATE.sourcesBodyFilterQuery === q && STATE.sourcesBodyFilterPaths instanceof Set) return;
  STATE.sourcesBodyFilterQuery = q;
  STATE.sourcesBodyFilterPaths = null;
  STATE.sourcesBodyFilterPending = true;
  const delay = opts.immediate ? 0 : 180;
  _sourcesBodyFilterTimer = setTimeout(() => _loadSourcesBodyMatches(q), delay);
}

async function _loadSourcesBodyMatches(q) {
  const seq = ++_sourcesBodyFilterSeq;
  if (_sourcesBodyFilterAbort) _sourcesBodyFilterAbort.abort();
  _sourcesBodyFilterAbort = new AbortController();
  try {
    const resp = await api(
      'GET',
      `/api/sources/content-matches?q=${encodeURIComponent(q)}&limit=10000`,
      undefined,
      { signal: _sourcesBodyFilterAbort.signal },
    );
    if (seq !== _sourcesBodyFilterSeq || qs('sources-filter').value.trim().toLowerCase() !== q) return;
    STATE.sourcesBodyFilterQuery = q;
    STATE.sourcesBodyFilterPaths = new Set((resp && resp.paths) || []);
    STATE.sourcesBodyFilterPending = false;
    renderSourceTree(_getFilteredSorted());
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    STATE.sourcesBodyFilterPending = false;
    console.warn('[sources-filter] body match lookup failed', err);
  }
}

function _renderSourcesNsChip() {
  const chip = document.getElementById('sources-ns-chip');
  if (!chip) return;
  if (STATE.sourcesNsFilter) {
    chip.innerHTML = `<span class="sources-ns-chip">ns: ${escapeHtml(STATE.sourcesNsFilter)} <button class="sources-ns-chip-clear" title="Clear filter">\u2715</button></span>`;
    chip.querySelector('.sources-ns-chip-clear').addEventListener('click', () => {
      STATE.sourcesNsFilter = '';
      _renderSourcesNsChip();
      renderSourceTree(_getFilteredSorted());
    });
    chip.hidden = false;
  } else {
    chip.innerHTML = '';
    chip.hidden = true;
  }
}

// ---- AI summary language-drift banner -----------------------------------
// Shown above the source list when one or more cached AI summaries are in
// a language that doesn't match the current ``summary_language`` setting.
// Two actions: bulk regenerate (kicks off the background job + polls) or
// dismiss-for-session (sessionStorage, doesn't persist across reloads so
// users see it again next session if drift is still present).

function _renderLanguageDriftBanner(drift) {
  const banner = document.getElementById('sources-language-drift');
  if (!banner) return;
  if (!drift || !drift.count) {
    banner.hidden = true;
    banner.innerHTML = '';
    return;
  }
  if (sessionStorage.getItem('summaryDriftDismissed') === '1') {
    banner.hidden = true;
    banner.innerHTML = '';
    return;
  }
  // Non-translated fallback strings ship as the source of truth — the
  // i18n loader replaces them once locales are wired (see locales/*.json).
  const tFn = (typeof t === 'function') ? t : ((_, fb) => fb);
  const msg = tFn(
    'sources.language_drift_banner',
    `⚠️ ${drift.count} summaries don't match your language setting (${drift.current_setting})`,
  ).replace('{count}', drift.count).replace('{current}', drift.current_setting);
  const regenLabel = tFn('sources.regenerate_all_btn', 'Regenerate all');
  const laterLabel = tFn('sources.regenerate_later_btn', 'Later');
  banner.innerHTML = `
    <span class="source-language-drift-msg">${escapeHtml(msg)}</span>
    <button class="btn-primary btn-xs source-drift-regen-btn">${escapeHtml(regenLabel)}</button>
    <button class="btn-ghost btn-xs source-drift-later-btn">${escapeHtml(laterLabel)}</button>
  `;
  banner.hidden = false;
  banner.querySelector('.source-drift-regen-btn').addEventListener('click', _onRegenerateSummariesClicked);
  banner.querySelector('.source-drift-later-btn').addEventListener('click', () => {
    sessionStorage.setItem('summaryDriftDismissed', '1');
    banner.hidden = true;
    banner.innerHTML = '';
  });
}

async function _onRegenerateSummariesClicked() {
  const banner = document.getElementById('sources-language-drift');
  if (!banner) return;
  const tFn = (typeof t === 'function') ? t : ((_, fb) => fb);
  // Disable both buttons while the request is in flight to avoid double
  // POST. The polling loop replaces the button row with a progress chip.
  banner.querySelectorAll('button').forEach(b => (b.disabled = true));
  try {
    const resp = await api('POST', '/api/sources/regenerate-summaries');
    const total = (resp && resp.total) || 0;
    if (total === 0) {
      // Nothing to do — server already cleared its drift list. Refresh
      // the source view so the banner disappears too.
      showToast(tFn('sources.regenerate_done', 'Summaries regenerated.').replace('{processed}', 0), 'success');
      sessionStorage.removeItem('summaryDriftDismissed');
      await loadSources();
      return;
    }
    _pollRegenerateStatus();
  } catch (err) {
    const failLabel = tFn(
      'sources.regenerate_failed',
      'Failed to start regeneration: {error}',
    ).replace('{error}', (err && err.message) || '');
    showToast(failLabel, 'error');
    banner.querySelectorAll('button').forEach(b => (b.disabled = false));
  }
}

let _regenPollTimer = null;
async function _pollRegenerateStatus() {
  // Mirror of ``_indexingPollUntilIdle``: 500ms cadence, stops when the
  // server flips ``running`` to false. Updates the banner inline so users
  // can watch progress without leaving the Source tab.
  const banner = document.getElementById('sources-language-drift');
  const tFn = (typeof t === 'function') ? t : ((_, fb) => fb);
  const tick = async () => {
    try {
      const status = await api('GET', '/api/sources/regenerate-status');
      if (banner && !banner.hidden) {
        const progressLabel = tFn(
          'sources.regenerate_in_progress',
          'Regenerating {done}/{total}…',
        ).replace('{done}', status.done).replace('{total}', status.total);
        banner.innerHTML = `<span class="source-language-drift-msg">${escapeHtml(progressLabel)}</span>`;
      }
      if (!status.running) {
        if (_regenPollTimer) {
          clearInterval(_regenPollTimer);
          _regenPollTimer = null;
        }
        const doneLabel = tFn('sources.regenerate_done', 'Regenerated {processed} summaries.')
          .replace('{processed}', status.done);
        showToast(doneLabel, 'success');
        sessionStorage.removeItem('summaryDriftDismissed');
        await loadSources();
      }
    } catch (err) {
      // Transient error — keep polling unless it persists; another tick
      // will retry. Don't dismiss the banner so the user can see it stuck.
      console.warn('regenerate-status poll failed', err);
    }
  };
  if (_regenPollTimer) clearInterval(_regenPollTimer);
  _regenPollTimer = setInterval(tick, 500);
  tick();
}

function navigateToSourcesByNs(nsName) {
  STATE.sourcesNsFilter = nsName;
  activateTab('sources');
}

qs('sources-filter').addEventListener('input', () => {
  _scheduleSourcesBodyFilter();
  renderSourceTree(_getFilteredSorted());
});

document.querySelectorAll('.sources-sort-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.sources-sort-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    STATE.sourcesSortBy = btn.dataset.sort;
    renderSourceTree(_getFilteredSorted());
  });
});

// ── Memory mode header actions ──
// Inline +Add path form toggle, submit/cancel, Reindex all. Wired here
// (rather than inside ``sources-memory-dirs.js``) so the file can stay
// focused on per-row helpers while DOM event wiring lives in one place.
(function _wireMemoryHeaderActions() {
  const addBtn = qs('memory-add-path-btn');
  const reindexAllBtn = qs('memory-reindex-all-btn');
  const addRow = qs('memory-add-row');
  const addInput = qs('memory-add-input');
  const addSubmit = qs('memory-add-submit');
  const addCancel = qs('memory-add-cancel');
  if (addBtn && addRow && addInput) {
    addBtn.addEventListener('click', () => {
      addRow.hidden = !addRow.hidden;
      if (!addRow.hidden) addInput.focus();
    });
  }
  if (addSubmit && addInput) {
    addSubmit.addEventListener('click', async () => {
      const val = addInput.value;
      addInput.value = '';
      if (addRow) addRow.hidden = true;
      if (typeof mdAdd === 'function') await mdAdd(val);
    });
    addInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); addSubmit.click(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); if (addCancel) addCancel.click(); }
    });
  }
  if (addCancel && addRow && addInput) {
    addCancel.addEventListener('click', () => {
      addInput.value = '';
      addRow.hidden = true;
    });
  }
  if (reindexAllBtn) {
    reindexAllBtn.addEventListener('click', () => {
      if (typeof mdReindexAll === 'function') mdReindexAll(reindexAllBtn);
    });
  }
})();

// ── Sources vendor sub-tabs ──
// Issue #570: only one vendor's tree renders at a time. The tab strip
// (``#sources-vendor-tabs``) drives ``STATE.sourcesActiveVendor`` which
// scopes ``_renderMemorySourceTree`` to a single vendor.
const _SOURCES_VENDORS = ['user', 'claude', 'openai'];
const _SOURCES_VENDOR_LS = 'memtomem.sources.active_vendor';
const _SOURCES_VENDOR_DEFAULT = 'user';

function _readActiveSourcesVendor() {
  try {
    const v = localStorage.getItem(_SOURCES_VENDOR_LS);
    if (v && _SOURCES_VENDORS.includes(v)) return v;
  } catch (_err) { /* localStorage may be unavailable in private modes */ }
  return _SOURCES_VENDOR_DEFAULT;
}

function _writeActiveSourcesVendor(vendor) {
  try { localStorage.setItem(_SOURCES_VENDOR_LS, vendor); }
  catch (_err) { /* best-effort persistence */ }
}

// Update tab DOM (active class, aria-selected, roving tabindex) without
// re-rendering the tree. Called from ``_renderMemorySourceTree`` on the
// first lazy resolve and from ``_setActiveSourcesVendor`` on user click /
// arrow nav.
function _syncSourcesVendorTabs(activeVendor) {
  document.querySelectorAll('.sources-vendor-tab').forEach(btn => {
    const isActive = btn.dataset.vendor === activeVendor;
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    btn.tabIndex = isActive ? 0 : -1;
  });
}

function _setActiveSourcesVendor(vendor) {
  if (!_SOURCES_VENDORS.includes(vendor)) return;
  if (STATE.sourcesActiveVendor === vendor) return;
  STATE.sourcesActiveVendor = vendor;
  _writeActiveSourcesVendor(vendor);
  _syncSourcesVendorTabs(vendor);
  // ``_getFilteredSorted`` already covers the filter + sort axes; the
  // active vendor is read from STATE inside the render itself.
  if (typeof renderSourceTree === 'function') renderSourceTree(_getFilteredSorted());
}

(function _wireSourcesVendorTabs() {
  const tabs = document.getElementById('sources-vendor-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.sources-vendor-tab');
    if (!btn || !btn.dataset.vendor) return;
    _setActiveSourcesVendor(btn.dataset.vendor);
    btn.focus();
  });
  // Auto-activation arrow nav — same model as the main tablist
  // (``focus + activate``) so a keyboard user toggles vendors by
  // walking the strip without an extra Enter press.
  tabs.addEventListener('keydown', (e) => {
    if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
    const buttons = Array.from(tabs.querySelectorAll('.sources-vendor-tab'));
    const currentIdx = buttons.indexOf(document.activeElement);
    const nextIdx = _arrowNavIndex(buttons.length, currentIdx === -1 ? 0 : currentIdx, e.key);
    if (nextIdx < 0) return;
    e.preventDefault();
    const next = buttons[nextIdx];
    next.focus();
    if (next.dataset.vendor) _setActiveSourcesVendor(next.dataset.vendor);
  });
})();

async function loadSources() {
  // Single-panel Sources: vendor grouping (사용자 / Claude / Codex) is the
  // only classification axis. We pull every memory_dir's status and every
  // indexed source file regardless of backend ``kind`` so user-added dirs
  // that classify as ``general`` still surface their file rows under their
  // vendor group.
  const list = qs('sources-list');
  panelLoading(list);
  try {
    const [statusResp, sourcesResp] = await Promise.all([
      api('GET', '/api/memory-dirs/status'),
      api('GET', '/api/sources?limit=10000'),
    ]);
    const statusByPath = {};
    for (const entry of (statusResp && statusResp.dirs) || []) {
      if (entry && typeof entry.path === 'string') statusByPath[entry.path] = entry;
    }
    STATE.memoryStatusByPath = statusByPath;
    STATE.memoryDirs = (STATE.serverConfig?.indexing?.memory_dirs) || Object.keys(statusByPath);
    STATE.allSources = (sourcesResp && sourcesResp.sources) || [];
    STATE.sourcesLanguageDrift = (sourcesResp && sourcesResp.language_drift) || null;
    _renderSourcesNsChip();
    _renderLanguageDriftBanner(STATE.sourcesLanguageDrift);
    _scheduleSourcesBodyFilter({ immediate: true });
    renderSourceTree(_getFilteredSorted());
  } catch (err) {
    list.innerHTML = `<div class="empty-state"><p>Error: ${escapeHtml(err.message)}</p></div>`;
  }
}

function renderSourceTree(sources) {
  const list = qs('sources-list');

  // Top summary sums the *indexed* portion of each memory_dir so the
  // figure matches what the user can actually click into below \u2014 and
  // avoids inflating the count with unindexed read-only-discovered dirs
  // whose ``file_count`` reflects disk-only files. Per-dir badges also
  // surface the ``indexed/files`` split, so this number lines up with
  // the left side of those badges.
  //
  // Scope: the sub-tab strip (issue #570) only renders one vendor's
  // tree at a time, so the stats line is scoped to that vendor too \u2014
  // a global "47 files \u00b7 442 chunks" sitting above a User-only sidebar
  // read as ambiguous (was that User's 20 or all three combined?).
  // The active-vendor scoping makes the number always match what the
  // user is currently looking at; global totals can be inferred by
  // summing the three sub-tab badges.
  // Stats compute is deferred to ``_renderMemorySourceTree`` so it can
  // observe the *post-auto-switch* active vendor. Reading
  // ``STATE.sourcesActiveVendor`` here would race the NS-filter
  // follow-through that shifts the sub-tab inside the tree pass \u2014 the
  // stats line would then briefly show the pre-switch vendor's totals
  // sitting above a post-switch tree (review feedback on PR #673).
  _renderMemorySourceTree(sources, list);
}

function _renderSourcesStats(activeVendor) {
  const statsEl = qs('sources-stats');
  if (!statsEl) return;
  const statusByPath = STATE.memoryStatusByPath || {};
  let indexedFiles = 0;
  let totalChunks = 0;
  for (const s of Object.values(statusByPath)) {
    if (!s || s.exists === false) continue;
    // Same fallback rule as ``_renderMemorySourceTree``: unknown
    // providers (forward-compat for a server that adds a vendor before
    // the client deploys) bucket into ``user``.
    const rawProvider = s.provider;
    const provider = (_SOURCES_VENDORS && _SOURCES_VENDORS.includes(rawProvider))
      ? rawProvider : 'user';
    if (provider !== activeVendor) continue;
    indexedFiles += s.source_file_count || 0;
    totalChunks += s.chunk_count || 0;
  }
  if (indexedFiles || totalChunks) {
    statsEl.textContent = t('header.stat_files_chunks', {
      files: indexedFiles,
      chunks: totalChunks.toLocaleString(),
    });
    statsEl.hidden = false;
  } else {
    statsEl.hidden = true;
  }
}


function hideBrowser() {
  hide(qs('chunks-browser-content'));
  const browser = qs('chunks-browser');
  browser.innerHTML = emptyState('📄', 'Select a source to browse its chunks');
}

// ── Memory-mode source tree ─────────────────────────────────────────
// Renders the same ``sources-list`` container in two layers:
//   1. ``<details class="source-vendor-group">`` per vendor
//      (user / claude / openai), keyed off the ``provider`` field on
//      ``GET /api/memory-dirs/status``.
//   2. ``.source-group`` per ``memory_dir`` inside that vendor, with
//      the same chevron header used by General mode plus three
//      hover-revealed actions on the right edge: Open · Reindex ·
//      Remove. Each source-group's children are file cards (the same
//      ``.source-item`` shape General uses) so the file-click → chunks
//      drill-in flow is identical in both modes.
//
// Vendor / category constants come from ``sources-memory-dirs.js``
// (underscore-prefixed module-level ``const`` — globals at script
// scope since that file loads after app.js).
function _renderMemorySourceTree(sources, list) {
  const memDirs = STATE.memoryDirs || [];
  const statusByPath = STATE.memoryStatusByPath || {};

  // Orphan = indexed source whose ``memory_dir`` is null on the
  // ``/api/sources`` row. Two paths feed into this bucket:
  //   1. Index tab uploads land in ``~/.memtomem/uploads/`` (see
  //      ``system.py:upload_files``), which isn't a configured
  //      ``memory_dir`` — server returns ``memory_dir=null, kind=null``.
  //   2. A configured dir was removed from ``memory_dirs`` after its
  //      files were indexed; the chunks survive but no longer have an
  //      owning dir. Server-side intent (sources.py: "orphans ride
  //      along with general") is to surface these so users can find and
  //      prune them — without the bucket below they vanish entirely.
  // Rendered as a collapsed sub-section under the ``user`` vendor since
  // both feed paths are user-driven.
  const sourcesByDir = {};
  const orphanItems = [];
  for (const s of sources) {
    if (!s.memory_dir) {
      orphanItems.push(s);
      continue;
    }
    const key = s.memory_dir;
    if (!sourcesByDir[key]) sourcesByDir[key] = [];
    sourcesByDir[key].push(s);
  }

  // Hide dirs whose files are all filtered out so a path/namespace
  // filter doesn't leave empty "Claude (0)" rows behind. The NS filter
  // matters here because clicking a namespace card's Sources button
  // routes through ``navigateToSourcesByNs`` — without this branch the
  // tree keeps every dir header and only their file children disappear,
  // which reads as "the link did nothing" when most dirs have zero
  // matches.
  const filterActive = !!(qs('sources-filter') && qs('sources-filter').value.trim())
    || !!STATE.sourcesNsFilter;
  const bodyFilterPending = !!(qs('sources-filter') && qs('sources-filter').value.trim())
    && STATE.sourcesBodyFilterPending;
  const fileSortMode = ['chunks', 'size', 'recent'].includes(STATE.sourcesSortBy);

  const PROVIDER_ORDER = (typeof _MEMORY_DIR_PROVIDER_ORDER !== 'undefined')
    ? _MEMORY_DIR_PROVIDER_ORDER : ['user', 'claude', 'openai'];
  const PROVIDER_LABEL_KEY = (typeof _MEMORY_DIR_PROVIDER_LABEL_KEY !== 'undefined')
    ? _MEMORY_DIR_PROVIDER_LABEL_KEY : {};
  const CATEGORY_ORDER = (typeof _MEMORY_DIR_CATEGORY_ORDER !== 'undefined')
    ? _MEMORY_DIR_CATEGORY_ORDER : ['user', 'claude-memory', 'claude-plans', 'codex'];
  const CATEGORY_LABEL_KEY = (typeof _MEMORY_DIR_CATEGORY_LABEL_KEY !== 'undefined')
    ? _MEMORY_DIR_CATEGORY_LABEL_KEY : {};
  const CATEGORY_TO_PROVIDER = {
    'user': 'user',
    'claude-memory': 'claude',
    'claude-plans': 'claude',
    'codex': 'openai',
  };
  const PROVIDER_COLLAPSED = (typeof _MEMORY_DIR_PROVIDER_COLLAPSED !== 'undefined')
    ? _MEMORY_DIR_PROVIDER_COLLAPSED : new Set(['claude', 'openai']);
  const CATEGORY_PAGE_SIZE = 10;
  const CLAUDE_PROJECT_TREE_MAX_DEPTH = 2;
  const TREE_DEFAULT_OPEN_DEPTH = 1;
  if (!STATE.sourcesCategoryLimits || typeof STATE.sourcesCategoryLimits !== 'object') {
    STATE.sourcesCategoryLimits = {};
  }
  if (!STATE.sourcesExpandedDirs || typeof STATE.sourcesExpandedDirs !== 'object') {
    STATE.sourcesExpandedDirs = {};
  }
  if (!STATE.sourcesActiveCategoryByVendor || typeof STATE.sourcesActiveCategoryByVendor !== 'object') {
    STATE.sourcesActiveCategoryByVendor = {};
  }
  const categoryLimitKey = (provider, cat) => `${provider}:${cat}`;
  const getCategoryLimit = (provider, cat) => (
    Math.max(CATEGORY_PAGE_SIZE, STATE.sourcesCategoryLimits[categoryLimitKey(provider, cat)] || CATEGORY_PAGE_SIZE)
  );
  const setCategoryLimit = (provider, cat, limit) => {
    STATE.sourcesCategoryLimits[categoryLimitKey(provider, cat)] = Math.max(CATEGORY_PAGE_SIZE, limit);
  };
  const normalizeDirPath = (path) => String(path || '').replace(/\\/g, '/').replace(/\/+$/, '');
  const presentationCategoryForDir = (dir, st) => {
    const normalized = normalizeDirPath(dir);
    if (/\/\.claude\/plans(?:\/|$)/.test(normalized)) return 'claude-plans';
    if (/\/\.claude\/projects\/[^/]+\/memory$/.test(normalized)) return 'claude-memory';
    return (st && CATEGORY_LABEL_KEY[st.category]) ? st.category : 'user';
  };
  const presentationProviderForDir = (dir, st) => {
    const cat = presentationCategoryForDir(dir, st);
    const providerFromCat = CATEGORY_TO_PROVIDER[cat];
    if (providerFromCat) return providerFromCat;
    const rawProvider = st && st.provider;
    return PROVIDER_ORDER.includes(rawProvider) ? rawProvider : 'user';
  };

  const byProvider = {};
  for (const p of PROVIDER_ORDER) byProvider[p] = { order: [], byCategory: {} };
  // Union of configured dirs and dirs that show up in the sources
  // response — covers orphaned chunks whose memory_dir was unset in
  // config but still has indexed rows.
  const allDirs = new Set(memDirs);
  for (const k of Object.keys(sourcesByDir)) if (k) allDirs.add(k);
  for (const d of allDirs) {
    const st = statusByPath[d];
    const cat = presentationCategoryForDir(d, st);
    const provider = presentationProviderForDir(d, st);
    const bucket = byProvider[provider];
    if (!bucket.byCategory[cat]) {
      bucket.byCategory[cat] = [];
      bucket.order.push(cat);
    }
    bucket.byCategory[cat].push(d);
  }

  list.innerHTML = '';
  const maxChunks = Math.max(1, ...sources.map(s => s.chunk_count || 0));

  // "Discovered" = memory dirs that are configured (or auto-detected)
  // but have no chunks yet — the user hasn't indexed them. We group
  // these as a collapsed sub-section under their vendor so the indexed
  // cards stay primary and the candidates read as "ready to index", not
  // "errors". A dir with ``exists === false`` stays in the main list as
  // ``.missing`` (a real error).
  const isDiscovered = (path) => {
    const st = statusByPath[path];
    if (!st || st.exists === false) return false;
    const chunks = st.chunk_count || 0;
    const files = (typeof st.file_count === 'number') ? st.file_count : 0;
    return chunks === 0 && files > 0;
  };

  // For the "user" vendor, the first entry in ``memory_dirs`` config is
  // the conventional primary memory dir — pin it to the top so the most
  // common destination doesn't get sorted under a longer alphabetical
  // sibling.
  const defaultDir = (STATE.serverConfig?.indexing?.memory_dirs || [])[0];
  const sortUserDirs = (dirs) => {
    if (!defaultDir) return dirs;
    const idx = dirs.indexOf(defaultDir);
    if (idx <= 0) return dirs;
    const out = dirs.slice();
    out.splice(idx, 1);
    out.unshift(defaultDir);
    return out;
  };

  // Pass 1: compute per-vendor stats. The sidebar only renders the
  // active vendor's content (issue #570) but every vendor's count
  // populates its sub-tab badge so the user can see at a glance which
  // vendors hold matches before clicking. ``PROVIDER_COLLAPSED`` no
  // longer drives an outer ``<details>`` collapse — the sub-tab strip
  // *is* the disclosure now — but the constant stays imported in case
  // a future view (e.g. an "all vendors" mode) wants it.
  void PROVIDER_COLLAPSED;
  const vendorPlans = {};
  for (const provider of PROVIDER_ORDER) {
    const bucket = byProvider[provider];

    const categoriesAll = CATEGORY_ORDER.filter(c => bucket.byCategory[c]);
    // Filter narrows indexed dirs (chunks>0) by source matches, but always
    // keeps "Discovered" dirs (chunks=0 && files>0) regardless of filter
    // state — without this branch, any NS/path filter ever set on the page
    // makes all Discovered dirs vanish at once (the original ``Claude (0)``
    // guard was scoped to indexed-empty rows, not Discovered ones).
    const visibleCatsRaw = filterActive
      ? categoriesAll
          .map(cat => [cat, bucket.byCategory[cat].filter(d => (sourcesByDir[d] || []).length > 0 || isDiscovered(d))])
          .filter(([, dirs]) => dirs.length > 0)
      : categoriesAll.map(cat => [cat, bucket.byCategory[cat]]);

    // Partition each category's dirs into indexed (default first for
    // user provider) and discovered (read-only, rendered as a collapsed
    // sub-section).
    const visibleCats = visibleCatsRaw.map(([cat, dirs]) => {
      const indexed = dirs.filter(d => !isDiscovered(d));
      const discovered = dirs.filter(d => isDiscovered(d));
      const indexedSorted = (provider === 'user') ? sortUserDirs(indexed) : indexed;
      return [cat, indexedSorted, discovered];
    });

    // Orphan rows ride along with the ``user`` vendor (see comment on
    // ``orphanItems`` above for why). Counted toward both the sub-tab
    // badge and the vendor's emptiness check so a user with only
    // uploaded files sees a populated tab instead of "user memory not
    // found".
    const vendorOrphans = (provider === 'user') ? orphanItems : [];
    const isEmptyVendor = !visibleCats.some(([, indexed, discovered]) => indexed.length || discovered.length)
      && vendorOrphans.length === 0;
    const totalFiles = visibleCats.reduce(
      (sum, [, indexed]) => sum + indexed.reduce((s, d) => s + (sourcesByDir[d] || []).length, 0),
      0,
    ) + vendorOrphans.length;
    // Tracked separately from ``totalFiles`` so the sub-tab badge keeps its
    // "indexed + orphans" meaning while the empty-state guard further down
    // can still see discovered dirs (codex review on #896: a vendor with
    // only Discovered dirs would otherwise hit the "No matches" fallback
    // because ``totalFiles`` is zero, hiding the section the carve-out in
    // ``visibleCatsRaw`` just preserved).
    const discoveredCount = visibleCats.reduce(
      (sum, [, , discovered]) => sum + discovered.length, 0,
    );
    vendorPlans[provider] = {
      visibleCats, isEmptyVendor, totalFiles, discoveredCount,
      orphans: vendorOrphans,
    };

    // Update the sub-tab badge + empty class so all three vendor tabs
    // reflect current state, not just the active one.
    const countEl = document.querySelector(`[data-vendor-count="${provider}"]`);
    if (countEl) {
      countEl.textContent = String(totalFiles);
      // Hide a "0" badge so the tab label stays uncluttered when the
      // vendor has no indexed files. The ``sources-vendor-tab-empty``
      // class still signals empty-via-color, so we don't lose the cue.
      countEl.hidden = totalFiles === 0;
    }
    const tabBtn = document.querySelector(`.sources-vendor-tab[data-vendor="${provider}"]`);
    if (tabBtn) tabBtn.classList.toggle('sources-vendor-tab-empty', isEmptyVendor);
  }

  // Resolve the active vendor. First render after page load reads
  // localStorage; subsequent renders read STATE so filter/sort/reindex
  // re-renders don't re-hit storage on the hot path.
  let activeVendor = STATE.sourcesActiveVendor;
  if (!activeVendor || !PROVIDER_ORDER.includes(activeVendor)) {
    activeVendor = _readActiveSourcesVendor();
    STATE.sourcesActiveVendor = activeVendor;
    _syncSourcesVendorTabs(activeVendor);
  }

  // NS-filter follow-through: a click on a namespace card's "Sources"
  // button (settings-namespaces.js → navigateToSourcesByNs) sets
  // ``sourcesNsFilter`` and switches to the Sources tab, but the
  // active vendor stays at whatever the user last chose. For a
  // ``claude:…`` or ``codex:…`` namespace this lands the user on the
  // ``user`` tab where every dir is empty after filtering — chip says
  // "ns: claude:…" but the visible tree is unrelated, which reads as
  // "the link did nothing." When the current vendor has zero matches
  // we shift to the vendor with the most. Not persisted via
  // ``_writeActiveSourcesVendor`` — this is a navigation hint, not the
  // user's stored preference.
  if (STATE.sourcesNsFilter) {
    const curPlan = vendorPlans[activeVendor];
    if (!curPlan || curPlan.totalFiles === 0) {
      // Tie-break leans on stable sort + ``PROVIDER_ORDER`` =
      // ``['user', 'claude', 'openai']``: when two vendors hold the
      // same number of NS matches, the earlier vendor in the order
      // wins. Intentional — ``user`` is the conventional primary
      // surface, so a tie shouldn't bury the user's own dirs behind
      // a less-frequented vendor.
      const best = PROVIDER_ORDER
        .map(p => [p, vendorPlans[p] ? vendorPlans[p].totalFiles : 0])
        .filter(([, n]) => n > 0)
        .sort((a, b) => b[1] - a[1])[0];
      if (best) {
        activeVendor = best[0];
        STATE.sourcesActiveVendor = activeVendor;
        _syncSourcesVendorTabs(activeVendor);
      }
    }
  }

  // Stats line uses the *post-auto-switch* vendor so the "N files ·
  // M chunks" caption above the tree always matches what's rendered
  // below. Doing this in the caller (``renderSourceTree``) would race
  // the NS-filter follow-through that mutates ``activeVendor`` here.
  _renderSourcesStats(activeVendor);

  const dirOpenStateKey = (provider, cat, dir) => `${provider}:${cat}:${dir}`;
  const getDirOpen = (provider, cat, dir, defaultOpen) => {
    const key = dirOpenStateKey(provider, cat, dir);
    return Object.prototype.hasOwnProperty.call(STATE.sourcesExpandedDirs, key)
      ? !!STATE.sourcesExpandedDirs[key]
      : defaultOpen;
  };
  const setDirOpen = (provider, cat, dir, open) => {
    STATE.sourcesExpandedDirs[dirOpenStateKey(provider, cat, dir)] = !!open;
  };
  const treeOpenStateKey = (provider, cat, key) => `${provider}:${cat}:tree:${key}`;
  const getTreeOpen = (provider, cat, key, defaultOpen = false) => {
    const stateKey = treeOpenStateKey(provider, cat, key);
    return Object.prototype.hasOwnProperty.call(STATE.sourcesExpandedDirs, stateKey)
      ? !!STATE.sourcesExpandedDirs[stateKey]
      : defaultOpen;
  };
  const setTreeOpen = (provider, cat, key, open) => {
    STATE.sourcesExpandedDirs[treeOpenStateKey(provider, cat, key)] = !!open;
  };

  const renderDir = (dir, opts = {}) => _renderMemoryDirGroup(
    dir,
    sourcesByDir[dir] || [],
    statusByPath[dir],
    maxChunks,
    { isDefault: dir === defaultDir, ...opts },
  );

  const renderDiscoveredBlock = (dirs) => {
    if (!dirs.length) return null;
    const det = document.createElement('details');
    det.className = 'source-vendor-discovered';
    const sum = document.createElement('summary');
    sum.className = 'source-vendor-discovered-summary';
    const lbl = document.createElement('span');
    lbl.className = 'source-vendor-discovered-label';
    lbl.textContent = (typeof t === 'function') ? t('sources.discovered_label') : 'Discovered';
    sum.appendChild(lbl);
    const cnt = document.createElement('span');
    cnt.className = 'source-vendor-count';
    cnt.textContent = String(dirs.length);
    sum.appendChild(cnt);
    det.appendChild(sum);
    for (const d of dirs) det.appendChild(renderDir(d));
    return det;
  };

  // Orphan section: rows whose owning ``memory_dir`` is null on the
  // server response (Index tab uploads + chunks whose dir was removed
  // from config). Reuses the ``source-item`` card shape so file-click →
  // chunks drill-in works identically to indexed dirs. Rendered only
  // when ``activeVendor === 'user'`` because ``orphans`` is populated
  // exclusively for that vendor in the stats pass above.
  //
  // Bar normalisation: passes the outer ``maxChunks`` (tree-wide max,
  // line ~2477) rather than a local max so the chunk-bar widths stay
  // visually comparable to the indexed groups above — same chunk_count
  // → same bar length regardless of which sub-section a row sits in.
  const renderOrphanBlock = (items) => {
    if (!items.length) return null;
    const det = document.createElement('details');
    det.className = 'source-vendor-orphan';
    const sum = document.createElement('summary');
    sum.className = 'source-vendor-orphan-summary';
    const lbl = document.createElement('span');
    lbl.className = 'source-vendor-orphan-label';
    lbl.textContent = (typeof t === 'function') ? t('sources.orphan_label') : 'Other (unregistered)';
    sum.appendChild(lbl);
    const cnt = document.createElement('span');
    cnt.className = 'source-vendor-count';
    cnt.textContent = String(items.length);
    sum.appendChild(cnt);
    det.appendChild(sum);
    for (const s of items) det.appendChild(_renderMemorySourceItem(s, maxChunks));
    return det;
  };

  const categoryFileCount = (indexed) => (
    indexed.reduce((sum, d) => sum + (sourcesByDir[d] || []).length, 0)
  );

  const renderCategoryNav = (cats, activeCat) => {
    const visible = cats.filter(([, indexed, discovered]) => indexed.length || discovered.length);
    if (visible.length <= 1) return null;
    const nav = document.createElement('nav');
    nav.className = 'source-category-nav';
    nav.setAttribute(
      'aria-label',
      (typeof t === 'function') ? t('sources.category_nav_label') : 'Source categories',
    );
    for (const [cat, indexed, discovered] of visible) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'source-category-nav-btn';
      if (cat === activeCat) btn.classList.add('active');
      btn.dataset.category = cat;
      btn.setAttribute('aria-pressed', cat === activeCat ? 'true' : 'false');
      const label = document.createElement('span');
      label.className = 'source-category-nav-label';
      const key = CATEGORY_LABEL_KEY[cat] || cat;
      label.textContent = (typeof t === 'function') ? t(key) : key;
      btn.appendChild(label);
      const count = document.createElement('span');
      count.className = 'source-category-nav-count';
      count.textContent = String(categoryFileCount(indexed));
      btn.appendChild(count);
      btn.addEventListener('click', () => {
        STATE.sourcesActiveCategoryByVendor[activeVendor] = cat;
        renderSourceTree(_getFilteredSorted());
      });
      nav.appendChild(btn);
    }
    return nav;
  };

  const sliceIndexedFiles = (indexed, limit) => {
    const rendered = [];
    let shown = 0;
    for (const dir of indexed) {
      const items = sourcesByDir[dir] || [];
      if (!items.length) {
        rendered.push([dir, items]);
        continue;
      }
      if (shown >= limit) break;
      const remaining = limit - shown;
      const nextItems = items.slice(0, remaining);
      rendered.push([dir, nextItems]);
      shown += nextItems.length;
    }
    return { rendered, shown };
  };

  const sliceIndexedFolders = (indexed, limit) => {
    const rendered = indexed.slice(0, limit).map(dir => [dir, sourcesByDir[dir] || []]);
    return { rendered, shown: rendered.length };
  };

  const sliceIndexedFlatFiles = (indexed, limit) => {
    const dirSet = new Set(indexed);
    const rendered = sources
      .filter(s => s.memory_dir && dirSet.has(s.memory_dir))
      .slice(0, limit);
    return { rendered, shown: rendered.length };
  };

  const splitPathSegments = (path) => String(path || '')
    .replace(/\\/g, '/')
    .split('/')
    .filter(Boolean);

  const commonPrefixLength = (segmentLists) => {
    if (!segmentLists.length) return 0;
    let len = segmentLists[0].length;
    for (const segments of segmentLists.slice(1)) {
      let i = 0;
      while (i < len && i < segments.length && segments[i] === segmentLists[0][i]) i += 1;
      len = i;
    }
    return len;
  };

  const claudeProjectSegments = (dir) => {
    const match = String(dir || '').replace(/\\/g, '/').match(/\/\.claude\/projects\/([^/]+)\/memory\/?$/);
    if (!match) return null;
    const slug = match[1];
    if (!slug.startsWith('-')) return null;
    return slug.replace(/^-/, '').split('-').filter(Boolean);
  };

  const dirSegmentsForTree = (cat, dir) => {
    const claudeSegments = cat === 'claude-memory' ? claudeProjectSegments(dir) : null;
    if (claudeSegments && claudeSegments.length) return claudeSegments;
    const segments = splitPathSegments(dir);
    if (segments.length > 1 && segments[segments.length - 1] === 'memory') {
      return segments.slice(0, -1);
    }
    return segments.length ? segments : [String(dir || '')];
  };

  const limitTreeSegments = (cat, segments) => {
    if (cat !== 'claude-memory' || segments.length <= CLAUDE_PROJECT_TREE_MAX_DEPTH) return segments;
    const head = segments.slice(0, CLAUDE_PROJECT_TREE_MAX_DEPTH - 1);
    const tail = segments.slice(CLAUDE_PROJECT_TREE_MAX_DEPTH - 1).join('-');
    return [...head, tail].filter(Boolean);
  };

  const shouldPreserveTopTreeSegment = (cat, segments) => {
    if (segments.length <= 1) return false;
    if (cat === 'claude-memory') return segments.length > CLAUDE_PROJECT_TREE_MAX_DEPTH;
    return true;
  };

  const compactSingleChildChains = (node, joiner = '/') => {
    for (const child of Array.from(node.children.values())) compactSingleChildChains(child, joiner);
    if (node.preserveSelf) return;
    while (!node.leaf && node.children.size === 1) {
      const only = Array.from(node.children.values())[0];
      node.label = node.label ? `${node.label}${joiner}${only.label}` : only.label;
      node.key = only.key;
      node.children = only.children;
      node.leaf = only.leaf;
      node.preserveSelf = !!only.preserveSelf;
      if (node.preserveSelf) break;
    }
  };

  const buildDirTree = (cat, dirs, rendered) => {
    const allSegments = dirs.map(dir => dirSegmentsForTree(cat, dir));
    const prefixLen = commonPrefixLength(allSegments);
    const segmentByDir = new Map();
    dirs.forEach((dir, idx) => {
      let segments = allSegments[idx].slice(prefixLen);
      if (!segments.length) segments = [allSegments[idx][allSegments[idx].length - 1] || dir];
      const preserveTop = shouldPreserveTopTreeSegment(cat, segments);
      segments = limitTreeSegments(cat, segments);
      segmentByDir.set(dir, { segments, preserveTop });
    });
    const root = { label: '', key: '', children: new Map(), leaf: null };
    for (const [dir, items] of rendered) {
      const treeInfo = segmentByDir.get(dir) || { segments: [dir], preserveTop: false };
      const { segments, preserveTop } = treeInfo;
      let node = root;
      let key = '';
      segments.forEach((segment, idx) => {
        key = key ? `${key}/${segment}` : segment;
        if (!node.children.has(segment)) {
          node.children.set(segment, { label: segment, key, children: new Map(), leaf: null, preserveSelf: false });
        }
        node = node.children.get(segment);
        if (idx === 0 && preserveTop) node.preserveSelf = true;
      });
      node.leaf = { dir, items };
    }
    for (const child of Array.from(root.children.values())) {
      compactSingleChildChains(child, cat === 'claude-memory' ? '-' : '/');
    }
    return root;
  };

  const treeKeysForDir = (cat, dirs, dir) => {
    const allSegments = dirs.map(d => dirSegmentsForTree(cat, d));
    const prefixLen = commonPrefixLength(allSegments);
    const idx = dirs.indexOf(dir);
    if (idx < 0) return [];
    let segments = allSegments[idx].slice(prefixLen);
    if (!segments.length) segments = [allSegments[idx][allSegments[idx].length - 1] || dir];
    segments = limitTreeSegments(cat, segments);
    const keys = [];
    let key = '';
    for (const segment of segments) {
      key = key ? `${key}/${segment}` : segment;
      keys.push(key);
    }
    return keys.slice(0, -1);
  };

  const renderDirTreeNode = (node, provider, cat, depth) => {
    const wrap = document.createElement('div');
    wrap.className = 'source-dir-tree-node';
    wrap.style.setProperty('--tree-depth', String(depth));

    if (node.leaf) {
      const { dir, items } = node.leaf;
      const pendingInDir = !!STATE.pendingActivatePath
        && items.some(s => s.path === STATE.pendingActivatePath);
      wrap.appendChild(_renderMemoryDirGroup(
        dir,
        items,
        statusByPath[dir],
        maxChunks,
        {
          isDefault: dir === defaultDir,
          label: node.label,
          open: getDirOpen(provider, cat, dir, pendingInDir),
          onToggle: (open) => setDirOpen(provider, cat, dir, open),
        },
      ));
    }

    if (node.children.size) {
      const details = document.createElement('details');
      details.className = 'source-dir-tree-branch';
      details.style.setProperty('--tree-depth', String(depth));
      details.open = getTreeOpen(provider, cat, node.key, depth <= TREE_DEFAULT_OPEN_DEPTH);
      details.addEventListener('toggle', () => setTreeOpen(provider, cat, node.key, details.open));
      const summary = document.createElement('summary');
      summary.className = 'source-dir-tree-summary';
      const label = document.createElement('span');
      label.className = 'source-dir-tree-label';
      label.textContent = node.label;
      summary.appendChild(label);
      const count = document.createElement('span');
      count.className = 'source-vendor-count';
      count.textContent = String(Array.from(node.children.values()).reduce(
        (sum, child) => sum + countTreeLeaves(child),
        0,
      ));
      summary.appendChild(count);
      details.appendChild(summary);
      for (const child of node.children.values()) {
        details.appendChild(renderDirTreeNode(child, provider, cat, depth + 1));
      }
      wrap.appendChild(details);
    }
    return wrap;
  };

  const countTreeLeaves = (node) => (
    (node.leaf ? 1 : 0)
      + Array.from(node.children.values()).reduce((sum, child) => sum + countTreeLeaves(child), 0)
  );

  const renderDirTree = (cat, dirs, rendered, provider) => {
    const tree = buildDirTree(cat, dirs, rendered);
    const frag = document.createDocumentFragment();
    for (const child of tree.children.values()) {
      frag.appendChild(renderDirTreeNode(child, provider, cat, 0));
    }
    return frag;
  };

  if (STATE.pendingActivatePath) {
    const pendingSrc = (STATE.allSources || sources || []).find(s => s.path === STATE.pendingActivatePath);
    if (pendingSrc && pendingSrc.memory_dir) {
      const st = statusByPath[pendingSrc.memory_dir];
      const pendingCat = presentationCategoryForDir(pendingSrc.memory_dir, st);
      const pendingProvider = presentationProviderForDir(pendingSrc.memory_dir, st);
      const pendingPlan = vendorPlans[pendingProvider];
      const pendingEntry = pendingPlan && pendingPlan.visibleCats.find(([cat]) => cat === pendingCat);
      if (pendingEntry) {
        STATE.sourcesActiveCategoryByVendor[pendingProvider] = pendingCat;
        const [, indexed] = pendingEntry;
        if (indexed.length > 1) {
          const dirIdx = indexed.indexOf(pendingSrc.memory_dir);
          if (dirIdx >= 0) {
            setCategoryLimit(
              pendingProvider,
              pendingCat,
              Math.ceil((dirIdx + 1) / CATEGORY_PAGE_SIZE) * CATEGORY_PAGE_SIZE,
            );
            for (const key of treeKeysForDir(pendingCat, indexed, pendingSrc.memory_dir)) {
              setTreeOpen(pendingProvider, pendingCat, key, true);
            }
            setDirOpen(pendingProvider, pendingCat, pendingSrc.memory_dir, true);
          }
        } else {
          let seen = 0;
          for (const dir of indexed) {
            const items = sourcesByDir[dir] || [];
            const idx = items.findIndex(s => s.path === STATE.pendingActivatePath);
            if (idx >= 0) {
              setCategoryLimit(
                pendingProvider,
                pendingCat,
                Math.ceil((seen + idx + 1) / CATEGORY_PAGE_SIZE) * CATEGORY_PAGE_SIZE,
              );
              break;
            }
            seen += items.length;
          }
        }
      }
    }
  }

  const setupAutoCategoryMore = () => {
    if (!list) return;
    if (list._sourcesAutoMoreHandler) {
      list.removeEventListener('scroll', list._sourcesAutoMoreHandler);
    }
    let ticking = false;
    const handler = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        const rows = Array.from(list.querySelectorAll('.source-category-more-row[data-auto-more="true"]'));
        if (!rows.length) return;
        const listRect = list.getBoundingClientRect();
        const threshold = listRect.bottom + 48;
        const row = rows.find(el => el.getBoundingClientRect().top <= threshold);
        if (!row) return;
        const cat = row.dataset.category;
        const nextLimit = Number(row.dataset.nextLimit || 0);
        if (!cat || !nextLimit) return;
        setCategoryLimit(activeVendor, cat, nextLimit);
        renderSourceTree(_getFilteredSorted());
      });
    };
    list._sourcesAutoMoreHandler = handler;
    list.addEventListener('scroll', handler, { passive: true });
  };

  // Pass 2: render only the active vendor's content directly into the
  // sidebar list — the sub-tab strip carries the vendor disclosure, so
  // no per-vendor ``<details>`` wrapper is needed.
  list.innerHTML = '';
  const plan = vendorPlans[activeVendor];

  if (plan.isEmptyVendor && !filterActive) {
    const placeholder = document.createElement('div');
    placeholder.className = 'source-vendor-placeholder';
    const msg = document.createElement('span');
    msg.className = 'source-vendor-placeholder-msg';
    const vendorName = (typeof t === 'function')
      ? t(PROVIDER_LABEL_KEY[activeVendor] || activeVendor)
      : activeVendor;
    msg.textContent = (typeof t === 'function')
      ? t('sources.empty_vendor_placeholder', { vendor: vendorName })
      : `${vendorName} memory not found`;
    placeholder.appendChild(msg);
    const cta = document.createElement('button');
    cta.type = 'button';
    cta.className = 'btn-ghost btn-xs source-vendor-add-cta';
    cta.textContent = (typeof t === 'function') ? t('sources.add_manually_btn') : '+ Add manually';
    cta.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const addBtn = qs('memory-add-path-btn');
      if (addBtn) addBtn.click();
    });
    placeholder.appendChild(cta);
    list.appendChild(placeholder);
  } else {
    const products = document.createElement('div');
    products.className = 'source-vendor-products';
    const visibleProductCount = plan.visibleCats.filter(
      ([, indexed, discovered]) => indexed.length || discovered.length,
    ).length;
    const hasCategoryNav = visibleProductCount > 1;
    const visibleCatsForMenu = plan.visibleCats.filter(
      ([, indexed, discovered]) => indexed.length || discovered.length,
    );
    let activeCategory = STATE.sourcesActiveCategoryByVendor[activeVendor];
    if (hasCategoryNav && !visibleCatsForMenu.some(([cat]) => cat === activeCategory)) {
      activeCategory = visibleCatsForMenu[0] ? visibleCatsForMenu[0][0] : '';
      if (activeCategory) STATE.sourcesActiveCategoryByVendor[activeVendor] = activeCategory;
    }
    const catsToRender = hasCategoryNav
      ? visibleCatsForMenu.filter(([cat]) => cat === activeCategory)
      : visibleCatsForMenu;
    for (const [cat, indexed, discovered] of catsToRender) {
      if (!indexed.length && !discovered.length) continue;
      const section = document.createElement('section');
      section.className = 'source-vendor-product';
      section.dataset.category = cat;
      const totalFiles = categoryFileCount(indexed);
      if (hasCategoryNav) {
        section.setAttribute('tabindex', '-1');
      } else {
        const productHeader = document.createElement('div');
        productHeader.className = 'source-vendor-product-header';
        productHeader.setAttribute('tabindex', '-1');
        const pLabel = document.createElement('span');
        pLabel.className = 'source-vendor-product-label';
        const pKey = CATEGORY_LABEL_KEY[cat] || cat;
        pLabel.textContent = (typeof t === 'function') ? t(pKey) : pKey;
        productHeader.appendChild(pLabel);
        const pCount = document.createElement('span');
        pCount.className = 'source-vendor-count';
        pCount.textContent = String(totalFiles);
        productHeader.appendChild(pCount);
        section.appendChild(productHeader);
      }
      const flatFileMode = filterActive || fileSortMode;
      const folderMode = indexed.length > 1 && !flatFileMode;
      const limit = getCategoryLimit(activeVendor, cat);
      const totalVisibleUnits = folderMode ? indexed.length : totalFiles;
      const { rendered, shown } = folderMode
        ? sliceIndexedFolders(indexed, limit)
        : (flatFileMode ? sliceIndexedFlatFiles(indexed, limit) : sliceIndexedFiles(indexed, limit));
      if (folderMode) {
        section.appendChild(renderDirTree(cat, indexed, rendered, activeVendor));
      } else if (flatFileMode) {
        for (const s of rendered) section.appendChild(_renderMemorySourceItem(s, maxChunks));
      } else {
        for (const [dir, items] of rendered) {
          if (hasCategoryNav && indexed.length === 1) {
            for (const s of items) section.appendChild(_renderMemorySourceItem(s, maxChunks));
          } else {
            section.appendChild(_renderMemoryDirGroup(
              dir,
              items,
              statusByPath[dir],
              maxChunks,
              {
                isDefault: dir === defaultDir,
                open: getDirOpen(activeVendor, cat, dir, true),
                onToggle: (open) => setDirOpen(activeVendor, cat, dir, open),
              },
            ));
          }
        }
      }
      if (shown < totalVisibleUnits) {
        const moreRow = document.createElement('div');
        moreRow.className = 'source-category-more-row';
        moreRow.dataset.autoMore = 'true';
        moreRow.dataset.category = cat;
        moreRow.dataset.nextLimit = String(limit + CATEGORY_PAGE_SIZE);
        const status = document.createElement('span');
        status.className = 'source-category-more-status';
        status.textContent = (typeof t === 'function')
          ? t(
              folderMode ? 'sources.category_scroll_more_folders' : 'sources.category_scroll_more',
              { shown, total: totalVisibleUnits },
            )
          : `Showing ${shown}/${totalVisibleUnits}; scroll for more`;
        moreRow.appendChild(status);
        section.appendChild(moreRow);
      }
      const disc = renderDiscoveredBlock(discovered);
      if (disc) section.appendChild(disc);
      products.appendChild(section);
    }
    // Skip the wrapper when every category was filtered out — happens
    // when a ``user`` vendor has only orphan rows (no indexed / no
    // discovered dirs). Without this guard an empty
    // ``.source-vendor-products`` div would still ship its CSS margin
    // before the orphan block sits below.
    if (products.children.length) list.appendChild(products);
    const categoryNav = renderCategoryNav(plan.visibleCats, activeCategory);
    if (categoryNav) {
      products.classList.add('source-vendor-products-with-nav');
      list.insertBefore(categoryNav, products);
    }
  }
  setupAutoCategoryMore();

  // Append the orphan block last so indexed groups stay primary. Only
  // shows up when ``activeVendor === 'user'`` because ``plan.orphans``
  // is populated for that vendor only.
  const orphanBlock = renderOrphanBlock(plan.orphans || []);
  if (orphanBlock) list.appendChild(orphanBlock);

  // Empty-state fallbacks scoped to the active vendor. If no memory
  // dirs exist anywhere, show the "Add one with + Add path" hint
  // regardless of which tab is active. The orphan check keeps a user
  // who has only Index-tab uploads (no configured dirs) from seeing
  // the "No memory directories" hint — their uploads are real content.
  // If a filter is active and the active vendor has no matches but
  // other vendors do, the muted "no matches" hint sits inside the
  // panel — the populated badge on sibling tabs tells the user where
  // to look.
  if (!filterActive && !allDirs.size && !orphanItems.length) {
    list.innerHTML = '<div class="empty-state">' + emptyState('📁', 'No memory directories', 'Add one with the + Add path button') + '</div>';
  } else if (bodyFilterPending && filterActive && !plan.totalFiles && !plan.discoveredCount) {
    list.innerHTML = '<div class="empty-state">' + emptyState('🔎', 'Searching indexed body…') + '</div>';
  } else if (filterActive && !plan.totalFiles && !plan.discoveredCount) {
    // ``totalFiles`` only counts indexed+orphan rows; a vendor whose
    // ``visibleCats`` carries discovered dirs (the #896 carve-out) would
    // otherwise be wiped here, defeating the carry-over fix.
    list.innerHTML = '<div class="empty-state">' + emptyState('🔍', 'No matches for that filter') + '</div>';
  }

  // Pending source activation set by ``_navigateToSource``. The tree
  // exists now, so we can resolve the source-item, switch the vendor
  // sub-tab if the cold-load eager resolve guessed wrong (or missed
  // because STATE was empty), and trigger the chunk browser. Cleared
  // immediately on consume so a follow-up filter/sort/reindex render
  // doesn't re-scrollIntoView an item the user has navigated past.
  if (STATE.pendingActivatePath) {
    const path = STATE.pendingActivatePath;
    // Path-based navigation is an explicit user intent override —
    // drop any pre-existing namespace chip so (a) the source isn't
    // hidden by a filter that has nothing to do with this click, and
    // (b) NS auto-switch up top doesn't fight the pending vendor
    // resolve below in a loop (NS picks the best-NS-match vendor;
    // pending picks the source's vendor; if they disagree the two
    // ping-pong on every re-render).
    if (STATE.sourcesNsFilter) {
      STATE.sourcesNsFilter = '';
      _renderSourcesNsChip();
      renderSourceTree(_getFilteredSorted());
      return;
    }
    const src = (STATE.allSources || []).find(s => s.path === path);
    const status = src && src.memory_dir
      ? (STATE.memoryStatusByPath || {})[src.memory_dir]
      : null;
    const wantedProvider = (status && _SOURCES_VENDORS.includes(status.provider))
      ? status.provider : null;
    if (wantedProvider && STATE.sourcesActiveVendor !== wantedProvider) {
      // Eager resolve in ``_navigateToSource`` couldn't see the data
      // (cold load — Home → Sources first time, before
      // ``loadDashboard``'s STATE mirror landed) so we land here on
      // the wrong vendor's tree. Switch and re-render once; the
      // re-entry will hit the matching tree and consume the pending
      // path. Guarded by the vendor-mismatch check so we don't loop.
      STATE.sourcesActiveVendor = wantedProvider;
      _syncSourcesVendorTabs(wantedProvider);
      renderSourceTree(_getFilteredSorted());
      return;
    }
    const target = list.querySelector(`.source-item[title="${CSS.escape(path)}"]`);
    if (target) {
      target.classList.add('active');
      target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      // ``pendingActivateChunkId`` means the caller (Timeline → Source jump
      // in PR #676) wants a specific chunk highlighted. Default ``limit=100``
      // would silently miss any target past position 100 in a large source —
      // bumping to 500 covers ~99% of indexed sources without making the
      // source-level Home/recent jump pay the larger fetch. The chunk-side
      // consumer in ``browseSource`` shows a toast if it still can't find
      // the card after the 500-row fetch.
      const browseLimit = STATE.pendingActivateChunkId ? 500 : undefined;
      if (typeof browseSource === 'function') browseSource(path, browseLimit);
    } else if (STATE.uiMode === 'dev') {
      // Path made it through the vendor + filter resolves but the
      // ``.source-item`` isn't in the rendered list — most likely an
      // edge case the cold-load fix didn't anticipate (orphan source,
      // path normalisation drift like #675, etc.). Surface it in dev
      // so the next bug report has a breadcrumb instead of "click did
      // nothing"; prod stays silent because users can't act on it.
      console.warn('[memtomem] pendingActivatePath found no .source-item:', path);
    }
    STATE.pendingActivatePath = '';
  }

  const renderedSources = Array.from(list.querySelectorAll('.source-item'));
  if (renderedSources.length && !list.querySelector('.source-item.active')) {
    const currentPath = qs('chunks-browser')?.querySelector('.chunks-browser-header .file-path')?.textContent || '';
    const target = renderedSources.find(el => el.title === currentPath) || renderedSources[0];
    if (target) {
      target.classList.add('active');
      if (typeof browseSource === 'function') browseSource(target.title);
    }
  }
}

function _renderMemoryDirGroup(dir, items, status, maxChunks, opts) {
  const { isDefault, label = null, open = true, onToggle = null } = opts || {};
  // ``<details>`` gives us a native chevron + auto-flip on toggle and
  // matches the vendor group's disclosure shape one level up — keeping a
  // single mental model for collapse state across the whole tree.
  const group = document.createElement('details');
  group.className = 'source-group source-group-memory';
  if (status && status.exists === false) group.classList.add('source-group-missing');
  group.open = !!open;
  group.dataset.dir = dir;
  if (typeof onToggle === 'function') {
    group.addEventListener('toggle', () => onToggle(group.open));
  }

  const header = document.createElement('summary');
  header.className = 'source-group-header';
  header.title = dir;

  const dirLabel = document.createElement('span');
  dirLabel.className = 'source-group-dir';
  dirLabel.textContent = label || ((typeof shortDir === 'function') ? shortDir(dir) : dir);
  header.appendChild(dirLabel);

  if (isDefault) {
    const pill = document.createElement('span');
    pill.className = 'source-group-default-pill';
    pill.textContent = (typeof t === 'function') ? t('sources.default_pill') : 'default';
    header.appendChild(pill);
  }

  // 3-state status:
  //   - missing  → exists === false (red, "missing")
  //   - pending  → file_count > 0 && chunk_count === 0 (amber, ready to index)
  //   - indexed  → otherwise (no color)
  if (status) {
    const statsBadge = document.createElement('span');
    statsBadge.className = 'source-group-stats';
    const files = (typeof status.file_count === 'number') ? status.file_count : 0;
    const indexed = status.source_file_count || 0;
    const chunks = status.chunk_count || 0;
    if (status.exists === false) {
      statsBadge.classList.add('missing');
      statsBadge.textContent = (typeof t === 'function')
        ? t('sources.memory_dirs.status_missing') : 'missing';
    } else {
      if (chunks === 0 && files > 0) statsBadge.classList.add('pending');
      statsBadge.textContent = (typeof t === 'function')
        ? t('sources.memory_dirs.status_group', { files, indexed, chunks })
        : (indexed + '/' + files + ' files, ' + chunks + ' chunks');
    }
    header.appendChild(statsBadge);
  }

  // Hover-revealed actions: open the dir in the OS file manager,
  // index/reindex its files, or remove it from memory_dirs config.
  // ``preventDefault`` keeps the click from toggling the parent
  // ``<details>`` (default behavior on summary clicks).
  const actions = document.createElement('div');
  actions.className = 'source-group-actions';
  const tFallback = (key, fb) => (typeof t === 'function' ? t(key) : fb);

  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'btn-ghost btn-xs';
  openBtn.textContent = tFallback('sources.memory_dirs.action_open', 'Open');
  openBtn.title = tFallback('sources.memory_dirs.open_title', 'Open in file manager');
  if (status && status.exists === false) openBtn.disabled = true;
  openBtn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    if (typeof mdOpenOne === 'function') mdOpenOne(dir, openBtn);
  });
  actions.appendChild(openBtn);

  const reindexBtn = document.createElement('button');
  reindexBtn.type = 'button';
  reindexBtn.className = 'btn-ghost btn-xs';
  const hasChunks = status && status.exists !== false && (status.chunk_count || 0) > 0;
  reindexBtn.textContent = tFallback(
    hasChunks ? 'sources.memory_dirs.action_reindex' : 'sources.memory_dirs.action_index',
    hasChunks ? 'Reindex' : 'Index',
  );
  reindexBtn.title = tFallback('sources.memory_dirs.reindex_title', 'Reindex this directory');
  reindexBtn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    if (typeof mdReindexOne === 'function') mdReindexOne(dir, reindexBtn);
  });
  actions.appendChild(reindexBtn);

  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'btn-ghost btn-xs source-group-remove';
  removeBtn.textContent = tFallback('sources.memory_dirs.action_delete', 'Remove');
  removeBtn.title = tFallback('sources.memory_dirs.delete_title', 'Remove from memory_dirs');
  if ((STATE.memoryDirs || []).length <= 1) removeBtn.disabled = true;
  removeBtn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    if (typeof mdRemove === 'function') mdRemove(dir);
  });
  actions.appendChild(removeBtn);

  header.appendChild(actions);
  group.appendChild(header);

  for (const s of items) {
    group.appendChild(_renderMemorySourceItem(s, maxChunks));
  }

  return group;
}

async function _reindexSourceFile(path, btn) {
  if (typeof _indexingTryStartOrRefresh === 'function') {
    if (!(await _indexingTryStartOrRefresh())) return;
  } else if (typeof _indexingTryStart === 'function' && !_indexingTryStart()) {
    return;
  }
  if (btn) btnLoading(btn, true);
  try {
    const resp = await api(
      'POST',
      '/api/index',
      { path, recursive: false, force: true },
      { timeout: 300_000 },
    );
    const count = (resp && resp.indexed_chunks) || 0;
    const errors = (resp && resp.errors) || [];
    if (errors.length) {
      showToast(t('toast.source_reindex_partial', { count: errors.length, first: errors[0] }), 'error');
    } else {
      showToast(t('toast.source_reindexed', { count }), 'success');
    }
    _markDataStale();
    loadStats();
    await loadSources();
    browseSource(path);
  } catch (err) {
    showToast(t('toast.source_reindex_failed', { error: err.message }), 'error');
  } finally {
    if (btn) btnLoading(btn, false);
    if (typeof _indexingEnd === 'function') _indexingEnd();
  }
}

async function _deleteSourceFile(path) {
  const ok = await showConfirm({
    title: t('confirm.source_delete_title'),
    message: t('confirm.source_delete_msg', { path }),
    confirmText: t('common.delete'),
  });
  if (!ok) return;
  try {
    await api('DELETE', `/api/sources?path=${encodeURIComponent(path)}`);
    showToast(t('toast.source_deleted'), 'success');
    STATE.allSources = (STATE.allSources || []).filter(s => s.path !== path);
    STATE.lastResults = (STATE.lastResults || []).filter(r => r.chunk?.source_file !== path);
    _markDataStale();
    loadStats();
    const activePath = qs('chunks-browser')?.querySelector('.file-path')?.textContent || '';
    if (activePath === path) hideBrowser();
    renderSourceTree(_getFilteredSorted());
  } catch (err) {
    showToast(t('toast.delete_failed', { error: err.message }), 'error');
  }
}

function _renderMemorySourceItem(s, maxChunks) {
  const filename = s.path.split('/').pop() || s.path;
  const item = document.createElement('div');
  item.className = 'source-item';
  item.title = s.path;
  const size = s.file_size != null ? formatBytes(s.file_size) : '';
  const age = s.last_indexed_at ? relativeTime(s.last_indexed_at) : '';
  const barPct = Math.round(((s.chunk_count || 0) / maxChunks) * 100);
  const nsBadges = (s.namespaces || [])
    .filter(ns => ns !== 'default')
    .map(ns => `<span class="badge badge-ns source-ns-badge">${highlightText(ns, _sourcesFilterHighlightQuery())}</span>`)
    .join('');
  // Three-tier preview: filename row1 (anchor) → title (heading
  // subtitle) → AI summary OR heuristic excerpt (body preview). When an
  // AI summary is cached the prose replaces the heuristic excerpt and
  // is marked with a ✨ prefix; otherwise the heuristic excerpt fills
  // the same slot. Title comes from the heading hierarchy regardless,
  // so a row keeps a visible label even when the body slot is empty.
  let summaryHtml = '';
  const filterQuery = _sourcesFilterHighlightQuery();
  const titlePart = s.title
    ? `<div class="source-item-title">${highlightText(s.title, filterQuery)}</div>`
    : '';
  let bodyPart = '';
  if (s.ai_summary) {
    bodyPart =
      `<div class="source-item-excerpt" data-ai="true">` +
      `<span class="source-item-ai-badge" aria-hidden="true">✨</span> ` +
      `${highlightText(s.ai_summary, filterQuery)}` +
      `</div>`;
  } else if (s.excerpt) {
    bodyPart = `<div class="source-item-excerpt">${highlightText(s.excerpt, filterQuery)}</div>`;
  }
  if (titlePart || bodyPart) {
    summaryHtml = `<div class="source-item-summary">${titlePart}${bodyPart}</div>`;
  }
  item.innerHTML = `
    <div class="source-item-row1">
      <span class="source-type-dot" style="background:${fileTypeColor(s.path)}"></span>
      <span class="source-name">${highlightText(filename, filterQuery)}</span>
      ${nsBadges}
      <span class="source-item-actions">
        <button type="button" class="source-action-btn source-reindex-btn" title="${escapeAttr(t('sources.source_reindex_title'))}">${escapeHtml(t('sources.source_reindex_btn'))}</button>
        <button type="button" class="source-action-btn source-del-btn" title="${escapeAttr(t('sources.source_delete_title'))}">${escapeHtml(t('sources.source_delete_btn'))}</button>
      </span>
    </div>
    ${summaryHtml}
    <div class="source-item-row2">
      ${escapeHtml(t(s.chunk_count === 1 ? 'home.source_chunks_one' : 'home.source_chunks_other', { count: s.chunk_count ?? '?' }))}${size ? ' · ' + size : ''}${s.avg_tokens ? ' · ' + escapeHtml(t('sources.meta_avg_tokens', { n: s.avg_tokens })) : ''}${age ? ' · ' + age : ''}
    </div>
    <div class="source-chunk-bar">
      <div class="source-chunk-bar-fill" style="width:${barPct}%"></div>
    </div>
  `;
  item.setAttribute('tabindex', '0');
  item.querySelector('.source-reindex-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    _reindexSourceFile(s.path, e.currentTarget);
  });
  item.querySelector('.source-del-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    _deleteSourceFile(s.path);
  });
  item.addEventListener('click', () => {
    // Manual source-item click is an explicit user pivot — drop any
    // chunk-highlight target left over from a prior ``_navigateToSource``
    // (Timeline jump → Source render → user reroutes mid-render). The
    // stale id wouldn't cause a wrong card to highlight (chunk ids are
    // UUIDs), but it would silently no-op the next ``browseSource`` flash
    // path until something else clears it. PR #676 review follow-up.
    STATE.pendingActivateChunkId = '';
    STATE.pendingActivateChunkSourcePath = '';
    document.querySelectorAll('.source-item').forEach(el => el.classList.remove('active'));
    item.classList.add('active');
    browseSource(s.path);
  });
  item.addEventListener('keydown', e => {
    // Only treat Enter/Space as a row-open when the row itself has focus.
    // Buttons inside `.source-item-actions` (reindex / delete) need their
    // own native activation; if we preventDefault here for descendant
    // targets, those actions become mouse-only.
    if (e.target !== item) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); item.click(); }
  });
  return item;
}

async function browseSource(path, limit = 100) {
  const browser = qs('chunks-browser');
  panelLoading(browser);
  try {
    const data = await api('GET', `/api/chunks?source=${encodeURIComponent(path)}&limit=${limit}`);
    browser.innerHTML = '';
    const content = document.createElement('div');
    content.id = 'chunks-browser-content';

    const header = document.createElement('div');
    header.className = 'chunks-browser-header';
    header.innerHTML = `
      <span class="file-path">${escapeHtml(path)}</span>
      <span class="badge badge-blue">${escapeHtml(t(data.total === 1 ? 'home.source_chunks_one' : 'home.source_chunks_other', { count: data.total }))}</span>
      <span class="chunks-browser-info">${escapeHtml(t('chunks.shown_of_total', { shown: data.chunks.length, total: data.total }))}</span>
    `;
    if (data.chunks.length < data.total) {
      const loadAllBtn = document.createElement('button');
      loadAllBtn.className = 'btn-ghost btn-xs chunks-load-all-btn';
      const loadAllLabel = (typeof t === 'function')
        ? t('chunks.load_all') : 'Load All';
      loadAllBtn.textContent = loadAllLabel;
      loadAllBtn.setAttribute('data-i18n', 'chunks.load_all');
      loadAllBtn.addEventListener('click', () => browseSource(path, 500));
      header.appendChild(loadAllBtn);
    }
    // View mode toggle: Chunks | Document
    const viewToggle = document.createElement('div');
    viewToggle.className = 'view-mode-toggle';
    const chunksBtn = document.createElement('button');
    chunksBtn.className = 'view-mode-btn active';
    chunksBtn.textContent = t('chunks.view_chunks');
    const docBtn = document.createElement('button');
    docBtn.className = 'view-mode-btn';
    docBtn.textContent = t('chunks.view_document');
    viewToggle.appendChild(chunksBtn);
    viewToggle.appendChild(docBtn);
    header.appendChild(viewToggle);
    content.appendChild(header);

    if (!data.chunks.length) {
      content.innerHTML += `<div class="empty-state" style="height:80px"><p>${escapeHtml(t('chunks.none_found'))}</p></div>`;
    } else {
      // Document view container (hidden initially)
      const docView = document.createElement('div');
      docView.className = 'document-view';
      docView.hidden = true;
      _renderDocumentView(data.chunks, docView, path);
      content.appendChild(docView);

      // Toggle handlers
      chunksBtn.addEventListener('click', () => {
        chunksBtn.classList.add('active'); docBtn.classList.remove('active');
        chunkList.hidden = false; docView.hidden = true;
      });
      docBtn.addEventListener('click', () => {
        docBtn.classList.add('active'); chunksBtn.classList.remove('active');
        chunkList.hidden = true; docView.hidden = false;
      });

      const chunkList = document.createElement('div');
      const lang = getLanguage(path);
      const cardPairs = [];  // [card, contentDiv] — accordion 활성화는 DOM 삽입 후 일괄 처리
      data.chunks.forEach(c => {
        const card = document.createElement('div');
        card.className = 'chunk-card';
        card.dataset.chunkId = c.id;
        const cardTypeLabel = c.chunk_type.replace('_', ' ');
        const cardTrail = c.heading_hierarchy.length
          ? `, ${c.heading_hierarchy.join(' › ')}`
          : '';
        card.setAttribute(
          'aria-label',
          `${cardTypeLabel}, lines ${c.start_line}-${c.end_line}${cardTrail}`,
        );
        card.innerHTML = `
          <div class="chunk-card-meta">
            <span class="badge badge-gray">${escapeHtml(c.chunk_type.replace('_', ' '))}</span>
            <span class="chunk-card-lines">lines ${c.start_line}–${c.end_line}</span>
            ${_tierBadgeHtml(c.target_scope)}
            ${c.heading_hierarchy.length ? `<span class="hierarchy-trail">${escapeHtml(c.heading_hierarchy.join(' › '))}</span>` : ''}
            <div class="chunk-card-actions">
              <button class="btn-ghost btn-xs card-copy-btn" title="${escapeAttr(t('chunks.card_copy_title'))}">${escapeHtml(t('chunks.card_copy'))}</button>
              <button class="btn-ghost btn-xs card-edit-btn" title="${escapeAttr(t('chunks.card_edit_title'))}">${escapeHtml(t('chunks.card_edit'))}</button>
              <button class="btn-danger btn-xs card-delete-btn" title="${escapeAttr(t('chunks.card_delete_title'))}">${escapeHtml(t('chunks.card_delete'))}</button>
            </div>
          </div>
        `;
        const contentDiv = document.createElement('div');
        contentDiv.className = 'chunk-card-content';
        if (lang && lang !== 'markdown' && window.Prism) {
          const pre = document.createElement('pre');
          const code = document.createElement('code');
          code.className = `language-${lang}`;
          code.textContent = c.content;
          pre.appendChild(code);
          contentDiv.appendChild(pre);
          Prism.highlightElement(code);
        } else {
          contentDiv.textContent = c.content;
        }
        card.appendChild(contentDiv);

        // Copy
        card.querySelector('.card-copy-btn').addEventListener('click', e => {
          e.stopPropagation();
          copyToClipboard(c.content);
        });

        // Edit
        card.querySelector('.card-edit-btn').addEventListener('click', e => {
          e.stopPropagation();
          _startChunkEdit(card, c, path);
        });

        // Delete
        card.querySelector('.card-delete-btn').addEventListener('click', async e => {
          e.stopPropagation();
          const ok = await showConfirm({
            title: t('confirm.chunk_delete_title'),
            message: t('confirm.chunk_delete_simple_msg', { start: c.start_line, end: c.end_line }),
            confirmText: t('common.delete'),
          });
          if (!ok) return;
          try {
            await api('DELETE', `/api/chunks/${c.id}`);
            card.remove();
            showToast(t('toast.chunk_deleted'), 'success');
            // Update count badge
            const countEl = content.querySelector('.badge-blue');
            const remaining = content.querySelectorAll('.chunk-card').length;
            if (countEl) countEl.textContent = t('settings.ns.group_chunks', { count: remaining });
            STATE.lastResults = STATE.lastResults.filter(r => String(r.chunk.id) !== String(c.id));
            renderResults(STATE.lastResults);
            _markDataStale();
            loadStats();
          } catch (err) {
            showToast(t('toast.delete_failed', { error: err.message }), 'error');
          }
        });

        chunkList.appendChild(card);
        cardPairs.push([card, contentDiv]);
      });
      content.appendChild(chunkList);

      // 모든 카드가 DOM에 삽입된 뒤 단일 rAF로 accordion 활성화
      requestAnimationFrame(() => {
        cardPairs.forEach(([card, contentDiv]) => {
          if (contentDiv.scrollHeight > 120) {
            card.classList.add('chunk-card-collapsible');
            card.setAttribute('aria-expanded', 'false');
            card.setAttribute('role', 'button');
            card.setAttribute('tabindex', '0');
            const toggleCard = () => {
              contentDiv.classList.toggle('expanded');
              card.setAttribute('aria-expanded', contentDiv.classList.contains('expanded'));
            };
            let dragStartX = 0, dragStartY = 0;
            card.addEventListener('mousedown', e => { dragStartX = e.clientX; dragStartY = e.clientY; });
            card.addEventListener('click', e => {
              if (Math.abs(e.clientX - dragStartX) > 4 || Math.abs(e.clientY - dragStartY) > 4) return;
              if (e.target.closest('.chunk-card-edit-area')) return;
              toggleCard();
            });
            card.addEventListener('keydown', e => {
              if (e.target.closest('.chunk-card-actions, .chunk-card-edit-area')) return;
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                toggleCard();
              }
            });
          }
        });
      });
    }
    browser.appendChild(content);

    // Consume ``pendingActivateChunkId`` (set by ``_navigateToSource``
    // when the caller — e.g. Timeline — knows the specific chunk).
    // Done after appendChild so the card is in the DOM. On a miss, keep
    // the target for this same source so a larger follow-up fetch can
    // retry. The accordion-expand listener attaches in the next rAF, so
    // we run inside one too — but we don't depend on the listener: we set
    // the ``expanded`` class directly when the card is collapsible.
    const pendingChunkPath = STATE.pendingActivateChunkSourcePath || path;
    if (STATE.pendingActivateChunkId && pendingChunkPath === path) {
      const targetId = STATE.pendingActivateChunkId;
      requestAnimationFrame(() => {
        const card = content.querySelector(
          `.chunk-card[data-chunk-id="${CSS.escape(String(targetId))}"]`,
        );
        if (!card) {
          // Caller asked for a specific chunk highlight but the card isn't
          // in this fetch. ``_renderMemorySourceTree`` already bumps the
          // limit to 500 when ``pendingActivateChunkId`` is set, so reaching
          // here means the source has 500+ chunks (rare) — surface a toast
          // so the user knows why the jump landed on the source without a
          // flash, rather than reading it as a regression.
          if (typeof showToast === 'function' && typeof t === 'function') {
            showToast(t('toast.chunk_target_missing'), 'info');
          }
          return;
        }
        STATE.pendingActivateChunkId = '';
        STATE.pendingActivateChunkSourcePath = '';
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const contentDiv = card.querySelector('.chunk-card-content');
        if (contentDiv && card.classList.contains('chunk-card-collapsible')) {
          contentDiv.classList.add('expanded');
          card.setAttribute('aria-expanded', 'true');
        }
        card.classList.add('tl-target-flash');
        setTimeout(() => card.classList.remove('tl-target-flash'), 1400);
      });
    }
  } catch (err) {
    browser.innerHTML = `<div class="empty-state"><p>Error: ${escapeHtml(err.message)}</p></div>`;
  }
}

function _renderDocumentView(chunks, container, path) {
  const sorted = [...chunks].sort((a, b) => (a.start_line || 0) - (b.start_line || 0));
  const fullText = sorted.map(c => c.content).join('\n');
  const lang = getLanguage(path);

  // Copy All button
  const copyBtn = document.createElement('button');
  copyBtn.className = 'btn-ghost btn-xs document-copy-btn';
  copyBtn.textContent = 'Copy All';
  copyBtn.addEventListener('click', e => {
    e.stopPropagation();
    copyToClipboard(fullText);
  });
  container.appendChild(copyBtn);

  const contentDiv = document.createElement('div');
  contentDiv.className = 'document-content';

  // Render each chunk as a hoverable editable block (code or markdown/plain)
  sorted.forEach(c => {
    const block = document.createElement('div');
    block.className = 'doc-chunk-block';

    if (lang && lang !== 'markdown' && window.Prism) {
      const pre = document.createElement('pre');
      pre.style.margin = '0';
      const code = document.createElement('code');
      code.className = `language-${lang}`;
      code.textContent = c.content;
      pre.appendChild(code);
      block.appendChild(pre);
      Prism.highlightElement(code);
    } else {
      block.textContent = c.content;
    }

    const editBtn = document.createElement('button');
    editBtn.className = 'btn-ghost btn-xs doc-chunk-edit-btn';
    editBtn.textContent = 'Edit';
    editBtn.title = `lines ${c.start_line}–${c.end_line}`;
    editBtn.addEventListener('click', e => {
      e.stopPropagation();
      _startChunkEdit(block, c, path);
    });
    block.appendChild(editBtn);
    contentDiv.appendChild(block);
  });

  container.appendChild(contentDiv);
}

function _startChunkEdit(card, chunk, sourcePath) {
  // Prevent duplicate edit areas
  if (card.querySelector('.chunk-card-edit-area')) return;
  const editArea = document.createElement('div');
  editArea.className = 'chunk-card-edit-area';
  const ta = document.createElement('textarea');
  ta.value = chunk.content;
  editArea.appendChild(ta);

  const actionsDiv = document.createElement('div');
  actionsDiv.className = 'chunk-card-edit-actions';
  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn-primary btn-xs';
  saveBtn.textContent = 'Save';
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn-ghost btn-xs';
  cancelBtn.textContent = 'Cancel';
  actionsDiv.appendChild(saveBtn);
  actionsDiv.appendChild(cancelBtn);
  editArea.appendChild(actionsDiv);
  card.appendChild(editArea);

  ta.focus();

  cancelBtn.addEventListener('click', e => {
    e.stopPropagation();
    editArea.remove();
  });

  saveBtn.addEventListener('click', async e => {
    e.stopPropagation();
    const newContent = ta.value;
    if (newContent === chunk.content) { editArea.remove(); return; }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      const resp = await apiWithRedactionRetry(
        'PATCH',
        `/api/chunks/${chunk.id}`,
        { new_content: newContent },
      );
      if (resp === null) {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
        return;
      }
      showToast(t('toast.chunk_updated'), 'success');
      _syncResultContent(chunk.id, newContent);
      _markDataStale();
      // Refresh the browser to show updated content
      browseSource(sourcePath, card.closest('#chunks-browser-content')?.querySelectorAll('.chunk-card').length > 100 ? 500 : 100);
    } catch (err) {
      showToast(t('toast.update_failed', { error: err.message }), 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
}

// ---------------------------------------------------------------------------
// Index — mode toggle (folder / upload / compose)
// ---------------------------------------------------------------------------
//
// The three input paths live in one card; the segmented toggle picks which
// panel is visible. Mirrors the main tablist's auto-activation arrow-nav
// (focus + activate together) so a keyboard user toggles by walking the row.

const INDEX_MODES = ['folder', 'upload', 'compose'];
const INDEX_MODE_LS_KEY = 'memtomem.index.mode';

function _readIndexMode() {
  try {
    const v = localStorage.getItem(INDEX_MODE_LS_KEY);
    if (v && INDEX_MODES.includes(v)) return v;
  } catch (_e) { /* private mode */ }
  // S1.6: a new install defaults to the most intuitive "New memory" (compose)
  // mode rather than the technical folder scan. Returning users keep their
  // saved choice (handled above).
  return 'compose';
}

function setIndexMode(mode) {
  if (!INDEX_MODES.includes(mode)) mode = 'folder';
  STATE.indexMode = mode;
  try { localStorage.setItem(INDEX_MODE_LS_KEY, mode); } catch (_e) { /* ignore */ }
  for (const m of INDEX_MODES) {
    const btn = qs(`index-mode-${m}`);
    const pnl = qs(`index-panel-${m}`);
    if (!btn || !pnl) continue;
    const active = m === mode;
    btn.classList.toggle('btn-active', active);
    btn.setAttribute('aria-selected', String(active));
    btn.setAttribute('tabindex', active ? '0' : '-1');
    pnl.hidden = !active;
    const guide = document.querySelector(`[data-mode-guide="${m}"]`);
    if (guide) guide.hidden = !active;
  }
  if (mode === 'upload') loadUploadUsage();
}

INDEX_MODES.forEach(m => {
  const btn = qs(`index-mode-${m}`);
  if (btn) btn.addEventListener('click', () => setIndexMode(m));
});

document.querySelector('.index-mode-toggle')?.addEventListener('keydown', (e) => {
  if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
  const buttons = Array.from(document.querySelectorAll('.index-mode-toggle [role="tab"]'));
  const currentIdx = buttons.indexOf(document.activeElement);
  const nextIdx = _arrowNavIndex(buttons.length, currentIdx === -1 ? 0 : currentIdx, e.key);
  if (nextIdx < 0) return;
  e.preventDefault();
  const next = buttons[nextIdx];
  next.focus();
  if (next.dataset.mode) setIndexMode(next.dataset.mode);
});

setIndexMode(_readIndexMode());

// Header sys-info chip ("provider/model · backend") jumps to Settings → Config
// so users can act on a slow-search / wrong-storage hunch in one click.
(function wireHeaderSysInfoJump() {
  const el = qs('header-sys-info');
  if (!el) return;
  const open = () => {
    document.querySelector('.tab-btn[data-tab="settings"]')?.click();
    setTimeout(() => {
      document.querySelector('.settings-nav-btn[data-section="config"]')?.click();
    }, 50);
  };
  el.addEventListener('click', open);
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });
})();

// Folder mode is one-shot — surface the persistent alternative inline
// (link inside the panel + "Register as Source" action on the success toast).
function goToSourcesAddPath() {
  document.querySelector('.tab-btn[data-tab="sources"]')?.click();
  setTimeout(() => {
    const btn = qs('memory-add-path-btn');
    btn?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    btn?.focus({ preventScroll: true });
  }, 50);
}
qs('folder-hint-sources-link')?.addEventListener('click', e => {
  e.preventDefault();
  goToSourcesAddPath();
});

// ---------------------------------------------------------------------------
// Add Memory
// ---------------------------------------------------------------------------

// Compile the server's ``/api/privacy/patterns`` payload into JS RegExp
// objects, cached on STATE.privacyPatterns. Soft-fail on network /
// translator error: leave the cache null and the submit handler skips
// the scan rather than break Add on a transient blip. The server's
// /api/add route enforces ``privacy.enforce_write_guard`` (the trust
// boundary); this client-side check is defense-in-depth that warns
// before submit so a paste-then-cancel doesn't have to round-trip.
async function loadPrivacyPatterns() {
  try {
    const data = await api('GET', '/api/privacy/patterns');
    const entries = (data && data.patterns) || [];
    STATE.privacyPatterns = entries
      .map(({ pattern, flags }) => {
        try { return new RegExp(pattern, flags); }
        catch (e) { console.warn('[privacy] skip pattern', pattern, e); return null; }
      })
      .filter(Boolean);
  } catch (err) {
    console.warn('[privacy] pattern fetch failed; compose warning disabled', err);
    STATE.privacyPatterns = null;
  }
}

qs('add-btn').addEventListener('click', async () => {
  const content = qs('add-content').value.trim();
  if (!content) { setMsg(qs('add-msg'), 'Content is required.', true); return; }

  // Privacy pre-check (#580). On a hit, surface a confirm dialog that
  // names the *concrete behavior* — "stored as-is in the local
  // database and exposed to search" — rather than abstract anxiety.
  // Clean inputs and a missing pattern cache both pass through
  // silently: no checkmark, no helper text. A success indicator on a
  // clean input would be peak false security since the regex misses
  // many real secrets.
  //
  // Both client ``re.test()`` and server ``privacy.scan()`` cover the
  // entire content — there is no scan-window asymmetry. The server's
  // ``enforce_write_guard`` remains the source of truth; this client
  // check is a UX-time hint that fires before the request goes out.
  let forceUnsafe = false;
  const patterns = STATE.privacyPatterns;
  if (patterns && patterns.some(re => re.test(content))) {
    const ok = await showConfirm({
      title: t('compose.privacy_warning_title'),
      message: t('compose.privacy_warning_message'),
      confirmText: t('compose.privacy_warning_proceed'),
    });
    if (!ok) return;
    // The server-side ``enforce_write_guard`` defaults to blocking, so
    // a confirmed warning must travel as ``force_unsafe: true`` or the
    // submission gets a 403 from the same patterns the client just
    // surfaced. Without this the confirm dialog would be a no-op.
    forceUnsafe = true;
  }

  const title = qs('add-title').value.trim() || null;
  const tagsRaw = qs('add-tags').value.trim();
  const tags = tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [];
  const file = qs('add-file').value.trim() || null;
  const namespace = qs('add-namespace').value.trim() || null;

  const btn = qs('add-btn');
  btnLoading(btn, true);
  hide(qs('add-msg'));

  try {
    const body = { content, title, tags, file, namespace };
    if (forceUnsafe) body.force_unsafe = true;
    // ``apiWithRedactionRetry`` covers the case where the client pre-check
    // missed (cache failed to load) or where the server-side pattern set
    // is broader than the client's cached snapshot — the server's 403
    // becomes a confirm-and-retry instead of an opaque error toast.
    const data = await apiWithRedactionRetry('POST', '/api/add', body);
    if (data === null) return;
    const n = data.indexed_chunks;
    showToast(t('toast.saved_to_file', { path: tildifyPath(data.file), count: n }), 'success');
    qs('add-content').value = '';
    _markDataStale();
    loadStats();
  } catch (err) {
    if (err && err.name === 'ProjectTierBlockedError') {
      // Render the rejection message + literal CLI hint via the shared
      // formatter so the Playwright spec exercises the same code path
      // (today's compose form doesn't expose a tier picker so this
      // catch-arm is dev-tools / API-only — see #924 PR description).
      showToast(formatProjectTierBlockedToast(err), 'error');
    } else {
      showToast(t('toast.save_failed', { error: err.message }), 'error');
    }
  } finally {
    btnLoading(btn, false);
  }
});

// ---------------------------------------------------------------------------
// File Upload
// ---------------------------------------------------------------------------

(function initUpload() {
  const drop     = qs('upload-drop');
  const input    = qs('upload-input');
  const list     = qs('upload-file-list');
  const btn      = qs('upload-btn');
  const msg      = qs('upload-msg');
  const result   = qs('upload-result');
  let selectedFiles = [];

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function renderFileList() {
    if (!selectedFiles.length) { hide(list); btn.disabled = true; return; }
    show(list);
    btn.disabled = false;
    list.innerHTML = '';
    selectedFiles.forEach((f, i) => {
      const row = document.createElement('div');
      row.className = 'upload-file-item';
      row.innerHTML = `
        <span class="upload-file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
        <span class="upload-file-size">${fmtSize(f.size)}</span>
        <button class="upload-file-remove" data-i="${i}" title="Remove">✕</button>
      `;
      list.appendChild(row);
    });
    list.querySelectorAll('.upload-file-remove').forEach(b => {
      b.addEventListener('click', () => {
        selectedFiles.splice(Number(b.dataset.i), 1);
        renderFileList();
      });
    });
  }

  function addFiles(files) {
    for (const f of files) {
      if (!selectedFiles.find(x => x.name === f.name && x.size === f.size)) {
        selectedFiles.push(f);
      }
    }
    renderFileList();
  }

  // Click on drop zone opens file picker
  drop.addEventListener('click', () => input.click());
  input.addEventListener('change', () => { addFiles(Array.from(input.files)); input.value = ''; });

  // Drag & drop
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('drag-over');
    addFiles(Array.from(e.dataTransfer.files));
  });

  btn.addEventListener('click', async () => {
    if (!selectedFiles.length) return;
    btnLoading(btn, true);
    hide(msg); hide(result);

    const form = new FormData();
    selectedFiles.forEach(f => form.append('files', f));

    try {
      const upload = await uploadFilesWithRedactionRetry(form);
      const data = upload.data;
      // Render per-file results
      show(result);
      result.innerHTML = '';
      data.files.forEach(r => {
        const row = document.createElement('div');
        row.className = 'upload-result-row';
        if (r.error) {
          row.innerHTML = `<span class="upload-result-err">✗</span><span>${escapeHtml(r.filename)}: ${escapeHtml(r.error)}</span>`;
        } else {
          row.innerHTML = `<span class="upload-result-ok">✓</span><span>${escapeHtml(r.filename)} — ${r.indexed_chunks} chunk${r.indexed_chunks !== 1 ? 's' : ''}</span>`;
        }
        result.appendChild(row);
      });
      // Mixed-batch refresh: the first ``_post(false)`` already persisted /
      // indexed every clean file in the batch (only the redaction-blocked
      // rows came back with an ``error`` field). Whether the user proceeds,
      // cancels, or the retry partially succeeds, the stats / source filter
      // / usage panels are stale and must refresh — the early-return cancel
      // path used to skip them, leaving the per-file result list showing
      // newly saved files while the rest of the UI lagged behind. Drop
      // through to the unified refresh below in every branch.
      if (upload.cancelled) {
        showToast(t('toast.upload_redaction_cancelled', { count: upload.blockedFileCount }), 'error');
        // Prune already-landed clean files from the selection so a
        // user-driven re-upload doesn't trip the server's
        // ``_{mtime_ns}`` collision suffix on rows that already exist on
        // disk (issue #803 — the same dup-write the narrowed retry
        // FormData fixed). The first-pass response aligns positionally
        // with ``selectedFiles`` (one row per input file, in order), so
        // an index-based filter is duplicate-basename-safe.
        selectedFiles = selectedFiles.filter((_, i) => data.files[i]?.error);
        renderFileList();
      } else {
        // Partial bypass: helper already emitted ``toast.redaction_bypass_partial``
        // with the succeeded/total counts. Skip the generic "Upload complete"
        // success toast (it would falsely audit a successful write on top of
        // the partial warning).
        const partial = upload.blockedFileCount > 0 && !upload.bypassed;
        if (!partial) {
          const firstPath = data.files.find(r => !r.error && r.path)?.path;
          const successMsg = firstPath
            ? t('toast.upload_complete_with_path', { count: data.total_indexed, path: tildifyPath(firstPath) })
            : t('toast.upload_complete', { count: data.total_indexed });
          showToast(successMsg, 'success');
          selectedFiles = [];
          renderFileList();
        } else {
          // Same prune as the cancel branch: clean files from the first
          // pass and any retry rows that landed are now on disk; only
          // rows that still carry ``error`` in mergedData should remain
          // selected so a follow-up Upload click sends just the unwritten
          // ones. Without this prune, partial bypass is functionally
          // identical to the original #803 dup bug for users who retry
          // after a partial outcome.
          selectedFiles = selectedFiles.filter((_, i) => data.files[i]?.error);
          renderFileList();
        }
      }
      _markDataStale();
      loadSourceFilter();
      loadStats();
      loadUploadUsage();
    } catch (err) {
      showToast(t('toast.upload_failed', { error: err.message }), 'error');
    } finally {
      btnLoading(btn, false);
    }
  });
})();

// Cumulative footprint of /api/upload's destination directory. Hidden when
// empty so a fresh-install Upload mode stays visually clean (issue #583).
async function loadUploadUsage() {
  const el = qs('upload-usage');
  const stats = qs('upload-usage-stats');
  if (!el || !stats) return;
  try {
    const res = await fetch('/api/uploads/usage');
    if (!res.ok) {
      console.warn('[upload-usage] /api/uploads/usage returned', res.status);
      hide(el);
      return;
    }
    const d = await res.json();
    if (!d.file_count) { hide(el); return; }
    const countKey = d.file_count === 1
      ? 'index.upload_usage_count_one'
      : 'index.upload_usage_count_other';
    const parts = [
      t(countKey, { count: d.file_count }),
      formatBytes(d.total_bytes),
    ];
    if (d.oldest_mtime !== null && d.oldest_mtime !== undefined) {
      parts.push(t('index.upload_usage_oldest', { rel: relativeTime(d.oldest_mtime * 1000) }));
    }
    stats.textContent = parts.join(' · ');
    show(el);
  } catch (err) {
    console.warn('[upload-usage] fetch failed', err);
    hide(el);
  }
}

// ---------------------------------------------------------------------------
// Tags tab
// ---------------------------------------------------------------------------

async function loadTags() {
  const emptyEl = qs('tags-empty');
  const listEl  = qs('tags-list');
  emptyEl.innerHTML = `<div class="spinner-panel"></div>${srLoading()}`;
  show(emptyEl);
  hide(listEl);
  hide(qs('tags-stats'));

  try {
    const data = await api('GET', '/api/tags');
    listEl.innerHTML = '';

    if (data.tags.length === 0) {
      emptyEl.innerHTML = emptyState('🏷', t('tags.empty_msg'), t('tags.empty_hint'));
      return;
    }

    STATE.lastTagsData = data.tags;

    // Compute and display stats
    _renderTagStats(data.tags);

    // Render with current filter/sort
    _renderTagViews();

    hide(emptyEl);
    // Show whichever view is active
    if (STATE.tagsView === 'cloud') { show(qs('tags-cloud')); hide(listEl); }
    else { show(listEl); hide(qs('tags-cloud')); }
  } catch (err) {
    emptyEl.innerHTML = emptyState('🏷', 'Failed to load tags: ' + err.message);
  }
}

function _renderTagStats(tags) {
  const statsEl = qs('tags-stats');
  if (!tags.length) { hide(statsEl); return; }
  const total = tags.length;
  const totalChunks = tags.reduce((s, t) => s + t.count, 0);
  const avgPerChunk = totalChunks > 0 ? (totalChunks / Math.max(total, 1)).toFixed(1) : '0';
  const top3 = tags.slice(0, 3).map(t => t.tag);
  statsEl.innerHTML =
    `<span class="tags-stats-label">${total}</span> tags` +
    `<span class="tags-stats-sep">|</span>` +
    `<span class="tags-stats-label">${avgPerChunk}</span> avg uses` +
    `<span class="tags-stats-sep">|</span>` +
    `Top: ${top3.map(t => `<span style="color:${_tagColor(t)}">${escapeHtml(t)}</span>`).join(', ')}`;
  show(statsEl);
}

function sortTags(tags) {
  const sorted = [...tags];
  switch (STATE.tagsSortBy) {
    case 'count-desc': sorted.sort((a, b) => b.count - a.count); break;
    case 'count-asc':  sorted.sort((a, b) => a.count - b.count); break;
    case 'az':         sorted.sort((a, b) => a.tag.localeCompare(b.tag)); break;
    case 'za':         sorted.sort((a, b) => b.tag.localeCompare(a.tag)); break;
  }
  return sorted;
}

function _getFilteredTags() {
  const q = (qs('tags-search').value || '').trim().toLowerCase();
  let tags = STATE.lastTagsData;
  if (q) tags = tags.filter(t => t.tag.toLowerCase().includes(q));
  return sortTags(tags);
}

function _renderTagViews() {
  const tags = _getFilteredTags();
  const maxCount = tags.reduce((m, t) => Math.max(m, t.count), 1);
  const listEl = qs('tags-list');

  // Render list view
  listEl.innerHTML = '';
  tags.forEach(({ tag, count }) => {
    const row = document.createElement('div');
    row.className = 'tag-row';
    const pct = Math.round((count / maxCount) * 100);
    const color = _tagColor(tag);
    row.innerHTML = `
      <span class="tag-name" style="color:${color}">${escapeHtml(tag)}</span>
      <div class="tag-bar-wrap">
        <div class="tag-bar" style="width:${pct}%;background:${color}"></div>
      </div>
      <span class="tag-count">${count}</span>
      <div class="tag-actions">
        <button type="button" class="tag-action-btn" data-act="rename"
          aria-label="${escapeAttr(t('tags.manage_rename') + ': ' + tag)}" data-i18n="tags.manage_rename">${escapeHtml(t('tags.manage_rename'))}</button>
        <button type="button" class="tag-action-btn" data-act="merge"
          aria-label="${escapeAttr(t('tags.manage_merge') + ': ' + tag)}" data-i18n="tags.manage_merge">${escapeHtml(t('tags.manage_merge'))}</button>
        <button type="button" class="tag-action-btn tag-action-danger" data-act="delete"
          aria-label="${escapeAttr(t('tags.manage_delete') + ': ' + tag)}" data-i18n="tags.manage_delete">${escapeHtml(t('tags.manage_delete'))}</button>
      </div>`;
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => _searchByTag(tag));
    // Manage actions must not also trigger the row's search-by-tag click.
    row.querySelectorAll('.tag-action-btn').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        manageTag(btn.dataset.act, tag);
      });
    });
    listEl.appendChild(row);
  });

  // Render cloud view
  _renderTagCloud(tags, maxCount);
}

// Tag cloud helpers
// STATE.lastTagsData, STATE.tagsView, STATE.tagsSortBy now in STATE

// Deterministic color from tag string (hue rotation, pastel)
function _tagColor(tag) {
  const colors = localStorage.getItem('m2m-tag-colors');
  const map = colors ? JSON.parse(colors) : {};
  if (map[tag]) return map[tag];
  let hash = 0;
  for (let i = 0; i < tag.length; i++) hash = tag.charCodeAt(i) + ((hash << 5) - hash);
  const hue = ((hash % 360) + 360) % 360;
  return `hsl(${hue}, 60%, 65%)`;
}

function _renderTagCloud(tags, maxCount) {
  const cloud = qs('tags-cloud');
  const minSize = 0.75, maxSize = 2.6;
  // Stable sort by count descending (largest first) — no random shuffle
  const stable = [...tags].sort((a, b) => b.count - a.count);
  cloud.innerHTML = stable.map(({ tag, count }) => {
    const ratio = maxCount > 1 ? (count - 1) / (maxCount - 1) : 0;
    const size = minSize + ratio * (maxSize - minSize);
    const color = _tagColor(tag);
    // Deterministic rotation & offset from tag hash
    let h = 0;
    for (let i = 0; i < tag.length; i++) h = tag.charCodeAt(i) + ((h << 5) - h);
    const rot = ((h % 25) - 12).toFixed(1);
    const yOff = ((h >> 4) % 15) - 7;
    const pad = 2 + Math.abs((h >> 8) % 7);
    return `<span class="tag-cloud-item" style="font-size:${size.toFixed(2)}rem;color:${color};transform:rotate(${rot}deg) translateY(${yOff}px);padding:${pad}px ${pad + 4}px"
      title="${escapeAttr(tag)}: ${count} chunks" data-tag="${escapeAttr(tag)}">${escapeHtml(tag)}</span>`;
  }).join('');
  cloud.querySelectorAll('.tag-cloud-item').forEach(el => {
    el.addEventListener('click', () => _searchByTag(el.dataset.tag));
  });
}

// ---------------------------------------------------------------------------
// Tag management — rename / delete / merge (#688)
//
// Every action runs a dry-run first (authoritative affected count + sample,
// global across scopes — see #1175) and only writes on an explicit confirm.
// rename and merge collect a value (new name / merge target) in a first
// "input" phase, then transition to the shared "preview" phase; delete goes
// straight to preview. All three call the shared tag-management service via
// /api/tags/* so Web, MCP, and the `mm tags` CLI stay symmetric.
// ---------------------------------------------------------------------------
const _TAG_ACTIONS = {
  rename: {
    needsInput: true, danger: false,
    titleKey: 'tags.manage_rename_title',
    inputLabelKey: 'tags.manage_new_name_label',
    applyKey: 'tags.manage_rename',
    dryRun: (tag, v) => api('PUT', `/api/tags/${encodeURIComponent(tag)}?dry_run=true`, { new_name: v }),
    apply: (tag, v) => api('PUT', `/api/tags/${encodeURIComponent(tag)}`, { new_name: v }),
    doneToast: (tag, v, n) => t('tags.manage_rename_done', { old: tag, new: v, count: n }),
  },
  merge: {
    needsInput: true, danger: false,
    titleKey: 'tags.manage_merge_title',
    inputLabelKey: 'tags.manage_target_label',
    applyKey: 'tags.manage_merge',
    dryRun: (tag, v) => api('POST', '/api/tags/merge?dry_run=true', { sources: [tag], target: v }),
    apply: (tag, v) => api('POST', '/api/tags/merge', { sources: [tag], target: v }),
    doneToast: (tag, v, n) => t('tags.manage_merge_done', { source: tag, target: v, count: n }),
  },
  delete: {
    needsInput: false, danger: true,
    titleKey: 'tags.manage_delete_title',
    applyKey: 'tags.manage_delete',
    dryRun: tag => api('DELETE', `/api/tags/${encodeURIComponent(tag)}?dry_run=true`),
    apply: tag => api('DELETE', `/api/tags/${encodeURIComponent(tag)}`),
    doneToast: (tag, _v, n) => t('tags.manage_delete_done', { tag, count: n }),
  },
};

// Monotonic token shared across tag-manage modal opens: every dry-run / apply
// captures the current value and bails on return if a later edit, close, or
// reopen has advanced it. Keeps apply pinned to the previewed value.
let _tagReqSeq = 0;

function manageTag(action, tag) {
  const cfg = _TAG_ACTIONS[action];
  if (!cfg) return;

  const modal = qs('tag-manage-modal');
  const inputRow = qs('tag-manage-input-row');
  const input = qs('tag-manage-input');
  const inputLabel = qs('tag-manage-input-label');
  const impactEl = qs('tag-manage-impact');
  const samplesEl = qs('tag-manage-samples');
  const errEl = qs('tag-manage-error');
  const okBtn = qs('tag-manage-ok-btn');
  const cancelBtn = qs('tag-manage-cancel-btn');

  qs('tag-manage-title').textContent = t(cfg.titleKey, { tag });
  okBtn.className = cfg.danger ? 'btn-danger' : 'btn-primary';
  impactEl.textContent = '';
  samplesEl.innerHTML = '';
  errEl.hidden = true; errEl.textContent = '';
  input.disabled = false;

  let phase = cfg.needsInput ? 'input' : 'preview';
  // The value whose dry-run preview is currently shown. apply() may only run
  // for this exact value, so an edit in flight can never write an un-previewed
  // value. ``_tagReqSeq`` (module-level) tags every async request; a response
  // that returns after a later edit, a close, or a reopen no longer matches
  // the live seq and is dropped — preventing a stale dry-run/apply from
  // mutating a reused modal.
  let previewedValue = null;

  if (cfg.needsInput) {
    inputLabel.textContent = t(cfg.inputLabelKey);
    input.value = '';
    inputRow.hidden = false;
  } else {
    inputRow.hidden = true;
  }

  function setOk(label, disabled) { okBtn.textContent = label; okBtn.disabled = !!disabled; }
  function showError(msg) { errEl.textContent = msg; errEl.hidden = false; }
  function value() { return cfg.needsInput ? input.value.trim() : null; }

  function renderPreview(res) {
    impactEl.textContent = res.affected_chunks > 0
      ? t('tags.manage_impact', { count: res.affected_chunks })
      : t('tags.manage_impact_none');
    samplesEl.innerHTML = '';
    (res.samples || []).forEach(s => {
      const div = document.createElement('div');
      div.className = 'tag-manage-sample';
      const src = document.createElement('span');
      src.className = 'tag-manage-sample-src';
      src.textContent = s.source_file;
      const prev = document.createElement('span');
      prev.className = 'tag-manage-sample-preview';
      prev.textContent = s.content_preview;
      div.appendChild(src); div.appendChild(prev);
      samplesEl.appendChild(div);
    });
  }

  async function enterPreview() {
    if (cfg.needsInput && !value()) { showError(t('tags.manage_input_required')); return; }
    const submitted = value();
    const myReq = ++_tagReqSeq;
    errEl.hidden = true;
    setOk('…', true);
    let res;
    try {
      res = await cfg.dryRun(tag, submitted);
    } catch (err) {
      if (myReq !== _tagReqSeq) return; // superseded by an edit / close / reopen
      // Backend 400 (empty / same-name / etc.) — stay in input phase so the
      // user can correct the value without reopening.
      showError(err.message || String(err));
      phase = 'input';
      setOk(cfg.needsInput ? t('tags.manage_preview') : t(cfg.applyKey), false);
      return;
    }
    if (myReq !== _tagReqSeq) return;   // a newer request (or a close) won
    renderPreview(res);
    phase = 'preview';
    previewedValue = submitted;
    // affected_chunks === 0 ⇒ nothing to apply; keep OK disabled. The input
    // stays editable; any edit reverts to the input phase via onInput().
    setOk(t(cfg.applyKey), res.affected_chunks === 0);
    if (!cfg.danger && res.affected_chunks > 0) okBtn.focus();
  }

  async function applyOp() {
    // Defence in depth: never write a value that differs from the previewed
    // one — onInput() already reverts to the input phase on any edit.
    if (cfg.needsInput && value() !== previewedValue) { onInput(); return; }
    const myReq = ++_tagReqSeq;
    const applied = cfg.needsInput ? previewedValue : null;
    setOk('…', true);
    let res;
    try {
      res = await cfg.apply(tag, applied);
    } catch (err) {
      if (myReq !== _tagReqSeq) return;
      showError(err.message || String(err));
      setOk(t(cfg.applyKey), false);
      return;
    }
    if (myReq !== _tagReqSeq) return;   // modal closed mid-apply
    cleanup();
    showToast(cfg.doneToast(tag, cfg.needsInput ? applied : tag, res.affected_chunks), 'success');
    loadTags();
  }

  function onOk() {
    if (okBtn.disabled) return;          // in-flight, or nothing to apply
    (phase === 'input' ? enterPreview : applyOp)();
  }

  // Any edit invalidates the last preview — whether it is already shown or
  // still in flight. Bump the request seq (so a pending dry-run is dropped on
  // return rather than rendering against the new value) and reset to a clean
  // input phase. Runs on every keystroke; that is fine — it is just an int
  // bump plus idempotent DOM clears.
  function onInput() {
    _tagReqSeq++;
    phase = 'input';
    previewedValue = null;
    impactEl.textContent = '';
    samplesEl.innerHTML = '';
    setOk(t('tags.manage_preview'), false);
  }

  // --- show + a11y (mirrors showConfirm) ---
  show(modal);
  const focusables = () =>
    [input, cancelBtn, okBtn].filter(
      el => el && !el.disabled && !(el === input && inputRow.hidden)
    );
  const releaseA11y = openModalA11y(modal);
  (cfg.needsInput ? input : cancelBtn).focus();
  registerModalCloser(modal, () => cleanup());

  function cleanup() {
    _tagReqSeq++;            // invalidate any in-flight dry-run / apply
    hide(modal);
    releaseA11y();
    _MODAL_CLOSERS.delete(modal);
    inputRow.hidden = true;
    input.disabled = false;
    modal.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKey, true);
    okBtn.onclick = null; cancelBtn.onclick = null;
    input.oninput = null; input.onkeydown = null;
  }
  function onBackdrop(e) { if (e.target === modal) cleanup(); }
  function onKey(e) {
    if (e.key === 'Escape') { e.stopPropagation(); cleanup(); return; }
    if (e.key === 'Tab') {
      e.preventDefault();
      const f = focusables();
      const idx = f.indexOf(document.activeElement);
      f[(idx + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
    }
  }
  modal.addEventListener('click', onBackdrop);
  document.addEventListener('keydown', onKey, true);
  okBtn.onclick = onOk;
  cancelBtn.onclick = () => cleanup();
  input.oninput = onInput;
  input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); onOk(); } };

  if (phase === 'input') setOk(t('tags.manage_preview'), false);
  else enterPreview(); // delete: fetch the preview immediately
}

function _searchByTag(tag) {
  // Navigate to Search tab with tag filter pre-filled.
  // search-input is intentionally left untouched so the tag is a filter axis
  // only, not a duplicate BM25 query. doSearch early-returns on empty q.
  document.querySelector('[data-tab="search"]').click();
  const filters = document.querySelector('.search-filters');
  if (filters.hidden) qs('filter-toggle').click();
  qs('tag-filter').value = tag;
  doSearch();
}

// View toggle
qs('tags-cloud-btn').addEventListener('click', () => {
  STATE.tagsView = 'cloud';
  qs('tags-cloud-btn').classList.add('btn-active');
  qs('tags-list-btn').classList.remove('btn-active');
  show(qs('tags-cloud')); hide(qs('tags-list'));
});
qs('tags-list-btn').addEventListener('click', () => {
  STATE.tagsView = 'list';
  qs('tags-list-btn').classList.add('btn-active');
  qs('tags-cloud-btn').classList.remove('btn-active');
  show(qs('tags-list')); hide(qs('tags-cloud'));
});

// Tag search/filter
qs('tags-search').addEventListener('input', () => {
  if (STATE.lastTagsData.length) _renderTagViews();
});

// Tag sort controls
document.querySelectorAll('.tags-sort-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    STATE.tagsSortBy = btn.dataset.sort;
    document.querySelectorAll('.tags-sort-btn').forEach(b => b.classList.remove('btn-active'));
    btn.classList.add('btn-active');
    if (STATE.lastTagsData.length) _renderTagViews();
  });
});

function _renderAutoTagSample(s) {
  const fname = (s.source_file || '').split('/').pop() || s.source_file || '';
  const noneLabel = (typeof t === 'function')
    ? t('tags.autotag_sample_no_current_tags')
    : '(none)';
  const currentLabel = (typeof t === 'function') ? t('tags.autotag_sample_current') : 'Current:';
  const suggestedLabel = (typeof t === 'function') ? t('tags.autotag_sample_suggested') : 'Suggested:';
  const current = (s.current_tags || []).length
    ? s.current_tags
        .map(tg => `<span class="autotag-tag autotag-tag-current">${escapeHtml(tg)}</span>`)
        .join('')
    : `<span class="autotag-no-tags">${escapeHtml(noneLabel)}</span>`;
  const suggested = (s.suggested_tags || [])
    .map(tg => `<span class="autotag-tag autotag-tag-suggested">${escapeHtml(tg)}</span>`)
    .join('');
  return `
    <div class="autotag-sample-card">
      <div class="autotag-sample-header">
        <span class="autotag-sample-source" title="${escapeHtml(s.source_file || '')}">${escapeHtml(fname)}</span>
        <span class="autotag-sample-id">${escapeHtml(s.chunk_id || '')}</span>
      </div>
      <div class="autotag-sample-preview">${escapeHtml(s.content_preview || '')}</div>
      <div class="autotag-sample-tag-row">
        <span class="autotag-sample-label">${escapeHtml(currentLabel)}</span>
        ${current}
      </div>
      <div class="autotag-sample-tag-row">
        <span class="autotag-sample-label">${escapeHtml(suggestedLabel)}</span>
        ${suggested}
      </div>
    </div>`;
}

async function runAutoTag() {
  const source  = qs('autotag-source').value.trim() || null;
  const maxTags = parseInt(qs('autotag-max').value) || 5;
  const overwrite = qs('autotag-overwrite').checked;
  const dryRun    = qs('autotag-dry-run').checked;

  const autotagBtn = qs('autotag-btn');
  btnLoading(autotagBtn, true);
  hide(qs('autotag-result'));
  try {
    const data = await api('POST', '/api/tags/auto', {
      source_filter: source,
      max_tags: maxTags,
      overwrite,
      dry_run: dryRun,
    });
    qs('at-total').textContent   = data.total_chunks;
    qs('at-tagged').textContent  = data.tagged_chunks;
    qs('at-skipped').textContent = data.skipped_chunks;
    show(qs('autotag-result'));

    const samplesEl = qs('autotag-samples');
    const samplesList = qs('autotag-samples-list');
    const samplesCount = qs('autotag-samples-count');
    if (samplesEl && samplesList && Array.isArray(data.samples) && data.samples.length) {
      samplesCount.textContent = `(${data.samples.length})`;
      samplesList.innerHTML = data.samples.map(_renderAutoTagSample).join('');
      samplesEl.hidden = false;
    } else if (samplesEl) {
      samplesEl.hidden = true;
      if (samplesList) samplesList.innerHTML = '';
    }

    const label = dryRun ? '(dry run) ' : '';
    showToast(t('toast.tagged_count', { label, count: data.tagged_chunks }), 'success');
    if (!dryRun) { loadTags(); loadStats(); _markDataStale(); }
  } catch (err) {
    showToast(t('toast.autotag_failed', { error: err.message }), 'error');
  } finally {
    btnLoading(autotagBtn, false);
  }
}

// ---------------------------------------------------------------------------
// Index (SSE-streamed; folder-mode primary action)
// ---------------------------------------------------------------------------

async function runIndexStream() {
  const path     = qs('index-path').value.trim();
  if (!path) { setMsg(qs('index-msg'), 'Please enter a path to index.', true); return; }
  if (!(await _indexingTryStartOrRefresh())) return;
  const recursive = qs('index-recursive').checked;
  const force     = qs('index-force').checked;
  const namespace = qs('index-namespace').value.trim();
  const registerAsSource = qs('index-register-source')?.checked === true;

  const progressEl = qs('index-progress');
  const barEl      = qs('index-progress-bar');
  const labelEl    = qs('index-progress-label');
  const fileEl     = qs('index-progress-file');
  const resultEl   = qs('index-result');
  const btn        = qs('index-btn');

  show(progressEl); hide(resultEl); hide(qs('index-msg'));
  barEl.style.width = '0%';
  labelEl.textContent = 'Starting…';
  fileEl.textContent = '';

  btnLoading(btn, true);

  // ``new EventSource`` can throw synchronously (malformed URL, browser
  // storage policy edge cases). Without this guard a throw here would
  // leave ``STATE.indexing`` stuck on ``true`` and the indicator
  // permanently visible until page reload, blocking every subsequent
  // indexing trigger across all surfaces.
  let es;
  try {
    const paramObj = { path, recursive, force };
    if (namespace) paramObj.namespace = namespace;
    const params = new URLSearchParams(paramObj);
    es = new EventSource(`/api/index/stream?${params}`);
  } catch (err) {
    console.error('[index-stream] failed to open stream:', err);
    showToast(t('toast.stream_fallback'), 'error');
    hide(progressEl);
    btnLoading(btn, false);
    _indexingEnd();
    return;
  }
  let _sseFailCount = 0;
  const _SSE_MAX_FAILS = 3;
  const _chunkProgress = makeChunkProgressRenderer({
    targetEl: fileEl,
    formatKey: 'common.file_chunk_progress',
  });

  es.onmessage = (e) => {
    let event;
    try { event = JSON.parse(e.data); }
    catch {
      _sseFailCount++;
      console.warn(`[index-stream] malformed SSE (${_sseFailCount}/${_SSE_MAX_FAILS}):`, e.data);
      if (_sseFailCount >= _SSE_MAX_FAILS) {
        es.close();
        showToast(t('toast.stream_fallback'), 'error');
        hide(progressEl);
        btnLoading(btn, false);
        _indexingEnd();
      }
      return;
    }
    _sseFailCount = 0;
    if (event.type === 'chunk_progress') {
      _chunkProgress.onChunk(event);
      return;
    }
    if (event.type === 'progress') {
      _chunkProgress.onProgressBoundary();
      const pct = event.files_total > 0
        ? Math.round((event.files_done / event.files_total) * 100) : 0;
      barEl.style.width = pct + '%';
      labelEl.textContent = t('index.progress_files', {
        done: event.files_done,
        total: event.files_total,
      });
      fileEl.textContent  = basename(event.file);
    } else if (event.type === 'complete') {
      es.close();
      barEl.style.width = '100%';
      labelEl.textContent = t('index.progress_done', { count: event.total_files });
      fileEl.textContent  = '';

      qs('r-files').textContent   = event.total_files;
      qs('r-chunks').textContent  = event.total_chunks;
      qs('r-indexed').textContent = event.indexed_chunks;
      qs('r-skipped').textContent = event.skipped_chunks;
      qs('r-deleted').textContent = event.deleted_chunks;
      qs('r-duration').textContent = `${event.duration_ms.toFixed(0)} ms`;
      const nsCell = qs('r-namespace');
      if (nsCell) {
        nsCell.textContent = renderResolvedNamespaces(event.resolved_namespaces, { mode: 'applied' });
      }

      // #354 / #590: ``complete.errors`` may be present even on HTTP-200
      // streams (e.g. ONNX missing on a subset of files, binary/too-large
      // skips). Surface the same partial-failure UX as the previous
      // non-stream POST handler — red toast + visible error row capped at
      // 5 entries with a "+N more" tail.
      const errList = Array.isArray(event.errors) ? event.errors : [];
      const errRow = qs('r-errors-row');
      if (errList.length > 0) {
        const shown = errList.slice(0, 5);
        const more = errList.length - shown.length;
        qs('r-errors').textContent = (
          more > 0 ? [...shown, `…and ${more} more`] : shown
        ).join('\n');
        errRow.hidden = false;
        showToast(
          t('toast.index_partial', {
            count: event.indexed_chunks,
            errors: errList.length,
            first: errList[0],
          }),
          'error',
        );
      } else {
        qs('r-errors').textContent = '';
        errRow.hidden = true;
        if (registerAsSource) {
          // Checkbox already opted in to persistent registration, so skip
          // the toast's "Register as Source" action — it would just be a
          // duplicate of what we're about to do.
          showToast(t('toast.indexed_count', { count: event.indexed_chunks }), 'success');
          api('POST', '/api/memory-dirs/add', { path, auto_index: false })
            .then(() => {
              showToast(t('toast.index_registered_as_source', { path }), 'success');
              loadStats();
            })
            .catch(err => {
              showToast(
                t('toast.index_register_failed', { error: err?.message || String(err) }),
                'error',
              );
            });
        } else {
          showToast(t('toast.indexed_count', { count: event.indexed_chunks }), 'success', {
            action: {
              label: t('toast.action.register_persistent'),
              onClick: goToSourcesAddPath,
            },
          });
        }
      }

      show(resultEl);
      _markDataStale();
      loadStats();
      loadNamespaceDropdowns();
      loadSourceFilter();
      btnLoading(btn, false);
      _indexingEnd();
    }
  };

  es.onerror = () => {
    es.close();
    showToast(t('toast.stream_fallback'), 'error');
    hide(progressEl);
    btnLoading(btn, false);
    _indexingEnd();
  };
}

qs('index-btn').addEventListener('click', runIndexStream);

qs('refresh-tags-btn').addEventListener('click', loadTags);
qs('autotag-btn').addEventListener('click', runAutoTag);


// ---------------------------------------------------------------------------
// Find Similar
// ---------------------------------------------------------------------------

qs('d-similar-btn').addEventListener('click', findSimilar);
qs('similar-close-btn').addEventListener('click', () => hide(qs('similar-panel')));

async function findSimilar() {
  if (!STATE.selectedChunkId) return;
  const panel = qs('similar-panel');
  const list  = qs('similar-list');
  show(panel);
  list.innerHTML = '<div class="empty-state" style="height:60px"><p>Loading…</p></div>';

  try {
    const data = await api('GET', `/api/chunks/${STATE.selectedChunkId}/similar?top_k=5`);
    if (!data.results.length) {
      list.innerHTML = '<div class="empty-state" style="height:60px"><p>No similar chunks found</p></div>';
      return;
    }
    list.innerHTML = '';
    data.results.forEach(r => {
      const card = document.createElement('div');
      card.className = 'similar-card';
      card.innerHTML = `
        <div class="similar-card-meta">
          <span class="score-badge">${r.score.toFixed(3)}</span>
          <span class="file-path" style="font-size:0.72rem">${escapeHtml(truncate(r.chunk.source_file, 55))}</span>
        </div>
        <div class="similar-card-content">${escapeHtml(truncate(r.chunk.content, 180))}</div>
      `;
      card.addEventListener('click', () => {
        showDetail(r);
        hide(qs('similar-panel'));
        document.querySelectorAll('.result-item').forEach(el => el.classList.remove('selected'));
      });
      list.appendChild(card);
    });
  } catch (err) {
    list.innerHTML = `<div class="empty-state" style="height:60px"><p>Error: ${escapeHtml(err.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------------------
// XSS helpers + highlighting
// ---------------------------------------------------------------------------

/**
 * Highlight query tokens in text. Returns HTML string with <mark> wrapping matches.
 * Safely escapes all content to prevent XSS.
 */
function highlightText(text, query) {
  const escaped = escapeHtml(text);
  if (!query) return escaped;

  // Split query into non-empty tokens (word characters, 2+ chars)
  const tokens = query.split(/\s+/).filter(t => t.length >= 2);
  if (!tokens.length) return escaped;

  // Build alternation regex from escaped token literals
  const pattern = tokens
    .map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
  const re = new RegExp(`(${pattern})`, 'gi');
  return escaped.replace(re, '<mark>$1</mark>');
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
// Alias kept for caller-side intent (attribute vs body context). escapeHtml
// already emits the full ``& < > " '`` set so a single-quoted attribute is
// also safe — past redundant ``.replace(/"/g, ...)`` was a no-op once the
// quote handling moved into escapeHtml.
function escapeAttr(str) { return escapeHtml(str); }

// ---------------------------------------------------------------------------
// Search History (A)
// ---------------------------------------------------------------------------

const HISTORY_KEY = 'memtomem_search_history';
const HISTORY_MAX = 10;

function _loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch { return []; }
}
function saveToHistory(query) {
  if (!query) return;
  let h = _loadHistory().filter(q => q !== query);
  h.unshift(query);
  localStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(0, HISTORY_MAX)));
}
function _removeFromHistory(query) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(_loadHistory().filter(q => q !== query)));
}

function renderHistoryDropdown() {
  const dropdown = qs('search-history-dropdown');
  const filter   = qs('search-input').value.trim().toLowerCase();
  let history    = _loadHistory();
  if (filter) history = history.filter(q => q.toLowerCase().includes(filter));
  if (!history.length) { hide(dropdown); return; }

  dropdown.innerHTML = '';
  history.forEach(q => {
    const item = document.createElement('div');
    item.className = 'history-item';
    item.innerHTML = `<span class="history-text">${escapeHtml(q)}</span><button class="history-remove" title="${t('search.history_remove_title')}">✕</button>`;
    item.querySelector('.history-text').addEventListener('mousedown', e => {
      e.preventDefault(); // keep focus on input
      qs('search-input').value = q;
      hide(dropdown);
      doSearch();
    });
    item.querySelector('.history-remove').addEventListener('mousedown', e => {
      e.preventDefault();
      _removeFromHistory(q);
      renderHistoryDropdown();
    });
    dropdown.appendChild(item);
  });

  const clearAll = document.createElement('div');
  clearAll.className = 'history-clear-all';
  clearAll.textContent = t('search.history_clear');
  clearAll.addEventListener('mousedown', e => {
    e.preventDefault();
    localStorage.removeItem(HISTORY_KEY);
    hide(dropdown);
  });
  dropdown.appendChild(clearAll);
  show(dropdown);
}

function renderRecentChips() {
  const container = qs('recent-chips');
  if (!container) return;
  const history = _loadHistory();
  if (!history.length) { hide(container); return; }
  container.innerHTML = `<span class="recent-chips-label">${t('search.recent_label')}</span>` +
    history.slice(0, 6).map(q =>
      `<button class="recent-chip" title="${escapeAttr(q)}">${escapeHtml(truncate(q, 24))}</button>`
    ).join('');
  container.querySelectorAll('.recent-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      qs('search-input').value = btn.title;
      doSearch();
    });
  });
  show(container);
}

// Render chips on initial load
// renderRecentChips() is now called from the unified DOMContentLoaded handler in initTheme()

// S1.5 — first-run welcome card. It sits next to #results-empty (a sibling,
// so the empty render path that rewrites #results-empty.innerHTML can't wipe
// it) and is shown only while the search panel is in its empty state and the
// card hasn't been dismissed (persisted in localStorage).
function _welcomeDismissed() {
  try { return !!localStorage.getItem('m2m-welcome-dismissed'); } catch { return false; }
}

function _syncWelcomeVisibility() {
  const welcome = qs('search-welcome');
  if (!welcome) return;
  const empty = qs('results-empty');
  const emptyShown = !!empty && !empty.hidden;
  welcome.hidden = _welcomeDismissed() || !emptyShown;
}

function _initSearchWelcome() {
  const welcome = qs('search-welcome');
  if (!welcome) return;
  qs('search-welcome-dismiss')?.addEventListener('click', () => {
    try { localStorage.setItem('m2m-welcome-dismissed', '1'); } catch {}
    welcome.hidden = true;
  });
  // "Add a memory" → the Index tab; "Try a search" → focus the query box.
  qs('search-welcome-add')?.addEventListener('click', () => activateTab('index'));
  qs('search-welcome-search')?.addEventListener('click', () => qs('search-input')?.focus());
  _syncWelcomeVisibility();
}

// S2.3 — first-run Home orientation block. It's a <details open>, so the first
// visit lands expanded; we persist the collapse choice (m2m-home-orientation-
// collapsed) and mirror the open state onto .home-layout so the activity
// heatmap's confusing "sample" summary stays hidden while a new user is still
// orienting. The three CTAs jump to the add → connect → search journey.
function _initHomeOrientation() {
  const details = qs('home-orientation');
  if (!details) return;
  const layout = details.closest('.home-layout');
  let collapsed = false;
  try { collapsed = localStorage.getItem('m2m-home-orientation-collapsed') === '1'; } catch {}
  if (collapsed) details.open = false;
  const syncLayout = () => layout?.classList.toggle('orientation-open', details.open);
  syncLayout();
  details.addEventListener('toggle', () => {
    try { localStorage.setItem('m2m-home-orientation-collapsed', details.open ? '0' : '1'); } catch {}
    syncLayout();
  });
  qs('home-orientation-add')?.addEventListener('click', () => activateTab('index'));
  qs('home-orientation-connect')?.addEventListener('click', () => activateTab('context-gateway'));
  qs('home-orientation-search')?.addEventListener('click', () => activateTab('search'));
}

// S3.1 — first-run wizard. A one-time, three-step guided intro (add → search →
// connect, matching the orientation block's order and reusing its copy) shown
// over Home on a genuine first run. The trigger lives in _applyLandingTab, on
// the same ``firstRun && stamped`` branch that routes to Home — so it inherits
// the once-only guarantee (m2m-app-initialized) and the private-mode guard (no
// stamp → no wizard) for free, with no extra storage key to gate on. Reuses the
// modal a11y stack (openModal pushes onto _ACTIVE_MODALS / traps focus); the
// Esc dispatcher and registerModalCloser route the close back through
// _closeFirstRunWizard. The wizard never calls activateTab on its own to open,
// so the landing-tab tests' activateTab spy stays clean.
const FR_WIZARD_TOTAL_STEPS = 3;
let _frWizardStep = 1;
let _frWizardRelease = null;
let _frWizardInited = false;

function _renderFirstRunWizardStep() {
  const modal = qs('first-run-wizard');
  if (!modal) return;
  modal.querySelectorAll('.fr-wizard-step').forEach(sec => {
    sec.hidden = Number(sec.dataset.step) !== _frWizardStep;
  });
  modal.querySelectorAll('.fr-wizard-dot').forEach(dot => {
    const n = Number(dot.dataset.step);
    dot.classList.toggle('is-active', n === _frWizardStep);
    dot.classList.toggle('is-done', n < _frWizardStep);
  });
  // Counter + Next/Done label are JS-rendered (params / step-dependent), so they
  // re-render here on every step change and on the langchange hook below — the
  // static data-i18n labels are handled by applyDOM. _frWizardText() falls back
  // to English when t() returns the raw key, because the wizard can open from the
  // synchronous landing handler before I18N.init() loads the locale cache — and a
  // raw "wizard.step_counter" flash is exactly the jargon this onboarding fights.
  // The English fallbacks equal the en.json values, so en users see no change and
  // the langchange re-render swaps in the real locale (e.g. KO) once init fires.
  const counter = qs('fr-wizard-counter-text');
  if (counter) {
    counter.textContent = _frWizardText(
      'wizard.step_counter', { n: _frWizardStep, total: FR_WIZARD_TOTAL_STEPS },
      `Step ${_frWizardStep} of ${FR_WIZARD_TOTAL_STEPS}`);
  }
  const next = qs('fr-wizard-next');
  if (next) {
    next.textContent = _frWizardStep === FR_WIZARD_TOTAL_STEPS
      ? _frWizardText('common.done', null, 'Done')
      : _frWizardText('wizard.next', null, 'Next');
  }
  const back = qs('fr-wizard-back');
  if (back) back.hidden = _frWizardStep === 1;
}

// t() returns the raw key when the locale cache is not loaded yet; substitute an
// explicit English fallback so a pre-init render degrades like a static
// data-i18n default instead of showing the key verbatim.
function _frWizardText(key, params, fallback) {
  const s = (typeof t === 'function') ? t(key, params) : key;
  return s === key ? fallback : s;
}

function _closeFirstRunWizard() {
  const modal = qs('first-run-wizard');
  if (!modal) return;
  hide(modal);
  if (_frWizardRelease) { _frWizardRelease(); _frWizardRelease = null; }
  // The user just walked the same three steps, so collapse the always-visible
  // Home orientation recap (still expandable) instead of showing it twice.
  // Setting .open fires the orientation block's toggle handler, which persists
  // the collapsed choice.
  const details = qs('home-orientation');
  if (details && details.open) details.open = false;
}

function _showFirstRunWizard() {
  const modal = qs('first-run-wizard');
  if (!modal || typeof window.openModal !== 'function') return;
  // The async boot handler (initTheme's DOMContentLoaded listener) is still
  // suspended on ``await I18N.init()`` when the *separate*, synchronous landing
  // listener opens the wizard — so _initFirstRunWizard() (line ~149) has not run
  // yet and the closer is unregistered. Wire it here (idempotent) before opening,
  // or an Esc/click in that window would route through closeModal's fallback
  // hide() and never call _frWizardRelease(), leaving the modal a11y stack
  // (_ACTIVE_MODALS / background inert / focus trap) stuck.
  _initFirstRunWizard();
  _frWizardStep = 1;
  _renderFirstRunWizardStep();
  _frWizardRelease = window.openModal(modal, {
    focusables: () => Array.from(modal.querySelectorAll('button'))
      .filter(el => !el.disabled && !el.closest('[hidden]')),
  });
}

function _initFirstRunWizard() {
  const modal = qs('first-run-wizard');
  if (!modal || _frWizardInited) return;  // idempotent: show-path + DCL both call this
  _frWizardInited = true;
  registerModalCloser(modal, _closeFirstRunWizard);
  qs('fr-wizard-close')?.addEventListener('click', _closeFirstRunWizard);
  qs('fr-wizard-skip')?.addEventListener('click', _closeFirstRunWizard);
  qs('fr-wizard-back')?.addEventListener('click', () => {
    if (_frWizardStep > 1) { _frWizardStep -= 1; _renderFirstRunWizardStep(); }
  });
  qs('fr-wizard-next')?.addEventListener('click', () => {
    if (_frWizardStep < FR_WIZARD_TOTAL_STEPS) { _frWizardStep += 1; _renderFirstRunWizardStep(); }
    else _closeFirstRunWizard();
  });
  // Each step's primary CTA drops the user straight into that tab and closes the
  // wizard (the happy path); Next/Back page through for readers.
  const cta = (id, tab) => qs(id)?.addEventListener('click', () => { _closeFirstRunWizard(); activateTab(tab); });
  cta('fr-wizard-cta-add', 'index');
  cta('fr-wizard-cta-search', 'search');
  cta('fr-wizard-cta-connect', 'context-gateway');
}
// Registered at module load (not inside _initFirstRunWizard) so it is in place
// before I18N.init()'s one-shot langchange — which can fire before or after
// DOMContentLoaded. The counter + Next/Done labels are JS-rendered, so the first
// paint (during the landing handler, before the locale cache populates) holds
// the raw-key fallback until this re-render swaps in the real strings. Cheap and
// idempotent when the wizard is closed (re-applies step state to a hidden node).
window.addEventListener('langchange', _renderFirstRunWizardStep);

// ---------------------------------------------------------------------------
// Keyboard Shortcuts (B)
// ---------------------------------------------------------------------------

function _isTextField(el) {
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable;
}

document.addEventListener('keydown', e => {
  // Esc: close topmost overlay first (always handled)
  if (e.key === 'Escape') {
    const confirmModal = qs('confirm-modal');
    if (confirmModal && !confirmModal.hidden) return; // handled by showConfirm's own listener
    const frWizard = qs('first-run-wizard');
    if (frWizard && !frWizard.hidden) { closeModal(frWizard); return; }
    const installGuideModal = qs('ctx-install-guide-modal');
    if (installGuideModal && !installGuideModal.hidden) { closeModal(installGuideModal); return; }
    const srcPreview = qs('source-preview-modal');
    if (srcPreview && !srcPreview.hidden) { closeModal(srcPreview); return; }
    const expandModal = qs('expand-modal');
    if (expandModal && !expandModal.hidden) { closeModal(expandModal); return; }
    const settingsModal = qs('settings-modal');
    if (settingsModal && !settingsModal.hidden) { closeModal(settingsModal); return; }
    const modal = qs('shortcuts-modal');
    if (modal && !modal.hidden) { closeModal(modal); return; }
    const dropdown = qs('search-history-dropdown');
    if (dropdown && !dropdown.hidden) { hide(dropdown); return; }
    const similar = qs('similar-panel');
    if (similar && !similar.hidden) { hide(similar); return; }
    const sourceChunks = qs('source-chunks-panel');
    if (sourceChunks && !sourceChunks.hidden) { hide(sourceChunks); return; }
    if (qs('detail-view') && !qs('detail-view').hidden) { clearDetail(); return; }
    return;
  }

  // A11Y-3.1: every non-Esc bare-key shortcut below (?, /, h, j/k, p, c)
  // is suspended while any modal is on screen. The one exception is ? as a
  // toggle-close when the shortcuts modal owns the top of the stack — the
  // bare-key UX that opened it should also dismiss it. Other modals on top
  // keep ? entirely (otherwise it could pop the shortcuts modal over them).
  if (window.isAnyModalOpen()) {
    if (e.key === '?' && window.isTopModal(qs('shortcuts-modal'))) {
      e.preventDefault();
      closeShortcutsModal();
    }
    return;
  }

  // Other shortcuts: skip when user is typing
  if (_isTextField(e.target)) return;

  if (e.key === '/') {
    e.preventDefault();
    const input = qs('search-input');
    input.focus();
    input.select();
    return;
  }

  if (e.key === '?') {
    e.preventDefault();
    const modal = qs('shortcuts-modal');
    if (modal.hidden) openShortcutsModal(); else closeShortcutsModal();
    return;
  }

  if (e.key === 'h') {
    e.preventDefault();
    toggleHelp();
    return;
  }

  if (e.key === 'j' || e.key === 'k') {
    e.preventDefault();
    const items = [...document.querySelectorAll('.result-item')];
    if (!items.length) return;
    const cur = document.querySelector('.result-item.selected');
    const idx = cur ? items.indexOf(cur) : -1;
    const next = e.key === 'j'
      ? (idx < items.length - 1 ? items[idx + 1] : items[0])
      : (idx > 0 ? items[idx - 1] : items[items.length - 1]);
    next.click();
    next.scrollIntoView({ block: 'nearest' });
    return;
  }

  // H. Pin shortcut
  if (e.key === 'p' && STATE.selectedChunkId) {
    e.preventDefault();
    qs('d-pin-btn').click();
    return;
  }

  // H. Copy shortcut
  if (e.key === 'c' && STATE.selectedChunkId) {
    e.preventDefault();
    copyToClipboard(qs('d-editor').value);
    return;
  }
});

// ---------------------------------------------------------------------------
// Chunk-type filter (C2) — client-side re-render
// ---------------------------------------------------------------------------

qs('chunk-type-filter').addEventListener('change', () => {
  _updateFilterCountBadge();
  renderResults(STATE.lastResults);
});
qs('ns-filter').addEventListener('change', () => {
  _updateFilterCountBadge();
  renderResults(STATE.lastResults);
  if (_hasSearchAxis()) doSearch();
});
const _debouncedTagFilterSearch = debounce(() => {
  if (_hasSearchAxis()) doSearch();
}, 300);
qs('tag-filter').addEventListener('input', () => {
  _updateFilterCountBadge();
  renderResults(STATE.lastResults);
  _debouncedTagFilterSearch();
});

// URL query sync (C2)
function _syncSearchToURL() {
  const params = new URLSearchParams();
  const q = qs('search-input').value.trim();
  if (q) params.set('q', q);
  const ct = qs('chunk-type-filter').value;
  if (ct) params.set('type', ct);
  // source-filter is now a multi-select; not synced to URL
  history.replaceState(null, '', params.toString() ? '?' + params : window.location.pathname);
}

(function _loadSearchFromURL() {
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q');
  const ct = params.get('type');
  if (q) qs('search-input').value = q;
  if (ct && qs('chunk-type-filter')) qs('chunk-type-filter').value = ct;
  // source multi-filter not synced from URL
  _updateFilterCountBadge();
  if (q) {
    activateTab('search');
    doSearch();
  }
})();

// ---------------------------------------------------------------------------
// Load More (C3)
// ---------------------------------------------------------------------------

qs('load-more-btn').addEventListener('click', async () => {
  // Mirror doSearch — load-more must preserve tag/source-only searches
  // plus namespace/context filters from the current result set.
  if (!_hasSearchAxis()) return;
  STATE.currentTopK = Math.min(STATE.currentTopK + 10, 100);
  const params = _buildSearchParams(STATE.currentTopK);
  const btn = qs('load-more-btn');
  btnLoading(btn, true);
  try {
    const data = await api('GET', `/api/search?${params}`);
    renderResults(data.results);
  } catch (err) {
    showToast(t('toast.error', { error: err.message }), 'error');
  } finally {
    btnLoading(btn, false);
  }
});

// ---------------------------------------------------------------------------
// Pin / Favorite (D2)
// ---------------------------------------------------------------------------

function _getPinStore() {
  try { return JSON.parse(localStorage.getItem('m2m-pins') || '{}'); } catch { return {}; }
}
function _savePinStore(store) {
  localStorage.setItem('m2m-pins', JSON.stringify(store));
}
function isPinned(id) {
  return id !== null && String(id) in _getPinStore();
}
function pinChunk(id, preview) {
  const store = _getPinStore();
  store[String(id)] = preview;
  _savePinStore(store);
}
function unpinChunk(id) {
  const store = _getPinStore();
  delete store[String(id)];
  _savePinStore(store);
}
function _updatePinPreview(id, patch) {
  const store = _getPinStore();
  const key = String(id);
  if (!(key in store)) return;
  store[key] = { ...(store[key] || {}), ...patch };
  _savePinStore(store);
}
function updatePinBtn(chunkId) {
  const btn = qs('d-pin-btn');
  if (!btn) return;
  const pinned = isPinned(chunkId);
  btn.textContent = pinned ? '★ Pinned' : '☆ Pin';
  btn.classList.toggle('btn-pin-active', pinned);
}
// Monotonic counter so a slow click on pin A doesn't clobber the result of
// a later click on pin B (same pattern as the tier-loader stale-response
// guard in settings-hooks-watchdog.js).
let _pinOpenSeq = 0;
async function openPinnedChunk(id) {
  const key = String(id);
  const seq = ++_pinOpenSeq;
  try {
    const chunk = await api('GET', `/api/chunks/${encodeURIComponent(key)}`);
    if (seq !== _pinOpenSeq) return;
    _updatePinPreview(key, {
      source: chunk.source_file || '',
      snippet: (chunk.content || '').slice(0, 100),
      stale: false,
    });
    if (!chunk.source_file) {
      showToast(t('toast.chunk_target_missing'), 'info');
      return;
    }
    _navigateToSource(chunk.source_file, key);
  } catch (err) {
    if (seq !== _pinOpenSeq) return;
    if (err && err.status === 404) {
      _updatePinPreview(key, { stale: true });
      renderPinnedSection();
      showToast(t('toast.pinned_chunk_missing'), 'error');
      return;
    }
    console.warn('[pin] openPinnedChunk failed', err);
    showToast(t('toast.error', { error: err.message }), 'error');
  }
}
function renderPinnedSection() {
  const list = qs('home-pinned-list');
  if (!list) return;
  let store = {};
  try { store = _getPinStore(); }
  catch (err) {
    list.innerHTML = emptyState('⚠', t('home.state.load_failed'), err.message || '');
    return;
  }
  const items = Object.entries(store);
  if (!items.length) {
    list.innerHTML = `<div class="empty-state" style="height:50px"><span>${escapeHtml(t('home.state.no_pinned'))}</span></div>`;
    return;
  }
  // Row = wrapper div; the navigate target is a real <button> sibling of
  // the Remove button. Two reasons:
  //   1. ARIA: a role=button container with a real <button> descendant is
  //      a nested-interactive antipattern (assistive-tech announces both).
  //   2. Keyboard: Enter/Space on the focused Remove button bubbles to the
  //      row's keydown handler, double-firing unpin + navigate.
  // Sibling buttons sidestep both — each gets native keyboard handling and
  // there's no shared listener between them.
  list.innerHTML = items.map(([id, p]) => {
    const stale = Boolean(p.stale);
    const source = p.source || t('home.health.unknown');
    const openTitle = stale
      ? t('home.pin.missing_title')
      : t('home.pin.open_title', { source });
    const removeLabel = stale ? t('home.pin.remove_label') : '✕';
    const removeTitle = stale
      ? t('home.pin.remove_title')
      : t('home.pin.unpin_title');
    return `
    <div class="home-source-item home-pinned-item${stale ? ' home-pinned-stale' : ''}">
      <button type="button" class="home-pinned-open" data-id="${escapeAttr(id)}" title="${escapeAttr(openTitle)}" aria-label="${escapeAttr(openTitle)}">
        <span class="home-source-name">${escapeHtml(source)}</span>
        <span class="home-pinned-snippet">${escapeHtml(truncate(p.snippet || '', 50))}</span>
        ${stale ? `<span class="badge badge-yellow home-pinned-stale-badge">${escapeHtml(t('home.pin.missing_badge'))}</span>` : ''}
      </button>
      <button class="unpin-btn btn-ghost btn-xs" data-id="${escapeAttr(id)}" title="${escapeAttr(removeTitle)}">${escapeHtml(removeLabel)}</button>
    </div>`;
  }).join('');
  list.querySelectorAll('.home-pinned-open').forEach(el => {
    el.addEventListener('click', () => openPinnedChunk(el.dataset.id));
  });
  list.querySelectorAll('.unpin-btn').forEach(b => {
    b.addEventListener('click', e => {
      e.stopPropagation();
      unpinChunk(b.dataset.id);
      renderPinnedSection();
      if (STATE.selectedChunkId && String(STATE.selectedChunkId) === b.dataset.id) updatePinBtn(STATE.selectedChunkId);
    });
  });
}

qs('d-pin-btn').addEventListener('click', () => {
  if (!STATE.selectedChunkId) return;
  const id = String(STATE.selectedChunkId);
  if (isPinned(id)) {
    unpinChunk(id);
    showToast(t('toast.unpinned'), 'info');
  } else {
    pinChunk(id, {
      source: qs('d-file').textContent || '',
      snippet: qs('d-editor').value.slice(0, 100),
    });
    showToast(t('toast.pinned'), 'info');
  }
  updatePinBtn(id);
  STATE.homeStale = true;
});

// ---------------------------------------------------------------------------
// Settings (E1)
// ---------------------------------------------------------------------------

function _loadSettings() {
  return { defaultTab: _lsGet('m2m-default-tab') || 'search' };
}

// S2.1 — a brand-new install lands on Home (orientation) instead of the empty
// Search screen. "First run" means localStorage carries no app-owned state yet:
// no key under the app's prefixes, including our own "seen" stamp. The prefix
// scan keeps this robust as new persisted keys are added (search history, saved
// queries, gateway state, …) so an existing user upgrading into this feature is
// never misread as fresh and yanked off their usual landing.
//
// Exception: a few keys are written by the app itself on a cold boot and so are
// present even on a genuinely fresh install — setIndexMode() persists the
// applied default at module load (app.js:5570). Those are excluded below. The
// invariant that this list is complete (no *other* boot-time write slips in
// before we land) is pinned by the cold-boot first-run test, which lands on
// Search instead of Home the moment a new boot-time write appears.
const _BOOT_WRITTEN_LS_KEYS = new Set(['memtomem.index.mode']);
function _hasPriorAppState() {
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && (k.startsWith('m2m-') || k.startsWith('memtomem'))
          && !_BOOT_WRITTEN_LS_KEYS.has(k)) return true;
    }
    return false;
  } catch {
    // Can't read storage (private mode) — treat as a returning user so we
    // never force-route someone whose state we can't inspect.
    return true;
  }
}
function _isFirstRun() { return !_hasPriorAppState(); }

// Deferred until DOMContentLoaded so sibling scripts (settings-config.js, etc.)
// have parsed — activateTab('settings') calls loadConfig() defined there.
function _applyLandingTab() {
  // A deep-link hash is owned by the earlier hash handler — never override it.
  if (location.hash.slice(1)) return;
  const firstRun = _isFirstRun();
  // Stamp the install as oriented, and only honor first-run routing if the
  // stamp actually persisted — otherwise a private-mode session (setItem
  // throws) would be force-routed to Home on every visit.
  let stamped = false;
  try { localStorage.setItem('m2m-app-initialized', '1'); stamped = true; } catch {}
  let target = (firstRun && stamped) ? 'home' : _loadSettings().defaultTab;
  // Clamp a returning user's saved default (selectable as Tags/Timeline in the
  // settings modal) to a currently-visible tab — otherwise Simple-by-default
  // force-routes them onto a hidden advanced tab on every boot. The highest-
  // probability real-world strand for this feature.
  if (!_visibleMainTabs().includes(target)) {
    target = _visibleMainTabs()[0] || 'home';
  }
  const currentActive = document.querySelector('.tab-btn.active');
  const currentTab = currentActive ? currentActive.dataset.tab : null;
  if (currentTab !== target) {
    activateTab(target);
  }
  // S3.1 — the same genuine-first-run signal that routes to Home also opens the
  // one-time guided wizard over it. Wrapped so a missing element or a sibling
  // init throw can never abort the landing routing above.
  if (firstRun && stamped) {
    try { _showFirstRunWizard(); } catch {}
  }
}
document.addEventListener('DOMContentLoaded', _applyLandingTab);

let _settingsRelease = null;
function openSettingsModal() {
  const modal = qs('settings-modal');
  const s = _loadSettings();
  qs('settings-default-tab').value = s.defaultTab;
  const curTopK = STATE.serverConfig?.search?.default_top_k || STATE.currentTopK || 10;
  qs('settings-default-topk').value = String(curTopK);
  show(modal);
  _settingsRelease = openModalA11y(modal, {
    focusables: () => Array.from(
      modal.querySelectorAll('input, select, button, [tabindex="0"]')
    ).filter(el => !el.disabled && el.offsetParent !== null),
  });
}
function closeSettingsModal() {
  hide(qs('settings-modal'));
  if (_settingsRelease) { _settingsRelease(); _settingsRelease = null; }
}
window.openSettingsModal = openSettingsModal;
registerModalCloser(qs('settings-modal'), closeSettingsModal);

qs('settings-btn').addEventListener('click', openSettingsModal);
qs('settings-close-btn').addEventListener('click', closeSettingsModal);
qs('settings-modal').addEventListener('click', e => {
  if (e.target === qs('settings-modal')) closeSettingsModal();
});
qs('settings-save-btn').addEventListener('click', async () => {
  localStorage.setItem('m2m-default-tab', qs('settings-default-tab').value);
  const newTopK = parseInt(qs('settings-default-topk').value, 10);
  // Sync top-k to server config so all paths see the same value
  try {
    await api('PATCH', '/api/config?persist=true', { search: { default_top_k: newTopK } });
    if (STATE.serverConfig?.search) STATE.serverConfig.search.default_top_k = newTopK;
    qs('top-k').value = String(newTopK);
    STATE.currentTopK = newTopK;
  } catch (e) {
    console.warn('Failed to persist top-k to server config:', e);
  }
  showToast(t('toast.settings_saved'), 'success');
  closeSettingsModal();
});
qs('settings-reset-btn').addEventListener('click', () => {
  localStorage.removeItem('m2m-default-tab');
  qs('settings-default-tab').value = 'search';
  // Reset top-k to server default
  const serverTopK = STATE.serverConfig?.search?.default_top_k || 10;
  qs('settings-default-topk').value = String(serverTopK);
  showToast(t('toast.settings_reset'), 'info');
});

let _shortcutsRelease = null;
function openShortcutsModal() {
  const modal = qs('shortcuts-modal');
  show(modal);
  _shortcutsRelease = openModalA11y(modal, {
    focusables: () => [qs('shortcuts-close-btn')],
  });
}
function closeShortcutsModal() {
  hide(qs('shortcuts-modal'));
  if (_shortcutsRelease) { _shortcutsRelease(); _shortcutsRelease = null; }
}
window.openShortcutsModal = openShortcutsModal;
window.closeShortcutsModal = closeShortcutsModal;
registerModalCloser(qs('shortcuts-modal'), closeShortcutsModal);

qs('shortcuts-close-btn').addEventListener('click', closeShortcutsModal);
qs('shortcuts-modal').addEventListener('click', e => {
  if (e.target === qs('shortcuts-modal')) closeShortcutsModal();
});

// ---------------------------------------------------------------------------
// Source Multi-Filter (F3)
// ---------------------------------------------------------------------------

async function loadSourceFilter() {
  const sel = qs('source-filter');
  if (!sel) return;
  const selected = new Set(_getSelectedSourceFilters());
  try {
    const data = await api('GET', '/api/sources?limit=10000');
    const sources = Array.isArray(data.sources) ? data.sources : [];
    if (!sources.length) {
      sel.innerHTML = '<option value="" disabled>No sources indexed</option>';
      return;
    }
    sel.innerHTML = sources.map(s =>
      `<option value="${escapeAttr(s.path)}" ${selected.has(s.path) ? 'selected' : ''}>${escapeHtml(basename(s.path))}</option>`
    ).join('');
  } catch (e) {
    console.warn('[source-filter]', e);
    sel.innerHTML = '<option value="" disabled>Error loading sources</option>';
  }
}

qs('source-filter').addEventListener('change', () => {
  _updateFilterCountBadge();
  renderResults(STATE.lastResults);
  if (_hasSearchAxis()) doSearch();
});

// ---------------------------------------------------------------------------
// Score Threshold (F2)
// ---------------------------------------------------------------------------

qs('score-threshold').addEventListener('input', () => {
  STATE.scoreMin = parseFloat(qs('score-threshold').value);
  qs('score-val').textContent = STATE.scoreMin.toFixed(1);
  _updateFilterCountBadge();
  renderResults(STATE.lastResults);
});

window.addEventListener('langchange', _updateFilterCountBadge);

// ---------------------------------------------------------------------------
// Date Range Filter (Kibana-style)
// ---------------------------------------------------------------------------

function _getDateRange() {
  const preset = qs('date-range-preset').value;
  if (!preset) return null;
  const now = Date.now();
  const dayMs = 86400000;
  if (preset === 'today') {
    const start = new Date(); start.setHours(0,0,0,0);
    return { from: start.getTime(), to: now };
  }
  if (preset === '7d') return { from: now - 7 * dayMs, to: now };
  if (preset === '30d') return { from: now - 30 * dayMs, to: now };
  if (preset === '90d') return { from: now - 90 * dayMs, to: now };
  if (preset === 'custom') {
    const fromVal = qs('date-from').value;
    const toVal = qs('date-to').value;
    const from = fromVal ? new Date(fromVal).getTime() : 0;
    const to = toVal ? new Date(toVal + 'T23:59:59').getTime() : now;
    return { from, to };
  }
  return null;
}

qs('date-range-preset').addEventListener('change', () => {
  const custom = qs('date-range-custom');
  custom.hidden = qs('date-range-preset').value !== 'custom';
  _updateFilterCountBadge();
  if (STATE.lastResults.length) renderResults(STATE.lastResults);
});
qs('date-from').addEventListener('change', () => {
  _updateFilterCountBadge();
  if (STATE.lastResults.length) renderResults(STATE.lastResults);
});
qs('date-to').addEventListener('change', () => {
  _updateFilterCountBadge();
  if (STATE.lastResults.length) renderResults(STATE.lastResults);
});

// ---------------------------------------------------------------------------
// Filter Row Toggle
// ---------------------------------------------------------------------------

qs('filter-toggle').addEventListener('click', () => {
  const filters = document.querySelector('.search-filters');
  filters.hidden = !filters.hidden;
  qs('filter-toggle').classList.toggle('btn-active', !filters.hidden);
  qs('filter-toggle').setAttribute('aria-expanded', String(!filters.hidden));
});

// ---------------------------------------------------------------------------
// Advanced Filters Toggle
// ---------------------------------------------------------------------------

qs('adv-toggle').addEventListener('click', () => {
  const panel = qs('search-advanced');
  panel.hidden = !panel.hidden;
  qs('adv-toggle').classList.toggle('btn-active', !panel.hidden);
  qs('adv-toggle').setAttribute('aria-expanded', String(!panel.hidden));
  if (!panel.hidden) loadSourceFilter();
});

// ---------------------------------------------------------------------------
// View Toggle (I1)
// ---------------------------------------------------------------------------

// JS owns #view-toggle's title/aria-label because the label is state-dependent
// (next action, not current mode). The HTML element therefore has no
// data-i18n-title / data-i18n-aria-label — applyDOM() would otherwise reset
// both attributes to the generic search.view_title string on every langchange,
// silently undoing the per-state label written here.
function _syncViewToggle() {
  const btn = qs('view-toggle');
  if (!btn) return;
  const isList = STATE.viewMode === 'list';
  const label = isList ? t('search.view_card_title') : t('search.view_list_title');
  btn.textContent = isList ? '⊟' : '☰';
  btn.title = label;
  btn.setAttribute('aria-label', label);
}

qs('view-toggle').addEventListener('click', () => {
  STATE.viewMode = STATE.viewMode === 'card' ? 'list' : 'card';
  _syncViewToggle();
  renderResults(STATE.lastResults);
});

window.addEventListener('langchange', _syncViewToggle);

// ---------------------------------------------------------------------------
// Expand Detail (I2)
// ---------------------------------------------------------------------------

let _expandRelease = null;
function openExpandModal() {
  const modal = qs('expand-modal');
  qs('expand-modal-title').textContent = qs('d-file').textContent || 'Content';
  const pre = qs('expand-content');
  pre.textContent = '';
  const fileExt = (qs('d-file').textContent || '').split('.').pop();
  const lang = getLanguage('.' + fileExt);
  const code = document.createElement('code');
  if (lang) code.className = `language-${lang}`;
  code.textContent = qs('d-editor').value;
  pre.appendChild(code);
  if (lang && window.Prism) Prism.highlightElement(code);
  show(modal);
  _expandRelease = openModalA11y(modal, {
    focusables: () => [qs('expand-close-btn')],
  });
}
function closeExpandModal() {
  hide(qs('expand-modal'));
  if (_expandRelease) { _expandRelease(); _expandRelease = null; }
}
window.openExpandModal = openExpandModal;
registerModalCloser(qs('expand-modal'), closeExpandModal);

qs('d-expand-btn').addEventListener('click', openExpandModal);
qs('expand-close-btn').addEventListener('click', closeExpandModal);
qs('expand-modal').addEventListener('click', e => {
  if (e.target === qs('expand-modal')) closeExpandModal();
});

// ---------------------------------------------------------------------------
// Source Preview Modal — full document view with chunk highlight
// ---------------------------------------------------------------------------

let _sourcePreviewRelease = null;
function openSourcePreviewModal() {
  const modal = qs('source-preview-modal');
  show(modal);
  _sourcePreviewRelease = openModalA11y(modal, {
    focusables: () => [qs('source-preview-close')],
  });
}
function closeSourcePreviewModal() {
  hide(qs('source-preview-modal'));
  if (_sourcePreviewRelease) { _sourcePreviewRelease(); _sourcePreviewRelease = null; }
}
window.openSourcePreviewModal = openSourcePreviewModal;
registerModalCloser(qs('source-preview-modal'), closeSourcePreviewModal);

async function openSourcePreview(sourcePath, highlightStart, highlightEnd) {
  const modal = qs('source-preview-modal');
  const body = qs('source-preview-body');
  const title = qs('source-preview-title');
  const info = qs('source-preview-info');

  title.textContent = basename(sourcePath);
  title.title = sourcePath;
  info.textContent = 'Loading…';
  panelLoading(body);
  openSourcePreviewModal();

  try {
    const data = await api('GET', `/api/sources/content?path=${encodeURIComponent(sourcePath)}`);
    const lines = data.content.split('\n');
    info.textContent = `${lines.length} lines · ${formatBytes(data.size)}`;

    const table = document.createElement('table');
    lines.forEach((line, i) => {
      const lineNum = i + 1;
      const tr = document.createElement('tr');
      if (highlightStart && highlightEnd && lineNum >= highlightStart && lineNum <= highlightEnd) {
        tr.className = 'highlight-chunk';
      }
      const noTd = document.createElement('td');
      noTd.className = 'line-no';
      noTd.textContent = lineNum;
      const contentTd = document.createElement('td');
      contentTd.className = 'line-content';
      contentTd.textContent = line || '\u00A0';
      tr.appendChild(noTd);
      tr.appendChild(contentTd);
      table.appendChild(tr);
    });

    body.innerHTML = '';
    body.appendChild(table);

    // Scroll to highlighted chunk
    if (highlightStart) {
      const target = table.querySelector('tr.highlight-chunk');
      if (target) {
        requestAnimationFrame(() => {
          target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        });
      }
    }
  } catch (err) {
    body.innerHTML = `<div style="padding:24px;color:var(--danger)">${escapeHtml(err.message)}</div>`;
    info.textContent = '';
  }
}

qs('source-preview-close').addEventListener('click', closeSourcePreviewModal);
qs('source-preview-modal').addEventListener('click', e => {
  if (e.target === qs('source-preview-modal')) closeSourcePreviewModal();
});

// Make d-file clickable to open source preview
qs('d-file').style.cursor = 'pointer';
qs('d-file').title = 'Click to view full source file';
qs('d-file').addEventListener('click', () => {
  const path = qs('d-file').textContent;
  if (!path) return;
  const r = STATE.lastResults.find(x => String(x.chunk.id) === String(STATE.selectedChunkId));
  const start = r ? r.chunk.start_line : null;
  const end = r ? r.chunk.end_line : null;
  openSourcePreview(path, start, end);
});

// ---------------------------------------------------------------------------
// Sort (J1)
// ---------------------------------------------------------------------------

qs('sort-select').addEventListener('change', () => {
  STATE.currentSortMode = qs('sort-select').value;
  renderResults(STATE.lastResults);
});

// ---------------------------------------------------------------------------
// Word Count (K2)
// ---------------------------------------------------------------------------

function _updateWordCount() {
  const text = qs('d-editor').value;
  const chars = text.length;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  const tokens = Math.round(chars / 4);
  const el = qs('d-word-count');
  if (el) el.textContent = t('search.editor_word_count', { chars, words, tokens });
}

qs('d-editor').addEventListener('input', _updateWordCount);

// ---------------------------------------------------------------------------
// Source Nav — prev/next in source (J2)
// ---------------------------------------------------------------------------

// Cache of all chunks for the current source file (for full navigation)
let _sourceChunksCache = { file: null, chunks: [] };

async function _loadSourceChunks(sourceFile) {
  if (_sourceChunksCache.file === sourceFile && _sourceChunksCache.chunks.length) {
    return _sourceChunksCache.chunks;
  }
  try {
    const data = await api('GET', `/api/chunks?source=${encodeURIComponent(sourceFile)}&limit=500`);
    _sourceChunksCache = { file: sourceFile, chunks: data.chunks || [] };
    return _sourceChunksCache.chunks;
  } catch {
    return [];
  }
}

function _getSourceIdx(chunks) {
  return chunks.findIndex(c => String(c.id) === String(STATE.selectedChunkId));
}

async function _updateSourceNav() {
  const prevBtn = qs('d-prev-btn');
  const nextBtn = qs('d-next-btn');
  const posEl   = qs('d-source-pos');
  if (!prevBtn) return;
  const sf = qs('d-file')?.textContent;
  if (!sf || !STATE.selectedChunkId) {
    prevBtn.disabled = true; nextBtn.disabled = true; posEl.textContent = ''; return;
  }
  const chunks = await _loadSourceChunks(sf);
  const idx = _getSourceIdx(chunks);
  prevBtn.disabled = idx <= 0;
  nextBtn.disabled = idx < 0 || idx >= chunks.length - 1;
  posEl.textContent = idx >= 0 ? `${idx + 1}/${chunks.length}` : '';
}

qs('d-prev-btn').addEventListener('click', async () => {
  const sf = qs('d-file')?.textContent;
  if (!sf) return;
  const chunks = await _loadSourceChunks(sf);
  const idx = _getSourceIdx(chunks);
  if (idx <= 0) return;
  const c = chunks[idx - 1];
  const existing = STATE.lastResults.find(r => String(r.chunk.id) === String(c.id));
  showDetail(existing || { chunk: c, score: 0, rank: 0, source: 'browse' });
});

qs('d-next-btn').addEventListener('click', async () => {
  const sf = qs('d-file')?.textContent;
  if (!sf) return;
  const chunks = await _loadSourceChunks(sf);
  const idx = _getSourceIdx(chunks);
  if (idx < 0 || idx >= chunks.length - 1) return;
  const c = chunks[idx + 1];
  const existing = STATE.lastResults.find(r => String(r.chunk.id) === String(c.id));
  showDetail(existing || { chunk: c, score: 0, rank: 0, source: 'browse' });
});

// ---------------------------------------------------------------------------
// Saved Searches (J3)
// ---------------------------------------------------------------------------

const _SAVED_KEY = 'm2m-saved-queries';

function _getSavedQueries() {
  try { return JSON.parse(localStorage.getItem(_SAVED_KEY) || '[]'); } catch { return []; }
}
function _setSavedQueries(list) { localStorage.setItem(_SAVED_KEY, JSON.stringify(list)); }

function _renderSavedSelect() {
  const sel = qs('saved-queries-select');
  if (!sel) return;
  const list = _getSavedQueries();
  sel.innerHTML = '<option value="">— Load saved —</option>' +
    list.map((q, i) => `<option value="${i}">${escapeHtml(q.name)}</option>`).join('');
}

qs('save-query-btn').addEventListener('click', () => {
  const q = qs('search-input').value.trim();
  if (!q) { showToast(t('toast.enter_query'), 'error'); return; }
  const name = prompt('Save search as:', q);
  if (!name) return;
  const list = _getSavedQueries();
  list.push({ name, query: q, typeFilter: qs('chunk-type-filter').value, tagFilter: qs('tag-filter').value.trim() });
  _setSavedQueries(list);
  _renderSavedSelect();
  _renderSavedBar();
  showToast(t('toast.query_saved', { name }), 'success');
});

qs('saved-queries-select').addEventListener('change', () => {
  const idx = parseInt(qs('saved-queries-select').value);
  if (isNaN(idx)) return;
  const q = _getSavedQueries()[idx];
  if (!q) return;
  qs('search-input').value = q.query;
  if (qs('chunk-type-filter')) qs('chunk-type-filter').value = q.typeFilter || '';
  if (qs('tag-filter')) qs('tag-filter').value = q.tagFilter || '';
  qs('saved-queries-select').value = '';
  _updateFilterCountBadge();
  doSearch();
});

qs('delete-query-btn').addEventListener('click', () => {
  const idx = parseInt(qs('saved-queries-select').value);
  if (isNaN(idx)) { showToast(t('toast.select_saved'), 'error'); return; }
  const list = _getSavedQueries();
  const name = list[idx]?.name;
  list.splice(idx, 1);
  _setSavedQueries(list);
  _renderSavedSelect();
  _renderSavedBar();
  showToast(t('toast.query_deleted', { name }), 'info');
});

_renderSavedSelect();

function _renderSavedBar() {
  const bar = qs('saved-searches-bar');
  if (!bar) return;
  const list = _getSavedQueries();
  if (!list.length) { hide(bar); return; }
  bar.innerHTML = '<span class="saved-bar-label">Saved:</span>' +
    list.map((q, i) =>
      `<span class="saved-chip" data-idx="${i}" title="${escapeAttr(q.query)}">` +
      `<span class="saved-chip-name">${escapeHtml(q.name)}</span>` +
      `<button class="saved-chip-remove" data-idx="${i}" title="Remove">✕</button></span>`
    ).join('');
  bar.querySelectorAll('.saved-chip-name').forEach(el => {
    el.addEventListener('click', () => {
      const idx = parseInt(el.parentElement.dataset.idx);
      const q = list[idx];
      if (!q) return;
      qs('search-input').value = q.query;
      if (qs('chunk-type-filter')) qs('chunk-type-filter').value = q.typeFilter || '';
      if (qs('tag-filter')) qs('tag-filter').value = q.tagFilter || '';
      _updateFilterCountBadge();
      doSearch();
    });
  });
  bar.querySelectorAll('.saved-chip-remove').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.idx);
      const name = list[idx]?.name;
      list.splice(idx, 1);
      _setSavedQueries(list);
      _renderSavedBar();
      _renderSavedSelect();
      showToast(t('toast.query_removed', { name }), 'info');
    });
  });
  show(bar);
}
_renderSavedBar();

// ---------------------------------------------------------------------------
// Source Chunks Browser (K3)
// ---------------------------------------------------------------------------

qs('d-source-btn').addEventListener('click', async () => {
  const panel = qs('source-chunks-panel');
  if (!panel.hidden) { hide(panel); return; }
  hide(qs('similar-panel'));

  const sourceFile = qs('d-file').textContent;
  if (!sourceFile) return;

  const list = qs('source-chunks-list');
  panelLoading(list);
  // Hide related panel to avoid stacking
  hide(qs('related-panel'));
  show(panel);
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const data = await api('GET', `/api/chunks?source=${encodeURIComponent(sourceFile)}&limit=100`);
    const chunks = data.chunks || [];
    if (!chunks.length) {
      list.innerHTML = '<div class="empty-state" style="height:40px"><span>No chunks found</span></div>';
      return;
    }
    list.innerHTML = chunks.map(c => `
      <div class="similar-item${String(c.id) === String(STATE.selectedChunkId) ? ' source-chunk-current' : ''}" data-id="${escapeAttr(String(c.id))}">
        <div class="similar-item-header">
          <span class="badge badge-gray" style="font-size:0.65rem">${escapeHtml(c.chunk_type.replace('_', ' '))}</span>
          <span style="font-size:0.65rem;color:var(--muted)">L${c.start_line}–${c.end_line}</span>
        </div>
        <div class="similar-item-snippet">${escapeHtml(truncate(c.content, 90))}</div>
      </div>`).join('');
    list.querySelectorAll('.similar-item').forEach(el => {
      el.style.cursor = 'pointer';
      el.addEventListener('click', () => {
        const c = chunks.find(ch => String(ch.id) === el.dataset.id);
        if (c) showDetailFromChunk(c);
      });
    });
  } catch (err) {
    list.innerHTML = `<div class="status-msg err">${escapeHtml(err.message)}</div>`;
  }
});

qs('source-chunks-close-btn').addEventListener('click', () => hide(qs('source-chunks-panel')));

// ---------------------------------------------------------------------------
// M1 + M2: Export (bulk selected / all results)
// ---------------------------------------------------------------------------

function downloadResults(items, format) {
  let content, mime, ext;
  if (format === 'csv') {
    const header = 'id,source,type,score,content\n';
    const rows = items.map(r => {
      const c = r.chunk || r;
      const score = r.score != null ? r.score.toFixed(4) : '';
      return [
        c.id, c.source_file, c.chunk_type, score,
        '"' + (c.content || '').replace(/"/g, '""').replace(/\n/g, '\\n') + '"',
      ].join(',');
    }).join('\n');
    content = header + rows; mime = 'text/csv'; ext = 'csv';
  } else if (format === 'markdown') {
    content = items.map(r => {
      const c = r.chunk || r;
      const title = (c.heading_hierarchy || []).join(' › ') || basename(c.source_file || 'chunk');
      return `## ${title}\n\n*Source: ${c.source_file} | Type: ${c.chunk_type}*\n\n${c.content}\n`;
    }).join('\n---\n\n');
    mime = 'text/markdown'; ext = 'md';
  } else {
    content = JSON.stringify(items, null, 2); mime = 'application/json'; ext = 'json';
  }
  const blob = new Blob([content], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `memtomem-export-${Date.now()}.${ext}`;
  a.click();
  URL.revokeObjectURL(a.href);
  showToast(t('toast.exported_count', { count: items.length, ext: ext.toUpperCase() }), 'success');
}

qs('bulk-export-btn').addEventListener('click', () => {
  const ids = [...STATE.selectedIds];
  if (!ids.length) return;
  const format = qs('bulk-export-fmt').value;
  const selected = STATE.lastResults.filter(r => ids.includes(String(r.chunk.id)));
  downloadResults(selected, format);
});

qs('export-all-btn').addEventListener('click', () => {
  if (!STATE.lastResults.length) { showToast(t('toast.no_results_export'), 'error'); return; }
  downloadResults(STATE.lastResults, qs('export-format').value);
});

// ---------------------------------------------------------------------------
// M3: Edit history (localStorage per chunk)
// ---------------------------------------------------------------------------

const _HIST_PREFIX = 'm2m-hist-';
const _HIST_MAX = 5;

function _getHistory(chunkId) {
  try { return JSON.parse(localStorage.getItem(_HIST_PREFIX + chunkId) || '[]'); }
  catch (_) { return []; }
}

function _pushHistory(chunkId, content) {
  const hist = _getHistory(chunkId);
  hist.unshift({ content, ts: new Date().toISOString() });
  hist.splice(_HIST_MAX);
  localStorage.setItem(_HIST_PREFIX + chunkId, JSON.stringify(hist));
}

function _updateHistoryBtn(chunkId) {
  const btn = qs('d-history-btn');
  _getHistory(chunkId).length ? show(btn) : hide(btn);
}

qs('d-history-btn').addEventListener('click', () => {
  const panel = qs('history-panel');
  if (!panel.hidden) { hide(panel); return; }
  hide(qs('similar-panel'));
  hide(qs('source-chunks-panel'));
  const hist = _getHistory(STATE.selectedChunkId);
  const list = qs('history-list');
  if (!hist.length) {
    list.innerHTML = '<div class="empty-state" style="height:40px"><span>No history</span></div>';
  } else {
    list.innerHTML = hist.map((h, i) => `
      <div class="similar-item" data-idx="${i}" style="cursor:pointer">
        <div class="similar-item-header">
          <span class="badge badge-gray" style="font-size:0.65rem">${relativeTime(h.ts)}</span>
          <span style="font-size:0.65rem;color:var(--muted)">${escapeHtml(new Date(h.ts).toLocaleString())}</span>
        </div>
        <div class="similar-item-snippet">${escapeHtml(truncate(h.content, 80))}</div>
      </div>`).join('');
    list.querySelectorAll('.similar-item').forEach(el => {
      el.addEventListener('click', () => {
        const h = hist[parseInt(el.dataset.idx)];
        const ops = diffLines(h.content, qs('d-editor').value);
        qs('d-diff').innerHTML = renderDiff(ops);
        show(qs('d-diff')); hide(qs('d-editor'));
        const diffBtn = qs('d-diff-btn');
        show(diffBtn); diffBtn.textContent = 'Edit'; diffBtn.dataset.mode = 'diff';
        showToast(t('toast.diff_shown'), 'info');
      });
    });
  }
  show(panel);
});

qs('history-close-btn').addEventListener('click', () => hide(qs('history-panel')));

// ---------------------------------------------------------------------------
// N1: Group by source toggle
// ---------------------------------------------------------------------------

// STATE.groupMode now in STATE

qs('group-toggle').addEventListener('click', () => {
  STATE.groupMode = !STATE.groupMode;
  qs('group-toggle').classList.toggle('btn-active', STATE.groupMode);
  if (STATE.lastResults.length) renderResults(STATE.lastResults);
});

// ---------------------------------------------------------------------------
// N2: Drag-to-index on search tab
// ---------------------------------------------------------------------------

{
  const _tab = qs('tab-search');
  let _dragCnt = 0;

  _tab.addEventListener('dragenter', e => {
    if (![...e.dataTransfer.items].some(i => i.kind === 'file')) return;
    e.preventDefault();
    _dragCnt++;
    show(qs('search-drop-overlay'));
  });

  _tab.addEventListener('dragleave', () => {
    _dragCnt = Math.max(0, _dragCnt - 1);
    if (_dragCnt === 0) hide(qs('search-drop-overlay'));
  });

  _tab.addEventListener('dragover', e => { e.preventDefault(); });

  _tab.addEventListener('drop', async e => {
    e.preventDefault();
    _dragCnt = 0;
    hide(qs('search-drop-overlay'));
    const files = [...e.dataTransfer.files].filter(f =>
      /\.(md|txt|py|js|ts|tsx|json|yaml|yml)$/i.test(f.name));
    if (!files.length) { showToast(t('toast.file_filter'), 'error'); return; }
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    showToast(t('toast.indexing_files', { count: files.length }), 'info');
    try {
      const upload = await uploadFilesWithRedactionRetry(fd);
      const data = upload.data;
      // Mixed-batch refresh: same rationale as upload-mode caller — the
      // first POST already indexed clean files even when the user later
      // cancels the bypass dialog. Unlike the upload tab there is no
      // per-file result list here, so without a cancel toast the operator
      // has zero signal that some files DID land. Surface the cancel toast
      // and drop through to the staleness refresh in every branch.
      if (upload.cancelled) {
        showToast(t('toast.upload_redaction_cancelled', { count: upload.blockedFileCount }), 'error');
      } else {
        // On partial bypass the helper already surfaced the per-file
        // failure via ``toast.redaction_bypass_partial``; suppress the
        // generic success toast here so the audit-relevant warning isn't
        // followed by a contradicting "indexed N files" message.
        const partial = upload.blockedFileCount > 0 && !upload.bypassed;
        if (!partial) {
          // ``/api/upload`` returns ``{files, total_indexed}`` — never a
          // ``results`` field. The original ``data.results`` read fell
          // back to ``[]`` so this toast has been showing "indexed N
          // files (0 chunks)" since v0.1.0.
          const chunks = (data.files || []).reduce((s, r) => s + (r.indexed_chunks || 0), 0);
          showToast(t('toast.indexed_files_chunks', { files: files.length, chunks }), 'success');
        }
      }
      _markDataStale();
      loadSourceFilter();
      loadStats();
    } catch (err) {
      showToast(t('toast.upload_failed', { error: err.message }), 'error');
    }
  });
}




// =====================================================================
// GLOBAL EVENT DELEGATION (CSP-safe: no inline onclick)
// =====================================================================

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;

  if (action === 'session-events') {
    showSessionEvents(el.dataset.id);
  } else if (action === 'scratch-delete') {
    deleteScratchEntry(el.dataset.key);
  } else if (action === 'scratch-promote') {
    promoteScratchEntry(el.dataset.key);
  } else if (action === 'toggle-next') {
    const sib = el.nextElementSibling;
    if (sib) sib.hidden = !sib.hidden;
  }
});

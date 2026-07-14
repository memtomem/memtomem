/*
 * Context Portal — multi-project board (ADR-0021 PR4).
 *
 * The ``ctx-projects`` settings section: a single board listing every
 * discovered project scope with health (missing / stale), per-type item
 * counts, an inline label rename, active-project switching, and unregister.
 * Loaded AFTER app.js and context-gateway.js in index.html, so it reuses
 * their globals rather than re-implementing them:
 *
 *   app.js            — t, escapeHtml, panelLoading, emptyState, showToast,
 *                       showConfirm, ensureCsrfToken, btnLoading
 *   context-gateway.js — _ctxFetchProjects, _ctxProjectsCache,
 *                       _ctxActiveScopeId, _ctxTargetScope, _CTX_ACTIVE_SCOPE_KEY,
 *                       _ctxScopeIsServerCwd, _ctxScopeIsActive,
 *                       _ctxScopeDisplayLabel, _ctxNormalizeActiveScope,
 *                       _ctxBumpActiveScopeDetailSeq, _ctxClearDeepLink,
 *                       _ctxScopeIsEnrolled, _ctxScopeSyncEligible,
 *                       _ctxSyncProjectScope, _ctxRefreshWriteBlockedState
 *
 * Backend contract (PR2): ``GET /api/context/projects?include=counts`` returns
 * scopes carrying ``{missing, stale, counts}``; ``PATCH`` / ``DELETE
 * /api/context/known-projects/{scope_id}`` rename / unregister. Counts are
 * opt-in, so the board requests them.
 *
 * The board renders from its OWN post-guard snapshot ``_ctxPortalScopes`` (set
 * only after loadCtxProjects' sequence/scope check passes), NOT the shared
 * ``_ctxProjectsCache`` directly — so a late, superseded ``_ctxFetchProjects``
 * that clobbers the shared cache can't make this section's search/sort operate
 * on stale data. (The shared-cache clobber itself is a pre-existing property of
 * ``_ctxFetchProjects`` affecting every caller; tracked in #1194.)
 *
 * Edit state is declarative: ``_ctxPortalEditingId`` names the single row in
 * inline-rename mode and drives the render, so at most one editor is ever open
 * and every repaint re-binds fresh listeners (no orphaned/detached handlers).
 *
 * Deferred to later PRs (intentional seams): per-CLI runtime traffic-lights
 * (PR5 — needs GET /api/context/runtimes) and one-click Initialize (no web
 * init endpoint yet — stale rows show a CLI hint instead).
 */

// Bumped per loadCtxProjects entry; a late fetch whose seq no longer matches is
// dropped (mirrors loadCtxList's _ctxListSeq). The board also snapshots the
// target-scope at entry and bails if it drifts mid-await (#972 stale-response
// guard) — counts are tier-dependent, so a tier flip during the fetch must not
// paint stale numbers.
let _ctxProjectsSeq = 0;
// AbortController paired with _ctxProjectsSeq (#1286): a re-issued loadCtxProjects
// aborts the prior invocation's projects fetch + its fan-out of per-scope
// /runtimes fetches. _ctxSwapAbort / _ctxIsAbortError are defined in
// context-gateway.js (loaded first) and shared via the global script scope.
let _ctxProjectsAbort = null;
// Post-guard snapshot of the scopes this section renders (see header). search /
// sort / re-render / lookups all read this, never the shared cache.
let _ctxPortalScopes = [];
// scope_id of the row currently in inline-rename mode (null = none).
let _ctxPortalEditingId = null;
// Client-side view state — never refetched on search/sort, only the rows are
// repainted (the search box keeps focus).
let _ctxPortalSearch = '';
let _ctxPortalSort = 'name'; // 'name' | 'items'
// Default ON: a fresh board is dominated by ~dozens of discovered-but-stale
// (uninitialized, no ``.memtomem/``) roots whose 0/0/0/0 rows bury the few
// real projects. The toggle hides ``scope.stale`` rows (Server CWD is always
// kept, it is pinned regardless of count) and a hint reports how many it hid.
let _ctxPortalHideUninit = true;

// Map of scope_id -> runtimes list (one GET /api/context/runtimes per scope; the
// endpoint resolves per-scope via resolve_scope_root's project_scope_id param).
let _ctxPortalRuntimesMap = {};
// Parallel availability map, keyed the same way: scope_ids whose runtimes
// probe is UNKNOWN (#1692 PR 6) — the fetch failed, or the server reported
// runtimes_status 'unavailable'. Kept separate so _ctxPortalRuntimesMap values
// stay plain arrays for the filter and row lights. Unknown must not render as
// four grey "uninstalled" chips (false-healthy); it gets its own chip + Retry.
// Reset alongside the runtimes map on every fresh load.
let _ctxPortalRuntimesUnavailable = {};
// Set of scope_ids whose synced context has drifted from the Store, from the
// deferred GET /api/context/status-all fetch (#1649). Kept out of the initial
// render path: status-all shells out to git per project (sequential), so the
// board paints from projects+runtimes first and the drift badge fills in when
// this resolves. Reset on every fresh load so a tier flip / refresh can't leave
// a stale badge. project_shared tier only (the endpoint 400s on other tiers).
let _ctxPortalDriftMap = {};
// Parallel to the drift map, keyed the same way: projects whose status-all
// entry is ``error`` — the per-project status check itself failed (corrupt
// lockfile or a probe that raised), so drift is *unknown*, not clean. Reset
// alongside the drift map. Unlike drift, an error is not Sync-remediable, so
// its badge offers no Sync affordance.
let _ctxPortalErrorMap = {};
// Registry read report from the last projects payload (#1692): null when the
// registry read was clean, else {status: 'ok'|'unavailable', warnings: [...]}.
// Drives the non-blocking banner above the board — the roster below still
// renders (server-cwd / scan rows survive a degraded registry). Reset on every
// fresh load so a repaired registry clears the banner on Retry.
let _ctxPortalRegistryWarning = null;
// Active runtime filter: null | 'claude' | 'antigravity' | 'codex' | 'kimi'
let _ctxPortalRuntimeFilter = null;

// In-scope provider clients (ADR-0021 §B), in display order. Antigravity is the
// gemini-family client and keeps its own label (RUNTIME_TO_CLIENT: gemini→antigravity).
const _CTX_PORTAL_RUNTIME_CLIENTS = ['claude', 'antigravity', 'codex', 'kimi'];

// Display label for a provider client. Proper-noun product names (Claude,
// Antigravity, Codex, Kimi) are identical across locales, so this is
// intentionally not i18n.
function _ctxPortalRuntimeLabel(name) {
  if (name === 'antigravity') return 'Antigravity';
  return name.charAt(0).toUpperCase() + name.slice(1);
}

// Initialize guide modal
const _ctxPortalInstallGuideModal = document.getElementById('ctx-install-guide-modal');
if (_ctxPortalInstallGuideModal) {
  let releaseFn = null;
  const closeGuide = () => {
    if (releaseFn) { releaseFn(); releaseFn = null; }
    _ctxPortalInstallGuideModal.setAttribute('hidden', '');
  };
  window.registerModalCloser(_ctxPortalInstallGuideModal, closeGuide);
  document.getElementById('ctx-install-guide-close-btn')?.addEventListener('click', closeGuide);
  document.getElementById('ctx-install-guide-ok-btn')?.addEventListener('click', closeGuide);
  
  window._ctxPortalShowInstallGuide = (runtimeName) => {
    const titleEl = document.getElementById('ctx-install-guide-title');
    const bodyEl = document.getElementById('ctx-install-guide-body');
    if (!titleEl || !bodyEl) return;
    // Re-entrancy guard: release a still-open guide before reopening so we never
    // orphan an _ACTIVE_MODALS entry / leave the background inert.
    if (releaseFn) { releaseFn(); releaseFn = null; }

    const displayName = _ctxPortalRuntimeLabel(runtimeName);
    titleEl.textContent = t('settings.ctx.install_guide_title').replace('{runtime}', displayName);

    // Code-fence header labels: "Terminal" flows through i18n
    // (guide_code_header_terminal). JSON / TOML stay literal as format names /
    // proper nouns — an explicit decision, not an oversight (#1351). The commands
    // inside use the documented exact-pinned, no-install uvx form. This remains
    // launchable when the web UI itself came from a project-local ``uv add``
    // environment that external clients cannot discover on PATH.
    let guideHtml = '';
    if (runtimeName === 'claude') {
      guideHtml = `
        <p class="guide-text">${escapeHtml(t('settings.ctx.guide_claude_desc'))}</p>
        <div class="guide-code-block">
          <div class="guide-code-header">
            <span>${escapeHtml(t('settings.ctx.guide_code_header_terminal'))}</span>
            <button type="button" class="btn-ghost btn-xs copy-code-btn">${escapeHtml(t('settings.ctx.copy'))}</button>
          </div>
          <pre class="guide-code"><code>claude mcp add memtomem -- uvx --isolated --from 'memtomem[all]==0.3.11' memtomem-server</code></pre>
        </div>
        <p class="guide-note">${escapeHtml(t('settings.ctx.guide_claude_note'))}</p>
      `;
    } else if (runtimeName === 'antigravity') {
      guideHtml = `
        <p class="guide-text">${escapeHtml(t('settings.ctx.guide_antigravity_desc'))}</p>
        <h5 class="guide-section-sub">${escapeHtml(t('settings.ctx.guide_antigravity_cli'))}</h5>
        <p class="guide-text-sm">${escapeHtml(t('settings.ctx.guide_antigravity_cli_desc'))}</p>
        <div class="guide-code-block">
          <div class="guide-code-header">
            <span>JSON</span>
            <button type="button" class="btn-ghost btn-xs copy-code-btn">${escapeHtml(t('settings.ctx.copy'))}</button>
          </div>
          <pre class="guide-code"><code>{
  "mcpServers": {
    "memtomem": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--isolated", "--from", "memtomem[all]==0.3.11", "memtomem-server"]
    }
  }
}</code></pre>
        </div>
      `;
    } else if (runtimeName === 'codex') {
      guideHtml = `
        <p class="guide-text">${escapeHtml(t('settings.ctx.guide_codex_desc'))}</p>
        <div class="guide-code-block">
          <div class="guide-code-header">
            <span>TOML</span>
            <button type="button" class="btn-ghost btn-xs copy-code-btn">${escapeHtml(t('settings.ctx.copy'))}</button>
          </div>
          <pre class="guide-code"><code>[mcp_servers.memtomem]
command = "uvx"
args = ["--isolated", "--from", "memtomem[all]==0.3.11", "memtomem-server"]</code></pre>
        </div>
      `;
    } else if (runtimeName === 'kimi') {
      guideHtml = `
        <p class="guide-text">${escapeHtml(t('settings.ctx.guide_kimi_desc'))}</p>
        <div class="guide-code-block">
          <div class="guide-code-header">
            <span>${escapeHtml(t('settings.ctx.guide_code_header_terminal'))}</span>
            <button type="button" class="btn-ghost btn-xs copy-code-btn">${escapeHtml(t('settings.ctx.copy'))}</button>
          </div>
          <pre class="guide-code"><code>mm init --mcp kimi</code></pre>
        </div>
      `;
    }

    bodyEl.innerHTML = guideHtml;
    releaseFn = window.openModal(_ctxPortalInstallGuideModal);
    // Move focus into the dialog so keyboard/SR users aren't parked on the now-
    // inert trigger chip (OK is the safe default — no destructive action), mirroring
    // the sibling conflict modal in context-gateway.js.
    document.getElementById('ctx-install-guide-ok-btn')?.focus();

    // Wire copy buttons. navigator.clipboard is undefined on insecure (non-localhost)
    // contexts, so guard the call and swallow a denied/absent clipboard quietly.
    bodyEl.querySelectorAll('.copy-code-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const code = btn.closest('.guide-code-block').querySelector('.guide-code code').textContent;
        if (!navigator.clipboard?.writeText) return;
        navigator.clipboard.writeText(code).then(() => {
          const originalText = btn.textContent;
          btn.textContent = t('settings.ctx.copied');
          setTimeout(() => { btn.textContent = originalText; }, 1500);
        }).catch(() => {});
      });
    });
  };
}

function _ctxPortalSyncFilterFromDeepLink() {
  const link = _ctxParseDeepLink();
  _ctxPortalRuntimeFilter =
    link && _CTX_PORTAL_RUNTIME_CLIENTS.includes(link.runtime) ? link.runtime : null;
}

function _ctxPortalSetRuntimeFilter(runtime) {
  _ctxPortalRuntimeFilter = runtime;
  const link = _ctxParseDeepLink() || {};
  link.runtime = runtime || '';
  _ctxSetDeepLink(link);
  _ctxPortalRenderHeadingChips();
  _ctxPortalRenderRows();
}

// Per-CLI traffic-lights on a project row (PR4-deferred "row UI"). Each dot is a
// non-color cue carrier via role=img + aria-label (WCAG 1.4.1 — state is not
// conveyed by color alone). Uses dedicated short copy (no install-guide click
// hint — only the heading greyed chips open the guide).
function _ctxPortalRowTrafficLightsHtml(scope) {
  if (scope.missing) return '';
  // Unknown probe state (#1692 PR 6): four grey "uninstalled" dots would be
  // false-healthy, so render a single explicit unavailable light. role=img +
  // aria-label like the sibling dots — state is not conveyed by color alone.
  if (_ctxPortalRuntimesUnavailable[scope.scope_id]) {
    const label = `${t('settings.ctx.portal_runtimes_unavailable')}: ${t('settings.ctx.portal_runtimes_unavailable_tip')}`;
    return `<div class="ctx-portal-row-lights"><span class="ctx-portal-row-light ctx-portal-row-light--unavailable" role="img" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"></span></div>`;
  }
  const runtimes = _ctxPortalRuntimesMap[scope.scope_id] || [];
  const lights = _CTX_PORTAL_RUNTIME_CLIENTS.map(name => {
    const r = runtimes.find(item => item.name === name);
    let state = 'uninstalled'; // 'uninstalled' | 'installed' | 'registered'
    let detail = t('settings.ctx.runtime_not_installed');
    if (r && r.error_kind) {
      // Config unreadable — error-first precedence, matching the heading chip, so
      // one runtime never reads installed/registered here yet error in the heading.
      detail = t('settings.ctx.runtime_error_tooltip');
    } else if (r && r.installed) {
      if (r.memtomem_registered || r.mms_registered) {
        state = 'registered';
        detail = t('settings.ctx.runtime_registered_tooltip').replace('{path}', (r.config_paths || []).join(', '));
      } else {
        state = 'installed';
        detail = t('settings.ctx.runtime_installed_tooltip');
      }
    }
    const label = `${_ctxPortalRuntimeLabel(name)}: ${detail}`;
    return `<span class="ctx-portal-row-light ctx-portal-row-light--${state}" role="img" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"></span>`;
  }).join('');
  return `<div class="ctx-portal-row-lights">${lights}</div>`;
}

function _ctxPortalRenderHeadingChips() {
  const container = document.getElementById('ctx-portal-heading-chips');
  if (!container) return;

  // The availability map mirrors the runtimes map's scope selection: an
  // unavailable scope holds ``[]`` (truthy) in the runtimes map, so the ''
  // fallback below only fires when the active scope has no entry at all.
  const chipScopeId = Object.prototype.hasOwnProperty.call(_ctxPortalRuntimesMap, _ctxActiveScopeId)
    ? _ctxActiveScopeId : '';
  const runtimes = _ctxPortalRuntimesMap[chipScopeId] || [];
  const runtimesUnavailable = !!_ctxPortalRuntimesUnavailable[chipScopeId];

  const runtimeChips = _CTX_PORTAL_RUNTIME_CLIENTS.map(name => {
    const r = runtimes.find(item => item.name === name);
    const installed = !!(r && r.installed);
    const registered = !!(r && (r.memtomem_registered || r.mms_registered));
    const configPaths = (r && r.config_paths) || [];
    const errorKind = (r && r.error_kind) || null;
    const displayName = _ctxPortalRuntimeLabel(name);

    let stateClass;
    let tooltip;
    if (errorKind) {
      // Config exists but is unreadable (permission/parse) — greyed, but the
      // install guide is not the remedy, so this chip stays non-interactive.
      stateClass = 'ctx-runtime-chip--greyed';
      tooltip = t('settings.ctx.runtime_error_tooltip');
    } else if (!installed) {
      stateClass = 'ctx-runtime-chip--greyed';
      tooltip = t('settings.ctx.runtime_uninstalled_tooltip');
    } else if (registered) {
      stateClass = 'ctx-runtime-chip--registered';
      tooltip = t('settings.ctx.runtime_registered_tooltip').replace('{path}', configPaths.join(', '));
    } else {
      stateClass = 'ctx-runtime-chip--installed';
      tooltip = t('settings.ctx.runtime_installed_tooltip');
    }

    // A not-installed chip is the install-guide trigger, so render a real
    // <button> — keyboard-reachable and it announces its action (#1003 prefers
    // a true button over a click-only span). Installed/registered/error chips
    // are non-interactive status, so a <span> with a hover title suffices.
    const aria = `${displayName}: ${tooltip}`;
    if (!installed && !errorKind) {
      return `<button type="button" class="ctx-runtime-chip ${stateClass}" data-runtime="${escapeHtml(name)}" title="${escapeHtml(tooltip)}" aria-label="${escapeHtml(aria)}">${escapeHtml(displayName)}</button>`;
    }
    // role=img + aria-label so the colored status (and config path) is exposed to
    // screen readers, not just the hover title — matching the row dots.
    return `<span class="ctx-runtime-chip ${stateClass}" data-runtime="${escapeHtml(name)}" role="img" title="${escapeHtml(tooltip)}" aria-label="${escapeHtml(aria)}">${escapeHtml(displayName)}</span>`;
  }).join('');

  // Unknown probe state (#1692 PR 6): the fetch failed or the server said
  // runtimes_status 'unavailable'. Four grey chips would read as "nothing
  // installed" — false-healthy — so render one explicit unavailable chip plus
  // Retry instead (same affordance as the counts pill). role=img + aria-label
  // keep the state off the color channel, matching the sibling chips.
  let chipStripHtml = runtimeChips;
  if (runtimesUnavailable) {
    const unavailableLabel = t('settings.ctx.portal_runtimes_unavailable');
    const unavailableTip = t('settings.ctx.portal_runtimes_unavailable_tip');
    // Name the active scope in the Retry's accessible label: the same view can
    // hold registry and per-project count Retry buttons, so a bare "Retry"
    // would be indistinguishable to screen-reader users (mirrors the
    // portal_counts_retry_aria convention).
    const activeScope = _ctxPortalScopes.find((s) => s.scope_id === _ctxActiveScopeId);
    const retryAria = t('settings.ctx.portal_runtimes_retry_aria', {
      label: activeScope ? _ctxScopeDisplayLabel(activeScope) : '',
    });
    chipStripHtml =
      `<span class="ctx-runtime-chip ctx-runtime-chip--unavailable" role="img" title="${escapeHtml(unavailableTip)}" aria-label="${escapeHtml(`${unavailableLabel}: ${unavailableTip}`)}">${escapeHtml(unavailableLabel)}</span>`
      + `<button type="button" class="btn-ghost btn-xs ctx-portal-runtimes-retry" aria-label="${escapeHtml(retryAria)}">${escapeHtml(t('settings.ctx.retry'))}</button>`;
  }

  const filterGroup = ['all', ..._CTX_PORTAL_RUNTIME_CLIENTS].map(name => {
    const active = (name === 'all' && !_ctxPortalRuntimeFilter) || (_ctxPortalRuntimeFilter === name);
    const label = name === 'all' ? t('settings.ctx.filter_all') : _ctxPortalRuntimeLabel(name);
    return `<button type="button" class="${active ? 'active' : ''}" data-filter="${escapeHtml(name)}" aria-pressed="${active}">${escapeHtml(label)}</button>`;
  }).join('');

  // rank 13: a legend decoding the two color/glyph systems used on each card —
  // the per-runtime traffic-light dots (color = install state) and the inventory
  // emoji (glyph = artifact type). Reuses the app's ``.graph-legend`` language and
  // the EXACT ``.ctx-portal-row-light--*`` swatches so the colors match the row
  // dots 1:1. The runtime order behind the dots is already discoverable from the
  // Runtimes row above (Claude · Antigravity · Codex · Kimi via _ctxPortalRuntimeLabel).
  const legendDots = [
    { state: 'uninstalled', key: 'settings.ctx.portal_legend_uninstalled' },
    { state: 'installed', key: 'settings.ctx.portal_legend_installed' },
    { state: 'registered', key: 'settings.ctx.portal_legend_registered' },
  ].map(d => `<span class="graph-legend-item"><span class="ctx-portal-row-light ctx-portal-row-light--${d.state}"></span>${escapeHtml(t(d.key))}</span>`).join('');
  const legendCounts = _CTX_PORTAL_COUNT_TYPES.map(c =>
    `<span class="graph-legend-item"><span aria-hidden="true">${c.icon}</span>${escapeHtml(t(c.labelKey))}</span>`
  ).join('');

  container.innerHTML = `
    <div class="ctx-portal-runtimes-row">
      <span class="ctx-portal-heading-label">${escapeHtml(t('settings.ctx.runtimes_label'))}</span>
      <div class="ctx-portal-runtimes-list">${chipStripHtml}</div>
    </div>
    <div class="ctx-portal-filter-row">
      <span class="ctx-portal-heading-label" id="ctx-portal-filter-label">${escapeHtml(t('settings.ctx.filter_label'))}</span>
      <div class="ctx-portal-filter-group" role="group" aria-labelledby="ctx-portal-filter-label">${filterGroup}</div>
    </div>
    <div class="ctx-portal-legend-row">
      <span class="ctx-portal-heading-label">${escapeHtml(t('settings.ctx.portal_legend_label'))}</span>
      <div class="graph-legend">${legendDots}${legendCounts}</div>
    </div>
  `;

  // Only the not-installed chips render as <button> (error-state greyed chips
  // are non-interactive <span>s), so target buttons to open the install guide.
  container.querySelectorAll('button.ctx-runtime-chip--greyed').forEach(chip => {
    chip.addEventListener('click', () => {
      const name = chip.dataset.runtime;
      if (name && typeof window._ctxPortalShowInstallGuide === 'function') {
        window._ctxPortalShowInstallGuide(name);
      }
    });
  });

  // Whole-roster refetch, same recovery affordance as the counts pill and the
  // registry banner (#1692): a repaired probe repopulates the chips.
  const runtimesRetry = container.querySelector('.ctx-portal-runtimes-retry');
  if (runtimesRetry) runtimesRetry.addEventListener('click', () => { loadCtxProjects(); });

  container.querySelectorAll('.ctx-portal-filter-group button').forEach(btn => {
    btn.addEventListener('click', () => {
      const filter = btn.dataset.filter;
      const targetFilter = filter === 'all' ? null : filter;
      _ctxPortalSetRuntimeFilter(targetFilter);
    });
  });
}

const _CTX_PORTAL_COUNT_TYPES = [
  { key: 'skills', icon: '🧩', labelKey: 'settings.nav.ctx_skills' },
  { key: 'commands', icon: '⌘', labelKey: 'settings.nav.ctx_commands' },
  { key: 'agents', icon: '🤖', labelKey: 'settings.nav.ctx_agents' },
  { key: 'mcp-servers', icon: '🔌', labelKey: 'settings.nav.ctx_mcp_servers' },
];

function _ctxPortalTotalCount(scope) {
  if (!scope || !scope.counts) return 0;
  return _CTX_PORTAL_COUNT_TYPES.reduce((sum, ct) => sum + (scope.counts[ct.key] || 0), 0);
}

// A count probe failed for at least one kind (#1692 PR 5) — the row's counts
// are partial (failed kinds ride as 0), so neither the chips nor the items
// sort may treat the total as authoritative. ``Array.isArray`` doubles as the
// old-server guard: a payload without the field renders the legacy chip path.
function _ctxPortalCountsUnavailable(scope) {
  return !!scope && Array.isArray(scope.counts_unavailable) && scope.counts_unavailable.length > 0;
}

// A scope is "managed" — renameable / pausable / unregisterable — only when it
// is enrolled (carries a known_projects entry; ``_ctxScopeIsEnrolled`` reads
// ``sources`` for "known-projects"). A scan-only auto-displayed row has no
// entry, so PATCH/DELETE would 404 — it gets an Enroll action instead. The
// implicit Server-CWD row is never managed even when it is also enrolled: its
// registration is incidental and the running directory cannot be paused.
function _ctxPortalIsManaged(scope) {
  return _ctxScopeIsEnrolled(scope) && !_ctxScopeIsServerCwd(scope);
}

// A discoverable-but-not-enrolled, present project can be enrolled (POST
// known-projects) so it joins sync. Server-cwd is implicitly sync-eligible and
// a missing root cannot be enrolled.
function _ctxPortalCanEnroll(scope) {
  return !!scope && !_ctxScopeIsEnrolled(scope) && !_ctxScopeIsServerCwd(scope) && !scope.missing;
}

// An enrolled, manageable scope that has been paused (``enabled: false``) —
// excluded from sync until resumed. MUST exclude server-cwd: the backend
// coalesces a known-projects entry with the running dir into one scope, so a
// project paused then reopened as cwd carries ``enabled: false`` yet stays
// ``sync_eligible: true`` (the running dir cannot be paused). Without the
// server-cwd guard the row would show an unresumable "sync paused" badge that
// contradicts its actual eligibility — and Resume is already suppressed for cwd
// via _ctxPortalIsManaged. Scan-only (not enrolled) rows are "not enrolled",
// not "paused". Drives both the paused badge and the Resume toggle label.
function _ctxPortalIsPaused(scope) {
  return _ctxScopeIsEnrolled(scope) && !_ctxScopeIsServerCwd(scope) && scope.enabled === false;
}

async function loadCtxProjects() {
  const seq = ++_ctxProjectsSeq;
  // Abort the superseded portal load's in-flight fetches (#1286): the signal
  // threads through the projects fetch and the per-scope /runtimes fan-out, so a
  // rapid re-entry / tier flip cancels the whole load instead of just dropping
  // its render. _ctxSwapAbort degrades to seq-only where AbortController is
  // unavailable (the guards below stay as defense in depth).
  _ctxProjectsAbort = _ctxSwapAbort(_ctxProjectsAbort);
  const signal = _ctxProjectsAbort?.signal;
  const requestedScope = _ctxTargetScope;
  const listEl = document.getElementById('ctx-projects-list');
  if (!listEl) return false;
  panelLoading(listEl);
  
  _ctxPortalSyncFilterFromDeepLink();
  
  try {
    // Fetch under the pinned tier, then commit the shared cache ONLY after the
    // guard passes — a superseded in-flight fetch must not clobber the shared
    // ``_ctxProjectsCache`` / active scope (#1194). The board already renders
    // from its own post-guard ``_ctxPortalScopes`` snapshot; this extends the
    // same discipline to the shared cache the helper used to commit pre-guard.
    const result = await _ctxFetchProjectsData({ targetScope: requestedScope, signal, includeCounts: false });
    // Bail if a newer load started OR the tier flipped under us mid-fetch
    // (#972): counts in this payload were computed for ``requestedScope``.
    if (seq !== _ctxProjectsSeq || requestedScope !== _ctxTargetScope) return false;
    const data = _ctxCommitProjects(result);
    const scopes = data.scopes || [];
    // Reset edit state on a fresh load and commit the validated snapshot before
    // any render reads it.
    _ctxPortalEditingId = null;
    _ctxPortalScopes = scopes;
    // Drop any drift badges from the previous load before it repaints — a tier
    // flip or refresh must not carry a stale badge into the new roster. The
    // deferred fetch below (project_shared only) repopulates it.
    _ctxPortalDriftMap = {};
    _ctxPortalErrorMap = {};
    // Registry read report (#1692). Warnings may be present while status stays
    // "ok" (row-level skips), so gate on either signal.
    const registryWarnings = Array.isArray(data.warnings) ? data.warnings : [];
    _ctxPortalRegistryWarning =
      data.registry_status === 'unavailable' || registryWarnings.length
        ? { status: data.registry_status || 'ok', warnings: registryWarnings }
        : null;
    if (!scopes.length) {
      // Server CWD is always present, so an empty roster means the load
      // failed — render the shared load-error + Retry (#1287; the helper
      // lives in context-gateway.js, which loads before this file). Clear
      // the heading chip strip too, so a stale strip can't outlive an
      // emptied list.
      const headingEl = document.getElementById('ctx-portal-heading-chips');
      if (headingEl) headingEl.innerHTML = '';
      _ctxScopesLoadError(
        listEl, t('settings.ctx.scopes_load_failed'), '', () => loadCtxProjects(),
      );
      return true; // this invocation owned the (load-error) render — not a supersede
    }

    // Paint the cheap roster before any per-project probes complete. Enrichment
    // is bounded to four workers so a large registry cannot create an N-request
    // burst or delay the first useful frame.
    _ctxPortalRuntimesMap = {};
    _ctxPortalRuntimesUnavailable = {};
    _ctxPortalRenderHeadingChips();
    _ctxPortalRenderScaffold(listEl);
    _ctxPortalRenderRows();

    const enrichScope = async (scope) => {
      if (scope.missing) {
        // Deliberate empty (missing root), not an unknown probe state.
        return { scopeId: scope.scope_id, runtimes: [], unavailable: false };
      }
      // Counts detail and runtimes are fetched under separate try blocks so a
      // failure in one can't mask the other's result.
      try {
        if (scope.counts == null) {
          const detailUrl = _ctxWithTargetScope(
            `/api/context/projects?scope_id=${encodeURIComponent(scope.scope_id)}&include=counts,runtime_coverage`,
            { includeScope: false, targetScope: requestedScope },
          );
          const detailRes = await fetch(detailUrl, { signal });
          const enriched = detailRes.ok
            ? (await detailRes.json()).scopes?.find((item) => item.scope_id === scope.scope_id)
            : null;
          if (enriched) {
            Object.assign(scope, enriched);
          } else {
            // The whole detail probe failed (non-OK / scope vanished). Leaving
            // ``counts`` null would render NO chips at all — a silent variant
            // of the failure-invisible state #1692 fixed. Mark every kind
            // unavailable so the shared badge + Retry affordance renders.
            scope.counts = {};
            scope.counts_unavailable = _CTX_PORTAL_COUNT_TYPES.map((ct) => ct.key);
          }
        }
      } catch (err) {
        // An aborted probe means a newer load superseded us — its own fetch
        // owns the counts, so don't stamp a failure badge on the shared scope.
        if (scope.counts == null && !_ctxIsAbortError(err)) {
          scope.counts = {};
          scope.counts_unavailable = _CTX_PORTAL_COUNT_TYPES.map((ct) => ct.key);
        }
      }
      try {
        const url = _ctxWithTargetScope('/api/context/runtimes', { scopeId: scope.scope_id });
        const res = await fetch(url, { signal });
        if (!res.ok) throw new Error();
        const rData = await res.json();
        // Array.isArray (not ||): a truthy malformed ``runtimes`` would reach
        // .find() in the renderers. Strict status check: an old server has no
        // runtimes_status, which must read healthy, not unavailable.
        return {
          scopeId: scope.scope_id,
          runtimes: Array.isArray(rData.runtimes) ? rData.runtimes : [],
          unavailable: rData.runtimes_status === 'unavailable',
        };
      } catch (err) {
        // A failed fetch is UNKNOWN state, not "no clients" — but an aborted
        // one belongs to a superseding load (same idiom as the counts catch
        // above), whose own fetch owns the availability verdict.
        return { scopeId: scope.scope_id, runtimes: [], unavailable: !_ctxIsAbortError(err) };
      }
    };
    const queue = scopes.slice();
    const runtimesResults = [];
    const worker = async () => {
      while (queue.length) {
        const scope = queue.shift();
        const enriched = await enrichScope(scope);
        runtimesResults.push(enriched);
        if (seq === _ctxProjectsSeq && requestedScope === _ctxTargetScope) {
          _ctxPortalRuntimesMap[enriched.scopeId] = enriched.runtimes;
          if (enriched.unavailable) _ctxPortalRuntimesUnavailable[enriched.scopeId] = true;
          _ctxPortalRenderRows();
          // The heading chips read the ACTIVE scope's runtimes from the same
          // map — the roster-first paint rendered them from an empty map, so
          // repaint once its data lands or they stay greyed for the run.
          if (enriched.scopeId === _ctxActiveScopeId) _ctxPortalRenderHeadingChips();
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, queue.length) }, worker));
    if (seq !== _ctxProjectsSeq || requestedScope !== _ctxTargetScope) return false;

    _ctxPortalRuntimesMap = {};
    _ctxPortalRuntimesUnavailable = {};
    for (const result of runtimesResults) {
      _ctxPortalRuntimesMap[result.scopeId] = result.runtimes;
      if (result.unavailable) _ctxPortalRuntimesUnavailable[result.scopeId] = true;
    }

    _ctxPortalRenderHeadingChips();
    _ctxPortalRenderRows();
    // Fire-and-forget the cross-project drift check (#1649). Not awaited: the
    // board is already painted and status-all runs sequential per-project git
    // work, so the drift badge fills in when it resolves. Gated on
    // project_shared — the only tier the endpoint serves (400 otherwise).
    if (requestedScope === 'project_shared') {
      _ctxPortalLoadDrift(seq, requestedScope, signal);
    }
    return true;
  } catch (err) {
    // Aborted = a newer portal load superseded us (#1286); its seq guard owns
    // the list, so don't paint a load-error + Retry over it. Return false so the
    // Refresh button doesn't toast a success this superseded load never made
    // (Codex review).
    if (_ctxIsAbortError(err) || seq !== _ctxProjectsSeq) return false;
    // Route through the shared load-error helper so the failure is announced
    // (``role="alert"``) and retryable, matching the empty-roster path above.
    _ctxScopesLoadError(
      listEl, t('settings.ctx.portal_load_failed'), err.message || '',
      () => loadCtxProjects(),
    );
    return true; // owned the (error) render — ran to completion as the latest
  }
}

// Deferred cross-project drift check (#1649) — the sole web consumer of
// GET /api/context/status-all. Called (not awaited) after the board paints, so
// its sequential per-project git work never blocks the initial render. Best
// effort throughout: any abort / non-OK / network failure leaves the board as
// painted (no badge, no toast), mirroring the overview wiki-behind badge's
// defensive-reader posture (#1546). ``seq``/``requestedScope``/``signal`` are
// captured from the enclosing loadCtxProjects invocation so a superseded load's
// late result is discarded by the same guard the projects+runtimes fetches use.
async function _ctxPortalLoadDrift(seq, requestedScope, signal) {
  let data;
  try {
    const res = await fetch(
      '/api/context/status-all?target_scope=project_shared', { signal },
    );
    if (!res.ok) return;
    data = await res.json();
  } catch (err) {
    return; // abort (superseded load) or network error — leave the board as-is
  }
  // A newer load started or the tier flipped mid-fetch: this payload was
  // computed for a roster that is no longer on screen.
  if (seq !== _ctxProjectsSeq || requestedScope !== _ctxTargetScope) return;
  const projects = Array.isArray(data?.projects) ? data.projects : [];
  const driftMap = {};
  const errorMap = {};
  for (const entry of projects) {
    if (entry?.status === 'drift') driftMap[entry.project_scope_id || ''] = true;
    else if (entry?.status === 'error') errorMap[entry.project_scope_id || ''] = true;
  }
  _ctxPortalDriftMap = driftMap;
  _ctxPortalErrorMap = errorMap;
  // Skip the immediate repaint while a row is in inline-rename mode — a full
  // repaint would discard the open input's typed value. The map is already set,
  // so the next natural repaint (save/cancel/langchange) shows the badges.
  if (_ctxPortalEditingId === null) _ctxPortalRenderRows();
}

// Header (search + sort) is painted once and kept across row repaints so the
// search field never loses focus mid-type; only #ctx-projects-rows is rebuilt
// on search/sort input.
// Non-blocking registry read-failure banner (#1692). Unlike
// _ctxScopesLoadError this never replaces the board — a degraded registry
// only hides *registered* rows, so the roster (server-cwd / scan) still
// renders below it. Message keys off registry_status: "unavailable" is the
// whole-file failure; "ok"-with-warning is the row-skip case, whose count
// rides in the warning item's skipped_rows.
function _ctxPortalRegistryBannerHtml() {
  const warn = _ctxPortalRegistryWarning;
  if (!warn) return '';
  const first = warn.warnings[0] || {};
  const msg = warn.status === 'unavailable'
    ? t('settings.ctx.portal_registry_unavailable')
    : t('settings.ctx.portal_registry_rows_skipped', {
        count: typeof first.skipped_rows === 'number' ? first.skipped_rows : 0,
      });
  const reason = first.message
    ? `<div class="ctx-diagnostic-detail"><div class="ctx-diagnostic-reason">${escapeHtml(first.message)}</div></div>`
    : '';
  return `
    <div class="ctx-portal-registry-banner" role="alert">
      <div class="ctx-portal-registry-banner-msg">${escapeHtml(msg)}${reason}</div>
      <button type="button" class="btn-ghost ctx-scopes-retry">${escapeHtml(t('settings.ctx.retry'))}</button>
    </div>`;
}

function _ctxPortalRenderScaffold(listEl) {
  const sortName = escapeHtml(t('settings.ctx.portal_sort_name'));
  const sortItems = escapeHtml(t('settings.ctx.portal_sort_items'));
  listEl.innerHTML = `
    ${_ctxPortalRegistryBannerHtml()}
    <div class="ctx-portal-toolbar">
      <input type="search" id="ctx-portal-search" class="ctx-portal-search"
             value="${escapeHtml(_ctxPortalSearch)}"
             placeholder="${escapeHtml(t('settings.ctx.portal_search_placeholder'))}"
             aria-label="${escapeHtml(t('settings.ctx.portal_search_placeholder'))}">
      <label class="ctx-portal-sort">
        <span>${escapeHtml(t('settings.ctx.portal_sort_label'))}</span>
        <select id="ctx-portal-sort">
          <option value="name"${_ctxPortalSort === 'name' ? ' selected' : ''}>${sortName}</option>
          <option value="items"${_ctxPortalSort === 'items' ? ' selected' : ''}>${sortItems}</option>
        </select>
      </label>
      <label class="ctx-portal-hide-uninit">
        <input type="checkbox" id="ctx-portal-hide-uninit"${_ctxPortalHideUninit ? ' checked' : ''}>
        <span>${escapeHtml(t('settings.ctx.portal_hide_uninit'))}</span>
      </label>
    </div>
    <div id="ctx-projects-rows" class="ctx-portal-rows"></div>`;

  const search = listEl.querySelector('#ctx-portal-search');
  if (search) {
    search.addEventListener('input', () => {
      _ctxPortalSearch = search.value;
      _ctxPortalRenderRows();
    });
  }
  const sort = listEl.querySelector('#ctx-portal-sort');
  if (sort) {
    sort.addEventListener('change', () => {
      _ctxPortalSort = sort.value === 'items' ? 'items' : 'name';
      _ctxPortalRenderRows();
    });
  }
  const hideUninit = listEl.querySelector('#ctx-portal-hide-uninit');
  if (hideUninit) {
    hideUninit.addEventListener('change', () => {
      _ctxPortalHideUninit = hideUninit.checked;
      _ctxPortalRenderRows();
    });
  }
  const registryRetry = listEl.querySelector('.ctx-portal-registry-banner .ctx-scopes-retry');
  if (registryRetry) {
    registryRetry.addEventListener('click', () => { loadCtxProjects(); });
  }
}

// Scopes passing the search box + provider filter, before the hide-uninit
// toggle and sort. Shared by ``_ctxPortalVisibleScopes`` and the hidden-count
// hint so the two can never disagree about what "uninitialized" was filtered.
function _ctxPortalMatchedScopes() {
  const all = Array.isArray(_ctxPortalScopes) ? _ctxPortalScopes : [];
  const q = _ctxPortalSearch.trim().toLowerCase();
  let matched = q
    ? all.filter(s => {
        const label = (_ctxScopeDisplayLabel(s) || '').toLowerCase();
        const root = (s.root || '').toLowerCase();
        return label.includes(q) || root.includes(q);
      })
    : all.slice();

  // Apply provider client-side filter. Runtimes-unavailable scopes hold []
  // and drop out of a filtered view — unknown ≠ registered (#1692 PR 6); the
  // unfiltered board still shows their explicit unavailable light.
  if (_ctxPortalRuntimeFilter) {
    matched = matched.filter(s => {
      const runtimes = _ctxPortalRuntimesMap[s.scope_id] || [];
      const r = runtimes.find(item => item.name === _ctxPortalRuntimeFilter);
      return r && (r.memtomem_registered || r.mms_registered);
    });
  }
  return matched;
}

// Whether the hide-uninit toggle is actively suppressing rows right now. An
// explicit search query overrides it — typing a name should find a project
// even if it is uninitialized, so the toggle only declutters the default
// browse view, not search results.
function _ctxPortalHideUninitActive() {
  return _ctxPortalHideUninit && _ctxPortalSearch.trim() === '';
}

// Count of stale (uninitialized) non-CWD scopes the hide-uninit toggle is
// currently suppressing — drives the "N hidden" hint and the toggle relevance.
function _ctxPortalHiddenUninitCount() {
  if (!_ctxPortalHideUninitActive()) return 0;
  return _ctxPortalMatchedScopes().filter(s => s.stale && !_ctxScopeIsServerCwd(s)).length;
}

function _ctxPortalVisibleScopes() {
  let matched = _ctxPortalMatchedScopes();

  // Hide uninitialized (stale = no ``.memtomem/`` yet) roots unless explicitly
  // shown or being searched for. Server CWD is never hidden — it is the primary
  // tree and is pinned first below regardless of count.
  if (_ctxPortalHideUninitActive()) {
    matched = matched.filter(s => !s.stale || _ctxScopeIsServerCwd(s));
  }

  // Server CWD is pinned first (the primary working tree); the rest follow the
  // chosen sort. ``localeCompare`` keeps the name sort stable + accent-aware.
  const cwd = matched.filter(_ctxScopeIsServerCwd);
  const rest = matched.filter(s => !_ctxScopeIsServerCwd(s));
  rest.sort((a, b) => {
    if (_ctxPortalSort === 'items') {
      // Rows with a failed count probe sort after rows with reliable counts —
      // their partial totals (failed kinds ride as 0) must not silently rank
      // against authoritative ones (#1692 PR 5). Name-order within the
      // unavailable group keeps the tail deterministic.
      const aUnavail = _ctxPortalCountsUnavailable(a);
      const bUnavail = _ctxPortalCountsUnavailable(b);
      if (aUnavail !== bUnavail) return aUnavail ? 1 : -1;
      if (aUnavail) return _ctxScopeDisplayLabel(a).localeCompare(_ctxScopeDisplayLabel(b));
      return _ctxPortalTotalCount(b) - _ctxPortalTotalCount(a);
    }
    return _ctxScopeDisplayLabel(a).localeCompare(_ctxScopeDisplayLabel(b));
  });
  return [...cwd, ...rest];
}

function _ctxPortalRenderRows() {
  const rowsEl = document.getElementById('ctx-projects-rows');
  if (!rowsEl) return;
  const scopes = _ctxPortalVisibleScopes();
  const hiddenUninit = _ctxPortalHiddenUninitCount();
  const hiddenHint = hiddenUninit
    ? `<div class="ctx-portal-hidden-hint text-muted">${escapeHtml(t('settings.ctx.portal_hidden_uninit', { count: hiddenUninit }))}</div>`
    : '';
  if (!scopes.length) {
    // Both predicates (search box + runtime-filter chip) can empty the board,
    // but state for them is split across ``_ctxPortalSearch`` and
    // ``_ctxPortalRuntimeFilter``. Name the real cause instead of always
    // interpolating the (possibly blank) query — a blank ``{query}`` rendered
    // literal empty quotes and hid that a filter chip was the culprit (#1349).
    const query = _ctxPortalSearch.trim();
    const runtime = _ctxPortalRuntimeFilter
      ? _ctxPortalRuntimeLabel(_ctxPortalRuntimeFilter)
      : '';
    let message;
    if (query && runtime) {
      message = t('settings.ctx.portal_no_match_filtered', { query, runtime });
    } else if (runtime) {
      message = t('settings.ctx.portal_no_runtime_match', { runtime });
    } else if (query) {
      message = t('settings.ctx.portal_no_match', { query });
    } else {
      message = t('settings.ctx.portal_no_projects');
    }
    rowsEl.innerHTML = hiddenHint + emptyState('', message, '');
    return;
  }
  rowsEl.innerHTML = hiddenHint + scopes.map(_ctxPortalRowHtml).join('');
  _ctxPortalWireRows(rowsEl);
  // The freshly-rendered ``.ctx-portal-sync`` buttons must pick up the tier
  // write-block state (``data-write-blocked`` + reason tooltip) on user /
  // project_local tiers. The portal didn't previously call this — the matrix
  // did, on the overview render path that no longer exists. Global from
  // context-gateway.js.
  _ctxRefreshWriteBlockedState();
  // A row may have just (re)entered edit mode — focus its input after paint.
  if (_ctxPortalEditingId !== null) {
    const input = rowsEl.querySelector('.ctx-portal-label-input');
    if (input) { input.focus(); input.select(); }
  }
}

function _ctxPortalCountsHtml(scope) {
  // ``counts: null`` means "not computed" (or a missing root) — render nothing
  // rather than a misleading row of zeros.
  if (!scope.counts) return '';
  // Failed count probe (#1692 PR 5): failed kinds ride as 0 in ``counts``, so
  // falling through would either show misleading zeros or — worse — the
  // zero-suppressed "Empty" pill, which is exactly the false-confidence bug
  // being fixed. Render one unavailable pill + Retry instead of any chips.
  if (_ctxPortalCountsUnavailable(scope)) {
    const tip = escapeHtml(t('settings.ctx.portal_counts_unavailable_tip', {
      kinds: scope.counts_unavailable.join(', '),
    }));
    const retryAria = escapeHtml(t('settings.ctx.portal_counts_retry_aria', {
      label: _ctxScopeDisplayLabel(scope),
    }));
    return `<div class="ctx-portal-counts">`
      + `<span class="ctx-portal-count ctx-portal-count--unavailable" title="${tip}">${escapeHtml(t('settings.ctx.portal_counts_unavailable'))}</span>`
      + `<button type="button" class="btn-ghost btn-xs ctx-portal-counts-retry" aria-label="${retryAria}">${escapeHtml(t('settings.ctx.retry'))}</button>`
      + `</div>`;
  }
  // Zero-suppress: four 0-chips are the dominant (and meaningless) visual
  // texture of the board, so collapse an all-zero inventory to one muted
  // "empty" pill. Non-empty scopes keep the per-type breakdown.
  const total = _CTX_PORTAL_COUNT_TYPES.reduce((sum, ct) => sum + (scope.counts[ct.key] || 0), 0);
  if (total === 0) {
    return `<div class="ctx-portal-counts"><span class="ctx-portal-count ctx-portal-count--empty">${escapeHtml(t('settings.ctx.portal_counts_empty'))}</span></div>`;
  }
  const chips = _CTX_PORTAL_COUNT_TYPES.map(ct => {
    const n = scope.counts[ct.key] || 0;
    const title = escapeHtml(t(ct.labelKey));
    return `<span class="ctx-portal-count" title="${title}">${ct.icon} ${n}</span>`;
  }).join('');
  return `<div class="ctx-portal-counts">${chips}</div>`;
}

function _ctxPortalBadgesHtml(scope) {
  // Self-contained (the shared _ctxScopeBadges has no stale badge yet). Inline
  // t() — no data-i18n, so the i18n DOM walker can't clobber the rendered text;
  // langchange repaints the whole board instead.
  const parts = [];
  if (scope.experimental) {
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--experimental">${escapeHtml(t('settings.ctx.scope_experimental'))}</span>`);
  }
  // Sync-paused is orthogonal to missing/stale (an enrolled project can be both
  // paused and stale), so it is its own check rather than part of the chain.
  if (_ctxPortalIsPaused(scope)) {
    const tip = escapeHtml(t('settings.ctx.portal_sync_paused_tip'));
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--paused" title="${tip}">${escapeHtml(t('settings.ctx.portal_sync_paused_badge'))}</span>`);
  }
  if (scope.missing) {
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--missing">${escapeHtml(t('settings.ctx.scope_missing'))}</span>`);
  } else if (scope.stale) {
    const tip = escapeHtml(t('settings.ctx.portal_stale_tip'));
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--stale" title="${tip}">${escapeHtml(t('settings.ctx.portal_stale'))}</span>`);
  }
  // Cross-project drift (#1649) — this scope's synced context no longer matches
  // the Store, per the deferred status-all fetch. Skipped for missing rows (no
  // synced context to drift). Populated only under project_shared; the map is
  // empty otherwise, so this renders nothing on other tiers.
  if (!scope.missing && _ctxPortalDriftMap[scope.scope_id || '']) {
    const tip = escapeHtml(t('settings.ctx.portal_drift_tip'));
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--drift" title="${tip}">${escapeHtml(t('settings.ctx.portal_drift_badge'))}</span>`);
  } else if (!scope.missing && _ctxPortalErrorMap[scope.scope_id || '']) {
    // Status check failed for this project (corrupt lockfile / probe raised):
    // drift is unknown, so we cannot claim clean OR drift. Deliberately no Sync
    // affordance in the tooltip — an error is not Sync-remediable; point at the
    // CLI for the failure detail instead. ``else if`` because a status-all entry
    // is exactly one status, so error and drift are mutually exclusive.
    const tip = escapeHtml(t('settings.ctx.portal_status_error_tip'));
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--status-error" title="${tip}">${escapeHtml(t('settings.ctx.portal_status_error_badge'))}</span>`);
  }
  return parts.join('');
}

// The label cell: either a static label + badges, or — when this row is the one
// being renamed — an inline input + Save/Cancel. The edit form is part of the
// declarative render, so a fresh paint always rebinds its listeners and only
// ``_ctxPortalEditingId`` can have one open.
function _ctxPortalLabelCellHtml(scope, editing) {
  if (editing) {
    const current = escapeHtml(_ctxScopeDisplayLabel(scope));
    const ph = escapeHtml(t('settings.ctx.portal_label_placeholder'));
    return `<span class="ctx-portal-label" data-editing="true">
      <input type="text" class="ctx-portal-label-input" value="${current}" placeholder="${ph}" aria-label="${ph}">
      <button type="button" class="btn-ghost btn-xs ctx-portal-label-save">${escapeHtml(t('settings.ctx.portal_save'))}</button>
      <button type="button" class="btn-ghost btn-xs ctx-portal-label-cancel">${escapeHtml(t('settings.ctx.portal_cancel'))}</button>
    </span>`;
  }
  return `<span class="ctx-portal-label">${escapeHtml(_ctxScopeDisplayLabel(scope))}</span>
      ${_ctxPortalBadgesHtml(scope)}`;
}

function _ctxPortalRowHtml(scope) {
  const sid = escapeHtml(scope.scope_id || '');
  const active = _ctxScopeIsActive(scope);
  const managed = _ctxPortalIsManaged(scope);
  const editing = managed && scope.scope_id === _ctxPortalEditingId;
  // ``--missing`` greys out unusable rows (the "(missing)" badge is the
  // non-color cue + a dashed left border in CSS); stale rows stay legible with
  // the stale badge + hint.
  const classes = ['ctx-portal-row'];
  if (active) classes.push('ctx-portal-row--active');
  if (scope.missing) classes.push('ctx-portal-row--missing');
  const rootTitle = scope.root ? ` title="${escapeHtml(scope.root)}"` : '';
  const rootText = scope.root
    ? escapeHtml(scope.root)
    : escapeHtml(t('settings.ctx.portal_root_unknown'));

  // Sibling <button> pairs — never a role=button row wrapping real buttons
  // (#1003 nested-interactive + Enter double-fire). The row is a plain div with
  // no row-level keydown. Actions are suppressed while this row is being edited
  // (the Save/Cancel pair lives in the label cell).
  const actions = [];
  if (!editing) {
    if (active) {
      actions.push(`<span class="ctx-portal-active-badge">${escapeHtml(t('settings.ctx.portal_active'))}</span>`);
    } else if (!scope.missing) {
      const useAria = escapeHtml(t('settings.ctx.portal_use_aria').replace('{label}', _ctxScopeDisplayLabel(scope)));
      actions.push(`<button type="button" class="btn-ghost btn-xs ctx-portal-use" data-scope-id="${sid}" aria-label="${useAria}">${escapeHtml(t('settings.ctx.portal_use'))}</button>`);
    }
    // Per-project Sync — fan out THIS row's scope to its runtimes (the lone
    // action carried over from the removed Overview matrix). Rendered for EVERY
    // scope, including Server CWD (whose effective id collapses to '' in
    // ``_ctxSyncProjectScope``), gated on sync-eligibility exactly as the matrix
    // was: disabled with a reason tooltip for project_local / missing / paused /
    // not-enrolled. Reuses the shared ``matrix_sync_*`` tooltip keys (also read
    // by ``settings-hooks-watchdog.js`` — do NOT rename). ``data-i18n-title``
    // (not bare ``title``) so the tier write-block sweep restores the reason.
    // Sync syncs the row only — it never changes the active project (that's Use).
    const isProjectLocal = _ctxTargetScope === 'project_local';
    let syncDisabled = '';
    if (isProjectLocal || scope.missing) {
      const k = 'settings.ctx.matrix_sync_disabled_title';
      syncDisabled = ` disabled data-i18n-title="${k}" title="${escapeHtml(t(k))}"`;
    } else if (!_ctxScopeSyncEligible(scope)) {
      const k = _ctxScopeIsEnrolled(scope)
        ? 'settings.ctx.matrix_sync_paused_title'
        : 'settings.ctx.matrix_sync_not_enrolled_title';
      syncDisabled = ` disabled data-i18n-title="${k}" title="${escapeHtml(t(k))}"`;
    }
    const syncAria = escapeHtml(t('settings.ctx.portal_sync_aria').replace('{label}', _ctxScopeDisplayLabel(scope)));
    actions.push(`<button type="button" class="btn-primary btn-xs ctx-portal-sync" data-scope-id="${sid}" aria-label="${syncAria}"${syncDisabled}>${escapeHtml(t('settings.ctx.sync'))}</button>`);
    // Enroll: a discovered-but-not-enrolled project joins sync (POST). Mutually
    // exclusive with the managed block below (``canEnroll`` ⇔ not enrolled,
    // ``managed`` ⇔ enrolled), so a row shows Enroll OR the Pause/Rename/Remove
    // trio, never both.
    if (_ctxPortalCanEnroll(scope)) {
      const enrollAria = escapeHtml(t('settings.ctx.portal_enroll_aria').replace('{label}', _ctxScopeDisplayLabel(scope)));
      const enrollTip = escapeHtml(t('settings.ctx.portal_enroll_tip'));
      actions.push(`<button type="button" class="btn-ghost btn-xs ctx-portal-enroll" data-scope-id="${sid}" aria-label="${enrollAria}" title="${enrollTip}">${escapeHtml(t('settings.ctx.portal_enroll'))}</button>`);
    }
    if (managed) {
      // Pause / Resume sync (PATCH ``enabled``). A paused project shows Resume;
      // an active one shows Pause. The handler re-derives the target state from
      // the scope, so the button only carries its scope-id.
      const paused = _ctxPortalIsPaused(scope);
      const toggleKey = paused ? 'settings.ctx.portal_resume_sync' : 'settings.ctx.portal_pause_sync';
      const toggleAriaKey = paused ? 'settings.ctx.portal_resume_sync_aria' : 'settings.ctx.portal_pause_sync_aria';
      const toggleAria = escapeHtml(t(toggleAriaKey).replace('{label}', _ctxScopeDisplayLabel(scope)));
      actions.push(`<button type="button" class="btn-ghost btn-xs ctx-portal-toggle-sync" data-scope-id="${sid}" aria-label="${toggleAria}">${escapeHtml(t(toggleKey))}</button>`);
      const renameAria = escapeHtml(t('settings.ctx.portal_rename_aria').replace('{label}', _ctxScopeDisplayLabel(scope)));
      actions.push(`<button type="button" class="btn-ghost btn-xs ctx-portal-rename" data-scope-id="${sid}" aria-label="${renameAria}">${escapeHtml(t('settings.ctx.portal_rename'))}</button>`);
      const removeAria = escapeHtml(t('settings.ctx.remove_project_aria').replace('{label}', _ctxScopeDisplayLabel(scope)).replace('{root}', scope.root || scope.scope_id));
      actions.push(`<button type="button" class="btn-ghost btn-xs ctx-portal-remove" data-scope-id="${sid}" aria-label="${removeAria}" title="${removeAria}">${escapeHtml(t('settings.ctx.remove'))}</button>`);
    }
  }

  const trafficLights = _ctxPortalRowTrafficLightsHtml(scope);

  return `<div class="${classes.join(' ')}" data-scope-id="${sid}">
    <div class="ctx-portal-row-main">
      <div class="ctx-portal-row-head">
        ${_ctxPortalLabelCellHtml(scope, editing)}
        ${trafficLights}
      </div>
      <div class="ctx-portal-root"${rootTitle}>${rootText}</div>
      ${_ctxPortalCountsHtml(scope)}
    </div>
    <div class="ctx-portal-row-actions">${actions.join('')}</div>
  </div>`;
}

function _ctxPortalScopeById(scopeId) {
  const all = Array.isArray(_ctxPortalScopes) ? _ctxPortalScopes : [];
  return all.find(s => (s.scope_id || '') === scopeId) || null;
}

function _ctxPortalWireRows(rowsEl) {
  rowsEl.querySelectorAll('.ctx-portal-use').forEach(btn => {
    btn.addEventListener('click', () => _ctxPortalSetActive(btn.dataset.scopeId || ''));
  });
  rowsEl.querySelectorAll('.ctx-portal-sync').forEach(btn => {
    // Row-only sync: pass THIS row's scope_id (Server CWD's collapses to '' in
    // ``_ctxEffectiveScopeId``). Must NOT touch ``_ctxActiveScopeId`` — Sync is
    // not a selection change (only ``.ctx-portal-use`` is). ``_ctxSyncProjectScope``
    // is a global from context-gateway.js (loaded before this file).
    btn.addEventListener('click', () => _ctxSyncProjectScope(btn.dataset.scopeId || '', btn));
  });
  rowsEl.querySelectorAll('.ctx-portal-rename').forEach(btn => {
    btn.addEventListener('click', () => {
      // Declarative: name the editing row and repaint. Any previously-open
      // editor closes (single _ctxPortalEditingId), so two rows can't edit at
      // once and no listeners are left dangling.
      _ctxPortalEditingId = btn.dataset.scopeId || '';
      _ctxPortalRenderRows();
    });
  });
  rowsEl.querySelectorAll('.ctx-portal-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const scope = _ctxPortalScopeById(btn.dataset.scopeId || '');
      if (scope) _ctxPortalUnregister(scope);
    });
  });
  rowsEl.querySelectorAll('.ctx-portal-enroll').forEach(btn => {
    btn.addEventListener('click', () => {
      const scope = _ctxPortalScopeById(btn.dataset.scopeId || '');
      if (scope) _ctxPortalEnroll(scope);
    });
  });
  rowsEl.querySelectorAll('.ctx-portal-toggle-sync').forEach(btn => {
    btn.addEventListener('click', () => {
      const scope = _ctxPortalScopeById(btn.dataset.scopeId || '');
      if (scope) _ctxPortalToggleEnabled(scope);
    });
  });
  rowsEl.querySelectorAll('.ctx-portal-counts-retry').forEach(btn => {
    // Whole-roster refetch, same as the registry banner's Retry — one
    // round-trip re-probes every row and the seq guard handles supersede.
    btn.addEventListener('click', () => { loadCtxProjects(); });
  });

  // Wire the single open editor (if any).
  const editor = rowsEl.querySelector('.ctx-portal-label[data-editing="true"]');
  if (editor) {
    const scope = _ctxPortalScopeById(_ctxPortalEditingId || '');
    const input = editor.querySelector('.ctx-portal-label-input');
    const save = editor.querySelector('.ctx-portal-label-save');
    const cancel = editor.querySelector('.ctx-portal-label-cancel');
    const commit = () => { if (scope) _ctxPortalSaveLabel(scope, input ? input.value : ''); };
    const close = () => { _ctxPortalEditingId = null; _ctxPortalRenderRows(); };
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        else if (e.key === 'Escape') { e.preventDefault(); close(); }
      });
    }
    if (save) save.addEventListener('click', commit);
    if (cancel) cancel.addEventListener('click', close);
  }
}

// Mirror _ctxWireProjectControls' active-switch: set the shared active id,
// re-normalize against the cache, bump detail seqs so open per-type panels
// refetch, clear any deep-link, persist. Then repaint the board highlight.
function _ctxPortalSetActive(scopeId) {
  if (scopeId === _ctxActiveScopeId) return;
  _ctxActiveScopeId = scopeId;
  _ctxNormalizeActiveScope(_ctxProjectsCache);
  _ctxBumpActiveScopeDetailSeq();
  try { localStorage.setItem(_CTX_ACTIVE_SCOPE_KEY, _ctxActiveScopeId); } catch {}
  if (typeof _ctxClearDeepLink === 'function') _ctxClearDeepLink();
  _ctxPortalRuntimeFilter = null; // Clear runtime filter on active project switch
  _ctxPortalRenderHeadingChips();
  _ctxPortalRenderRows();
}

async function _ctxPortalSaveLabel(scope, rawValue) {
  const value = (rawValue || '').trim();
  try {
    const csrf = await ensureCsrfToken();
    const r = await fetch(`/api/context/known-projects/${encodeURIComponent(scope.scope_id)}`, {
      method: 'PATCH',
      headers: csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: value }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
      return;
    }
    showToast(t('settings.ctx.portal_rename_success'));
    // Refetch so the renamed label (and any precedence fallback to basename on
    // a cleared label) re-derives from the server; loadCtxProjects clears the
    // edit state and repaints.
    _ctxPortalEditingId = null;
    await loadCtxProjects();
  } catch (err) {
    showToast(t('toast.request_failed', { error: err.message }), 'error');
  }
}

async function _ctxPortalUnregister(scope) {
  const ok = await showConfirm({
    title: t('settings.ctx.remove_project'),
    message: t('settings.ctx.confirm_remove_project')
      .replace('{label}', scope.label)
      .replace('{root}', scope.root || scope.scope_id),
    confirmText: t('settings.ctx.remove'),
  });
  if (!ok) return;
  try {
    const csrf = await ensureCsrfToken();
    const r = await fetch(`/api/context/known-projects/${encodeURIComponent(scope.scope_id)}`, {
      method: 'DELETE',
      headers: csrf ? { 'X-Memtomem-CSRF': csrf } : {},
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
      return;
    }
    await loadCtxProjects();
  } catch (err) {
    showToast(t('toast.delete_failed', { error: err.message }), 'error');
  }
}

// Enroll a discovered-but-not-enrolled project (POST known-projects) so it can
// participate in sync. The auto-display filter only surfaces marker-bearing
// roots, so the no-runtime-marker warning path is unreachable here — but the
// POST returns 200 even with a warning, so an ``r.ok`` success is correct
// regardless. A refetch flips the row to its enrolled (managed) state.
async function _ctxPortalEnroll(scope) {
  if (!scope.root) return; // _ctxPortalCanEnroll already excludes missing roots
  try {
    const csrf = await ensureCsrfToken();
    const r = await fetch('/api/context/known-projects', {
      method: 'POST',
      headers: csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: scope.root }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
      return;
    }
    showToast(t('settings.ctx.portal_enroll_success'));
    await loadCtxProjects();
  } catch (err) {
    showToast(t('toast.request_failed', { error: err.message }), 'error');
  }
}

// Pause / resume sync for an enrolled project (PATCH ``enabled``). The target
// state is the inverse of the current one, re-derived from the scope so the
// button is stateless: a paused scope resumes (enabled:true), an active one
// pauses (enabled:false). A refetch re-renders the badge + toggle label.
async function _ctxPortalToggleEnabled(scope) {
  const newEnabled = _ctxPortalIsPaused(scope);
  try {
    const csrf = await ensureCsrfToken();
    const r = await fetch(`/api/context/known-projects/${encodeURIComponent(scope.scope_id)}`, {
      method: 'PATCH',
      headers: csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: newEnabled }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
      return;
    }
    showToast(t(newEnabled ? 'settings.ctx.portal_resume_success' : 'settings.ctx.portal_pause_success'));
    await loadCtxProjects();
  } catch (err) {
    showToast(t('toast.request_failed', { error: err.message }), 'error');
  }
}

// Refresh button in the Portal header — forces a re-fetch (``_ctxFetchProjects``
// never caches, so this always hits the server) and repaints. Mirrors the
// overview's ``ctx-refresh-btn``.
document.getElementById('ctx-projects-refresh-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-projects-refresh-btn');
  btnLoading(btn, true);
  try {
    // Toast only when the load actually completed: a concurrent tier flip / a
    // second refresh aborts this one, which now returns false (#1286) — a
    // "Refresh complete" toast then would be false while the winning request is
    // still in flight (Codex review).
    if (await loadCtxProjects()) showToast(t('toast.refresh_complete'));
  } finally {
    btnLoading(btn, false);
  }
});

// Locale flip: the board renders via inline t() (no data-i18n on dynamic text),
// so repaint it when its section is the active sub-pane. Repaint from the
// snapshot (locale-only change — no fetch, no spinner flash); cold-mount fetches
// when nothing has loaded yet. ``_ctxProjectsSeq`` makes a rapid EN→KO→EN burst
// safe.
window.addEventListener('langchange', () => {
  const section = document.getElementById('settings-ctx-projects');
  if (!section || !section.classList.contains('active')) return;
  const gatewayTab = document.getElementById('tab-context-gateway');
  if (gatewayTab && !gatewayTab.classList.contains('active')) return;
  if (Array.isArray(_ctxPortalScopes) && _ctxPortalScopes.length) {
    const listEl = document.getElementById('ctx-projects-list');
    if (listEl) {
      _ctxPortalRenderHeadingChips();
      _ctxPortalRenderScaffold(listEl);
      _ctxPortalRenderRows();
    }
  } else {
    loadCtxProjects();
  }
});

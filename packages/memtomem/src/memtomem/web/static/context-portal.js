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
// Active runtime filter: null | 'claude' | 'antigravity' | 'codex' | 'kimi'
let _ctxPortalRuntimeFilter = null;

// In-scope provider clients (ADR-0021 §B), in display order. Antigravity is the
// gemini-family client and keeps its own label (RUNTIME_TO_CLIENT: gemini→antigravity).
const _CTX_PORTAL_RUNTIME_CLIENTS = ['claude', 'antigravity', 'codex', 'kimi'];

// Display label for a provider client. Proper-noun product names are identical
// across locales (matches scope_experimental), so this is intentionally not i18n.
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

    // Code-fence header labels are format/context names kept literal (JSON / TOML
    // are proper nouns; "Terminal" is a deferred i18n nit). The commands inside
    // are copied verbatim from docs/guides/mcp-clients.md (the registration SoT).
    let guideHtml = '';
    if (runtimeName === 'claude') {
      guideHtml = `
        <p class="guide-text">${escapeHtml(t('settings.ctx.guide_claude_desc'))}</p>
        <div class="guide-code-block">
          <div class="guide-code-header">
            <span>Terminal</span>
            <button type="button" class="btn-ghost btn-xs copy-code-btn">${escapeHtml(t('settings.ctx.copy'))}</button>
          </div>
          <pre class="guide-code"><code>claude mcp add memtomem -- uvx --from memtomem memtomem-server</code></pre>
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
      "args": ["--from", "memtomem", "memtomem-server"]
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
args = ["--from", "memtomem", "memtomem-server"]</code></pre>
        </div>
      `;
    } else if (runtimeName === 'kimi') {
      guideHtml = `
        <p class="guide-text">${escapeHtml(t('settings.ctx.guide_kimi_desc'))}</p>
        <div class="guide-code-block">
          <div class="guide-code-header">
            <span>Terminal</span>
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

  const runtimes = _ctxPortalRuntimesMap[_ctxActiveScopeId] || _ctxPortalRuntimesMap[''] || [];

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
      <div class="ctx-portal-runtimes-list">${runtimeChips}</div>
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
  const requestedScope = _ctxTargetScope;
  const listEl = document.getElementById('ctx-projects-list');
  if (!listEl) return;
  panelLoading(listEl);
  
  _ctxPortalSyncFilterFromDeepLink();
  
  try {
    // Fetch under the pinned tier, then commit the shared cache ONLY after the
    // guard passes — a superseded in-flight fetch must not clobber the shared
    // ``_ctxProjectsCache`` / active scope (#1194). The board already renders
    // from its own post-guard ``_ctxPortalScopes`` snapshot; this extends the
    // same discipline to the shared cache the helper used to commit pre-guard.
    const result = await _ctxFetchProjectsData({ targetScope: requestedScope });
    // Bail if a newer load started OR the tier flipped under us mid-fetch
    // (#972): counts in this payload were computed for ``requestedScope``.
    if (seq !== _ctxProjectsSeq || requestedScope !== _ctxTargetScope) return;
    const data = _ctxCommitProjects(result);
    const scopes = data.scopes || [];
    // Reset edit state on a fresh load and commit the validated snapshot before
    // any render reads it.
    _ctxPortalEditingId = null;
    _ctxPortalScopes = scopes;
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
      return;
    }

    // Now fetch runtimes for each scope in parallel to build the per-CLI traffic-lights (row UI)
    const runtimePromises = scopes.map(async (scope) => {
      if (scope.missing) {
        return { scopeId: scope.scope_id, runtimes: [] };
      }
      try {
        const url = _ctxWithTargetScope('/api/context/runtimes', { scopeId: scope.scope_id });
        const res = await fetch(url);
        if (!res.ok) throw new Error();
        const rData = await res.json();
        return { scopeId: scope.scope_id, runtimes: rData.runtimes || [] };
      } catch (err) {
        return { scopeId: scope.scope_id, runtimes: [] };
      }
    });

    const runtimesResults = await Promise.all(runtimePromises);
    if (seq !== _ctxProjectsSeq || requestedScope !== _ctxTargetScope) return;

    _ctxPortalRuntimesMap = {};
    for (const result of runtimesResults) {
      _ctxPortalRuntimesMap[result.scopeId] = result.runtimes;
    }

    _ctxPortalRenderHeadingChips();
    _ctxPortalRenderScaffold(listEl);
    _ctxPortalRenderRows();
  } catch (err) {
    if (seq !== _ctxProjectsSeq) return;
    // Route through the shared load-error helper so the failure is announced
    // (``role="alert"``) and retryable, matching the empty-roster path above.
    _ctxScopesLoadError(
      listEl, t('settings.ctx.portal_load_failed'), err.message || '',
      () => loadCtxProjects(),
    );
  }
}

// Header (search + sort) is painted once and kept across row repaints so the
// search field never loses focus mid-type; only #ctx-projects-rows is rebuilt
// on search/sort input.
function _ctxPortalRenderScaffold(listEl) {
  const sortName = escapeHtml(t('settings.ctx.portal_sort_name'));
  const sortItems = escapeHtml(t('settings.ctx.portal_sort_items'));
  listEl.innerHTML = `
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

  // Apply provider client-side filter
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
    rowsEl.innerHTML = hiddenHint + emptyState(
      '',
      t('settings.ctx.portal_no_match', { query: _ctxPortalSearch.trim() }),
      '',
    );
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
    await loadCtxProjects();
    showToast(t('toast.refresh_complete'));
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

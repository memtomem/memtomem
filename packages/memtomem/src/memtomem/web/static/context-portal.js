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
 *                       _ctxBumpActiveScopeDetailSeq, _ctxClearDeepLink
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

  container.innerHTML = `
    <div class="ctx-portal-runtimes-row">
      <span class="ctx-portal-heading-label">${escapeHtml(t('settings.ctx.runtimes_label'))}</span>
      <div class="ctx-portal-runtimes-list">${runtimeChips}</div>
    </div>
    <div class="ctx-portal-filter-row">
      <span class="ctx-portal-heading-label" id="ctx-portal-filter-label">${escapeHtml(t('settings.ctx.filter_label'))}</span>
      <div class="ctx-portal-filter-group" role="group" aria-labelledby="ctx-portal-filter-label">${filterGroup}</div>
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

// A registered (non-server-cwd) scope is the only kind that can be renamed or
// unregistered — the implicit Server-CWD row has no known_projects entry.
function _ctxPortalIsManaged(scope) {
  return !!scope && !_ctxScopeIsServerCwd(scope);
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
      // Server CWD is always present, so this is defensive only. Clear the
      // heading chip strip too, so a stale strip can't outlive an emptied list.
      const headingEl = document.getElementById('ctx-portal-heading-chips');
      if (headingEl) headingEl.innerHTML = '';
      listEl.innerHTML = emptyState('', t('settings.ctx.no_project_scopes'), '');
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
    listEl.innerHTML = emptyState('⚠', t('settings.ctx.portal_load_failed'), err.message || '');
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
}

function _ctxPortalVisibleScopes() {
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
  if (!scopes.length) {
    rowsEl.innerHTML = emptyState(
      '',
      t('settings.ctx.portal_no_match', { query: _ctxPortalSearch.trim() }),
      '',
    );
    return;
  }
  rowsEl.innerHTML = scopes.map(_ctxPortalRowHtml).join('');
  _ctxPortalWireRows(rowsEl);
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
    if (managed) {
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
      showToast(err.detail || t('toast.request_failed'), 'error');
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
      showToast(err.detail || t('toast.request_failed'), 'error');
      return;
    }
    await loadCtxProjects();
  } catch (err) {
    showToast(t('toast.delete_failed', { error: err.message }), 'error');
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

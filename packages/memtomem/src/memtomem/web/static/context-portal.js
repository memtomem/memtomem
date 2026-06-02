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
  try {
    const data = await _ctxFetchProjects();
    // Bail if a newer load started OR the tier flipped under us mid-fetch
    // (#972): counts in this payload were computed for ``requestedScope``.
    if (seq !== _ctxProjectsSeq || requestedScope !== _ctxTargetScope) return;
    const scopes = data.scopes || [];
    // Reset edit state on a fresh load and commit the validated snapshot before
    // any render reads it.
    _ctxPortalEditingId = null;
    _ctxPortalScopes = scopes;
    if (!scopes.length) {
      // Server CWD is always present, so this is defensive only.
      listEl.innerHTML = emptyState('', t('settings.ctx.no_project_scopes'), '');
      return;
    }
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
  const matched = q
    ? all.filter(s => {
        const label = (_ctxScopeDisplayLabel(s) || '').toLowerCase();
        const root = (s.root || '').toLowerCase();
        return label.includes(q) || root.includes(q);
      })
    : all.slice();
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

  return `<div class="${classes.join(' ')}" data-scope-id="${sid}">
    <div class="ctx-portal-row-main">
      <div class="ctx-portal-row-head">
        ${_ctxPortalLabelCellHtml(scope, editing)}
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
      _ctxPortalRenderScaffold(listEl);
      _ctxPortalRenderRows();
    }
  } else {
    loadCtxProjects();
  }
});

/**
 * Context Gateway — Skills / Commands / Agents CRUD, diff, sync, import.
 *
 * Depends on globals from app.js: qs, escapeHtml, t, showConfirm, showToast,
 * panelLoading, btnLoading, emptyState, diffLines, renderDiff,
 * switchSettingsSection.  Loaded AFTER app.js in index.html.
 */

// -- Status helpers -----------------------------------------------------------

const _ctxStatusCls = {
  'in sync':           'ctx-runtime-badge--sync',
  'out of sync':       'ctx-runtime-badge--warn',
  'missing target':    'ctx-runtime-badge--missing',
  // Runtime-only items (canonical absent) are a normal pre-import state, not
  // an error — the same red treatment as `parse error` over-signaled it.
  'missing canonical': 'ctx-runtime-badge--pending',
  'parse error':       'ctx-runtime-badge--error',
};
const _ctxStatusLabel = {
  'in sync':           'settings.ctx.status_in_sync',
  'out of sync':       'settings.ctx.status_out_of_sync',
  'missing target':    'settings.ctx.status_missing_target',
  'missing canonical': 'settings.ctx.status_missing_canonical',
};

// Settings overview badge i18n map. Keys are the wire status values that
// ``context_overview`` (web/routes/context_gateway.py) emits for the
// ``settings`` slot — derived from ``diff_settings`` and collapsed to
// ``in_sync`` / ``out_of_sync`` / ``error``. Unknown statuses fall through
// to the legacy ``replace('_', ' ')`` path so a future status string still
// renders something readable instead of an empty badge.
const _SETTINGS_STATUS_I18N = {
  in_sync:      'settings.hooks.badge_in_sync',
  out_of_sync:  'settings.hooks.badge_out_of_sync',
  error:        'settings.hooks.badge_error',
};

// Localized status text for a wire status value. Falls back to the raw
// status string when no i18n key is mapped — keeps unknown/future statuses
// visible instead of silently rendering an empty label.
function _ctxStatusText(status) {
  return t(_ctxStatusLabel[status] || '', status);
}

function _ctxBadge(status) {
  const cls = _ctxStatusCls[status] || 'ctx-runtime-badge--missing';
  return `<span class="ctx-runtime-badge ${cls}">${escapeHtml(_ctxStatusText(status))}</span>`;
}

function renderRuntimeBadges(runtimes) {
  if (!runtimes || !runtimes.length) return '';
  return '<div class="ctx-runtime-badges">' +
    runtimes.map(r => {
      const short = r.runtime.replace(/_skills|_commands|_agents/g, '');
      return `<span class="ctx-runtime-badge ${_ctxStatusCls[r.status] || ''}" title="${escapeHtml(r.runtime)}">${escapeHtml(short)}: ${escapeHtml(_ctxStatusText(r.status))}</span>`;
    }).join('') + '</div>';
}

function renderDroppedChips(fields) {
  if (!fields || !fields.length) return '';
  return fields.map(f => `<span class="ctx-dropped-chip">${escapeHtml(t('settings.ctx.dropped_fields'))}: ${escapeHtml(f)}</span>`).join('');
}

function renderImportResult(data) {
  let html = `<div class="ctx-import-result">`;
  html += `<div class="ctx-import-priority">${t('settings.ctx.import_priority')}</div>`;
  if (data.imported && data.imported.length) {
    html += `<h4>${t('settings.ctx.import_success')}</h4>`;
    for (const item of data.imported) {
      html += `<div class="ctx-import-item"><span class="badge badge-success">${escapeHtml(item.name)}</span></div>`;
    }
  }
  if (data.skipped && data.skipped.length) {
    html += `<h4 style="margin-top:8px">Skipped</h4>`;
    for (const item of data.skipped) {
      html += `<div class="ctx-import-item">${escapeHtml(item.name)} <span class="badge badge-warning">${escapeHtml(item.reason)}</span></div>`;
    }
  }
  if (!data.imported?.length && !data.skipped?.length) {
    html += `<div class="text-muted">${t('settings.ctx.no_artifacts_hint')}</div>`;
  }
  html += '</div>';
  return html;
}

// -- Overview -----------------------------------------------------------------

async function loadCtxOverview() {
  const el = qs('ctx-overview-content');
  panelLoading(el);
  try {
    const res = await fetch('/api/context/overview');
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to load overview');
    const data = await res.json();

    const types = [
      { key: 'skills',   label: t('settings.ctx.skills_title'),   section: 'ctx-skills' },
      { key: 'commands', label: t('settings.ctx.commands_title'), section: 'ctx-commands' },
      { key: 'agents',   label: t('settings.ctx.agents_title'),   section: 'ctx-agents' },
      { key: 'settings', label: t('settings.hooks.title'),        section: 'hooks-sync' },
    ];

    let html = '<div class="ctx-overview-grid">';
    for (const typ of types) {
      const d = data[typ.key] || {};
      const total = d.total || 0;
      const inSync = d.in_sync || 0;
      // ``/api/context/overview`` aggregates ``(runtime, name, status)`` triples.
      // ``total`` is the count of distinct names; status counts are per
      // ``(runtime, name)`` pair, so when one artifact is tracked under
      // multiple runtimes the per-status counts can sum above ``total``.
      // Concrete: ``commands: {total: 3, in_sync: 3, missing_target: 3}``
      // means 3 commands all in sync for one runtime AND all missing on
      // another. ``inSync < total`` alone misses that case (#692). Treat
      // any non-``in_sync`` count as a real issue so multi-runtime
      // divergence doesn't hide behind a green ``3/3 synced`` badge.
      const missingTarget = d.missing_target || 0;
      const missingCanonical = d.missing_canonical || 0;
      const outOfSync = d.out_of_sync || 0;
      const parseError = d.parse_error || 0;
      const issueCount = missingTarget + missingCanonical + outOfSync + parseError;
      const hasIssue = d.error || issueCount > 0
        || d.status === 'out_of_sync' || d.status === 'error';
      const badgeCls = d.error ? 'badge-danger' : (hasIssue ? 'badge-warning' : 'badge-success');

      // Pick the most actionable status to surface in the badge. Order
      // matters: ``parse_error`` is a hard failure (file is malformed),
      // then unsynced-runtime states, then unimported-canonical, then
      // out-of-sync content. Falling through to the historical
      // ``{inSync}/{total} synced`` keeps the all-clear case unchanged.
      let badgeText;
      if (d.error) {
        badgeText = 'Error';
      } else if (typ.key === 'settings') {
        const key = _SETTINGS_STATUS_I18N[d.status];
        badgeText = key ? t(key) : (d.status || '').replace('_', ' ');
      } else if (parseError > 0) {
        badgeText = `${parseError} ${t('settings.ctx.badge_parse_error')}`;
      } else if (missingTarget > 0) {
        badgeText = `${missingTarget} ${t('settings.ctx.badge_missing_target')}`;
      } else if (missingCanonical > 0) {
        badgeText = `${missingCanonical} ${t('settings.ctx.badge_missing_canonical')}`;
      } else if (outOfSync > 0) {
        badgeText = `${outOfSync} ${t('settings.ctx.badge_out_of_sync')}`;
      } else {
        badgeText = `${inSync}/${total} ${t('settings.ctx.badge_synced')}`;
      }

      html += `<div class="ctx-overview-stat" data-section="${typ.section}">
        <div class="ctx-overview-count">${typ.key === 'settings' ? (d.status === 'in_sync' ? '\u2714' : '\u26A0') : total}</div>
        <div class="ctx-overview-label">${escapeHtml(typ.label)}</div>
        <div class="ctx-overview-badge"><span class="badge ${badgeCls}">${escapeHtml(badgeText)}</span></div>
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;

    // Gate the Sync All button: when every artifact type's items are
    // entirely runtime-only (no canonicals to fan out), Sync All resolves
    // to a series of `no_canonical_root` skips. Surface that pre-click
    // via a data attribute (CSS dims the button) plus a native ``title``
    // hover tooltip + ``aria-disabled`` so the user understands the
    // dimmed state without having to click first; the post-click toast
    // stays as a fallback for users who don't hover (mobile, keyboard).
    const syncAllBtn = document.getElementById('ctx-sync-all-btn');
    if (syncAllBtn) {
      const totals = ['skills', 'commands', 'agents'].reduce((acc, k) => {
        const d = data[k] || {};
        acc.total += d.total || 0;
        acc.runtimeOnly += d.missing_canonical || 0;
        return acc;
      }, { total: 0, runtimeOnly: 0 });
      if (totals.total > 0 && totals.runtimeOnly === totals.total) {
        syncAllBtn.dataset.runtimeOnly = 'true';
        syncAllBtn.title = t('settings.ctx.sync_all_disabled_tooltip');
        syncAllBtn.setAttribute('aria-disabled', 'true');
      } else {
        delete syncAllBtn.dataset.runtimeOnly;
        syncAllBtn.removeAttribute('title');
        syncAllBtn.removeAttribute('aria-disabled');
      }
    }

    // Click to navigate
    el.querySelectorAll('.ctx-overview-stat').forEach(card => {
      card.addEventListener('click', () => switchSettingsSection(card.dataset.section));
    });
  } catch (err) {
    el.innerHTML = emptyState('', 'Failed to load overview', err.message);
  }
}

// JS-owned ``title`` strings (as opposed to ``data-i18n-title`` which
// I18N.applyDOM handles automatically) need a langchange refresh so the
// hover tooltip stays in sync with the active locale. Only rewrites the
// attribute when the gate is still active — clearing it during normal
// state would erase any caller's title.
window.addEventListener('langchange', () => {
  const btn = document.getElementById('ctx-sync-all-btn');
  if (btn && btn.dataset.runtimeOnly === 'true') {
    btn.title = t('settings.ctx.sync_all_disabled_tooltip');
  }
});

// Sync All button
document.getElementById('ctx-sync-all-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-sync-all-btn');
  if (btn.dataset.runtimeOnly === 'true') {
    showToast(t('settings.ctx.sync_all_disabled_tooltip'),
      'info');
    return;
  }
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all'),
    message: t('settings.ctx.confirm_sync').replace('{type}', 'all'),
    confirmText: t('settings.ctx.sync'),
  });
  if (!ok) return;
  btnLoading(btn, true);
  try {
    const csrf = await ensureCsrfToken();
    const headers = csrf
      ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
      : { 'Content-Type': 'application/json' };
    const types = ['skills', 'commands', 'agents'];
    for (const typ of types) {
      const resp = await fetch(`/api/context/${typ}/sync`, { method: 'POST', headers });
      if (!resp.ok) throw new Error(`Sync ${typ} failed`);
    }
    // Settings hooks sync (additive merge) — appends memtomem-owned hook
    // entries to ~/.claude/settings.json without clobbering user-authored
    // entries. Promoted from dev-only via RFC #761 (ADR-0001 §5 criteria
    // + HTTP-layer test fixtures).
    const settingsResp = await fetch('/api/context/settings/sync', { method: 'POST', headers });
    if (!settingsResp.ok) throw new Error('Settings sync failed');
    showToast(t('settings.ctx.sync_success'));
    loadCtxOverview();
  } catch (err) {
    showToast(t('toast.sync_failed', { error: err.message }), 'error');
  } finally { btnLoading(btn, false); }
});

// Detect button
document.getElementById('ctx-detect-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-detect-btn');
  btnLoading(btn, true);
  try {
    await loadCtxOverview();
    showToast(t('toast.detection_complete'));
  } finally { btnLoading(btn, false); }
});

// -- List (Skills / Commands / Agents) ----------------------------------------

let _ctxCurrentDetail = { type: null, name: null };

// POSIX basename, JS-side. Used to keep absolute project_root paths out
// of the toast copy — the wire still carries the absolute path so the
// reverse-proxy / debug case stays self-describing.
function _ctxBasename(p) {
  if (!p) return '';
  return String(p).replace(/\/$/, '').split('/').pop() || String(p);
}

function _ctxScopeIsServerCwd(scope) {
  return scope && Array.isArray(scope.sources) && scope.sources.includes('server-cwd');
}

function _ctxScopeBadges(scope) {
  // Compact non-default-source flags rendered next to the scope label so the
  // user can tell at a glance why a scope appears (and whether it's missing).
  // Inline ``t()`` is sufficient — no ``data-i18n`` attribute, the i18n DOM
  // walker would otherwise re-translate and clobber the rendered text.
  const parts = [];
  if (scope.experimental) {
    const tip = t('settings.ctx.scope_experimental_tip');
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--experimental" title="${escapeHtml(tip)}">${escapeHtml(t('settings.ctx.scope_experimental'))}</span>`);
  }
  if (scope.missing) {
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--missing">${escapeHtml(t('settings.ctx.scope_missing'))}</span>`);
  }
  return parts.join('');
}

function _ctxScopeCount(scope, type) {
  return (scope.counts && scope.counts[type]) || 0;
}

function _ctxRenderItemsHtml(items, type, projectRoot, { clickable }) {
  if (!items.length) {
    const canonical = `.memtomem/${type}`;
    const hint = t('settings.ctx.empty_hint')
      .replace(/\{type\}/g, type)
      .replace('{canonical}', canonical)
      .replace('{scan_dirs}', '');
    return emptyState(
      '',
      t('settings.ctx.no_artifacts').replace('{type}', type),
      hint,
    );
  }
  const cardClass = clickable ? 'ctx-card' : 'ctx-card ctx-card--readonly';
  let html = '';
  for (const item of items) {
    // ``data-canonical-path`` is read by the click handler to choose between
    // the canonical detail GET (which 404s for runtime-only items, since the
    // wire endpoint only resolves canonical paths) and the runtime-only diff
    // path. Empty string when the item is runtime-only — readers test for
    // truthiness so the absence/empty distinction is irrelevant.
    const canonAttr = item.canonical_path
      ? ` data-canonical-path="${escapeHtml(item.canonical_path)}"`
      : ' data-canonical-path=""';
    // ``data-out-of-sync`` lets the list-click handler hint to
    // ``loadCtxDetail`` that the user should land on the Diff tab —
    // otherwise the canonical pane is the default and the user has
    // to click Diff to discover *what* is out of sync. Computed here
    // because the list response carries the per-runtime statuses;
    // ``loadCtxDetail`` would otherwise need a second fetch.
    const outOfSync = (item.runtimes || []).some(r => r.status === 'out of sync');
    html += `<div class="${cardClass}" data-name="${escapeHtml(item.name)}"${canonAttr} data-out-of-sync="${outOfSync}">
      <div class="ctx-card-header">
        <div>
          <div class="ctx-card-name">${escapeHtml(item.name)}</div>
          ${item.canonical_path ? `<div class="ctx-card-path">${escapeHtml(item.canonical_path)}</div>` : '<div class="ctx-card-path text-muted">(runtime only)</div>'}
        </div>
        ${renderRuntimeBadges(item.runtimes)}
      </div>
    </div>`;
  }
  return html;
}

async function _loadScopeGroupItems(type, scope, container) {
  panelLoading(container);
  try {
    const params = _ctxScopeIsServerCwd(scope) ? '' : `?scope_id=${encodeURIComponent(scope.scope_id)}`;
    const res = await fetch(`/api/context/${type}${params}`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Failed to load ${type}`);
    const data = await res.json();
    const items = data[type] || [];
    container.innerHTML = _ctxRenderItemsHtml(items, type, scope.root, {
      clickable: _ctxScopeIsServerCwd(scope),
    });

    if (_ctxScopeIsServerCwd(scope)) {
      // Only the cwd is mutable, so its canonical/runtime split drives the
      // section-level Sync vs Import affordance gating. Expose the count via
      // a data attribute so CSS can flip primary/disabled states without a
      // classList toggle that risks drift across re-renders.
      _ctxRefreshSectionState(type, items, data.scanned_dirs || []);

      const listEl = qs(`ctx-${type}-list`);
      container.querySelectorAll('.ctx-card').forEach(card => {
        card.addEventListener('click', () => {
          listEl.querySelectorAll('.ctx-card').forEach(c => c.classList.remove('active'));
          card.classList.add('active');
          // Runtime-only items have no canonical file; calling the GET detail
          // endpoint returns 404. Branch into the diff-backed renderer so the
          // user sees the actual runtime contents instead of a "not found".
          if (card.dataset.canonicalPath) {
            loadCtxDetail(type, card.dataset.name, {
              autoOpenDiff: card.dataset.outOfSync === 'true',
            });
          } else {
            const detailEl = qs(`ctx-${type}-detail`);
            _ctxLoadRuntimeOnlyDetail(type, card.dataset.name, detailEl);
          }
        });
      });
    }
  } catch (err) {
    container.innerHTML = emptyState('', 'Failed to load ' + type, err.message);
  }
}

// Reflect the cwd canonical/runtime split onto the section so CSS can gate
// the primary action. Also (re)renders the runtime-only banner above the
// scope groups when items exist but none are canonical — the user landing
// on a fresh project shouldn't have to infer that Import is the next step.
function _ctxRefreshSectionState(type, cwdItems, scannedDirs) {
  const canonicalCount = cwdItems.filter(i => i.canonical_path).length;
  const sectionEl = document.getElementById(`settings-ctx-${type}`);
  if (sectionEl) sectionEl.dataset.canonicalCount = String(canonicalCount);

  const listEl = qs(`ctx-${type}-list`);
  if (!listEl) return;
  const existing = listEl.querySelector('.ctx-runtime-only-banner');
  if (existing) existing.remove();
  if (canonicalCount === 0 && cwdItems.length > 0) {
    const scanList = (scannedDirs || []).join(', ') || `.${type}/`;
    const msg = t('settings.ctx.runtime_only_banner')
      .replace('{count}', cwdItems.length)
      .replace(/\{type\}/g, type)
      .replace('{scan_dirs}', scanList);
    const banner = document.createElement('div');
    banner.className = 'ctx-runtime-only-banner';
    banner.textContent = msg;
    listEl.insertBefore(banner, listEl.firstChild);
  }
}

async function loadCtxList(type) {
  const listEl = qs(`ctx-${type}-list`);
  const detailEl = qs(`ctx-${type}-detail`);
  const statusEl = qs(`ctx-${type}-status`);
  if (detailEl) { detailEl.hidden = true; detailEl.innerHTML = ''; }
  if (statusEl) statusEl.innerHTML = '';
  panelLoading(listEl);
  _ctxCurrentDetail = { type: null, name: null };
  // Clear stale gating attribute so a failed reload doesn't keep the buttons
  // pinned to a previous canonical-count state. _ctxRefreshSectionState resets
  // it when the cwd group resolves successfully.
  const sectionEl = document.getElementById(`settings-ctx-${type}`);
  if (sectionEl) delete sectionEl.dataset.canonicalCount;

  try {
    const res = await fetch('/api/context/projects');
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to load projects');
    const data = await res.json();
    const scopes = data.scopes || [];
    if (!scopes.length) {
      // Should never happen — server cwd always present — but render
      // something instead of leaving the panel blank.
      listEl.innerHTML = emptyState('', 'No project scopes', '');
      return;
    }

    let html = '';
    for (const scope of scopes) {
      const isCwd = _ctxScopeIsServerCwd(scope);
      const count = _ctxScopeCount(scope, type);
      const groupId = `ctx-${type}-group-${escapeHtml(scope.scope_id)}`;
      const removable = !isCwd;
      const removeBtn = removable
        ? `<button class="ctx-scope-remove" data-scope-id="${escapeHtml(scope.scope_id)}" title="${escapeHtml(t('settings.ctx.remove_project'))}">×</button>`
        : '';
      // Full root path on the summary's title attribute lets the user
      // disambiguate same-name scopes (``Edu/inflearn`` vs ``Work/inflearn``)
      // on hover without inflating the visible label.
      const rootTitle = scope.root ? `title="${escapeHtml(scope.root)}"` : '';
      html += `<details class="ctx-scope-group" data-scope-id="${escapeHtml(scope.scope_id)}" data-tier="${escapeHtml(scope.tier)}"${isCwd ? ' open' : ''}>
        <summary class="ctx-scope-summary" ${rootTitle}>
          <span class="ctx-scope-summary-label">${escapeHtml(scope.label)}</span>
          <span class="ctx-scope-summary-count">${count}</span>
          ${_ctxScopeBadges(scope)}
          ${removeBtn}
        </summary>
        <div class="ctx-scope-items" id="${groupId}" data-loaded="false"></div>
      </details>`;
    }
    listEl.innerHTML = html;

    // Wire up: lazy fetch on toggle, immediate fetch for the open cwd group,
    // and the per-scope remove (×) button.
    for (const scope of scopes) {
      const groupEl = listEl.querySelector(`details[data-scope-id="${CSS.escape(scope.scope_id)}"]`);
      if (!groupEl) continue;
      const itemsEl = groupEl.querySelector('.ctx-scope-items');
      const fetchOnce = () => {
        if (itemsEl.dataset.loaded === 'true') return;
        itemsEl.dataset.loaded = 'true';
        _loadScopeGroupItems(type, scope, itemsEl);
      };
      if (groupEl.open) fetchOnce();
      groupEl.addEventListener('toggle', () => { if (groupEl.open) fetchOnce(); });

      const removeBtn = groupEl.querySelector('.ctx-scope-remove');
      if (removeBtn) {
        removeBtn.addEventListener('click', async (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          const ok = await showConfirm({
            title: t('settings.ctx.remove_project'),
            message: t('settings.ctx.confirm_remove_project')
              .replace('{label}', scope.label),
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
            loadCtxList(type);
          } catch (err) {
            showToast(t('toast.delete_failed', { error: err.message }), 'error');
          }
        });
      }
    }
  } catch (err) {
    listEl.innerHTML = emptyState('', 'Failed to load ' + type, err.message);
  }
}

// -- Detail -------------------------------------------------------------------

async function loadCtxDetail(type, name, opts = {}) {
  // ``opts.autoOpenDiff`` (default false): when the list-click handler
  // sees an "out of sync" runtime on the card, it passes ``true`` here
  // so the detail view lands on the Diff tab pre-fetched, instead of
  // forcing the user to discover what's drifted by clicking Diff
  // themselves. Other call paths (post-save / post-delete reload at
  // line ~575/588) leave it false to preserve their canonical-pane
  // default.
  const autoOpenDiff = opts.autoOpenDiff === true;
  const detailEl = qs(`ctx-${type}-detail`);
  detailEl.hidden = false;
  _ctxCurrentDetail = { type, name };
  panelLoading(detailEl);

  try {
    const res = await fetch(`/api/context/${type}/${encodeURIComponent(name)}`);
    if (res.status === 404) {
      detailEl.innerHTML = emptyState('', `"${name}" not found`, t('settings.ctx.no_artifacts_hint'));
      return;
    }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Failed to load ${name}`);
    const data = await res.json();

    let html = '<div class="ctx-detail">';
    html += `<div class="ctx-detail-header">
      <strong>${escapeHtml(name)}</strong>
      <div style="display:flex;gap:6px">
        <button class="btn-ghost ctx-detail-edit-btn" data-i18n="settings.ctx.edit">${t('settings.ctx.edit')}</button>
        <button class="btn-ghost ctx-detail-diff-btn" data-i18n="settings.ctx.diff_view">${t('settings.ctx.diff_view')}</button>
        <button class="btn-ghost btn-danger ctx-detail-delete-btn" data-i18n="settings.ctx.delete">${t('settings.ctx.delete')}</button>
      </div>
    </div>`;

    html += '<div class="ctx-detail-tabs">';
    html += `<div class="ctx-detail-tab active" data-pane="canonical">${t('settings.ctx.canonical_source')}</div>`;
    html += `<div class="ctx-detail-tab" data-pane="diff">${t('settings.ctx.diff_view')}</div>`;
    html += '</div>';

    html += '<div class="ctx-detail-pane active" id="ctx-pane-canonical">';
    html += `<pre class="ctx-content-pre">${escapeHtml(data.content || '')}</pre>`;
    if (data.files && data.files.length) {
      html += `<div style="margin-top:8px"><strong>${t('settings.ctx.auxiliary_files')}</strong>`;
      for (const f of data.files) {
        html += `<div class="text-muted" style="font-size:0.78rem">${escapeHtml(f.path)} (${f.size} bytes)</div>`;
      }
      html += '</div>';
    }
    html += '</div>';

    html += '<div class="ctx-detail-pane" id="ctx-pane-diff"><div class="text-muted">Click Diff tab to load...</div></div>';

    html += `<div id="ctx-pane-edit" hidden>
      <textarea class="ctx-edit-area" id="ctx-edit-content">${escapeHtml(data.content || '')}</textarea>
      <div class="ctx-edit-actions">
        <button class="btn-ghost ctx-edit-cancel">${t('settings.ctx.cancel')}</button>
        <button class="btn-primary ctx-edit-save">${t('settings.ctx.save')}</button>
      </div>
    </div>`;

    html += '</div>';
    detailEl.innerHTML = html;
    // mtime_ns is a string (JS Number can't safely represent ns epochs).
    detailEl.dataset.mtimeNs = data.mtime_ns || '';

    // Tab switching
    detailEl.querySelectorAll('.ctx-detail-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        detailEl.querySelectorAll('.ctx-detail-tab').forEach(t => t.classList.remove('active'));
        detailEl.querySelectorAll('.ctx-detail-pane').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        const pane = detailEl.querySelector(`#ctx-pane-${tab.dataset.pane}`);
        if (pane) pane.classList.add('active');
        if (tab.dataset.pane === 'diff') _ctxLoadDiff(type, name, detailEl);
      });
    });

    // Out-of-sync prefetch + tab activation. Uses a synthetic ``click()``
    // on the Diff tab so the same handler above runs — keeps the active
    // class + pane wiring in one place. ``click()`` is sync but the
    // delegated ``_ctxLoadDiff`` is async; that's fine, we don't await.
    if (autoOpenDiff) {
      const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
      if (diffTab) diffTab.click();
    }

    // Edit
    detailEl.querySelector('.ctx-detail-edit-btn')?.addEventListener('click', () => {
      const canonPane = detailEl.querySelector('#ctx-pane-canonical');
      const editPane = detailEl.querySelector('#ctx-pane-edit');
      if (canonPane) canonPane.hidden = true;
      if (editPane) editPane.hidden = false;
      detailEl.querySelectorAll('.ctx-detail-tab').forEach(t => t.style.display = 'none');
    });

    // Cancel edit
    detailEl.querySelector('.ctx-edit-cancel')?.addEventListener('click', () => {
      const canonPane = detailEl.querySelector('#ctx-pane-canonical');
      const editPane = detailEl.querySelector('#ctx-pane-edit');
      if (canonPane) canonPane.hidden = false;
      if (editPane) editPane.hidden = true;
      detailEl.querySelectorAll('.ctx-detail-tab').forEach(t => t.style.display = '');
    });

    // Save
    detailEl.querySelector('.ctx-edit-save')?.addEventListener('click', async () => {
      const btn = detailEl.querySelector('.ctx-edit-save');
      const content = detailEl.querySelector('#ctx-edit-content').value;
      const mtime_ns = detailEl.dataset.mtimeNs || '';
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const r = await fetch(`/api/context/${type}/${encodeURIComponent(name)}`, {
          method: 'PUT',
          headers,
          body: JSON.stringify({ content, mtime_ns }),
        });
        if (r.status === 409) {
          showToast(t('settings.ctx.mtime_conflict'), 'warning');
          loadCtxDetail(type, name);
          return;
        }
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        const result = await r.json();
        if (result.name) {
          showToast(t('settings.ctx.save_success').replace('{name}', name));
          detailEl.dataset.mtimeNs = result.mtime_ns || '';
          loadCtxDetail(type, name);
        }
      } catch (err) {
        showToast(t('toast.save_failed', { error: err.message }), 'error');
      } finally { btnLoading(btn, false); }
    });

    // Diff button
    detailEl.querySelector('.ctx-detail-diff-btn')?.addEventListener('click', () => {
      const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
      if (diffTab) diffTab.click();
    });

    // Delete
    detailEl.querySelector('.ctx-detail-delete-btn')?.addEventListener('click', async () => {
      // Cascade is opt-in: the canonical artifact is the ``.memtomem/``
      // entry, runtime files are mirrored copies. Default-off keeps the
      // dialog conservative — a stray click only removes the canonical,
      // and the user has to consciously check the box to fan-out delete
      // into ``~/.claude/skills/``, ``~/.codex/...``, etc.
      const result = await showConfirm({
        title: t('settings.ctx.confirm_delete').replace('{name}', name),
        message: t('settings.ctx.confirm_delete_msg'),
        confirmText: t('settings.ctx.delete'),
        extraOption: {
          id: 'cascade',
          label: t('settings.ctx.cascade_delete'),
          defaultChecked: false,
        },
      });
      if (!result || !result.ok) return;
      const cascade = !!(result.extras && result.extras.cascade);
      try {
        const csrf = await ensureCsrfToken();
        const r = await fetch(
          `/api/context/${type}/${encodeURIComponent(name)}?cascade=${cascade}`,
          { method: 'DELETE', headers: csrf ? { 'X-Memtomem-CSRF': csrf } : {} },
        );
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        const data = await r.json();
        if (data.deleted) {
          showToast(t('settings.ctx.delete_success').replace('{name}', name));
          detailEl.hidden = true;
          loadCtxList(type);
        }
      } catch (err) {
        showToast(t('toast.delete_failed', { error: err.message }), 'error');
      }
    });

  } catch (err) {
    detailEl.innerHTML = emptyState('', 'Failed to load detail', err.message);
  }
}

// Surfaces (agents, commands) whose ``/rendered`` endpoint emits a
// ``field_map`` matrix. Skills don't produce per-runtime field drops,
// so a matrix would be uniformly green and not worth the row.
const _CTX_FIELD_MAP_TYPES = new Set(['agents', 'commands']);

function _ctxRenderFieldMapHtml(fieldMap, runtimes) {
  // ``fieldMap[field][runtime] = bool`` (True = kept). ``runtimes`` is the
  // ordered list of runtime keys from the same response so column order
  // matches the runtime sections rendered below.
  if (!fieldMap || !runtimes || !runtimes.length) return '';
  const fields = Object.keys(fieldMap);
  if (!fields.length) return '';
  const heading = escapeHtml(t('settings.ctx.field_map'));
  let html = `<table class="ctx-field-map" aria-label="${heading}">`;
  html += `<thead><tr><th scope="col">${heading}</th>`;
  for (const rt of runtimes) {
    html += `<th scope="col">${escapeHtml(rt)}</th>`;
  }
  html += '</tr></thead><tbody>';
  for (const f of fields) {
    html += `<tr><th scope="row">${escapeHtml(f)}</th>`;
    for (const rt of runtimes) {
      const kept = !!(fieldMap[f] && fieldMap[f][rt]);
      // ✓ for kept, em-dash for dropped — high-contrast, locale-stable
      // (no translation needed; the matrix headers carry the labels).
      html += `<td>${kept ? '✓' : '—'}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  return html;
}

async function _ctxFetchFieldMap(type, name) {
  // Fail-soft: a missing/invalid /rendered response should not break the
  // diff pane. The diff fetch is the user-facing source of truth here;
  // the field map is supplementary.
  try {
    const res = await fetch(`/api/context/${type}/${encodeURIComponent(name)}/rendered`);
    if (!res.ok) return null;
    const data = await res.json();
    if (!data || !data.field_map) return null;
    const runtimes = (data.runtimes || []).map(rt => rt.runtime);
    return { fieldMap: data.field_map, runtimes };
  } catch (_err) {
    return null;
  }
}

async function _ctxLoadDiff(type, name, detailEl) {
  const pane = detailEl.querySelector('#ctx-pane-diff');
  if (!pane) return;
  pane.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    // Diff is required, field map is optional + parallel-fetched. ``Promise.all``
    // would fail the whole pane on a /rendered hiccup; the explicit
    // fail-soft inside ``_ctxFetchFieldMap`` is what we want here.
    const diffPromise = fetch(`/api/context/${type}/${encodeURIComponent(name)}/diff`);
    const fieldMapPromise = _CTX_FIELD_MAP_TYPES.has(type)
      ? _ctxFetchFieldMap(type, name)
      : Promise.resolve(null);
    const res = await diffPromise;
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Diff failed');
    const data = await res.json();
    const fieldMapData = await fieldMapPromise;

    let html = '';
    if (fieldMapData) {
      html += _ctxRenderFieldMapHtml(fieldMapData.fieldMap, fieldMapData.runtimes);
    }
    if (!data.runtimes || !data.runtimes.length) {
      html += '<div class="text-muted">No runtime targets found.</div>';
    } else {
      for (const rt of data.runtimes) {
        html += `<div style="margin-bottom:12px">`;
        html += `<strong>${escapeHtml(rt.runtime)}</strong> ${_ctxBadge(rt.status)}`;
        if (rt.dropped_fields && rt.dropped_fields.length) {
          html += `<div style="margin-top:4px">${renderDroppedChips(rt.dropped_fields)}</div>`;
        }
        if (rt.status === 'out of sync' && data.canonical_content != null && rt.runtime_content != null) {
          const ops = diffLines(data.canonical_content, rt.runtime_content);
          html += `<div class="diff-view" style="margin-top:6px">${renderDiff(ops)}</div>`;
        } else if (rt.runtime_content) {
          html += `<pre class="ctx-content-pre" style="margin-top:6px">${escapeHtml(rt.runtime_content)}</pre>`;
        }
        html += '</div>';
      }
    }
    pane.innerHTML = html;
  } catch (err) {
    pane.innerHTML = `<div class="text-muted">Diff failed: ${escapeHtml(err.message)}</div>`;
  }
}

// Render a detail panel for runtime-only items (no canonical file yet). The
// canonical detail GET 404s for these by design; the diff endpoint already
// returns ``runtime_content`` for each runtime, so we reuse it as the
// preview source and surface an "Import all" CTA so the user can pull every
// runtime-only artifact in one click.
async function _ctxLoadRuntimeOnlyDetail(type, name, detailEl) {
  detailEl.hidden = false;
  _ctxCurrentDetail = { type, name };
  panelLoading(detailEl);

  try {
    const res = await fetch(`/api/context/${type}/${encodeURIComponent(name)}/diff`);
    if (!res.ok) {
      throw new Error((await res.json().catch(() => ({}))).detail || `Failed to load ${name}`);
    }
    const data = await res.json();

    let html = '<div class="ctx-detail">';
    html += `<div class="ctx-detail-header">
      <strong>${escapeHtml(name)}</strong>
      ${_ctxBadge('missing canonical')}
    </div>`;
    html += `<div class="text-muted" style="margin:6px 0 12px">${t('settings.ctx.runtime_only_detail_hint')}</div>`;

    if (!data.runtimes || !data.runtimes.length) {
      html += `<div class="text-muted">${t('settings.ctx.no_artifacts_hint')}</div>`;
    } else {
      for (const rt of data.runtimes) {
        html += `<div style="margin-bottom:12px">`;
        html += `<strong>${escapeHtml(rt.runtime)}</strong> ${_ctxBadge(rt.status)}`;
        if (rt.runtime_content != null) {
          html += `<pre class="ctx-content-pre" style="margin-top:6px">${escapeHtml(rt.runtime_content)}</pre>`;
        }
        html += '</div>';
      }
    }

    // ``type`` is always the plural section name (skills/commands/agents);
    // strip the trailing ``s`` for the singular CTA copy. Both i18n strings
    // expose ``{type}`` so the same placeholder works across en/ko.
    const singular = type.endsWith('s') ? type.slice(0, -1) : type;
    html += `<div class="ctx-edit-actions" style="margin-top:12px">
      <button class="btn-primary ctx-runtime-only-import" data-type="${escapeHtml(type)}">
        ${t('settings.ctx.import_this').replace('{type}', singular)}
      </button>
    </div>`;

    html += '</div>';
    detailEl.innerHTML = html;

    detailEl.querySelector('.ctx-runtime-only-import')?.addEventListener('click', async () => {
      const btn = detailEl.querySelector('.ctx-runtime-only-import');
      btnLoading(btn, true);
      try {
        const r = await fetch(`/api/context/${type}/${encodeURIComponent(name)}/import`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        const data = await r.json();
        if (data.imported && data.imported.length) {
          showToast(t('settings.ctx.import_success'));
        } else if (data.skipped && data.skipped.length) {
          // ``reason`` is backend English (e.g. "canonical exists"); the user
          // still needs to know the import didn't run, so we surface it as-is
          // rather than swallow it. Localizing every backend skip reason
          // would multiply i18n keys without changing behavior.
          showToast(data.skipped[0].reason || t('toast.request_failed'), 'warning');
        }
        loadCtxList(type);
      } catch (err) {
        showToast(t('toast.import_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    });
  } catch (err) {
    detailEl.innerHTML = emptyState('', 'Failed to load detail', err.message);
  }
}

// -- Sync / Import buttons (delegated) ----------------------------------------

document.querySelectorAll('.ctx-sync-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const type = btn.dataset.type;
    // Guard against pressing Sync when the cwd has no canonical artifacts —
    // the request would resolve to a `no_canonical_root` skip with an info
    // toast, but that arrives after a confirm dialog, which is the wrong
    // shape of feedback for "this button does nothing right now."
    const section = btn.closest('.settings-section');
    if (section?.dataset.canonicalCount === '0') {
      showToast(t('settings.ctx.sync_disabled_tooltip').replace('{type}', type),
        'info');
      return;
    }
    const ok = await showConfirm({
      title: t('settings.ctx.sync'),
      message: t('settings.ctx.confirm_sync').replace('{type}', type),
      confirmText: t('settings.ctx.sync'),
    });
    if (!ok) return;
    btnLoading(btn, true);
    try {
      const csrf = await ensureCsrfToken();
      const headers = csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' };
      const r = await fetch(`/api/context/${type}/sync`, { method: 'POST', headers });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(err.detail || t('toast.request_failed'), 'error');
        return;
      }
      const data = await r.json();
      const generated = data.generated || [];
      const dropped = data.dropped || [];
      const skipped = data.skipped || [];
      const emptyCanonical = generated.length === 0
        && skipped.some(s => s && s.reason_code === 'no_canonical_root');
      if (emptyCanonical) {
        const msg = t('settings.ctx.sync_empty_canonical')
          .replace('{type}', type)
          .replace('{canonical}', data.canonical_root || `.memtomem/${type}`);
        showToast(msg, 'info');
      } else if (dropped.length) {
        // commands/agents render dropped per-field omissions — keep the
        // existing warning so the user can investigate field-level loss.
        showToast(t('settings.ctx.sync_dropped')
          .replace('{count}', dropped.length), 'warning');
      } else {
        showToast(t('settings.ctx.sync_success'));
      }
      loadCtxList(type);
    } catch (err) {
      showToast(t('toast.sync_failed', { error: err.message }), 'error');
    } finally { btnLoading(btn, false); }
  });
});

document.querySelectorAll('.ctx-import-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const type = btn.dataset.type;
    // Overwrite is opt-in: the default skip-when-canonical-exists rule
    // protects user-maintained canonicals from a stray Import wiping
    // them out with a stale runtime copy. The checkbox lets the user
    // explicitly say "yes, the runtime is the source of truth this
    // round" — used after editing in-place in ``~/.claude/skills/``
    // and wanting to flow that back into ``.memtomem/``.
    const result = await showConfirm({
      title: t('settings.ctx.import'),
      message: t('settings.ctx.confirm_import').replace('{type}', type),
      confirmText: t('settings.ctx.import'),
      extraOption: {
        id: 'overwrite',
        label: t('settings.ctx.confirm_import_overwrite_label'),
        defaultChecked: false,
      },
    });
    if (!result || !result.ok) return;
    const overwrite = !!(result.extras && result.extras.overwrite);
    btnLoading(btn, true);
    try {
      const r = await fetch(`/api/context/${type}/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ overwrite }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(err.detail || t('toast.request_failed'), 'error');
        return;
      }
      const data = await r.json();
      const statusEl = qs(`ctx-${type}-status`);
      if (statusEl) statusEl.innerHTML = renderImportResult(data);
      const importedCount = data.imported?.length || 0;
      const skippedCount = data.skipped?.length || 0;
      if (importedCount === 0 && skippedCount === 0) {
        // Nothing in any scanned runtime dir — give the user the actual
        // paths we looked in so they can drop a SKILL.md / *.md / etc.
        // Render basename(project_root) so a long absolute path doesn't
        // crowd the toast; scanned_dirs already gives full orientation.
        const scanList = (data.scanned_dirs || []).join(', ') || '—';
        const rootLabel = _ctxBasename(data.project_root) || '.';
        const msg = t('settings.ctx.import_no_runtimes')
          .replace('{type}', type)
          .replace('{root}', rootLabel)
          .replace('{scan_dirs}', scanList);
        showToast(msg, 'info');
      } else if (importedCount + skippedCount > 0) {
        showToast(t('settings.ctx.import_result')
          .replace('{imported}', importedCount)
          .replace('{skipped}', skippedCount));
      } else {
        showToast(t('settings.ctx.import_success'));
      }
      loadCtxList(type);
    } catch (err) {
      showToast(t('toast.import_failed', { error: err.message }), 'error');
    } finally { btnLoading(btn, false); }
  });
});

// -- Create button (delegated) ------------------------------------------------

document.querySelectorAll('.ctx-create-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const type = btn.dataset.type;
    const listEl = qs(`ctx-${type}-list`);
    if (listEl.querySelector('.ctx-create-form')) return;
    const form = document.createElement('div');
    form.className = 'ctx-create-form';
    form.innerHTML = `
      <label>Name</label>
      <input type="text" class="ctx-create-name" placeholder="my-${type.slice(0, -1)}" style="width:100%" />
      <label style="margin-top:8px">Content</label>
      <textarea class="ctx-edit-area ctx-create-content" rows="6" placeholder="# ${type.slice(0, -1).charAt(0).toUpperCase() + type.slice(0, -1).slice(1)} content..."></textarea>
      <div class="ctx-edit-actions">
        <button class="btn-ghost ctx-create-cancel">${t('settings.ctx.cancel')}</button>
        <button class="btn-primary ctx-create-submit">${t('settings.ctx.create')}</button>
      </div>`;
    listEl.prepend(form);

    form.querySelector('.ctx-create-cancel').addEventListener('click', () => form.remove());
    form.querySelector('.ctx-create-submit').addEventListener('click', async () => {
      const nameInput = form.querySelector('.ctx-create-name').value.trim();
      const content = form.querySelector('.ctx-create-content').value;
      if (!nameInput) { showToast(t('toast.name_required'), 'error'); return; }
      const submitBtn = form.querySelector('.ctx-create-submit');
      btnLoading(submitBtn, true);
      try {
        const r = await fetch(`/api/context/${type}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nameInput, content }),
        });
        if (!r.ok) {
          const err = await r.json();
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        showToast(t('settings.ctx.create_success').replace('{name}', nameInput));
        form.remove();
        loadCtxList(type);
      } catch (err) {
        showToast(t('toast.create_failed', { error: err.message }), 'error');
      } finally { btnLoading(submitBtn, false); }
    });

    form.querySelector('.ctx-create-name').focus();
  });
});

// -- Add Project button (delegated) ------------------------------------------

document.querySelectorAll('.ctx-add-project-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const type = btn.dataset.type;
    // Defer to the shared folder picker (``path-picker.js``) instead of
    // ``window.prompt``: visual breadcrumb navigation, validation
    // against the server's ``/api/fs/list`` allow-list, and no
    // copy-paste path errors. ``PathPicker.open`` is async w.r.t.
    // the user; we hand it a callback that runs the POST when the
    // picker resolves a path. ``window.prompt`` fallback survives in
    // case ``path-picker.js`` failed to load (vendor cache miss, etc.)
    // — better than a non-functional Add Project button.
    const onSelect = async (root) => {
      if (!root) return;
      btnLoading(btn, true);
      try {
        const r = await fetch('/api/context/known-projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ root }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        const data = await r.json();
        if (data.warning) {
          showToast(data.warning, 'warning');
        } else {
          showToast(t('settings.ctx.add_project_success'), 'success');
        }
        loadCtxList(type);
      } catch (err) {
        showToast(t('toast.request_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    };
    if (window.PathPicker && typeof window.PathPicker.open === 'function') {
      window.PathPicker.open({ onSelect });
      return;
    }
    const raw = window.prompt(
      t('settings.ctx.add_project_prompt'),
      '',
    );
    if (!raw) return;
    const root = raw.trim();
    if (!root) return;
    onSelect(root);
  });
});

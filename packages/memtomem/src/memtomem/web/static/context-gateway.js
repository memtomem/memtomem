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
  'missing canonical': 'ctx-runtime-badge--error',
  'parse error':       'ctx-runtime-badge--error',
};
const _ctxStatusLabel = {
  'in sync':           'settings.ctx.status_in_sync',
  'out of sync':       'settings.ctx.status_out_of_sync',
  'missing target':    'settings.ctx.status_missing_target',
  'missing canonical': 'settings.ctx.status_missing_canonical',
};

function _ctxBadge(status) {
  const cls = _ctxStatusCls[status] || 'ctx-runtime-badge--missing';
  const label = t(_ctxStatusLabel[status] || '', status);
  return `<span class="ctx-runtime-badge ${cls}">${escapeHtml(label)}</span>`;
}

function renderRuntimeBadges(runtimes) {
  if (!runtimes || !runtimes.length) return '';
  return '<div class="ctx-runtime-badges">' +
    runtimes.map(r => {
      const short = r.runtime.replace(/_skills|_commands|_agents/g, '');
      return `<span class="ctx-runtime-badge ${_ctxStatusCls[r.status] || ''}" title="${escapeHtml(r.runtime)}">${escapeHtml(short)}: ${escapeHtml(r.status)}</span>`;
    }).join('') + '</div>';
}

function renderDroppedChips(fields) {
  if (!fields || !fields.length) return '';
  return fields.map(f => `<span class="ctx-dropped-chip">${escapeHtml(t('settings.ctx.dropped_fields', 'Dropped'))}: ${escapeHtml(f)}</span>`).join('');
}

function renderImportResult(data) {
  let html = `<div class="ctx-import-result">`;
  html += `<div class="ctx-import-priority">${t('settings.ctx.import_priority')}</div>`;
  if (data.imported && data.imported.length) {
    html += `<h4>${t('settings.ctx.import_success', 'Imported')}</h4>`;
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
      { key: 'skills',   label: t('settings.ctx.skills_title', 'Skills'),   section: 'ctx-skills' },
      { key: 'commands', label: t('settings.ctx.commands_title', 'Commands'), section: 'ctx-commands' },
      { key: 'agents',   label: t('settings.ctx.agents_title', 'Agents'),   section: 'ctx-agents' },
      { key: 'settings', label: t('settings.hooks.title', 'Settings'),      section: 'hooks-sync' },
    ];

    let html = '<div class="ctx-overview-grid">';
    for (const typ of types) {
      const d = data[typ.key] || {};
      const total = d.total || 0;
      const inSync = d.in_sync || 0;
      const hasIssue = d.error || (total > 0 && inSync < total) || d.status === 'out_of_sync' || d.status === 'error';
      const badgeCls = d.error ? 'badge-danger' : (hasIssue ? 'badge-warning' : 'badge-success');
      const badgeText = d.error ? 'Error' : (typ.key === 'settings' ? (d.status || '').replace('_', ' ') : `${inSync}/${total} synced`);

      html += `<div class="ctx-overview-stat" data-section="${typ.section}">
        <div class="ctx-overview-count">${typ.key === 'settings' ? (d.status === 'in_sync' ? '\u2714' : '\u26A0') : total}</div>
        <div class="ctx-overview-label">${escapeHtml(typ.label)}</div>
        <div class="ctx-overview-badge"><span class="badge ${badgeCls}">${escapeHtml(badgeText)}</span></div>
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;

    // Click to navigate
    el.querySelectorAll('.ctx-overview-stat').forEach(card => {
      card.addEventListener('click', () => switchSettingsSection(card.dataset.section));
    });
  } catch (err) {
    el.innerHTML = emptyState('', 'Failed to load overview', err.message);
  }
}

// Sync All button
document.getElementById('ctx-sync-all-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-sync-all-btn');
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all', 'Sync All'),
    message: t('settings.ctx.confirm_sync', 'Fan out all artifacts to runtimes?').replace('{type}', 'all'),
    confirmText: t('settings.ctx.sync', 'Sync'),
  });
  if (!ok) return;
  btnLoading(btn, true);
  try {
    const types = ['skills', 'commands', 'agents'];
    for (const typ of types) {
      const resp = await fetch(`/api/context/${typ}/sync`, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
      if (!resp.ok) throw new Error(`Sync ${typ} failed`);
    }
    // Settings hooks sync (additive merge)
    const settingsResp = await fetch('/api/context/settings/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    if (!settingsResp.ok) throw new Error('Settings sync failed');
    showToast(t('settings.ctx.sync_success', 'Sync completed'));
    loadCtxOverview();
  } catch (err) {
    showToast('Sync failed: ' + err.message, 'error');
  } finally { btnLoading(btn, false); }
});

// Detect button
document.getElementById('ctx-detect-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-detect-btn');
  btnLoading(btn, true);
  try {
    await loadCtxOverview();
    showToast('Detection complete');
  } finally { btnLoading(btn, false); }
});

// -- List (Skills / Commands / Agents) ----------------------------------------

let _ctxCurrentDetail = { type: null, name: null };

async function loadCtxList(type) {
  const listEl = qs(`ctx-${type}-list`);
  const detailEl = qs(`ctx-${type}-detail`);
  const statusEl = qs(`ctx-${type}-status`);
  if (detailEl) { detailEl.hidden = true; detailEl.innerHTML = ''; }
  if (statusEl) statusEl.innerHTML = '';
  panelLoading(listEl);
  _ctxCurrentDetail = { type: null, name: null };

  try {
    const res = await fetch(`/api/context/${type}`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Failed to load ${type}`);
    const data = await res.json();
    const items = data[type] || [];

    if (!items.length) {
      listEl.innerHTML = emptyState('', t('settings.ctx.no_artifacts', 'No {type} found').replace('{type}', type), t('settings.ctx.no_artifacts_hint'));
      return;
    }

    let html = '';
    for (const item of items) {
      html += `<div class="ctx-card" data-name="${escapeHtml(item.name)}">
        <div class="ctx-card-header">
          <div>
            <div class="ctx-card-name">${escapeHtml(item.name)}</div>
            ${item.canonical_path ? `<div class="ctx-card-path">${escapeHtml(item.canonical_path)}</div>` : '<div class="ctx-card-path text-muted">(runtime only)</div>'}
          </div>
          ${renderRuntimeBadges(item.runtimes)}
        </div>
      </div>`;
    }
    listEl.innerHTML = html;

    // Click cards to show detail
    listEl.querySelectorAll('.ctx-card').forEach(card => {
      card.addEventListener('click', () => {
        listEl.querySelectorAll('.ctx-card').forEach(c => c.classList.remove('active'));
        card.classList.add('active');
        loadCtxDetail(type, card.dataset.name);
      });
    });
  } catch (err) {
    listEl.innerHTML = emptyState('', 'Failed to load ' + type, err.message);
  }
}

// -- Detail -------------------------------------------------------------------

async function loadCtxDetail(type, name) {
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
        <button class="btn-ghost ctx-detail-edit-btn" data-i18n="settings.ctx.edit">${t('settings.ctx.edit', 'Edit')}</button>
        <button class="btn-ghost ctx-detail-diff-btn" data-i18n="settings.ctx.diff_view">${t('settings.ctx.diff_view', 'Diff')}</button>
        <button class="btn-ghost btn-danger ctx-detail-delete-btn" data-i18n="settings.ctx.delete">${t('settings.ctx.delete', 'Delete')}</button>
      </div>
    </div>`;

    html += '<div class="ctx-detail-tabs">';
    html += `<div class="ctx-detail-tab active" data-pane="canonical">${t('settings.ctx.canonical_source', 'Canonical')}</div>`;
    html += `<div class="ctx-detail-tab" data-pane="diff">${t('settings.ctx.diff_view', 'Diff')}</div>`;
    html += '</div>';

    html += '<div class="ctx-detail-pane active" id="ctx-pane-canonical">';
    html += `<pre class="ctx-content-pre">${escapeHtml(data.content || '')}</pre>`;
    if (data.files && data.files.length) {
      html += `<div style="margin-top:8px"><strong>${t('settings.ctx.auxiliary_files', 'Auxiliary files')}</strong>`;
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
        <button class="btn-ghost ctx-edit-cancel">${t('settings.ctx.cancel', 'Cancel')}</button>
        <button class="btn-primary ctx-edit-save">${t('settings.ctx.save', 'Save')}</button>
      </div>
    </div>`;

    html += '</div>';
    detailEl.innerHTML = html;
    detailEl.dataset.mtime = data.mtime || '';

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
      const mtime = parseFloat(detailEl.dataset.mtime) || 0;
      btnLoading(btn, true);
      try {
        const r = await fetch(`/api/context/${type}/${encodeURIComponent(name)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content, mtime }),
        });
        if (r.status === 409) {
          showToast(t('settings.ctx.mtime_conflict'), 'warning');
          loadCtxDetail(type, name);
          return;
        }
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || 'Save failed', 'error');
          return;
        }
        const result = await r.json();
        if (result.name) {
          showToast(t('settings.ctx.save_success', '"{name}" saved').replace('{name}', name));
          detailEl.dataset.mtime = result.mtime || '';
          loadCtxDetail(type, name);
        }
      } catch (err) {
        showToast('Save failed: ' + err.message, 'error');
      } finally { btnLoading(btn, false); }
    });

    // Diff button
    detailEl.querySelector('.ctx-detail-diff-btn')?.addEventListener('click', () => {
      const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
      if (diffTab) diffTab.click();
    });

    // Delete
    detailEl.querySelector('.ctx-detail-delete-btn')?.addEventListener('click', async () => {
      const ok = await showConfirm({
        title: t('settings.ctx.confirm_delete', 'Delete "{name}"?').replace('{name}', name),
        message: t('settings.ctx.confirm_delete_msg'),
        confirmText: t('settings.ctx.delete', 'Delete'),
      });
      if (!ok) return;
      try {
        const r = await fetch(`/api/context/${type}/${encodeURIComponent(name)}?cascade=false`, { method: 'DELETE' });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || 'Delete failed', 'error');
          return;
        }
        const result = await r.json();
        if (result.deleted) {
          showToast(t('settings.ctx.delete_success', '"{name}" deleted').replace('{name}', name));
          detailEl.hidden = true;
          loadCtxList(type);
        }
      } catch (err) {
        showToast('Delete failed: ' + err.message, 'error');
      }
    });

  } catch (err) {
    detailEl.innerHTML = emptyState('', 'Failed to load detail', err.message);
  }
}

async function _ctxLoadDiff(type, name, detailEl) {
  const pane = detailEl.querySelector('#ctx-pane-diff');
  if (!pane) return;
  pane.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    const res = await fetch(`/api/context/${type}/${encodeURIComponent(name)}/diff`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Diff failed');
    const data = await res.json();

    let html = '';
    if (!data.runtimes || !data.runtimes.length) {
      html = '<div class="text-muted">No runtime targets found.</div>';
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

// -- Sync / Import buttons (delegated) ----------------------------------------

document.querySelectorAll('.ctx-sync-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const type = btn.dataset.type;
    const ok = await showConfirm({
      title: t('settings.ctx.sync', 'Sync'),
      message: t('settings.ctx.confirm_sync', 'Fan out {type} to all runtimes?').replace('{type}', type),
      confirmText: t('settings.ctx.sync', 'Sync'),
    });
    if (!ok) return;
    btnLoading(btn, true);
    try {
      const r = await fetch(`/api/context/${type}/sync`, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(err.detail || 'Sync failed', 'error');
        return;
      }
      const data = await r.json();
      const dropped = data.dropped || [];
      if (dropped.length) {
        showToast(t('settings.ctx.sync_dropped', '{count} field(s) dropped').replace('{count}', dropped.length), 'warning');
      } else {
        showToast(t('settings.ctx.sync_success', 'Sync completed'));
      }
      loadCtxList(type);
    } catch (err) {
      showToast('Sync failed: ' + err.message, 'error');
    } finally { btnLoading(btn, false); }
  });
});

document.querySelectorAll('.ctx-import-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const type = btn.dataset.type;
    const ok = await showConfirm({
      title: t('settings.ctx.import', 'Import'),
      message: t('settings.ctx.confirm_import', 'Import {type} from runtimes?').replace('{type}', type),
      confirmText: t('settings.ctx.import', 'Import'),
    });
    if (!ok) return;
    btnLoading(btn, true);
    try {
      const r = await fetch(`/api/context/${type}/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(err.detail || 'Import failed', 'error');
        return;
      }
      const data = await r.json();
      const statusEl = qs(`ctx-${type}-status`);
      if (statusEl) statusEl.innerHTML = renderImportResult(data);
      const total = (data.imported?.length || 0) + (data.skipped?.length || 0);
      if (total > 0) {
        showToast(t('settings.ctx.import_result', '{imported} imported, {skipped} skipped')
          .replace('{imported}', data.imported?.length || 0)
          .replace('{skipped}', data.skipped?.length || 0));
      } else {
        showToast(t('settings.ctx.import_success', 'Import completed'));
      }
      loadCtxList(type);
    } catch (err) {
      showToast('Import failed: ' + err.message, 'error');
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
        <button class="btn-ghost ctx-create-cancel">${t('settings.ctx.cancel', 'Cancel')}</button>
        <button class="btn-primary ctx-create-submit">${t('settings.ctx.create', 'Create')}</button>
      </div>`;
    listEl.prepend(form);

    form.querySelector('.ctx-create-cancel').addEventListener('click', () => form.remove());
    form.querySelector('.ctx-create-submit').addEventListener('click', async () => {
      const nameInput = form.querySelector('.ctx-create-name').value.trim();
      const content = form.querySelector('.ctx-create-content').value;
      if (!nameInput) { showToast('Name is required', 'error'); return; }
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
          showToast(err.detail || 'Create failed', 'error');
          return;
        }
        showToast(t('settings.ctx.create_success', '"{name}" created').replace('{name}', nameInput));
        form.remove();
        loadCtxList(type);
      } catch (err) {
        showToast('Create failed: ' + err.message, 'error');
      } finally { btnLoading(submitBtn, false); }
    });

    form.querySelector('.ctx-create-name').focus();
  });
});

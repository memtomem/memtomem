/**
 * Health Watchdog panel — periodic check results and manual run.
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

// =====================================================================
// HEALTH WATCHDOG PANEL
// =====================================================================

function _wdDot(status) {
  const cls = status === 'ok' ? 'health-ok' : status === 'warning' ? 'health-slow' : 'health-down';
  return `<span class="health-dot ${cls}"></span>`;
}

function _wdLabel(status) {
  return status === 'ok' ? 'OK' : status === 'warning' ? 'Warning' : 'Critical';
}

// ── Hooks Sync ──
async function loadHooksSync() {
  const statusEl = qs('hooks-sync-status');
  const contentEl = qs('hooks-sync-content');
  panelLoading(contentEl);
  statusEl.innerHTML = '';

  try {
    const res = await fetch('/api/settings-sync');
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed: ${res.status}`);
    }
    const data = await res.json();

    // Status badge
    const badges = {
      in_sync: { cls: 'badge-success', text: t('settings.hooks.in_sync', 'All hooks are in sync') },
      out_of_sync: { cls: 'badge-warning', text: `${data.hooks?.pending?.length || 0} ${t('settings.hooks.pending', 'hooks will be added on sync')}` },
      conflicts: { cls: 'badge-danger', text: `${data.hooks?.conflicts?.length || 0} ${t('settings.hooks.conflicts', 'conflicts found')}` },
      no_source: { cls: 'badge-muted', text: t('settings.hooks.no_source', 'No .memtomem/settings.json found') },
      error: { cls: 'badge-danger', text: data.error || 'Error' },
    };
    const badge = badges[data.status] || badges.error;
    statusEl.innerHTML = `<span class="badge ${badge.cls}">${escapeHtml(badge.text)}</span>`
      + `<span class="text-muted" style="margin-left:0.5rem;font-size:0.85rem">${escapeHtml(data.target_path || '')}</span>`;

    if (data.status === 'no_source' || data.status === 'error') {
      contentEl.innerHTML = emptyState('', badge.text, data.status === 'no_source'
        ? 'Create .memtomem/settings.json or run mm init to set up hooks.'
        : 'Fix the JSON file and reload.');
      return;
    }

    let html = '';

    function _ruleLabel(item) {
      return item.matcher ? `${item.event}:${item.matcher}` : item.event;
    }

    // Conflicts
    if (data.hooks.conflicts.length) {
      html += '<h3 style="margin:1rem 0 0.5rem">Conflicts</h3>';
      for (const c of data.hooks.conflicts) {
        const label = _ruleLabel(c);
        const oldText = JSON.stringify(c.existing, null, 2);
        const newText = JSON.stringify(c.proposed, null, 2);
        const ops = diffLines(oldText, newText);
        html += `<div class="hooks-sync-card hooks-sync-conflict" data-event="${escapeHtml(c.event)}" data-matcher="${escapeHtml(c.matcher || '')}">
          <div class="hooks-sync-card-header">
            <strong>${escapeHtml(label)}</strong>
            <button class="btn-sm btn-primary hooks-resolve-btn"
              data-i18n="settings.hooks.use_proposed">${t('settings.hooks.use_proposed', "Use memtomem's")}</button>
          </div>
          <div class="diff-view">${renderDiff(ops)}</div>
        </div>`;
      }
    }

    // Pending
    if (data.hooks.pending.length) {
      html += '<h3 style="margin:1rem 0 0.5rem">Pending</h3>';
      for (const p of data.hooks.pending) {
        const label = _ruleLabel(p);
        html += `<div class="hooks-sync-card">
          <div class="hooks-sync-card-header"><strong>${escapeHtml(label)}</strong>
            <span class="badge badge-warning">will be added</span></div>
          <pre class="hooks-sync-preview">${escapeHtml(JSON.stringify(p.rule, null, 2))}</pre>
        </div>`;
      }
    }

    // Synced
    if (data.hooks.synced.length) {
      html += '<h3 style="margin:1rem 0 0.5rem">' + t('settings.hooks.synced', 'In sync') + '</h3>';
      html += '<div class="text-muted">';
      for (const s of data.hooks.synced) {
        html += `<div style="padding:0.25rem 0">${escapeHtml(_ruleLabel(s))}</div>`;
      }
      html += '</div>';
    }

    if (!html) {
      html = emptyState('', t('settings.hooks.in_sync', 'All hooks are in sync'), 'No hooks defined in .memtomem/settings.json.');
    }

    contentEl.innerHTML = html;

    // Resolve buttons
    contentEl.querySelectorAll('.hooks-resolve-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const card = btn.closest('.hooks-sync-card');
        const event = card.dataset.event;
        const matcher = card.dataset.matcher || '';
        const label = matcher ? `${event}:${matcher}` : event;
        const ok = await showConfirm({
          title: t('confirm.hooks_replace_title'),
          message: t('confirm.hooks_replace_msg', { label }),
          confirmText: t('common.replace'),
        });
        if (!ok) return;
        btnLoading(btn, true);
        try {
          const r = await fetch('/api/context/settings/resolve', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({event, matcher, action: 'use_proposed'}),
          });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            showToast(err.detail || t('toast.request_failed'), 'error');
            return;
          }
          const result = await r.json();
          if (result.status === 'ok') {
            showToast(result.reason);
            loadHooksSync();
          } else {
            showToast(result.reason || t('toast.unexpected_response'), 'error');
          }
        } finally { btnLoading(btn, false); }
      });
    });

  } catch (err) {
    contentEl.innerHTML = emptyState('', 'Failed to load sync status', err.message);
  }
}

// Sync Now button
document.getElementById('hooks-sync-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('hooks-sync-btn');
  const ok = await showConfirm({
    title: t('confirm.hooks_sync_title'),
    message: t('confirm.hooks_sync_msg'),
    confirmText: t('common.sync'),
  });
  if (!ok) return;
  btnLoading(btn, true);
  try {
    const res = await fetch('/api/settings-sync', {method: 'POST', headers: {'Content-Type': 'application/json'}});
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed: ${res.status}`);
    }
    const data = await res.json();
    const warnings = data.results?.flatMap(r => r.warnings || []) || [];
    if (warnings.length) {
      showToast(t('toast.hooks_warnings', { count: warnings.length }), 'warning');
    } else {
      showToast(t('settings.hooks.sync_success', 'Sync completed'));
    }
    loadHooksSync();
  } catch (err) {
    showToast(t('toast.sync_failed', { error: err.message }), 'error');
  } finally { btnLoading(btn, false); }
});

async function loadWatchdogStatus() {
  const report = qs('health-watchdog-report');
  const bar = qs('health-watchdog-status-bar');
  bar.style.display = 'none';
  report.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    const d = await api('GET', '/api/watchdog/status');
    if (!d.enabled) {
      report.innerHTML = '<div class="empty-state">Health watchdog is disabled.<br><code>MEMTOMEM_HEALTH_WATCHDOG__ENABLED=true</code></div>';
      _watchdogEnabled = false;
      return;
    }
    _watchdogEnabled = true;
    const checks = d.checks || {};
    const names = Object.keys(checks).sort();
    if (!names.length) {
      report.innerHTML = '<div class="empty-state">Watchdog is running but no checks recorded yet.</div>';
      return;
    }
    const criticals = names.filter(n => checks[n].status === 'critical').length;
    const warnings = names.filter(n => checks[n].status === 'warning').length;
    let summary;
    if (criticals > 0) summary = `<span class="health-dot health-down"></span> ${criticals} critical, ${warnings} warning`;
    else if (warnings > 0) summary = `<span class="health-dot health-slow"></span> ${warnings} warning`;
    else summary = `<span class="health-dot health-ok"></span> All checks OK`;

    report.innerHTML = `
      <div class="health-section" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;font-size:0.9rem">${summary} &mdash; ${names.length} checks</div>
      </div>
      <div class="health-grid">
        ${names.map(n => {
          const c = checks[n];
          const val = c.value || {};
          const detail = Object.entries(val).map(([k,v]) => `<span class="mono">${k}</span>: ${v}`).join(' &middot; ');
          return `<div class="health-card card">
            <div class="health-card-title" style="display:flex;align-items:center;gap:6px">${_wdDot(c.status)} ${n}</div>
            <div style="font-size:0.85rem;font-weight:600;margin:4px 0">${_wdLabel(c.status)}</div>
            <div class="health-card-detail">${detail || '—'}</div>
            <div class="health-card-detail" style="opacity:0.5">${c.tier}</div>
          </div>`;
        }).join('')}
      </div>
    `;
  } catch (e) {
    report.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

let _watchdogEnabled = false;

async function runWatchdogNow() {
  if (!_watchdogEnabled) {
    showToast(t('toast.watchdog_disabled'), 'error');
    return;
  }
  const bar = qs('health-watchdog-status-bar');
  const btn = qs('health-watchdog-run-btn');
  btn.disabled = true;
  btn.textContent = 'Running...';
  bar.style.display = 'none';
  try {
    await api('POST', '/api/watchdog/run');
    bar.className = 'status-msg ok';
    bar.textContent = 'Health checks completed.';
    bar.style.display = 'block';
    await loadWatchdogStatus();
  } catch (e) {
    bar.className = 'status-msg err';
    bar.textContent = 'Run failed: ' + e.message;
    bar.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run Now';
  }
}

qs('health-watchdog-refresh-btn')?.addEventListener('click', loadWatchdogStatus);
qs('health-watchdog-run-btn')?.addEventListener('click', runWatchdogNow);


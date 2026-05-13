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

// Flat registry of hook rules — entries are indexed by their position
// in the combined synced + pending lists rather than by
// ``event:matcher``. Claude Code allows multiple rules to share the
// same ``(event, matcher)`` pair (see ``settings_sync.py:128`` PR #844
// fix — the server preserves multiplicity), so an
// ``event:matcher``-keyed dict would silently collapse duplicates and
// both rows would resolve to the last rule's detail. The index-keyed
// shape is stable across re-renders within a single ``loadHooksSync``
// call and is the source of truth that the row's ``data-hook-key``
// references.
let _hooksRuleRegistry = [];
let _hooksSyncSeq = 0;

function _hooksCurrentTargetScope() {
  if (typeof _ctxTargetScope === 'string') return _ctxTargetScope;
  return 'project_shared';
}

function _hooksCurrentProjectScope() {
  if (typeof _ctxActiveScopeId === 'string') return _ctxActiveScopeId;
  return '';
}

function _hooksScopedUrl(path) {
  if (typeof _ctxWithTargetScope === 'function') {
    return _ctxWithTargetScope(path);
  }
  return path;
}

function _hooksProjectControlsHtml() {
  if (typeof _ctxProjectControls !== 'function') return '';
  return _ctxProjectControls('hooks-sync');
}

function _hooksWireProjectControls() {
  if (typeof _ctxWireProjectControls === 'function') {
    _ctxWireProjectControls();
  }
}

function _hooksTierControlsHtml() {
  if (typeof _ctxTierControls !== 'function') return '';
  return _ctxTierControls('hooks-sync');
}

function _hooksWireTierControls() {
  if (typeof _ctxWireTierControls === 'function') {
    _ctxWireTierControls();
  }
}

function _renderHookRuleDetail(key, contentEl) {
  const idx = Number(key);
  const entry = Number.isInteger(idx) ? _hooksRuleRegistry[idx] : undefined;
  const panel = contentEl.querySelector('#hooks-rule-detail');
  if (!panel || !entry) return;

  const rule = entry.rule || {};
  const label = entry.matcher ? `${entry.event}:${entry.matcher}` : entry.event;
  // Claude Code's rule format: top-level ``matcher`` + ``hooks`` array
  // of command entries, each with ``type`` / ``command`` / optional
  // ``timeout`` / etc. Render the union so the user can see exactly
  // what the hook will execute.
  const hooks = Array.isArray(rule.hooks) ? rule.hooks : [];

  function _row(label, value) {
    if (value === undefined || value === null || value === '') return '';
    return `<div class="hooks-rule-detail-row">`
      + `<span class="hooks-rule-detail-label">${escapeHtml(label)}</span>`
      + `<span class="hooks-rule-detail-value">${escapeHtml(String(value))}</span>`
      + `</div>`;
  }

  let html = `<div class="hooks-rule-detail-header">`;
  html += `<strong>${escapeHtml(label)}</strong>`;
  html += `<span class="badge ${entry._bucket === 'pending' ? 'badge-warning' : 'badge-success'}">${escapeHtml(entry._bucket || '')}</span>`;
  html += `</div>`;
  html += `<div class="hooks-rule-detail-inner">`;
  html += _row(t('settings.hooks.detail.event'), entry.event);
  html += _row(t('settings.hooks.detail.matcher'), entry.matcher);
  for (const h of hooks) {
    html += _row(t('settings.hooks.detail.type'), h.type);
    html += _row(t('settings.hooks.detail.command'), h.command);
    if (h.timeout !== undefined && h.timeout !== null && h.timeout !== '') {
      html += _row(t('settings.hooks.detail.timeout'), h.timeout);
    }
  }
  html += `<div class="hooks-rule-detail-row">`;
  html += `<span class="hooks-rule-detail-label">${escapeHtml(t('settings.hooks.detail.rule_json'))}</span>`;
  html += `<pre class="hooks-rule-detail-json">${escapeHtml(JSON.stringify(rule, null, 2))}</pre>`;
  html += `</div>`;
  html += `</div>`;

  panel.innerHTML = html;
  panel.hidden = false;
  panel.setAttribute('data-hook-key', key);
}

async function loadHooksSync() {
  const seq = ++_hooksSyncSeq;
  const statusEl = qs('hooks-sync-status');
  const contentEl = qs('hooks-sync-content');
  panelLoading(contentEl);
  const requestedScope = _hooksCurrentTargetScope();
  const requestedProjectScope = _hooksCurrentProjectScope();
  if (typeof _ctxFetchProjects === 'function') {
    try {
      await _ctxFetchProjects();
    } catch (err) {
      if (
        seq !== _hooksSyncSeq
        || requestedScope !== _hooksCurrentTargetScope()
        || requestedProjectScope !== _hooksCurrentProjectScope()
      ) return;
      contentEl.innerHTML = emptyState('', 'Failed to load projects', err.message);
      return;
    }
  }
  statusEl.innerHTML = _hooksProjectControlsHtml() + _hooksTierControlsHtml();
  _hooksWireProjectControls();
  _hooksWireTierControls();

  try {
    const res = await fetch(_hooksScopedUrl('/api/settings-sync'));
    if (
      seq !== _hooksSyncSeq
      || requestedScope !== _hooksCurrentTargetScope()
      || requestedProjectScope !== _hooksCurrentProjectScope()
    ) return;
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed: ${res.status}`);
    }
    const data = await res.json();
    if (
      seq !== _hooksSyncSeq
      || requestedScope !== _hooksCurrentTargetScope()
      || requestedProjectScope !== _hooksCurrentProjectScope()
    ) return;

    // Status badge
    const badges = {
      in_sync: { cls: 'badge-success', text: t('settings.hooks.in_sync') },
      out_of_sync: { cls: 'badge-warning', text: `${data.hooks?.pending?.length || 0} ${t('settings.hooks.pending')}` },
      conflicts: { cls: 'badge-danger', text: `${data.hooks?.conflicts?.length || 0} ${t('settings.hooks.conflicts')}` },
      no_source: { cls: 'badge-muted', text: t('settings.hooks.no_source') },
      error: { cls: 'badge-danger', text: data.error || 'Error' },
    };
    const badge = badges[data.status] || badges.error;
    // Scope-aware target label (issue #962). Old payloads (or any future
    // scope name without a specific key) fall back to the generic
    // ``target_label``. ``t()`` echoes the key when no translation is
    // registered, so detect that and fall back rather than rendering the
    // raw key text.
    const scope = data.target_scope;
    const scopeKey = scope ? `settings.hooks.target_label_${scope}` : null;
    const translated = scopeKey ? t(scopeKey) : null;
    const targetLabel = translated && translated !== scopeKey
      ? translated
      : t('settings.hooks.target_label');
    // In ``no_source`` state the canonical file does not exist, so the
    // target path is irrelevant — the badge already names the condition
    // ("No .memtomem/settings.json found"). Suppress the target line so
    // the empty-state hint doesn't compete with a leftover scope label
    // (PR D follow-up). ``error`` state still shows it because the
    // target path is often what the user needs to inspect.
    const showTarget = !!data.target_path && data.status !== 'no_source';
    statusEl.innerHTML =
      _hooksProjectControlsHtml()
      + _hooksTierControlsHtml()
      + `<span class="badge ${badge.cls}">${escapeHtml(badge.text)}</span>`
      + (showTarget
        ? `<div class="hooks-status-target" data-target-scope="${escapeHtml(scope || '')}">${escapeHtml(targetLabel)} <code>${escapeHtml(data.target_path)}</code></div>`
        : '');
    _hooksWireTierControls();
    _hooksWireProjectControls();

    // Sync Now is only meaningful when a canonical source exists. Disable
    // the button in ``no_source`` so clicking it doesn't fire a POST that
    // can never succeed; restore the enabled state on the other branches
    // since every ``loadHooksSync`` call ends here.
    const syncBtn = document.getElementById('hooks-sync-btn');
    if (syncBtn) {
      const isNoSource = data.status === 'no_source';
      syncBtn.disabled = isNoSource;
      if (isNoSource) {
        syncBtn.setAttribute('data-no-source', 'true');
        syncBtn.title = t('settings.hooks.sync_now_disabled_no_source');
      } else {
        syncBtn.removeAttribute('data-no-source');
        syncBtn.title = t('settings.hooks.sync_now_tooltip');
      }
    }

    if (data.status === 'no_source' || data.status === 'error') {
      // Status badge above already names the condition — keep the body to
      // a single actionable line so the same string isn't echoed twice.
      contentEl.innerHTML = emptyState(
        '',
        data.status === 'no_source'
          ? t('settings.hooks.no_source_hint')
          : t('settings.hooks.error_hint'),
      );
      return;
    }

    let html = '';

    function _ruleLabel(item) {
      return item.matcher ? `${item.event}:${item.matcher}` : item.event;
    }

    // Build a flat registry of every clickable rule. Index-based keys
    // preserve duplicates (Claude Code allows multiple rules to share
    // the same ``event:matcher`` pair — see ``settings_sync.py:128``).
    // Each row caches its index in ``data-hook-key``; the click
    // handler reads the index, not the label, so two rows that share a
    // label still surface the right entry.
    _hooksRuleRegistry = [];
    const _pendingKeys = [];
    const _syncedKeys = [];
    for (const p of data.hooks.pending) {
      _pendingKeys.push(String(_hooksRuleRegistry.length));
      _hooksRuleRegistry.push({ ...p, _bucket: 'pending' });
    }
    for (const s of data.hooks.synced) {
      _syncedKeys.push(String(_hooksRuleRegistry.length));
      _hooksRuleRegistry.push({ ...s, _bucket: 'synced' });
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
              data-i18n="settings.hooks.use_proposed">${t('settings.hooks.use_proposed')}</button>
          </div>
          <div class="diff-view">${renderDiff(ops)}</div>
        </div>`;
      }
    }

    // Pending — rows are clickable so the per-rule detail panel reveals
    // the full rule body (event / matcher / command / type / timeout /
    // raw JSON). The pre-rendered ``hooks-sync-preview`` block is gone —
    // power users get the same info via Click → Rule JSON.
    if (data.hooks.pending.length) {
      html += '<h3 style="margin:1rem 0 0.5rem">Pending</h3>';
      data.hooks.pending.forEach((p, i) => {
        const label = _ruleLabel(p);
        const key = _pendingKeys[i];
        html += `<div class="hooks-sync-card hooks-rule-row" data-hook-key="${escapeHtml(key)}" tabindex="0" role="button">
          <div class="hooks-sync-card-header"><strong>${escapeHtml(label)}</strong>
            <span class="badge badge-warning">will be added</span></div>
        </div>`;
      });
    }

    // Synced — rows are clickable so the per-rule detail panel reveals
    // the full rule body (PR #968). The section ``<h3>`` is dropped when
    // the entire panel is in_sync (PR #966) because the badge above
    // already says "All hooks are in sync"; keep the heading when mixed
    // state (conflicts/pending alongside synced) makes the section
    // separator load-bearing.
    if (data.hooks.synced.length) {
      if (data.status !== 'in_sync') {
        html += '<h3 style="margin:1rem 0 0.5rem">' + t('settings.hooks.synced') + '</h3>';
      }
      html += '<div class="hooks-synced-list text-muted">';
      data.hooks.synced.forEach((s, i) => {
        const label = _ruleLabel(s);
        const key = _syncedKeys[i];
        html += `<div class="hooks-rule-row hooks-rule-row--synced" data-hook-key="${escapeHtml(key)}" tabindex="0" role="button">${escapeHtml(label)}</div>`;
      });
      html += '</div>';
    }

    // Shared per-rule detail panel — empty until a row is clicked.
    if (data.hooks.synced.length || data.hooks.pending.length) {
      html += `<div id="hooks-rule-detail" class="hooks-rule-detail" hidden></div>`;
    }

    if (!html) {
      html = emptyState('', t('settings.hooks.in_sync'), t('settings.hooks.no_hooks_defined'));
    }

    contentEl.innerHTML = html;

    // Per-rule detail click handler (#962). Synced + pending rows surface
    // the full rule body inline; conflict cards already render their own
    // diff view as the effective detail and are intentionally skipped here.
    contentEl.querySelectorAll('.hooks-rule-row').forEach(row => {
      const handler = () => _renderHookRuleDetail(row.dataset.hookKey, contentEl);
      row.addEventListener('click', handler);
      row.addEventListener('keydown', evt => {
        if (evt.key === 'Enter' || evt.key === ' ') {
          evt.preventDefault();
          handler();
        }
      });
    });

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
          const csrf = await ensureCsrfToken();
          const headers = csrf
            ? {'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf}
            : {'Content-Type': 'application/json'};
          const r = await fetch(_hooksScopedUrl('/api/context/settings/resolve'), {
            method: 'POST',
            headers,
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
    if (
      seq !== _hooksSyncSeq
      || requestedScope !== _hooksCurrentTargetScope()
      || requestedProjectScope !== _hooksCurrentProjectScope()
    ) return;
    contentEl.innerHTML = emptyState('', t('settings.hooks.load_failed'), err.message);
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
    const csrf = await ensureCsrfToken();
    const headers = csrf
      ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
      : { 'Content-Type': 'application/json' };
    // The confirm modal IS the host-write trust gate (issue #962). Send
    // ``allow_host_writes: true`` so the server doesn't re-prompt with
    // ``needs_confirmation`` for the user-scope ``~/.claude/settings.json``
    // path — the same gate the CLI confirms interactively
    // (``cli/context_cmd.py:_confirm_settings_host_writes``).
    const res = await fetch(_hooksScopedUrl('/api/settings-sync'), {
      method: 'POST',
      headers,
      body: JSON.stringify({ allow_host_writes: true }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed: ${res.status}`);
    }
    const data = await res.json();
    const results = Array.isArray(data.results) ? data.results : [];
    const needsConfirmation = results.filter(r => r.status === 'needs_confirmation');
    if (needsConfirmation.length) {
      // Defensive branch: the first POST already carried
      // ``allow_host_writes: true``. If the server still returns
      // ``needs_confirmation`` a deeper trust-gate kicked in (e.g.
      // unexpected scope resolution); surface the targets so the user
      // can investigate via the CLI rather than silently retrying
      // with the same flag (would loop).
      const targets = needsConfirmation.map(r => r.target).filter(Boolean);
      const body = t('settings.hooks.needs_confirmation_body', {
        targets: targets.join('\n'),
      });
      const statusEl = qs('hooks-sync-status');
      if (statusEl) {
        const banner = document.createElement('div');
        banner.className = 'hooks-sync-needs-confirmation';
        banner.setAttribute('role', 'alert');
        const title = document.createElement('strong');
        title.textContent = t('settings.hooks.needs_confirmation_title');
        const detail = document.createElement('div');
        detail.className = 'hooks-sync-needs-confirmation-body';
        detail.textContent = body;
        banner.appendChild(title);
        banner.appendChild(detail);
        statusEl.appendChild(banner);
      }
      showToast(t('settings.hooks.needs_confirmation_title'), 'warning');
      // Do NOT show sync_success on this branch — silent failure 금지.
      return;
    }
    const warnings = results.flatMap(r => r.warnings || []);
    if (warnings.length) {
      showToast(t('toast.hooks_warnings', { count: warnings.length }), 'warning');
    } else {
      showToast(t('settings.hooks.sync_success'));
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

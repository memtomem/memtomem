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
let _hooksLastSyncData = null;

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
  const badgeClass = entry._bucket === 'pending'
    ? 'badge-warning'
    : (entry._bucket === 'configured' || entry._bucket === 'target-only')
      ? 'badge-muted'
      : 'badge-success';
  html += `<span class="badge ${badgeClass}">${escapeHtml(entry._bucket || '')}</span>`;
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
  if (entry._bucket === 'configured' || entry._bucket === 'target-only') {
    html += `<div class="hooks-rule-detail-actions">`;
    html += `<button class="btn-sm btn-secondary hooks-rule-promote-btn" data-action="promote" data-hook-key="${escapeHtml(key)}">${escapeHtml(t('settings.hooks.promote_btn'))}</button>`;
    html += `<button class="btn-sm btn-danger hooks-rule-delete-btn" data-action="delete" data-hook-key="${escapeHtml(key)}">${escapeHtml(t('settings.hooks.delete_btn'))}</button>`;
    html += `</div>`;
    html += `<div class="hooks-rule-edit-hint">${escapeHtml(t('settings.hooks.edit_unavailable_v1_hint', { path: _hooksLastSyncData?.target_path || '' }))}</div>`;
  }
  html += `</div>`;

  panel.innerHTML = html;
  panel.hidden = false;
  panel.setAttribute('data-hook-key', key);
}

function _hooksRuleActionPayload(entry, confirmPrivateToShared) {
  const data = _hooksLastSyncData || {};
  return {
    event: entry.event || '',
    matcher: entry.matcher || '',
    rule_index: entry.rule_index,
    rule_hash: entry.rule_hash,
    target_mtime_ns: data.target_mtime_ns ?? null,
    canonical_mtime_ns: data.canonical_mtime_ns ?? null,
    confirm_private_to_shared: !!confirmPrivateToShared,
  };
}

function _hooksIsPrivateTargetScope() {
  const scope = _hooksLastSyncData?.target_scope || _hooksCurrentTargetScope();
  return scope === 'user' || scope === 'project_local';
}

async function _hooksFetchSyncData() {
  const csrf = await ensureCsrfToken();
  const headers = csrf
    ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
    : { 'Content-Type': 'application/json' };
  const res = await fetch(_hooksScopedUrl('/api/settings-sync'), { method: 'GET', headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

async function _hooksPostRuleAction(action, entry, confirmPrivateToShared) {
  const csrf = await ensureCsrfToken();
  const headers = csrf
    ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
    : { 'Content-Type': 'application/json' };
  const res = await fetch(_hooksScopedUrl(`/api/context/settings/rules/${action}`), {
    method: 'POST',
    headers,
    body: JSON.stringify(_hooksRuleActionPayload(entry, confirmPrivateToShared)),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

async function _confirmHooksPromote(count, label) {
  const privateTarget = _hooksIsPrivateTargetScope();
  const targetPath = _hooksLastSyncData?.target_path || '';
  const title = count > 1
    ? t('settings.hooks.promote_all_btn')
    : t('settings.hooks.promote_btn');
  const message = count > 1
    ? t(
      privateTarget
        ? 'settings.hooks.promote_all_confirm_private'
        : 'settings.hooks.promote_all_confirm',
      { count, path: targetPath },
    )
    : t(
      privateTarget
        ? 'settings.hooks.promote_confirm_private'
        : 'settings.hooks.promote_confirm',
      { label, path: targetPath },
    );
  const choice = await showConfirm({
    title,
    message,
    confirmText: t('settings.hooks.promote_btn'),
    extraOption: {
      id: 'delete_original',
      label: t('settings.hooks.promote_delete_original_option'),
      defaultChecked: false,
    },
  });
  if (typeof choice === 'boolean') {
    return { ok: choice, deleteOriginal: false };
  }
  return {
    ok: !!choice?.ok,
    deleteOriginal: !!choice?.extras?.delete_original,
  };
}

function _findMatchingTargetRule(data, entry) {
  const rows = Array.isArray(data?.target_hooks?.configured)
    ? data.target_hooks.configured
    : [];
  return rows.find(row =>
    row.event === entry.event
    && (row.matcher || '') === (entry.matcher || '')
    && row.rule_hash === entry.rule_hash,
  );
}

async function _deleteOriginalAfterPromote(entry) {
  const latest = await _hooksFetchSyncData();
  _hooksLastSyncData = latest;
  const current = _findMatchingTargetRule(latest, entry);
  if (!current) {
    return { status: 'ok', reason: t('settings.hooks.original_already_removed') };
  }
  return _hooksPostRuleAction('delete', current, false);
}

async function _handleHooksRuleAction(action, key, btn) {
  const idx = Number(key);
  const entry = Number.isInteger(idx) ? _hooksRuleRegistry[idx] : undefined;
  if (!entry) return;
  const label = entry.matcher ? `${entry.event}:${entry.matcher}` : entry.event;

  if (action === 'delete') {
    const ok = await showConfirm({
      title: t('settings.hooks.delete_btn'),
      message: t('settings.hooks.delete_confirm', { label }),
      confirmText: t('common.delete'),
    });
    if (!ok) return;
  } else if (action === 'promote') {
    const choice = await _confirmHooksPromote(1, label);
    if (!choice.ok) return;
    entry._deleteOriginalAfterPromote = choice.deleteOriginal;
  }

  btnLoading(btn, true);
  try {
    let result = await _hooksPostRuleAction(action, entry, _hooksIsPrivateTargetScope());
    if (result.status === 'needs_confirmation' && action === 'promote') {
      // Defensive fallback for older clients or unexpected scope changes:
      // the normal promote path already asks once before the request.
      result = await _hooksPostRuleAction(action, entry, true);
    }

    if (result.status === 'ok') {
      if (action === 'promote' && entry._deleteOriginalAfterPromote) {
        const deleted = await _deleteOriginalAfterPromote(entry);
        if (deleted.status === 'ok') {
          showToast(t('settings.hooks.promote_delete_success'));
        } else {
          showToast(deleted.reason || t('settings.hooks.promote_delete_partial'), 'warning');
        }
        loadHooksSync();
        return;
      }
      showToast(
        result.reason || t('settings.hooks.rule_action_success'),
        result.idempotent ? 'info' : 'success',
      );
      loadHooksSync();
      return;
    }
    if (result.status === 'conflict') {
      showToast(result.reason || t('settings.hooks.promote_conflict'), 'warning');
      loadHooksSync();
      return;
    }
    if (result.status === 'aborted') {
      showToast(result.reason || t('settings.hooks.rule_action_stale'), 'error');
      loadHooksSync();
      return;
    }
    showToast(result.reason || t('toast.unexpected_response'), 'error');
  } catch (err) {
    showToast(err.message || t('toast.request_failed'), 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function _handleHooksPromoteAll(btn) {
  const entries = _hooksRuleRegistry.filter(entry => entry._bucket === 'target-only');
  if (!entries.length) return;
  const choice = await _confirmHooksPromote(entries.length, '');
  if (!choice.ok) return;

  btnLoading(btn, true);
  const summary = { saved: 0, deleted: 0, conflicts: 0, aborted: 0, failed: 0 };
  const deleteQueue = [];
  try {
    for (const entry of entries) {
      try {
        let result = await _hooksPostRuleAction('promote', entry, _hooksIsPrivateTargetScope());
        if (result.status === 'needs_confirmation') {
          result = await _hooksPostRuleAction('promote', entry, true);
        }
        if (result.status === 'ok') {
          summary.saved += 1;
          if (choice.deleteOriginal) deleteQueue.push(entry);
          // Each successful promote rewrites .memtomem/settings.json; refresh
          // mtimes so the next iteration's freshness check doesn't abort.
          if (_hooksLastSyncData) {
            if (result.canonical_mtime_ns != null) {
              _hooksLastSyncData.canonical_mtime_ns = result.canonical_mtime_ns;
            }
            if (result.target_mtime_ns != null) {
              _hooksLastSyncData.target_mtime_ns = result.target_mtime_ns;
            }
          }
        } else if (result.status === 'conflict') {
          summary.conflicts += 1;
        } else if (result.status === 'aborted') {
          summary.aborted += 1;
        } else {
          summary.failed += 1;
        }
      } catch (_err) {
        summary.failed += 1;
      }
    }

    for (const entry of deleteQueue) {
      try {
        const deleted = await _deleteOriginalAfterPromote(entry);
        if (deleted.status === 'ok') summary.deleted += 1;
        else summary.failed += 1;
      } catch (_err) {
        summary.failed += 1;
      }
    }

    const tone = summary.conflicts || summary.aborted || summary.failed ? 'warning' : 'success';
    showToast(t('settings.hooks.promote_all_result', summary), tone);
    loadHooksSync();
  } finally {
    btnLoading(btn, false);
  }
}

async function loadHooksSync() {
  const seq = ++_hooksSyncSeq;
  const statusEl = qs('hooks-sync-status');
  const contentEl = qs('hooks-sync-content');
  panelLoading(contentEl);
  const requestedScope = _hooksCurrentTargetScope();
  let requestedProjectScope = _hooksCurrentProjectScope();
  if (typeof _ctxFetchProjects === 'function') {
    try {
      await _ctxFetchProjects();
    } catch (err) {
      requestedProjectScope = _hooksCurrentProjectScope();
      if (seq !== _hooksSyncSeq || requestedScope !== _hooksCurrentTargetScope()
        || requestedProjectScope !== _hooksCurrentProjectScope()) return;
      contentEl.innerHTML = emptyState('', 'Failed to load projects', err.message);
      return;
    }
  }
  requestedProjectScope = _hooksCurrentProjectScope();
  statusEl.innerHTML = _hooksProjectControlsHtml() + _hooksTierControlsHtml();
  _hooksWireProjectControls();
  _hooksWireTierControls();

  try {
    const res = await fetch(_hooksScopedUrl('/api/settings-sync'));
    if (seq !== _hooksSyncSeq || requestedScope !== _hooksCurrentTargetScope()
      || requestedProjectScope !== _hooksCurrentProjectScope()) return;
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
    _hooksLastSyncData = data;

    // Status badge
    const badges = {
      in_sync: { cls: 'badge-success', text: t('settings.hooks.in_sync') },
      out_of_sync: { cls: 'badge-warning', text: `${data.hooks?.pending?.length || 0} ${t('settings.hooks.pending')}` },
      conflicts: { cls: 'badge-danger', text: `${data.hooks?.conflicts?.length || 0} ${t('settings.hooks.conflicts')}` },
      no_hooks: { cls: 'badge-muted', text: t('settings.hooks.no_hooks') },
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

    // Sync Now is only meaningful when a canonical source exists and has
    // at least one hook rule. Disable the button for empty sources so a
    // no-op cannot look like a successful hook sync.
    const syncBtn = document.getElementById('hooks-sync-btn');
    if (syncBtn) {
      const isNoSource = data.status === 'no_source';
      const isNoHooks = data.status === 'no_hooks';
      syncBtn.disabled = isNoSource || isNoHooks;
      if (isNoSource) {
        syncBtn.setAttribute('data-no-source', 'true');
        syncBtn.removeAttribute('data-no-hooks');
        syncBtn.title = t('settings.hooks.sync_now_disabled_no_source');
      } else if (isNoHooks) {
        syncBtn.removeAttribute('data-no-source');
        syncBtn.setAttribute('data-no-hooks', 'true');
        syncBtn.title = t('settings.hooks.sync_now_disabled_no_hooks');
      } else {
        syncBtn.removeAttribute('data-no-source');
        syncBtn.removeAttribute('data-no-hooks');
        syncBtn.title = t('settings.hooks.sync_now_tooltip');
      }
    }

    const hasTargetConfigured = Array.isArray(data.target_hooks?.configured)
      && data.target_hooks.configured.length > 0;
    if ((data.status === 'no_source' && !hasTargetConfigured) || data.status === 'error') {
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
    const _configuredKeys = [];
    for (const p of data.hooks.pending) {
      _pendingKeys.push(String(_hooksRuleRegistry.length));
      _hooksRuleRegistry.push({ ...p, _bucket: 'pending' });
    }
    for (const s of data.hooks.synced) {
      _syncedKeys.push(String(_hooksRuleRegistry.length));
      _hooksRuleRegistry.push({ ...s, _bucket: 'synced' });
    }
    const targetConfigured = Array.isArray(data.target_hooks?.configured)
      ? data.target_hooks.configured
      : [];
    const targetOnlyRows = Array.isArray(data.target_hooks?.target_only)
      ? data.target_hooks.target_only
      : [];
    const targetOnlyKeys = new Set(targetOnlyRows.map(row =>
      `${row.event || ''}\u0000${row.matcher || ''}\u0000${JSON.stringify(row.rule || {})}`,
    ));
    function _targetRuleKey(row) {
      return `${row.event || ''}\u0000${row.matcher || ''}\u0000${JSON.stringify(row.rule || {})}`;
    }
    for (const row of targetConfigured) {
      _configuredKeys.push(String(_hooksRuleRegistry.length));
      _hooksRuleRegistry.push({
        ...row,
        _bucket: targetOnlyKeys.has(_targetRuleKey(row)) ? 'target-only' : 'configured',
      });
    }

    // Conflicts
    if (data.hooks.conflicts.length) {
      html += '<h3 style="margin:1rem 0 0.5rem">Conflicts</h3>';
      for (const c of data.hooks.conflicts) {
        const label = _ruleLabel(c);
        const oldText = JSON.stringify(c.existing, null, 2);
        const newText = JSON.stringify(c.proposed, null, 2);
        const ops = diffLines(oldText, newText);
        // Carry the exact rule identity (issue #1112) so resolving the Nth
        // same-matcher conflict updates the Nth target row, not the first.
        // Absent on legacy payloads → the resolve POST falls back to
        // label-only first-match.
        const idAttrs = (c.target_rule_index != null && c.target_rule_hash != null)
          ? ` data-rule-index="${escapeHtml(String(c.target_rule_index))}"`
            + ` data-rule-hash="${escapeHtml(c.target_rule_hash)}"`
            + ` data-proposed-hash="${escapeHtml(c.proposed_hash || '')}"`
          : '';
        html += `<div class="hooks-sync-card hooks-sync-conflict" data-event="${escapeHtml(c.event)}" data-matcher="${escapeHtml(c.matcher || '')}"${idAttrs}>
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

    if (targetConfigured.length) {
      const targetOnlyCount = targetConfigured.filter(row => targetOnlyKeys.has(_targetRuleKey(row))).length;
      html += '<div class="hooks-section-header">';
      html += '<h3>' + t('settings.hooks.configured') + '</h3>';
      if (targetOnlyCount) {
        html += `<button class="btn-sm btn-primary hooks-promote-all-btn">${escapeHtml(t('settings.hooks.promote_all_btn'))} (${targetOnlyCount})</button>`;
      }
      html += '</div>';
      html += '<div class="hooks-synced-list text-muted">';
      targetConfigured.forEach((row, i) => {
        const label = _ruleLabel(row);
        const key = _configuredKeys[i];
        const targetOnly = targetOnlyKeys.has(_targetRuleKey(row));
        html += `<div class="hooks-rule-row hooks-rule-row--configured" data-hook-key="${escapeHtml(key)}" tabindex="0" role="button">`
          + `${escapeHtml(label)}`
          + (targetOnly ? ` <span class="badge badge-muted">${escapeHtml(t('settings.hooks.target_only'))}</span>` : '')
          + `</div>`;
      });
      html += '</div>';
    }

    // Shared per-rule detail panel — empty until a row is clicked.
    if (data.hooks.synced.length || data.hooks.pending.length || targetConfigured.length) {
      html += `<div id="hooks-rule-detail" class="hooks-rule-detail" hidden></div>`;
    }

    if (!html) {
      if (data.status === 'no_hooks') {
        html = emptyState('', t('settings.hooks.no_hooks'), t('settings.hooks.no_hooks_hint'));
      } else {
        html = emptyState('', t('settings.hooks.in_sync'), t('settings.hooks.no_hooks_defined'));
      }
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

    if (!contentEl._hooksRuleActionWired) {
      contentEl.addEventListener('click', evt => {
        const bulkBtn = evt.target.closest?.('.hooks-promote-all-btn');
        if (bulkBtn) {
          evt.preventDefault();
          evt.stopPropagation();
          _handleHooksPromoteAll(bulkBtn);
          return;
        }
        const btn = evt.target.closest?.('.hooks-rule-promote-btn, .hooks-rule-delete-btn');
        if (!btn) return;
        evt.preventDefault();
        evt.stopPropagation();
        _handleHooksRuleAction(btn.dataset.action, btn.dataset.hookKey, btn);
      });
      contentEl._hooksRuleActionWired = true;
    }

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
          // Send the exact rule identity when the card carries it (issue
          // #1112) so the Nth same-matcher conflict resolves the Nth row.
          // Legacy cards without identity fall back to label-only first-match.
          const resolveBody = {event, matcher, action: 'use_proposed'};
          if (card.dataset.ruleIndex !== undefined && card.dataset.ruleHash !== undefined) {
            resolveBody.rule_index = Number(card.dataset.ruleIndex);
            resolveBody.rule_hash = card.dataset.ruleHash;
            if (card.dataset.proposedHash) resolveBody.proposed_hash = card.dataset.proposedHash;
          }
          const r = await fetch(_hooksScopedUrl('/api/context/settings/resolve'), {
            method: 'POST',
            headers,
            body: JSON.stringify(resolveBody),
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
  // Snapshot the scoped URL at click time and reuse it for BOTH the probe and
  // the authorized write, so the targets disclosed in the confirm dialog and
  // the files actually written belong to the SAME scope. The scope globals
  // (`_ctxTargetScope` / `_ctxActiveScopeId`) are live; recomputing the URL per
  // call would let a mid-flight scope change authorize a write against a
  // different tier than was disclosed (TOCTOU). Before the write we re-derive
  // the URL and abort if it drifted.
  const scopedUrl = _hooksScopedUrl('/api/settings-sync');
  const postSettingsSync = async (allowHostWrites) => {
    const csrf = await ensureCsrfToken();
    const headers = csrf
      ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
      : { 'Content-Type': 'application/json' };
    const res = await fetch(scopedUrl, {
      method: 'POST',
      headers,
      body: JSON.stringify({ allow_host_writes: allowHostWrites }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed: ${res.status}`);
    }
    return res.json();
  };

  const renderNeedsConfirmation = (needsConfirmation) => {
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
  };

  const finishSyncResponse = (data) => {
    const results = Array.isArray(data.results) ? data.results : [];
    // Multi-runtime fan-out (Codex/Gemini, ADR-0018): a per-runtime
    // ``error`` (malformed target JSON) or ``aborted`` (concurrent write)
    // must NOT be reported as success — same no-silent-failure contract as
    // the needs_confirmation branch below. Without this a Codex/Gemini
    // failure would fall through to ``sync_success`` while only Claude
    // actually synced.
    const failed = results.filter(r => r.status === 'error' || r.status === 'aborted');
    if (failed.length) {
      const detail = failed.map(r => `${r.name}: ${r.reason || r.status}`).join('; ');
      showToast(t('toast.sync_failed', { error: detail }), 'error');
      loadHooksSync();
      return;
    }
    const needsConfirmation = results.filter(r => r.status === 'needs_confirmation');
    if (needsConfirmation.length) {
      // Defensive branch: this should only happen after the user has
      // confirmed the returned host targets. Surface the server's targets
      // and stop; retrying with the same flag would loop.
      renderNeedsConfirmation(needsConfirmation);
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
  };

  btnLoading(btn, true);
  try {
    // Probe first without host-write permission. For user-scope installs the
    // server returns the exact host targets (Claude/Codex/Gemini) to disclose
    // in the confirmation modal; project-scope writes proceed immediately.
    const firstData = await postSettingsSync(false);
    const firstResults = Array.isArray(firstData.results) ? firstData.results : [];
    const needsConfirmation = firstResults.filter(r => r.status === 'needs_confirmation');
    if (needsConfirmation.length) {
      const targets = needsConfirmation.map(r => r.target).filter(Boolean);
      btnLoading(btn, false);
      if (targets.length === 0) {
        // Fail closed: never authorize a host write whose targets the server
        // did not disclose. (The backend currently always populates ``target``.)
        showToast(t('toast.sync_targets_unavailable'), 'error');
        loadHooksSync();
        return;
      }
      const ok = await showConfirm({
        title: t('confirm.hooks_sync_title'),
        message: t('confirm.hooks_sync_msg', { targets: targets.join('\n') }),
        confirmText: t('common.sync'),
      });
      if (!ok) {
        loadHooksSync();
        return;
      }
      // Trust gate: the scope must not have drifted between the disclosure
      // (probe) and this authorization. Re-derive the scoped URL and abort if
      // it no longer matches the one the disclosed targets came from.
      if (_hooksScopedUrl('/api/settings-sync') !== scopedUrl) {
        showToast(t('toast.sync_scope_changed'), 'error');
        loadHooksSync();
        return;
      }
      btnLoading(btn, true);
      finishSyncResponse(await postSettingsSync(true));
      return;
    }
    finishSyncResponse(firstData);
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

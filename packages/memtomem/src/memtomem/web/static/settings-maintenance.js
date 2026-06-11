/**
 * Maintenance — Dedup scan, Decay scan/expire, Export/Import.
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

// ---------------------------------------------------------------------------
// Dedup
// ---------------------------------------------------------------------------

qs('dedup-scan-btn').addEventListener('click', runDedupScan);

// STATE.dedupScanActive, STATE.dedupAbortCtrl now in STATE
const DEDUP_SCAN_TIMEOUT_MS = 120_000;

// Which message ``dedup-empty`` is showing ('initial' | 'failed' |
// 'no_results'; null while the candidate list is visible), plus the
// threshold baked into the no-results text. The langchange listener at
// the end of this section re-renders the JS-owned strings from these.
let _dedupEmptyKind = 'initial';
let _dedupLastThreshold = null;

function _renderDedupEmpty(kind) {
  _dedupEmptyKind = kind;
  const message = kind === 'failed' ? t('settings.dedup.scan_failed')
    : kind === 'no_results' ? t('settings.dedup.no_results', { threshold: _dedupLastThreshold })
    : t('settings.dedup.empty_text');
  // Icon must stay a trusted literal — ``emptyState`` inserts its icon
  // slot into innerHTML unescaped, so translated values can't go there.
  qs('dedup-empty').innerHTML = emptyState('📋', message);
}

function resetDedupPanel() {
  // Don't reset while a scan is still running — keep the UI consistent
  if (STATE.dedupScanActive) return;
  hide(qs('dedup-list'));
  _renderDedupEmpty('initial');
  show(qs('dedup-empty'));
}

async function runDedupScan() {
  const threshold = parseFloat(qs('dedup-threshold').value);
  const limit     = parseInt(qs('dedup-limit').value, 10);
  const maxScan   = parseInt(qs('dedup-max-scan').value, 10);
  const btn       = qs('dedup-scan-btn');
  const empty     = qs('dedup-empty');

  STATE.dedupScanActive = true;
  btnLoading(btn, true);
  hide(qs('dedup-list'));
  hide(qs('dedup-msg'));
  show(empty);
  empty.innerHTML = '<div class="spinner-panel"></div>';

  // Abort any previous request and set a timeout aligned with the server.
  if (STATE.dedupAbortCtrl) STATE.dedupAbortCtrl.abort();
  STATE.dedupAbortCtrl = new AbortController();
  const timeoutId = setTimeout(() => STATE.dedupAbortCtrl.abort(), DEDUP_SCAN_TIMEOUT_MS);

  try {
    const params = new URLSearchParams({ threshold, limit, max_scan: maxScan });
    const res = await fetch(`/api/dedup/candidates?${params}`, { signal: STATE.dedupAbortCtrl.signal });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(typeof err.detail === 'string' ? err.detail : res.statusText);
    }
    const data = await res.json();
    renderDedupCandidates(data.candidates, threshold);
  } catch (err) {
    const msg = err.name === 'AbortError' ? t('settings.dedup.scan_timeout') : err.message;
    setMsg(qs('dedup-msg'), t('settings.dedup.scan_error', { error: msg }), true);
    _renderDedupEmpty('failed');
  } finally {
    clearTimeout(timeoutId);
    STATE.dedupScanActive = false;
    btnLoading(btn, false);
  }
}

function renderDedupCandidates(candidates, threshold) {
  const list  = qs('dedup-list');
  const empty = qs('dedup-empty');

  if (!candidates.length) {
    hide(list);
    _dedupLastThreshold = threshold;
    _renderDedupEmpty('no_results');
    show(empty);
    return;
  }

  hide(empty);
  show(list);
  _dedupEmptyKind = null;

  // Translated text enters through data-i18n* attributes (applyDOM writes
  // textContent/title only) or through the text-node helpers below — never
  // through innerHTML — so a locale value containing markup stays inert.
  const summary = document.createElement('div');
  summary.className = 'dedup-summary';
  summary.dataset.summaryKey =
    candidates.length === 1 ? 'settings.dedup.summary_one' : 'settings.dedup.summary_other';
  _setDedupSummaryText(summary, candidates.length);
  list.innerHTML = '';
  list.appendChild(summary);

  candidates.forEach((c, i) => {
    const row = document.createElement('div');
    row.className = 'dedup-row';

    const badge = c.exact
      ? '<span class="badge badge-danger" data-i18n="settings.dedup.badge_exact"></span>'
      : `<span class="badge badge-warn">~${c.score.toFixed(3)}</span>`;

    row.innerHTML = `
      <div class="dedup-row-header">
        <span class="dedup-index">#${i + 1}</span>
        ${badge}
        <div class="dedup-actions">
          <button class="btn-primary keep-a-btn" data-i18n="settings.dedup.keep_a"
                  data-i18n-title="settings.dedup.keep_a_title"></button>
          <button class="btn-ghost keep-b-btn" data-i18n="settings.dedup.keep_b"
                  data-i18n-title="settings.dedup.keep_b_title"></button>
          <button class="btn-ghost skip-btn" data-i18n="settings.dedup.skip"></button>
        </div>
      </div>
      <div class="dedup-chunks">
        <div class="dedup-chunk">
          <div class="dedup-chunk-label" data-i18n="settings.dedup.chunk_a_label"></div>
          <div class="dedup-chunk-meta">
            <span class="file-path">${escapeHtml(c.chunk_a.source_file)}</span>
            <span class="lines-info" data-start="${escapeAttr(c.chunk_a.start_line)}"
                  data-end="${escapeAttr(c.chunk_a.end_line)}"></span>
          </div>
          <div class="dedup-chunk-content">${escapeHtml(truncate(c.chunk_a.content, 240))}</div>
        </div>
        <div class="dedup-chunk">
          <div class="dedup-chunk-label" data-i18n="settings.dedup.chunk_b_label"></div>
          <div class="dedup-chunk-meta">
            <span class="file-path">${escapeHtml(c.chunk_b.source_file)}</span>
            <span class="lines-info" data-start="${escapeAttr(c.chunk_b.start_line)}"
                  data-end="${escapeAttr(c.chunk_b.end_line)}"></span>
          </div>
          <div class="dedup-chunk-content">${escapeHtml(truncate(c.chunk_b.content, 240))}</div>
        </div>
      </div>
      <div class="dedup-row-msg status-msg" hidden></div>
    `;

    row.querySelector('.keep-a-btn').addEventListener('click', async () => {
      const ok = await showConfirm({
        title: t('confirm.merge_dupe_title'),
        message: t('confirm.merge_dupe_keep_a_msg'),
        confirmText: t('common.merge'),
      });
      if (ok) doMerge(row, c.chunk_a.id, [c.chunk_b.id]);
    });
    row.querySelector('.keep-b-btn').addEventListener('click', async () => {
      const ok = await showConfirm({
        title: t('confirm.merge_dupe_title'),
        message: t('confirm.merge_dupe_keep_b_msg'),
        confirmText: t('common.merge'),
      });
      if (ok) doMerge(row, c.chunk_b.id, [c.chunk_a.id]);
    });
    row.querySelector('.skip-btn').addEventListener('click', () => row.remove());

    list.appendChild(row);
  });

  // Fill the freshly created data-i18n* nodes now; langchange re-fills
  // them automatically via the same applyDOM pass.
  if (typeof I18N !== 'undefined') I18N.applyDOM();
  _applyDedupLinesText(list);
}

// "Found <strong>N</strong> candidate pairs…" — the count is a real
// element (doMerge rewrites it in place, Bug #12) and the surrounding
// translation is inserted as text nodes around it.
function _setDedupSummaryText(el, count) {
  const [before, after = ''] = t(el.dataset.summaryKey).split('{count}');
  const strong = document.createElement('strong');
  strong.textContent = count;
  el.textContent = '';
  el.append(before, strong, after);
}

function _applyDedupLinesText(scope) {
  scope.querySelectorAll('.lines-info').forEach(el => {
    el.textContent = t('settings.dedup.lines', { start: el.dataset.start, end: el.dataset.end });
  });
}

// data-i18n* nodes are re-translated by applyDOM before langchange fires;
// the JS-owned strings (empty-state message, summary text around the
// count, line ranges) are re-rendered here so scan results stay readable
// across EN/KO toggles.
window.addEventListener('langchange', () => {
  const list = qs('dedup-list');
  if (!list.hidden) {
    const summary = list.querySelector('.dedup-summary');
    if (summary) {
      const strong = summary.querySelector('strong');
      _setDedupSummaryText(summary, strong ? strong.textContent : '');
    }
    _applyDedupLinesText(list);
  }
  // Skip while a scan is running — ``dedup-empty`` holds the spinner then,
  // and the post-scan render uses the new locale anyway.
  if (!STATE.dedupScanActive && !qs('dedup-empty').hidden && _dedupEmptyKind) {
    _renderDedupEmpty(_dedupEmptyKind);
  }
});

async function doMerge(rowEl, keepId, deleteIds) {
  const btns = rowEl.querySelectorAll('button');
  btns.forEach(b => { b.disabled = true; });

  try {
    await api('POST', '/api/dedup/merge', { keep_id: keepId, delete_ids: deleteIds });
    showToast(t('toast.dupes_merged'), 'success');
    rowEl.style.opacity = '0.45';
    // Bug #5: remove deleted chunks from search results
    STATE.lastResults = STATE.lastResults.filter(r => !deleteIds.includes(String(r.chunk.id)));
    renderResults(STATE.lastResults);
    _markDataStale();
    // Bug #12: update dedup summary count
    const summaryEl = qs('dedup-list')?.querySelector('.dedup-summary strong');
    if (summaryEl) {
      const remaining = qs('dedup-list').querySelectorAll('.dedup-row').length - 1;
      summaryEl.textContent = Math.max(0, remaining);
    }
    loadStats();
  } catch (err) {
    showToast(t('toast.merge_failed', { error: err.message }), 'error');
    btns.forEach(b => { b.disabled = false; });
  }
}

// ---------------------------------------------------------------------------
// Decay tab
// ---------------------------------------------------------------------------

function resetDecayPanel() {
  hide(qs('decay-result'));
  hide(qs('decay-msg'));
  qs('decay-expire-btn').disabled = true;

  // Sync defaults from config
  const cfg = STATE.serverConfig?.decay;
  if (cfg) {
    if (cfg.half_life_days) qs('decay-max-age').value = cfg.half_life_days;
  }
}

async function runDecayScan() {
  const maxAge = parseFloat(qs('decay-max-age').value) || 90;
  const srcFilter = qs('decay-source-filter').value.trim();
  const params = new URLSearchParams({ max_age_days: maxAge });
  if (srcFilter) params.set('source_filter', srcFilter);

  const scanBtn = qs('decay-scan-btn');
  btnLoading(scanBtn, true);
  try {
    const data = await api('GET', `/api/decay/scan?${params}`);
    qs('decay-r-total').textContent   = data.total_chunks;
    qs('decay-r-expired').textContent = data.expired_chunks;
    qs('decay-r-deleted').textContent = '—';
    show(qs('decay-result'));
    qs('decay-expire-btn').disabled = data.expired_chunks === 0;
    if (data.expired_chunks === 0) {
      setMsg(qs('decay-msg'), t('settings.decay.no_expire'), false);
    }
  } catch (err) {
    setMsg(qs('decay-msg'), t('settings.decay.scan_failed', { error: err.message }), true);
  } finally {
    btnLoading(scanBtn, false);
  }
}

async function runDecayExpire() {
  // Pull the count from the most recent scan rendered in the result
  // panel — the Expire button is only enabled when scan returned > 0
  // (see ``runDecayScan``), so this is always populated when we get
  // here. Falling back to '?' keeps the modal sensible if a future
  // refactor breaks that invariant rather than silently dropping the
  // count from the message.
  const expiredCountText = qs('decay-r-expired').textContent || '?';
  const ok = await showConfirm({
    title: t('confirm.expire_title'),
    message: t('confirm.expire_msg', { count: expiredCountText }),
    confirmText: t('common.expire'),
  });
  if (!ok) return;
  const maxAge = parseFloat(qs('decay-max-age').value) || 90;
  const srcFilter = qs('decay-source-filter').value.trim() || null;

  const expireBtn = qs('decay-expire-btn');
  btnLoading(expireBtn, true);
  try {
    const data = await api('POST', '/api/decay/expire', {
      max_age_days: maxAge,
      source_filter: srcFilter,
      dry_run: false,
    });
    qs('decay-r-total').textContent   = data.total_chunks;
    qs('decay-r-expired').textContent = data.expired_chunks;
    qs('decay-r-deleted').textContent = data.deleted_chunks;
    show(qs('decay-result'));
    showToast(t('toast.expired_count', { count: data.deleted_chunks }), 'success');
    // Bug #6: clear search results since we don't know which chunks were deleted
    if (data.deleted_chunks > 0) {
      STATE.lastResults = [];
      renderResults([]);
      _markDataStale();
    }
    loadStats();
  } catch (err) {
    showToast(t('toast.expire_failed', { error: err.message }), 'error');
    expireBtn.disabled = false;
  } finally {
    btnLoading(expireBtn, false);
  }
}

qs('decay-scan-btn').addEventListener('click', runDecayScan);
qs('decay-expire-btn').addEventListener('click', runDecayExpire);


// ---------------------------------------------------------------------------
// Export / Import tab
// ---------------------------------------------------------------------------

function resetExportPanel() {
  hide(qs('exp-preview'));
  hide(qs('exp-msg'));
  hide(qs('imp-result'));
  hide(qs('imp-msg'));
  qs('imp-btn').disabled = !qs('imp-file').files?.length;
}

function _exportParams() {
  const params = new URLSearchParams();
  const src   = qs('exp-source').value.trim();
  const tag   = qs('exp-tag').value.trim();
  const since = qs('exp-since').value.trim();
  const ns    = qs('exp-namespace').value;
  if (src)   params.set('source', src);
  if (tag)   params.set('tag', tag);
  if (since) params.set('since', since);
  if (ns)    params.set('namespace', ns);
  return params;
}

async function runExportPreview() {
  hide(qs('exp-preview'));
  try {
    const data = await api('GET', `/api/export/stats?${_exportParams()}`);
    qs('exp-count').textContent = data.total_chunks;
    show(qs('exp-preview'));
  } catch (err) {
    setMsg(qs('exp-msg'), 'Preview failed: ' + err.message, true);
  }
}

function runExportDownload() {
  const url = `/api/export?${_exportParams()}`;
  const a = document.createElement('a');
  a.href = url;
  a.download = 'memtomem_export.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

async function runImport() {
  const file = qs('imp-file').files[0];
  if (!file) return;

  hide(qs('imp-result'));
  qs('imp-btn').disabled = true;

  const form = new FormData();
  form.append('file', file);

  try {
    const csrf = await ensureCsrfToken();
    const headers = csrf ? { 'X-Memtomem-CSRF': csrf } : {};
    const res = await fetch('/api/export/import', { method: 'POST', body: form, headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    qs('imp-total').textContent    = data.total_chunks;
    qs('imp-imported').textContent = data.imported_chunks;
    qs('imp-skipped').textContent  = data.skipped_chunks;
    qs('imp-failed').textContent   = data.failed_chunks;
    show(qs('imp-result'));
    showToast(t('toast.imported_count', { count: data.imported_chunks }), 'success');
    _markDataStale();
    loadSourceFilter();
    loadStats();
  } catch (err) {
    showToast(t('toast.import_failed', { error: err.message }), 'error');
  } finally {
    qs('imp-btn').disabled = false;
  }
}


// ---------------------------------------------------------------------------
// Database Reset
// ---------------------------------------------------------------------------

async function loadResetInfo() {
  try {
    const stats = await api('GET', '/api/stats');
    qs('reset-chunk-count').textContent = stats.total_chunks ?? '—';
  } catch {
    qs('reset-chunk-count').textContent = '?';
  }
}

qs('reset-btn').addEventListener('click', async () => {
  const ok = await showConfirm({
    title: t('settings.reset.confirm_title'),
    message: t('settings.reset.confirm_message'),
    confirmText: t('settings.reset.confirm_btn'),
  });
  if (!ok) return;

  const btn = qs('reset-btn');
  btnLoading(btn, true);
  hide(qs('reset-result'));
  hide(qs('reset-msg'));

  try {
    const data = await api('POST', '/api/reset', undefined, { timeout: 120_000 });
    // Show per-table results
    const table = qs('reset-result-table');
    table.innerHTML = '';
    for (const [name, count] of Object.entries(data.deleted)) {
      if (count > 0) {
        const row = document.createElement('tr');
        row.innerHTML = `<td>${escapeHtml(name)}</td><td>${count}</td>`;
        table.appendChild(row);
      }
    }
    show(qs('reset-result'));
    showToast(data.message, 'success');

    STATE.lastResults = [];
    renderResults([]);
    _markDataStale();
    loadStats();
    qs('reset-chunk-count').textContent = '0';
  } catch (err) {
    setMsg(qs('reset-msg'), 'Reset failed: ' + err.message, true);
  } finally {
    btnLoading(btn, false);
  }
});

// Load chunk count when reset section becomes visible
const _resetObserver = new MutationObserver(() => {
  const section = document.getElementById('settings-reset');
  if (section && !section.hidden && section.classList.contains('active')) {
    loadResetInfo();
  }
});
_resetObserver.observe(document.getElementById('settings-reset'), { attributes: true });

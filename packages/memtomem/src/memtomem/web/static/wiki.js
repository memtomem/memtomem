/*
 * wiki.js — read-only browser for the GLOBAL wiki (~/.memtomem-wiki).
 *
 * ADR-0008 PR-E (E-1). Unlike the per-project context_* surfaces, the wiki is
 * a single host-global git repo, so this controller carries NO project/tier
 * control bar (ctx-wiki is deliberately absent from _CTX_SECTION_BAR_TYPE) and
 * dispatches from switchSettingsSection via loadWiki() rather than loadCtxList.
 *
 * Depends on globals from app.js: qs, show, hide, escapeHtml, t, showToast,
 * showConfirm, ensureCsrfToken, emptyState, panelLoading.
 *
 * i18n: all dynamic text goes through inline t() (never data-i18n on injected
 * nodes — applyDOM would clobber re-rendered content), and a `langchange`
 * listener repaints from cached data so a mid-view language switch is live.
 */

// Cached GET /api/wiki payload so langchange (and detail re-renders) repaint
// without a refetch. `_wikiAbsent` distinguishes "no wiki on disk" (onboarding
// empty-state) from "loaded but empty" so the right message repaints on
// langchange.
let _wikiData = null;
let _wikiAbsent = false;
let _wikiActive = null; // { type, name } currently open in the detail pane
let _wikiVendor = null; // selected vendor in the detail pane
let _wikiView = null; // { diff, lint } last fetched for the open vendor
let _wikiInstallScopeId = ''; // E-3: project picked for install/update ('' = Server CWD)

// Seq guards for overlapping fetches (the innerHTML race rule): a newer load
// bumps the seq so a slower in-flight response can't paint over it.
let _wikiListSeq = 0;
let _wikiDetailSeq = 0;
let _wikiListAbort = null;

function _wikiIsAbort(err) {
  return !!err && (err.name === 'AbortError');
}

async function _wikiErrDetail(res) {
  // The route layer returns the _error envelope ({detail: {message, ...}}); the
  // privacy 422 path returns a string detail. Surface either, else the status.
  try {
    const body = await res.json();
    const d = body && body.detail;
    if (d && typeof d === 'object' && typeof d.message === 'string') return d.message;
    if (typeof d === 'string') return d;
  } catch { /* non-JSON body */ }
  return `HTTP ${res.status}`;
}

function _wikiTypeLabel(type) {
  const key = {
    skills: 'settings.ctx.wiki_type_skills',
    agents: 'settings.ctx.wiki_type_agents',
    commands: 'settings.ctx.wiki_type_commands',
  }[type];
  return key ? t(key) : type;
}

function _renderWikiAbsent() {
  const listEl = qs('wiki-list');
  const headEl = qs('wiki-head');
  const statusEl = qs('wiki-status');
  const detailEl = qs('wiki-detail');
  if (headEl) headEl.textContent = '';
  if (statusEl) statusEl.innerHTML = '';
  if (detailEl) { hide(detailEl); detailEl.innerHTML = ''; }
  if (listEl) {
    listEl.innerHTML = emptyState(
      '📚',
      t('settings.ctx.wiki_empty'),
      t('settings.ctx.wiki_empty_hint'),
    );
  }
}

function _renderWikiHead() {
  const headEl = qs('wiki-head');
  if (!headEl || !_wikiData) return;
  const head = _wikiData.wiki_head || '';
  const shortSha = head ? head.slice(0, 12) : '';
  let html = '';
  if (shortSha) {
    html += `<span class="wiki-head-sha">${escapeHtml(t('settings.ctx.wiki_head'))}: `
      + `<code>${escapeHtml(shortSha)}</code></span>`;
  }
  if (_wikiData.is_dirty) {
    html += ` <span class="badge badge-warning">${escapeHtml(t('settings.ctx.wiki_dirty'))}</span>`;
  }
  headEl.innerHTML = html;
}

function _renderWikiList() {
  const listEl = qs('wiki-list');
  if (!listEl || !_wikiData) return;
  const items = _wikiData.items || [];
  if (!items.length) {
    listEl.innerHTML = emptyState(
      '📭',
      t('settings.ctx.wiki_no_assets'),
      t('settings.ctx.wiki_no_assets_hint'),
    );
    return;
  }
  const order = ['skills', 'agents', 'commands'];
  const groups = {};
  items.forEach((it) => { (groups[it.type] = groups[it.type] || []).push(it); });
  let html = '';
  order.forEach((type) => {
    const group = groups[type];
    if (!group || !group.length) return;
    html += '<div class="wiki-group">';
    html += `<h3 class="wiki-group-title">${escapeHtml(_wikiTypeLabel(type))} `
      + `<span class="wiki-group-count">${group.length}</span></h3>`;
    html += '<ul class="wiki-list-items">';
    group.forEach((it) => {
      // name is server-validated ([A-Za-z0-9._-]+) so it is safe inside the
      // double-quoted data-* attribute; escapeHtml is belt-and-suspenders.
      html += `<li><button type="button" class="wiki-item" `
        + `data-type="${escapeHtml(it.type)}" data-name="${escapeHtml(it.name)}">`
        + `<span class="wiki-item-name">${escapeHtml(it.name)}</span></button></li>`;
    });
    html += '</ul></div>';
  });
  listEl.innerHTML = html;
  listEl.querySelectorAll('.wiki-item').forEach((btn) => {
    if (_wikiActive && btn.dataset.type === _wikiActive.type && btn.dataset.name === _wikiActive.name) {
      btn.classList.add('active');
    }
    btn.addEventListener('click', () => loadWikiDetail(btn.dataset.type, btn.dataset.name));
  });
}

function _renderUnifiedDiff(lines) {
  return (lines || []).map((line) => {
    const stripped = line.replace(/\n$/, '');
    let cls = 'diff-eq';
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-file';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+')) cls = 'diff-add';
    else if (line.startsWith('-')) cls = 'diff-del';
    return `<div class="diff-line ${cls}">${escapeHtml(stripped)}</div>`;
  }).join('');
}

function _renderDroppedNote(dropped) {
  if (!dropped || !dropped.length) return '';
  return `<div class="wiki-dropped">${escapeHtml(
    t('settings.ctx.wiki_dropped', { fields: dropped.join(', ') }),
  )}</div>`;
}

function _renderDiffSection(diff) {
  let html = `<div class="wiki-section"><h4>${escapeHtml(t('settings.ctx.wiki_diff_title'))}</h4>`;
  if (diff && diff._error) {
    html += `<div class="wiki-error">${escapeHtml(diff._error)}</div></div>`;
    return html;
  }
  html += _renderDroppedNote(diff && diff.dropped);
  if (!diff || !diff.exists) {
    html += `<div class="wiki-note">${escapeHtml(t('settings.ctx.wiki_diff_none'))}</div>`;
  } else if (diff.in_sync) {
    html += `<div class="wiki-note"><span class="badge badge-success">`
      + `${escapeHtml(t('settings.ctx.wiki_diff_insync'))}</span></div>`;
  } else {
    html += `<pre class="wiki-diff">${_renderUnifiedDiff(diff.diff_lines)}</pre>`;
  }
  html += '</div>';
  return html;
}

function _renderLintSection(lint) {
  let html = `<div class="wiki-section"><h4>${escapeHtml(t('settings.ctx.wiki_lint_title'))}</h4>`;
  if (lint && lint._error) {
    html += `<div class="wiki-error">${escapeHtml(lint._error)}</div></div>`;
    return html;
  }
  const findings = (lint && lint.findings) || [];
  if (lint && lint.ok && !findings.length) {
    html += `<div class="wiki-note"><span class="badge badge-success">`
      + `${escapeHtml(t('settings.ctx.wiki_lint_ok'))}</span></div>`;
  } else {
    html += '<ul class="wiki-findings">';
    findings.forEach((f) => {
      const badgeCls = f.level === 'error' ? 'badge-danger' : 'badge-warning';
      const levelLabel = f.level === 'error'
        ? t('settings.ctx.wiki_lint_error')
        : t('settings.ctx.wiki_lint_warning');
      html += `<li class="wiki-finding"><span class="badge ${badgeCls}">`
        + `${escapeHtml(levelLabel)}</span> ${escapeHtml(f.message)}</li>`;
    });
    html += '</ul>';
  }
  html += '</div>';
  return html;
}

// --- Override seeding (E-2, dev tier only) ----------------------------------
// Mirrors `mm wiki <type> override`: renders the canonical baseline into
// overrides/<vendor>.<ext> for the user to edit and commit. Gated to dev mode
// (the POST route only mounts there) and, for a re-seed that would clobber an
// existing override, behind a confirm (the writer keeps a .bak sibling).

function _wikiDevMode() {
  return typeof document !== 'undefined'
    && !!document.body
    && document.body.classList.contains('dev-mode');
}

function _wikiVendorRenderable(vendor) {
  if (!_wikiActive) return false;
  const item = ((_wikiData && _wikiData.items) || []).find(
    (i) => i.type === _wikiActive.type && i.name === _wikiActive.name,
  );
  const v = ((item && item.vendors) || []).find((x) => x.vendor === vendor);
  return !!(v && v.renderable);
}

function _renderWikiSeedAction() {
  // Dev-tier only: seeding writes to the host-global wiki working tree.
  if (!_wikiDevMode() || !_wikiActive || !_wikiVendor) return '';
  // A diff error means we can't tell whether an override exists — don't offer a
  // mutation we'd mislabel (Seed vs Re-seed). Non-renderable vendors (the
  // ("commands","codex") placeholder) have no generator and would 400, so
  // mirror the disabled <option> in the picker and omit the button.
  const diff = _wikiView && _wikiView.diff;
  if (!diff || diff._error || !_wikiVendorRenderable(_wikiVendor)) return '';
  const exists = !!diff.exists;
  const label = exists ? t('settings.ctx.wiki_reseed') : t('settings.ctx.wiki_seed');
  return '<div class="wiki-section wiki-seed-action">'
    + '<button type="button" class="btn-ghost wiki-seed-btn" id="wiki-seed-btn"'
    + ` data-exists="${exists ? '1' : '0'}">${escapeHtml(label)}</button>`
    + `<span class="wiki-seed-hint">${escapeHtml(t('settings.ctx.wiki_seed_hint'))}</span>`
    + '</div>';
}

function _bindWikiSeedAction() {
  const btn = qs('wiki-seed-btn');
  if (!btn) return;
  btn.addEventListener('click', () => { _onWikiSeedClick(btn.dataset.exists === '1'); });
}

async function _onWikiSeedClick(exists) {
  if (!_wikiActive || !_wikiVendor) return;
  const { type, name } = _wikiActive;
  const vendor = _wikiVendor;
  if (exists) {
    // Re-seed overwrites the user's (possibly edited) override — a .bak keeps
    // the previous content, but gate the clobber behind a confirm. A first-time
    // seed (no existing file) stays one click.
    const ok = await showConfirm({
      title: t('settings.ctx.wiki_reseed_confirm_title'),
      message: t('settings.ctx.wiki_reseed_confirm_msg', {
        vendor, type: _wikiTypeLabel(type), name,
      }),
      confirmText: t('settings.ctx.wiki_reseed_confirm_ok'),
      cancelText: t('modal.cancel_btn'),
      danger: true,
    });
    if (!ok) return;
  }
  await _seedWikiOverride(type, name, vendor, exists);
}

async function _seedWikiOverride(type, name, vendor, force) {
  const url = `/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}/override`;
  let res;
  try {
    const csrf = await ensureCsrfToken();
    const headers = { 'Content-Type': 'application/json' };
    if (csrf) headers['X-Memtomem-CSRF'] = csrf;
    res = await fetch(url, { method: 'POST', headers, body: JSON.stringify({ vendor, force }) });
  } catch (err) {
    showToast(t('settings.ctx.wiki_seed_failed', { error: String((err && err.message) || err) }), 'error');
    return;
  }
  if (!res.ok) {
    showToast(t('settings.ctx.wiki_seed_failed', { error: await _wikiErrDetail(res) }), 'error');
    return;
  }
  const data = await res.json();
  // Repaint the HEAD dirty badge from the response without re-listing (a11y: no
  // list re-render / focus loss — see feedback_ctx_a11y_conventions).
  if (_wikiData) { _wikiData.is_dirty = !!data.wiki_dirty; _renderWikiHead(); }
  const dropped = (data && data.dropped) || [];
  showToast(
    dropped.length
      ? t('settings.ctx.wiki_seed_ok_dropped', { vendor, fields: dropped.join(', ') })
      : t('settings.ctx.wiki_seed_ok', { vendor }),
    'success',
  );
  // Refresh diff/lint: the override now exists (diff → in-sync) and the button
  // flips to "Re-seed". Guard against a vendor/asset switch mid-request.
  if (_wikiActive && _wikiActive.type === type && _wikiActive.name === name
      && _wikiVendor === vendor) {
    await _loadWikiVendorView(type, name, vendor);
  }
}

// --- Install / update into a project (E-3, dev tier only) -------------------
// Web parity of `mm context install` / `mm context update`: these READ the
// host-global wiki but WRITE into a project's .memtomem/, so — unlike the wiki
// browser, which has NO project bar — the action carries its own lightweight
// project <select> local to the detail pane. The roster + scope helpers live in
// context-gateway.js; every reference is typeof-guarded so wiki.js never hard-
// depends on that script's load order (and the vitest harness can stub them).

function _wikiScopeList() {
  if (typeof _ctxProjectsCache !== 'undefined' && Array.isArray(_ctxProjectsCache)) {
    return _ctxProjectsCache;
  }
  return [];
}

function _wikiActiveScopeId() {
  return (typeof _ctxActiveScopeId === 'string') ? _ctxActiveScopeId : '';
}

function _wikiScopeParam(id) {
  if (typeof _ctxScopeParam === 'function') return _ctxScopeParam(id);
  return id ? `scope_id=${encodeURIComponent(id)}` : '';
}

function _wikiScopeOptionLabel(scope) {
  if (typeof _ctxScopeDisplayLabel === 'function') return _ctxScopeDisplayLabel(scope);
  return scope.label || scope.scope_id || '';
}

function _wikiScopeLabelById(id) {
  if (typeof _ctxScopeDisplayLabelById === 'function') return _ctxScopeDisplayLabelById(id);
  return id || t('settings.ctx.server_cwd');
}

async function _wikiEnsureProjects() {
  // Lazily populate the roster cache so the project <select> has options. The
  // cache already carries a Server-CWD entry; an empty cache means it was never
  // fetched (or context-gateway.js isn't loaded — tests pre-seed it instead).
  if (_wikiScopeList().length) return;
  if (typeof _ctxFetchProjects === 'function') {
    try { await _ctxFetchProjects(); } catch { /* roster fetch is best-effort */ }
  }
}

function _renderWikiInstallAction() {
  // Dev-tier only: install/update writes into a project's git-tracked tree (the
  // POST routes only mount in dev). Asset-level (vendor-independent), so this
  // renders in the detail head, NOT the per-vendor view.
  if (!_wikiDevMode() || !_wikiActive) return '';
  let scopes = _wikiScopeList().filter((s) => !s.missing);
  if (!scopes.some((s) => (s.scope_id || '') === '')) {
    // Always offer Server-CWD (the CLI default target) even with an empty roster.
    scopes = [{ scope_id: '', label: t('settings.ctx.server_cwd'), root: '' }, ...scopes];
  }
  const options = scopes.map((s) => {
    const id = s.scope_id || '';
    const sel = id === (_wikiInstallScopeId || '') ? ' selected' : '';
    return `<option value="${escapeHtml(id)}"${sel}>${escapeHtml(_wikiScopeOptionLabel(s))}</option>`;
  }).join('');
  return '<div class="wiki-section wiki-install-action">'
    + '<label class="wiki-install-project-row">'
    + `<span>${escapeHtml(t('settings.ctx.wiki_install_project'))}</span>`
    + `<select class="wiki-vendor-select" id="wiki-install-project">${options}</select></label>`
    + '<div class="wiki-install-buttons">'
    + `<button type="button" class="btn-ghost" id="wiki-install-btn">`
    + `${escapeHtml(t('settings.ctx.wiki_install'))}</button>`
    + `<button type="button" class="btn-ghost" id="wiki-update-btn">`
    + `${escapeHtml(t('settings.ctx.wiki_update'))}</button>`
    + '</div>'
    + `<span class="wiki-seed-hint">${escapeHtml(t('settings.ctx.wiki_install_hint'))}</span>`
    + '</div>';
}

function _bindWikiInstallAction() {
  const sel = qs('wiki-install-project');
  if (sel) sel.addEventListener('change', () => { _wikiInstallScopeId = sel.value || ''; });
  const installBtn = qs('wiki-install-btn');
  if (installBtn) installBtn.addEventListener('click', () => { _onWikiInstallOrUpdate('install'); });
  const updateBtn = qs('wiki-update-btn');
  if (updateBtn) updateBtn.addEventListener('click', () => { _onWikiInstallOrUpdate('update'); });
}

async function _onWikiInstallOrUpdate(verb) {
  if (!_wikiActive) return;
  const { type, name } = _wikiActive;
  await _installWikiAsset(type, name, _wikiInstallScopeId || '', false, verb);
}

async function _installWikiAsset(type, name, scopeId, force, verb) {
  const base = `/api/context/${encodeURIComponent(type)}/${encodeURIComponent(name)}/${verb}`;
  const scopeParam = _wikiScopeParam(scopeId);
  const url = scopeParam ? `${base}?${scopeParam}` : base;
  let res;
  try {
    const csrf = await ensureCsrfToken();
    const headers = { 'Content-Type': 'application/json' };
    if (csrf) headers['X-Memtomem-CSRF'] = csrf;
    const init = { method: 'POST', headers };
    if (verb === 'update') init.body = JSON.stringify({ force });
    res = await fetch(url, init);
  } catch (err) {
    showToast(t('settings.ctx.wiki_install_failed', { error: String((err && err.message) || err) }), 'error');
    return;
  }
  const project = _wikiScopeLabelById(scopeId);
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON body */ }
    const reason = (body && body.detail && body.detail.reason_code) || '';
    if (verb === 'update' && reason === 'stale_install' && !force) {
      // Dirty dest: offer a force overwrite (each edited file is kept as a .bak).
      const ok = await showConfirm({
        title: t('settings.ctx.wiki_force_confirm_title'),
        message: t('settings.ctx.wiki_force_confirm_msg', {
          type: _wikiTypeLabel(type), name, project,
        }),
        confirmText: t('settings.ctx.wiki_force_confirm_ok'),
        cancelText: t('modal.cancel_btn'),
        danger: true,
      });
      if (ok) await _installWikiAsset(type, name, scopeId, true, 'update');
      return;
    }
    if (reason === 'already_installed') {
      showToast(t('settings.ctx.wiki_already_installed', { type: _wikiTypeLabel(type), name }), 'error');
      return;
    }
    if (reason === 'not_installed') {
      showToast(t('settings.ctx.wiki_not_installed', { type: _wikiTypeLabel(type), name }), 'error');
      return;
    }
    const detail = (body && body.detail && body.detail.message) || `HTTP ${res.status}`;
    showToast(t('settings.ctx.wiki_install_failed', { error: detail }), 'error');
    return;
  }
  const data = await res.json();
  if (verb === 'install') {
    showToast(t('settings.ctx.wiki_install_ok', { type: _wikiTypeLabel(type), name, project }), 'success');
  } else if (data && data.was_no_op) {
    showToast(t('settings.ctx.wiki_update_unchanged', { type: _wikiTypeLabel(type), name, project }), 'success');
  } else {
    showToast(t('settings.ctx.wiki_update_ok', { type: _wikiTypeLabel(type), name, project }), 'success');
  }
}

function _renderWikiVendorView() {
  const view = qs('wiki-vendor-view');
  if (!view) return;
  if (!_wikiView) { view.innerHTML = ''; return; }
  view.innerHTML = _renderDiffSection(_wikiView.diff)
    + _renderLintSection(_wikiView.lint)
    + _renderWikiSeedAction();
  _bindWikiSeedAction();
}

function _renderWikiDetail() {
  const el = qs('wiki-detail');
  if (!el || !_wikiActive) return;
  const { type, name } = _wikiActive;
  const item = ((_wikiData && _wikiData.items) || []).find(
    (i) => i.type === type && i.name === name,
  );
  const vendors = (item && item.vendors) || [];
  let html = '<div class="wiki-detail-head">';
  html += `<h3 class="wiki-detail-name">${escapeHtml(name)} `
    + `<span class="wiki-detail-type">${escapeHtml(_wikiTypeLabel(type))}</span></h3>`;
  if (vendors.length) {
    html += `<label class="wiki-vendor-row"><span>${escapeHtml(t('settings.ctx.wiki_vendor'))}</span>`;
    html += '<select class="wiki-vendor-select" id="wiki-vendor-select">';
    vendors.forEach((v) => {
      const sel = v.vendor === _wikiVendor ? ' selected' : '';
      const dis = v.renderable ? '' : ' disabled';
      const label = v.renderable
        ? v.vendor
        : `${v.vendor} (${t('settings.ctx.wiki_vendor_unsupported')})`;
      html += `<option value="${escapeHtml(v.vendor)}"${sel}${dis}>${escapeHtml(label)}</option>`;
    });
    html += '</select></label>';
  }
  html += _renderWikiInstallAction();
  html += '</div><div id="wiki-vendor-view"></div>';
  el.innerHTML = html;
  show(el);
  _renderWikiVendorView();
  _bindWikiInstallAction();
  const select = qs('wiki-vendor-select');
  if (select) {
    select.addEventListener('change', () => {
      _wikiVendor = select.value;
      _loadWikiVendorView(type, name, _wikiVendor);
    });
  }
}

async function _loadWikiVendorView(type, name, vendor) {
  const view = qs('wiki-vendor-view');
  if (!view || !vendor) return;
  // Drop the previous vendor's cached diff/lint immediately so a langchange
  // landing mid-fetch repaints an empty pane rather than the OLD vendor's
  // details under the newly-selected vendor (Codex review on PR-E).
  _wikiView = null;
  const seq = ++_wikiDetailSeq;
  panelLoading(view);
  const base = `/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}`;
  const vq = `?vendor=${encodeURIComponent(vendor)}`;
  try {
    const [diffRes, lintRes] = await Promise.all([
      fetch(`${base}/diff${vq}`),
      fetch(`${base}/lint${vq}`),
    ]);
    if (seq !== _wikiDetailSeq) return;
    const diff = diffRes.ok ? await diffRes.json() : { _error: await _wikiErrDetail(diffRes) };
    const lint = lintRes.ok ? await lintRes.json() : { _error: await _wikiErrDetail(lintRes) };
    if (seq !== _wikiDetailSeq) return;
    _wikiView = { diff, lint };
    _renderWikiVendorView();
  } catch (err) {
    if (_wikiIsAbort(err) || seq !== _wikiDetailSeq) return;
    _wikiView = null;
    view.innerHTML = '';
    if (typeof showToast === 'function') {
      showToast(t('settings.ctx.wiki_detail_failed', { error: String((err && err.message) || err) }), 'error');
    }
  }
}

async function loadWikiDetail(type, name) {
  _wikiActive = { type, name };
  _wikiView = null;
  const listEl = qs('wiki-list');
  if (listEl) {
    listEl.querySelectorAll('.wiki-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.type === type && b.dataset.name === name);
    });
  }
  const item = ((_wikiData && _wikiData.items) || []).find(
    (i) => i.type === type && i.name === name,
  );
  const vendors = (item && item.vendors) || [];
  const firstRenderable = vendors.find((v) => v.renderable) || vendors[0];
  _wikiVendor = firstRenderable ? firstRenderable.vendor : null;
  // Populate the project roster so the dev-tier install/update picker has
  // options (no-op when already cached or not in dev mode).
  if (_wikiDevMode()) await _wikiEnsureProjects();
  _renderWikiDetail();
  if (_wikiVendor) await _loadWikiVendorView(type, name, _wikiVendor);
}

async function loadWiki() {
  const listEl = qs('wiki-list');
  if (!listEl) return;
  const seq = ++_wikiListSeq;
  if (_wikiListAbort) { try { _wikiListAbort.abort(); } catch { /* best-effort */ } }
  _wikiListAbort = (typeof AbortController === 'function') ? new AbortController() : null;
  _wikiActive = null;
  _wikiView = null;
  _wikiInstallScopeId = _wikiActiveScopeId();
  const detailEl = qs('wiki-detail');
  if (detailEl) { hide(detailEl); detailEl.innerHTML = ''; }
  panelLoading(listEl);
  try {
    const res = await fetch('/api/wiki', {
      signal: _wikiListAbort ? _wikiListAbort.signal : undefined,
    });
    if (seq !== _wikiListSeq) return;
    if (res.status === 404) {
      // Absent wiki (ADR-0008 Invariant 3): onboarding empty-state, NOT an
      // error toast — the user just hasn't run `mm wiki init` yet.
      _wikiData = null;
      _wikiAbsent = true;
      _renderWikiAbsent();
      return;
    }
    if (!res.ok) {
      const detail = await _wikiErrDetail(res);
      _wikiData = null;
      _wikiAbsent = false;
      listEl.innerHTML = '';
      if (typeof showToast === 'function') {
        showToast(t('settings.ctx.wiki_load_failed', { error: detail }), 'error');
      }
      return;
    }
    const data = await res.json();
    if (seq !== _wikiListSeq) return;
    _wikiData = data;
    _wikiAbsent = false;
    _renderWikiHead();
    _renderWikiList();
  } catch (err) {
    if (_wikiIsAbort(err) || seq !== _wikiListSeq) return;
    _wikiData = null;
    _wikiAbsent = false;
    listEl.innerHTML = '';
    if (typeof showToast === 'function') {
      showToast(t('settings.ctx.wiki_load_failed', { error: String((err && err.message) || err) }), 'error');
    }
  }
}

// Repaint cached content in the newly selected language (i18n-dynamic-render).
window.addEventListener('langchange', () => {
  if (_wikiAbsent) { _renderWikiAbsent(); return; }
  if (!_wikiData) return;
  _renderWikiHead();
  _renderWikiList();
  if (_wikiActive) _renderWikiDetail();
});

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
let _wikiView = null; // { diff, lint, override } last fetched for the open vendor
let _wikiInstallScopeId = ''; // E-3: project picked for install/update ('' = Server CWD)
// Editor-A (ADR-0027): override-edit mode for the open vendor. `_wikiEditDraft`
// holds the in-progress textarea value so a langchange repaint doesn't reset it.
let _wikiEditing = false;
let _wikiEditDraft = null;
// Editor-B (ADR-0027): canonical-edit mode for the open asset. The canonical is
// artifact-level (NOT per-vendor), so this state lives beside the asset, not the
// vendor: `_wikiCanonical` caches GET …/canonical ({ content, mtime_ns } or
// { _error }); `_wikiCanonDraft` keeps the in-progress textarea so a langchange
// repaint doesn't reset it.
let _wikiCanonical = null;
let _wikiCanonEditing = false;
let _wikiCanonDraft = null;

// Commit affordance (ADR-0027 §3): per-asset map of the targets the user has
// Saved this session, keyed `${type}/${name}` → { canonical|override:<vendor>:
// mtime_ns }. The mtime_ns is the token the editor last saw (from the Save
// response), so the commit can detect an external same-file edit between Save and
// Commit (→409). The wiki is host-global, so no project component in the key.
const _wikiPending = {};

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
  // No wiki on disk → nothing to commit → clear any stale nav dot (#1417).
  _renderWikiNavBadge(false);
}

// Nav/glance dirty badge (#1417). The section body already shows the dirty
// state in #wiki-head, but from any OTHER Context Gateway section the sidebar
// gave no signal that the wiki has uncommitted edits `mm context install` won't
// reach yet. This toggles a small dot on the Wiki nav button, driven by the
// SAME is_dirty the section tracks: every dirty-state repaint flows through
// _renderWikiHead / _renderWikiAbsent, so both call here. a11y: just toggle the
// element's `hidden` (no aria-live) — the dot's aria-label is read when a SR
// reaches the nav button, but its appearance is never announced (no spam, per
// the ctx dashboard conventions).
function _renderWikiNavBadge(isDirty) {
  const badge = document.querySelector(
    '.settings-nav-btn[data-section="ctx-wiki"] .wiki-nav-dirty',
  );
  if (badge) badge.hidden = !isDirty;
}

// Eager probe so the nav badge is correct on a cold gateway open, before the
// Wiki section has been visited (a wiki left dirty in a prior session). Cheap —
// HEAD + `git status` only, no asset list. Best-effort: a missing wiki
// (present:false) or any fetch failure leaves the dot hidden rather than
// surfacing an error. Called from app.js on Context Gateway activation.
async function _probeWikiNavStatus() {
  try {
    const res = await fetch('/api/wiki/status');
    if (!res.ok) return;
    const data = await res.json();
    _renderWikiNavBadge(!!data.is_dirty);
  } catch { /* glance signal only — never block the gateway on it */ }
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
  // Commit affordance (ADR-0027 §3): when the active asset has Saved-but-
  // uncommitted edits this session (dev tier), offer an explicit Commit. Save and
  // commit are two acts — this is never auto-fired on Save.
  if (_wikiDevMode() && _wikiActivePendingCount() > 0) {
    html += ' <button type="button" class="btn-ghost wiki-commit-btn" id="wiki-commit-btn">'
      + `${escapeHtml(t('settings.ctx.wiki_commit'))}</button>`;
  }
  headEl.innerHTML = html;
  const commitBtn = qs('wiki-commit-btn');
  if (commitBtn) commitBtn.addEventListener('click', () => { _openWikiCommitModal(); });
  // Mirror the dirty state onto the nav/glance dot (#1417). Every site that
  // flips _wikiData.is_dirty (load + the Save/seed/commit responses) repaints
  // the head, so this single hook keeps the sidebar in sync without touching
  // each call site.
  _renderWikiNavBadge(!!_wikiData.is_dirty);
}

// --- Commit affordance (ADR-0027 §3, dev tier only) -------------------------
// An explicit, opt-in Commit of the asset's Saved-but-uncommitted edits as an
// ISOLATED git commit (the server stages only the resolved target paths via an
// out-of-worktree temp index + ref CAS). Mounts in the HEAD row beside the dirty
// badge; the targets are whatever the user Saved this session for the open asset.

let _wikiCommitRelease = null;

function _wikiAssetKey(type, name) { return `${type}/${name}`; }

function _wikiPendingAdd(type, name, targetKey, mtimeNs) {
  const key = _wikiAssetKey(type, name);
  (_wikiPending[key] = _wikiPending[key] || {})[targetKey] = String(mtimeNs);
}

function _wikiPendingClear(type, name) { delete _wikiPending[_wikiAssetKey(type, name)]; }

function _wikiActivePendingCount() {
  if (!_wikiActive) return 0;
  const map = _wikiPending[_wikiAssetKey(_wikiActive.type, _wikiActive.name)];
  return map ? Object.keys(map).length : 0;
}

function _wikiPendingTargetsList(type, name) {
  const map = _wikiPending[_wikiAssetKey(type, name)] || {};
  return Object.keys(map).map((key) => {
    if (key === 'canonical') return { kind: 'canonical', mtime_ns: map[key] };
    return { kind: 'override', vendor: key.slice('override:'.length), mtime_ns: map[key] };
  });
}

function _closeWikiCommitModal() {
  const modal = qs('wiki-commit-modal');
  if (modal) hide(modal);
  if (_wikiCommitRelease) {
    try { _wikiCommitRelease(); } catch { /* release is idempotent */ }
    _wikiCommitRelease = null;
  }
}

function _openWikiCommitModal() {
  if (!_wikiActive) return;
  const { type, name } = _wikiActive;
  const targets = _wikiPendingTargetsList(type, name);
  if (!targets.length) return;
  const modal = qs('wiki-commit-modal');
  if (!modal) return;
  const summaryEl = qs('wiki-commit-targets');
  const msgEl = qs('wiki-commit-msg');
  const errEl = qs('wiki-commit-error');
  const okBtn = qs('wiki-commit-ok-btn');
  const cancelBtn = qs('wiki-commit-cancel-btn');
  if (errEl) { errEl.hidden = true; errEl.textContent = ''; }
  if (summaryEl) {
    summaryEl.textContent = t('settings.ctx.wiki_commit_summary', {
      count: targets.length, asset: `${type}/${name}`,
    });
  }
  if (msgEl) msgEl.value = t('settings.ctx.wiki_commit_msg_default', { asset: `${type}/${name}` });
  // Assign via .onclick (not addEventListener) so re-opening never stacks handlers.
  if (cancelBtn) cancelBtn.onclick = () => { _closeWikiCommitModal(); };
  if (okBtn) { okBtn.disabled = false; okBtn.onclick = () => { _doWikiCommit(false); }; }
  show(modal);
  _wikiCommitRelease = openModalA11y(modal);
  registerModalCloser(modal, () => { _closeWikiCommitModal(); });
  if (msgEl) { msgEl.focus(); msgEl.select(); }
}

async function _doWikiCommit(force) {
  if (!_wikiActive) { _closeWikiCommitModal(); return; }
  const { type, name } = _wikiActive;
  const targets = _wikiPendingTargetsList(type, name);
  if (!targets.length) { _closeWikiCommitModal(); return; }
  const msgEl = qs('wiki-commit-msg');
  const errEl = qs('wiki-commit-error');
  const okBtn = qs('wiki-commit-ok-btn');
  const message = (msgEl && msgEl.value) || '';
  const expectedHead = (_wikiData && _wikiData.wiki_head) || '';
  const showErr = (text) => { if (errEl) { errEl.hidden = false; errEl.textContent = text; } };
  if (okBtn) okBtn.disabled = true;
  const url = `/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}/commit`;
  let res;
  try {
    const csrf = await ensureCsrfToken();
    const headers = { 'Content-Type': 'application/json' };
    if (csrf) headers['X-Memtomem-CSRF'] = csrf;
    res = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify({ expected_head: expectedHead, message, targets, force }),
    });
  } catch (err) {
    if (okBtn) okBtn.disabled = false;
    showErr(t('settings.ctx.wiki_commit_failed', { error: String((err && err.message) || err) }));
    return;
  }
  if (okBtn) okBtn.disabled = false;
  if (res.status === 503) { showErr(t('settings.ctx.wiki_commit_busy')); return; }
  if (res.status === 409) {
    let body = {};
    try { body = await res.json(); } catch { /* non-JSON */ }
    if (body.reason_code === 'stale_target') {
      // An external editor changed a target since Save. Offer to commit the
      // current on-disk bytes anyway (force, WARNING-audited server-side).
      // The confirm dialog (#confirm-modal) shares the .modal-overlay z-index
      // and sits earlier in the DOM, so it would stack UNDER this modal
      // (openModalA11y orders by DOM, not z-index) — hide the commit modal
      // for the disclosure and restore it on decline (the move/copy
      // needs_confirmation precedent in context-gateway.js).
      const modal = qs('wiki-commit-modal');
      if (modal) hide(modal);
      const ok = await showConfirm({
        title: t('settings.ctx.wiki_commit_target_changed_title'),
        message: t('settings.ctx.wiki_commit_target_changed_msg'),
        confirmText: t('settings.ctx.wiki_commit_force_ok'),
        cancelText: t('modal.cancel_btn'),
        danger: true,
      });
      if (ok) {
        // The force retry re-reads the (hidden) modal's message field and
        // closes it properly on success via _closeWikiCommitModal.
        await _doWikiCommit(true);
      } else {
        if (modal) show(modal);
        showErr(t('settings.ctx.wiki_commit_target_changed_msg'));
      }
      return;
    }
    // stale_head: HEAD moved under us. CLEAR the pending targets — their tokens
    // were captured against the OLD head, so a one-click retry against the NEW
    // head would let the CAS pass for bytes that were never reconciled with the
    // intervening change. Forcing a fresh Save (which re-derives the token against
    // the current state) before another commit is the safe resolution. The
    // working-tree edits are untouched on disk.
    _wikiPendingClear(type, name);
    if (_wikiData && body.wiki_head) _wikiData.wiki_head = body.wiki_head;
    _renderWikiHead();
    _closeWikiCommitModal();
    showToast(t('settings.ctx.wiki_commit_head_moved'), 'error');
    return;
  }
  if (!res.ok) { showErr(t('settings.ctx.wiki_commit_failed', { error: await _wikiErrDetail(res) })); return; }
  const data = await res.json();
  // Update HEAD + dirty from the server's read-back (no list re-render → no focus
  // loss), clear the asset's pending targets, repaint the head, close the modal.
  if (_wikiData) {
    if (data.wiki_head) _wikiData.wiki_head = data.wiki_head;
    _wikiData.is_dirty = !!data.wiki_dirty;
  }
  _wikiPendingClear(type, name);
  _renderWikiHead();
  _closeWikiCommitModal();
  if (data.committed === false) {
    showToast(t('settings.ctx.wiki_commit_nothing'), 'info');
    return;
  }
  showToast(t('settings.ctx.wiki_commit_ok', { sha: (data.wiki_head || '').slice(0, 12) }), 'success');
  if (data.privacy_warning) {
    showToast(t('settings.ctx.wiki_commit_privacy_warn', { count: data.privacy_warning }), 'error');
  }
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

// --- Override editor (ADR-0027 Editor-A, dev tier only) ---------------------
// Edit an EXISTING vendor override in place: a read pane (<pre>) seeded from
// GET …/override, an Edit toggle → textarea, and Save → PUT …/override under an
// optimistic mtime_ns guard (the ctx skill-editor pattern). Save writes + leaves
// the wiki dirty; it never commits (the commit affordance is the deferred PR).
// Self-contained here — wiki.js avoids a hard load-order dep on context-gateway.js.

function _renderWikiOverrideEditor() {
  const ov = _wikiView && _wikiView.override;
  // Only when dev + renderable + the override actually exists (a not-yet-seeded
  // override is created via the Seed button, which renders from canonical).
  if (!_wikiDevMode() || !ov || ov._error || !ov.exists) return '';
  if (!_wikiVendorRenderable(_wikiVendor)) return '';
  const heading = `<h4>${escapeHtml(t('settings.ctx.wiki_override_title'))}</h4>`;
  if (!_wikiEditing) {
    return '<div class="wiki-section wiki-override-editor">'
      + heading
      + `<pre class="wiki-override-pre">${escapeHtml(ov.content)}</pre>`
      + '<div class="wiki-override-actions">'
      + '<button type="button" class="btn-ghost" id="wiki-override-edit-btn">'
      + `${escapeHtml(t('settings.ctx.wiki_override_edit'))}</button></div>`
      + `<span class="wiki-seed-hint">${escapeHtml(t('settings.ctx.wiki_override_hint'))}</span>`
      + '</div>';
  }
  const draft = _wikiEditDraft != null ? _wikiEditDraft : ov.content;
  return '<div class="wiki-section wiki-override-editor">'
    + heading
    + '<div class="wiki-conflict-banner" id="wiki-conflict-banner" hidden></div>'
    + '<textarea class="wiki-override-area" id="wiki-override-content" '
    + `aria-label="${escapeHtml(t('settings.ctx.wiki_override_title'))}" `
    + `data-mtime-ns="${escapeHtml(String(ov.mtime_ns))}">${escapeHtml(draft)}</textarea>`
    + '<div class="wiki-override-actions">'
    + '<button type="button" class="btn-ghost" id="wiki-override-cancel-btn">'
    + `${escapeHtml(t('modal.cancel_btn'))}</button>`
    + '<button type="button" class="btn-primary" id="wiki-override-save-btn">'
    + `${escapeHtml(t('settings.ctx.wiki_override_save'))}</button></div>`
    + '</div>';
}

function _bindWikiOverrideEditor() {
  const editBtn = qs('wiki-override-edit-btn');
  if (editBtn) {
    editBtn.addEventListener('click', () => {
      _wikiEditing = true;
      _wikiEditDraft = null;
      _renderWikiVendorView();
    });
  }
  const cancelBtn = qs('wiki-override-cancel-btn');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      _wikiEditing = false;
      _wikiEditDraft = null;
      _renderWikiVendorView();
    });
  }
  const saveBtn = qs('wiki-override-save-btn');
  if (saveBtn) saveBtn.addEventListener('click', () => { _onWikiOverrideSave(); });
}

async function _onWikiOverrideSave() {
  if (!_wikiActive || !_wikiVendor) return;
  const ta = qs('wiki-override-content');
  if (!ta) return;
  await _saveWikiOverride(
    _wikiActive.type, _wikiActive.name, _wikiVendor, ta.value, ta.dataset.mtimeNs || '0', false,
  );
}

async function _saveWikiOverride(type, name, vendor, content, mtimeNs, force) {
  const url = `/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}/override`;
  let res;
  try {
    const csrf = await ensureCsrfToken();
    const headers = { 'Content-Type': 'application/json' };
    if (csrf) headers['X-Memtomem-CSRF'] = csrf;
    res = await fetch(url, {
      method: 'PUT',
      headers,
      body: JSON.stringify({ vendor, content, mtime_ns: mtimeNs, force }),
    });
  } catch (err) {
    showToast(
      t('settings.ctx.wiki_override_save_failed', { error: String((err && err.message) || err) }),
      'error',
    );
    return;
  }
  if (res.status === 409) { await _onWikiOverrideConflict(type, name, vendor, content); return; }
  if (res.status === 503) { showToast(t('settings.ctx.wiki_override_busy'), 'error'); return; }
  if (!res.ok) {
    showToast(t('settings.ctx.wiki_override_save_failed', { error: await _wikiErrDetail(res) }), 'error');
    return;
  }
  const data = await res.json();
  // Record the saved override as a pending commit target (its fresh mtime_ns is
  // the per-target token the commit re-checks). Then repaint the HEAD dirty badge
  // + Commit button from the response without re-listing (a11y: no list
  // re-render / focus loss — feedback_ctx_a11y_conventions).
  _wikiPendingAdd(type, name, `override:${vendor}`, data.mtime_ns);
  if (_wikiData) { _wikiData.is_dirty = !!data.wiki_dirty; _renderWikiHead(); }
  _wikiEditing = false;
  _wikiEditDraft = null;
  if (data.privacy_warning) {
    showToast(
      t('settings.ctx.wiki_override_privacy_warn', { vendor, count: data.privacy_warning }),
      'error',
    );
  } else {
    showToast(t('settings.ctx.wiki_override_saved', { vendor }), 'success');
  }
  // Refresh diff/lint + the read pane: the override changed. Guard against a
  // vendor/asset switch mid-request.
  if (_wikiActive && _wikiActive.type === type && _wikiActive.name === name
      && _wikiVendor === vendor) {
    await _loadWikiVendorView(type, name, vendor);
  }
}

async function _onWikiOverrideConflict(type, name, vendor, draftContent) {
  // The override changed on disk between read and save. Fetch the current bytes
  // so the user can compare, then offer Reload (discard my edits) or Force save
  // (re-PUT my draft with force; a .bak keeps the on-disk copy).
  let fresh = null;
  try {
    const base = `/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}`;
    const r = await fetch(`${base}/override?vendor=${encodeURIComponent(vendor)}`);
    if (r.ok) fresh = await r.json();
  } catch { /* best-effort — the banner still offers reload/force */ }
  const banner = qs('wiki-conflict-banner');
  if (!banner) return; // edit pane gone (vendor/asset switched mid-conflict)
  const freshContent = fresh ? (fresh.content || '') : '';
  const freshMtime = fresh ? String(fresh.mtime_ns || '0') : '0';
  banner.hidden = false;
  banner.innerHTML = '<p class="wiki-conflict-msg" role="alert">'
    + `${escapeHtml(t('settings.ctx.wiki_override_conflict_msg'))}</p>`
    + `<pre class="wiki-override-pre wiki-conflict-fresh">${escapeHtml(freshContent)}</pre>`
    + '<div class="wiki-override-actions">'
    + '<button type="button" class="btn-ghost" id="wiki-conflict-reload-btn">'
    + `${escapeHtml(t('settings.ctx.wiki_override_conflict_reload'))}</button>`
    + '<button type="button" class="btn-danger" id="wiki-conflict-force-btn">'
    + `${escapeHtml(t('settings.ctx.wiki_override_conflict_force'))}</button></div>`;
  const reloadBtn = qs('wiki-conflict-reload-btn');
  if (reloadBtn) {
    reloadBtn.addEventListener('click', () => {
      const ta = qs('wiki-override-content');
      if (ta) { ta.value = freshContent; ta.dataset.mtimeNs = freshMtime; }
      banner.hidden = true;
      banner.innerHTML = '';
    });
  }
  const forceBtn = qs('wiki-conflict-force-btn');
  if (forceBtn) {
    forceBtn.addEventListener('click', () => {
      _saveWikiOverride(type, name, vendor, draftContent, freshMtime, true);
    });
  }
}

// --- Canonical editor (ADR-0027 Editor-B, dev tier only) --------------------
// Edit the base CANONICAL (SKILL.md / agent.md / command.md) in place. Unlike
// the per-vendor override editor, the canonical is ARTIFACT-level: it renders in
// the detail head (not the per-vendor view), and a successful save re-derives
// EVERY vendor's diff/lint baseline (render_seed_bytes), so it reloads both the
// read pane AND the open vendor view. The server parse-gates agents/commands
// (layout="dir") → a 400 means an unparseable canonical was rejected and nothing
// was written. Save writes + leaves the wiki dirty; it never commits (the commit
// affordance is the deferred §3 PR).

function _renderWikiCanonicalEditor() {
  // Repaints ONLY the #wiki-canonical-editor host (no detail re-render → no focus
  // loss). Called from _renderWikiDetail (after the host exists), _loadWikiCanonical
  // (after fetch), and the Edit/Cancel handlers.
  const host = qs('wiki-canonical-editor');
  if (!host) return;
  const cn = _wikiCanonical;
  // Dev-only, and only once the canonical has loaded (null = still fetching).
  if (!_wikiDevMode() || !_wikiActive || !cn) { host.innerHTML = ''; return; }
  const heading = `<h4>${escapeHtml(t('settings.ctx.wiki_canonical_title'))}</h4>`;
  if (cn._error) {
    host.innerHTML = '<div class="wiki-section wiki-canonical-editor">'
      + heading
      + `<div class="wiki-error">${escapeHtml(cn._error)}</div></div>`;
    return;
  }
  // A well-formed canonical GET always carries a string `content`; anything else
  // (a malformed/empty body) is not renderable as a read pane — show nothing
  // rather than the literal "undefined".
  if (typeof cn.content !== 'string') { host.innerHTML = ''; return; }
  if (!_wikiCanonEditing) {
    host.innerHTML = '<div class="wiki-section wiki-canonical-editor">'
      + heading
      + `<pre class="wiki-override-pre">${escapeHtml(cn.content)}</pre>`
      + '<div class="wiki-override-actions">'
      + '<button type="button" class="btn-ghost" id="wiki-canonical-edit-btn">'
      + `${escapeHtml(t('settings.ctx.wiki_canonical_edit'))}</button></div>`
      + `<span class="wiki-seed-hint">${escapeHtml(t('settings.ctx.wiki_canonical_hint'))}</span>`
      + '</div>';
    _bindWikiCanonicalEditor();
    return;
  }
  const draft = _wikiCanonDraft != null ? _wikiCanonDraft : cn.content;
  host.innerHTML = '<div class="wiki-section wiki-canonical-editor">'
    + heading
    + '<div class="wiki-conflict-banner" id="wiki-canonical-conflict-banner" hidden></div>'
    + '<textarea class="wiki-override-area" id="wiki-canonical-content" '
    + `aria-label="${escapeHtml(t('settings.ctx.wiki_canonical_title'))}" `
    + `data-mtime-ns="${escapeHtml(String(cn.mtime_ns))}">${escapeHtml(draft)}</textarea>`
    + '<div class="wiki-override-actions">'
    + '<button type="button" class="btn-ghost" id="wiki-canonical-cancel-btn">'
    + `${escapeHtml(t('modal.cancel_btn'))}</button>`
    + '<button type="button" class="btn-primary" id="wiki-canonical-save-btn">'
    + `${escapeHtml(t('settings.ctx.wiki_canonical_save'))}</button></div>`
    + '</div>';
  _bindWikiCanonicalEditor();
}

function _bindWikiCanonicalEditor() {
  const editBtn = qs('wiki-canonical-edit-btn');
  if (editBtn) {
    editBtn.addEventListener('click', () => {
      _wikiCanonEditing = true;
      _wikiCanonDraft = null;
      _renderWikiCanonicalEditor();
    });
  }
  const cancelBtn = qs('wiki-canonical-cancel-btn');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      _wikiCanonEditing = false;
      _wikiCanonDraft = null;
      _renderWikiCanonicalEditor();
    });
  }
  const saveBtn = qs('wiki-canonical-save-btn');
  if (saveBtn) saveBtn.addEventListener('click', () => { _onWikiCanonicalSave(); });
}

async function _onWikiCanonicalSave() {
  if (!_wikiActive) return;
  const ta = qs('wiki-canonical-content');
  if (!ta) return;
  await _saveWikiCanonical(
    _wikiActive.type, _wikiActive.name, ta.value, ta.dataset.mtimeNs || '0', false,
  );
}

async function _saveWikiCanonical(type, name, content, mtimeNs, force) {
  const url = `/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}/canonical`;
  let res;
  try {
    const csrf = await ensureCsrfToken();
    const headers = { 'Content-Type': 'application/json' };
    if (csrf) headers['X-Memtomem-CSRF'] = csrf;
    res = await fetch(url, {
      method: 'PUT',
      headers,
      body: JSON.stringify({ content, mtime_ns: mtimeNs, force }),
    });
  } catch (err) {
    showToast(
      t('settings.ctx.wiki_canonical_save_failed', { error: String((err && err.message) || err) }),
      'error',
    );
    return;
  }
  if (res.status === 409) { await _onWikiCanonicalConflict(type, name, content); return; }
  if (res.status === 503) { showToast(t('settings.ctx.wiki_canonical_busy'), 'error'); return; }
  if (res.status === 400) {
    // Parse gate: an unparseable agent/command canonical. Surface the server's
    // (path-safe) message so the user can fix the frontmatter; nothing was written.
    showToast(
      t('settings.ctx.wiki_canonical_parse_failed', { error: await _wikiErrDetail(res) }),
      'error',
    );
    return;
  }
  if (!res.ok) {
    showToast(t('settings.ctx.wiki_canonical_save_failed', { error: await _wikiErrDetail(res) }), 'error');
    return;
  }
  const data = await res.json();
  // Record the saved canonical as a pending commit target, then repaint the HEAD
  // dirty badge + Commit button without re-listing (a11y: no focus loss).
  _wikiPendingAdd(type, name, 'canonical', data.mtime_ns);
  if (_wikiData) { _wikiData.is_dirty = !!data.wiki_dirty; _renderWikiHead(); }
  _wikiCanonEditing = false;
  _wikiCanonDraft = null;
  if (data.privacy_warning) {
    showToast(t('settings.ctx.wiki_canonical_privacy_warn', { count: data.privacy_warning }), 'error');
  } else {
    showToast(t('settings.ctx.wiki_canonical_saved'), 'success');
  }
  // The canonical changed → re-derive the read pane AND every vendor's diff/lint
  // baseline. Reload the canonical + the open vendor view. Guard against an asset
  // switch mid-request.
  if (_wikiActive && _wikiActive.type === type && _wikiActive.name === name) {
    await _loadWikiCanonical(type, name);
    if (_wikiVendor) await _loadWikiVendorView(type, name, _wikiVendor);
  }
}

async function _onWikiCanonicalConflict(type, name, draftContent) {
  // The canonical changed on disk between read and save. Fetch the current bytes
  // so the user can compare, then offer Reload (discard my edits) or Force save
  // (re-PUT my draft with force; a .bak keeps the on-disk copy).
  let fresh = null;
  try {
    const r = await fetch(`/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}/canonical`);
    if (r.ok) fresh = await r.json();
  } catch { /* best-effort — the banner still offers reload/force */ }
  const banner = qs('wiki-canonical-conflict-banner');
  if (!banner) return; // edit pane gone (asset switched mid-conflict)
  const freshContent = fresh ? (fresh.content || '') : '';
  const freshMtime = fresh ? String(fresh.mtime_ns || '0') : '0';
  banner.hidden = false;
  banner.innerHTML = '<p class="wiki-conflict-msg" role="alert">'
    + `${escapeHtml(t('settings.ctx.wiki_canonical_conflict_msg'))}</p>`
    + `<pre class="wiki-override-pre wiki-conflict-fresh">${escapeHtml(freshContent)}</pre>`
    + '<div class="wiki-override-actions">'
    + '<button type="button" class="btn-ghost" id="wiki-canonical-conflict-reload-btn">'
    + `${escapeHtml(t('settings.ctx.wiki_override_conflict_reload'))}</button>`
    + '<button type="button" class="btn-danger" id="wiki-canonical-conflict-force-btn">'
    + `${escapeHtml(t('settings.ctx.wiki_override_conflict_force'))}</button></div>`;
  const reloadBtn = qs('wiki-canonical-conflict-reload-btn');
  if (reloadBtn) {
    reloadBtn.addEventListener('click', () => {
      const ta = qs('wiki-canonical-content');
      if (ta) { ta.value = freshContent; ta.dataset.mtimeNs = freshMtime; }
      banner.hidden = true;
      banner.innerHTML = '';
    });
  }
  const forceBtn = qs('wiki-canonical-conflict-force-btn');
  if (forceBtn) {
    forceBtn.addEventListener('click', () => {
      _saveWikiCanonical(type, name, draftContent, freshMtime, true);
    });
  }
}

async function _loadWikiCanonical(type, name) {
  // Dev-tier only: the canonical GET mounts in dev. Fetch the base canonical
  // bytes for the artifact-level read pane, then repaint just that section. An
  // asset switch mid-fetch is dropped via the _wikiActive identity guard (the
  // canonical is asset-scoped, not vendor-scoped — the _wikiDetailSeq guard the
  // vendor view uses is the wrong axis here).
  if (!_wikiDevMode()) { _wikiCanonical = null; return; }
  let next;
  try {
    const r = await fetch(`/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}/canonical`);
    next = r.ok ? await r.json() : { _error: await _wikiErrDetail(r) };
  } catch (err) {
    if (_wikiIsAbort(err)) return;
    next = { _error: String((err && err.message) || err) };
  }
  if (!_wikiActive || _wikiActive.type !== type || _wikiActive.name !== name) return;
  _wikiCanonical = next;
  _renderWikiCanonicalEditor();
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
    if (reason === 'privacy_blocked') {
      // Localized copy, not the envelope's detail.message — that prose is
      // English-only server text and, being always non-empty, would shadow
      // every translation (the #1348 class).
      showToast(
        t('settings.ctx.wiki_install_privacy_blocked', { type: _wikiTypeLabel(type), name }),
        'error',
      );
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

function _wikiStashEditDraft() {
  // Capture the in-progress override edit before a full re-render destroys the
  // textarea, so the draft survives into _renderWikiOverrideEditor's reseed. The
  // langchange path rebuilds the WHOLE detail via _renderWikiDetail (wiping the
  // textarea) before _renderWikiVendorView runs, so this must be called at the
  // top of both re-render entry points — by the time the vendor view rebuilds,
  // the old textarea is already gone.
  if (!_wikiEditing) return;
  const ta = qs('wiki-override-content');
  if (ta) _wikiEditDraft = ta.value;
}

function _wikiStashCanonDraft() {
  // Editor-B counterpart of _wikiStashEditDraft: capture the in-progress canonical
  // edit before _renderWikiDetail's el.innerHTML wipes the textarea (e.g. a
  // langchange repaint), so the draft survives into _renderWikiCanonicalEditor's
  // reseed. The canonical editor lives in the detail head, only rebuilt by
  // _renderWikiDetail, so this is called there (not in _renderWikiVendorView).
  if (!_wikiCanonEditing) return;
  const ta = qs('wiki-canonical-content');
  if (ta) _wikiCanonDraft = ta.value;
}

function _renderWikiVendorView() {
  const view = qs('wiki-vendor-view');
  if (!view) return;
  if (!_wikiView) { view.innerHTML = ''; return; }
  _wikiStashEditDraft();
  view.innerHTML = _renderDiffSection(_wikiView.diff)
    + _renderLintSection(_wikiView.lint)
    + _renderWikiSeedAction()
    + _renderWikiOverrideEditor();
  _bindWikiSeedAction();
  _bindWikiOverrideEditor();
}

function _renderWikiDetail() {
  const el = qs('wiki-detail');
  if (!el || !_wikiActive) return;
  // Stash any in-progress override / canonical edit BEFORE el.innerHTML wipes the
  // textareas (e.g. a langchange repaint), so the drafts survive the rebuild below.
  _wikiStashEditDraft();
  _wikiStashCanonDraft();
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
  // Editor-B mount: the canonical editor is artifact-level (vendor-independent),
  // so it lives in the detail head, below the install action and above the
  // per-vendor diff/lint/override view. Filled async by _loadWikiCanonical (and
  // repainted from cache on langchange via the _renderWikiCanonicalEditor call
  // below).
  html += '<div id="wiki-canonical-editor"></div>';
  html += '</div><div id="wiki-vendor-view"></div>';
  el.innerHTML = html;
  show(el);
  _renderWikiCanonicalEditor();
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
  // details under the newly-selected vendor (Codex review on PR-E). Switching
  // vendor also abandons any in-progress override edit.
  _wikiView = null;
  _wikiEditing = false;
  _wikiEditDraft = null;
  const seq = ++_wikiDetailSeq;
  panelLoading(view);
  const base = `/api/wiki/${encodeURIComponent(type)}/${encodeURIComponent(name)}`;
  const vq = `?vendor=${encodeURIComponent(vendor)}`;
  // The override read pane is the dev-tier editor's surface: the GET only mounts
  // in dev. The prod read-only browser shows diff/lint only — fetching the
  // override there would 404. Gate on dev + renderable (non-renderable vendors
  // have no editor, mirroring the disabled <option> and the PUT's 400).
  const wantOverride = _wikiDevMode() && _wikiVendorRenderable(vendor);
  try {
    const reqs = [fetch(`${base}/diff${vq}`), fetch(`${base}/lint${vq}`)];
    if (wantOverride) reqs.push(fetch(`${base}/override${vq}`));
    const [diffRes, lintRes, overrideRes] = await Promise.all(reqs);
    if (seq !== _wikiDetailSeq) return;
    const diff = diffRes.ok ? await diffRes.json() : { _error: await _wikiErrDetail(diffRes) };
    const lint = lintRes.ok ? await lintRes.json() : { _error: await _wikiErrDetail(lintRes) };
    let override = null;
    if (wantOverride && overrideRes) {
      override = overrideRes.ok
        ? await overrideRes.json()
        : { _error: await _wikiErrDetail(overrideRes) };
    }
    if (seq !== _wikiDetailSeq) return;
    _wikiView = { diff, lint, override };
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
  // Switching assets abandons any in-progress canonical edit and the cached
  // canonical (it belongs to the previous asset).
  _wikiCanonical = null;
  _wikiCanonEditing = false;
  _wikiCanonDraft = null;
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
  // Editor-B: load the artifact-level canonical read pane (dev-tier; the GET only
  // mounts in dev). Independent of the per-vendor view below.
  if (_wikiDevMode()) await _loadWikiCanonical(type, name);
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
  _wikiCanonical = null;
  _wikiCanonEditing = false;
  _wikiCanonDraft = null;
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

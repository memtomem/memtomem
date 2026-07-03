/**
 * Context Gateway — part 5/7: conflict. The 409 mtime conflict-resolution flow
 * and the move/copy transfer modal (#1289). Classic script (#1517).
 *
 *   depends on: app.js globals; context-gateway-core.js (state, scope helpers);
 *               context-gateway-list.js (scope predicates)
 *   provides:   window.openCtxConflictModal (the a11y shim, set at load),
 *               _ctxResolveConflict, the move/copy modal entry points
 */

// -- 409 mtime conflict resolution (issue #763) -------------------------------
//
// When PUT /context/{type}/{name} returns 409 the user has unsaved edits and
// the on-disk file changed underneath them. Silent reload would discard the
// buffer; instead we open a dialog with three explicit choices:
//
//   * Reload  — discard buffer, fetch fresh, drop draft.
//   * Open diff editor — render user-buffer-vs-on-disk diff inline above the
//     textarea so the user can hand-merge; keep the buffer hot, refresh
//     mtime_ns to the freshly-read value so the next Save no longer 409s.
//   * Force save — re-PUT with ``force: true``; backend logs WARNING with
//     both mtime values for the audit trail.
//
// The buffer is stashed in sessionStorage on every 409 entry so that
// closing the dialog (Escape / backdrop / accidental tab close) does not
// destroy work — the next mount of the same detail rehydrates it.

function _ctxStashKey(type, name) {
  // Key drafts under the *effective* scope, not the raw active id, so the
  // draft namespace always matches the scope the request actually used. During
  // a transient outage the active id is preserved (#1102) but requests fall
  // back to Server-CWD; keying off the raw id here would cross-contaminate the
  // real project's drafts after recovery.
  const scopeToken = _ctxEffectiveScopeId() || '__default__';
  return `m2m-ctx-conflict-buffer:${type}:${encodeURIComponent(scopeToken)}:${encodeURIComponent(name)}`;
}
// Stash / restore / clear all take the *pinned* key the editor captured at
// mount (``detailEl.dataset.draftKey``) rather than recomputing it live. The
// effective scope can shift underneath an open editor — e.g. a transient
// projects-fetch outage that preserved the selection (#1102) recovers
// mid-conflict — and recomputing at clear time would target a different
// namespace than stash time, orphaning the draft (and resurrecting it later
// after the user already discarded/saved). One key per editor session.
// Once-per-page-session latch for stash-failure feedback (#1291). The stash
// exists to survive navigation, so a silent drop means the user's conflict
// buffer can vanish with no warning; but a busted sessionStorage (quota,
// private mode) fails on EVERY stash, so repeat failures stay quiet after
// the first warning.
let _ctxStashWarnedOnce = false;
function _ctxStashDraft(key, content) {
  try {
    sessionStorage.setItem(key, content);
  } catch (_e) { // quota / private mode
    if (!_ctxStashWarnedOnce) {
      _ctxStashWarnedOnce = true;
      showToast(t('settings.ctx.draft_stash_failed'), 'warning');
    }
  }
}
function _ctxRestoreDraft(key, type, name) {
  try {
    const scopedDraft = sessionStorage.getItem(key);
    if (scopedDraft != null) return scopedDraft;
    // Only fall back to the pre-scope legacy buffer when we are *effectively*
    // on Server-CWD (the legacy unscoped key's origin). A real project — or an
    // id that resolves to a real project — must not adopt the unscoped draft.
    if (_ctxEffectiveScopeId()) return null;
    const legacyKey = `m2m-ctx-conflict-buffer:${type}:${encodeURIComponent(name)}`;
    return sessionStorage.getItem(legacyKey);
  } catch (_e) {
    return null;
  }
}
function _ctxClearDraft(key, type, name) {
  const legacyKey = `m2m-ctx-conflict-buffer:${type}:${encodeURIComponent(name)}`;
  try {
    sessionStorage.removeItem(key);
    sessionStorage.removeItem(legacyKey);
  } catch (_e) {
    /* */
  }
}

async function _ctxFetchFresh(type, name) {
  // Returns ``{content, mtime_ns}`` from the canonical detail GET, or null
  // on transport / decode failure (toast already shown).
  try {
    const res = await fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}`),
    );
    if (!res.ok) {
      showToast(t('toast.request_failed'), 'error');
      return null;
    }
    const data = await res.json();
    return { content: data.content || '', mtime_ns: data.mtime_ns || '' };
  } catch (err) {
    showToast(t('toast.request_failed'), 'error');
    return null;
  }
}

function _ctxResolveConflict(userBuffer, freshContent) {
  // Opens the 3-button modal and resolves to 'reload' | 'force' | 'diff'
  // — or null if dismissed via Escape / backdrop click.
  return new Promise(resolve => {
    const modalEl = qs('ctx-conflict-modal');
    qs('ctx-conflict-yours').textContent = userBuffer;
    qs('ctx-conflict-theirs').textContent = freshContent;
    const reloadBtn = qs('ctx-conflict-reload-btn');
    const diffBtn = qs('ctx-conflict-diff-btn');
    const forceBtn = qs('ctx-conflict-force-btn');
    // window.openModal funnels through openModalA11y so the conflict modal
    // joins _ACTIVE_MODALS and the global shortcut gate (A11Y-3.1) sees it.
    const releaseA11y = window.openModal(modalEl, {
      focusables: () => [reloadBtn, diffBtn, forceBtn],
    });
    window.registerModalCloser(modalEl, () => cleanup(null));
    // Focus the safest choice. Force-save is destructive (overwrites the
    // other writer's edits) and the modal exists precisely to make that
    // choice explicit — auto-focusing the danger button would let a
    // reflexive Enter-press silently overwrite work. Reload preserves
    // the on-disk content; the user can still tab to Force.
    reloadBtn.focus();

    function cleanup(choice) {
      hide(modalEl);
      releaseA11y();
      modalEl.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      reloadBtn.onclick = null;
      diffBtn.onclick = null;
      forceBtn.onclick = null;
      resolve(choice);
    }
    function onBackdrop(e) { if (e.target === modalEl) cleanup(null); }
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); cleanup(null); }
    }
    modalEl.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    reloadBtn.onclick = () => cleanup('reload');
    diffBtn.onclick = () => cleanup('diff');
    forceBtn.onclick = () => cleanup('force');
  });
}

// Test/dev entry point — production callers use ``_ctxResolveConflict``
// with real user/disk buffers. The no-arg shim is what the A11Y Playwright
// pins drive so they don't need to set up a full edit-conflict scenario.
window.openCtxConflictModal = () => _ctxResolveConflict('', '');

// ── Move/Copy destination modal (B-6 #1289) ─────────────────────────────────
// Per-artifact "Move/Copy to…" built on POST /api/context/{kind}/{name}/transfer
// (the A-5 #1276 endpoint). A dry-run preview (``?dry_run=1``) runs on every
// destination change and gates the Apply button: a collision, a same-(project,
// tier) no-op, or a sync-ineligible destination all keep Apply disabled with an
// inline warning; a clean plan enables it. Apply then threads the tier-keyed
// confirm round-trip (project_shared → ``confirm_project_shared``; user →
// ``allow_host_writes`` via the shared host-write disclosure). skills/commands/
// agents only — they support the full surface (both modes, all three tiers,
// rename). mcp-servers ride the SAME modal in a constrained variant (#1314).
const _CTX_TRANSFER_TYPES = new Set(['skills', 'commands', 'agents']);

// mcp-servers (#1314 / engine A-12 #1282) are a constrained move/copy: copy
// only (a cross-project move would orphan the source ``.mcp.json`` fan-out),
// cross-project only (single-tier ⇒ no second tier to copy to within a
// project), ``project_shared`` pinned, and no rename. The modal hides the
// mode/tier/rename controls for this kind and requires a non-source
// destination project. ``_ctxCanMoveCopy`` gates BOTH the detail-pane button
// and ``_ctxOpenMoveCopyModal``; ``_CTX_TRANSFER_TYPES`` stays the full-surface
// set so the body builder / visibility toggles can branch on it.
const _CTX_MCP_COPY_TYPE = 'mcp-servers';
function _ctxCanMoveCopy(type) {
  return _CTX_TRANSFER_TYPES.has(type) || type === _CTX_MCP_COPY_TYPE;
}

// Open-modal state, shared with the ``langchange`` re-render; ``null`` when
// closed. ``lastPreview`` caches the latest dry-run outcome so a locale flip can
// re-paint the JS-owned subject/preview/warning lines without re-issuing fetch.
let _ctxMoveCopyState = null;
// Monotonic guard: a stale dry-run response (slow request; the user changed a
// control meanwhile) must never paint over a fresher selection — same
// supersession discipline as ``_ctxDetailSeq``.
let _ctxMoveCopySeq = 0;
let _ctxMoveCopyPreviewTimer = null;

// Localized tier label for a transfer ``to_target_scope`` — reuses the tier
// radio keys so the preview/subject text never drifts from the radio options.
function _ctxTierLabel(scope) {
  const key = {
    user: 'settings.ctx.tier_option_user',
    project_shared: 'settings.ctx.tier_option_project_shared',
    project_local: 'settings.ctx.tier_option_project_local',
  }[scope];
  return key ? t(key) : (scope || '');
}

// Toggle the per-mode/tier rows: the user tier is global (no per-project
// destination), and rename is copy-only (the engine 400s ``move + as_name``).
// The modal markup is SHARED static DOM, so this fully sets every row's
// visibility on each open — a prior mcp session that hid the mode/tier
// fieldsets must not leave them hidden for the next skills session. The
// constrained mcp variant (#1314) shows only the destination-project picker
// plus its explanatory note.
function _ctxSyncMoveCopyVisibility(state) {
  const modalEl = qs('ctx-move-copy-modal');
  if (!modalEl) return;
  const modeField = qs('ctx-mc-mode-field');
  const tierField = qs('ctx-mc-tier-field');
  const projRow = qs('ctx-mc-project-row');
  const renameRow = qs('ctx-mc-rename-row');
  const note = qs('ctx-mc-mcp-note');
  if (state && state.isMcp) {
    if (modeField) modeField.hidden = true;
    if (tierField) tierField.hidden = true;
    if (projRow) projRow.hidden = false;   // cross-project destination required
    if (renameRow) renameRow.hidden = true;
    if (note) note.hidden = false;
    return;
  }
  if (modeField) modeField.hidden = false;
  if (tierField) tierField.hidden = false;
  if (note) note.hidden = true;
  const mode = modalEl.querySelector('input[name="ctx-mc-mode"]:checked')?.value || 'copy';
  const tier = modalEl.querySelector('input[name="ctx-mc-tier"]:checked')?.value || 'project_shared';
  if (projRow) projRow.hidden = (tier === 'user');
  if (renameRow) renameRow.hidden = (mode !== 'copy');
}

// Build the transfer request body from the live modal controls. Destination
// project: ``null`` when it equals the source (the route's implicit
// same-project destination, which also inherits the source scope record for the
// eligibility gate); an explicit id otherwise. The raw roster scope_id is the
// comparison key (matches ``_resolve_destination``'s discovery), independent of
// how server-cwd is spelled in ``_ctxActiveScopeId``.
function _ctxMoveCopyBody(state) {
  const modalEl = qs('ctx-move-copy-modal');
  if (state.isMcp) {
    // Constrained mcp-servers copy (#1314): every dimension but the
    // destination project is pinned. ``to_project_scope_id`` is REQUIRED — the
    // route 400s a null one ("cross-project only"); the project select excludes
    // the source, so its value (an empty string === Server CWD is a valid
    // cross-project destination here) goes through verbatim. ``null`` only when
    // there is no eligible destination at all (no options) — the preview
    // short-circuits that case before this body is ever sent.
    const projSel = qs('ctx-mc-project');
    const hasDest = !!(projSel && projSel.options.length);
    return {
      mode: 'copy',
      to_target_scope: 'project_shared',
      to_project_scope_id: hasDest ? projSel.value : null,
      from_scope: 'project_shared',
      confirm_project_shared: false,
      allow_host_writes: false,
    };
  }
  const mode = modalEl.querySelector('input[name="ctx-mc-mode"]:checked')?.value || 'copy';
  const toTier = modalEl.querySelector('input[name="ctx-mc-tier"]:checked')?.value || 'project_shared';
  const projSel = qs('ctx-mc-project');
  const rename = ((qs('ctx-mc-rename') || {}).value || '').trim();
  let toProject = null;
  if (toTier !== 'user') {
    const sel = projSel ? projSel.value : '';
    toProject = (sel === state.srcScopeIdRaw) ? null : sel;
  }
  const body = {
    mode,
    to_target_scope: toTier,
    to_project_scope_id: toProject,
    from_scope: state.srcTier,
    confirm_project_shared: false,
    allow_host_writes: false,
  };
  if (mode === 'copy' && rename) body.as_name = rename;
  return body;
}

// POST the transfer. ``dryRun`` picks the preview leg; ``extra`` carries the
// confirmed gate flag on a re-apply. CSRF rides EVERY unsafe /api/* request
// (CSRFGuardMiddleware), so thread the header on all legs. Source project+tier
// are pinned (``scopeResolved``) so a mid-modal scope drift can't re-target.
async function _ctxMoveCopyPost(state, body, dryRun) {
  const csrf = await ensureCsrfToken();
  const headers = csrf
    ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
    : { 'Content-Type': 'application/json' };
  const url = _ctxWithTargetScope(
    `/api/context/${state.srcType}/${encodeURIComponent(state.srcName)}/transfer`
    + (dryRun ? '?dry_run=1' : ''),
    { scopeId: state.srcScopeIdEff, scopeResolved: true, targetScope: state.srcTier },
  );
  return fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
}

// Paint the cached dry-run outcome into the JS-owned lines + Apply state.
// Re-runnable on ``langchange`` (reads ``state.lastPreview`` — no fetch).
function _ctxRenderMoveCopyPreview(state) {
  const titleEl = qs('ctx-mc-title');
  const subjectEl = qs('ctx-mc-subject');
  const previewEl = qs('ctx-mc-preview');
  const warnEl = qs('ctx-mc-warning');
  const applyBtn = qs('ctx-mc-apply-btn');
  if (!previewEl || !warnEl || !applyBtn) return;
  if (titleEl) {
    // The title carries ``data-i18n=move_copy_title`` for the default kinds, so
    // ``I18N.applyDOM`` resets it on a locale flip; this render runs AFTER
    // applyDOM (langchange fires post-applyDOM) and overrides it for the
    // constrained mcp variant, so the copy-only title always wins.
    titleEl.textContent = state.isMcp
      ? t('settings.ctx.move_copy_mcp_title')
      : t('settings.ctx.move_copy_title');
  }
  if (subjectEl) {
    subjectEl.textContent = t('settings.ctx.move_copy_subject', {
      type: _ctxTypeNameSingular(state.srcType),
      name: state.srcName,
      from: _ctxTierLabel(state.srcTier),
    });
  }
  const p = state.lastPreview;
  if (!p || p.kind === 'pending') {
    hide(previewEl); previewEl.textContent = '';
    hide(warnEl); warnEl.textContent = '';
    applyBtn.disabled = true;
    return;
  }
  if (p.kind === 'plan') {
    const d = p.data || {};
    const dest = d.to_scope === 'user'
      ? _ctxTierLabel('user')
      : `${_ctxScopeDisplayLabelById(d.dst_project_scope_id || '')} · ${_ctxTierLabel(d.to_scope)}`;
    previewEl.textContent = t('settings.ctx.move_copy_preview', {
      dest,
      dst: d.dst_name || state.srcName,
    });
    show(previewEl);
    hide(warnEl); warnEl.textContent = '';
    applyBtn.disabled = false;
    return;
  }
  // collision | error → inline warning, Apply stays disabled.
  hide(previewEl); previewEl.textContent = '';
  warnEl.textContent = p.message || t('toast.request_failed');
  show(warnEl);
  applyBtn.disabled = true;
}

// Run a seq-guarded dry-run preview for the current controls. A clean plan
// enables Apply; collision (409 destination_exists) / same-store 400 /
// ineligible 409 / privacy 422 / any error all land as a disabled-Apply
// inline warning.
async function _ctxMoveCopyPreview(state) {
  if (_ctxMoveCopyPreviewTimer) { clearTimeout(_ctxMoveCopyPreviewTimer); _ctxMoveCopyPreviewTimer = null; }
  // Constrained mcp variant (#1314) with no eligible cross-project destination:
  // a dry-run would only 400 ("cross-project only"). Surface a dedicated,
  // actionable inline warning and leave Apply disabled instead. Synchronous, so
  // no seq guard is needed.
  if (state.isMcp) {
    const projSel = qs('ctx-mc-project');
    if (!projSel || !projSel.options.length) {
      state.lastPreview = { kind: 'error', message: t('settings.ctx.move_copy_mcp_no_dest') };
      _ctxRenderMoveCopyPreview(state);
      return;
    }
  }
  const seq = ++_ctxMoveCopySeq;
  state.lastPreview = { kind: 'pending' };
  _ctxRenderMoveCopyPreview(state);
  let outcome;
  try {
    const r = await _ctxMoveCopyPost(state, _ctxMoveCopyBody(state), true);
    if (r.ok) {
      outcome = { kind: 'plan', data: await r.json() };
    } else {
      const err = await r.json().catch(() => ({}));
      const detail = err && err.detail;
      if (detail && typeof detail === 'object' && detail.reason_code === 'destination_exists') {
        outcome = { kind: 'collision', message: t('settings.ctx.move_copy_collision') };
      } else {
        outcome = { kind: 'error', message: _ctxErrDetail(detail, t('toast.request_failed')) };
      }
    }
  } catch (e) {
    outcome = { kind: 'error', message: (e && e.message) || t('toast.request_failed') };
  }
  // Drop a stale response: a newer preview started, or the modal closed/reopened.
  if (seq !== _ctxMoveCopySeq || _ctxMoveCopyState !== state) return;
  state.lastPreview = outcome;
  _ctxRenderMoveCopyPreview(state);
}

// Debounced preview for the rename keystroke stream (immediate triggers —
// radios/select — call _ctxMoveCopyPreview directly).
function _ctxSchedulePreview(state) {
  if (_ctxMoveCopyPreviewTimer) clearTimeout(_ctxMoveCopyPreviewTimer);
  _ctxMoveCopyPreviewTimer = setTimeout(() => { _ctxMoveCopyPreview(state); }, 300);
}

// Lock / unlock the destination controls for the duration of an apply, so the
// frozen request body can't drift from the visible selection and a late
// failure can't clobber a preview the user kicked off mid-apply.
function _ctxSetMoveCopyControlsDisabled(modalEl, disabled) {
  modalEl
    .querySelectorAll('input[name="ctx-mc-mode"], input[name="ctx-mc-tier"], #ctx-mc-project, #ctx-mc-rename')
    .forEach((el) => { el.disabled = disabled; });
}

// Classify a failed transfer response into a preview-shaped outcome so the
// apply path and the dry-run path render identically. A ``destination_exists``
// 409 is the collision the engine can ALSO raise at apply time (the
// re-check after the pair-lock acquire, transfer.py "destination appeared
// during lock acquire") — even when the preview was clean — and it is
// terminal, so it must leave Apply disabled, exactly like a preview collision.
async function _ctxMoveCopyErrorOutcome(r) {
  const err = await r.json().catch(() => ({}));
  const detail = err && err.detail;
  if (detail && typeof detail === 'object' && detail.reason_code === 'destination_exists') {
    return { kind: 'collision', message: t('settings.ctx.move_copy_collision') };
  }
  return { kind: 'error', message: _ctxErrDetail(detail, t('toast.request_failed')) };
}

// Apply the transfer: real POST, then the tier-keyed gate round-trip, then the
// success refresh. Gates are mutually exclusive (the route emits at most one).
// A failed apply is folded back into ``state.lastPreview`` so Apply's enabled
// state tracks the last outcome — a collision stays disabled until the user
// changes destination/name and a fresh dry-run succeeds (the finally re-derives
// it after clearing the loading spinner; ordering-independent).
async function _ctxMoveCopyApply(state) {
  const modalEl = qs('ctx-move-copy-modal');
  const applyBtn = qs('ctx-mc-apply-btn');
  const body = _ctxMoveCopyBody(state);
  const send = (extra) => _ctxMoveCopyPost(state, { ...body, ...extra }, false);
  // Freeze the destination for the whole apply: drop any pending debounced
  // preview, lock the controls (so no new dry-run can start mid-apply against a
  // changed destination), and snapshot the preview seq. Together these stop a
  // late apply failure from overwriting a newer preview's result.
  if (_ctxMoveCopyPreviewTimer) { clearTimeout(_ctxMoveCopyPreviewTimer); _ctxMoveCopyPreviewTimer = null; }
  const applySeq = _ctxMoveCopySeq;
  _ctxSetMoveCopyControlsDisabled(modalEl, true);
  btnLoading(applyBtn, true);
  let outcome = null;   // a failed-apply lastPreview shape, or null on success/decline
  // The modal is shared static DOM. If the user closes mid-apply and reopens a
  // new session, ownership moves on; this stale apply must then touch NOTHING
  // shared — not the gate dialog, not close, not the success toast/refresh. It
  // bails at every resumption point (and the finally is likewise state-guarded).
  const owns = () => _ctxMoveCopyState === state;
  try {
    let r = await send({});
    if (!owns()) return;
    if (!r.ok) { outcome = await _ctxMoveCopyErrorOutcome(r); return; }
    let data = await r.json();
    if (!owns()) return;
    if (data && data.status === 'needs_confirmation') {
      // The gate's confirm dialog (#confirm-modal) shares the .modal-overlay
      // z-index and sits earlier in the DOM, so it would stack UNDER this
      // modal (openModalA11y orders by DOM, not z-index). Hide this one for
      // the disclosure — one overlay on screen — and restore it on decline.
      hide(modalEl);
      if (data.confirm === 'allow_host_writes') {
        // Reuse the shared host-write disclosure (host_targets capped at 8).
        r = await _ctxConfirmHostWrite(data, () => send({ allow_host_writes: true }));
      } else {
        // project_shared Gate B — disclose the git-tracked write, then re-POST.
        // Render the localized key, NOT ``data.reason``: the backend reason is
        // raw developer prose ("Re-POST with confirm_project_shared=true …") and,
        // being always non-empty, shadowed the i18n fallback in every locale so
        // non-English users never saw their translation (#1348). The envelope's
        // ``confirm`` flag already told us this is the project_shared gate, so we
        // don't need the prose to pick the right copy.
        const ok = await showConfirm({
          title: t('settings.ctx.move_copy_shared_confirm_title'),
          message: t('settings.ctx.move_copy_shared_confirm_message'),
          confirmText: t('settings.ctx.move_copy_shared_confirm_btn'),
          danger: false,
        });
        r = ok ? await send({ confirm_project_shared: true }) : null;
      }
      if (!owns()) return;                     // superseded during the disclosure
      if (!r) { show(modalEl); return; }       // declined — restore the modal
      if (!r.ok) { show(modalEl); outcome = await _ctxMoveCopyErrorOutcome(r); return; }
      data = await r.json();
      if (!owns()) return;
    }
    _ctxMoveCopyClose(state);
    _ctxMoveCopySuccess(state, data || {});
  } catch (e) {
    outcome = { kind: 'error', message: (e && e.message) || t('toast.request_failed') };
  } finally {
    // The modal + its controls are SHARED static DOM. Only touch them if THIS
    // apply still owns the modal — a close (success/Cancel/Escape) followed by a
    // reopen hands ownership to a newer state, and a stale apply settling later
    // must not unlock that session's locked controls or clear its spinner.
    // (close/open own the reset of stale DOM for the superseded case.)
    if (_ctxMoveCopyState === state) {
      btnLoading(applyBtn, false);
      _ctxSetMoveCopyControlsDisabled(modalEl, false);   // unlock so the user can adjust
      // Reflect a failed apply in the preview state so Apply's enabled-ness
      // tracks it (collision/error → disabled; the user adjusts to re-dry-run).
      // Skip if a newer preview superseded this apply (seq advanced) — a stale
      // failure must never clobber the current destination's result.
      if (outcome && _ctxMoveCopySeq === applySeq) {
        state.lastPreview = outcome;
        _ctxRenderMoveCopyPreview(state);
      }
    }
  }
}

// Success toast (with an optional destination-pinned sync follow-up) + refresh.
function _ctxMoveCopySuccess(state, data) {
  const mode = data.mode || 'copy';
  const opts = {};
  // One-click "Sync destination now": pin BOTH project and tier to the
  // DESTINATION (never the live UI scope — it may have drifted). Offered only
  // when the engine flagged needs_sync with a command — project_local sets it
  // false, and the user tier is left to a manual host-write sync.
  if (data.needs_sync && data.sync_command && data.to_scope === 'project_shared') {
    opts.action = {
      label: t('settings.ctx.move_copy_sync_now'),
      onClick: () => _ctxMoveCopyRunDestSync(state, data),
    };
  }
  showToast(t(mode === 'move' ? 'settings.ctx.move_success' : 'settings.ctx.copy_success', {
    type: _ctxTypeNameSingular(state.srcType),
    name: state.srcName,
    dst: data.to_scope === 'user'
      ? _ctxTierLabel('user')
      : _ctxScopeDisplayLabelById(data.dst_project_scope_id || ''),
  }), 'success', opts);
  // Refresh the source list (badges/rows); a move consumed the source artifact
  // (wipe the detail), a copy left it in place (reload so it stays current).
  loadCtxList(state.srcType);
  const detailEl = qs(`ctx-${state.srcType}-detail`);
  if (mode === 'move') {
    if (detailEl) detailEl.hidden = true;
  } else {
    loadCtxDetail(state.srcType, state.srcName);
  }
}

// Destination-pinned per-type sync (the needs_sync follow-up). Pins project +
// tier to the transfer destination so a UI scope change between apply and click
// can't sync the wrong scope.
async function _ctxMoveCopyRunDestSync(state, data) {
  try {
    const csrf = await ensureCsrfToken();
    const headers = csrf
      ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
      : { 'Content-Type': 'application/json' };
    const url = _ctxWithTargetScope(`/api/context/${state.srcType}/sync`, {
      scopeId: data.dst_project_scope_id || '', scopeResolved: true, targetScope: data.to_scope,
    });
    const r = await fetch(url, { method: 'POST', headers, body: JSON.stringify({}) });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(_ctxErrDetail(err && err.detail, t('toast.request_failed')), 'error');
      return;
    }
    showToast(t('settings.ctx.move_copy_sync_done'));
    loadCtxList(state.srcType);
  } catch (e) {
    showToast((e && e.message) || t('toast.request_failed'), 'error');
  }
}

// Tear down listeners (the modal markup is static and persists, so a reopen
// must not stack duplicate handlers), hide the modal, clear shared state, and
// reset the SHARED controls/Apply so a close mid-apply can't reopen frozen
// (the in-flight apply's finally is state-guarded and won't touch them).
function _ctxMoveCopyClose(state) {
  if (state && state._teardown) { state._teardown(); state._teardown = null; }
  // Only the owning session touches the shared modal DOM / global timer — a
  // stale settled apply must never hide a newly reopened session (defense; the
  // apply path also bails on lost ownership before it can reach here).
  if (_ctxMoveCopyState === state) {
    if (_ctxMoveCopyPreviewTimer) { clearTimeout(_ctxMoveCopyPreviewTimer); _ctxMoveCopyPreviewTimer = null; }
    const modalEl = qs('ctx-move-copy-modal');
    if (modalEl) {
      hide(modalEl);
      _ctxResetMoveCopyControls(modalEl);
    }
    _ctxMoveCopyState = null;
  }
}

// Reset the shared static modal's controls to a clean, enabled, not-loading
// state. Run on both close and open so neither a close mid-apply nor a stale
// settled apply can leave the next session's destination fields disabled.
function _ctxResetMoveCopyControls(modalEl) {
  _ctxSetMoveCopyControlsDisabled(modalEl, false);
  const applyBtn = modalEl.querySelector('#ctx-mc-apply-btn');
  if (applyBtn) btnLoading(applyBtn, false);
}

// Open the modal for one artifact. Pins source identity + scope/tier ONCE
// (ADR-0021 §C). skills/commands/agents (full surface) + mcp-servers
// (constrained copy-only variant, #1314).
function _ctxOpenMoveCopyModal(srcType, srcName) {
  const modalEl = qs('ctx-move-copy-modal');
  if (!modalEl || !_ctxCanMoveCopy(srcType)) return;
  const isMcp = srcType === _CTX_MCP_COPY_TYPE;
  // Clear any stale disabled/loading DOM left by an interrupted prior session.
  _ctxResetMoveCopyControls(modalEl);
  const srcScope = (_ctxProjectsCache || []).find(_ctxScopeIsActive);
  const state = {
    srcType,
    srcName,
    isMcp,
    // mcp-servers are single-tier ⇒ the source is always the project_shared
    // canonical, regardless of the live UI tier filter. Pinning it here keeps
    // the subject line ("from …") and the pinned ``from_scope`` body field
    // honest (the transfer route ignores the URL ``target_scope`` and resolves
    // the source by ``scope_id`` alone).
    srcTier: isMcp ? 'project_shared' : _ctxTargetScope,
    // Query source: effective id ('' = server-cwd → route default). Same value
    // every other gateway request sends.
    srcScopeIdEff: _ctxEffectiveScopeId(_ctxActiveScopeId),
    // Destination same-project comparison key: the raw roster scope_id of the
    // active source (server-cwd's compute_scope_id, or '' when none active).
    srcScopeIdRaw: srcScope ? srcScope.scope_id : (_ctxActiveScopeId || ''),
    lastPreview: null,
    _teardown: null,
  };
  _ctxMoveCopyState = state;

  // Destination project options. Full kinds: sync-eligible scopes plus always
  // the source project (same-project promote must be offered; a paused source
  // surfaces inline at dry-run). mcp-servers (#1314): cross-project ONLY, so
  // the source project is EXCLUDED and every option must be sync-eligible (the
  // route 409s a paused/never-enrolled destination — no point offering it).
  // Missing scopes excluded either way.
  const projSel = qs('ctx-mc-project');
  if (projSel) {
    const list = (_ctxProjectsCache || []).filter(s => {
      if (!s || s.missing) return false;
      if (isMcp) return s.scope_id !== state.srcScopeIdRaw && _ctxScopeSyncEligible(s);
      return _ctxScopeSyncEligible(s) || _ctxScopeIsActive(s);
    });
    projSel.innerHTML = list.map(s => {
      // Full kinds preselect the source (same-project promote default); mcp
      // leaves the browser's first-option default (any of them is valid).
      const sel = (!isMcp && s.scope_id === state.srcScopeIdRaw) ? ' selected' : '';
      return `<option value="${escapeHtml(s.scope_id)}"${sel}>${escapeHtml(_ctxScopeDisplayLabel(s))}</option>`;
    }).join('');
  }

  // Defaults: copy, and a destination tier. Full kinds pick a tier that differs
  // from the source so the first preview isn't a same-store no-op; mcp-servers
  // are project_shared-pinned (the radios are hidden but kept consistent).
  const defaultTier = isMcp
    ? 'project_shared'
    : (state.srcTier === 'project_shared' ? 'project_local' : 'project_shared');
  modalEl.querySelectorAll('input[name="ctx-mc-mode"]').forEach(el => { el.checked = el.value === 'copy'; });
  modalEl.querySelectorAll('input[name="ctx-mc-tier"]').forEach(el => { el.checked = el.value === defaultTier; });
  const renameEl = qs('ctx-mc-rename');
  if (renameEl) renameEl.value = '';
  _ctxSyncMoveCopyVisibility(state);

  const applyBtn = qs('ctx-mc-apply-btn');
  const cancelBtn = qs('ctx-mc-cancel-btn');
  const onChange = () => { _ctxSyncMoveCopyVisibility(state); _ctxMoveCopyPreview(state); };
  const onRenameInput = () => _ctxSchedulePreview(state);
  const onApply = () => _ctxMoveCopyApply(state);
  const onCancel = () => _ctxMoveCopyClose(state);
  const onBackdrop = (e) => { if (e.target === modalEl) _ctxMoveCopyClose(state); };
  // Only act on Escape while THIS modal is the visible one — during a gate
  // round-trip it is hidden under the confirm dialog, which owns Escape then.
  const onKey = (e) => { if (e.key === 'Escape' && !modalEl.hidden) { e.stopPropagation(); _ctxMoveCopyClose(state); } };
  const radios = modalEl.querySelectorAll('input[name="ctx-mc-mode"], input[name="ctx-mc-tier"]');

  const releaseA11y = window.openModal(modalEl, {
    focusables: () => Array.from(modalEl.querySelectorAll('input, select, button')),
  });
  window.registerModalCloser(modalEl, () => _ctxMoveCopyClose(state));

  state._teardown = () => {
    releaseA11y();
    modalEl.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKey, true);
    if (applyBtn) applyBtn.removeEventListener('click', onApply);
    if (cancelBtn) cancelBtn.removeEventListener('click', onCancel);
    radios.forEach(el => el.removeEventListener('change', onChange));
    if (projSel) projSel.removeEventListener('change', onChange);
    if (renameEl) renameEl.removeEventListener('input', onRenameInput);
  };

  radios.forEach(el => el.addEventListener('change', onChange));
  if (projSel) projSel.addEventListener('change', onChange);
  if (renameEl) renameEl.addEventListener('input', onRenameInput);
  if (applyBtn) applyBtn.addEventListener('click', onApply);
  if (cancelBtn) cancelBtn.addEventListener('click', onCancel);
  modalEl.addEventListener('click', onBackdrop);
  document.addEventListener('keydown', onKey, true);

  _ctxRenderMoveCopyPreview(state);   // paint title/subject + reset Apply
  _ctxMoveCopyPreview(state);         // kick the first dry-run
  // Initial focus goes to the first VISIBLE control, NOT Apply — Apply starts
  // disabled (pending the first dry-run), and focusing a disabled button drops
  // focus to the now-inert background. For mcp the mode radio is hidden, so the
  // destination-project select is the first focusable control. The Tab trap
  // (openModalA11y) cycles from here.
  const firstControl = isMcp
    ? (qs('ctx-mc-project') || modalEl.querySelector('select, button'))
    : (modalEl.querySelector('input[name="ctx-mc-mode"]:checked')
      || modalEl.querySelector('input, select, button'));
  firstControl?.focus();
}

// Re-paint the open Move/Copy modal's JS-owned lines on a locale flip. The
// static labels ride ``data-i18n`` (I18N.applyDOM); the subject/preview/warning
// are JS-set, so re-render from the cached preview (no re-fetch).
window.addEventListener('langchange', () => {
  if (_ctxMoveCopyState) _ctxRenderMoveCopyPreview(_ctxMoveCopyState);
});

function _ctxRenderConflictBanner(detailEl, userBuffer, freshContent) {
  // Inline diff inside the edit pane, above the textarea. Diff orientation
  // is on-disk → user-buffer so '+' lines are the user's edits and '-'
  // lines are what the user is about to overwrite — matches the "your
  // edits over what's there" mental model.
  const banner = detailEl.querySelector('.ctx-conflict-banner');
  if (!banner) return;
  const heading = `${escapeHtml(t('settings.ctx.conflict_your_edits'))} ↔ ${escapeHtml(t('settings.ctx.conflict_on_disk'))}`;
  const ops = diffLines(freshContent, userBuffer);
  // ``role="alert"`` lives on the short heading only — the assertive region must
  // not wrap the scrolling diff body, or the screen reader would read the whole
  // diff. The conflict is an error-class interrupt the user must act on.
  banner.innerHTML = `<div class="text-muted" style="margin-bottom:6px;font-size:0.78rem" role="alert">${heading}</div>`
    + `<div class="diff-view" style="max-height:200px;overflow:auto;margin-bottom:8px">${renderDiff(ops)}</div>`;
  banner.hidden = false;
}

async function _ctxHandleConflict(type, name, userBuffer, staleMtimeNs, detailEl) {
  // ``staleMtimeNs`` is the mtime_ns the user's first Save was already
  // racing against — i.e. what they thought disk was. We thread it
  // through to the force PUT body so the server-side WARNING log
  // captures distinct ``client_mtime_ns`` / ``server_mtime_ns`` values;
  // sending ``fresh.mtime_ns`` would make the two values nearly equal
  // and defeat the audit trail's "what was being overridden" purpose.
  //
  // Stash early so the buffer survives an Escape-out / tab close. Use the key
  // pinned at editor mount so a mid-conflict scope shift can't orphan it.
  const draftKey = detailEl.dataset.draftKey || _ctxStashKey(type, name);
  _ctxStashDraft(draftKey, userBuffer);
  const fresh = await _ctxFetchFresh(type, name);
  if (fresh == null) return;
  const choice = await _ctxResolveConflict(userBuffer, fresh.content);
  if (choice === 'reload') {
    _ctxClearDraft(draftKey, type, name);
    loadCtxDetail(type, name);
    return;
  }
  if (choice === 'diff') {
    // Refresh mtime_ns to the freshly-read value so the user's next Save
    // is comparing against a version we *know* is current. The buffer
    // remains in the textarea; clear-on-success / clear-on-cancel happen
    // in the regular save / cancel handlers.
    detailEl.dataset.mtimeNs = fresh.mtime_ns;
    _ctxRenderConflictBanner(detailEl, userBuffer, fresh.content);
    return;
  }
  if (choice === 'force') {
    try {
      const csrf = await ensureCsrfToken();
      const headers = csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' };
      const forcePut = (extra) => fetch(
        _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}`),
        {
          method: 'PUT',
          headers,
          body: JSON.stringify({
            content: userBuffer, mtime_ns: staleMtimeNs, force: true, ...extra,
          }),
        },
      );
      let r2 = await forcePut({});
      if (!r2.ok) {
        const err = await r2.json().catch(() => ({}));
        showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
        return;
      }
      let result = await r2.json();
      if (_ctxIsHostWriteEnvelope(result)) {
        // #1263: force=true skips the mtime pre-check server-side, so an
        // unconfirmed user-tier force-save reaches the host-write gate —
        // run the same disclose-then-re-PUT leg as the plain save.
        r2 = await _ctxConfirmHostWrite(result, () => forcePut({ allow_host_writes: true }));
        if (!r2) return;
        if (!r2.ok) {
          const err = await r2.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        result = await r2.json();
      }
      if (result.name) {
        showToast(t('settings.ctx.conflict_force_done'), 'warning');
        detailEl.dataset.mtimeNs = result.mtime_ns || '';
        _ctxClearDraft(draftKey, type, name);
        // List badges went stale the moment the force-PUT landed — refresh
        // alongside the detail re-mount (#1247 id 22, same as plain Save).
        loadCtxList(type);
        loadCtxDetail(type, name);
      }
    } catch (err) {
      showToast(t('toast.save_failed', { error: err.message }), 'error');
    }
    return;
  }
  // ``null`` (Escape / backdrop) — leave detail as-is, draft stays
  // stashed so a later refresh / re-mount can rehydrate.
}

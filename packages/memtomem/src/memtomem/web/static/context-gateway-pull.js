// ADR-0030 PR-D2 — source-selectable Pull picker.
//
// Reads the read-only pull-preview (GET /api/context/{kind}/{name}/pull-preview)
// into a candidate radio table — one row per tool that has the artifact on
// disk, showing the two-axis content/privacy status — then POSTs the pull-apply
// route (…/pull) over the SAME pull_apply engine the CLI drives. The §5
// ambiguity refusal is made visible: when the tool copies diverge, Apply stays
// disabled until the user names a source.
//
// The modal markup (#ctx-pull-modal) is SHARED static DOM, so this mirrors the
// Move/Copy modal's ownership + supersession discipline
// (context-gateway-conflict.js): a single owning ``_ctxPullState``, a monotonic
// ``_ctxPullSeq`` that drops stale preview paints, and a per-open teardown that
// removes every listener so a reopen never stacks duplicates.

let _ctxPullState = null;
let _ctxPullSeq = 0;

// Pull-eligible kinds (IMPORT_SOURCE_RUNTIMES server-side). mcp-servers have no
// Pull route, so the button is never offered for them.
const _CTX_PULL_KINDS = ['skills', 'agents', 'commands'];
function _ctxCanPull(type) {
  return _CTX_PULL_KINDS.includes(type);
}

// Destination tiers a Pull can land in. project_local is excluded — it has no
// tool fan-out to pull FROM (ADR-0011 §3), so the route rejects it.
function _ctxPullTierLabel(scope) {
  const key = {
    user: 'settings.ctx.tier_option_user',
    project_shared: 'settings.ctx.tier_option_project_shared',
  }[scope];
  return key ? t(key) : (scope || '');
}

function _ctxPullUrl(state, suffix) {
  // Pin BOTH dimensions (ADR-0021 §C): the destination tier is always explicit
  // (the route requires target_scope — no silent git-tier default), and the
  // project ``scope_id`` is snapshotted at open like every other detail request
  // (``_ctxWithTargetScope``). WITHOUT the scope_id a Pull opened from a
  // registered non-CWD project would preview/apply against Server CWD and could
  // overwrite a same-named artifact there. ``state.scopeId`` is an
  // already-effective id (empty for Server CWD → omitted, the route default).
  const parts = [`target_scope=${encodeURIComponent(state.tier)}`];
  if (state.scopeId) parts.push(`scope_id=${encodeURIComponent(state.scopeId)}`);
  return `/api/context/${state.kind}/${encodeURIComponent(state.name)}${suffix}?${parts.join('&')}`;
}

function _ctxPullErr(detail) {
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
  if (typeof detail === 'string') return detail;
  return t('toast.request_failed');
}

// GET the read-only preview for the current destination tier. Supersession-
// guarded: a slow response for a tier the user has since changed must never
// paint over the fresher selection.
async function _ctxPullPreview(state) {
  const seq = ++_ctxPullSeq;
  state.lastPreview = { kind: 'pending' };
  _ctxRenderPullPreview(state);
  let outcome;
  try {
    const r = await fetch(_ctxPullUrl(state, '/pull-preview'), { method: 'GET' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      outcome = { kind: 'error', message: _ctxPullErr(err && err.detail) };
    } else {
      outcome = { kind: 'preview', data: await r.json() };
    }
  } catch (e) {
    outcome = { kind: 'error', message: t('toast.request_failed') };
  }
  if (seq !== _ctxPullSeq || _ctxPullState !== state) return; // stale drop
  state.lastPreview = outcome;
  if (outcome.kind === 'preview') {
    const d = outcome.data;
    // Auto-select the unambiguous source; force an explicit pick when the tool
    // copies diverge (the §5 fail-closed refusal, surfaced in the UI).
    state.selectedRuntime = (!d.ambiguous && d.auto_source) ? d.auto_source : null;
  }
  _ctxRenderPullPreview(state);
}

function _ctxPullContentBadge(s) {
  return `<span class="ctx-pull-badge ctx-pull-content-${escapeHtml(s)}">`
    + `${escapeHtml(t('settings.ctx.pull_status_' + s))}</span>`;
}

function _ctxPullGateBadge(g) {
  if (!g) return '';
  return `<span class="ctx-pull-badge ctx-pull-gate-${escapeHtml(g)}">`
    + `${escapeHtml(t('settings.ctx.pull_gate_' + g))}</span>`;
}

// A candidate is selectable only if it is importable AND its would-land bytes
// were computable — a ``landing_error`` copy is importable but unreadable, so
// the route would deterministically refuse it (``selected_landing_error``); it
// stays visible (with its reason) but gets no radio.
function _ctxPullSelectable(c) {
  return c.importable && c.content_status !== 'landing_error';
}

function _ctxPullCandidateRow(state, c, ambiguous, autoSource) {
  const selectable = _ctxPullSelectable(c);
  const checked = state.selectedRuntime === c.runtime ? 'checked' : '';
  const auto = (selectable && !ambiguous && c.runtime === autoSource)
    ? `<span class="ctx-pull-auto">${escapeHtml(t('settings.ctx.pull_auto'))}</span>`
    : '';
  const override = c.override_warning
    ? `<span class="ctx-pull-override" title="${escapeHtml(t('settings.ctx.pull_override_warning'))}">`
      + `${escapeHtml(t('settings.ctx.pull_override'))}</span>`
    : '';
  // ``reason`` is display-sanitized server-side (never raw HTML), but still
  // escape it at the sink as defense in depth.
  const reason = (!selectable && c.reason)
    ? `<span class="ctx-pull-cand-reason">${escapeHtml(c.reason)}</span>`
    : '';
  const control = selectable
    ? `<input type="radio" name="ctx-pull-source" value="${escapeHtml(c.runtime)}" ${checked}>`
    : '<span class="ctx-pull-radio-spacer" aria-hidden="true"></span>';
  // Display the friendly tool name (Claude Code / Antigravity / …); the radio
  // ``value`` keeps the raw runtime id the route expects.
  return `<label class="ctx-pull-candidate${selectable ? '' : ' ctx-pull-candidate-disabled'}">
    ${control}
    <span class="ctx-pull-runtime">${escapeHtml(_ctxRuntimeLabel(c.runtime))}</span>
    ${_ctxPullContentBadge(c.content_status)}
    ${_ctxPullGateBadge(c.gate_status)}
    ${auto}
    ${override}
    ${reason}
  </label>`;
}

// Paint the cached preview into the candidate table + gate the Apply button.
// Re-runnable on ``langchange`` (reads ``state.lastPreview`` — no fetch).
function _ctxRenderPullPreview(state) {
  const subjectEl = qs('ctx-pull-subject');
  const listEl = qs('ctx-pull-candidates');
  const overwriteRow = qs('ctx-pull-overwrite-row');
  const forceRow = qs('ctx-pull-force-row');
  const warnEl = qs('ctx-pull-warning');
  const applyBtn = qs('ctx-pull-apply-btn');
  if (!listEl || !warnEl || !applyBtn) return;
  if (subjectEl) {
    subjectEl.textContent = t('settings.ctx.pull_subject', {
      type: _ctxTypeNameSingular(state.kind),
      name: state.name,
    });
  }
  const p = state.lastPreview;
  const hideExtras = () => {
    // Hide AND clear — a pending/error preview between two tiers must not leave
    // an overwrite/force box checked to reappear against the new tier's store.
    if (overwriteRow) overwriteRow.hidden = true;
    if (forceRow) forceRow.hidden = true;
    _ctxClearPullConsent();
  };

  if (!p || p.kind === 'pending') {
    listEl.innerHTML = `<p class="ctx-pull-loading">${escapeHtml(t('settings.ctx.pull_loading'))}</p>`;
    hide(warnEl);
    hideExtras();
    applyBtn.disabled = true;
    return;
  }
  if (p.kind === 'error') {
    listEl.innerHTML = '';
    warnEl.textContent = p.message || t('toast.request_failed');
    show(warnEl);
    hideExtras();
    applyBtn.disabled = true;
    return;
  }

  // p.kind === 'preview'
  const d = p.data;
  const cands = d.candidates || [];
  listEl.innerHTML = cands.length
    ? cands.map((c) => _ctxPullCandidateRow(state, c, d.ambiguous, d.auto_source)).join('')
    : '';

  const selectable = cands.filter(_ctxPullSelectable);
  const selected = selectable.find((c) => c.runtime === state.selectedRuntime) || null;

  // Overwrite: only meaningful when the Store already holds this artifact.
  // Force-unsafe: only for a selected candidate whose privacy gate is a
  // force-bypassable warning (a hard project_shared block is NOT bypassable).
  // A consent checkbox is UNCHECKED whenever its row hides, so stale consent can
  // never ride along to a different source/tier once the row reappears
  // (``_ctxPullBody`` also guards on ``!hidden``, but clearing here is the
  // authoritative reset — Codex Major: hidden-but-checked consent reuse).
  if (overwriteRow) {
    overwriteRow.hidden = !d.store_present;
    const overwriteEl = qs('ctx-pull-overwrite');
    if (overwriteRow.hidden && overwriteEl) overwriteEl.checked = false;
  }
  const forceApplies = !!selected && selected.gate_status === 'requires_unsafe_confirmation';
  if (forceRow) {
    forceRow.hidden = !forceApplies;
    const forceEl = qs('ctx-pull-force');
    if (forceRow.hidden && forceEl) forceEl.checked = false;
  }

  // Warning + Apply gating.
  let warn = '';
  let canApply = false;
  if (!selectable.length) {
    warn = t('settings.ctx.pull_nothing', { type: _ctxTypeNameSingular(state.kind) });
  } else if (!selected) {
    // No selection: ambiguous copies (must pick) vs a not-yet-clicked auto row.
    warn = d.ambiguous ? t('settings.ctx.pull_ambiguous') : '';
  } else if (selected.gate_status === 'blocked') {
    warn = t('settings.ctx.pull_gate_hard_refuse');
  } else {
    canApply = true;
  }
  if (warn) { warnEl.textContent = warn; show(warnEl); } else { hide(warnEl); }
  applyBtn.disabled = !canApply;
}

// Build the apply body from the live controls. force_unsafe_import is included
// ONLY when the checkbox is checked and visible — it must be a literal ``true``
// (the route validates literal-true; a coercible value would not bypass).
function _ctxPullBody(state) {
  const overwriteEl = qs('ctx-pull-overwrite');
  const overwriteRow = qs('ctx-pull-overwrite-row');
  const forceEl = qs('ctx-pull-force');
  const forceRow = qs('ctx-pull-force-row');
  const overwrite = !!(overwriteEl && overwriteEl.checked && overwriteRow && !overwriteRow.hidden);
  const body = { source_runtime: state.selectedRuntime, overwrite };
  if (forceEl && forceEl.checked && forceRow && !forceRow.hidden) {
    body.force_unsafe_import = true;
  }
  return body;
}

async function _ctxPullPost(state, body) {
  const csrf = await ensureCsrfToken();
  const headers = csrf
    ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
    : { 'Content-Type': 'application/json' };
  const r = await fetch(_ctxPullUrl(state, '/pull'), {
    method: 'POST', headers, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

function _ctxPullShowWarning(state, message) {
  const warnEl = qs('ctx-pull-warning');
  if (warnEl) { warnEl.textContent = message; show(warnEl); }
}

// Uncheck the consent boxes. ``forceOnly`` clears just the privacy valve (a
// source change keeps the same tier's overwrite intent but must not carry a
// force-bypass to a different candidate's bytes); otherwise both are cleared (a
// tier change re-targets a different store entirely).
function _ctxClearPullConsent(forceOnly) {
  if (!forceOnly) {
    const o = qs('ctx-pull-overwrite');
    if (o) o.checked = false;
  }
  const f = qs('ctx-pull-force');
  if (f) f.checked = false;
}

function _ctxSetPullControlsDisabled(modalEl, disabled) {
  modalEl
    .querySelectorAll('input[name="ctx-pull-source"], input[name="ctx-pull-tier"], #ctx-pull-overwrite, #ctx-pull-force')
    .forEach((el) => { el.disabled = disabled; });
}

// Apply the Pull: POST without a consent flag, run the tier-keyed gate round-
// trip if the route asks for confirmation, then the success refresh. The gates
// are mutually exclusive (the route emits at most one).
async function _ctxPullApply(state) {
  const modalEl = qs('ctx-pull-modal');
  const applyBtn = qs('ctx-pull-apply-btn');
  const body = _ctxPullBody(state);
  _ctxSetPullControlsDisabled(modalEl, true);
  btnLoading(applyBtn, true);
  const owns = () => _ctxPullState === state;
  // Track the confirm-disclosure hide so EVERY exit after it (decline, network
  // failure on the re-POST, a superseding close) restores the picker — a hidden
  // modal whose modal-stack entry is still live would otherwise strand the page.
  let hiddenForConfirm = false;
  try {
    let res = await _ctxPullPost(state, body);
    if (!owns()) return;
    if (res.ok && res.data && res.data.status === 'needs_confirmation') {
      // The confirm dialog (#confirm-modal) shares the overlay z-index and sits
      // earlier in the DOM, so it would stack UNDER this modal; hide this one
      // for the disclosure and restore it afterwards (Move/Copy precedent).
      hide(modalEl);
      hiddenForConfirm = true;
      const conf = res.data;
      let ok;
      if (conf.confirm === 'allow_host_writes') {
        const targets = (conf.host_targets || []).slice(0, 8).join('\n');
        ok = await showConfirm({
          title: t('settings.ctx.pull_host_confirm_title'),
          message: t('settings.ctx.pull_host_confirm_message') + (targets ? '\n\n' + targets : ''),
          confirmText: t('settings.ctx.pull_apply'),
        });
      } else {
        // Localized keys, NOT conf.reason — the backend reason is raw developer
        // prose that would shadow the i18n fallback in every locale (#1348).
        ok = await showConfirm({
          title: t('settings.ctx.pull_shared_confirm_title'),
          message: t('settings.ctx.pull_shared_confirm_message'),
          confirmText: t('settings.ctx.pull_apply'),
        });
      }
      if (!owns()) return;
      if (!ok) { show(modalEl); hiddenForConfirm = false; return; } // declined
      res = await _ctxPullPost(state, { ...body, [conf.confirm]: true });
      if (!owns()) return;
      show(modalEl);
      hiddenForConfirm = false;
    }
    _ctxHandlePullResult(state, res);
  } catch (e) {
    if (owns()) {
      if (hiddenForConfirm) show(modalEl); // restore before surfacing the error
      _ctxPullShowWarning(state, (e && e.message) || t('toast.request_failed'));
    }
  } finally {
    // Shared static DOM: only the owning session touches it. A close+reopen (or
    // an applied-success close, which nulls _ctxPullState) hands ownership on, so
    // ownership alone is the correct guard for re-enabling the controls.
    if (owns()) {
      btnLoading(applyBtn, false);
      _ctxSetPullControlsDisabled(modalEl, false);
      // ``btnLoading(false)`` re-enables Apply, but a refusal kicked off an
      // unawaited re-preview (``lastPreview`` is now ``pending``) — keep Apply
      // disabled until that preview resolves, so the stale body can't be
      // re-submitted in the gap (Codex Major).
      if (state.lastPreview && state.lastPreview.kind === 'pending') applyBtn.disabled = true;
    }
  }
}

// Route the apply outcome. applied → success toast + close + refresh. Anything
// else — an error envelope (503/409/500/400) or a result-coded refusal — surfaces
// the (server-sanitized) reason as a toast and RE-PREVIEWS: an apply-time refusal
// means the Store changed since the preview (a canonical appeared →
// canonical_exists, a new gate hit, plan_stale), so the table + consent controls
// must be recomputed against current reality, not left showing the stale plan.
function _ctxHandlePullResult(state, res) {
  if (res.ok && (res.data || {}).status === 'applied') {
    _ctxPullClose(state);
    showToast(t('settings.ctx.pull_success', {
      type: _ctxTypeNameSingular(state.kind),
      name: state.name,
      from: _ctxRuntimeLabel(res.data.selected_runtime || ''),
    }));
    // Refresh the list/detail ONLY when the destination the Pull landed in is
    // the one currently on screen — ``loadCtxList``/``loadCtxDetail`` key off the
    // LIVE global ``_ctxTargetScope`` + effective project, so a tier mismatch
    // (pulling into ``user`` while viewing ``project_shared``) OR a project
    // switch mid-request would otherwise reload the wrong pane (unchanged copy
    // or a 404). The toast confirms the write regardless. A user-tier Pull is
    // project-independent, so only project tiers gate on the pinned scope.
    const sameTier = state.tier === _ctxTargetScope;
    const sameProject = state.tier === 'user' || state.scopeId === _ctxEffectiveScopeId();
    if (sameTier && sameProject) {
      if (typeof loadCtxList === 'function') loadCtxList(state.kind);
      if (typeof loadCtxDetail === 'function') loadCtxDetail(state.kind, state.name);
    }
    return;
  }
  const message = res.ok
    ? ((res.data || {}).reason || t('toast.request_failed'))
    : _ctxPullErr(res.data && res.data.detail);
  // Toast (not the in-modal warning) so the message survives the re-preview's
  // repaint, which clears the warning line.
  showToast(message, 'warning');
  _ctxPullPreview(state);
}

function _ctxResetPullControls(modalEl) {
  _ctxSetPullControlsDisabled(modalEl, false);
  const list = qs('ctx-pull-candidates');
  if (list) list.innerHTML = '';
  const overwrite = qs('ctx-pull-overwrite');
  if (overwrite) overwrite.checked = false;
  const force = qs('ctx-pull-force');
  if (force) force.checked = false;
  const overwriteRow = qs('ctx-pull-overwrite-row');
  if (overwriteRow) overwriteRow.hidden = true;
  const forceRow = qs('ctx-pull-force-row');
  if (forceRow) forceRow.hidden = true;
  const warnEl = qs('ctx-pull-warning');
  if (warnEl) hide(warnEl);
  const applyBtn = qs('ctx-pull-apply-btn');
  if (applyBtn) { btnLoading(applyBtn, false); applyBtn.disabled = true; }
}

function _ctxPullClose(state) {
  if (state && state._teardown) { state._teardown(); state._teardown = null; }
  if (_ctxPullState === state) {
    const modalEl = qs('ctx-pull-modal');
    if (modalEl) {
      hide(modalEl);
      _ctxResetPullControls(modalEl);
    }
    _ctxPullState = null;
  }
}

function _ctxOpenPullModal(kind, name) {
  const modalEl = qs('ctx-pull-modal');
  if (!modalEl || !_ctxCanPull(kind)) return;
  // A prior session (a double / programmatic reopen) must be torn down first —
  // the modal markup is shared static DOM, so replacing the owner without
  // closing it would stack listeners and leak a second modal-stack entry that a
  // single Apply-success close can't release (leaving the page inert).
  if (_ctxPullState) _ctxPullClose(_ctxPullState);
  _ctxResetPullControls(modalEl);

  // Default the destination tier to project_shared (the pull-preview route's
  // default) — the user can switch to their personal library. Pin the effective
  // project scope ONCE (like the Move/Copy modal's ``srcScopeIdEff``) so a
  // mid-modal project switch can't re-target the preview/apply.
  const defaultTier = 'project_shared';
  const state = {
    kind, name, tier: defaultTier, scopeId: _ctxEffectiveScopeId(),
    selectedRuntime: null, lastPreview: null, _teardown: null,
  };
  _ctxPullState = state;

  modalEl.querySelectorAll('input[name="ctx-pull-tier"]').forEach((el) => {
    el.checked = el.value === defaultTier;
  });

  const onTierChange = () => {
    const t2 = modalEl.querySelector('input[name="ctx-pull-tier"]:checked');
    state.tier = (t2 && t2.value) || 'project_shared';
    state.selectedRuntime = null;
    _ctxClearPullConsent(); // a different store — drop overwrite AND force intent
    _ctxPullPreview(state); // tier changes both the privacy gate and the content
  };
  const onSourceChange = (e) => {
    if (!e.target || e.target.name !== 'ctx-pull-source') return;
    state.selectedRuntime = e.target.value;
    _ctxClearPullConsent(true); // force applies to the SELECTED bytes — never carry it over
    _ctxRenderPullPreview(state); // repaint force-valve visibility + Apply
  };
  const onForceChange = () => _ctxRenderPullPreview(state);
  const onApply = () => _ctxPullApply(state);
  const onCancel = () => _ctxPullClose(state);
  const onBackdrop = (e) => { if (e.target === modalEl) _ctxPullClose(state); };
  const onKey = (e) => {
    if (e.key === 'Escape' && !modalEl.hidden) { e.stopPropagation(); _ctxPullClose(state); }
  };

  const tierRadios = modalEl.querySelectorAll('input[name="ctx-pull-tier"]');
  const listEl = qs('ctx-pull-candidates');
  const forceEl = qs('ctx-pull-force');
  const applyBtn = qs('ctx-pull-apply-btn');
  const cancelBtn = qs('ctx-pull-cancel-btn');

  const releaseA11y = window.openModal(modalEl, {
    // Only VISIBLE, ENABLED controls trap focus — a hidden-row input (overwrite
    // / force before they apply) or the disabled Apply button would otherwise
    // swallow Tab and strand keyboard users (``offsetParent === null`` ⇔ a
    // ``[hidden]`` ancestor).
    focusables: () => Array.from(modalEl.querySelectorAll('input, button'))
      .filter((el) => !el.disabled && el.offsetParent !== null),
  });
  window.registerModalCloser(modalEl, () => _ctxPullClose(state));

  state._teardown = () => {
    tierRadios.forEach((el) => el.removeEventListener('change', onTierChange));
    if (listEl) listEl.removeEventListener('change', onSourceChange);
    if (forceEl) forceEl.removeEventListener('change', onForceChange);
    if (applyBtn) applyBtn.removeEventListener('click', onApply);
    if (cancelBtn) cancelBtn.removeEventListener('click', onCancel);
    modalEl.removeEventListener('mousedown', onBackdrop);
    modalEl.removeEventListener('keydown', onKey);
    if (releaseA11y) releaseA11y();
  };

  tierRadios.forEach((el) => el.addEventListener('change', onTierChange));
  if (listEl) listEl.addEventListener('change', onSourceChange); // delegated
  if (forceEl) forceEl.addEventListener('change', onForceChange);
  if (applyBtn) applyBtn.addEventListener('click', onApply);
  if (cancelBtn) cancelBtn.addEventListener('click', onCancel);
  modalEl.addEventListener('mousedown', onBackdrop);
  modalEl.addEventListener('keydown', onKey);

  _ctxRenderPullPreview(state); // paint subject + reset Apply
  _ctxPullPreview(state);       // kick the first preview

  const first = modalEl.querySelector('input[name="ctx-pull-tier"]:checked') || cancelBtn;
  if (first && typeof first.focus === 'function') first.focus();
}

window.ctxOpenPullModal = _ctxOpenPullModal;
window.ctxCanPull = _ctxCanPull;

// Re-paint the JS-owned candidate table / warnings without re-fetching when the
// locale flips (mirrors the Move/Copy langchange listener).
window.addEventListener('langchange', () => {
  if (_ctxPullState) _ctxRenderPullPreview(_ctxPullState);
});

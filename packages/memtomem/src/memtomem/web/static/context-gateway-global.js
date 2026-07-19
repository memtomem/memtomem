// ADR-0030 PR-F2 — user-tier "global library" landing.
//
// The user tier is one global ~/.memtomem Store with no per-project fan-out, so
// it does NOT ride the project_shared-only /context/status-all fleet endpoint
// (context-portal.js) — it has its own GET /api/context/status-global (PR-F1).
// This section shows the global inventory counts plus a READ-ONLY pull-direction
// drift summary ("a tool's copy differs from your Store"); Pull is a leaf action
// into the SHARED #ctx-pull-modal (context-gateway-pull.js), defaulted to the
// user tier. Nothing here writes — detection is automatic, writes stay explicit
// (ADR-0030 §1).
//
// Supersession-guarded like the portal drift loader (context-portal.js): a
// monotonic sequence drops a slow response the user has since refreshed past.

let _ctxGlobalSeq = 0;      // section-body render supersession (loadCtxGlobal only)
let _ctxGlobalNavSeq = 0;   // nav-dot supersession, SHARED by the loader + eager probe
let _ctxGlobalLast = null;  // last payload, for a no-refetch langchange repaint

// Flip the sidebar glance-dot (mirrors wiki.js _renderWikiNavBadge): role=img +
// hidden, no aria-live — a glance signal folded into the nav button's accessible
// name, never announced as it appears (ctx dashboard a11y convention).
function _ctxRenderGlobalNavDot(hasDrift) {
  const dot = document.querySelector(
    '.settings-nav-btn[data-section="ctx-global"] .ctx-global-nav-dot',
  );
  if (dot) dot.hidden = !hasDrift;
}

// Commit a nav-dot write only if it is still the latest one INITIATED — the
// loader and the eager probe both set the dot asynchronously, so without a
// shared sequence a slow eager probe could resolve after a newer clean load (or
// a post-Pull refresh) and re-light stale drift (Codex F2). Last-initiated wins.
function _ctxCommitNavDot(navSeq, hasDrift) {
  if (navSeq !== _ctxGlobalNavSeq) return;
  _ctxRenderGlobalNavDot(hasDrift);
}

function _ctxGlobalVerdictBadge(verdict) {
  // Pull-direction verdict — deliberately distinct from the push-direction
  // ctx-scope-badge--drift on the project roster (opposite axis).
  return `<span class="ctx-global-badge ctx-global-badge--${escapeHtml(verdict)}">`
    + `${escapeHtml(t('settings.ctx.global_verdict_' + verdict))}</span>`;
}

function _ctxGlobalRow(row) {
  const canPull = row.verdict === 'differs'
    && typeof window.ctxCanPull === 'function' && window.ctxCanPull(row.kind);
  let detail = '';
  if (row.verdict === 'differs' && (row.runtimes || []).length) {
    const runtimes = row.runtimes.map((r) => _ctxRuntimeLabel(r)).join(', ');
    detail = `<span class="ctx-global-row-runtimes">${escapeHtml(runtimes)}</span>`;
  } else if (row.verdict === 'error' && row.reason) {
    // ``reason`` is display-sanitized server-side (_redact_pull_reason); escape
    // again at the sink as defense in depth.
    detail = `<span class="ctx-global-row-reason">${escapeHtml(row.reason)}</span>`;
  }
  // Every row's button shares the same visible label ("Preview / Pull"), so a
  // per-row aria-label names the artifact — button-only SR navigation otherwise
  // can't tell the rows apart (Codex a11y).
  const pullAria = `${t('settings.ctx.global_pull')}: ${t('settings.ctx.kind_' + row.kind)} ${row.name}`;
  const action = canPull
    ? `<button class="btn-ghost ctx-global-pull-btn" type="button"`
      + ` data-kind="${escapeHtml(row.kind)}" data-name="${escapeHtml(row.name)}"`
      + ` aria-label="${escapeHtml(pullAria)}">`
      + `${escapeHtml(t('settings.ctx.global_pull'))}</button>`
    : '';
  return `<div class="ctx-global-row" data-verdict="${escapeHtml(row.verdict)}">
    <span class="ctx-global-row-kind">${escapeHtml(t('settings.ctx.kind_' + row.kind))}</span>
    <span class="ctx-global-row-name">${escapeHtml(row.name)}</span>
    ${_ctxGlobalVerdictBadge(row.verdict)}
    ${detail}
    ${action}
  </div>`;
}

function _ctxRenderCtxGlobal(bodyEl, data) {
  const store = data.store || {};
  const drift = data.pull_drift || { rows: [], has_pull_drift: false };
  const rows = drift.rows || [];

  // Inventory counts read as a plural quantity ("2 Skills"), so use the plural
  // section-title labels — NOT the singular per-row kind_* labels (Codex).
  const _KIND_PLURAL = {
    skills: 'settings.ctx.skills_title',
    agents: 'settings.ctx.agents_title',
    commands: 'settings.ctx.commands_title',
  };
  const counts = ['skills', 'agents', 'commands'].map((k) =>
    `<span class="ctx-global-count"><strong>${escapeHtml(String(store[k] || 0))}</strong> `
    + `${escapeHtml(t(_KIND_PLURAL[k]))}</span>`).join('');

  let driftHtml;
  if (!rows.length) {
    driftHtml = `<p class="ctx-global-empty">${escapeHtml(t('settings.ctx.global_empty'))}</p>`;
  } else {
    const summary = drift.has_pull_drift
      ? `<p class="ctx-global-drift-summary ctx-global-drift-summary--drift">`
        + `${escapeHtml(t('settings.ctx.global_drift_summary'))}</p>`
      : `<p class="ctx-global-drift-summary">${escapeHtml(t('settings.ctx.global_in_sync'))}</p>`;
    driftHtml = summary + `<div class="ctx-global-rows">${rows.map(_ctxGlobalRow).join('')}</div>`;
  }

  bodyEl.innerHTML = `<div class="ctx-global-inventory">${counts}</div>${driftHtml}`;

  bodyEl.querySelectorAll('.ctx-global-pull-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      // These rows are user-tier drift, so default the modal to the user tier
      // (the picker still lets the user switch). The shared modal owns its own
      // supersession/teardown (context-gateway-pull.js).
      if (typeof window.ctxOpenPullModal === 'function') {
        window.ctxOpenPullModal(btn.dataset.kind, btn.dataset.name, 'user');
      }
    });
  });
}

async function loadCtxGlobal() {
  const seq = ++_ctxGlobalSeq;
  const navSeq = ++_ctxGlobalNavSeq; // claim the latest dot write too
  const bodyEl = qs('ctx-global-content');
  if (!bodyEl) return;
  bodyEl.innerHTML = `<div class="ctx-global-loading">`
    + `${escapeHtml(t('settings.ctx.global_loading'))}</div>`;
  let data;
  try {
    const resp = await fetch('/api/context/status-global', { method: 'GET' });
    if (!resp.ok) throw new Error('status-global');
    data = await resp.json();
  } catch (e) {
    if (seq !== _ctxGlobalSeq) return;
    _ctxGlobalLast = null;
    bodyEl.innerHTML = `<div class="ctx-global-error" role="status">`
      + `${escapeHtml(t('settings.ctx.global_load_failed'))}</div>`;
    _ctxCommitNavDot(navSeq, false);
    return;
  }
  if (seq !== _ctxGlobalSeq) return; // superseded by a newer refresh
  _ctxGlobalLast = data;
  _ctxRenderCtxGlobal(bodyEl, data);
  _ctxCommitNavDot(navSeq, !!(data.pull_drift && data.pull_drift.has_pull_drift));
}

// Eager nav-dot probe so the sidebar glance-dot is correct on a cold gateway
// open, before the Global section has been visited (ADR-0030 §1: detection at
// portal open, alongside the fleet check — mirrors wiki.js _probeWikiNavStatus).
// Sets ONLY the dot; the section body is rendered lazily by loadCtxGlobal when
// the user opens the section. Best-effort — any failure leaves the dot hidden
// rather than surfacing an error. Called from app.js on gateway activation
// (skipped when Global IS the landing section, which already runs loadCtxGlobal).
async function _probeGlobalNavStatus() {
  const navSeq = ++_ctxGlobalNavSeq;
  try {
    const resp = await fetch('/api/context/status-global', { method: 'GET' });
    if (!resp.ok) { _ctxCommitNavDot(navSeq, false); return; }
    const data = await resp.json();
    _ctxCommitNavDot(navSeq, !!(data.pull_drift && data.pull_drift.has_pull_drift));
  } catch (e) {
    _ctxCommitNavDot(navSeq, false);
  }
}

// Re-paint the JS-owned rows from cached data on a locale flip (mirrors the
// pull-picker langchange listener — repaint, never re-fetch). Static section
// chrome is covered by data-i18n; the rows are not.
window.addEventListener('langchange', () => {
  const bodyEl = qs('ctx-global-content');
  if (bodyEl && _ctxGlobalLast) _ctxRenderCtxGlobal(bodyEl, _ctxGlobalLast);
});

// Refresh re-runs the probe (the loader is supersession-guarded, so a rapid
// double-click can't paint a stale response). Wired once at load — the section's
// static DOM is present (this classic script runs at end of body).
document.getElementById('ctx-global-refresh-btn')
  ?.addEventListener('click', () => loadCtxGlobal());

window.loadCtxGlobal = loadCtxGlobal;
window._probeGlobalNavStatus = _probeGlobalNavStatus;

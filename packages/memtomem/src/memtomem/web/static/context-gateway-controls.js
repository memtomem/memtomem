/**
 * Context Gateway — part 2/7: controls. Hoisted control bar, the tier-aware
 * write-block gate (#943), the #1263 user-tier host-write confirm round-trip,
 * and the ADR-0009 deep-link carrier. Classic script (#1517) — see
 * context-gateway-core.js for the split rationale.
 *
 *   depends on: app.js globals; context-gateway-core.js state + scope helpers
 *   provides:   _ctxRenderToolbars (runs at load), _ctxRefreshWriteBlockedState,
 *               _ctxBumpActiveScopeDetailSeq, deep-link helpers (_ctxParseDeepLink,
 *               _ctxSetDeepLink, _ctxClearDeepLink) — consumed by context-portal.js
 */

// -- Hoisted gateway control bar (rank 11) ------------------------------------
//
// The active-project ``<select>`` and the canonical-tier filter used to be
// re-emitted into EVERY gateway section's content (Overview, Skills, Commands,
// Agents, MCP, Hooks) — the same picker painted 6× for one piece of state,
// since ``_ctxActiveScopeId`` / ``_ctxTargetScope`` are module globals. They now
// render ONCE into the persistent ``#ctx-control-bar`` host that sits above the
// section panels. ``_ctxRenderControlBar`` repaints that single host for the
// active section; the unchanged ``_ctxWireProjectControls`` / ``_ctxWireTier
// Controls`` helpers route a change to the active section's loader via the
// control's ``data-type`` (overview→loadCtxOverview, hooks-sync→loadHooksSync,
// else→loadCtxList). The Projects portal owns its own roster and never carried
// these controls, so the bar is hidden there.
const _CTX_SECTION_BAR_TYPE = {
  'ctx-overview': 'overview',
  'ctx-skills': 'skills',
  'ctx-commands': 'commands',
  'ctx-agents': 'agents',
  'ctx-mcp-servers': 'mcp-servers',
  'hooks-sync': 'hooks-sync',
  // ``ctx-projects`` is intentionally absent → the bar hides on the portal.
};

// The control "type" of the active gateway section, or '' when none applies
// (Projects portal, or no gateway section active). The active section id is
// ``settings-<section>`` (e.g. ``settings-ctx-skills`` / ``settings-hooks-sync``);
// strip the prefix, then map to the loader-routing type.
function _ctxActiveGatewayType() {
  const active = document.querySelector('#tab-context-gateway .settings-section.active');
  if (!active || !active.id) return '';
  const section = active.id.replace(/^settings-/, '');
  return _CTX_SECTION_BAR_TYPE[section] || '';
}

// Repaint the persistent control bar for whatever gateway section is currently
// active — ALWAYS sourced from the live ``.settings-section.active`` (never a
// caller-supplied type). This is deliberate: the bar is one shared, visible
// host, and the loaders that trigger a repaint are async. If a stale
// ``loadCtxOverview`` / ``loadCtxList`` resolves AFTER the user has navigated to
// a different section, sourcing the type from the active section makes the late
// render paint the bar for the section the user is actually on (or hide it on
// the Projects portal) instead of hijacking it back to the loader's section and
// mis-routing the next tier/project change. Re-rendering replaces the host's
// markup, so the (idempotent, global) wire helpers re-bind the single live
// instance and the detached prior nodes drop their listeners with them.
function _ctxRenderControlBar() {
  const host = document.getElementById('ctx-control-bar');
  if (!host) return;
  const type = _ctxActiveGatewayType();
  if (!type) {
    host.hidden = true;
    host.innerHTML = '';
    return;
  }
  host.hidden = false;
  host.innerHTML = _ctxProjectControls(type) + _ctxTierControls(type);
  _ctxWireProjectControls();
  _ctxWireTierControls();
  // Re-apply a Sync All lock to the freshly-rendered controls so a repaint
  // mid-run (navigation / loader / langchange) can't silently re-enable them.
  _ctxApplySyncControlsLock();
}

function _ctxProjectControls(type, scopes = _ctxProjectsCache) {
  const list = Array.isArray(scopes) ? scopes : [];
  if (!list.length) return '';
  const options = list.map(scope => {
    const label = _ctxScopeDisplayLabel(scope);
    const suffix = scope.missing
      ? ` ${t('settings.ctx.scope_missing')}`
      : '';
    const selected = _ctxScopeIsActive(scope) ? ' selected' : '';
    // Missing scopes stay listed (the roster should show what's registered)
    // but are not actionable: selecting one would just have
    // ``_ctxNormalizeActiveScope`` silently snap back to Server-CWD and
    // persist the demotion (#1247 id 25). ``disabled`` makes the
    // non-actionability visible instead. The active scope can never be a
    // missing one (normalize filters them), so this can't disable the
    // current selection.
    const disabled = scope.missing ? ' disabled' : '';
    return `<option value="${escapeHtml(scope.scope_id)}"${selected}${disabled}>${escapeHtml(label + suffix)}</option>`;
  }).join('');
  return `<label class="ctx-project-switcher" data-type="${escapeHtml(type)}">
    <span>${escapeHtml(t('settings.ctx.active_project'))}</span>
    <select class="ctx-project-select">${options}</select>
  </label>`;
}

function _ctxWireProjectControls() {
  document.querySelectorAll('.ctx-project-select').forEach(select => {
    if (select.dataset.scopeWired === 'true') return;
    select.dataset.scopeWired = 'true';
    select.addEventListener('change', () => {
      // Server CWD is intentionally represented by an empty scope_id, so
      // treat '' as a valid selection and only short-circuit a no-op re-pick.
      const next = select.value || '';
      if (next === _ctxActiveScopeId) return;
      _ctxActiveScopeId = next;
      // A prior Sync All summary belongs to the project it ran on; drop it on
      // a project switch so the summary can't be misread against a new scope.
      _renderCtxSyncStatus(null);
      _ctxNormalizeActiveScope(_ctxProjectsCache);
      _ctxBumpActiveScopeDetailSeq();
      try { localStorage.setItem(_CTX_ACTIVE_SCOPE_KEY, _ctxActiveScopeId); } catch {}
      _ctxClearDeepLink();
      const type = select.closest('.ctx-project-switcher')?.dataset.type || '';
      if (type === 'overview') {
        loadCtxOverview();
      } else if (type === 'hooks-sync') {
        loadHooksSync();
      } else if (type) {
        loadCtxList(type);
      }
    });
  });
}

function _ctxBumpActiveScopeDetailSeq() {
  for (const scopeType of Object.keys(_ctxDetailSeq)) {
    if (typeof _ctxDetailSeq[scopeType] === 'number') {
      _ctxDetailSeq[scopeType] += 1;
      // Abort any in-flight detail fetch for the now-stale scope (#1286): it
      // would otherwise resolve and paint into a pane the scope switch
      // invalidated. The next mount mints a fresh controller via
      // ``_ctxSwapAbort``; leaving the aborted one in place is harmless.
      try { _ctxDetailAbort[scopeType]?.abort(); } catch { /* no-op */ }
    }
  }
}

function _ctxTierControls(type) {
  // The visible "Stored in" label gets its own ``.ctx-tier-switcher``
  // wrapper, styled by the same CSS rules as ``.ctx-project-switcher`` so
  // the two bar controls read as one family. It must NOT reuse the
  // project-switcher class itself: the rank-11 hoist guard
  // (tests-js/ctx-control-bar-hoist.test.mjs) pins exactly one
  // ``.ctx-project-switcher`` in the document and reads ``dataset.type``
  // off the first match. The wrapper is a <div>, NOT a <label> — buttons
  // are labelable elements, so a <label> wrapper would forward clicks on
  // the label text to the first tier button.
  // ``data-type`` stays on ``.ctx-tier-filter`` (``_ctxWireTierControls``
  // reads it via ``btn.closest('.ctx-tier-filter')``), and the buttons stay
  // inside ``.ctx-tier-filter`` so the sync-lock / browser-test selectors
  // (``#ctx-control-bar .ctx-tier-filter button``) keep matching.
  // ``aria-pressed`` + per-tier ``title`` answer "what does this tier mean"
  // on hover / to AT; both re-render with the bar, so no wiring change.
  const btn = (scope, optionKey, tooltipKey) =>
    `<button type="button" data-scope="${scope}"`
    + ` aria-pressed="${_ctxTargetScope === scope}"`
    + ` title="${escapeHtml(t(tooltipKey))}"`
    + ` class="${_ctxTargetScope === scope ? 'active' : ''}">`
    + `${escapeHtml(t(optionKey))}</button>`;
  return `<div class="ctx-tier-switcher">
    <span>${escapeHtml(t('settings.ctx.tier_filter'))}</span><span class="help-tip" data-help="${escapeHtml(t('settings.ctx.tier_glossary'))}" tabindex="0" role="img" aria-label="${escapeHtml(t('settings.ctx.tier_glossary'))}">i</span>
    <div class="ctx-tier-filter" data-type="${escapeHtml(type)}" role="group" aria-label="${escapeHtml(t('settings.ctx.tier_filter'))}">
    ${btn('user', 'settings.ctx.tier_option_user', 'settings.ctx.tier_tooltip_user')}
    ${btn('project_shared', 'settings.ctx.tier_option_project_shared', 'settings.ctx.tier_tooltip_project_shared')}
    ${btn('project_local', 'settings.ctx.tier_option_project_local', 'settings.ctx.tier_tooltip_project_local')}
  </div>
  </div>`;
}

function _ctxWireTierControls() {
  document.querySelectorAll('.ctx-tier-filter button').forEach(btn => {
    if (btn.dataset.tierWired === 'true') return;
    btn.dataset.tierWired = 'true';
    btn.addEventListener('click', () => {
      const next = btn.dataset.scope;
      if (!next || next === _ctxTargetScope) return;
      _ctxTargetScope = next;
      // A prior Sync All summary belongs to the tier it ran on; drop it so a
      // tier switch doesn't leave a result summary that no longer applies.
      _renderCtxSyncStatus(null);
      // Update write-blocked affordances synchronously so the user sees
      // the dim/banner change immediately, before the async list refetch
      // settles. ``loadCtxList`` / ``loadCtxOverview`` re-apply on success
      // (their callees call ``_ctxRefreshWriteBlockedState`` post-render).
      _ctxRefreshWriteBlockedState();
      const type = btn.closest('.ctx-tier-filter')?.dataset.type || '';
      if (type === 'overview') {
        loadCtxOverview();
      } else if (type === 'hooks-sync') {
        loadHooksSync();
      } else if (type) {
        // Tier swap is a fresh navigation intent; the prior deep-link's
        // filter/artifact target lived on the old tier and would render
        // an empty list (artifact missing) or a confusing partial filter
        // on the new one. Drop it so the user sees the new tier's full
        // list, not a silently-filtered subset.
        _ctxClearDeepLink();
        loadCtxList(type);
      }
    });
  });
}

// rank 2c: the "Show all projects" toggle. Rendered only when there is more
// than one scope (a single Server-CWD-only install has nothing to collapse,
// so the toggle would be a dead checkbox). The count in the label is the
// total scope count so the user knows how large the roster they're hiding
// is — the same framing as the Projects portal's row count.
function _ctxShowAllScopesControl(type, scopes) {
  const list = Array.isArray(scopes) ? scopes : [];
  if (list.length <= 1) return '';
  const label = t('settings.ctx.show_all_projects').replace('{n}', String(list.length));
  return `<label class="ctx-list-show-all" data-type="${escapeHtml(type)}">
    <input type="checkbox" id="ctx-${escapeHtml(type)}-show-all"${_ctxListShowAllScopes ? ' checked' : ''}>
    <span>${escapeHtml(label)}</span>
  </label>`;
}

function _ctxWireShowAllScopes(type, listEl) {
  const toggle = listEl.querySelector(`#ctx-${type}-show-all`);
  if (!toggle) return;
  toggle.addEventListener('change', () => {
    _ctxListShowAllScopes = toggle.checked;
    // Re-run the section so the scope loop re-filters; mirrors how the
    // project switcher / tier filter re-issue ``loadCtxList`` on change.
    loadCtxList(type);
  });
}

// -- Tier-aware write-block gate (issue #943) ---------------------------------
//
// ADR-0011 / #940 wired ``target_scope`` through every artifact route. The
// server gate is split (#1263): ``project_local`` writes 400 via
// ``_reject_project_local_write`` (mcp-servers/versions keep the stricter
// ``_reject_non_shared_write``), while unconfirmed ``user``-tier writes on
// skills/commands/agents return a 200 ``needs_confirmation`` envelope that
// ``_ctxConfirmHostWrite`` re-sends with ``allow_host_writes`` after the
// user approves the disclosed host paths. The client-side block below
// therefore covers project_local fully, and on the user tier only the
// surfaces with no user-tier route (version store, portal per-project
// sync, Sync All). Without the affordance, users who switch the tier
// filter would only learn an operation is blocked from a generic toast.
// #943 closed that UX gap by tagging every still-blocked write affordance
// with ``data-write-blocked="<tier>"`` so:
//
//   (1) CSS dims the button (``[data-write-blocked]`` selector in style.css),
//   (2) ``aria-disabled="true"`` announces the state to screen readers,
//   (3) the native ``title`` carries the tier-aware explanation, and
//   (4) a document-level capture-phase click handler intercepts the click
//       and fires a toast — the per-button handler never sees the event,
//       so no POST is ever issued.
//
// Per-section toolbar buttons (.ctx-create-btn / .ctx-import-btn /
// .ctx-sync-btn) are generated once at init by ``_ctxRenderToolbars`` (rank 21)
// and then persist in the DOM, so the refresh still applies on every render
// that touches the tier filter; per-item buttons (.ctx-detail-edit-btn /
// .ctx-detail-delete-btn) are minted by ``loadCtxDetail`` so its callers
// reapply the refresh after the detail innerHTML lands.
//
// The Sync All button stays governed by its existing
// ``data-runtime-only`` channel for the project_local-no-fanout and
// all-canonicals-empty cases; the user-tier case folds in here so a
// single tier-flip wires all five write affordances at once.

const _CTX_WRITE_BUTTON_SELECTOR = (
  '.ctx-create-btn, .ctx-import-btn, .ctx-sync-btn, '
  + '.ctx-detail-edit-btn, .ctx-detail-delete-btn, '
  // Per-project Sync moved from the (removed) Overview matrix to the Projects
  // portal card; it is the one tier-sensitive fan-out on the portal, so it
  // rides the same write-block sweep (portal add/remove are registry ops and
  // stay un-gated, as they were before).
  + '.ctx-portal-sync, '
  // The single-item runtime-only import route (#940 import_<type>)
  // also flows through the tier gate (project_local 400; user tier rides
  // the #1263 confirm round-trip), so the per-detail "Import this
  // <type>" button minted by ``_ctxLoadRuntimeOnlyDetail`` belongs in
  // the same write-blocked sweep.
  + '.ctx-runtime-only-import, '
  // ADR-0022 version-store writes (enable / freeze / promote / delete-label)
  // are project_shared-only canonical writes, so they ride the same tier gate.
  // ``.ctx-version-enable-btn`` (rank 6) adopts a flat artifact into dir layout
  // — also a project_shared-only canonical write.
  + '.ctx-version-enable-btn, '
  + '.ctx-version-freeze-btn, .ctx-version-promote-btn, .ctx-version-label-remove, '
  // ADR-0026 P1b: the Simple-mode inline Sync/Import buttons hit the SAME
  // tier-gated routes as the Advanced toolbar, so they ride the same write-block
  // sweep (project_local blocks both; user tier opens skills/commands/agents via
  // _CTX_USER_TIER_OPEN_SELECTOR below). Without this, a persisted
  // project_local + Simple session would open a confirm and POST a blocked write
  // instead of surfacing the no-write tier explanation up front (Codex review).
  + '.ctx-simple-action'
);

// rank 21: artifact-section toolbars (Skills / Commands / Agents / MCP Servers)
// render from one source instead of hand-copied static markup, so a button
// added here propagates to every section and each section's button set is a
// declared capability rather than an accidental copy-paste divergence.
//
//   - add_project / create / sync are universal.
//   - ``import`` is false for ``mcp-servers``: there is no per-type ``/import``
//     route (servers come from the single ``.mcp.json`` source — cf.
//     ``context_mcp_servers.py``, which ships no import endpoint), so the
//     omission is an explicit capability flag here, not a silent gap. The
//     user-facing "no Import" messaging lives in the MCP empty-state hint
//     (rank 7); this map keeps the structural omission from drifting.
//
// Buttons are emitted with the exact classes / ``data-type`` / ``data-i18n*``
// the static markup used, so the existing click bindings, the write-block
// sweep (``_CTX_WRITE_BUTTON_SELECTOR``), and i18n ``applyDOM`` keep working
// unchanged. Rendered once at init (below) — before the click bindings further
// down and before DOMContentLoaded's first ``applyDOM`` — so they behave like
// the old static buttons (English fallback text, translated on the first pass).
const _CTX_TOOLBAR_CAPS = {
  skills: { import: true },
  commands: { import: true },
  agents: { import: true },
  'mcp-servers': { import: false },
};

// ``data-type`` uses hyphens (``mcp-servers``) but the i18n keys use the
// underscore form (``mcp_servers_*``); bridge the two spellings here.
function _ctxToolbarI18nPrefix(type) {
  return type.replace(/-/g, '_');
}

function _ctxToolbarHtml(type) {
  const caps = _CTX_TOOLBAR_CAPS[type] || {};
  const p = _ctxToolbarI18nPrefix(type);
  const button = (cls, variant, labelKey, action, fallback) =>
    `<button class="${variant} ${cls}" data-type="${escapeHtml(type)}"`
    + ` data-i18n="${labelKey}"`
    + ` data-i18n-title="settings.ctx.${p}_${action}_tooltip"`
    + ` data-i18n-aria-label="settings.ctx.${p}_${action}_aria">${fallback}</button>`;
  const buttons = [
    button('ctx-add-project-btn', 'btn-ghost', 'settings.ctx.add_project', 'add_project', 'Add Project'),
    button('ctx-create-btn', 'btn-ghost', 'settings.ctx.create', 'create', 'Create'),
  ];
  if (caps.import) {
    buttons.push(button('ctx-import-btn', 'btn-ghost', 'settings.ctx.import', 'import', 'Import'));
  }
  // Sync stays rightmost and primary across every section.
  buttons.push(button('ctx-sync-btn', 'btn-primary', 'settings.ctx.sync', 'sync', 'Sync'));
  return buttons.join('\n');
}

// Fill the per-section ``.ctx-toolbar`` containers from the single template
// above. Runs at module load so the buttons exist for the ``querySelectorAll``
// click bindings and for the first write-block sweep, exactly as the static
// markup did.
function _ctxRenderToolbars() {
  document.querySelectorAll('.ctx-toolbar[data-type]').forEach(el => {
    el.innerHTML = _ctxToolbarHtml(el.dataset.type);
  });
}
_ctxRenderToolbars();

// Subset of ``_CTX_WRITE_BUTTON_SELECTOR`` whose routes accept user-tier
// writes behind the #1263 ``allow_host_writes`` confirm round-trip — these
// stay live on the user tier FOR the artifact families whose routes are
// open (``_CTX_USER_TIER_OPEN_TYPES``). The class match alone is not
// enough: the MCP Servers section mints the same button classes, but its
// routes stay project_shared-only by design (ADR-0011 §1) — unblocking
// them would send users into avoidable 400s (Codex review). Version-store
// writes and the portal per-project sync likewise remain
// project_shared-only server-side (ADR-0022 / multi-phase Sync All
// semantics) and keep the block on BOTH non-shared tiers; project_local
// blocks everything (no fan-out, ADR-0011 §3).
const _CTX_USER_TIER_OPEN_SELECTOR = (
  '.ctx-create-btn, .ctx-import-btn, .ctx-sync-btn, '
  + '.ctx-detail-edit-btn, .ctx-detail-delete-btn, .ctx-runtime-only-import, '
  // ADR-0026 P1b: Simple-mode inline Sync/Import for skills/commands/agents is
  // user-tier-open too (it rides the #1263 host-write confirm), exactly like the
  // Advanced toolbar buttons above. The _CTX_USER_TIER_OPEN_TYPES gate keeps the
  // open set to those three; mcp-servers never mints an inline action (no /import).
  + '.ctx-simple-action'
);
const _CTX_USER_TIER_OPEN_TYPES = new Set(['skills', 'commands', 'agents']);

// Artifact family a write button belongs to: the toolbar buttons carry
// ``data-type``; detail-minted buttons (edit / delete / runtime-only
// import) resolve through their enclosing ``settings-ctx-<type>`` section.
// Portal buttons live outside any section and resolve to '' (never open).
function _ctxBtnArtifactType(btn) {
  if (btn.dataset.type) return btn.dataset.type;
  const section = btn.closest('[id^="settings-ctx-"]');
  return section ? section.id.replace('settings-ctx-', '') : '';
}

function _ctxRefreshWriteBlockedState() {
  const tier = _ctxTargetScope;
  const tooltipKey = tier === 'project_local'
    ? 'settings.ctx.write_blocked_project_local_tooltip'
    : 'settings.ctx.write_blocked_user_tooltip';
  document.querySelectorAll(_CTX_WRITE_BUTTON_SELECTOR).forEach(btn => {
    const userTierOpen = btn.matches(_CTX_USER_TIER_OPEN_SELECTOR)
      && _CTX_USER_TIER_OPEN_TYPES.has(_ctxBtnArtifactType(btn));
    const blocked = tier === 'project_local'
      || (tier === 'user' && !userTierOpen);
    if (blocked) {
      btn.dataset.writeBlocked = tier;
      btn.setAttribute('aria-disabled', 'true');
      btn.title = t(tooltipKey);
    } else {
      delete btn.dataset.writeBlocked;
      btn.removeAttribute('aria-disabled');
      const titleKey = btn.dataset.i18nTitle;
      if (titleKey) {
        btn.title = t(titleKey);
      } else {
        btn.removeAttribute('title');
      }
    }
  });

  // Sync All deliberately stays a project_shared action (#1263): its
  // multi-phase run also hits settings + mcp-servers, which have no
  // user-tier write surface, so gate it here pre-click rather than
  // surfacing a mid-run mixed result. project_local already carries
  // ``data-runtime-only`` from ``_renderCtxOverview`` (no-fanout copy
  // is the more specific signal there) — leave that channel alone.
  const syncAll = document.getElementById('ctx-sync-all-btn');
  if (syncAll) {
    if (_ctxTargetScope === 'user') {
      syncAll.dataset.writeBlocked = 'user';
      syncAll.setAttribute('aria-disabled', 'true');
      syncAll.title = t('settings.ctx.write_blocked_user_tooltip');
    } else if (syncAll.dataset.writeBlocked === 'user') {
      // Only clear when WE set it — don't clobber project_local's
      // ``data-runtime-only`` ARIA / title state.
      delete syncAll.dataset.writeBlocked;
      if (!syncAll.dataset.runtimeOnly) {
        syncAll.removeAttribute('aria-disabled');
        const titleKey = syncAll.dataset.i18nTitle;
        if (titleKey) {
          syncAll.title = t(titleKey);
        } else {
          syncAll.removeAttribute('title');
        }
      }
    }
  }
}

// Document-level capture-phase intercept. Capture phase fires before
// the per-button click handlers registered at module-init, so a blocked
// click never reaches the route fetch. Lives at document scope so it
// covers both the static section buttons and the dynamically-minted
// per-item Edit/Delete buttons inside ``loadCtxDetail``'s innerHTML.
document.addEventListener('click', (e) => {
  const target = e.target.closest('[data-write-blocked]');
  if (!target) return;
  e.preventDefault();
  e.stopPropagation();
  e.stopImmediatePropagation();
  const tier = target.dataset.writeBlocked;
  const key = tier === 'project_local'
    ? 'settings.ctx.write_blocked_project_local_tooltip'
    : 'settings.ctx.write_blocked_user_tooltip';
  showToast(t(key), 'info');
}, true);

// -- #1263 user-tier host-write confirm round-trip ----------------------------
//
// Since #1302 the skills/commands/agents write routes accept
// ``target_scope=user``: the first unconfirmed request writes nothing and
// answers HTTP 200 with ``{status: "needs_confirmation",
// confirm: "allow_host_writes", reason, host_targets}`` (ADR-0015 §4c
// rider). ``_ctxConfirmHostWrite`` owns the second leg: disclose the host
// paths in the shared confirm modal and, on approval, invoke ``resend()``
// — the SAME request with ``allow_host_writes=true`` (a body field on
// POST/PUT, a query parameter on DELETE). Resolves to the re-sent
// ``Response``, or ``null`` when the user declines (callers bail silently
// — declining a disclosure is a choice, not an error).

const _CTX_HOST_TARGET_PREVIEW_CAP = 8;

function _ctxIsHostWriteEnvelope(data) {
  return !!data && data.status === 'needs_confirmation'
    && data.confirm === 'allow_host_writes';
}

async function _ctxConfirmHostWrite(data, resend) {
  const targets = Array.isArray(data.host_targets) ? data.host_targets : [];
  // ``warningText`` renders pre-line (style.css) so one path per row; cap
  // the listing so a many-skills × 4-runtimes sync can't outgrow the modal.
  const shown = targets.slice(0, _CTX_HOST_TARGET_PREVIEW_CAP);
  let listing = shown.join('\n');
  if (targets.length > shown.length) {
    listing += '\n' + t('settings.ctx.host_write_more', {
      count: targets.length - shown.length,
    });
  }
  const ok = await showConfirm({
    title: t('settings.ctx.host_write_confirm_title'),
    message: t('settings.ctx.host_write_confirm_message', { count: targets.length }),
    warningText: listing,
    confirmText: t('settings.ctx.host_write_confirm_btn'),
    // Host writes are consequential but not destructive-by-default; red
    // stays reserved for deletes (the delete flow composes both dialogs).
    danger: false,
  });
  if (!ok) return null;
  return await resend();
}

// ``_ctxMaybeForceUnsafeImport`` owns the (optional) third import leg. When a
// user-tier import skipped one or more files because they tripped Gate A's
// secret-shape heuristic, the engine reports each as a skip with
// ``reason_code === 'privacy_blocked'`` — the force-able tier. (``project_shared``
// is a hard 422 the caller never reaches here, and ``project_local`` import is
// rejected outright, so a ``privacy_blocked`` skip is always a user-tier one.)
// We offer the same reviewed-bypass valve the CLI's ``--force-unsafe-import``
// and the upload/memory/chunk web write surfaces already expose.
//
// ``reimport(extra)`` is the caller's ``importOnce`` — it merges ``extra`` into
// the request body. Resolves to the re-imported payload, or ``null`` when
// nothing was privacy-blocked or the user declined (callers keep the original
// ``data``). Only the file name and hit count are surfaced — the matched bytes
// are never echoed (Gate A "never echo secrets" contract).
//
// Consent separation: approving the privacy override is NOT consent to write
// outside the project root. We retry with ``force_unsafe_import`` ALONE first —
// forcing flips the blocked files to would-import, so the server's user-tier
// host-write gate now has real ``~/.memtomem/`` destinations to disclose — then
// run the host-write confirm and resend with both flags. Bundling
// ``allow_host_writes`` into the first retry would skip the host disclosure
// whenever the original (non-forced) pass had nothing to disclose (the
// all-blocked case): the force files would land in the User store unannounced.
async function _ctxMaybeForceUnsafeImport(data, reimport) {
  const blocked = (data?.skipped || []).filter(
    s => s && s.reason_code === 'privacy_blocked',
  );
  if (!blocked.length) return null;
  const names = blocked.map(s => s.name).filter(Boolean);
  const ok = await showConfirm({
    title: t('settings.ctx.force_unsafe_title'),
    message: t('settings.ctx.force_unsafe_message', { count: blocked.length }),
    warningText: names.join('\n') || '—',
    confirmText: t('settings.ctx.force_unsafe_btn'),
    // Overriding a secret-shape detection is the one import action that warrants
    // the red button — every other import leg passes ``danger: false``.
    danger: true,
  });
  if (!ok) return null;
  const fail = async (r) => {
    const err = r ? await r.json().catch(() => ({})) : {};
    showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
    return null;
  };
  let r = await reimport({ force_unsafe_import: true });
  if (!r || !r.ok) return fail(r);
  let result = await r.json();
  if (_ctxIsHostWriteEnvelope(result)) {
    // Now that forcing surfaced the destinations, disclose the host paths and
    // resend with both flags only on approval (declining is a choice, not an
    // error — ``_ctxConfirmHostWrite`` returns null).
    r = await _ctxConfirmHostWrite(
      result, () => reimport({ force_unsafe_import: true, allow_host_writes: true }),
    );
    if (!r) return null;
    if (!r.ok) return fail(r);
    result = await r.json();
  }
  return result;
}

// Import error toast text. The import route's only 422 is the ``project_shared``
// Gate A privacy hard-block (#1378), whose server ``detail`` is a fixed,
// deliberately path-free ENGLISH string (issue-pinned, locale-unaware). In the
// Korean UI that English body read alongside the appended Korean hint (#1398
// item 1), so for the 422 — which is ALWAYS this one block — we surface the
// fully-localized user-tier hint ALONE (it already states the block AND the
// remedy) rather than prefixing the English detail. Every other status falls
// back to the shared error-detail renderer unchanged.
function _ctxImportErrToast(status, detail) {
  if (status === 422) return t('settings.ctx.privacy_blocked_shared_hint');
  return _ctxErrDetail(detail, t('toast.request_failed'));
}

// Sync error toast text. Unlike import — whose ONLY 422 is the privacy block,
// so it keys on ``status`` (#1398 item 1) — the per-type sync route has OTHER
// 422 causes (parse_error, strict_drop), so the privacy block can't be inferred
// from the status alone. It's disambiguated by the top-level ``reason_code`` the
// server now hoists alongside the path-free string ``detail`` (#1409). On
// ``privacy_blocked`` we surface the fully-localized sync hint (the English
// server detail is locale-unaware AND issue-pinned path-free, so it's shown to
// nobody); every other error falls back to the shared detail renderer unchanged.
// ``err`` is the parsed response body (``{detail, reason_code?}``).
function _ctxSyncErrToast(err) {
  if (err && err.reason_code === 'privacy_blocked') {
    return t('settings.ctx.privacy_blocked_shared_sync_hint');
  }
  return _ctxErrDetail(err && err.detail, t('toast.request_failed'));
}

// ``_ctxMaybeForceUnsafeSync`` is the fan-out (sync) mirror of
// ``_ctxMaybeForceUnsafeImport``: when a user-tier sync skipped one or more
// canonical files because they tripped Gate A's secret-shape heuristic, the
// engine reports each as a skip with ``reason_code === 'privacy_blocked'``.
// (``project_shared`` is a hard 422 the caller renders as an error before this
// point, and ``project_local`` sync is rejected outright, so a
// ``privacy_blocked`` *skip* is always user-tier.) We offer the same reviewed
// bypass valve the import side and the CLI's ``--force-unsafe`` expose. The
// sync skip tuple serializes the artifact name under the ``runtime`` key, so
// names read from ``s.runtime`` (``s.name`` tolerated for shape drift). Only
// the name + the fact of a hit are surfaced — matched bytes are never echoed
// (Gate A "never echo secrets" contract).
//
// Consent: unlike import, a forced sync needs NO second host-write
// disclosure. ``_user_sync_host_targets`` is name-based (every canonical
// name → its fan-out destinations, an upper bound independent of the privacy
// outcome), so the pre-force host-write confirm in ``_ctxRunSync`` already
// disclosed and got approval for the exact paths a forced write lands on —
// even in the all-blocked case. The only NEW consent is the red secret
// override below, after which we resend with BOTH flags. (Import differs:
// forcing CHANGES which files import, so it must disclose AFTER forcing —
// see ``_ctxMaybeForceUnsafeImport``.)
//
// ``resync(extra)`` is the caller's ``syncOnce`` — merges ``extra`` into the
// POST body. Resolves to the re-synced payload, or ``null`` when nothing was
// privacy-blocked or the user declined (callers keep the original ``data``).
async function _ctxMaybeForceUnsafeSync(data, resync) {
  const blocked = (data?.skipped || []).filter(
    s => s && s.reason_code === 'privacy_blocked',
  );
  if (!blocked.length) return null;
  const names = blocked.map(s => s.runtime || s.name).filter(Boolean);
  // The sync skip tuple serializes one entry PER RUNTIME for the same artifact,
  // so ``blocked.length`` is the runtime fan-out count, not the file count. The
  // count and the warning list must agree on UNIQUE files — a security-sensitive
  // "override secret detection" dialog overstating the affected-file count by
  // the fan-out factor (e.g. "4 files" for 1 file × 4 runtimes) is misleading
  // (#1397). Derive both from one de-duped list.
  const uniqueNames = [...new Set(names)];
  const ok = await showConfirm({
    title: t('settings.ctx.force_unsafe_sync_title'),
    message: t('settings.ctx.force_unsafe_sync_message', { count: uniqueNames.length }),
    warningText: uniqueNames.join('\n') || '—',
    confirmText: t('settings.ctx.force_unsafe_sync_btn'),
    // Overriding a secret-shape detection is the one sync action that warrants
    // the red button.
    danger: true,
  });
  if (!ok) return null;
  // Host writes were already disclosed + approved (see consent note above), so
  // the forced re-sync carries allow_host_writes — the gate passes without a
  // second envelope.
  const r = await resync({ force_unsafe_sync: true, allow_host_writes: true });
  if (!r || !r.ok) {
    const err = r ? await r.json().catch(() => ({})) : {};
    showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
    return null;
  }
  return await r.json();
}

// Shared single-item import flow used by both the runtime-only "Import" button
// (imports at the current tier) and the "Import to user library" button (the
// cross-tier project→user route). ``importOnce(extra)`` is the route-specific
// fetch. Handles the #1263 user-tier host-write disclosure envelope and then
// offers the #1379 reviewed Gate A force valve. Resolves to the final payload,
// or ``null`` when the user bailed at a gate or an error was already toasted
// (the caller owns the success/skip toast + list refresh).
async function _ctxRunRuntimeImportFlow(importOnce) {
  let r = await importOnce({});
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    showToast(_ctxImportErrToast(r.status, err.detail), 'error');
    return null;
  }
  let data = await r.json();
  if (_ctxIsHostWriteEnvelope(data)) {
    r = await _ctxConfirmHostWrite(data, () => importOnce({ allow_host_writes: true }));
    if (!r) return null;
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(_ctxImportErrToast(r.status, err.detail), 'error');
      return null;
    }
    data = await r.json();
  }
  const forced = await _ctxMaybeForceUnsafeImport(data, importOnce);
  if (forced) data = forced;
  return data;
}

// -- Deep-link carrier (ADR-0009 §3) ----------------------------------------
//
// Dashboard issue cards push ``?section=<type>&filter=<status>&artifact=<name>``
// onto the URL when the user clicks them, then call ``switchSettingsSection``
// to navigate to the leaf. ``loadCtxList`` reads the carrier on mount and
// applies the filter to the cwd-scope items, hiding non-matching cards
// (filter mode) or rendering only the named artifact (artifact mode), then
// scrolls to and pulses the first match.
//
// Why query string over app-state object or hash anchor: bookmarkable + back-
// button-friendly + shareable across users; no coupling between markup IDs
// and URL fragments. Decided in ADR-0009 §3.
//
// Filter values mirror the dashboard's ``count`` field names exactly
// (``out_of_sync`` / ``missing_target`` / ``missing_canonical`` /
// ``parse_error``). ``local_draft`` and ``error`` are tile-level rollups
// without a per-artifact analogue and are not exposed as filter values —
// the URL parser silently treats unknown filter values as no-filter.
const _CTX_DEEP_LINK_FILTERS = new Set([
  'out_of_sync',
  'missing_target',
  'missing_canonical',
  'parse_error',
  'invalid_name',
]);

// ``card.dataset.statuses`` is a space-separated list of these tokens; the
// per-runtime wire status (``"out of sync"``) maps to the filter token
// (``"out_of_sync"``) by replacing spaces with underscores. Centralized
// because both the renderer (writes the dataset) and the filter applier
// (reads it) need the same mapping; drift would silently break filtering.
function _ctxStatusBucket(runtimeStatus) {
  if (!runtimeStatus) return '';
  return String(runtimeStatus).replace(/ /g, '_');
}

// Walk a section ID (``ctx-skills``) back to the artifact type
// (``skills``). Used by the deep-link reader on mount to decide whether
// the URL's ``section`` matches the type currently being rendered.
function _ctxSectionToType(section) {
  if (!section || !section.startsWith('ctx-')) return '';
  return section.slice(4);
}

function _ctxParseDeepLink() {
  // ``URLSearchParams`` rather than a hand-rolled split so multi-encoded
  // artifact names ("foo bar.md") round-trip safely. Returns null when no
  // deep-link is present so callers can early-exit without a truthiness
  // dance over each individual field.
  let params;
  try {
    params = new URLSearchParams(window.location.search);
  } catch {
    return null;
  }
  const section = params.get('section') || '';
  const filter = params.get('filter') || '';
  const artifact = params.get('artifact') || '';
  const runtime = params.get('runtime') || '';
  if (!section && !filter && !artifact && !runtime) return null;
  return {
    section,
    filter: _CTX_DEEP_LINK_FILTERS.has(filter) ? filter : '',
    artifact,
    runtime,
  };
}

function _ctxBuildDeepLinkUrl({ section, filter, artifact, runtime }) {
  // Build the URL by mutating the *current* URL's search params rather
  // than constructing a fresh string — preserves any unrelated query
  // params the SPA might be using (or future feature might add) and
  // keeps the path/hash intact.
  const url = new URL(window.location.href);
  url.searchParams.delete('section');
  url.searchParams.delete('filter');
  url.searchParams.delete('artifact');
  url.searchParams.delete('runtime');
  if (section) url.searchParams.set('section', section);
  if (filter) url.searchParams.set('filter', filter);
  if (artifact) url.searchParams.set('artifact', artifact);
  if (runtime) url.searchParams.set('runtime', runtime);
  return url.pathname + (url.search || '') + (url.hash || '');
}

function _ctxSetDeepLink(state) {
  // ``replaceState`` rather than ``pushState`` so back-button navigates
  // out of the SPA (or to wherever the user came from) instead of
  // walking through a stack of intra-dashboard tile clicks. The URL is
  // a carrier for the leaf's filter state, not a navigation event.
  try {
    const next = _ctxBuildDeepLinkUrl(state);
    window.history.replaceState(window.history.state, '', next);
  } catch {
    /* opaque URL / sandboxed iframe; the in-DOM filter still applies */
  }
}

function _ctxClearDeepLink() {
  _ctxSetDeepLink({ section: '', filter: '', artifact: '', runtime: '' });
}

// Map the dashboard tile's dominant issue (the same ladder the badge
// text uses) to a filter token. ``null`` for clean / empty / error tiles
// — the click navigates to the section but does not filter the leaf.
function _ctxTileDominantFilter(d) {
  if (!d || d.error) return null;
  if ((d.parse_error || 0) > 0) return 'parse_error';
  if ((d.invalid_name || 0) > 0) return 'invalid_name';
  if ((d.missing_target || 0) > 0) return 'missing_target';
  if ((d.missing_canonical || 0) > 0) return 'missing_canonical';
  if ((d.out_of_sync || 0) > 0) return 'out_of_sync';
  return null;
}

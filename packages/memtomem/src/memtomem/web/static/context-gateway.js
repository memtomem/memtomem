/**
 * Context Gateway — Skills / Commands / Agents CRUD, diff, sync, import.
 *
 * Depends on globals from app.js: qs, show, hide, escapeHtml, t, showConfirm,
 * showToast, panelLoading, btnLoading, emptyState, diffLines, renderDiff,
 * switchSettingsSection.  Loaded AFTER app.js in index.html.
 */

// -- Status helpers -----------------------------------------------------------

const _ctxStatusCls = {
  'in sync':           'ctx-runtime-badge--sync',
  'out of sync':       'ctx-runtime-badge--warn',
  'missing target':    'ctx-runtime-badge--missing',
  // Runtime-only items (canonical absent) are a normal pre-import state, not
  // an error — the same red treatment as `parse error` over-signaled it.
  'missing canonical': 'ctx-runtime-badge--pending',
  'parse error':       'ctx-runtime-badge--error',
};
const _ctxStatusLabel = {
  'in sync':           'settings.ctx.status_in_sync',
  'out of sync':       'settings.ctx.status_out_of_sync',
  'missing target':    'settings.ctx.status_missing_target',
  'missing canonical': 'settings.ctx.status_missing_canonical',
  'parse error':       'settings.ctx.status_parse_error',
};

// Settings overview badge i18n map. Keys are the wire status values that
// ``context_overview`` (web/routes/context_gateway.py) emits for the
// ``settings`` slot — derived from ``diff_settings`` and collapsed to
// ``in_sync`` / ``out_of_sync`` / ``error``. Unknown statuses fall through
// to the legacy ``replace('_', ' ')`` path so a future status string still
// renders something readable instead of an empty badge.
const _SETTINGS_STATUS_I18N = {
  in_sync:      'settings.hooks.badge_in_sync',
  out_of_sync:  'settings.hooks.badge_out_of_sync',
  error:        'settings.hooks.badge_error',
};

// Localized status text for a wire status value. Falls back to the raw
// status string when no i18n key is mapped — keeps unknown/future statuses
// visible instead of silently rendering an empty label.
function _ctxStatusText(status) {
  return t(_ctxStatusLabel[status] || '', status);
}

function _ctxBadge(status) {
  const cls = _ctxStatusCls[status] || 'ctx-runtime-badge--missing';
  return `<span class="ctx-runtime-badge ${cls}">${escapeHtml(_ctxStatusText(status))}</span>`;
}

function renderRuntimeBadges(runtimes) {
  if (!runtimes || !runtimes.length) return '';
  return '<div class="ctx-runtime-badges">' +
    runtimes.map(r => {
      const short = r.runtime.replace(/_skills|_commands|_agents/g, '');
      return `<span class="ctx-runtime-badge ${_ctxStatusCls[r.status] || ''}" title="${escapeHtml(r.runtime)}">${escapeHtml(short)}: ${escapeHtml(_ctxStatusText(r.status))}</span>`;
    }).join('') + '</div>';
}

function renderDroppedChips(fields) {
  if (!fields || !fields.length) return '';
  return fields.map(f => `<span class="ctx-dropped-chip">${escapeHtml(t('settings.ctx.dropped_fields'))}: ${escapeHtml(f)}</span>`).join('');
}

function renderImportResult(data) {
  let html = `<div class="ctx-import-result">`;
  html += `<div class="ctx-import-priority">${t('settings.ctx.import_priority')}</div>`;
  if (data.imported && data.imported.length) {
    html += `<h4>${t('settings.ctx.import_success')}</h4>`;
    for (const item of data.imported) {
      html += `<div class="ctx-import-item"><span class="badge badge-success">${escapeHtml(item.name)}</span></div>`;
    }
  }
  if (data.skipped && data.skipped.length) {
    html += `<h4 style="margin-top:8px">${escapeHtml(t('settings.ctx.import_skipped'))}</h4>`;
    for (const item of data.skipped) {
      html += `<div class="ctx-import-item">${escapeHtml(item.name)} <span class="badge badge-warning">${escapeHtml(item.reason)}</span></div>`;
    }
  }
  if (!data.imported?.length && !data.skipped?.length) {
    html += `<div class="text-muted">${t('settings.ctx.no_artifacts_hint')}</div>`;
  }
  html += '</div>';
  return html;
}

async function _ctxErrorMessageFromResponse(resp, fallback) {
  const contentType = resp.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    const err = await resp.json().catch(() => ({}));
    const detail = err.detail;
    // Defensive branch for structured payloads shaped like `{detail: "..."}`
    // (ProjectTierBlocked / redaction) — a different shape than #1210's 409, so
    // handle it before delegating to the shared extractor.
    if (detail && typeof detail === 'object' && typeof detail.detail === 'string' && detail.detail) {
      return detail.detail;
    }
    // String detail, or #1210's ``{reason_code, message}`` write-guard 409, both
    // via the shared extractor — so the Sync All fan-out surfaces the same
    // localized paused / not-enrolled reason the per-row / per-section Sync
    // buttons do, instead of a generic English fallback.
    const extracted = _ctxErrDetail(detail, null);
    if (extracted) return extracted;
  } else {
    const text = await resp.text().catch(() => '');
    if (text.trim()) return text;
  }
  return fallback;
}

// -- Overview -----------------------------------------------------------------

// Sequence guard for in-flight fetch races. ``loadCtxOverview`` is called
// from the cold mount, the Refresh button, and the end of Sync All; rapid
// triggers can leave a stale response landing *after* a newer one and
// clobber the fresher render. Toggling language does NOT re-fetch — see
// ``_ctxOverviewCache`` and the ``langchange`` listener below.
let _ctxOverviewSeq = 0;

// Cache the last successful ``/api/context/overview`` payload so a
// ``langchange`` toggle can re-render the inline ``t()`` card text from
// the existing data — no fetch, no ``panelLoading`` spinner flash. The
// dashboard data itself is locale-independent (counts, statuses); only
// the labels and badge copy translate, so a cached re-render is
// equivalent to a re-fetch + re-render for translation-only events and
// drops the round-trip the langchange listener used to issue (#824
// review P2 / #825). Cleared on fetch errors so the next call falls
// back to a fresh fetch path.
let _ctxOverviewCache = null;
let _ctxTargetScope = 'project_shared';
const _CTX_ACTIVE_SCOPE_KEY = 'memtomem_ctx_active_scope_id';
let _ctxActiveScopeId = '';
let _ctxProjectsCache = [];

// De-dup memo for the `/api/context/projects` failure toast (#1101).
// ``_ctxFetchProjects`` runs from three independent panel-load paths
// (overview, settings projects, hooks sync), so a single persistent outage
// would otherwise stack one near-identical toast per path as the user
// navigates. We remember the last fired ``kind:status:detail`` key and skip
// re-toasting an identical failure; a successful (or silent-404) fetch clears
// the memo so a *later*, distinct outage notifies again instead of being
// swallowed by a stale key.
let _ctxProjectsFetchWarnKey = null;

try {
  _ctxActiveScopeId = localStorage.getItem(_CTX_ACTIVE_SCOPE_KEY) || '';
} catch {
  _ctxActiveScopeId = '';
}

function _ctxTargetScopeParam(targetScope = _ctxTargetScope) {
  // ``targetScope`` defaults to the live global so existing single-shot
  // callers are unchanged. A multi-phase flow (Sync All) snapshots the tier
  // once and passes it explicitly so a mid-run tier flip can't make later
  // phases land in a different tier (ADR-0021 §C / Major-1) — see the Sync
  // All handler's ``syncAllTier`` snapshot.
  if (targetScope === 'project_shared') return '';
  return `target_scope=${encodeURIComponent(targetScope)}`;
}

// The scope a request *effectively* targets, as a bare id ('' === Server-CWD).
// An active id that isn't an available, non-Server-CWD scope in the cache —
// Server-CWD itself, a now-``missing`` scope, or a selection preserved across
// a transient projects-fetch outage where only the synthetic Server-CWD scope
// is cached (#1102) — collapses to Server-CWD. This is the single source of
// truth for "what scope are we really on"; ``_ctxScopeParam`` (request URL) and
// ``_ctxStashKey`` (conflict-draft key) both route through it so they can never
// disagree — otherwise a draft saved while the request silently fell back to
// Server-CWD would be keyed under the preserved project id and leak across
// scopes after recovery.
function _ctxEffectiveScopeId(scopeId = _ctxActiveScopeId) {
  if (!scopeId) return '';
  const activeScope = (_ctxProjectsCache || []).find(scope =>
    scope && scope.scope_id === scopeId && !scope.missing);
  if (!activeScope || _ctxScopeIsServerCwd(activeScope)) return '';
  return scopeId;
}

function _ctxScopeParam(scopeId = _ctxActiveScopeId) {
  // Server CWD is the route default. Leaving scope_id off preserves the
  // legacy single-project URL shape while still sending ids for added projects.
  const eff = _ctxEffectiveScopeId(scopeId);
  return eff ? `scope_id=${encodeURIComponent(eff)}` : '';
}

function _ctxWithTargetScope(url, opts = {}) {
  const params = [];
  // ``opts.targetScope`` pins the tier (defaults to the live global); pass it
  // alongside ``opts.scopeId`` to freeze both dimensions of a multi-phase run.
  const targetParam = _ctxTargetScopeParam(opts.targetScope);
  let scopeParam;
  if (opts.includeScope === false) {
    scopeParam = '';
  } else if (opts.scopeResolved) {
    // ``opts.scopeId`` is an ALREADY-effective id snapshotted once for a
    // multi-phase run. Emit it verbatim, bypassing ``_ctxScopeParam`` →
    // ``_ctxEffectiveScopeId``'s live ``_ctxProjectsCache`` re-resolution — a
    // mid-run cache refresh marking the pinned project missing must not
    // collapse later phases to Server-CWD (ADR-0016 §5 / ADR-0021 §C: pin
    // BOTH scope and tier, not just tier).
    scopeParam = opts.scopeId ? `scope_id=${encodeURIComponent(opts.scopeId)}` : '';
  } else {
    scopeParam = _ctxScopeParam(opts.scopeId);
  }
  if (targetParam) params.push(targetParam);
  if (scopeParam) params.push(scopeParam);
  if (!params.length) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${params.join('&')}`;
}

function _ctxScopeIsActive(scope) {
  return !!scope && !scope.missing && scope.scope_id === _ctxActiveScopeId;
}

function _ctxScopeDisplayLabel(scope) {
  if (!scope) return '';
  if (_ctxScopeIsServerCwd(scope)) return t('settings.ctx.server_cwd');
  return scope.label || _ctxBasename(scope.root) || scope.scope_id;
}

function _ctxNormalizeActiveScope(scopes) {
  const list = Array.isArray(scopes) ? scopes : [];
  const availableScopes = list.filter(scope => !scope.missing);
  const previousActiveScopeId = _ctxActiveScopeId;
  if (!list.length) {
    _ctxActiveScopeId = '';
    try { localStorage.setItem(_CTX_ACTIVE_SCOPE_KEY, _ctxActiveScopeId); } catch {}
    return null;
  }
  let active = _ctxActiveScopeId
    ? availableScopes.find(scope => scope.scope_id === _ctxActiveScopeId)
    : null;
  if (!active) {
    active = availableScopes.find(_ctxScopeIsServerCwd) || availableScopes[0] || null;
  }
  _ctxActiveScopeId = active ? (active.scope_id || '') : '';
  if (previousActiveScopeId !== _ctxActiveScopeId) {
    _ctxBumpActiveScopeDetailSeq();
  }
  try { localStorage.setItem(_CTX_ACTIVE_SCOPE_KEY, _ctxActiveScopeId); } catch {}
  return active;
}

// Fetch ``/api/context/projects`` and classify the outcome, WITHOUT mutating
// any module global. Returns ``{ data, warn }``:
//   - ``data``  always a ``{scopes: [...]}`` object — the real scopes on
//               success, or the synthetic Server-CWD fallback on any failure.
//   - ``warn``  null on success / silent-404, else ``{kind, status?, detail}``
//               describing a "loud" (toastable) failure shape.
//
// Pure by contract (#1194): a superseded, still-in-flight fetch that resolves
// AFTER a newer one must not be able to clobber the shared ``_ctxProjectsCache``,
// the normalized+persisted ``_ctxActiveScopeId``, or the failure-toast memo.
// So this helper only READS — callers commit the result via
// ``_ctxCommitProjects`` ONLY after re-checking their own sequence/scope guard.
// ``opts.targetScope`` pins the tier when a caller must fetch projects and a
// sibling resource (e.g. overview) under one tier, so a mid-flight tier flip
// can't split the two requests across tiers (ADR-0021 §C).
async function _ctxFetchProjectsData(opts = {}) {
  let data;
  // Four failure shapes need to stay distinguishable per #1080:
  //   - 404 / network throw   → older deployment or absent endpoint; the
  //     legacy single-server-CWD fallback is the documented contract here,
  //     so stay silent to avoid noise on intentional-omit deployments.
  //   - 5xx (and non-404 4xx)  → endpoint exists but is failing; surface a
  //     non-blocking toast so a broken store doesn't masquerade as "no
  //     registered projects".
  //   - 200 with malformed JSON → endpoint reachable, response unreadable;
  //     same "endpoint exists but failing" class, surface a toast.
  //   - 200 with unexpected shape → parses cleanly but isn't {scopes: Array};
  //     same "endpoint exists but failing" class, surface a toast (#1100).
  let warn = null;
  try {
    // ``include`` tokens are opt-in server-side (ADR-0021 PR2). ``counts`` is
    // always requested — every caller renders the scope picker's per-scope
    // count badges. ``runtime_coverage`` is requested ONLY when the caller asks
    // (``opts.includeCoverage``): it costs a ``probe_all_runtimes`` pass (per-
    // client config reads) per scope and is consumed solely by the overview's
    // Project Scope Matrix, so cheap callers (the per-type list tabs, the
    // portal, hooks-sync) must NOT pay it on every reload. ``_ctxWithTargetScope``
    // appends ``&target_scope=`` after the existing ``?``.
    const include = opts.includeCoverage ? 'counts,runtime_coverage' : 'counts';
    const res = await fetch(_ctxWithTargetScope(`/api/context/projects?include=${include}`, { includeScope: false, targetScope: opts.targetScope }));
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
      if (res.status !== 404) warn = { kind: 'http', status: res.status, detail };
      throw new Error(detail);
    }
    try {
      data = await res.json();
    } catch (parseErr) {
      warn = { kind: 'parse', detail: String((parseErr && parseErr.message) || parseErr) };
      throw parseErr;
    }
    // Validate the shape *outside* the parse try/catch so it isn't
    // misclassified as a parse failure. A 200 that parses but isn't
    // {scopes: Array} — null, {}, {error: …}, a string, an array — would
    // otherwise fall through to ``data.scopes || []`` below: literal ``null``
    // TypeErrors (caller shows a generic "Failed to load overview", toast
    // never reached) and ``{}`` silently empties the cache, reproducing the
    // #1080 "unreadable store masquerading as no-projects" symptom. Route it
    // through the same loud-fallback path as the other failing-endpoint shapes.
    if (!data || !Array.isArray(data.scopes)) {
      warn = { kind: 'shape', detail: `unexpected response shape: ${typeof data}` };
      throw new Error(warn.detail);
    }
  } catch (_err) {
    // Browser tests and older deployments may not provide the multi-project
    // discovery endpoint. Preserve the legacy single-project behavior by
    // falling back to an implicit server-CWD scope; downstream requests omit
    // scope_id for server-CWD because it is the route default.
    data = {
      scopes: [{
        scope_id: '',
        label: t('settings.ctx.server_cwd'),
        root: '',
        tier: 'project',
        sources: ['server-cwd'],
        missing: false,
        // Match the API scope shape: ``stale`` (ADR-0021 PR2) and the full
        // four-key counts dict. The synthetic fallback is a safe default
        // (never stale → no spurious "Initialize" prompt) so a fetch outage
        // can't desync the rendered shape from a real response.
        stale: false,
        experimental: false,
        counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
      }],
    };
  }
  return { data, warn };
}

// Commit a ``_ctxFetchProjectsData`` result into the shared module state. Split
// from the fetch (#1194) so each caller commits ONLY after its own
// sequence/scope guard passes — a late, superseded fetch must not overwrite a
// newer one's cache / active scope / toast memo. Carries the #1102
// normalize-only-when-authoritative gate and the #1101 failure-toast de-dup.
// Returns ``data`` for callers that render from it.
function _ctxCommitProjects({ data, warn }) {
  _ctxProjectsCache = data.scopes || [];
  // Normalize only when the outcome is *authoritative* — i.e. exactly when we
  // did NOT raise a "loud" failure toast (``warn``). Three cases:
  //   - success (real scopes)          → normalize against the real list.
  //   - 404 / network throw (silent)   → endpoint absent / older deploy; the
  //     project list genuinely isn't available, so clear a now-stale active id
  //     to Server-CWD (preserves the pre-#1099 behavior — other consumers like
  //     ``_ctxRestoreDraft`` key off ``_ctxActiveScopeId`` and would otherwise
  //     leak a dangling ``proj-*`` selection).
  //   - 5xx / non-404 4xx / parse error → "endpoint exists but failing"; the
  //     synthetic one-element list is NOT authoritative about whether the
  //     user's project still exists, so skip normalization. Otherwise a
  //     transient failure would rewrite the active id to '' and persist that
  //     demotion to localStorage — silently dropping a still-valid selection
  //     the toast itself implied was temporary, that the next successful fetch
  //     would restore (#1102). ``_ctxScopeParam`` already omits ``scope_id``
  //     when the active id is absent from the cache, so requests during the
  //     degraded window safely fall back to the Server-CWD default.
  if (!warn) _ctxNormalizeActiveScope(_ctxProjectsCache);
  if (warn) {
    // De-dup so a persistent outage doesn't stack one toast per panel-load
    // path (#1101). Key on the failure shape, not just the message, so a
    // status change still surfaces.
    const warnKey = `${warn.kind}:${warn.status || ''}:${warn.detail}`;
    if (warnKey !== _ctxProjectsFetchWarnKey && typeof showToast === 'function') {
      showToast(t('settings.ctx.projects_fetch_failed', { error: warn.detail }), 'error');
    }
    _ctxProjectsFetchWarnKey = warnKey;
  } else {
    // Clean fetch (real scopes) or a silent 404 fallback — reset the memo so a
    // future, distinct failure is not suppressed by a stale key.
    _ctxProjectsFetchWarnKey = null;
  }
  return data;
}

// Legacy all-in-one: fetch THEN immediately commit. Preserved for direct
// callers and the #1080/#1101/#1102 tests that depend on the combined contract.
// DO NOT call from a concurrency-sensitive UI loader: it commits BEFORE any
// caller sequence guard, which re-introduces the #1194 stale-fetch race.
// Guarded loaders use ``_ctxFetchProjectsData`` + a post-guard
// ``_ctxCommitProjects`` instead.
async function _ctxFetchProjects() {
  const result = await _ctxFetchProjectsData();
  _ctxCommitProjects(result);
  return result.data;
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
    return `<option value="${escapeHtml(scope.scope_id)}"${selected}>${escapeHtml(label + suffix)}</option>`;
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
    }
  }
}

function _ctxTierControls(type) {
  return `<div class="ctx-tier-filter" data-type="${escapeHtml(type)}" role="group" aria-label="${escapeHtml(t('settings.ctx.tier_filter'))}">
    <button type="button" data-scope="user" class="${_ctxTargetScope === 'user' ? 'active' : ''}">${escapeHtml(t('settings.ctx.tier_option_user'))}</button>
    <button type="button" data-scope="project_shared" class="${_ctxTargetScope === 'project_shared' ? 'active' : ''}">${escapeHtml(t('settings.ctx.tier_option_project_shared'))}</button>
    <button type="button" data-scope="project_local" class="${_ctxTargetScope === 'project_local' ? 'active' : ''}">${escapeHtml(t('settings.ctx.tier_option_project_local'))}</button>
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

// -- Tier-aware write-block gate (issue #943) ---------------------------------
//
// ADR-0011 / #940 wired ``target_scope`` through every artifact route, with
// ``_reject_non_shared_write`` 400-rejecting create/update/delete/sync/import
// on user and project_local tiers. Without a matching UI affordance, users
// who switch the tier filter to ``user`` or ``project_local`` see the same
// write buttons as on ``project_shared`` and only learn the operation is
// blocked when the route surfaces a generic toast. #943 closes that UX
// gap by tagging every web-write affordance with
// ``data-write-blocked="<tier>"`` so:
//
//   (1) CSS dims the button (``[data-write-blocked]`` selector in style.css),
//   (2) ``aria-disabled="true"`` announces the state to screen readers,
//   (3) the native ``title`` carries the tier-aware explanation, and
//   (4) a document-level capture-phase click handler intercepts the click
//       and fires a toast — the per-button handler never sees the event,
//       so no POST is ever issued.
//
// Per-section buttons (.ctx-create-btn / .ctx-import-btn / .ctx-sync-btn)
// live in the static HTML so the refresh applies on every render that
// touches the tier filter; per-item buttons (.ctx-detail-edit-btn /
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
  + '.ctx-matrix-sync-btn, .ctx-matrix-add-project-btn, .ctx-matrix-remove-btn, '
  // The single-item runtime-only import route (#940 import_<type>)
  // also flows through ``_reject_non_shared_write``, so the per-detail
  // "Import this <type>" button minted by ``_ctxLoadRuntimeOnlyDetail``
  // belongs in the same write-blocked sweep.
  + '.ctx-runtime-only-import'
);

function _ctxRefreshWriteBlockedState() {
  const blocked = _ctxTargetScope !== 'project_shared';
  const tooltipKey = _ctxTargetScope === 'project_local'
    ? 'settings.ctx.write_blocked_project_local_tooltip'
    : 'settings.ctx.write_blocked_user_tooltip';
  document.querySelectorAll(_CTX_WRITE_BUTTON_SELECTOR).forEach(btn => {
    if (blocked) {
      btn.dataset.writeBlocked = _ctxTargetScope;
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

  // Sync All: user-tier writes hit the server's 400 reject path; gate
  // them here so the dashboard surfaces the decision pre-click instead
  // of relying on the post-click toast. project_local already carries
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
  if ((d.missing_target || 0) > 0) return 'missing_target';
  if ((d.missing_canonical || 0) > 0) return 'missing_canonical';
  if ((d.out_of_sync || 0) > 0) return 'out_of_sync';
  return null;
}

function _renderCtxOverview(data) {
  const el = qs('ctx-overview-content');
  if (!el) return;

  const types = [
    { key: 'skills',   label: t('settings.ctx.skills_title'),   section: 'ctx-skills' },
    { key: 'commands', label: t('settings.ctx.commands_title'), section: 'ctx-commands' },
    { key: 'agents',   label: t('settings.ctx.agents_title'),   section: 'ctx-agents' },
    { key: 'mcp_servers', label: t('settings.ctx.mcp_servers_title'), section: 'ctx-mcp-servers' },
    { key: 'settings', label: t('settings.hooks.title'),        section: 'hooks-sync' },
  ];

  // Issues #830/#831: surface project root and detected runtimes so a "0 skills"
  // tile isn't ambiguous between "empty project" and "wrong root". Defensive
  // readers — older _ctxOverviewCache payloads (pre-add) replay through this
  // path on langchange and would otherwise blow up.
  const runtimes = Array.isArray(data.detected_runtimes) ? data.detected_runtimes : [];
  const projectRoot = typeof data.project_root === 'string' ? data.project_root : '';
  // #952: ``Project: <root>`` is misleading on ``user`` tier — user-scope
  // canonicals live under ``~/.memtomem/`` (host-global), not the cwd.
  // Branch the header label + path on ``target_scope`` so the heading
  // matches the tier the counts are computed against. Defensive default
  // ``project_shared`` matches the route's query-param default.
  const targetScope = typeof data.target_scope === 'string' ? data.target_scope : 'project_shared';
  const isUserTier = targetScope === 'user';
  const rootLabel = isUserTier
    ? t('settings.ctx.user_canonical_label')
    : t('settings.ctx.project_root_label');
  const rootPath = isUserTier
    ? t('settings.ctx.user_canonical_path')
    : projectRoot;
  const undetectedTitle = escapeHtml(t('settings.ctx.runtime_undetected_tooltip'));
  const chips = runtimes.map(rt => {
    const available = !!rt.available;
    const cls = available ? 'badge badge-success' : 'badge badge-gray';
    const title = available ? '' : ` title="${undetectedTitle}"`;
    const name = escapeHtml(rt.name || '');
    return `<span class="${cls}"${title} data-runtime="${name}">${name}</span>`;
  }).join('');

  // Issue #832 / ADR-0009 §1.c: surface freshness as "Canonical updated: 5m
  // ago" sourced from canonical-source mtime. Issue #1076 follow-up: the
  // label was previously "Last sync", which overstated the data source —
  // editing a canonical artifact without fan-out also bumps this value, so
  // users diagnosing "did this actually reach Claude/Codex?" trusted it too
  // much. The label is now data-source-accurate and the explanation is
  // attached via ``title=`` on the label span (the row-level ``title=``
  // still carries the raw ISO so the diagnose case keeps the absolute
  // timestamp on hover). Suppress the line when the backend returns null
  // (fresh / empty project — no canonical files yet); rendering a "never"
  // sentinel or epoch-zero relative would be more confusing than silent
  // absence.
  const lastSyncedAt = typeof data.last_synced_at === 'string' && data.last_synced_at
    ? data.last_synced_at
    : '';
  let lastSyncHtml = '';
  if (lastSyncedAt) {
    const rel = escapeHtml(relativeTime(lastSyncedAt));
    const iso = escapeHtml(lastSyncedAt);
    const labelTip = escapeHtml(t('settings.ctx.last_synced_tooltip'));
    lastSyncHtml = `<div class="ctx-overview-last-sync" title="${iso}">
        <span class="ctx-overview-last-sync-label" title="${labelTip}">${escapeHtml(t('settings.ctx.last_synced_label'))}</span>
        <span class="ctx-overview-last-sync-value" data-iso="${iso}">${rel}</span>
      </div>`;
  }

  // Inline ``t()`` text rather than ``data-i18n`` attrs: the langchange
  // listener applies ``I18N.applyDOM`` first and then re-renders this
  // panel, so any ``data-i18n`` attr written *during* the re-render would
  // miss the translation pass and stay on its EN fallback. Tile labels in
  // this same render path use the same inline-``t()`` convention for the
  // same ordering reason.
  let html = `<div class="ctx-overview-header">
      <div class="ctx-overview-root" data-target-scope="${escapeHtml(targetScope)}">
        <span class="ctx-overview-root-label">${escapeHtml(rootLabel)}</span>
        <code class="ctx-overview-root-path">${escapeHtml(rootPath)}</code>
      </div>
      <div class="ctx-overview-runtimes">
        <span class="ctx-overview-runtimes-label">${escapeHtml(t('settings.ctx.runtimes_label'))}</span>
        ${chips}
      </div>
      ${lastSyncHtml}
    </div>`;
  html += _ctxProjectControls('overview');
  html += _ctxTierControls('overview');
  html += '<div class="ctx-overview-grid">';
  for (const typ of types) {
      const d = data[typ.key] || {};
      const total = d.total || 0;
      const inSync = d.in_sync || 0;
      // ``/api/context/overview`` aggregates ``(runtime, name, status)`` triples.
      // ``total`` is the count of distinct names; status counts are per
      // ``(runtime, name)`` pair, so when one artifact is tracked under
      // multiple runtimes the per-status counts can sum above ``total``.
      // Concrete: ``commands: {total: 3, in_sync: 3, missing_target: 3}``
      // means 3 commands all in sync for one runtime AND all missing on
      // another. ``inSync < total`` alone misses that case (#692). Treat
      // any non-``in_sync`` count as a real issue so multi-runtime
      // divergence doesn't hide behind a green ``3/3 synced`` badge.
      const missingTarget = d.missing_target || 0;
      const missingCanonical = d.missing_canonical || 0;
      const outOfSync = d.out_of_sync || 0;
      const parseError = d.parse_error || 0;
      const localDraft = d.local_draft || 0;
      const issueCount = missingTarget + missingCanonical + outOfSync + parseError;
      const hasIssue = d.error || issueCount > 0
        || d.status === 'out_of_sync' || d.status === 'error';
      // Empty state ≡ a tile with no actionable artifacts: settings carries
      // ``total = applicable generators`` (skipped runtimes excluded) so a
      // zero count there ≡ "no installed runtime has a canonical source",
      // legitimately empty. Pre-Q-PR3 the gate excluded settings because the
      // backend then only returned ``{status}``; with the count fields now
      // present settings participates in the same empty/issue/synced ladder.
      const isEmpty = total === 0 && !d.error && !hasIssue;
      // localDraft only flips to gray when nothing else is wrong — real
      // issues (parse_error / out_of_sync / missing_*) must still surface
      // as warning. project_local has no runtime fan-out today so issues
      // won't co-occur with drafts, but the gate stays robust if that changes.
      const badgeCls = d.error
        ? 'badge-danger'
        : (isEmpty || (localDraft > 0 && !hasIssue)
            ? 'badge-gray'
            : (hasIssue ? 'badge-warning' : 'badge-success'));

      // Pick the most actionable status to surface in the badge. Order
      // matters: ``error`` and the empty-tile case both pre-empt the count
      // ladder; then ``parse_error`` (hard failure — file is malformed),
      // unsynced-runtime states, unimported-canonical, and out-of-sync
      // content. Falling through to ``{inSync}/{total} synced`` keeps the
      // all-clear case unchanged.
      let badgeText;
      if (d.error) {
        // Own-namespace key — reaching across into ``settings.hooks.*``
        // would couple the dashboard to whatever the hooks panel
        // decides to label its errors as next.
        badgeText = t('settings.ctx.badge_error');
      } else if (isEmpty) {
        badgeText = t('settings.ctx.badge_empty');
      } else if (typ.key === 'settings') {
        // Settings badge stays status-driven even after Visual-1's count
        // alignment: "in sync" / "out of sync" reads more accurately than
        // ``${inSync}/${total} synced``, since the per-runtime semantics
        // (Claude/Codex each correctly merged) is qualitative, not a copy
        // count. The fallthrough uses ``/_/g`` (Visual-4) so a future
        // multi-underscore status like ``needs_user_confirm`` doesn't
        // render as ``needs user_confirm``.
        const key = _SETTINGS_STATUS_I18N[d.status];
        badgeText = key ? t(key) : (d.status || '').replace(/_/g, ' ');
      } else if (parseError > 0) {
        badgeText = `${parseError} ${t('settings.ctx.badge_parse_error')}`;
      } else if (missingTarget > 0) {
        badgeText = `${missingTarget} ${t('settings.ctx.badge_missing_target')}`;
      } else if (missingCanonical > 0) {
        badgeText = `${missingCanonical} ${t('settings.ctx.badge_missing_canonical')}`;
      } else if (outOfSync > 0) {
        badgeText = `${outOfSync} ${t('settings.ctx.badge_out_of_sync')}`;
      } else if (localDraft > 0) {
        badgeText = `${localDraft} ${t('settings.ctx.badge_local_draft')}`;
      } else {
        badgeText = `${inSync}/${total} ${t('settings.ctx.badge_synced')}`;
      }

      // ADR-0009 §2 sync-direction pointers — surface remediation intent
      // without expanding the dashboard's mutation surface. Order is fixed:
      // missing_target (push unambiguous) → out_of_sync (direction-neutral,
      // resolve on leaf) → missing_canonical (pull unambiguous, leaf-only
      // import). ``parse_error`` and ``d.error`` are intentionally NOT
      // surfaced as pointers — both are direction-neutral diagnostics
      // already conveyed by the badge; the leaf is the right place to
      // diagnose them. Settings tile cannot produce ``missing_canonical``
      // by design (ADR-0009 §2 last paragraph, ADR-0001 §5).
      const pointers = [];
      if (!d.error && parseError === 0) {
        if (missingTarget > 0) {
          pointers.push({
            action: 'sync-all',
            text: t('settings.ctx.pointer_missing_target', { count: missingTarget }),
          });
        }
        if (outOfSync > 0) {
          pointers.push({
            action: 'leaf',
            text: t('settings.ctx.pointer_out_of_sync', {
              count: outOfSync,
              leaf: typ.label,
            }),
          });
        }
        if (missingCanonical > 0 && typ.key !== 'settings') {
          pointers.push({
            action: 'leaf',
            text: t('settings.ctx.pointer_missing_canonical', {
              count: missingCanonical,
              leaf: typ.label,
            }),
          });
        }
      }
      let pointersHtml = '';
      if (pointers.length > 0) {
        pointersHtml = '<div class="ctx-overview-pointers">'
          + pointers.map(p =>
              `<button type="button" class="ctx-overview-pointer"`
              + ` data-action="${p.action}" data-section="${typ.section}">`
              + `${escapeHtml(p.text)}</button>`,
            ).join('')
          + '</div>';
      }

      // ``data-tile-key`` carries the overview-payload key (``skills`` /
      // ``commands`` / ``agents`` / ``settings``) so the click handler can
      // re-derive the dominant filter from the raw counts without re-
      // running the badge-text ladder. Settings is intentionally tagged
      // too — the tile routes to ``hooks-sync`` (not a context list), so
      // the filter is a no-op there, but keeping the attribute uniform
      // avoids a dataset-shape branch in the click loop.
      //
      // #1073 / PR #1088 review: the navigation control is a real
      // ``<button>`` *inside* the tile, NOT ``role=button`` on the outer
      // ``<div>``. Putting the role on the outer div nests the
      // ``.ctx-overview-pointer`` buttons inside a button-role element —
      // invalid ARIA (interactive content inside ``role=button`` is
      // forbidden) and inconsistently exposed by assistive tech. The
      // pointer buttons are siblings of the nav button so they keep
      // their own focus / activation. The accessible name composes the
      // dominant badge text with the kind label ("12 out of sync —
      // Skills") so screen-reader announce isn't just "Skills" with no
      // context.
      // ``data-section`` / ``data-tile-key`` stay on the outer tile div
      // because existing selectors (browser tests, deep-link applier,
      // CSS scoping) use them to identify the tile by kind — the click
      // handler reads them via ``navBtn.closest('.ctx-overview-stat')``.
      const tileAriaLabel = `${badgeText} — ${typ.label}`;
      html += `<div class="ctx-overview-stat" data-section="${typ.section}" data-tile-key="${typ.key}">
        <button type="button" class="ctx-overview-stat-nav" aria-label="${escapeHtml(tileAriaLabel)}">
          <div class="ctx-overview-count">${total}</div>
          <div class="ctx-overview-label">${escapeHtml(typ.label)}</div>
          <div class="ctx-overview-badge"><span class="badge ${badgeCls}">${escapeHtml(badgeText)}</span></div>
        </button>
        ${pointersHtml}
      </div>`;
    }
    html += '</div>';
    html += _renderProjectsMatrix();
    el.innerHTML = html;
    _ctxWireProjectControls();
    _ctxWireTierControls();

  // Gate the Sync All button: when every artifact type's items are
  // entirely runtime-only (no canonicals to fan out), Sync All resolves
  // to a series of `no_canonical_root` skips. Surface that pre-click
  // via a data attribute (CSS dims the button) plus a native ``title``
  // hover tooltip + ``aria-disabled`` so the user understands the
  // dimmed state without having to click first; the post-click toast
  // stays as a fallback for users who don't hover (mobile, keyboard).
  const syncAllBtn = document.getElementById('ctx-sync-all-btn');
  if (syncAllBtn) {
    // Sync-eligibility of the active scope (paused / not-enrolled) gates Sync
    // All the same way the matrix row Sync button is gated (#1203 review). The
    // specific reason rides on ``data-syncIneligible`` so the click handler and
    // the langchange refresh surface the matching copy, not the generic
    // no-fanout one.
    const syncAllIneligibleKey = _ctxSyncAllIneligibleKey();
    if (_ctxTargetScope === 'project_local') {
      // project_local has no runtime fan-out (ADR-0011 §3 / ADR-0016 §7),
      // so the Sync All button is disabled in this tier. We deliberately
      // do NOT ``return`` here — falling through lets the overview-card
      // click-to-navigate handler below still wire up, so the user can
      // drill into project_local lists from the overview tiles. Review
      // P2 (PR #940): the early return was making every overview tile
      // inert in this tier, leaving the count badges with no way to
      // navigate to the corresponding list.
      syncAllBtn.dataset.runtimeOnly = 'true';
      syncAllBtn.title = t('settings.ctx.project_local_no_fanout_tooltip');
      syncAllBtn.setAttribute('aria-disabled', 'true');
      // The no-fanout reason owns the disabled state in this tier; drop any
      // stale ineligibility reason so the handler's bail copy stays correct.
      delete syncAllBtn.dataset.syncIneligible;
    } else if (syncAllIneligibleKey) {
      syncAllBtn.dataset.runtimeOnly = 'true';
      syncAllBtn.dataset.syncIneligible = syncAllIneligibleKey;
      syncAllBtn.title = t(syncAllIneligibleKey);
      syncAllBtn.setAttribute('aria-disabled', 'true');
    } else {
      delete syncAllBtn.dataset.syncIneligible;
      const syncKinds = ['skills', 'commands', 'agents', 'mcp_servers'];
      const totals = syncKinds.reduce((acc, k) => {
        const d = data[k] || {};
        acc.total += d.total || 0;
        acc.runtimeOnly += d.missing_canonical || 0;
        return acc;
      }, { total: 0, runtimeOnly: 0 });
      if (totals.total > 0 && totals.runtimeOnly === totals.total) {
        syncAllBtn.dataset.runtimeOnly = 'true';
        syncAllBtn.title = t('settings.ctx.sync_all_disabled_tooltip');
        syncAllBtn.setAttribute('aria-disabled', 'true');
      } else {
        delete syncAllBtn.dataset.runtimeOnly;
        syncAllBtn.removeAttribute('aria-disabled');
        // The button has a default ``data-i18n-title`` (sync_all_tooltip);
        // wiping the attribute outright would clobber the locale-driven
        // hover tooltip that ``I18N.applyDOM`` set on page load.
        // Restore it from the dataset key instead.
        const titleKey = syncAllBtn.dataset.i18nTitle;
        if (titleKey) {
          syncAllBtn.title = t(titleKey);
        } else {
          syncAllBtn.removeAttribute('title');
        }
      }
    }
  }

  // Click to navigate. When the tile carries an actionable issue,
  // encode the dominant status into the URL so the leaf can filter and
  // highlight on mount (ADR-0009 §3 / issue #834). Tiles without an
  // issue (empty / synced / hard error) navigate without a filter,
  // *and* explicitly clear any prior deep-link so a stale ``?filter=``
  // from a previous click can't haunt the freshly-loaded leaf.
  //
  // The tile only knows the dominant *status* (the count rollup); it
  // does not know any specific artifact name, so ``artifact`` is left
  // empty. The artifact slot exists for shareable URLs (a teammate
  // pasting "open this URL to see the artifact I'm asking about") and
  // for future per-artifact issue panels.
  // PR #1088 review: navigation lives on the inner ``.ctx-overview-stat-nav``
  // <button> (a real button — Enter/Space activate natively, no custom
  // keydown shim). ``data-section`` / ``data-tile-key`` stay on the outer
  // tile so existing selectors keep working; the click handler reads them
  // via ``navBtn.closest('.ctx-overview-stat').dataset``. Pointer buttons
  // are siblings of the nav button (separate ``.ctx-overview-pointer``
  // selector below), so they're no longer nested inside the navigation
  // control.
  el.querySelectorAll('.ctx-overview-stat-nav').forEach(navBtn => {
    const tile = navBtn.closest('.ctx-overview-stat');
    const tileKey = tile ? tile.dataset.tileKey : '';
    const tileData = tileKey ? (data[tileKey] || {}) : null;
    const filter = tileData ? _ctxTileDominantFilter(tileData) : null;
    const section = tile ? tile.dataset.section : '';
    navBtn.addEventListener('click', () => {
      // Only deposit a ``?filter=`` deep-link when the target section is an
      // artifact list that actually consumes it. The settings tile navigates
      // to ``hooks-sync`` (``_ctxSectionToType`` → ''), which never reads the
      // filter, so setting one leaves a stale/misleading shareable URL.
      if (filter && _ctxSectionToType(section)) {
        _ctxSetDeepLink({ section, filter, artifact: '' });
      } else {
        _ctxClearDeepLink();
      }
      switchSettingsSection(section);
    });
  });

  // ADR-0009 §2: pointer-line click handlers. ``stopPropagation`` is
  // load-bearing: without it, clicking a ``data-action="sync-all"``
  // pointer would (1) trigger Sync All AND (2) the outer tile handler
  // would then call ``switchSettingsSection`` and pull the user off the
  // dashboard mid-fan-out. For ``data-action="leaf"`` pointers, both
  // handlers navigate to the same section, so propagation would be
  // idempotent — stopping it still keeps the call count at 1 for
  // testability and matches the sync-all handler's contract.
  el.querySelectorAll('.ctx-overview-pointer').forEach(btn => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      if (btn.dataset.action === 'sync-all') {
        const syncAllBtn = document.getElementById('ctx-sync-all-btn');
        if (syncAllBtn && syncAllBtn.getAttribute('aria-disabled') !== 'true') {
          syncAllBtn.click();
        }
      } else if (btn.dataset.action === 'leaf') {
        // Mirror the tile nav-button guard: a pointer leaf carries no filter
        // of its own, so a stale ``?filter=`` from a prior navigation must not
        // survive into a section that cannot consume it (e.g. hooks-sync),
        // which would make a reload/share land on the wrong, silently-filtered
        // section. Clear it before navigating when the target has no consumer.
        if (!_ctxSectionToType(btn.dataset.section)) {
          _ctxClearDeepLink();
        }
        switchSettingsSection(btn.dataset.section);
      }
    });
  });

  // Tier-aware write-block sweep (#945) — folds in the user-tier Sync All
  // gate alongside the existing data-runtime-only paths above. Idempotent
  // re-render after the runtime-only branches so the dim + ARIA states
  // settle on the final value (avoids the user-tier case being
  // clobbered by the project_shared else-branch that re-enables the
  // button). Placed AFTER the pointer click-handler wireup so any
  // future write-block sweep that wants to dim/disable a pointer can
  // see the buttons in their final wired state.
  _ctxRefreshWriteBlockedState();
  _ctxWireProjectsMatrix();
}

async function loadCtxOverview() {
  const seq = ++_ctxOverviewSeq;
  // Pin the tier once at entry. The projects fetch and the overview fetch must
  // run under the SAME tier — otherwise a tier flip between them would commit a
  // project list computed for one tier and then render overview counts for
  // another (ADR-0021 §C; mirrors the portal's #972 ``requestedScope`` guard).
  const requestedTier = _ctxTargetScope;
  const el = qs('ctx-overview-content');
  panelLoading(el);
  try {
    // Fetch projects, then commit ONLY after re-checking the guard, so a
    // superseded in-flight fetch can't clobber the shared cache / active scope
    // (#1194). The overview fetch URL depends on the just-committed active
    // scope, so the commit happens here, before it.
    // Overview is the only consumer of runtime coverage (the Project Scope
    // Matrix), so it is the only fetch that opts into the expensive probe.
    const projectsResult = await _ctxFetchProjectsData({
      targetScope: requestedTier,
      includeCoverage: true,
    });
    if (seq !== _ctxOverviewSeq || requestedTier !== _ctxTargetScope) return;
    _ctxCommitProjects(projectsResult);
    // Pin the resolved effective scope alongside the tier so the overview
    // request can't re-resolve against a cache a later refresh might mutate
    // (ADR-0021 §C: pin BOTH scope and tier).
    const pinnedScopeId = _ctxEffectiveScopeId();
    const res = await fetch(_ctxWithTargetScope('/api/context/overview', {
      targetScope: requestedTier,
      scopeId: pinnedScopeId,
      scopeResolved: true,
    }));
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || t('settings.ctx.load_overview_failed'));
    const data = await res.json();
    if (seq !== _ctxOverviewSeq || requestedTier !== _ctxTargetScope) return;
    // Shape guard (sibling of the #1100 projects-fetch hardening): a bare-null
    // or non-object 200 would TypeError inside _renderCtxOverview; route it
    // through the failure path instead.
    if (data === null || typeof data !== 'object') throw new Error(t('settings.ctx.load_overview_failed'));
    _ctxOverviewCache = data;
    _renderCtxOverview(data);
  } catch (err) {
    if (seq !== _ctxOverviewSeq || requestedTier !== _ctxTargetScope) return;
    _ctxOverviewCache = null;
    el.innerHTML = emptyState('', t('settings.ctx.load_overview_failed'), err.message);
  }
}

function _renderProjectsMatrix() {
  const scopes = _ctxProjectsCache || [];
  if (!scopes.length) return '';

  const runtimes = ['claude', 'gemini', 'codex', 'kimi'];

  // Table header
  let html = `<div class="ctx-projects-matrix-container">
    <h3 class="ctx-projects-matrix-title">${escapeHtml(t('settings.ctx.projects_matrix_title') || 'Project Scope Matrix')}</h3>
    <table class="ctx-projects-matrix-table">
      <thead>
        <tr>
          <th>${escapeHtml(t('settings.ctx.matrix_col_project') || 'Project')}</th>
          <th>${escapeHtml(t('settings.ctx.matrix_col_counts') || 'Inventory')}</th>
          ${runtimes.map(rt => `<th>${escapeHtml(rt.toUpperCase())}</th>`).join('')}
          <th>${escapeHtml(t('settings.ctx.matrix_col_actions') || 'Actions')}</th>
        </tr>
      </thead>
      <tbody>`;

  for (const scope of scopes) {
    const isActive = _ctxScopeIsActive(scope);
    const rowClass = isActive ? 'ctx-matrix-row--active' : '';
    const label = _ctxScopeDisplayLabel(scope);
    const rootPath = scope.root || '';
    const isMissing = !!scope.missing;

    // Inventory summary. ``counts`` is ``null`` when the projects fetch did not
    // opt into ``?include=counts`` (or the scope has no root) — render a muted
    // dash for "not computed" instead of a misleading all-zero row.
    const hasCounts = !!scope.counts && typeof scope.counts === 'object';
    const c = hasCounts ? scope.counts : {};
    const skillsCount = c.skills || 0;
    const commandsCount = c.commands || 0;
    const agentsCount = c.agents || 0;
    const mcpCount = c['mcp-servers'] || 0;
    const countsTitle = t('settings.ctx.matrix_counts_title', {
      skills: skillsCount, commands: commandsCount, agents: agentsCount, mcp: mcpCount,
    });
    const countsHtml = hasCounts
      ? `<span class="ctx-matrix-counts" title="${escapeHtml(countsTitle)}">
      🧩${skillsCount} ⌘${commandsCount} 🤖${agentsCount} 🔌${mcpCount}
    </span>`
      : '<span class="ctx-matrix-counts text-muted">—</span>';

    // Runtimes columns. State → (css class, label key, title key); labels and
    // tooltips are localized via ``t()`` so the badges translate (the rest of
    // the matrix already does). ``key`` also gates the registration suffix.
    const runtimeCols = runtimes.map(rtName => {
      const coverage = (scope.runtime_coverage || []).find(rc => rc.name === rtName);
      const available = !!(coverage && coverage.available);
      const installed = !!(coverage && coverage.installed);
      const registered = !!(coverage && coverage.memtomem_registered);

      let badgeCls = 'badge-gray';
      let key = 'none';
      if (available && installed && registered) {
        badgeCls = 'badge-success'; key = 'active';
      } else if (available && installed) {
        badgeCls = 'badge-warning'; key = 'detected';
      } else if (available) {
        badgeCls = 'badge-yellow'; key = 'available';
      } else if (installed) {
        badgeCls = 'badge-blue'; key = 'client';
      } else if (registered) {
        badgeCls = 'badge-gray'; key = 'registered';
      }

      let badgeText = t(`settings.ctx.matrix_badge_${key}`);
      // Mark registration only on states that don't already imply it — ``active``
      // and ``registered`` do, and ``none`` carries no runtime to annotate.
      if (registered && key !== 'active' && key !== 'registered' && key !== 'none') {
        badgeText += t('settings.ctx.matrix_badge_reg_suffix');
      }
      const titleText = t(`settings.ctx.matrix_badge_${key}_title`);

      return `<td>
        <span class="badge ${badgeCls}" title="${escapeHtml(titleText)}">${escapeHtml(badgeText)}</span>
      </td>`;
    }).join('');

    // Actions column
    // Switch scope button (Selected Project Scope for Reads/Sync)
    const selectBtn = isActive
      ? `<span class="badge badge-success">${escapeHtml(t('settings.ctx.active') || 'Selected')}</span>`
      : `<button type="button" class="btn-ghost btn-xs ctx-matrix-select-btn" data-scope-id="${escapeHtml(scope.scope_id)}">${escapeHtml(t('settings.ctx.select') || 'Select')}</button>`;

    // Sync button. Disabled reasons, in precedence order:
    //   1. project_local (no fan-out) / missing root — existing no-op cases.
    //   2. not sync-eligible — the scope is discoverable but excluded from sync
    //      because it is not enrolled, or enrolled-but-paused (#1203). The
    //      tooltip points the user at the Projects board to enroll / resume.
    // The tooltip MUST ride on ``data-i18n-title`` (not a plain ``title``):
    // ``.ctx-matrix-sync-btn`` is in ``_CTX_WRITE_BUTTON_SELECTOR``, so a flip
    // back to project_shared runs ``_ctxRefreshWriteBlockedState`` which
    // restores ``title`` from ``data-i18n-title`` — a plain ``title`` would be
    // stripped (``removeAttribute('title')``), silently dropping the reason.
    const isProjectLocal = _ctxTargetScope === 'project_local';
    let syncDisabled = '';
    if (isProjectLocal || isMissing) {
      const k = 'settings.ctx.matrix_sync_disabled_title';
      syncDisabled = ` disabled data-i18n-title="${k}" title="${escapeHtml(t(k))}"`;
    } else if (!_ctxScopeSyncEligible(scope)) {
      const k = _ctxScopeIsEnrolled(scope)
        ? 'settings.ctx.matrix_sync_paused_title'
        : 'settings.ctx.matrix_sync_not_enrolled_title';
      syncDisabled = ` disabled data-i18n-title="${k}" title="${escapeHtml(t(k))}"`;
    }
    const syncBtn = `<button type="button" class="btn-primary btn-xs ctx-matrix-sync-btn" data-scope-id="${escapeHtml(scope.scope_id)}"${syncDisabled}>${escapeHtml(t('settings.ctx.sync') || 'Sync')}</button>`;

    // Remove button (Server CWD는 삭제 불가능)
    const removable = !_ctxScopeIsServerCwd(scope);
    const removeAria = t('settings.ctx.remove_project_aria')
      .replace('{label}', scope.label)
      .replace('{root}', scope.root || scope.scope_id);
    const removeBtn = removable
      ? `<button type="button" class="ctx-matrix-remove-btn text-danger" data-scope-id="${escapeHtml(scope.scope_id)}" aria-label="${escapeHtml(removeAria)}" title="${escapeHtml(removeAria)}">×</button>`
      : '';

    html += `<tr class="${rowClass}">
      <td>
        <div class="ctx-matrix-project-label" title="${escapeHtml(rootPath)}">${escapeHtml(label)}</div>
        <code class="ctx-matrix-project-path">${escapeHtml(rootPath)}</code>
      </td>
      <td>${countsHtml}</td>
      ${runtimeCols}
      <td>
        <div class="ctx-matrix-actions">
          ${selectBtn}
          ${syncBtn}
          ${removeBtn}
        </div>
      </td>
    </tr>`;
  }

  html += `</tbody>
    </table>
    <div class="ctx-matrix-footer">
      <button type="button" class="btn-ghost btn-sm ctx-matrix-add-project-btn">+ ${escapeHtml(t('settings.ctx.add_project') || 'Add Project')}</button>
    </div>
  </div>`;

  return html;
}

function _ctxWireProjectsMatrix() {
  const container = document.querySelector('.ctx-projects-matrix-container');
  if (!container) return;

  // 1. Select / Switch Scope
  container.querySelectorAll('.ctx-matrix-select-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const scopeId = btn.dataset.scopeId || '';
      if (scopeId === _ctxActiveScopeId) return;
      _ctxActiveScopeId = scopeId;
      _ctxNormalizeActiveScope(_ctxProjectsCache);
      _ctxBumpActiveScopeDetailSeq();
      try { localStorage.setItem(_CTX_ACTIVE_SCOPE_KEY, _ctxActiveScopeId); } catch {}
      _ctxClearDeepLink();
      loadCtxOverview();
    });
  });

  // 2. Sync Scope
  container.querySelectorAll('.ctx-matrix-sync-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const scopeId = btn.dataset.scopeId;
      if (scopeId !== undefined) {
        _ctxSyncProjectScope(scopeId, btn);
      }
    });
  });

  // 3. Remove Scope (Delete)
  container.querySelectorAll('.ctx-matrix-remove-btn').forEach(btn => {
    btn.addEventListener('click', async (ev) => {
      const scopeId = btn.dataset.scopeId;
      const scope = _ctxProjectsCache.find(s => s.scope_id === scopeId);
      if (!scope) return;
      ev.preventDefault();
      ev.stopPropagation();
      const ok = await showConfirm({
        title: t('settings.ctx.remove_project'),
        message: t('settings.ctx.confirm_remove_project')
          .replace('{label}', scope.label)
          .replace('{root}', scope.root || scope.scope_id),
        confirmText: t('settings.ctx.remove'),
      });
      if (!ok) return;
      try {
        const csrf = await ensureCsrfToken();
        const r = await fetch(`/api/context/known-projects/${encodeURIComponent(scopeId)}`, {
          method: 'DELETE',
          headers: csrf ? { 'X-Memtomem-CSRF': csrf } : {},
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        loadCtxOverview();
      } catch (err) {
        showToast(t('toast.delete_failed', { error: err.message }), 'error');
      }
    });
  });

  // 4. Add Project
  container.querySelector('.ctx-matrix-add-project-btn')?.addEventListener('click', (ev) => {
    const btn = ev.currentTarget;
    const onSelect = async (root) => {
      if (!root) return;
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const r = await fetch('/api/context/known-projects', {
          method: 'POST',
          headers,
          body: JSON.stringify({ root }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        const data = await r.json();
        const warningKey = data.warning_code
          ? `settings.ctx.add_project_warning_${data.warning_code}`
          : null;
        if (warningKey) {
          const localized = t(warningKey);
          const message = localized === warningKey
            ? (data.warning || localized)
            : localized;
          showToast(message, 'warning');
        } else if (data.warning) {
          showToast(data.warning, 'warning');
        } else {
          showToast(t('settings.ctx.add_project_success'), 'success');
        }
        if (data.scope_id) {
          _ctxActiveScopeId = data.scope_id;
          try { localStorage.setItem(_CTX_ACTIVE_SCOPE_KEY, _ctxActiveScopeId); } catch {}
        }
        loadCtxOverview();
      } catch (err) {
        showToast(t('toast.request_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    };
    if (window.PathPicker && typeof window.PathPicker.open === 'function') {
      window.PathPicker.open({ purpose: 'project', onSelect });
      return;
    }
    const raw = window.prompt(t('settings.ctx.add_project_prompt'), '');
    if (!raw) return;
    const root = raw.trim();
    if (!root) return;
    onSelect(root);
  });
}

async function _ctxSyncProjectScope(scopeId, btn) {
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all'),
    message: t('settings.ctx.confirm_sync_all'),
    confirmText: t('settings.ctx.sync'),
  });
  if (!ok) return;

  btnLoading(btn, true);
  showToast(t('settings.ctx.sync_started') || 'Syncing project...', 'info');

  // Pin BOTH scope and tier once, up front, then pass them frozen to every
  // phase. ``_ctxWithTargetScope`` otherwise re-reads the mutable
  // ``_ctxTargetScope`` global and re-resolves the id against the live
  // ``_ctxProjectsCache`` on every call, so a mid-run tier-filter flip OR a
  // projects-cache refresh (marking the pinned scope missing) could send later
  // phases to a different (project, tier) — violating the "one (project, tier)
  // per invocation" invariant the canonical Sync All enforces (ADR-0016 §5 /
  // ADR-0021 §C). ``scopeResolved`` emits the already-effective id verbatim
  // (Server-CWD collapses to '').
  const pinnedScopeId = _ctxEffectiveScopeId(scopeId);
  const pinnedTier = _ctxTargetScope;
  const pinnedScopeOpts = {
    scopeId: pinnedScopeId,
    scopeResolved: true,
    targetScope: pinnedTier,
  };

  const succeeded = [];
  let failed = null;
  let settingsSeverity = null;
  let settingsReason = '';
  let anyPhaseStarted = false;
  try {
    const csrf = await ensureCsrfToken();
    const headers = csrf
      ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
      : { 'Content-Type': 'application/json' };
    
    const types = ['skills', 'commands', 'agents', 'mcp-servers'];
    for (const typ of types) {
      anyPhaseStarted = true;
      let resp;
      try {
        resp = await fetch(
          _ctxWithTargetScope(`/api/context/${typ}/sync`, pinnedScopeOpts),
          { method: 'POST', headers }
        );
      } catch (err) {
        failed = { phase: typ, reason: err.message };
        break;
      }
      if (!resp.ok) {
        failed = {
          phase: typ,
          reason: await _ctxErrorMessageFromResponse(resp, `Sync ${typ} failed`),
        };
        break;
      }
      succeeded.push(typ);
    }

    if (!failed) {
      anyPhaseStarted = true;
      try {
        const settingsResp = await fetch(
          _ctxWithTargetScope('/api/context/settings/sync', pinnedScopeOpts),
          { method: 'POST', headers }
        );
        if (!settingsResp.ok) {
          failed = {
            phase: 'settings',
            reason: await _ctxErrorMessageFromResponse(settingsResp, 'Settings sync failed'),
          };
        } else {
          const settingsData = await settingsResp.json().catch(() => ({}));
          const settingsResults = settingsData.results || [];
          const firstWithStatus = (s) => settingsResults.find(r => r && r.status === s);
          const errored = firstWithStatus('error');
          const aborted = firstWithStatus('aborted');
          const needsConfirmation = firstWithStatus('needs_confirmation');
          if (errored) {
            settingsSeverity = 'error';
            settingsReason = errored.reason || '';
          } else if (aborted) {
            settingsSeverity = 'aborted';
          } else if (needsConfirmation) {
            settingsSeverity = 'needs_confirmation';
          } else {
            settingsSeverity = 'ok';
          }
        }
      } catch (err) {
        failed = { phase: 'settings', reason: err.message };
      }
    }

    const phaseLabel = (p) => t(`settings.ctx.${String(p).replace(/-/g, '_')}_phase_title`);
    if (failed) {
      if (succeeded.length === 0) {
        showToast(t('toast.sync_failed', { error: failed.reason }), 'error');
      } else {
        showToast(
          t('toast.sync_partial_failed', {
            succeeded: succeeded.map(phaseLabel).join(', '),
            failed_phase: phaseLabel(failed.phase),
            reason: failed.reason,
          }),
          'error'
        );
      }
    } else if (settingsSeverity === 'error') {
      showToast(t('toast.sync_failed', { error: settingsReason }), 'error');
    } else if (settingsSeverity === 'aborted') {
      showToast(t('settings.ctx.mtime_conflict'), 'warning');
    } else if (settingsSeverity === 'needs_confirmation') {
      showToast(
        t('toast.sync_partial_settings_needs_confirmation'),
        'info',
        {
          action: {
            label: t('toast.open_settings_action'),
            onClick: () => switchSettingsSection('hooks-sync'),
          },
        }
      );
    } else {
      showToast(t('settings.ctx.sync_success'));
    }
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    if (anyPhaseStarted) {
      loadCtxOverview();
    }
    btnLoading(btn, false);
  }
}

// JS-owned ``title`` strings (as opposed to ``data-i18n-title`` which
// I18N.applyDOM handles automatically) need a langchange refresh so the
// hover tooltip stays in sync with the active locale. Only rewrites the
// attribute when the gate is still active — clearing it during normal
// state would erase any caller's title.
//
// The overview cards themselves are inline-templated via ``t()`` in
// ``_renderCtxOverview``'s innerHTML, so ``I18N.applyDOM`` cannot re-translate
// the rendered text on toggle (it only walks ``data-i18n*`` attributes).
// Re-render only when both gates are active:
//   * the Settings *main tab* (``#tab-settings``) is the visible panel —
//     ``activateTab`` toggles ``.active`` + ``hidden`` here when the
//     user switches between main tabs.
//   * the Context Gateway *settings section* (``#settings-ctx-overview``
//     OR one of the per-type sections) is the active sub-pane —
//     ``switchSettingsSection`` toggles ``.active`` here when the user
//     clicks a settings nav item.
// Without both checks, switching from Settings → Search keeps the
// section's ``.active`` class set (``activateTab`` hides the panel but
// doesn't reach into sub-section classes), and a language toggle from
// Search would re-render an off-screen dashboard (#824 review P2).
//
// Overview re-render path: when ``_ctxOverviewCache`` holds a prior
// payload, render directly from it — translation is locale-only, so no
// fetch and no ``panelLoading`` spinner flash (#825). The cold-mount
// fallback to ``loadCtxOverview`` only fires when the dashboard has
// never successfully loaded (initial mount race, or prior fetch error
// cleared the cache); in that case ``loadCtxOverview``'s sequence guard
// handles the fetch-in-flight scenario.
//
// Per-type list re-render path (Q-PR4 / #826): the same inline-``t()``
// staleness exists in ``renderImportResult`` (post-Import receipt),
// ``_ctxScopeBadges`` (non-cwd scope badges), ``_ctxRefreshSectionState``
// (runtime-only banner), ``renderRuntimeBadges`` (status labels), and
// ``renderDroppedChips`` (Diff pane). All five are rebuilt by re-issuing
// ``loadCtxList(type)`` for the active section. The Import status box
// is intentionally cleared rather than cached: it's an ephemeral
// post-Import receipt and caching it across navigation would resurrect
// a stale message in misleading form. ``loadCtxList``'s ``_ctxListSeq``
// guard makes a rapid EN→KO→EN burst safe.
window.addEventListener('langchange', () => {
  const btn = document.getElementById('ctx-sync-all-btn');
  if (btn && btn.dataset.runtimeOnly === 'true') {
    // ``_renderCtxOverview`` sets ``dataset.runtimeOnly='true'`` in three
    // cases: (1) project_local tier (canonical drafts have no fan-out),
    // (2) all-canonicals-empty for any tier, and (3) the active scope is
    // sync-ineligible (paused / not enrolled — keyed on
    // ``data-syncIneligible``). Mirror its reason choice here so an EN→KO→EN
    // locale flip doesn't revert the hover text to the wrong copy. User-tier
    // writes are gated by ``_ctxRefreshWriteBlockedState`` below — that path
    // owns the user-tier tooltip refresh now (#943).
    btn.title = btn.dataset.syncIneligible
      ? t(btn.dataset.syncIneligible)
      : _ctxTargetScope === 'project_local'
        ? t('settings.ctx.project_local_no_fanout_tooltip')
        : t('settings.ctx.sync_all_disabled_tooltip');
  }
  // Re-translate write-blocked button tooltips on every locale flip so
  // the dim button's hover copy stays consistent with the active
  // locale. The banner text (set via ``textContent`` inside
  // ``loadCtxList``) is re-rendered by the ``loadCtxList`` re-issue
  // below — no separate handling needed.
  _ctxRefreshWriteBlockedState();
  // Gateway sections now live under ``#tab-context-gateway`` (#962). Keep
  // the legacy ``#tab-settings`` check as a fallback so a partial revert
  // doesn't silently disable the langchange re-render — drop it once the
  // sections live only under the Gateway tab.
  const gatewayTab = document.getElementById('tab-context-gateway');
  const settingsTab = document.getElementById('tab-settings');
  const hostActive =
    (gatewayTab && gatewayTab.classList.contains('active'))
    || (settingsTab && settingsTab.classList.contains('active'));
  if (!hostActive) return;

  const overviewSection = document.getElementById('settings-ctx-overview');
  if (overviewSection && overviewSection.classList.contains('active')) {
    if (_ctxOverviewCache) {
      _renderCtxOverview(_ctxOverviewCache);
    } else {
      loadCtxOverview();
    }
    // The Sync All status region lives outside ``#ctx-overview-content`` so
    // neither re-render above touches it. Re-render from the retained state so
    // its phase labels / summary follow the locale flip (#698 staleness class).
    if (_ctxSyncStatusState) _renderCtxSyncStatus(_ctxSyncStatusState);
    // The Context Gateway sub-sections are mutually exclusive — if the
    // overview is active, none of the per-type list sections can be.
    return;
  }

  for (const type of ['skills', 'commands', 'agents', 'mcp-servers']) {
    const sec = document.getElementById(`settings-ctx-${type}`);
    if (!sec || !sec.classList.contains('active')) continue;
    // Capture detail state *before* ``loadCtxList`` resets it (see the
    // ``_ctxCurrentDetail`` reset near the top of ``loadCtxList``). We
    // need ``runtimeOnly`` to route to the matching loader and
    // ``wasDiffActive`` to land back on the Diff tab so
    // ``renderDroppedChips`` re-renders without further user clicks.
    const detailEl = qs(`ctx-${type}-detail`);
    const openName = (_ctxCurrentDetail.type === type) ? _ctxCurrentDetail.name : null;
    const openRuntimeOnly = openName ? _ctxCurrentDetail.runtimeOnly === true : false;
    const wasDiffActive = openName && detailEl
      ? detailEl.querySelector('.ctx-detail-tab[data-pane="diff"].active') != null
      : false;
    // Edit-mode buffer preservation. Two complementary mechanisms:
    //   1. Capture the dirty textarea + pre-toggle mtime into the
    //      module-level ``_ctxPendingEdit`` stash *before* loadCtxList
    //      wipes the detail. Module-level (not closure-local) so a
    //      rapid second toggle that finds an already-wiped DOM doesn't
    //      lose the buffer — the stash carries it forward until the
    //      latest detail mount applies it.
    //   2. The post-loadCtxDetail ``.then()`` checks ``_ctxDetailSeq``
    //      so only the newest detail mount actually paints the
    //      stash into the DOM. An older `.then()` whose mount was
    //      superseded skips, leaving the stash for the newer mount.
    // Why module-level + seq-guarded instead of closure-local:
    //   T1 captures buffer; T2 fires before T1's fetch settles; T2
    //   sees a wiped DOM (no editPane) so closure-local capture would
    //   yield null → buffer silently dropped on T2's mount. Sharing
    //   via _ctxPendingEdit + bailing older .then()s gives the latest
    //   toggle's mount sole authority to apply the captured buffer.
    // mtime preservation: on capture, ``detailEl.dataset.mtimeNs`` is
    // the pre-toggle value (loadCtxDetail hasn't refetched yet). The
    // .then() restores it so the next Save still triggers the
    // backend's 409 conflict gate (#763) on an external on-disk edit.
    const editPane = detailEl ? detailEl.querySelector('#ctx-pane-edit') : null;
    const editTextarea = detailEl ? detailEl.querySelector('#ctx-edit-content') : null;
    const wasEditing = editPane != null && !editPane.hidden;
    if (wasEditing && editTextarea && openName && !openRuntimeOnly) {
      _ctxPendingEdit = {
        type,
        name: openName,
        content: editTextarea.value,
        mtimeNs: detailEl ? (detailEl.dataset.mtimeNs || '') : '',
      };
    }

    loadCtxList(type);

    if (openName) {
      // ``preservePendingEdit: true`` opts these mounts out of the
      // navigation-drop guard inside ``loadCtxDetail`` /
      // ``_ctxLoadRuntimeOnlyDetail`` — the langchange listener IS the
      // intended stash consumer, the drop only fires on user-initiated
      // navigations to a different (or same-name post-edit-discard)
      // mount. Without the flag here, the listener's own list-rebuild +
      // detail re-mount would clear the stash that the post-mount
      // ``.then()`` is about to apply (rapid-toggle regression).
      if (openRuntimeOnly) {
        _ctxLoadRuntimeOnlyDetail(type, openName, detailEl, {
          preservePendingEdit: true,
        });
      } else {
        const detailPromise = loadCtxDetail(type, openName, {
          autoOpenDiff: wasDiffActive,
          preservePendingEdit: true,
        });
        // ``loadCtxDetail`` synchronously bumps ``_ctxDetailSeq[type]``
        // before returning the promise, so reading it here captures
        // *this* invocation's seq. A subsequent invocation (e.g. from
        // a rapid second toggle) will bump the counter further; this
        // .then() then bails by inequality. The bail intentionally
        // does NOT clear the stash — a later .then() (rapid-toggle
        // L2) is the consumer; navigation orphans are caught up-front
        // by the navigation-drop guard, not here.
        const myDetailSeq = _ctxDetailSeq[type];
        if (detailPromise && typeof detailPromise.then === 'function') {
          detailPromise.then(() => {
            if (myDetailSeq !== _ctxDetailSeq[type]) return;
            const pending = _ctxPendingEdit;
            if (!pending || pending.type !== type || pending.name !== openName) return;
            // Re-resolve panes — the previous detailEl children were
            // wiped by loadCtxDetail's innerHTML rewrite.
            const newTa = detailEl.querySelector('#ctx-edit-content');
            const newCanonPane = detailEl.querySelector(`#ctx-pane-${type}-canonical`);
            const newEditPane = detailEl.querySelector('#ctx-pane-edit');
            if (!newTa || !newEditPane) return;
            newTa.value = pending.content;
            if (newCanonPane) newCanonPane.hidden = true;
            newEditPane.hidden = false;
            // Match the in-edit affordance: tabs are hidden while editing
            // (see the Edit click handler in loadCtxDetail).
            detailEl.querySelectorAll('.ctx-detail-tab').forEach(tab => {
              tab.style.display = 'none';
            });
            // Restore pre-toggle mtime so the next Save still surfaces
            // a 409 if the file changed on disk during the toggle window.
            detailEl.dataset.mtimeNs = pending.mtimeNs;
            _ctxPendingEdit = null;
          });
        }
      }
    }
    return;
  }
});

// -- Sync All per-phase progress + result summary (ADR-0021 §C) --------------
//
// The Sync All fan-out is a sequence of independent ``POST /sync`` calls (one
// per artifact type, then settings), so the streaming ``makeChunkProgressRenderer``
// (built for SSE chunk events) does not fit. Instead we render the phase list
// declaratively from a single state object: each phase carries a ``state``
// (pending → syncing → done | failed | not_run) and an optional one-line
// ``summary`` (generated/dropped/skipped counts). The same object is the source
// of truth re-rendered on ``langchange`` (so a locale flip re-translates the
// labels) and cleared on a scope/tier switch (the summary is per-run).
const _CTX_SYNC_PHASES = ['skills', 'commands', 'agents', 'mcp-servers', 'settings'];

// Last Sync All run's phase states, or null when nothing has run / was cleared.
// Shape: { skills: {state, summary?}, commands: {...}, ... }.
let _ctxSyncStatusState = null;

function _ctxSyncPhaseLabel(phase) {
  // Reuse the existing per-phase titles (also used by the partial-failure
  // toast). ``mcp-servers`` → ``mcp_servers_phase_title``.
  return t(`settings.ctx.${String(phase).replace(/-/g, '_')}_phase_title`);
}

// Extract RAW per-type counts from an artifact sync response body
// ({generated, dropped?, skipped}). Stored verbatim in the phase state — never
// a pre-localized string — so the ``langchange`` re-render can format them in
// the current locale (a frozen localized summary would leave a Korean phase
// label next to an English "2 generated", #698 staleness class).
function _ctxSyncArtifactCounts(body) {
  const len = (arr) => (Array.isArray(arr) ? arr.length : 0);
  return {
    generated: len(body && body.generated),
    dropped: len(body && body.dropped),
    skipped: len(body && body.skipped),
  };
}

// Format raw counts into a localized one-line summary, AT RENDER TIME. Counts
// of 0 for dropped/skipped are omitted to keep the line uncluttered;
// ``generated`` is always shown so a no-op sync reads as "0 generated" rather
// than as a blank.
function _ctxSyncFormatCounts(counts) {
  const c = counts || {};
  const parts = [t('settings.ctx.sync_count_generated', { count: c.generated || 0 })];
  if (c.dropped > 0) parts.push(t('settings.ctx.sync_count_dropped', { count: c.dropped }));
  if (c.skipped > 0) parts.push(t('settings.ctx.sync_count_skipped', { count: c.skipped }));
  return parts.join(' · ');
}

// Render (or clear, when ``states`` is null) the status region. Declarative —
// rebuilds the whole list each call so repeated updates can't desync.
function _renderCtxSyncStatus(states) {
  const el = document.getElementById('ctx-sync-status');
  if (!el) return;
  _ctxSyncStatusState = states;
  if (!states) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  const rows = _CTX_SYNC_PHASES.map((phase) => {
    const entry = states[phase] || { state: 'pending' };
    const label = escapeHtml(_ctxSyncPhaseLabel(phase));
    let badge;
    if (entry.state === 'syncing') {
      badge = `<span class="ctx-sync-spinner" aria-hidden="true"></span>`
        + `<span class="ctx-sync-state">${escapeHtml(t('settings.ctx.sync_state_syncing'))}</span>`;
    } else if (entry.state === 'done') {
      // Format the stored raw counts here so a langchange re-render picks up
      // the active locale; phases with no counts (settings) read as "Done".
      const text = entry.counts
        ? _ctxSyncFormatCounts(entry.counts)
        : t('settings.ctx.sync_state_done');
      badge = `<span class="ctx-sync-state ctx-sync-state--done">${escapeHtml(text)}</span>`;
    } else if (entry.state === 'failed') {
      badge = `<span class="ctx-sync-state ctx-sync-state--failed">`
        + `${escapeHtml(t('settings.ctx.sync_state_failed'))}</span>`;
    } else if (entry.state === 'attention') {
      // Settings landed but needs host-write confirmation — distinct from a
      // plain ``done`` so the row matches the "complete except Settings" toast.
      badge = `<span class="ctx-sync-state ctx-sync-state--attention">`
        + `${escapeHtml(t('settings.ctx.sync_state_needs_confirmation'))}</span>`;
    } else if (entry.state === 'not_run') {
      badge = `<span class="ctx-sync-state ctx-sync-state--muted">`
        + `${escapeHtml(t('settings.ctx.sync_state_not_run'))}</span>`;
    } else {
      badge = `<span class="ctx-sync-state ctx-sync-state--muted">`
        + `${escapeHtml(t('settings.ctx.sync_state_pending'))}</span>`;
    }
    // ``entry.state`` is a controlled enum (set only by ``setPhase`` with
    // literal values), so it needs no escaping in the class name.
    return `<li class="ctx-sync-phase ctx-sync-phase--${entry.state}">`
      + `<span class="ctx-sync-phase-label">${label}</span>${badge}</li>`;
  }).join('');
  el.hidden = false;
  el.innerHTML = `<p class="ctx-sync-status-heading">`
    + `${escapeHtml(t('settings.ctx.sync_status_heading'))}</p>`
    + `<ul class="ctx-sync-phase-list">${rows}</ul>`;
}

// Disable (or restore) the tier + active-project controls for the duration of
// a Sync All run. The tier/project values are already pinned into the phase
// URLs (see ``_ctxWithTargetScope`` opts), so this is a clarity affordance —
// it prevents the user from flipping a control whose change won't take effect
// until the next run. Scoped to the overview section so per-list-page controls
// are untouched. ``loadCtxOverview`` re-renders fresh (enabled) controls in the
// handler's ``finally``, so this only governs the in-flight window.
function _ctxSetSyncControlsDisabled(disabled) {
  document
    .querySelectorAll('#settings-ctx-overview .ctx-tier-filter button')
    .forEach((btn) => { btn.disabled = disabled; });
  document
    .querySelectorAll('#settings-ctx-overview .ctx-project-select')
    .forEach((sel) => { sel.disabled = disabled; });
}

// Sync All button
document.getElementById('ctx-sync-all-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-sync-all-btn');
  if (btn.dataset.runtimeOnly === 'true') {
    // ``_renderCtxOverview`` stamps ``runtimeOnly='true'`` in three cases:
    // project_local (canonical drafts have no fan-out — ADR-0011 §3 /
    // ADR-0016 §7), all-canonicals-empty for any other tier, and the active
    // scope being sync-ineligible (paused / not enrolled — keyed on
    // ``data-syncIneligible``). The post-click toast must mirror the
    // pre-click hover tooltip's reason choice (done in ``_renderCtxOverview``
    // and the ``langchange`` listener above); otherwise the user sees copy
    // that doesn't apply to the actual disable reason. Issue #1075 / #1203.
    const msgKey = btn.dataset.syncIneligible
      ? btn.dataset.syncIneligible
      : _ctxTargetScope === 'project_local'
        ? 'settings.ctx.project_local_no_fanout_tooltip'
        : 'settings.ctx.sync_all_disabled_tooltip';
    showToast(t(msgKey), 'info');
    return;
  }
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all'),
    message: t('settings.ctx.confirm_sync_all'),
    confirmText: t('settings.ctx.sync'),
  });
  if (!ok) return;
  btnLoading(btn, true);
  // Lock the tier/project controls for the run. The phase URLs are pinned to
  // the snapshot below, so this is a clarity affordance — a mid-run flip
  // wouldn't take effect until the next run, and disabling makes that obvious.
  _ctxSetSyncControlsDisabled(true);
  // Track per-phase outcomes so we can (a) refresh the overview even when
  // a later phase fails — without this the dashboard keeps showing
  // pre-sync counts while disk has already moved (issue #1074) — and
  // (b) surface a partial-result toast naming what landed and what
  // didn't. Without the partial copy, a "Sync failed: X" toast after
  // skills already wrote to disk looks like nothing happened.
  const succeeded = [];
  let failed = null;
  let anyPhaseStarted = false;
  // Declarative per-phase status: all pending up front, then each phase moves
  // pending → syncing → done | failed; phases never reached after a failure
  // are marked not_run at the end. ``setPhase`` mutates the shared object in
  // place and re-renders, so the ``langchange`` listener can re-translate from
  // the same object (ADR-0021 §C per-phase progress + result summary). The
  // third arg is RAW counts ({generated,dropped,skipped}), never a localized
  // string — formatting happens in ``_renderCtxSyncStatus`` so a locale flip
  // re-translates the summary too.
  const phaseStates = {};
  for (const phase of _CTX_SYNC_PHASES) phaseStates[phase] = { state: 'pending' };
  const setPhase = (phase, state, counts) => {
    phaseStates[phase] = { state, counts };
    _renderCtxSyncStatus(phaseStates);
  };
  _renderCtxSyncStatus(phaseStates);
  try {
    // Snapshot BOTH dimensions once, right after confirm and before the first
    // await, then pass them fixed to every phase URL. ``_ctxWithTargetScope``
    // otherwise re-reads the mutable ``_ctxTargetScope`` global (tier) and
    // re-resolves the scope against the live ``_ctxProjectsCache`` on each
    // call, so a mid-run tier flip OR a cache refresh could send later phases
    // to a different (project, tier) — violating "one (project, tier) per
    // invocation" (ADR-0016 §5 / ADR-0021 §C Major-1). The scope is resolved to
    // its effective value here (Server-CWD collapses to '') and passed with
    // ``scopeResolved`` so it is emitted verbatim. Pinning, not bailing: the
    // run completes on the (project, tier) the confirm dialog was shown for;
    // any flip applies to the next run.
    const syncAllScopeId = _ctxEffectiveScopeId(_ctxActiveScopeId);
    const syncAllTier = _ctxTargetScope;
    const csrf = await ensureCsrfToken();
    const headers = csrf
      ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
      : { 'Content-Type': 'application/json' };
    const types = ['skills', 'commands', 'agents', 'mcp-servers'];
    for (const typ of types) {
      anyPhaseStarted = true;
      setPhase(typ, 'syncing');
      let resp;
      try {
        resp = await fetch(
          _ctxWithTargetScope(`/api/context/${typ}/sync`, {
            scopeId: syncAllScopeId,
            scopeResolved: true,
            targetScope: syncAllTier,
          }),
          { method: 'POST', headers },
        );
      } catch (err) {
        failed = { phase: typ, reason: err.message };
        setPhase(typ, 'failed');
        break;
      }
      if (!resp.ok) {
        failed = {
          phase: typ,
          reason: await _ctxErrorMessageFromResponse(resp, `Sync ${typ} failed`),
        };
        setPhase(typ, 'failed');
        break;
      }
      // Parse the body for the per-type result counts (generated/dropped/
      // skipped). Tolerates an empty/non-JSON body — a bare ``{}`` yields all
      // zeros (renders as "0 generated"), never throws. Store RAW counts; the
      // render formats them per-locale.
      const body = await resp.json().catch(() => ({}));
      succeeded.push(typ);
      setPhase(typ, 'done', _ctxSyncArtifactCounts(body));
    }
    // Settings hooks sync (additive merge) — appends memtomem-owned hook
    // entries to ~/.claude/settings.json without clobbering user-authored
    // entries. Promoted from dev-only via RFC #761 (ADR-0001 §5 criteria
    // + HTTP-layer test fixtures). Skipped entirely if a prior artifact
    // phase failed — settings often share root cause with artifacts
    // (perms/scope), and attempting it after a failure is just noise.
    //
    // The route returns HTTP 200 even when individual generators fail —
    // each per-result entry carries its own ``status`` (one of ``ok`` /
    // ``skipped`` / ``error`` / ``needs_confirmation`` / ``aborted``,
    // see ``generate_all_settings``). ``resp.ok`` alone would let any
    // non-``ok`` result pass as a full success and the ``sync_success``
    // toast would lie about a merge that never happened. Inspect the
    // body and surface the most severe per-result status with the
    // matching toast class. Severity order matches the user-facing
    // signal from the dedicated Settings panel:
    //
    //   error              → error toast with reason   (#799)
    //   aborted            → mtime_conflict warning    (#799)
    //   needs_confirmation → info partial + Open Settings action (#774)
    //   all ok / skipped   → sync_success
    let settingsSeverity = null;
    let settingsReason = '';
    if (!failed) {
      anyPhaseStarted = true;
      setPhase('settings', 'syncing');
      let settingsResp;
      try {
        settingsResp = await fetch(
          _ctxWithTargetScope('/api/context/settings/sync', {
            scopeId: syncAllScopeId,
            scopeResolved: true,
            targetScope: syncAllTier,
          }),
          { method: 'POST', headers },
        );
      } catch (err) {
        failed = { phase: 'settings', reason: err.message };
      }
      if (!failed) {
        if (!settingsResp.ok) {
          failed = {
            phase: 'settings',
            reason: await _ctxErrorMessageFromResponse(settingsResp, 'Settings sync failed'),
          };
        } else {
          const settingsData = await settingsResp.json().catch(() => ({}));
          const settingsResults = settingsData.results || [];
          const firstWithStatus = (s) => settingsResults.find(r => r && r.status === s);
          const errored = firstWithStatus('error');
          const aborted = firstWithStatus('aborted');
          const needsConfirmation = firstWithStatus('needs_confirmation');
          if (errored) {
            settingsSeverity = 'error';
            settingsReason = errored.reason || '';
          } else if (aborted) {
            settingsSeverity = 'aborted';
          } else if (needsConfirmation) {
            settingsSeverity = 'needs_confirmation';
          } else {
            settingsSeverity = 'ok';
          }
        }
      }
      // Reflect the settings outcome in its status row. ``error``/``aborted``
      // (and a transport/non-OK ``failed``) read as failed; ``needs_confirmation``
      // reads as ``attention`` so the row matches the "complete except Settings"
      // toast (the toast also carries the Open Settings action); ``ok``/
      // ``skipped`` read as done.
      if (failed && failed.phase === 'settings') {
        setPhase('settings', 'failed');
      } else if (settingsSeverity === 'error' || settingsSeverity === 'aborted') {
        setPhase('settings', 'failed');
      } else if (settingsSeverity === 'needs_confirmation') {
        setPhase('settings', 'attention');
      } else {
        setPhase('settings', 'done');
      }
    }
    // Any phase still pending was skipped because an earlier phase failed —
    // mark it not_run so the summary doesn't leave a frozen spinner/pending.
    for (const phase of _CTX_SYNC_PHASES) {
      if (phaseStates[phase].state === 'pending') setPhase(phase, 'not_run');
    }
    // Decide the final toast. Partial-success branches name the phases
    // that landed so the user can map the toast to what disk actually
    // changed; a bare "Sync failed: X" after a half-completed run is
    // the failure mode the issue calls out.
    if (failed) {
      if (succeeded.length === 0) {
        showToast(t('toast.sync_failed', { error: failed.reason }), 'error');
      } else {
        showToast(
          t('toast.sync_partial_failed', {
            succeeded: succeeded.map(_ctxSyncPhaseLabel).join(', '),
            failed_phase: _ctxSyncPhaseLabel(failed.phase),
            reason: failed.reason,
          }),
          'error',
        );
      }
    } else if (settingsSeverity === 'error') {
      showToast(t('toast.sync_failed', { error: settingsReason }), 'error');
    } else if (settingsSeverity === 'aborted') {
      showToast(t('settings.ctx.mtime_conflict'), 'warning');
    } else if (settingsSeverity === 'needs_confirmation') {
      showToast(
        t('toast.sync_partial_settings_needs_confirmation'),
        'info',
        {
          action: {
            label: t('toast.open_settings_action'),
            onClick: () => switchSettingsSection('hooks-sync'),
          },
        },
      );
    } else {
      showToast(t('settings.ctx.sync_success'));
    }
  } finally {
    // Restore the tier/project controls. ``loadCtxOverview`` below re-renders
    // fresh (enabled) controls anyway, but restore explicitly so an early
    // bail or a sequence-guarded overview skip can't leave them stuck.
    _ctxSetSyncControlsDisabled(false);
    // Refresh the overview whenever any phase actually fired — this
    // is the load-bearing line for #1074. A mid-run failure still
    // leaves disk in a new state, so the dashboard counts must
    // reflect what the next attempt would be diffing against. The
    // ``#ctx-sync-status`` summary lives outside ``#ctx-overview-content``,
    // so this reload leaves the per-phase result summary on screen.
    if (anyPhaseStarted) {
      loadCtxOverview();
    }
    btnLoading(btn, false);
  }
});

// Refresh button — re-fetches /api/context/overview to pick up freshly
// generated runtime artifacts. The label/handler/toast were previously
// named "detect" but the action has always been a refresh; the rename
// aligns the button id, i18n keys, and toast copy.
document.getElementById('ctx-refresh-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-refresh-btn');
  btnLoading(btn, true);
  try {
    await loadCtxOverview();
    showToast(t('toast.refresh_complete'));
  } finally { btnLoading(btn, false); }
});

// -- List (Skills / Commands / Agents) ----------------------------------------

// Sequence guard for in-flight ``loadCtxList`` races, mirroring
// ``_ctxOverviewSeq``. Rapid EN→KO→EN langchange fires re-issue
// ``loadCtxList`` while the previous fetch is still in flight; without a
// seq check the older response (or a late failure) lands after the newer
// render and clobbers it. Per-type because the three sections are
// independent — a stale ``skills`` response must not be voided by an
// ``agents`` toggle. The same seq is threaded into
// ``_loadScopeGroupItems`` (which writes ``container.innerHTML`` and the
// runtime-only banner) so its async writes are gated by the parent
// ``loadCtxList`` invocation that originated them.
let _ctxListSeq = { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 };

// Sibling guard for ``loadCtxDetail`` and ``_ctxLoadRuntimeOnlyDetail``
// races. Both write to the same ``detailEl``, so they share one
// per-type counter. Rapid langchange / card-click bursts can put
// multiple detail fetches in flight; the guard ensures only the newest
// response paints into the live DOM, both for the locale-stale window
// and the Edit-mode buffer-restore race (where an older fetch would
// otherwise overwrite a textarea that the listener just rehydrated).
let _ctxDetailSeq = { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 };

// Module-level pending Edit-mode buffer that needs to be restored
// after the next detail mount. Set when a langchange fires while the
// user has unsaved changes in the textarea; consumed (and cleared)
// when the latest detail mount's ``.then()`` runs against a freshly
// mounted DOM. Surviving across multiple back-to-back toggles is the
// whole point: if T1 captures the buffer and T2 fires before T1's
// detail fetch completes, T2 sees a wiped DOM (no editPane to capture
// from) but ``_ctxPendingEdit`` still carries T1's stash, and T2's
// own (now-latest) detail mount applies it.
//
// Lifetime is bounded by two clear sites — there is intentionally no
// stash-clear inside the langchange ``.then()`` supersede branch, so a
// rapid same-card retoggle hands the stash forward to the latest
// mount's consumer:
//
//   1. **Apply-success** — the latest detail mount's ``.then()`` paints
//      the buffer back into the new textarea, then clears.
//   2. **Navigation-drop** — ``loadCtxDetail`` /
//      ``_ctxLoadRuntimeOnlyDetail`` clear the stash at the top of any
//      mount that was NOT initiated by the langchange listener. This
//      catches the langchange-then-navigate orphan (P2 review): the
//      user toggles language while editing card A, navigates to card
//      B before T1's reload settles, and the stash would otherwise
//      survive forever. The langchange listener opts back in to
//      preservation via ``opts.preservePendingEdit: true``.
let _ctxPendingEdit = null;

// ``runtimeOnly`` disambiguates the two detail loaders: ``loadCtxDetail``
// fetches the canonical file, ``_ctxLoadRuntimeOnlyDetail`` (line ~1134)
// uses the diff endpoint as a preview source for items with no canonical.
// The langchange listener needs the flag to route a re-mount through the
// matching loader after ``loadCtxList`` wipes the list and detail panes —
// without it, a runtime-only detail open at toggle time would 404 into
// emptyState.
let _ctxCurrentDetail = { type: null, name: null, runtimeOnly: false };

// POSIX basename, JS-side. Used to keep absolute project_root paths out
// of the toast copy — the wire still carries the absolute path so the
// reverse-proxy / debug case stays self-describing.
function _ctxBasename(p) {
  if (!p) return '';
  return String(p).replace(/\/$/, '').split('/').pop() || String(p);
}

function _ctxScopeIsServerCwd(scope) {
  return scope && Array.isArray(scope.sources) && scope.sources.includes('server-cwd');
}

// A scope is "enrolled" when it carries a ``known_projects.json`` entry — the
// backend signals this by including ``known-projects`` in ``sources`` (there is
// no separate ``enrolled`` field; #1203 backend contract). Only an enrolled
// scope has a PATCH/DELETE-able registration, so rename / pause / unregister
// gate on this, and only an enrolled-and-enabled (or server-cwd) scope syncs.
function _ctxScopeIsEnrolled(scope) {
  return !!scope && Array.isArray(scope.sources) && scope.sources.includes('known-projects');
}

// Whether the per-project Sync button is allowed to fire. The backend computes
// ``sync_eligible`` (server-cwd OR enrolled-and-enabled) and the client trusts
// it when present. When the field is absent (older payloads / pre-#1203 test
// stubs) re-derive it from the SAME formula so gating still holds — server-cwd
// is always eligible, an enrolled scope is eligible unless explicitly paused.
function _ctxScopeSyncEligible(scope) {
  if (scope && typeof scope.sync_eligible === 'boolean') return scope.sync_eligible;
  if (_ctxScopeIsServerCwd(scope)) return true;
  return _ctxScopeIsEnrolled(scope) && scope.enabled !== false;
}

// Map a structured 409 write-guard ``detail`` to a readable, localized string.
// Backend #1210 returns ``detail = {reason_code, message, project_scope_id}`` on
// sync-ineligible writes; every other route returns a plain string ``detail``.
// Precedence: known reason_code → i18n key; else the backend's English
// ``message``; else a string detail as-is; else the caller's fallback.
function _ctxErrDetail(detail, fallback) {
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const rc = detail.reason_code;
    if (rc === 'sync_paused') return t('settings.ctx.error_sync_paused');
    if (rc === 'sync_not_enrolled') return t('settings.ctx.error_sync_not_enrolled');
    if (typeof detail.message === 'string') return detail.message;
  }
  return fallback;
}

// Sync All fans out over the ACTIVE scope's artifact types, so it must honor the
// same eligibility gate as the per-row matrix Sync button — otherwise an
// ineligible active project (paused / not enrolled) is still syncable via Sync
// All, since the sync routes accept any resolvable scope (#1203 review). Returns
// the i18n tooltip key when the active scope is excluded from sync, else '' (it
// is eligible, server-cwd, or there is no resolvable active scope).
function _ctxSyncAllIneligibleKey() {
  const active = (_ctxProjectsCache || []).find(_ctxScopeIsActive);
  if (!active || _ctxScopeSyncEligible(active)) return '';
  return _ctxScopeIsEnrolled(active)
    ? 'settings.ctx.sync_all_paused_tooltip'
    : 'settings.ctx.sync_all_not_enrolled_tooltip';
}

function _ctxScopeBadges(scope) {
  // Compact non-default-source flags rendered next to the scope label so the
  // user can tell at a glance why a scope appears (and whether it's missing).
  // Inline ``t()`` is sufficient — no ``data-i18n`` attribute, the i18n DOM
  // walker would otherwise re-translate and clobber the rendered text.
  const parts = [];
  if (scope.experimental) {
    const tip = t('settings.ctx.scope_experimental_tip');
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--experimental" title="${escapeHtml(tip)}">${escapeHtml(t('settings.ctx.scope_experimental'))}</span>`);
  }
  if (scope.missing) {
    parts.push(`<span class="ctx-scope-badge ctx-scope-badge--missing">${escapeHtml(t('settings.ctx.scope_missing'))}</span>`);
  }
  return parts.join('');
}

function _ctxScopeCount(scope, type) {
  return (scope.counts && scope.counts[type]) || 0;
}

function _ctxMissingCanonicalCommands(scope) {
  const base = 'mm context';
  const include = '--include=agents,commands,skills';
  if (scope === 'project_shared') {
    return [
      `${base} init ${include} --scope project_shared --confirm-project-shared`,
      `${base} sync ${include} --scope project_shared`,
    ];
  }
  if (scope === 'project_local') {
    return [
      `${base} init ${include} --scope project_local`,
      `${base} sync ${include} --scope project_local`,
    ];
  }
  return [
    `${base} init ${include} --scope user`,
    `${base} sync ${include} --scope user`,
  ];
}

function _ctxMissingCanonicalRemediationHtml(type, count, scannedDirs) {
  const scanList = (scannedDirs || []).join(', ') || `.${type}/`;
  const scope = _ctxTargetScope === 'user' || _ctxTargetScope === 'project_local'
    ? _ctxTargetScope
    : 'project_shared';
  const scopeForKey = scope === 'project_shared'
    ? 'project_shared'
    : (scope === 'project_local' ? 'project_local' : 'user');
  const title = t(`settings.ctx.missing_canonical_${scopeForKey}_title`);
  const body = t(`settings.ctx.missing_canonical_${scopeForKey}_body`)
    .replace('{count}', count)
    .replace(/\{type\}/g, type)
    .replace('{scan_dirs}', scanList);
  const commands = _ctxMissingCanonicalCommands(scope)
    .map(cmd => `<code>${escapeHtml(cmd)}</code>`)
    .join('');
  return `<div class="ctx-runtime-only-banner ctx-missing-canonical-remediation" role="status" data-tier="${escapeHtml(scope)}">
      <div class="ctx-missing-canonical-title">${escapeHtml(title)}</div>
      <div class="ctx-missing-canonical-body">${escapeHtml(body)}</div>
      <div class="ctx-missing-canonical-commands">${commands}</div>
    </div>`;
}

function _ctxItemMissingCanonical(item) {
  if (!item.canonical_path) return true;
  return (item.runtimes || []).some(r => _ctxStatusBucket(r.status) === 'missing_canonical');
}

function _ctxRenderItemsHtml(items, type, projectRoot, scannedDirs, { clickable }) {
  if (!items.length) {
    // Branch the hint on the active tier (#956): user canonical lives at
    // ``~/.memtomem/<type>`` and is shared across all projects, so the
    // project-tier copy ("within this project", import-from-scan-dirs)
    // is wrong on ``?target_scope=user``. ``_ctxTargetScope`` is the
    // canonical client-side read — same source used by sibling tier-aware
    // code (``_ctxRefreshSectionState``, ``langchange`` listener).
    // ``project_local`` stays on the project-tier key by design: issue
    // #956 explicitly scopes "preserve current project-tier wording".
    const isUser = _ctxTargetScope === 'user';
    const canonical = isUser ? `~/.memtomem/${type}` : `.memtomem/${type}`;
    const hintKey = isUser ? 'settings.ctx.empty_hint_user' : 'settings.ctx.empty_hint';
    let hint = t(hintKey)
      .replace(/\{type\}/g, type)
      .replace('{canonical}', canonical);
    if (!isUser) {
      // Same fallback as the runtime-only banner so the hint stays
      // grammatical when no scan dirs are reported (fresh project / no runtimes).
      const scanList = (scannedDirs || []).join(', ') || `.${type}/`;
      hint = hint.replace('{scan_dirs}', scanList);
    }
    return emptyState(
      '',
      t('settings.ctx.no_artifacts').replace('{type}', type),
      hint,
    );
  }
  const cardClass = clickable ? 'ctx-card' : 'ctx-card ctx-card--readonly';
  let html = '';
  for (const item of items) {
    // ``data-canonical-path`` is read by the click handler to choose between
    // the canonical detail GET (which 404s for runtime-only items, since the
    // wire endpoint only resolves canonical paths) and the runtime-only diff
    // path. Empty string when the item is runtime-only — readers test for
    // truthiness so the absence/empty distinction is irrelevant.
    const canonAttr = item.canonical_path
      ? ` data-canonical-path="${escapeHtml(item.canonical_path)}"`
      : ' data-canonical-path=""';
    // ``data-out-of-sync`` lets the list-click handler hint to
    // ``loadCtxDetail`` that the user should land on the Diff tab —
    // otherwise the canonical pane is the default and the user has
    // to click Diff to discover *what* is out of sync. Computed here
    // because the list response carries the per-runtime statuses;
    // ``loadCtxDetail`` would otherwise need a second fetch.
    const outOfSync = (item.runtimes || []).some(r => r.status === 'out of sync');
    // ``data-statuses`` is a deduped, space-separated bucket list used
    // by the deep-link filter applier (ADR-0009 §3) to decide whether a
    // card matches ``?filter=<status>``. Tokens mirror the dashboard's
    // count-field names (``out_of_sync`` / ``missing_target`` /
    // ``missing_canonical`` / ``parse_error`` / ``in_sync``); the
    // mapping lives in ``_ctxStatusBucket`` so renderer and filter
    // applier can't drift. Runtime-only items also include
    // ``missing_canonical`` since their ``canonical_path`` is empty —
    // the per-runtime status string already says so, but pinning it
    // explicitly makes the no-runtime edge case (some future server
    // payload with an empty ``runtimes`` list) still filterable.
    const buckets = new Set();
    for (const r of (item.runtimes || [])) {
      const b = _ctxStatusBucket(r.status);
      if (b) buckets.add(b);
    }
    if (!item.canonical_path) buckets.add('missing_canonical');
    const statusesAttr = ` data-statuses="${escapeHtml(Array.from(buckets).join(' '))}"`;
    const tierBadge = _tierBadgeHtml(item.target_scope, { isContextRow: true });
    // #1073 / PR #1088 review: clickable cards expose button semantics +
    // keyboard focus, AND the aria-label includes every distinct non-
    // ``in sync`` status across runtimes (plus runtime-only if the
    // canonical is absent). The previous label was just the name + an
    // ``out of sync`` suffix, which silently dropped ``missing target``
    // / ``missing canonical`` / ``parse error`` / runtime-only cards —
    // because ``aria-label`` overrides the visible runtime-badge
    // contents for screen readers, those users would tab past a card
    // with no clue why it needed action. Statuses come from
    // ``_ctxStatusText`` so the SR string matches the visible badge
    // text (and stays localized). Readonly cards (other-scope groups)
    // stay non-interactive and inherit ``ctx-card--readonly``.
    const a11yAttrs = clickable ? ' role="button" tabindex="0"' : '';
    let cardAriaLabel = '';
    if (clickable) {
      const statusSet = new Set();
      for (const r of (item.runtimes || [])) {
        if (r.status && r.status !== 'in sync') {
          statusSet.add(_ctxStatusText(r.status));
        }
      }
      // Mirrors the bucket fallback below: ``!canonical_path`` is the
      // wire signal that the artifact is runtime-only, even when no
      // per-runtime row carries the ``missing canonical`` status.
      if (!item.canonical_path) {
        statusSet.add(_ctxStatusText('missing canonical'));
      }
      const statusParts = Array.from(statusSet);
      const suffix = statusParts.length ? ` — ${statusParts.join(', ')}` : '';
      cardAriaLabel = ` aria-label="${escapeHtml(item.name + suffix)}"`;
    }
    html += `<div class="${cardClass}"${a11yAttrs}${cardAriaLabel} data-name="${escapeHtml(item.name)}"${canonAttr} data-out-of-sync="${outOfSync}"${statusesAttr}>
      <div class="ctx-card-header">
        <div>
          <div class="ctx-card-name">${escapeHtml(item.name)}${tierBadge}</div>
          ${item.canonical_path ? `<div class="ctx-card-path">${escapeHtml(item.canonical_path)}</div>` : '<div class="ctx-card-path text-muted">(runtime only)</div>'}
        </div>
        ${renderRuntimeBadges(item.runtimes)}
      </div>
    </div>`;
  }
  return html;
}

async function _loadScopeGroupItems(type, scope, container, seq) {
  panelLoading(container);
  try {
    const params = new URLSearchParams();
    if (_ctxTargetScope !== 'project_shared') params.set('target_scope', _ctxTargetScope);
    if (scope && scope.scope_id && !_ctxScopeIsServerCwd(scope)) {
      params.set('scope_id', scope.scope_id);
    }
    const query = params.toString();
    const res = await fetch(`/api/context/${type}${query ? `?${query}` : ''}`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Failed to load ${type}`);
    const data = await res.json();
    // Bail if a newer ``loadCtxList`` invocation has superseded this one
    // (rapid langchange / Refresh). The list — and this very container —
    // were rebuilt by the newer invocation; writing into the detached
    // container is harmless, but a late ``_ctxRefreshSectionState`` call
    // would still mutate the live ``settings-ctx-${type}`` dataset and
    // re-insert the runtime-only banner above the fresh list.
    if (seq !== _ctxListSeq[type]) return;
    const items = data[type] || [];
    // Cards on the active project scope are clickable across all tiers — the detail /
    // rendered / diff / edit / delete endpoints now accept ``target_scope=``
    // (#940 r3), so a click on a project_local draft opens the project_local
    // canonical, not a same-named project_shared one. Writes on non-shared
    // tiers are rejected at the server with HTTP 400 (the route's
    // ``_reject_non_shared_write`` helper); the JS surfaces those as
    // toasts via the existing ``err.detail`` path.
    const clickable = _ctxScopeIsActive(scope);
    container.innerHTML = _ctxRenderItemsHtml(
      items,
      type,
      scope.root,
      data.scanned_dirs || [],
      { clickable },
    );

    if (_ctxScopeIsActive(scope)) {
      // Only the active project is mutable, so its canonical/runtime split drives the
      // section-level Sync vs Import affordance gating. Expose the count via
      // a data attribute so CSS can flip primary/disabled states without a
      // classList toggle that risks drift across re-renders.
      _ctxRefreshSectionState(type, items, data.scanned_dirs || []);

      if (clickable) {
        const listEl = qs(`ctx-${type}-list`);
        container.querySelectorAll('.ctx-card').forEach(card => {
          card.addEventListener('click', () => {
            listEl.querySelectorAll('.ctx-card').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            // Runtime-only items have no canonical file; calling the GET detail
            // endpoint returns 404. Branch into the diff-backed renderer so the
            // user sees the actual runtime contents instead of a "not found".
            if (card.dataset.canonicalPath) {
              loadCtxDetail(type, card.dataset.name, {
                autoOpenDiff: card.dataset.outOfSync === 'true',
              });
            } else {
              const detailEl = qs(`ctx-${type}-detail`);
              _ctxLoadRuntimeOnlyDetail(type, card.dataset.name, detailEl);
            }
          });
          // #1073: keyboard activation parity with click. Card renders with
          // role=button + tabindex=0 in ``_ctxRenderItemsHtml``; without
          // this handler the SR announce ("button") would be a lie.
          card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              card.click();
            }
          });
        });
      }

      // ADR-0009 §3 deep-link applier. Runs only on the active project group —
      // the dashboard's tile counts roll up the active project's canonical /
      // runtime split, so non-active groups stay unfiltered.
      _ctxApplyDeepLinkToContainer(type, container);
    }
  } catch (err) {
    // Late-failing fetch from a previous invocation must not paint
    // ``emptyState`` over the fresh container the newer ``loadCtxList``
    // rebuilt — same false-overwrite class as the success path above.
    if (seq !== _ctxListSeq[type]) return;
    container.innerHTML = emptyState('', t('settings.ctx.load_failed', { type }), err.message);
  }
}

// Reflect the cwd canonical/runtime split onto the section so CSS can gate
// the primary action. Also (re)renders the runtime-only banner above the
// scope groups when items exist but none are canonical — the user landing
// on a fresh project shouldn't have to infer that Import is the next step.
function _ctxRefreshSectionState(type, cwdItems, scannedDirs) {
  const canonicalCount = cwdItems.filter(i => i.canonical_path).length;
  const sectionEl = document.getElementById(`settings-ctx-${type}`);
  if (sectionEl) {
    sectionEl.dataset.canonicalCount = String(
      _ctxTargetScope === 'project_local' ? 0 : canonicalCount,
    );
    if (_ctxTargetScope === 'project_local') {
      sectionEl.dataset.noFanout = 'true';
    } else {
      delete sectionEl.dataset.noFanout;
    }
  }

  const listEl = qs(`ctx-${type}-list`);
  if (!listEl) return;
  const existing = listEl.querySelector('.ctx-runtime-only-banner');
  if (existing) existing.remove();
  const missingCanonicalCount = cwdItems.filter(_ctxItemMissingCanonical).length;
  if (missingCanonicalCount > 0) {
    const banner = document.createElement('div');
    banner.innerHTML = _ctxMissingCanonicalRemediationHtml(
      type,
      missingCanonicalCount,
      scannedDirs,
    );
    const remediation = banner.firstElementChild;
    // Keep the tier-aware read-only banner (#943) at the very top of
    // the list — its copy explains *why* the Import button below is
    // dim, so a runtime-only "Click Import to canonicalize" prompt
    // landing above it would contradict the gate. Insert this banner
    // immediately AFTER the write-blocked banner when present;
    // otherwise fall back to the legacy first-child position.
    const writeBlocked = listEl.querySelector('.ctx-write-blocked-banner');
    const anchor = writeBlocked ? writeBlocked.nextSibling : listEl.firstChild;
    listEl.insertBefore(remediation, anchor);
  }
}

// ADR-0009 §3 — apply a ``?section=&filter=&artifact=`` deep-link to the
// freshly-rendered cwd container.
//
// * ``?artifact=<name>``: render-only mode. Cards whose ``data-name``
//   doesn't match are *removed from the DOM* (not just visually hidden)
//   so the negative pin test (the leaf does NOT render its full list)
//   is enforced by ``querySelectorAll('.ctx-card').length === 1``, not
//   by a CSS-visibility heuristic. Removal also keeps tab-order /
//   keyboard navigation consistent with the visible state.
// * ``?filter=<status>``: cards whose ``data-statuses`` doesn't include
//   the bucket get ``hidden`` set (display:none via the HTML attribute)
//   so a "Show all" reset can flip them back without re-fetching.
// * Either mode also scrolls to and pulses the first matching card so
//   the user sees the target without scanning.
//
// A small banner is inserted above the list explaining what's filtered
// and offering a clear-link. If no card matches the link target (deep
// link from a stale share-URL after the artifact was deleted), the
// banner says so and offers the same clear-link.
function _ctxApplyDeepLinkToContainer(type, container) {
  const link = _ctxParseDeepLink();
  if (!link) return;
  if (_ctxSectionToType(link.section) !== type) return;
  if (!link.filter && !link.artifact) return;

  const cards = Array.from(container.querySelectorAll('.ctx-card'));
  let matched = [];
  if (link.artifact) {
    matched = cards.filter(c => c.dataset.name === link.artifact);
    // Render-only: drop the non-matches outright. Negative pin (ADR-0009 §3)
    // — the test asserts the leaf doesn't merely *hide* the rest.
    for (const c of cards) if (c.dataset.name !== link.artifact) c.remove();
  } else if (link.filter) {
    matched = cards.filter(c => {
      const buckets = (c.dataset.statuses || '').split(/\s+/);
      return buckets.includes(link.filter);
    });
    // Hide (don't remove) so the "Show all" reset can re-reveal without
    // refetching. ``hidden`` attribute rather than a class so we don't
    // need a matching CSS rule.
    for (const c of cards) {
      if (matched.includes(c)) c.hidden = false;
      else c.hidden = true;
    }
  }

  _ctxRenderDeepLinkBanner(type, link, matched.length);

  if (matched.length > 0) {
    const target = matched[0];
    target.classList.add('ctx-card--highlight');
    // Scroll AFTER the highlight class is added so the smooth-scroll
    // animation lands on a card that visually stands out — flipping the
    // class first avoids a flash of plain card → highlighted card on
    // arrival. ``scrollIntoView`` with ``block: 'center'`` works in all
    // modern browsers; older fallback would be ``true``/``false``.
    try {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch {
      target.scrollIntoView();
    }
    // 2-second pulse, then remove. ADR-0009 §3 cites "~2 seconds" as
    // the highlight window; the CSS animation is keyframed so the
    // class-removal here just stops further pulsing rather than
    // interrupting a frame mid-flight.
    setTimeout(() => target.classList.remove('ctx-card--highlight'), 2000);
  }
}

function _ctxRenderDeepLinkBanner(type, link, matchCount) {
  const listEl = qs(`ctx-${type}-list`);
  if (!listEl) return;
  // Remove any prior banner first — re-renders (lang toggle, tier swap,
  // refresh) would otherwise stack banners.
  const existing = listEl.querySelector('.ctx-deep-link-banner');
  if (existing) existing.remove();

  let label = '';
  if (link.artifact) {
    label = matchCount > 0
      ? t('settings.ctx.deep_link_artifact').replace('{name}', link.artifact)
      : t('settings.ctx.deep_link_artifact_missing').replace('{name}', link.artifact);
  } else if (link.filter) {
    const filterLabel = t('settings.ctx.badge_' + link.filter);
    label = t('settings.ctx.deep_link_filter')
      .replace('{filter}', filterLabel)
      .replace('{count}', String(matchCount));
  } else {
    return;
  }

  const banner = document.createElement('div');
  banner.className = 'ctx-deep-link-banner';
  // Announce the filter/artifact narrowing to screen readers when it appears.
  banner.setAttribute('role', 'status');
  // ``textContent`` for the label so escaped artifact names (e.g. with
  // ``&`` / ``<``) round-trip cleanly without an explicit escapeHtml
  // call. The reset link is a separate element so it can be a button.
  const labelEl = document.createElement('span');
  labelEl.className = 'ctx-deep-link-banner-label';
  labelEl.textContent = label;
  const resetBtn = document.createElement('button');
  resetBtn.type = 'button';
  resetBtn.className = 'ctx-deep-link-banner-reset';
  resetBtn.textContent = t('settings.ctx.deep_link_reset');
  resetBtn.addEventListener('click', () => {
    _ctxClearDeepLink();
    loadCtxList(type);
  });
  banner.appendChild(labelEl);
  banner.appendChild(resetBtn);
  // Insert above the per-scope groups but below any tier-filter row so
  // the banner reads as a list-level state, not a per-scope label.
  // ``.ctx-runtime-only-banner`` is a sibling concern (no canonicals);
  // both banners can co-exist when a project has runtime-only items
  // *and* the user deep-linked into the type.
  const tierRow = listEl.querySelector('.ctx-tier-filter');
  if (tierRow && tierRow.nextSibling) {
    listEl.insertBefore(banner, tierRow.nextSibling);
  } else {
    listEl.insertBefore(banner, listEl.firstChild);
  }
}

async function loadCtxList(type) {
  const seq = ++_ctxListSeq[type];
  const listEl = qs(`ctx-${type}-list`);
  const detailEl = qs(`ctx-${type}-detail`);
  const statusEl = qs(`ctx-${type}-status`);
  if (detailEl) { detailEl.hidden = true; detailEl.innerHTML = ''; }
  if (statusEl) statusEl.innerHTML = '';
  panelLoading(listEl);
  _ctxCurrentDetail = { type: null, name: null, runtimeOnly: false };
  // Clear stale gating attribute so a failed reload doesn't keep the buttons
  // pinned to a previous canonical-count state. _ctxRefreshSectionState resets
  // it when the cwd group resolves successfully.
  const sectionEl = document.getElementById(`settings-ctx-${type}`);
  if (sectionEl) {
    delete sectionEl.dataset.canonicalCount;
    delete sectionEl.dataset.noFanout;
  }

  try {
    // Fetch then commit ONLY after the sequence guard, so a superseded
    // in-flight projects fetch can't clobber the shared cache / active scope
    // (#1194). The render below already gates on this same guard.
    const result = await _ctxFetchProjectsData();
    if (seq !== _ctxListSeq[type]) return;
    const data = _ctxCommitProjects(result);
    const scopes = data.scopes || [];
    if (!scopes.length) {
      // Should never happen — server cwd always present — but render
      // something instead of leaving the panel blank.
      listEl.innerHTML = emptyState('', t('settings.ctx.no_project_scopes'), '');
      return;
    }

    let html = _ctxProjectControls(type, scopes);
    html += _ctxTierControls(type);
    for (const scope of scopes) {
      const isActive = _ctxScopeIsActive(scope);
      const count = _ctxScopeCount(scope, type);
      const groupId = `ctx-${type}-group-${escapeHtml(scope.scope_id)}`;
      const removable = !_ctxScopeIsServerCwd(scope);
      // ``×`` is the visible glyph; ``aria-label`` carries the
      // disambiguating "Remove project {label} ({root})" so screen-reader
      // users hear which destructive control they're on, and ``title``
      // mirrors it for sighted hover. Two registrations sharing a
      // basename (``app`` x2) otherwise read identically. #1079
      const removeAria = t('settings.ctx.remove_project_aria')
        .replace('{label}', scope.label)
        .replace('{root}', scope.root || scope.scope_id);
      const removeBtn = removable
        ? `<button class="ctx-scope-remove" data-scope-id="${escapeHtml(scope.scope_id)}" aria-label="${escapeHtml(removeAria)}" title="${escapeHtml(removeAria)}">×</button>`
        : '';
      // Full root path on the summary's title attribute lets the user
      // disambiguate same-name scopes (``Edu/inflearn`` vs ``Work/inflearn``)
      // on hover without inflating the visible label.
      const rootTitle = scope.root ? `title="${escapeHtml(scope.root)}"` : '';
      html += `<details class="ctx-scope-group" data-scope-id="${escapeHtml(scope.scope_id)}" data-tier="${escapeHtml(scope.tier)}"${isActive ? ' open' : ''}>
        <summary class="ctx-scope-summary" ${rootTitle}>
          <span class="ctx-scope-summary-label">${escapeHtml(scope.label)}</span>
          <span class="ctx-scope-summary-count">${count}</span>
          ${_ctxScopeBadges(scope)}
          ${removeBtn}
        </summary>
        <div class="ctx-scope-items" id="${groupId}" data-loaded="false"></div>
      </details>`;
    }
    listEl.innerHTML = html;
    _ctxWireProjectControls();
    _ctxWireTierControls();

    // Tier-aware read-only banner (issue #943): inserted at the top of
    // the list whenever the canonical-tier filter is set to a
    // non-shared tier. Sits ABOVE the runtime-only banner that
    // ``_ctxRefreshSectionState`` may insert later — the write-block
    // state is the more important framing (it's why the user can't
    // press the section's write buttons), so it should read first.
    if (_ctxTargetScope !== 'project_shared') {
      const bannerKey = _ctxTargetScope === 'project_local'
        ? 'settings.ctx.write_blocked_project_local_banner'
        : 'settings.ctx.write_blocked_user_banner';
      const banner = document.createElement('div');
      banner.className = 'ctx-write-blocked-banner';
      // Announce to screen readers that writes are now blocked (and why) when
      // the tier flip injects this banner.
      banner.setAttribute('role', 'status');
      banner.dataset.tier = _ctxTargetScope;
      banner.textContent = t(bannerKey);
      listEl.insertBefore(banner, listEl.firstChild);
    }
    // Refresh write-blocked state on every list render so the section's
    // header buttons reflect the current tier filter; per-item Edit /
    // Delete buttons mounted later by ``loadCtxDetail`` re-trigger this
    // helper from their own mount path.
    _ctxRefreshWriteBlockedState();

    // Wire up: lazy fetch on toggle, immediate fetch for the open cwd group,
    // and the per-scope remove (×) button. ``seq`` is threaded into the
    // group fetch so a late group response from a stale ``loadCtxList``
    // can't paint into the new list's ``ctx-scope-items`` containers.
    for (const scope of scopes) {
      const groupEl = listEl.querySelector(`details[data-scope-id="${CSS.escape(scope.scope_id)}"]`);
      if (!groupEl) continue;
      const itemsEl = groupEl.querySelector('.ctx-scope-items');
      const fetchOnce = () => {
        if (itemsEl.dataset.loaded === 'true') return;
        itemsEl.dataset.loaded = 'true';
        _loadScopeGroupItems(type, scope, itemsEl, seq);
      };
      if (groupEl.open) fetchOnce();
      groupEl.addEventListener('toggle', () => { if (groupEl.open) fetchOnce(); });

      const removeBtn = groupEl.querySelector('.ctx-scope-remove');
      if (removeBtn) {
        removeBtn.addEventListener('click', async (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          const ok = await showConfirm({
            title: t('settings.ctx.remove_project'),
            // Include the full root path so the user can disambiguate
            // duplicate folder names (e.g. ``Edu/app`` vs ``Work/app``)
            // at the moment of confirmation — labels alone default to
            // the basename and Add Project doesn't expose a rename. #1078
            message: t('settings.ctx.confirm_remove_project')
              .replace('{label}', scope.label)
              .replace('{root}', scope.root || scope.scope_id),
            confirmText: t('settings.ctx.remove'),
          });
          if (!ok) return;
          try {
            const csrf = await ensureCsrfToken();
            const r = await fetch(`/api/context/known-projects/${encodeURIComponent(scope.scope_id)}`, {
              method: 'DELETE',
              headers: csrf ? { 'X-Memtomem-CSRF': csrf } : {},
            });
            if (!r.ok) {
              const err = await r.json().catch(() => ({}));
              showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
              return;
            }
            loadCtxList(type);
          } catch (err) {
            showToast(t('toast.delete_failed', { error: err.message }), 'error');
          }
        });
      }
    }
  } catch (err) {
    if (seq !== _ctxListSeq[type]) return;
    listEl.innerHTML = emptyState('', t('settings.ctx.load_failed', { type }), err.message);
  }
}

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
function _ctxStashDraft(key, content) {
  try { sessionStorage.setItem(key, content); } catch (_e) { /* quota / private mode */ }
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

function _ctxRenderConflictBanner(detailEl, userBuffer, freshContent) {
  // Inline diff inside the edit pane, above the textarea. Diff orientation
  // is on-disk → user-buffer so '+' lines are the user's edits and '-'
  // lines are what the user is about to overwrite — matches the "your
  // edits over what's there" mental model.
  const banner = detailEl.querySelector('.ctx-conflict-banner');
  if (!banner) return;
  const heading = `${escapeHtml(t('settings.ctx.conflict_your_edits'))} ↔ ${escapeHtml(t('settings.ctx.conflict_on_disk'))}`;
  const ops = diffLines(freshContent, userBuffer);
  banner.innerHTML = `<div class="text-muted" style="margin-bottom:6px;font-size:0.78rem">${heading}</div>`
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
      const r2 = await fetch(
        _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}`),
        {
          method: 'PUT',
          headers,
          body: JSON.stringify({ content: userBuffer, mtime_ns: staleMtimeNs, force: true }),
        },
      );
      if (!r2.ok) {
        const err = await r2.json().catch(() => ({}));
        showToast(err.detail || t('toast.request_failed'), 'error');
        return;
      }
      const result = await r2.json();
      if (result.name) {
        showToast(t('settings.ctx.conflict_force_done'), 'warning');
        detailEl.dataset.mtimeNs = result.mtime_ns || '';
        _ctxClearDraft(draftKey, type, name);
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

// -- Detail -------------------------------------------------------------------

// Detail meta header — issue #962. Renders description / scope / layout /
// mtime above the Canonical|Diff tab strip. Agents and commands also get
// a parsed-field chip row; skills are intentionally meta-only because the
// SKILL.md frontmatter has no analogous field set (and skill aux files
// already surface separately inside the canonical pane).
function _ctxRenderDetailMetaHeader(type, data) {
  const fields = data.fields || {};
  const scope = data.target_scope || '';
  const layout = data.layout || '';
  const fileCount = Array.isArray(data.files) ? data.files.length : 0;

  const rows = [];
  if (fields.description) {
    rows.push({
      label: t('settings.ctx.detail.meta_description'),
      value: fields.description,
    });
  }
  if (scope) {
    rows.push({
      label: t('settings.ctx.detail.meta_scope'),
      value: t(`settings.hooks.target_label_${scope}`) !== `settings.hooks.target_label_${scope}`
        ? t(`settings.hooks.target_label_${scope}`).replace(/:\s*$/, '')
        : scope,
    });
  }
  if (layout) {
    const layoutLabel = layout === 'flat'
      ? t('settings.ctx.detail.meta_layout_flat')
      : t('settings.ctx.detail.meta_layout_dir');
    let value = layoutLabel;
    if (layout === 'dir' && fileCount > 0) {
      value += ' · ' + t('settings.ctx.detail.meta_file_count', { count: fileCount });
    }
    rows.push({
      label: t('settings.ctx.detail.meta_layout'),
      value,
    });
  }
  if (data.mtime_ns) {
    // Convert BigInt-safe nanosecond epoch string to a millisecond Date.
    // ``Number(mtime_ns) / 1e6`` is safe — we only need timestamp precision
    // for human display, not equality.
    const ts = Number(data.mtime_ns) / 1e6;
    if (Number.isFinite(ts)) {
      rows.push({
        label: t('settings.ctx.detail.meta_last_synced'),
        value: new Date(ts).toLocaleString(),
      });
    }
  }

  let chipsHtml = '';
  if (type === 'agents') {
    const chips = [
      ['agent_role', fields.role],
      ['agent_isolation', fields.isolation],
      ['agent_kind', fields.kind],
      ['agent_temperature', fields.temperature],
    ];
    chipsHtml = _ctxRenderDetailChipsHtml(chips);
  } else if (type === 'commands') {
    const tools = Array.isArray(fields.allowed_tools)
      ? fields.allowed_tools.join(', ')
      : fields.allowed_tools;
    const chips = [
      ['command_argument_hint', fields.argument_hint],
      ['command_allowed_tools', tools],
      ['command_model', fields.model],
    ];
    chipsHtml = _ctxRenderDetailChipsHtml(chips);
  }

  if (!rows.length && !chipsHtml) return '';

  let html = '<div class="ctx-detail-meta">';
  for (const row of rows) {
    html += '<div class="ctx-detail-meta-row">';
    html += `<span class="ctx-detail-meta-label">${escapeHtml(row.label)}</span>`;
    html += `<span class="ctx-detail-meta-value">${escapeHtml(String(row.value))}</span>`;
    html += '</div>';
  }
  if (chipsHtml) {
    html += chipsHtml;
  }
  html += '</div>';
  return html;
}


function _ctxRenderDetailChipsHtml(specs) {
  // ``specs`` is an array of ``[i18n_suffix, value]`` pairs. Empty /
  // missing values are skipped so the chip row stays clean for
  // partially-populated frontmatter (e.g. an agent without an explicit
  // temperature setting must not render an empty "Temperature:" chip).
  const items = specs
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => {
      const label = t(`settings.ctx.detail.${key}`);
      return `<span class="ctx-detail-chip">`
        + `<span class="ctx-detail-chip-key">${escapeHtml(label)}</span>`
        + `<span class="ctx-detail-chip-value">${escapeHtml(String(value))}</span>`
        + `</span>`;
    });
  if (!items.length) return '';
  return `<div class="ctx-detail-chips">${items.join('')}</div>`;
}


async function loadCtxDetail(type, name, opts = {}) {
  // ``opts.autoOpenDiff`` (default false): when the list-click handler
  // sees an "out of sync" runtime on the card, it passes ``true`` here
  // so the detail view lands on the Diff tab pre-fetched, instead of
  // forcing the user to discover what's drifted by clicking Diff
  // themselves. Other call paths (post-save / post-delete reload at
  // line ~575/588) leave it false to preserve their canonical-pane
  // default.
  //
  // ``opts.preservePendingEdit`` (default false): only set by the
  // langchange listener, which IS the intended consumer of
  // ``_ctxPendingEdit``. Every other caller (card-click navigation,
  // save/delete post-mount reload, etc.) is a user-initiated change
  // of context — drop any pending stash here so a future remount of
  // the original card cannot resurrect a stale draft.
  if (!opts.preservePendingEdit) {
    _ctxPendingEdit = null;
  }
  const seq = ++_ctxDetailSeq[type];
  const autoOpenDiff = opts.autoOpenDiff === true;
  const detailEl = qs(`ctx-${type}-detail`);
  detailEl.hidden = false;
  _ctxCurrentDetail = { type, name, runtimeOnly: false };
  panelLoading(detailEl);

  try {
    const res = await fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}`),
    );
    if (res.status === 404) {
      if (seq !== _ctxDetailSeq[type]) return;
      detailEl.innerHTML = emptyState('', t('settings.ctx.not_found', { name }), t('settings.ctx.no_artifacts_hint'));
      return;
    }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Failed to load ${name}`);
    const data = await res.json();
    // Bail if a newer ``loadCtxDetail`` (or ``_ctxLoadRuntimeOnlyDetail``)
    // has superseded us — both share ``_ctxDetailSeq[type]`` because
    // they paint into the same detailEl. Without this, a slow first
    // toggle's response would overwrite a freshly-mounted second
    // toggle's render and silently drop any edit buffer the second
    // toggle's `.then()` had just rehydrated (review P2).
    if (seq !== _ctxDetailSeq[type]) return;

    let html = '<div class="ctx-detail">';
    html += `<div class="ctx-detail-header">
      <strong>${escapeHtml(name)}</strong>
      <div style="display:flex;gap:6px">
        <button class="btn-ghost ctx-detail-edit-btn" data-i18n="settings.ctx.edit">${t('settings.ctx.edit')}</button>
        <button class="btn-ghost ctx-detail-diff-btn" data-i18n="settings.ctx.diff_view">${t('settings.ctx.diff_view')}</button>
        <button class="btn-ghost btn-danger ctx-detail-delete-btn" data-i18n="settings.ctx.delete">${t('settings.ctx.delete')}</button>
      </div>
    </div>`;

    // Detail meta header (#962). Surfaces fields the backend already
    // exposes but the canonical pane buried inside the raw file content:
    // description (from frontmatter), scope tier, layout (flat/dir),
    // file count (dir layout), and parsed-field chips for agents and
    // commands. Skills get the meta only — no chip row.
    html += _ctxRenderDetailMetaHeader(type, data);

    // #1073: ARIA tablist — tabs are buttons, panes are tabpanels labelled
    // by the tab that controls them, and only the active tab is in the
    // focus order (others tabindex=-1, arrow keys move focus). Mirrors
    // the main app's ``.tab-nav`` pattern in app.js.
    //
    // IDs are qualified by ``type`` (PR #1088 review): inactive sections
    // keep their detail DOM mounted, so ``ctx-tab-canonical`` /
    // ``ctx-pane-canonical`` would collide across skills/commands/agents.
    // ``aria-controls`` and ``aria-labelledby`` resolve via document-level
    // ``getElementById`` regardless of the surrounding DOM tree, so an
    // un-qualified ID would point at a hidden earlier section's pane
    // instead of the active one's. (The pre-existing ``ctx-pane-edit``
    // duplicate is functionally invisible because its lookups are all
    // detailEl-scoped — only the new ARIA refs needed qualifying.)
    html += '<div class="ctx-detail-tabs" role="tablist">';
    html += `<button type="button" class="ctx-detail-tab active" data-pane="canonical" role="tab" id="ctx-tab-${type}-canonical" aria-controls="ctx-pane-${type}-canonical" aria-selected="true" tabindex="0">${t('settings.ctx.canonical_source')}</button>`;
    html += `<button type="button" class="ctx-detail-tab" data-pane="diff" role="tab" id="ctx-tab-${type}-diff" aria-controls="ctx-pane-${type}-diff" aria-selected="false" tabindex="-1">${t('settings.ctx.diff_view')}</button>`;
    html += '</div>';

    html += `<div class="ctx-detail-pane active" id="ctx-pane-${type}-canonical" role="tabpanel" aria-labelledby="ctx-tab-${type}-canonical">`;
    html += `<pre class="ctx-content-pre">${escapeHtml(data.content || '')}</pre>`;
    if (data.files && data.files.length) {
      html += `<div style="margin-top:8px"><strong>${t('settings.ctx.auxiliary_files')}</strong>`;
      for (const f of data.files) {
        html += `<div class="text-muted" style="font-size:0.78rem">${escapeHtml(f.path)} (${f.size} bytes)</div>`;
      }
      html += '</div>';
    }
    html += '</div>';

    html += `<div class="ctx-detail-pane" id="ctx-pane-${type}-diff" role="tabpanel" aria-labelledby="ctx-tab-${type}-diff"><div class="text-muted">${escapeHtml(t('settings.ctx.diff_tab_hint'))}</div></div>`;

    // ``ctx-conflict-banner`` stays hidden in the normal edit flow. When a
    // 409 reaches the dialog and the user picks "Open diff editor", we
    // render the user-buffer-vs-on-disk diff into this banner above the
    // textarea so they can hand-merge with both sides visible (issue #763).
    html += `<div id="ctx-pane-edit" hidden>
      <div class="ctx-conflict-banner" hidden></div>
      <textarea class="ctx-edit-area" id="ctx-edit-content">${escapeHtml(data.content || '')}</textarea>
      <div class="ctx-edit-actions">
        <button class="btn-ghost ctx-edit-cancel">${t('settings.ctx.cancel')}</button>
        <button class="btn-primary ctx-edit-save">${t('settings.ctx.save')}</button>
      </div>
    </div>`;

    html += '</div>';
    detailEl.innerHTML = html;
    // mtime_ns is a string (JS Number can't safely represent ns epochs).
    detailEl.dataset.mtimeNs = data.mtime_ns || '';
    // Pin the conflict-draft key for this editor session. The effective scope
    // can shift while the editor is open (a transient projects-fetch outage
    // that preserved the selection recovers, #1102), so every stash/restore/
    // clear below keys off this captured value instead of recomputing it —
    // otherwise a draft stashed during the outage would be cleared under the
    // recovered project's key and orphaned.
    detailEl.dataset.draftKey = _ctxStashKey(type, name);

    // Draft restore (issue #763): if the user closed a conflict modal
    // without resolving (Escape / backdrop / tab close-and-reopen) their
    // unsaved buffer is in sessionStorage. Rehydrate the textarea, open
    // the edit pane, and toast so they know we kept their work.
    const stashed = _ctxRestoreDraft(detailEl.dataset.draftKey, type, name);
    if (stashed != null) {
      const ta = detailEl.querySelector('#ctx-edit-content');
      if (ta) ta.value = stashed;
      const canonPane = detailEl.querySelector(`#ctx-pane-${type}-canonical`);
      const editPane = detailEl.querySelector('#ctx-pane-edit');
      if (canonPane) canonPane.hidden = true;
      if (editPane) editPane.hidden = false;
      detailEl.querySelectorAll('.ctx-detail-tab').forEach(tab => { tab.style.display = 'none'; });
      showToast(t('settings.ctx.conflict_draft_restored'), 'info');
    }

    // Tab switching — click + keyboard. ARIA state (aria-selected,
    // tabindex roving) tracks the visual ``.active`` class so the screen
    // reader announces the right tab and only one tab is in the focus
    // order at a time (#1073). Mirrors app.js's main ``.tab-nav``.
    const _activateCtxDetailTab = (tab, opts = {}) => {
      const tabs = Array.from(detailEl.querySelectorAll('.ctx-detail-tab'));
      tabs.forEach(t => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
        t.setAttribute('tabindex', '-1');
      });
      detailEl.querySelectorAll('.ctx-detail-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      tab.setAttribute('tabindex', '0');
      if (opts.focus) tab.focus();
      const pane = detailEl.querySelector(`#ctx-pane-${type}-${tab.dataset.pane}`);
      if (pane) pane.classList.add('active');
      if (tab.dataset.pane === 'diff') _ctxLoadDiff(type, name, detailEl);
    };
    detailEl.querySelectorAll('.ctx-detail-tab').forEach(tab => {
      tab.addEventListener('click', () => _activateCtxDetailTab(tab));
    });
    const _ctxTabsContainer = detailEl.querySelector('.ctx-detail-tabs');
    if (_ctxTabsContainer) {
      _ctxTabsContainer.addEventListener('keydown', (e) => {
        if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
        const tabs = Array.from(_ctxTabsContainer.querySelectorAll('.ctx-detail-tab'));
        const currentIdx = tabs.indexOf(document.activeElement);
        const nextIdx = _arrowNavIndex(tabs.length, currentIdx === -1 ? 0 : currentIdx, e.key);
        if (nextIdx < 0) return;
        e.preventDefault();
        _activateCtxDetailTab(tabs[nextIdx], { focus: true });
      });
    }

    // Out-of-sync prefetch + tab activation. Uses a synthetic ``click()``
    // on the Diff tab so the same handler above runs — keeps the active
    // class + pane wiring in one place. ``click()`` is sync but the
    // delegated ``_ctxLoadDiff`` is async; that's fine, we don't await.
    if (autoOpenDiff) {
      const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
      if (diffTab) diffTab.click();
    }

    // Edit
    detailEl.querySelector('.ctx-detail-edit-btn')?.addEventListener('click', () => {
      const canonPane = detailEl.querySelector(`#ctx-pane-${type}-canonical`);
      const editPane = detailEl.querySelector('#ctx-pane-edit');
      if (canonPane) canonPane.hidden = true;
      if (editPane) editPane.hidden = false;
      detailEl.querySelectorAll('.ctx-detail-tab').forEach(t => t.style.display = 'none');
    });

    // Cancel edit
    detailEl.querySelector('.ctx-edit-cancel')?.addEventListener('click', () => {
      const canonPane = detailEl.querySelector(`#ctx-pane-${type}-canonical`);
      const editPane = detailEl.querySelector('#ctx-pane-edit');
      if (canonPane) canonPane.hidden = false;
      if (editPane) editPane.hidden = true;
      detailEl.querySelectorAll('.ctx-detail-tab').forEach(t => t.style.display = '');
      // Cancel resolves any pending conflict-diff state by discarding the
      // user's buffer — same intent as "Reload" in the dialog. Clear both
      // the inline banner and the sessionStorage stash so a later detail
      // mount doesn't re-restore a draft the user just walked away from.
      const banner = detailEl.querySelector('.ctx-conflict-banner');
      if (banner) { banner.hidden = true; banner.innerHTML = ''; }
      _ctxClearDraft(detailEl.dataset.draftKey, type, name);
    });

    // Save (issue #763: 409 opens conflict dialog instead of silent reload).
    detailEl.querySelector('.ctx-edit-save')?.addEventListener('click', async () => {
      const btn = detailEl.querySelector('.ctx-edit-save');
      const content = detailEl.querySelector('#ctx-edit-content').value;
      const mtime_ns = detailEl.dataset.mtimeNs || '';
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const r = await fetch(
          _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}`),
          {
            method: 'PUT',
            headers,
            body: JSON.stringify({ content, mtime_ns }),
          },
        );
        if (r.status === 409) {
          await _ctxHandleConflict(type, name, content, mtime_ns, detailEl);
          return;
        }
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        const result = await r.json();
        if (result.name) {
          showToast(t('settings.ctx.save_success').replace('{name}', name));
          detailEl.dataset.mtimeNs = result.mtime_ns || '';
          // Clear any stashed draft now that the buffer is durable on disk.
          _ctxClearDraft(detailEl.dataset.draftKey, type, name);
          loadCtxDetail(type, name);
        }
      } catch (err) {
        showToast(t('toast.save_failed', { error: err.message }), 'error');
      } finally { btnLoading(btn, false); }
    });

    // Diff button
    detailEl.querySelector('.ctx-detail-diff-btn')?.addEventListener('click', () => {
      const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
      if (diffTab) diffTab.click();
    });

    // Delete
    detailEl.querySelector('.ctx-detail-delete-btn')?.addEventListener('click', async () => {
      // Cascade is opt-in: the canonical artifact is the ``.memtomem/``
      // entry, runtime files are mirrored copies. Default-off keeps the
      // dialog conservative — a stray click only removes the canonical,
      // and the user has to consciously check the box to fan-out delete
      // into ``~/.claude/skills/``, ``~/.codex/...``, etc.
      //
      // The cascade fan-out writes the project runtime, which the backend 409s
      // for a sync-ineligible (paused / not-enrolled) project (#1210). A plain
      // canonical-only delete (cascade=false) stays UNgated, so offer the
      // cascade checkbox ONLY when the active scope is sync-eligible — and gate
      // the option, NOT the whole Delete button, so a canonical delete the
      // backend allows still works. Computed at click time so a mid-session
      // pause/resume is reflected. When ineligible we hide the option and note
      // that only the canonical copy is removed. The §2a 409 handler below is
      // the safety net if eligibility flips between this click and the request.
      const _delScope = (_ctxProjectsCache || []).find(_ctxScopeIsActive);
      const _cascadeOffered = !_delScope || _ctxScopeSyncEligible(_delScope);
      const confirmOpts = {
        title: t('settings.ctx.confirm_delete').replace('{name}', name),
        message: _cascadeOffered
          ? t('settings.ctx.confirm_delete_msg')
          : `${t('settings.ctx.confirm_delete_msg')} ${t('settings.ctx.cascade_unavailable_hint')}`,
        confirmText: t('settings.ctx.delete'),
      };
      if (_cascadeOffered) {
        confirmOpts.extraOption = {
          id: 'cascade',
          label: t('settings.ctx.cascade_delete'),
          defaultChecked: false,
        };
      }
      const result = await showConfirm(confirmOpts);
      // ``showConfirm`` resolves to a boolean without ``extraOption`` and to
      // ``{ok, extras}`` with it — normalize both shapes.
      const ok = (result && typeof result === 'object') ? result.ok : !!result;
      if (!ok) return;
      const cascade = !!(result && typeof result === 'object'
        && result.extras && result.extras.cascade);
      try {
        const csrf = await ensureCsrfToken();
        const r = await fetch(
          _ctxWithTargetScope(
            `/api/context/${type}/${encodeURIComponent(name)}?cascade=${cascade}`,
          ),
          { method: 'DELETE', headers: csrf ? { 'X-Memtomem-CSRF': csrf } : {} },
        );
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        const data = await r.json();
        if (data.deleted) {
          showToast(t('settings.ctx.delete_success').replace('{name}', name));
          detailEl.hidden = true;
          loadCtxList(type);
        }
      } catch (err) {
        showToast(t('toast.delete_failed', { error: err.message }), 'error');
      }
    });

    // Per-item Edit / Delete buttons just landed in ``detailEl``; mirror
    // the section-level gate so they pick up the tier filter without
    // requiring a list re-render (#943).
    _ctxRefreshWriteBlockedState();

  } catch (err) {
    if (seq !== _ctxDetailSeq[type]) return;
    detailEl.innerHTML = emptyState('', t('settings.ctx.load_detail_failed'), err.message);
  }
}

// Surfaces (agents, commands) whose ``/rendered`` endpoint emits a
// ``field_map`` matrix. Skills don't produce per-runtime field drops,
// so a matrix would be uniformly green and not worth the row.
const _CTX_FIELD_MAP_TYPES = new Set(['agents', 'commands']);

function _ctxRenderFieldMapHtml(fieldMap, runtimes) {
  // ``fieldMap[field][runtime] = bool`` (True = kept). ``runtimes`` is the
  // ordered list of runtime keys from the same response so column order
  // matches the runtime sections rendered below.
  if (!fieldMap || !runtimes || !runtimes.length) return '';
  const fields = Object.keys(fieldMap);
  if (!fields.length) return '';
  const heading = escapeHtml(t('settings.ctx.field_map'));
  let html = `<table class="ctx-field-map" aria-label="${heading}">`;
  html += `<thead><tr><th scope="col">${heading}</th>`;
  for (const rt of runtimes) {
    html += `<th scope="col">${escapeHtml(rt)}</th>`;
  }
  html += '</tr></thead><tbody>';
  for (const f of fields) {
    html += `<tr><th scope="row">${escapeHtml(f)}</th>`;
    for (const rt of runtimes) {
      const kept = !!(fieldMap[f] && fieldMap[f][rt]);
      // ✓ for kept, em-dash for dropped — high-contrast, locale-stable
      // (no translation needed; the matrix headers carry the labels).
      html += `<td>${kept ? '✓' : '—'}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  return html;
}

async function _ctxFetchFieldMap(type, name) {
  // Fail-soft: a missing/invalid /rendered response should not break the
  // diff pane. The diff fetch is the user-facing source of truth here;
  // the field map is supplementary.
  try {
    const res = await fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/rendered`),
    );
    if (!res.ok) return null;
    const data = await res.json();
    if (!data || !data.field_map) return null;
    const runtimes = (data.runtimes || []).map(rt => rt.runtime);
    return { fieldMap: data.field_map, runtimes };
  } catch (_err) {
    return null;
  }
}

async function _ctxLoadDiff(type, name, detailEl) {
  const pane = detailEl.querySelector(`#ctx-pane-${type}-diff`);
  if (!pane) return;
  pane.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    // Diff is required, field map is optional + parallel-fetched. ``Promise.all``
    // would fail the whole pane on a /rendered hiccup; the explicit
    // fail-soft inside ``_ctxFetchFieldMap`` is what we want here.
    const diffPromise = fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/diff`),
    );
    const fieldMapPromise = _CTX_FIELD_MAP_TYPES.has(type)
      ? _ctxFetchFieldMap(type, name)
      : Promise.resolve(null);
    const res = await diffPromise;
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || t('settings.ctx.diff_failed'));
    const data = await res.json();
    const fieldMapData = await fieldMapPromise;

    let html = '';
    if (fieldMapData) {
      html += _ctxRenderFieldMapHtml(fieldMapData.fieldMap, fieldMapData.runtimes);
    }
    if (!data.runtimes || !data.runtimes.length) {
      html += `<div class="text-muted">${escapeHtml(t('settings.ctx.no_runtime_targets'))}</div>`;
    } else {
      for (const rt of data.runtimes) {
        html += `<div style="margin-bottom:12px">`;
        html += `<strong>${escapeHtml(rt.runtime)}</strong> ${_ctxBadge(rt.status)}`;
        if (rt.dropped_fields && rt.dropped_fields.length) {
          html += `<div style="margin-top:4px">${renderDroppedChips(rt.dropped_fields)}</div>`;
        }
        if (rt.status === 'out of sync' && data.canonical_content != null && rt.runtime_content != null) {
          const ops = diffLines(data.canonical_content, rt.runtime_content);
          html += `<div class="diff-view" style="margin-top:6px">${renderDiff(ops)}</div>`;
        } else if (rt.runtime_content) {
          html += `<pre class="ctx-content-pre" style="margin-top:6px">${escapeHtml(rt.runtime_content)}</pre>`;
        }
        html += '</div>';
      }
    }
    pane.innerHTML = html;
  } catch (err) {
    pane.innerHTML = `<div class="text-muted">${escapeHtml(t('settings.ctx.diff_failed_detail', { error: err.message }))}</div>`;
  }
}

// Render a detail panel for runtime-only items (no canonical file yet). The
// canonical detail GET 404s for these by design; the diff endpoint already
// returns ``runtime_content`` for each runtime, so we reuse it as the
// preview source and surface an "Import all" CTA so the user can pull every
// runtime-only artifact in one click.
async function _ctxLoadRuntimeOnlyDetail(type, name, detailEl, opts = {}) {
  // Shares ``_ctxDetailSeq[type]`` with ``loadCtxDetail`` — both paint
  // into the same detailEl, so a runtime-only fetch racing against a
  // canonical fetch (or another runtime-only fetch) must obey the same
  // seq invariant. Review P2 specifically called out the runtime-only
  // path as having the same stale-response window.
  //
  // ``opts.preservePendingEdit`` mirrors the canonical sibling — see
  // ``loadCtxDetail`` for the rationale. A runtime-only mount is no
  // less of a navigation than a canonical one; orphan-drop must apply
  // to both paths or a langchange-then-navigate-to-runtime-only
  // sequence still leaks the stash.
  if (!opts.preservePendingEdit) {
    _ctxPendingEdit = null;
  }
  const seq = ++_ctxDetailSeq[type];
  detailEl.hidden = false;
  _ctxCurrentDetail = { type, name, runtimeOnly: true };
  panelLoading(detailEl);

  try {
    const res = await fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/diff`),
    );
    if (!res.ok) {
      throw new Error((await res.json().catch(() => ({}))).detail || `Failed to load ${name}`);
    }
    const data = await res.json();
    if (seq !== _ctxDetailSeq[type]) return;

    let html = '<div class="ctx-detail">';
    html += `<div class="ctx-detail-header">
      <strong>${escapeHtml(name)}</strong>
      ${_ctxBadge('missing canonical')}
    </div>`;
    html += `<div class="text-muted" style="margin:6px 0 12px">${t('settings.ctx.runtime_only_detail_hint')}</div>`;

    if (!data.runtimes || !data.runtimes.length) {
      html += `<div class="text-muted">${t('settings.ctx.no_artifacts_hint')}</div>`;
    } else {
      for (const rt of data.runtimes) {
        html += `<div style="margin-bottom:12px">`;
        html += `<strong>${escapeHtml(rt.runtime)}</strong> ${_ctxBadge(rt.status)}`;
        if (rt.runtime_content != null) {
          html += `<pre class="ctx-content-pre" style="margin-top:6px">${escapeHtml(rt.runtime_content)}</pre>`;
        }
        html += '</div>';
      }
    }

    // ``type`` is always the plural section name (skills/commands/agents);
    // strip the trailing ``s`` for the singular CTA copy. Both i18n strings
    // expose ``{type}`` so the same placeholder works across en/ko.
    const singular = type.endsWith('s') ? type.slice(0, -1) : type;
    html += `<div class="ctx-edit-actions" style="margin-top:12px">
      <button class="btn-primary ctx-runtime-only-import" data-type="${escapeHtml(type)}">
        ${t('settings.ctx.import_this').replace('{type}', singular)}
      </button>
    </div>`;

    html += '</div>';
    detailEl.innerHTML = html;

    detailEl.querySelector('.ctx-runtime-only-import')?.addEventListener('click', async () => {
      const btn = detailEl.querySelector('.ctx-runtime-only-import');
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const r = await fetch(
          _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/import`),
          {
            method: 'POST',
            headers,
            body: JSON.stringify({}),
          },
        );
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        const data = await r.json();
        if (data.imported && data.imported.length) {
          showToast(t('settings.ctx.import_success'));
        } else if (data.skipped && data.skipped.length) {
          // ``reason`` is backend English (e.g. "canonical exists"); the user
          // still needs to know the import didn't run, so we surface it as-is
          // rather than swallow it. Localizing every backend skip reason
          // would multiply i18n keys without changing behavior.
          showToast(data.skipped[0].reason || t('toast.request_failed'), 'warning');
        }
        loadCtxList(type);
      } catch (err) {
        showToast(t('toast.import_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    });
    // The ``ctx-runtime-only-import`` button also flows through the
    // single-item import route, which 400s on non-shared tiers; sweep
    // it now that it's in the DOM (#943).
    _ctxRefreshWriteBlockedState();
  } catch (err) {
    if (seq !== _ctxDetailSeq[type]) return;
    detailEl.innerHTML = emptyState('', t('settings.ctx.load_detail_failed'), err.message);
  }
}

// -- Sync / Import buttons (delegated) ----------------------------------------

document.querySelectorAll('.ctx-sync-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const type = btn.dataset.type;
    // Guard against pressing Sync when the cwd has no canonical artifacts —
    // the request would resolve to a `no_canonical_root` skip with an info
    // toast, but that arrives after a confirm dialog, which is the wrong
    // shape of feedback for "this button does nothing right now."
    const section = btn.closest('.settings-section');
    if (section?.dataset.canonicalCount === '0') {
      const message = section?.dataset.noFanout === 'true'
        ? t('settings.ctx.project_local_no_fanout_tooltip')
        : t('settings.ctx.sync_disabled_tooltip').replace('{type}', type);
      showToast(message, 'info');
      return;
    }
    const ok = await showConfirm({
      title: t('settings.ctx.sync'),
      message: t('settings.ctx.confirm_sync').replace('{type}', type),
      confirmText: t('settings.ctx.sync'),
    });
    if (!ok) return;
    btnLoading(btn, true);
    try {
      const csrf = await ensureCsrfToken();
      const headers = csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' };
      const r = await fetch(
        _ctxWithTargetScope(`/api/context/${type}/sync`),
        { method: 'POST', headers },
      );
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
        return;
      }
      const data = await r.json();
      const generated = data.generated || [];
      const dropped = data.dropped || [];
      const skipped = data.skipped || [];
      const emptyCanonical = generated.length === 0
        && skipped.some(s => s && s.reason_code === 'no_canonical_root');
      if (emptyCanonical) {
        const msg = t('settings.ctx.sync_empty_canonical')
          .replace('{type}', type)
          .replace('{canonical}', data.canonical_root || `.memtomem/${type}`);
        showToast(msg, 'info');
      } else if (dropped.length) {
        // commands/agents render dropped per-field omissions — keep the
        // existing warning so the user can investigate field-level loss.
        showToast(t('settings.ctx.sync_dropped')
          .replace('{count}', dropped.length), 'warning');
      } else {
        showToast(t('settings.ctx.sync_success'));
      }
      loadCtxList(type);
    } catch (err) {
      showToast(t('toast.sync_failed', { error: err.message }), 'error');
    } finally { btnLoading(btn, false); }
  });
});

document.querySelectorAll('.ctx-import-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const type = btn.dataset.type;
    // Overwrite is opt-in: the default skip-when-canonical-exists rule
    // protects user-maintained canonicals from a stray Import wiping
    // them out with a stale runtime copy. The checkbox lets the user
    // explicitly say "yes, the runtime is the source of truth this
    // round" — used after editing in-place in ``~/.claude/skills/``
    // and wanting to flow that back into ``.memtomem/``.
    const result = await showConfirm({
      title: t('settings.ctx.import'),
      message: t('settings.ctx.confirm_import').replace('{type}', type),
      confirmText: t('settings.ctx.import'),
      extraOption: {
        id: 'overwrite',
        label: t('settings.ctx.confirm_import_overwrite_label'),
        defaultChecked: false,
      },
    });
    if (!result || !result.ok) return;
    const overwrite = !!(result.extras && result.extras.overwrite);
    btnLoading(btn, true);
    try {
      const csrf = await ensureCsrfToken();
      const headers = csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' };
      const r = await fetch(_ctxWithTargetScope(`/api/context/${type}/import`), {
        method: 'POST',
        headers,
        body: JSON.stringify({ overwrite }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
        return;
      }
      const data = await r.json();
      const statusEl = qs(`ctx-${type}-status`);
      if (statusEl) statusEl.innerHTML = renderImportResult(data);
      const importedCount = data.imported?.length || 0;
      const skippedCount = data.skipped?.length || 0;
      if (importedCount === 0 && skippedCount === 0) {
        // Nothing in any scanned runtime dir — give the user the actual
        // paths we looked in so they can drop a SKILL.md / *.md / etc.
        // Render basename(project_root) so a long absolute path doesn't
        // crowd the toast; scanned_dirs already gives full orientation.
        const scanList = (data.scanned_dirs || []).join(', ') || '—';
        const rootLabel = _ctxBasename(data.project_root) || '.';
        const msg = t('settings.ctx.import_no_runtimes')
          .replace('{type}', type)
          .replace('{root}', rootLabel)
          .replace('{scan_dirs}', scanList);
        showToast(msg, 'info');
      } else if (importedCount + skippedCount > 0) {
        showToast(t('settings.ctx.import_result')
          .replace('{imported}', importedCount)
          .replace('{skipped}', skippedCount));
      } else {
        showToast(t('settings.ctx.import_success'));
      }
      loadCtxList(type);
    } catch (err) {
      showToast(t('toast.import_failed', { error: err.message }), 'error');
    } finally { btnLoading(btn, false); }
  });
});

// -- Create button (delegated) ------------------------------------------------

document.querySelectorAll('.ctx-create-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const type = btn.dataset.type;
    const listEl = qs(`ctx-${type}-list`);
    if (listEl.querySelector('.ctx-create-form')) return;
    const form = document.createElement('div');
    form.className = 'ctx-create-form';
    const contentPlaceholder = type === 'mcp-servers'
      ? '{\n  "command": "uvx",\n  "args": ["--from", "example", "example-server"]\n}'
      : t('settings.ctx.create_content_placeholder');
    form.innerHTML = `
      <label>${escapeHtml(t('settings.ctx.create_name_label'))}</label>
      <input type="text" class="ctx-create-name" placeholder="${escapeHtml(t('settings.ctx.create_name_placeholder', { type: type.slice(0, -1) }))}" style="width:100%" />
      <label style="margin-top:8px">${escapeHtml(t('settings.ctx.create_content_label'))}</label>
      <textarea class="ctx-edit-area ctx-create-content" rows="6" placeholder="${escapeHtml(contentPlaceholder)}"></textarea>
      <div class="ctx-edit-actions">
        <button class="btn-ghost ctx-create-cancel">${escapeHtml(t('settings.ctx.cancel'))}</button>
        <button class="btn-primary ctx-create-submit">${escapeHtml(t('settings.ctx.create'))}</button>
      </div>`;
    listEl.prepend(form);

    form.querySelector('.ctx-create-cancel').addEventListener('click', () => form.remove());
    form.querySelector('.ctx-create-submit').addEventListener('click', async () => {
      const nameInput = form.querySelector('.ctx-create-name').value.trim();
      const content = form.querySelector('.ctx-create-content').value;
      if (!nameInput) { showToast(t('toast.name_required'), 'error'); return; }
      const submitBtn = form.querySelector('.ctx-create-submit');
      btnLoading(submitBtn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const r = await fetch(_ctxWithTargetScope(`/api/context/${type}`), {
          method: 'POST',
          headers,
          body: JSON.stringify({ name: nameInput, content }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        showToast(t('settings.ctx.create_success').replace('{name}', nameInput));
        form.remove();
        loadCtxList(type);
      } catch (err) {
        showToast(t('toast.create_failed', { error: err.message }), 'error');
      } finally { btnLoading(submitBtn, false); }
    });

    form.querySelector('.ctx-create-name').focus();
  });
});

// -- Add Project button (delegated) ------------------------------------------

document.querySelectorAll('.ctx-add-project-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const type = btn.dataset.type;
    // Defer to the shared folder picker (``path-picker.js``) instead of
    // ``window.prompt``: visual breadcrumb navigation, validation
    // against the server's ``/api/fs/list`` allow-list, and no
    // copy-paste path errors. ``PathPicker.open`` is async w.r.t.
    // the user; we hand it a callback that runs the POST when the
    // picker resolves a path. ``window.prompt`` fallback survives in
    // case ``path-picker.js`` failed to load (vendor cache miss, etc.)
    // — better than a non-functional Add Project button.
    const onSelect = async (root) => {
      if (!root) return;
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const r = await fetch('/api/context/known-projects', {
          method: 'POST',
          headers,
          body: JSON.stringify({ root }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        const data = await r.json();
        // Prefer ``warning_code`` so the toast is localized; fall back to
        // ``data.warning`` only when the server emitted a code this client
        // doesn't have a translation for yet. Plain ``data.warning`` would
        // ship English prose to KO users even though the route already
        // provides a stable code (#1077 follow-up to #962).
        const warningKey = data.warning_code
          ? `settings.ctx.add_project_warning_${data.warning_code}`
          : null;
        if (warningKey) {
          const localized = t(warningKey);
          // ``t()`` returns the key itself when no translation exists; in
          // that case fall back to the server prose rather than showing the
          // bare lookup key to the user.
          const message = localized === warningKey
            ? (data.warning || localized)
            : localized;
          showToast(message, 'warning');
        } else if (data.warning) {
          showToast(data.warning, 'warning');
        } else {
          showToast(t('settings.ctx.add_project_success'), 'success');
        }
        if (data.scope_id) {
          _ctxActiveScopeId = data.scope_id;
          try { localStorage.setItem(_CTX_ACTIVE_SCOPE_KEY, _ctxActiveScopeId); } catch {}
        }
        // The Portal board (ADR-0021 PR4) shares this Add Project button but has
        // no per-type ``loadCtxList`` — route it to its own loader.
        if (type === 'projects') {
          loadCtxProjects();
        } else {
          loadCtxList(type);
        }
      } catch (err) {
        showToast(t('toast.request_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    };
    if (window.PathPicker && typeof window.PathPicker.open === 'function') {
      window.PathPicker.open({ purpose: 'project', onSelect });
      return;
    }
    const raw = window.prompt(
      t('settings.ctx.add_project_prompt'),
      '',
    );
    if (!raw) return;
    const root = raw.trim();
    if (!root) return;
    onSelect(root);
  });
});

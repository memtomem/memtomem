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
  'invalid name':      'ctx-runtime-badge--error',
};
const _ctxStatusLabel = {
  'in sync':           'settings.ctx.status_in_sync',
  'out of sync':       'settings.ctx.status_out_of_sync',
  'missing target':    'settings.ctx.status_missing_target',
  'missing canonical': 'settings.ctx.status_missing_canonical',
  'parse error':       'settings.ctx.status_parse_error',
  'invalid name':      'settings.ctx.status_invalid_name',
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

// Localized display name for an artifact-type route slug (``skills`` /
// ``commands`` / ``agents`` / ``mcp-servers``). User-visible copy must never
// interpolate the raw slug — it produced "Fan out mcp-servers to all
// runtimes?" and English slugs inside Korean sentences. Element ids, URLs,
// dataset keys, and the ``.${type}/`` scan-dir fallback path keep the raw
// slug; only rendered prose routes through these helpers.
// NOTE: ``t()`` has no fallback-string second argument — a missing key
// returns the KEY itself — so the unknown-slug fallback is explicit here.
function _ctxTypeName(type) {
  const key = `settings.ctx.type_names.${type}`;
  const label = t(key);
  return label === key ? type : label;
}

// Singular variant for one-artifact copy ("Import this skill").
function _ctxTypeNameSingular(type) {
  const key = `settings.ctx.type_names_singular.${type}`;
  const label = t(key);
  return label === key ? type : label;
}

// Display label for a runtime key. Only ``gemini`` needs remapping — it is the
// Antigravity client (RUNTIME_TO_CLIENT: gemini→antigravity), so printing the
// raw key leaks an internal name the Projects portal already shows as
// "Antigravity". The on-disk .gemini/ marker paths keep the gemini key
// untouched — this maps the *label* only.
const _CTX_RUNTIME_LABEL = { gemini: 'Antigravity' };
function _ctxRuntimeLabel(name) {
  return _CTX_RUNTIME_LABEL[name] || name;
}

function renderRuntimeBadges(runtimes) {
  if (!runtimes || !runtimes.length) return '';
  return '<div class="ctx-runtime-badges">' +
    runtimes.map(r => {
      const short = r.runtime.replace(/_skills|_commands|_agents/g, '');
      // U7 (#1229): surface the server-sanitized diagnostic reason as a
      // tooltip on the list-card badge — the leaf pane carries the full
      // rendering; the card gets the at-a-glance cause.
      const title = r.reason ? `${r.runtime} — ${r.reason}` : r.runtime;
      return `<span class="ctx-runtime-badge ${_ctxStatusCls[r.status] || ''}" title="${escapeHtml(title)}">${escapeHtml(_ctxRuntimeLabel(short))}: ${escapeHtml(_ctxStatusText(r.status))}</span>`;
    }).join('') + '</div>';
}

// U4 (#1229): pre-confirm sync impact. Counts are per (runtime, name) FILE
// copies — the same semantics as the overview tile counts. 'missing
// canonical' / 'parse error' / 'invalid name' are NOT sync writes (sync
// never deletes or pulls), so they don't contribute.
function _ctxImpactRuntimeLabel(runtime) {
  // Generator names are ``<vendor>_<kind>`` (claude_skills, gemini_commands…)
  // except the MCP fan-out's ``project_mcp``, whose target is .mcp.json.
  if (runtime === 'project_mcp') return '.mcp.json';
  return _ctxRuntimeLabel(runtime.split('_', 1)[0]);
}

function _ctxSyncImpact(items) {
  const impact = { create: 0, overwrite: 0, runtimes: new Set() };
  for (const item of items || []) {
    for (const rt of item.runtimes || []) {
      if (rt.status === 'missing target') {
        impact.create += 1;
        impact.runtimes.add(_ctxImpactRuntimeLabel(rt.runtime));
      } else if (rt.status === 'out of sync') {
        impact.overwrite += 1;
        impact.runtimes.add(_ctxImpactRuntimeLabel(rt.runtime));
      }
    }
  }
  return impact;
}

function _ctxSyncImpactMessage(impact) {
  if (impact.create === 0 && impact.overwrite === 0) {
    return t('settings.ctx.confirm_sync_no_changes');
  }
  return t('settings.ctx.confirm_sync_impact', {
    create: impact.create,
    overwrite: impact.overwrite,
    runtimes: [...impact.runtimes].sort().join(', '),
  });
}

function _ctxSyncOverwriteWarning(impact) {
  return impact && impact.overwrite > 0
    ? t('settings.ctx.confirm_sync_overwrite_warning', { overwrite: impact.overwrite })
    : '';
}

// U7 (#1229): per-runtime diagnostic block under a parse-error /
// invalid-name badge — server-sanitized reason text plus a fix-it hint
// naming the canonical file. Empty for healthy rows and for rows whose
// payload predates the reason field (older cached responses).
function _ctxDiagnosticDetail(rt, canonicalPath) {
  const broken = rt.status === 'parse error' || rt.status === 'invalid name';
  if (!broken) return '';
  let html = '<div class="ctx-diagnostic-detail">';
  if (rt.reason) {
    html += `<div class="ctx-diagnostic-reason">${escapeHtml(rt.reason)}</div>`;
  }
  if (rt.status === 'parse error' && canonicalPath) {
    html += `<div class="ctx-diagnostic-hint">${escapeHtml(
      t('settings.ctx.parse_error_hint', { path: canonicalPath }),
    )}</div>`;
  }
  html += '</div>';
  return html === '<div class="ctx-diagnostic-detail"></div>' ? '' : html;
}

// ADR-0022 PR4: read-only ``production → v2`` label chips for a list card,
// fed by the ``?include=versions`` enrichment (``item.versions.labels``). Chips
// are informational only — freeze / promote / remove live in the detail panel
// (PR3), so there is intentionally NO interactive element inside a card that is
// itself ``role=button`` (avoids the nested-interactive trap). ``versionInfo``
// is the per-item ``versions`` summary object; absent (no enrichment) or empty
// labels render nothing. Server emits labels already alphabetically sorted, so
// ``Object.keys`` insertion order is stable across reloads.
function renderLabelChips(versionInfo) {
  const labels = versionInfo && versionInfo.labels;
  if (!labels) return '';
  const names = Object.keys(labels);
  if (!names.length) return '';
  const chips = names.map((name) => {
    const tag = labels[name];
    const tip = t('settings.ctx.versions.list_chip_tooltip', { label: name, tag });
    return `<span class="ctx-card-label-chip" data-label="${escapeHtml(name)}" title="${escapeHtml(tip)}">`
      + `<span class="ctx-card-label-name">${escapeHtml(name)}</span>`
      + `<span class="ctx-card-label-arrow" aria-hidden="true">→</span>`
      + `<span class="ctx-card-label-tag">${escapeHtml(tag)}</span>`
      + `</span>`;
  }).join('');
  return `<div class="ctx-card-labels">${chips}</div>`;
}

function renderImportResult(data) {
  let html = `<div class="ctx-import-result">`;
  // The cross-runtime priority rule only matters when the receipt actually
  // has rows — leading an empty "nothing imported" receipt with it read as
  // unexplained pipeline trivia (review 2026-06-10, U12).
  if (data.imported?.length || data.skipped?.length) {
    html += `<div class="ctx-import-priority">${t('settings.ctx.import_priority')}</div>`;
  }
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

// Per-surface AbortControllers (#1286). The seq guards already stop a superseded
// response from PAINTING; these additionally abort the wasted in-flight request
// the moment a scope/tier/section switch supersedes it, so the winning request
// isn't queued behind stale ones on the browser's per-origin connection pool and
// can't keep racing the cache-commit path. Seq guards stay as defense in depth —
// abort is best-effort (a runtime without ``AbortController`` degrades to
// seq-only). ``loadCtxList`` / ``loadCtxDetail`` key theirs by type (declared
// next to ``_ctxListSeq`` / ``_ctxDetailSeq``); overview is a singleton and the
// Projects portal owns its own (``_ctxProjectsAbort`` in context-portal.js).
let _ctxOverviewAbort = null;

// Abort the superseded request held in ``prev`` (no-op if absent / already
// settled) and mint a fresh controller for the new invocation. Returns the new
// controller, or null where ``AbortController`` is unavailable so callers fall
// back to seq-only guarding. Pair with the surface's ``++seq`` at loader entry.
function _ctxSwapAbort(prev) {
  try { prev?.abort(); } catch { /* already aborted / unsupported — seq guards */ }
  return typeof AbortController === 'function' ? new AbortController() : null;
}

// True for a fetch rejected by an ``AbortController`` (#1286). A superseded
// request is never a real failure, so its caller must skip the error render /
// toast / active-scope demotion and let the winning request own the surface.
function _ctxIsAbortError(err) {
  return !!err && (err.name === 'AbortError' || err.code === 20);
}

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
// rank 11: a Sync All run disables the shared header bar's controls for its
// duration. The lock lives here, not only in the DOM, because the bar is a
// single shared host that ``_ctxRenderControlBar`` repaints on section switches
// / loader resolves / langchange — re-applying the flag after each repaint keeps
// the controls disabled until the run's ``finally`` clears it (Codex review).
let _ctxSyncControlsLocked = false;

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

// Resolve a bare scope_id ('' === Server CWD) to its human label via the
// projects cache. Used by the Sync / Sync-All / Import confirm dialogs to name
// the project being acted on (rank-10 confirm threading). Falls back to the
// Server-CWD label when the id isn't in the cache (cold cache / Server CWD).
function _ctxScopeDisplayLabelById(scopeId) {
  const scope = (_ctxProjectsCache || []).find(
    s => (s.scope_id || '') === (scopeId || ''),
  );
  return scope ? _ctxScopeDisplayLabel(scope) : t('settings.ctx.server_cwd');
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

// The synthetic single-project fallback returned when ``/api/context/projects``
// is unavailable (older deploy / browser test) or the fetch was aborted (#1286).
// A safe default that never reports ``stale`` — so a fetch outage can't desync
// the rendered shape from a real response or fire a spurious "Initialize"
// prompt — and matches the API scope shape (``stale`` + the full four-key counts
// dict, ADR-0021 PR2).
function _ctxServerCwdFallback() {
  return {
    scopes: [{
      scope_id: '',
      label: t('settings.ctx.server_cwd'),
      root: '',
      tier: 'project',
      sources: ['server-cwd'],
      missing: false,
      stale: false,
      experimental: false,
      counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
    }],
  };
}

// Fetch ``/api/context/projects`` and classify the outcome, WITHOUT mutating
// any module global. Returns ``{ data, warn, authoritative }``:
//   - ``data``  always a ``{scopes: [...]}`` object — the real scopes on
//               success, or the synthetic Server-CWD fallback on any failure.
//   - ``warn``  null on success / silent-404 / network throw, else
//               ``{kind, status?, detail}`` describing a "loud" (toastable)
//               failure shape.
//   - ``authoritative``  whether the outcome says something definitive about
//               the project roster — true on success and on a same-origin 404
//               (endpoint genuinely absent / older deploy), false when no
//               response was received at all (``fetch`` rejected: the server
//               became unreachable AFTER serving the page — restart, sleep-
//               wake, offline — i.e. the transient class of #1102, NOT
//               evidence the roster is gone). Gates normalization in
//               ``_ctxCommitProjects`` (#1247 id 20).
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
  // Five failure shapes need to stay distinguishable per #1080 / #1247 id 20:
  //   - 404                   → older deployment or absent endpoint; the
  //     legacy single-server-CWD fallback is the documented contract here,
  //     so stay silent to avoid noise on intentional-omit deployments.
  //   - network throw          → no response received; an absent endpoint on
  //     the origin that served this page resolves as a 404, so a rejection
  //     means the server became unreachable after page load — transient.
  //     Stays toast-silent like the 404 cell but is NOT authoritative about
  //     the roster, so it must not demote the persisted active scope.
  //   - 5xx (and non-404 4xx)  → endpoint exists but is failing; surface a
  //     non-blocking toast so a broken store doesn't masquerade as "no
  //     registered projects".
  //   - 200 with malformed JSON → endpoint reachable, response unreadable;
  //     same "endpoint exists but failing" class, surface a toast.
  //   - 200 with unexpected shape → parses cleanly but isn't {scopes: Array};
  //     same "endpoint exists but failing" class, surface a toast (#1100).
  let warn = null;
  // Flipped the moment ``fetch`` resolves — distinguishes "server answered
  // (with anything)" from "no response at all" in the catch-all below.
  let sawResponse = false;
  try {
    // ``include`` tokens are opt-in server-side (ADR-0021 PR2). ``counts`` is
    // always requested — every caller renders the scope picker's per-scope
    // count badges. The ``opts.includeCoverage`` token (``runtime_coverage``,
    // an expensive ``probe_all_runtimes`` pass) is retained as a capability but
    // has NO caller since rank 2 removed its only consumer (the Project Scope
    // Matrix); leaving the gate means re-adding a coverage consumer is a
    // one-flag change. ``_ctxWithTargetScope`` appends ``&target_scope=``.
    const include = opts.includeCoverage ? 'counts,runtime_coverage' : 'counts';
    const res = await fetch(_ctxWithTargetScope(`/api/context/projects?include=${include}`, { includeScope: false, targetScope: opts.targetScope }), { signal: opts.signal });
    sawResponse = true;
    // Superseded after the request was issued but before its body was read
    // (#1286): skip the parse + classification entirely and return the benign
    // class. A real browser rejects ``fetch`` on abort (handled in the catch);
    // this covers the narrow window where the response resolves first, keeping a
    // superseded fetch from racing the cache-commit path.
    if (opts.signal?.aborted) {
      return { data: _ctxServerCwdFallback(), warn: null, authoritative: false, aborted: true };
    }
    if (!res.ok) {
      const detail = _ctxErrDetail((await res.json().catch(() => ({}))).detail, `HTTP ${res.status}`);
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
    // An aborted fetch (superseded scope/tier/section switch — #1286) is NOT a
    // real failure: it says nothing about the project roster. Force it into the
    // benign, non-authoritative class (warn cleared, authoritative=false) so a
    // late abort during the body read can't be misclassified by the parse/shape
    // branches above as a loud failure (toast + active-scope demotion) — the
    // same demote-the-persisted-selection hazard #1247 id 20 closed for network
    // throws. The caller's seq guard discards this anyway; this also protects
    // the unguarded legacy/portal commit paths.
    if (_ctxIsAbortError(_err) || opts.signal?.aborted) {
      return { data: _ctxServerCwdFallback(), warn: null, authoritative: false, aborted: true };
    }
    // Browser tests and older deployments may not provide the multi-project
    // discovery endpoint. Preserve the legacy single-project behavior by
    // falling back to an implicit server-CWD scope; downstream requests omit
    // scope_id for server-CWD because it is the route default.
    data = _ctxServerCwdFallback();
  }
  return { data, warn, authoritative: sawResponse, aborted: false };
}

// Commit a ``_ctxFetchProjectsData`` result into the shared module state. Split
// from the fetch (#1194) so each caller commits ONLY after its own
// sequence/scope guard passes — a late, superseded fetch must not overwrite a
// newer one's cache / active scope / toast memo. Carries the #1102
// normalize-only-when-authoritative gate and the #1101 failure-toast de-dup.
// Returns ``data`` for callers that render from it.
function _ctxCommitProjects({ data, warn, authoritative, aborted }) {
  // A superseded/aborted projects fetch (#1286) carries no roster information,
  // so it must not touch the shared cache, the active-scope normalization, or
  // the failure-toast memo. Guarded callers already skip commit via their seq
  // guard; this also hardens the unguarded legacy/portal commit paths.
  if (aborted) return data;
  _ctxProjectsCache = data.scopes || [];
  // Normalize only when the outcome is *authoritative* about the roster.
  // Four cases:
  //   - success (real scopes)          → normalize against the real list.
  //   - silent 404                     → endpoint absent / older deploy; the
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
  //   - network throw (no response)     → same transient class as the 5xx
  //     cell, just one layer lower; ``authoritative === false`` skips
  //     normalization so an outage blip can't persist a Server-CWD demotion
  //     that recovery would otherwise have restored (#1247 id 20). Stays
  //     toast-silent (``warn`` is null), matching the documented contract.
  // ``authoritative !== false`` (not ``=== true``) keeps legacy/direct
  // callers that pass only ``{data, warn}`` on the old normalize-on-silent
  // semantics.
  if (!warn && authoritative !== false) _ctxNormalizeActiveScope(_ctxProjectsCache);
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
    <span>${escapeHtml(t('settings.ctx.tier_filter'))}</span>
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
  + '.ctx-version-freeze-btn, .ctx-version-promote-btn, .ctx-version-label-remove'
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
  + '.ctx-detail-edit-btn, .ctx-detail-delete-btn, .ctx-runtime-only-import'
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
    const label = escapeHtml(_ctxRuntimeLabel(rt.name || ''));
    return `<span class="${cls}"${title} data-runtime="${name}">${label}</span>`;
  }).join('');

  // Issue #832 / ADR-0009 §1.c: surface freshness as "Canonical updated: 5m
  // ago" sourced from canonical-source mtime. Issue #1076 follow-up: the
  // label was previously "Last sync", which overstated the data source —
  // editing a canonical artifact without fan-out also bumps this value, so
  // users diagnosing "did this actually reach Claude/Codex?" trusted it too
  // much. The label is now data-source-accurate and the explanation is
  // attached via ``title=`` on the label span (the row-level ``title=``
  // still carries the absolute timestamp so the diagnose case keeps it on
  // hover — rendered in the viewer's locale/TZ, not the raw UTC ISO, which
  // read as a "wrong" date for non-UTC users; ``data-iso`` keeps the
  // machine-readable form). Suppress the line when the backend returns null
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
    const localAbs = escapeHtml(new Date(lastSyncedAt).toLocaleString());
    const labelTip = escapeHtml(t('settings.ctx.last_synced_tooltip'));
    lastSyncHtml = `<div class="ctx-overview-last-sync" title="${localAbs}">
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
      const invalidName = d.invalid_name || 0;
      const localDraft = d.local_draft || 0;
      const issueCount = missingTarget + missingCanonical + outOfSync + parseError + invalidName;
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
      } else if (invalidName > 0) {
        badgeText = `${invalidName} ${t('settings.ctx.badge_invalid_name')}`;
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
      // diagnose them. But per ADR-0009 §2's mixed-combination rule a
      // parse error must not SUPPRESS the other pointers (1 unparseable
      // file + 5 missing-target still needs its "Run Sync All" line), so
      // only the whole-call failure shape (``d.error``) gates the block.
      // Settings tile cannot produce ``missing_canonical`` by design
      // (ADR-0009 §2 last paragraph, ADR-0001 §5).
      const pointers = [];
      if (!d.error) {
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
        // ADR-0009 §4 empty-state teaching copy: a bare "0 / Empty" tile
        // told a first-run user nothing about how to get started — the ADR
        // explicitly rejected that rendering. Per-kind keys because the
        // leaves offer different verbs: hooks have no create/import button
        // (you adopt runtime hooks there), MCP servers have no Import.
        if (isEmpty) {
          const emptyKey = typ.key === 'settings'
            ? 'settings.ctx.pointer_empty_hooks'
            : (typ.key === 'mcp_servers'
                ? 'settings.ctx.pointer_empty_mcp'
                : 'settings.ctx.pointer_empty');
          pointers.push({
            action: 'leaf',
            text: t(emptyKey, { leaf: typ.label }),
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
      // Whole-call failure diagnostics (#762 error taxonomy): the route
      // ships a classified ``error_kind`` plus an ``error_message`` that
      // ``_redact_message`` already sanitized FOR DISPLAY (HOME collapsed,
      // secret-shapes replaced, ≤200 chars — context_gateway.py). Dropping
      // them left the user a bare "error" badge with nothing to act on.
      // Shape guard: artifact tiles emit boolean ``error: true``; the
      // settings tile emits ``status: 'error'`` either for a whole-call
      // failure (with kind/message) or for in-band per-file errors
      // (without — those are diagnosed on the hooks leaf, so no line here).
      const errorKind = (d.error === true || d.status === 'error') ? (d.error_kind || '') : '';
      const errorMessage = (d.error === true || d.status === 'error') ? (d.error_message || '') : '';
      let errorDetailHtml = '';
      let badgeTitleAttr = '';
      if (errorKind || errorMessage) {
        // Shared kind→label + message composer (B-1 #1284); the helper echoes
        // the raw kind for unknown kinds so future kinds stay visible.
        const detailText = _ctxKindDetailText(errorKind, errorMessage);
        badgeTitleAttr = ` title="${escapeHtml(detailText)}"`;
        errorDetailHtml =
          `<div class="ctx-overview-error-detail" title="${escapeHtml(detailText)}">`
          + `${escapeHtml(detailText)}</div>`;
      }
      const tileAriaLabel = `${badgeText} — ${typ.label}`
        + (errorKind || errorMessage ? ` — ${[errorKind, errorMessage].filter(Boolean).join(': ')}` : '');
      html += `<div class="ctx-overview-stat" data-section="${typ.section}" data-tile-key="${typ.key}">
        <button type="button" class="ctx-overview-stat-nav" aria-label="${escapeHtml(tileAriaLabel)}">
          <div class="ctx-overview-count">${total}</div>
          <div class="ctx-overview-label">${escapeHtml(typ.label)}</div>
          <div class="ctx-overview-badge"><span class="badge ${badgeCls}"${badgeTitleAttr}>${escapeHtml(badgeText)}</span></div>
          ${errorDetailHtml}
        </button>
        ${pointersHtml}
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;
    // rank 11: the active-project + tier controls live in the persistent gateway
    // header bar, not inside this section's content. Repaint the (shared) bar —
    // it self-sources the type from the active section, so a stale overview
    // render that resolves after the user navigated away repaints the bar for
    // their CURRENT section rather than hijacking it back to Overview.
    _ctxRenderControlBar();

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
      // ``settings`` participates in the all-empty gate too: its ``total``
      // counts applicable generators with a stored source present, so 0
      // means the settings phase would also be a pure no-op. Without it a
      // fully-empty project kept Sync All live and the run ended in a
      // "Sync completed" toast for a complete no-op.
      const settingsTotal = (data.settings || {}).total || 0;
      if (totals.total === 0 && settingsTotal === 0) {
        syncAllBtn.dataset.runtimeOnly = 'true';
        syncAllBtn.title = t('settings.ctx.sync_all_disabled_tooltip');
        syncAllBtn.setAttribute('aria-disabled', 'true');
      } else if (totals.total > 0 && totals.runtimeOnly === totals.total) {
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
}

async function loadCtxOverview() {
  const seq = ++_ctxOverviewSeq;
  // Abort the superseded overview load's in-flight fetches (#1286); the signal
  // threads through BOTH the projects sub-fetch and the overview fetch so a
  // scope/tier/section switch cancels the whole load, not just its render.
  _ctxOverviewAbort = _ctxSwapAbort(_ctxOverviewAbort);
  const signal = _ctxOverviewAbort?.signal;
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
    // No caller opts into runtime_coverage anymore: its only consumer was the
    // Project Scope Matrix, removed in rank 2. Overview is now a counts-only
    // aggregate dashboard, so it skips the expensive ``probe_all_runtimes`` pass.
    const projectsResult = await _ctxFetchProjectsData({
      targetScope: requestedTier,
      signal,
    });
    if (seq !== _ctxOverviewSeq || requestedTier !== _ctxTargetScope) return false;
    _ctxCommitProjects(projectsResult);
    // Pin the resolved effective scope alongside the tier so the overview
    // request can't re-resolve against a cache a later refresh might mutate
    // (ADR-0021 §C: pin BOTH scope and tier).
    const pinnedScopeId = _ctxEffectiveScopeId();
    const res = await fetch(_ctxWithTargetScope('/api/context/overview', {
      targetScope: requestedTier,
      scopeId: pinnedScopeId,
      scopeResolved: true,
    }), { signal });
    if (!res.ok) throw new Error(_ctxErrDetail((await res.json().catch(() => ({}))).detail, t('settings.ctx.load_overview_failed')));
    const data = await res.json();
    if (seq !== _ctxOverviewSeq || requestedTier !== _ctxTargetScope) return false;
    // Shape guard (sibling of the #1100 projects-fetch hardening): a bare-null
    // or non-object 200 would TypeError inside _renderCtxOverview; route it
    // through the failure path instead.
    if (data === null || typeof data !== 'object') throw new Error(t('settings.ctx.load_overview_failed'));
    _ctxOverviewCache = data;
    _renderCtxOverview(data);
    return true;
  } catch (err) {
    // An aborted fetch means a newer load superseded this one (#1286) — its
    // seq guard already owns the panel; never paint an error over it. Return
    // false so a caller (the Refresh button) doesn't report a success it didn't
    // actually complete (Codex: aborted refresh must not toast "Refresh complete").
    if (_ctxIsAbortError(err) || seq !== _ctxOverviewSeq || requestedTier !== _ctxTargetScope) return false;
    _ctxOverviewCache = null;
    el.innerHTML = emptyState('', t('settings.ctx.load_overview_failed'), err.message);
    // This invocation owned the (error) render — it ran to completion as the
    // latest, so it is NOT a supersede; the refresh affordance may settle.
    return true;
  }
}

async function _ctxSyncProjectScope(scopeId, btn) {
  // Pin BOTH scope and tier once, BEFORE the confirm (U4 #1229 moved this
  // above the dialog so the impact preview shares the pin). The card's scope
  // is usually NOT the active scope, so ``_ctxOverviewCache`` does not
  // apply — fetch the overview for the pinned (project, tier) best-effort;
  // overview has no per-runtime split, so the portal confirm is counts-only.
  // ``_ctxWithTargetScope`` otherwise re-reads the mutable
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

  btnLoading(btn, true);
  let create = 0;
  let overwrite = 0;
  let haveImpact = false;
  try {
    const pr = await fetch(_ctxWithTargetScope('/api/context/overview', pinnedScopeOpts));
    if (pr.ok) {
      const data = await pr.json();
      for (const key of ['skills', 'commands', 'agents', 'mcp_servers', 'settings']) {
        const d = data?.[key];
        if (!d) continue;
        create += d.missing_target || 0;
        overwrite += d.out_of_sync || 0;
      }
      haveImpact = true;
    }
  } catch {
    /* best-effort impact preview */
  } finally {
    btnLoading(btn, false);
  }
  let message = t('settings.ctx.confirm_sync_all', {
    dest: _ctxScopeDisplayLabelById(scopeId),
  });
  if (haveImpact) {
    message += ' ' + (create === 0 && overwrite === 0
      ? t('settings.ctx.confirm_sync_no_changes')
      : t('settings.ctx.confirm_sync_counts', { create, overwrite }));
  }
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all'),
    // rank-10: name the specific project this portal card syncs (the raw
    // ``scopeId`` resolves to its label; '' === Server CWD).
    message,
    warningText: overwrite > 0
      ? t('settings.ctx.confirm_sync_overwrite_warning', { overwrite })
      : '',
    confirmText: t('settings.ctx.sync'),
    danger: false,
  });
  if (!ok) return;

  btnLoading(btn, true);
  showToast(t('settings.ctx.sync_started') || 'Syncing project...', 'info');

  const succeeded = [];
  let failed = null;
  let settingsSeverity = null;
  let settingsReason = '';
  let anyPhaseStarted = false;
  // Same failure-class skip surfacing as the overview Sync All (#1247 id 21
  // + B4) — an HTTP-200 phase can still have skipped the very items the card
  // claims to have synced.
  const attentionSkips = [];
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
      const body = await resp.json().catch(() => ({}));
      const phaseSkips = Array.isArray(body.skipped) ? body.skipped : [];
      attentionSkips.push(
        ...phaseSkips.filter(_ctxIsAttentionSkip).map(_ctxAttentionSkipLabel),
      );
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
    } else {
      // Artifact attention skips (#1247 id 21 + B4) and the settings
      // needs_confirmation outcome are INDEPENDENT facts — and unlike the
      // overview Sync All, this card path has no per-phase summary region
      // to carry whichever one a single-toast ladder would drop. Emit both
      // (toasts stack); only a run with neither reads as plain success
      // (Codex review).
      if (attentionSkips.length) {
        const items = [...new Set(attentionSkips)];
        showToast(
          t('settings.ctx.sync_skipped_attention', {
            count: items.length,
            items: items.join(', '),
          }),
          'warning',
        );
      }
      if (settingsSeverity === 'needs_confirmation') {
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
      }
      if (!attentionSkips.length && settingsSeverity !== 'needs_confirmation') {
        showToast(t('settings.ctx.sync_success'));
      }
    }
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    if (anyPhaseStarted) {
      // Refresh whichever gateway section is showing. Post-matrix-removal this
      // is only invoked from a Projects portal card, so repaint the portal
      // roster (fresh per-runtime state + counts) rather than the Overview the
      // matrix used to live on. ``loadCtxProjects`` is a global from
      // context-portal.js (loaded after this file; resolved at click time).
      // This refresh does not change ``_ctxActiveScopeId`` — Sync ≠ select.
      const projectsSection = document.getElementById('settings-ctx-projects');
      if (projectsSection && projectsSection.classList.contains('active')) {
        loadCtxProjects();
      } else {
        loadCtxOverview();
      }
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
// (runtime-only banner), and ``renderRuntimeBadges`` (status labels). All
// four are rebuilt by re-issuing
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

  // rank 11: the hoisted control bar's labels are inline ``t()`` (active-project
  // label, tier options, tier-filter aria), so ``I18N.applyDOM`` can't reach
  // them — repaint the bar for the active gateway section on a locale flip. The
  // per-section re-issue below also repaints it for the overview/list sections
  // (an idempotent same-tick double-render, no visible flicker); this standalone
  // call additionally covers hooks-sync, whose section has no re-issue branch
  // here. Gated on the Gateway tab being the visible host so we never repaint
  // into a hidden panel (the Settings-tab fallback above keeps ``hostActive``
  // true while the gateway sections are off-screen).
  if (gatewayTab && gatewayTab.classList.contains('active')) _ctxRenderControlBar();

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
    // ``wasDiffActive`` to land back on the Diff tab so the diff pane
    // (field-map matrix + per-runtime diffs) re-renders in the new
    // locale without further user clicks.
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

// Benign-by-design sync skip codes (``context/_skip_reasons.py``): nothing to
// sync, already byte-identical, or no fan-out target for that (artifact,
// runtime, scope) tuple. Every OTHER code means the engine skipped work the
// user asked for — parse_error, unknown_runtime, duplicate_name (#1247 B4),
// lock_timeout, target_conflict, privacy_blocked, … — and must not hide
// behind the "Sync completed" success toast (#1247 id 21). Allow-listing the
// benign codes (rather than block-listing the failures) keeps future skip
// codes loud by default.
const _CTX_BENIGN_SKIP_CODES = new Set([
  'no_canonical_root',
  'in_sync',
  'no_project_fanout_for_runtime',
]);

function _ctxIsAttentionSkip(s) {
  return !!s && !_CTX_BENIGN_SKIP_CODES.has(s.reason_code);
}

// Toast fragment naming one failure-class skip: "<who> (<code>)". Sync
// responses serialize the skip tuple's first element under the ``runtime``
// key — it carries the artifact/file name for parse_error / duplicate_name
// rows and the runtime name for unknown_runtime rows; tolerate ``name`` for
// shape drift.
function _ctxAttentionSkipLabel(s) {
  const who = (s && (s.runtime || s.name)) || '?';
  return s && s.reason_code ? `${who} (${s.reason_code})` : String(who);
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
      // Two attention shapes share the style: an artifact phase that finished
      // but skipped failure-class items (counts present — render them, the
      // skipped count + attention color carry the signal, #1247 id 21), and
      // the settings phase needing host-write confirmation (no counts —
      // keep the explicit copy so the row matches the "complete except
      // Settings" toast).
      const text = entry.counts
        ? _ctxSyncFormatCounts(entry.counts)
        : t('settings.ctx.sync_state_needs_confirmation');
      badge = `<span class="ctx-sync-state ctx-sync-state--attention">`
        + `${escapeHtml(text)}</span>`;
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
// until the next run. rank 11: the controls now live in the shared
// ``#ctx-control-bar`` header, not inside the overview section — target the bar
// directly (the old ``#settings-ctx-overview`` scope would match nothing and
// the lock would silently no-op). ``loadCtxOverview`` re-renders fresh
// (enabled) controls in the handler's ``finally``, so this only governs the
// in-flight window.
// Apply the current ``_ctxSyncControlsLocked`` flag to the shared bar's live
// controls. Called both when the lock toggles and after every bar repaint, so
// the disabled state survives the shared host being re-rendered mid-run.
function _ctxApplySyncControlsLock() {
  document
    .querySelectorAll('#ctx-control-bar .ctx-tier-filter button')
    .forEach((btn) => { btn.disabled = _ctxSyncControlsLocked; });
  document
    .querySelectorAll('#ctx-control-bar .ctx-project-select')
    .forEach((sel) => { sel.disabled = _ctxSyncControlsLocked; });
}

function _ctxSetSyncControlsDisabled(disabled) {
  _ctxSyncControlsLocked = disabled;
  _ctxApplySyncControlsLock();
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
  // U4 (#1229): pin BOTH dimensions BEFORE the confirm (previously
  // snapshotted after it) so the impact preview, the dialog copy, and every
  // phase URL agree on one (project, tier).
  const syncAllScopeId = _ctxEffectiveScopeId(_ctxActiveScopeId);
  const syncAllTier = _ctxTargetScope;
  // Display label captured WITH the pin — a project switch during the
  // preview fetch must not make the dialog name a different project than
  // the one the phases write to (Codex review).
  const syncAllDestLabel = _ctxScopeDisplayLabelById(_ctxActiveScopeId);
  const pinnedScopeOpts = {
    scopeId: syncAllScopeId,
    scopeResolved: true,
    targetScope: syncAllTier,
  };
  // Best-effort impact preview: refetch the four artifact lists AND the
  // overview (for the settings tile counts — settings are multi-runtime
  // targets with no per-runtime list payload, so they get their own
  // segment) under the pinned scope. Any failure → count-free fallback copy.
  btnLoading(btn, true);
  let impact = null;
  let settingsImpact = null;
  try {
    const types = ['skills', 'commands', 'agents', 'mcp-servers'];
    const responses = await Promise.all([
      ...types.map((typ) => fetch(_ctxWithTargetScope(`/api/context/${typ}`, pinnedScopeOpts))),
      fetch(_ctxWithTargetScope('/api/context/overview', pinnedScopeOpts)),
    ]);
    if (responses.every((r) => r.ok)) {
      const bodies = await Promise.all(responses.map((r) => r.json()));
      const allItems = types.flatMap((typ, i) => bodies[i]?.[typ] || []);
      impact = _ctxSyncImpact(allItems);
      const settings = bodies[types.length]?.settings;
      if (settings) {
        settingsImpact = {
          create: settings.missing_target || 0,
          overwrite: settings.out_of_sync || 0,
        };
      }
    }
  } catch {
    /* best-effort impact preview */
  } finally {
    btnLoading(btn, false);
  }
  let message = t('settings.ctx.confirm_sync_all', { dest: syncAllDestLabel });
  let warningText = '';
  if (impact) {
    const sCreate = settingsImpact?.create || 0;
    const sOverwrite = settingsImpact?.overwrite || 0;
    // "Already in sync" must reflect the COMBINED artifact + settings totals
    // — an artifact-only no-change sentence followed by a settings impact
    // segment reads as a contradiction (Codex review).
    if (impact.create + impact.overwrite + sCreate + sOverwrite === 0) {
      message += ' ' + t('settings.ctx.confirm_sync_no_changes');
    } else {
      if (impact.create + impact.overwrite > 0) {
        message += ' ' + _ctxSyncImpactMessage(impact);
      }
      if (sCreate + sOverwrite > 0) {
        message += ' ' + t('settings.ctx.confirm_sync_settings_impact', {
          create: sCreate,
          overwrite: sOverwrite,
        });
      }
    }
    const totalOverwrite = impact.overwrite + sOverwrite;
    if (totalOverwrite > 0) {
      warningText = t('settings.ctx.confirm_sync_overwrite_warning', {
        overwrite: totalOverwrite,
      });
    }
  }
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all'),
    // rank-10: name the active project being fanned out so the user sees
    // WHERE the artifacts come from before committing.
    message,
    warningText,
    confirmText: t('settings.ctx.sync'),
    danger: false,
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
  // Failure-class skips collected across artifact phases (#1247 id 21): the
  // phase itself completes (HTTP 200) but the engine skipped items it was
  // asked to sync (parse_error / unknown_runtime / duplicate_name / …). The
  // run must not end in the unqualified success toast, and the per-phase row
  // shows ``attention`` instead of ``done``.
  const attentionSkips = [];
  // No-op detection: a run that writes nothing (0 generated everywhere,
  // only ``no_canonical_root`` artifact skips, settings all-skipped) must
  // not end in the "Sync completed" success toast — on an empty project
  // that toast claims work that never happened. The pre-click gate above
  // catches the common case from the overview counts; this catches a
  // stale overview racing an emptied store.
  let generatedTotal = 0;
  let artifactNonEmptySkip = false;
  let settingsNoop = false;
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
    // BOTH dimensions were snapshotted BEFORE the confirm (U4) — the impact
    // preview, the dialog copy, and every phase URL share one (project,
    // tier); a mid-run tier flip OR a cache refresh applies to the next run
    // (ADR-0016 §5 / ADR-0021 §C Major-1).
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
      const phaseCounts = _ctxSyncArtifactCounts(body);
      // ``dropped`` is field-level conversion metadata (a drop can only
      // happen while generating a file, so dropped ⊆ generated runs) —
      // counted anyway so the no-op condition stays correct even if a
      // future engine emits drops standalone.
      generatedTotal += (phaseCounts.generated || 0) + (phaseCounts.dropped || 0);
      const phaseSkips = Array.isArray(body.skipped) ? body.skipped : [];
      if (phaseSkips.some(s => s && s.reason_code !== 'no_canonical_root')) {
        artifactNonEmptySkip = true;
      }
      // Failure-class skips demote the phase row from ``done`` to
      // ``attention`` (#1247 id 21) — the counts still render, the color
      // flags that the skipped column needs a look. Benign skips
      // (no_canonical_root / in_sync / no-fanout-by-design) keep ``done``.
      const phaseAttention = phaseSkips.filter(_ctxIsAttentionSkip);
      if (phaseAttention.length) {
        attentionSkips.push(...phaseAttention.map(_ctxAttentionSkipLabel));
        setPhase(typ, 'attention', phaseCounts);
      } else {
        setPhase(typ, 'done', phaseCounts);
      }
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
            // All-skipped (or empty) results ≡ the settings phase wrote
            // nothing — feeds the run-level no-op toast decision below.
            settingsNoop = settingsResults.length === 0
              || settingsResults.every(r => r && r.status === 'skipped');
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
    } else if (attentionSkips.length) {
      // Failure-class skips (parse_error / unknown_runtime / duplicate_name
      // / …) used to fall through to the unqualified success toast (#1247
      // id 21 + B4). Name the skipped items so the user can map the warning
      // to the artifact/runtime that needs fixing. De-dup: per-(target,
      // item) skips repeat one broken canonical once per runtime. Ordered
      // above ``needs_confirmation`` (warning > info) — when both apply,
      // the settings row still shows its own attention state in the summary.
      const items = [...new Set(attentionSkips)];
      showToast(
        t('settings.ctx.sync_skipped_attention', {
          count: items.length,
          items: items.join(', '),
        }),
        'warning',
      );
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
    } else if (generatedTotal === 0 && !artifactNonEmptySkip && settingsNoop) {
      // Every artifact phase produced 0 files with only ``no_canonical_root``
      // skips and settings was all-skipped — nothing on disk changed, so
      // "Sync completed" would overstate the run. Mirror the pre-click
      // empty gate with an explicit nothing-synced info toast.
      showToast(t('settings.ctx.sync_all_nothing_synced'), 'info');
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
    // Only toast on a load that actually completed: a concurrent scope/tier
    // switch (or a second refresh) aborts this one, which now returns false
    // (#1286) — toasting "Refresh complete" then would be a lie while the
    // winning request is still in flight (Codex review).
    if (await loadCtxOverview()) showToast(t('toast.refresh_complete'));
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
// Per-type AbortControllers paired with ``_ctxListSeq`` (#1286): a re-issued
// ``loadCtxList`` aborts the prior invocation's projects + scope-group fetches.
let _ctxListAbort = { skills: null, commands: null, agents: null, 'mcp-servers': null };

// rank 2c: artifact sections (Skills/Commands/Agents/MCP) scope to the
// active project by default — the same ~30-project roster the Projects
// portal already owns shouldn't be re-painted as a wall of collapsed
// accordions in every section. A "Show all projects" toggle opts back
// into the full roster. Shared across all four sections (a deliberate
// reading: the toggle answers "do I want the roster or just my project",
// which is the same intent regardless of which artifact I'm browsing).
let _ctxListShowAllScopes = false;

// Sibling guard for ``loadCtxDetail`` and ``_ctxLoadRuntimeOnlyDetail``
// races. Both write to the same ``detailEl``, so they share one
// per-type counter. Rapid langchange / card-click bursts can put
// multiple detail fetches in flight; the guard ensures only the newest
// response paints into the live DOM, both for the locale-stale window
// and the Edit-mode buffer-restore race (where an older fetch would
// otherwise overwrite a textarea that the listener just rehydrated).
let _ctxDetailSeq = { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 };
// Per-type AbortControllers paired with ``_ctxDetailSeq`` (#1286), shared by
// ``loadCtxDetail`` and ``_ctxLoadRuntimeOnlyDetail`` (both paint the same
// ``detailEl``). Aborted on a superseding mount AND on a scope switch / list
// wipe (see ``_ctxBumpActiveScopeDetailSeq`` and ``loadCtxList``).
let _ctxDetailAbort = { skills: null, commands: null, agents: null, 'mcp-servers': null };

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

// Map an ``error_kind`` to its localized label, echoing the raw kind when no
// i18n key exists (``t()`` returns the key itself for unknown keys, so future
// kinds stay visible). Shared by ``_ctxErrDetail`` and the overview tile so the
// kind→copy mapping lives in one place (B-1 #1284).
function _ctxKindLabel(errorKind) {
  if (!errorKind) return '';
  const key = `settings.ctx.error_kind_${errorKind}`;
  const translated = t(key);
  return translated === key ? errorKind : translated;
}

// Compose "<kind label> — <server message>" for display; either part may be
// empty. The server ``message`` is kept as secondary detail behind the
// localized kind label.
function _ctxKindDetailText(errorKind, message) {
  return [_ctxKindLabel(errorKind), message].filter(Boolean).join(' — ');
}

// Map a structured error ``detail`` to a readable, localized string.
// Precedence, in order:
//   1. string detail → returned verbatim (legacy routes + the central app.py
//      KeyError/ValueError/Exception handlers, and the issue-pinned privacy 422).
//   2. #1210 write-guard 409 ``{reason_code: sync_paused|sync_not_enrolled}`` →
//      the specific localized copy — checked BEFORE error_kind because those
//      409s carry BOTH error_kind:"conflict" and the reason_code, and the
//      reason_code copy is more precise than the generic "conflict" label.
//   3. B-1 #1284 object envelope ``{error_kind, message, reason_code?}`` →
//      localized kind label + the server message as secondary detail.
//   4. bare ``{message}`` → the server prose.
//   5. caller fallback.
function _ctxErrDetail(detail, fallback) {
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const rc = detail.reason_code;
    if (rc === 'sync_paused') return t('settings.ctx.error_sync_paused');
    if (rc === 'sync_not_enrolled') return t('settings.ctx.error_sync_not_enrolled');
    if (typeof detail.error_kind === 'string' && detail.error_kind) {
      const composed = _ctxKindDetailText(detail.error_kind, detail.message);
      if (composed) return composed;
    }
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
  // MCP servers take a dedicated body: the generic project_shared copy says
  // "Click Import above", and mcp-servers has no Import button or /import
  // route (#1247 id 31 made these rows reachable — Codex impl review). MCP
  // canonicals are project_shared-only, so one un-tiered key is enough.
  const bodyKey = type === 'mcp-servers'
    ? 'settings.ctx.missing_canonical_mcp_body'
    : `settings.ctx.missing_canonical_${scopeForKey}_body`;
  const body = t(bodyKey)
    .replace('{count}', count)
    .replace(/\{type\}/g, _ctxTypeName(type))
    .replace('{scan_dirs}', scanList);
  // The CLI bootstrap snippets cover skills/agents/commands only — there is
  // no ``--include=mcp-servers`` in ``mm context``, so rendering them for the
  // MCP section would hand the user commands that cannot touch MCP state.
  const commands = type === 'mcp-servers'
    ? ''
    : _ctxMissingCanonicalCommands(scope)
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
    // MCP servers have no Import affordance (single ``.mcp.json`` source, no
    // /import route), so the shared project-tier hint's "click Import" sentence
    // pointed at a button that doesn't exist for this section. Branch it.
    // MCP must branch BEFORE the user-tier check: MCP definitions exist only
    // at the project_shared tier (the overview route returns a constant
    // empty payload for other tiers and nothing ever reads
    // ``~/.memtomem/mcp-servers``), so the generic user-tier hint was
    // directing users to stock a directory the backend never looks at.
    let hintKey;
    if (type === 'mcp-servers') {
      hintKey = _ctxTargetScope === 'project_shared'
        ? 'settings.ctx.empty_hint_mcp'
        : 'settings.ctx.empty_hint_mcp_project_only';
    } else if (isUser) {
      hintKey = 'settings.ctx.empty_hint_user';
    } else {
      hintKey = 'settings.ctx.empty_hint';
    }
    // ``{canonical}`` stays the raw-slug path (``.memtomem/skills``) — it
    // names a real directory; only the prose ``{type}`` is localized.
    let hint = t(hintKey)
      .replace(/\{type\}/g, _ctxTypeName(type))
      .replace('{canonical}', canonical);
    if (!isUser) {
      // Same fallback as the runtime-only banner so the hint stays
      // grammatical when no scan dirs are reported (fresh project / no runtimes).
      const scanList = (scannedDirs || []).join(', ') || `.${type}/`;
      hint = hint.replace('{scan_dirs}', scanList);
    }
    return emptyState(
      '',
      t('settings.ctx.no_artifacts').replace('{type}', _ctxTypeName(type)),
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
      // Append label pointers (``production at v2``) so the SR string matches the
      // visible chips. A clickable card carries an ``aria-label``, which OVERRIDES
      // the visible chip text for assistive tech — chips not echoed here would be
      // invisible to SR (same reasoning as the status suffix above). The arrow
      // glyph in the visible chip is ``aria-hidden``; this uses a word phrasing.
      const ariaLabels = (item.versions && item.versions.labels) || {};
      const labelParts = Object.keys(ariaLabels).map((l) =>
        t('settings.ctx.versions.list_chip_aria', { label: l, tag: ariaLabels[l] }));
      const parts = statusParts.concat(labelParts);
      const suffix = parts.length ? ` — ${parts.join(', ')}` : '';
      cardAriaLabel = ` aria-label="${escapeHtml(item.name + suffix)}"`;
    }
    html += `<div class="${cardClass}"${a11yAttrs}${cardAriaLabel} data-name="${escapeHtml(item.name)}"${canonAttr} data-out-of-sync="${outOfSync}"${statusesAttr}>
      <div class="ctx-card-header">
        <div>
          <div class="ctx-card-name">${escapeHtml(item.name)}${tierBadge}</div>
          ${item.canonical_path ? `<div class="ctx-card-path">${escapeHtml(item.canonical_path)}</div>` : `<div class="ctx-card-path text-muted">${escapeHtml(t('settings.ctx.runtime_only_label'))}</div>`}
        </div>
        ${renderRuntimeBadges(item.runtimes)}
      </div>
      ${renderLabelChips(item.versions)}
    </div>`;
  }
  return html;
}

async function _loadScopeGroupItems(type, scope, container, seq, signal) {
  panelLoading(container);
  try {
    const params = new URLSearchParams();
    if (_ctxTargetScope !== 'project_shared') params.set('target_scope', _ctxTargetScope);
    if (scope && scope.scope_id && !_ctxScopeIsServerCwd(scope)) {
      params.set('scope_id', scope.scope_id);
    }
    // ADR-0022 PR4: only agents + commands carry a version store (skills are
    // out of v1, inv 7), so request the per-item ``versions`` enrichment for the
    // label chips only there. The skills list route has no ``include`` param and
    // would ignore it, but gating keeps the wire honest about what's versionable.
    if (type === 'agents' || type === 'commands') params.set('include', 'versions');
    const query = params.toString();
    const res = await fetch(`/api/context/${type}${query ? `?${query}` : ''}`, { signal });
    if (!res.ok) throw new Error(_ctxErrDetail((await res.json().catch(() => ({}))).detail, `Failed to load ${type}`));
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
    // canonical, not a same-named project_shared one. project_local writes
    // are rejected at the server with HTTP 400 (``_reject_project_local_write``)
    // and unconfirmed user-tier writes return a needs_confirmation envelope
    // ``_ctxConfirmHostWrite`` resolves into a confirmed re-send (#1263);
    // the JS surfaces the 400s as toasts via the existing ``err.detail`` path.
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
                focusOnLoad: true,
              });
            } else {
              const detailEl = qs(`ctx-${type}-detail`);
              _ctxLoadRuntimeOnlyDetail(type, card.dataset.name, detailEl, {
                focusOnLoad: true,
              });
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
    // rebuilt — same false-overwrite class as the success path above. An abort
    // is that same supersede (#1286), so bail before re-arming / painting.
    if (_ctxIsAbortError(err) || seq !== _ctxListSeq[type]) return;
    // Re-arm the lazy-load flag (set optimistically by ``fetchOnce`` BEFORE
    // the fetch, to de-dup rapid toggles): without this, a failed group load
    // is permanent for the session — re-toggling the <details> routes through
    // ``fetchOnce`` and no-ops on ``loaded === 'true'`` (#1247 id 24).
    container.dataset.loaded = 'false';
    container.innerHTML = emptyState('', t('settings.ctx.load_failed', { type: _ctxTypeName(type) }), err.message);
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
  // rank 11: the tier filter moved to the persistent header bar, so the list no
  // longer holds a tier-filter row to sit below. Keep the tier-aware
  // write-blocked banner (#943) at the very top — its copy explains why the
  // write buttons are dimmed, so the deep-link banner must not land above it —
  // and place this banner immediately AFTER it when present (mirroring the
  // runtime-only remediation banner's anchoring in _ctxRefreshSectionState),
  // otherwise at the list's first row. ``.ctx-runtime-only-banner`` is a sibling
  // concern; both can co-exist with a deep link.
  const writeBlocked = listEl.querySelector('.ctx-write-blocked-banner');
  const anchor = writeBlocked ? writeBlocked.nextSibling : listEl.firstChild;
  listEl.insertBefore(banner, anchor);
}

// #1287: a healthy projects payload always contains the server-cwd scope, so
// an empty roster (or a thrown load) means the load failed — not that the
// inventory is empty. The old "No project scopes" empty state described an
// impossible state and read as data loss; render a load error with a Retry
// instead. ``retry`` is the surface's own reload entry point — the artifact
// lists pass ``loadCtxList(type)``, the Projects portal (context-portal.js,
// loaded after this file) passes ``loadCtxProjects()``.
function _ctxScopesLoadError(listEl, message, detail, retry) {
  listEl.innerHTML = emptyState('', message, detail || '');
  // Announce the load failure: this renders in-place (no toast) and is only
  // reached on failure, so a one-shot ``role="alert"`` on the error card is safe
  // — it won't re-fire on routine list re-renders. We tag the rendered card
  // rather than the shared ``emptyState`` helper so benign empty states stay silent.
  listEl.querySelector('.empty-state')?.setAttribute('role', 'alert');
  const retryBtn = document.createElement('button');
  retryBtn.type = 'button';
  retryBtn.className = 'btn-ghost ctx-scopes-retry';
  retryBtn.textContent = t('settings.ctx.retry');
  retryBtn.addEventListener('click', () => { retry(); });
  (listEl.querySelector('.empty-state') || listEl).appendChild(retryBtn);
}

async function loadCtxList(type) {
  const seq = ++_ctxListSeq[type];
  // Abort the superseded list load's in-flight fetches (#1286) — the signal
  // threads through the projects sub-fetch and every scope-group fetch below.
  _ctxListAbort[type] = _ctxSwapAbort(_ctxListAbort[type]);
  const signal = _ctxListAbort[type]?.signal;
  const listEl = qs(`ctx-${type}-list`);
  const detailEl = qs(`ctx-${type}-detail`);
  const statusEl = qs(`ctx-${type}-status`);
  // This wipe owns the detail pane, so it must also invalidate any in-flight
  // detail mount: a tier flip / section re-entry routes through here without
  // a project change (the project handler bumps via
  // ``_ctxBumpActiveScopeDetailSeq``), and without the bump a stale
  // ``loadCtxDetail`` / ``_ctxLoadRuntimeOnlyDetail`` response would pass its
  // seq check, paint into the wiped-and-hidden pane, and fire draft-restore
  // side effects from an invisible mount (#1247 id 26). Callers that want the
  // detail re-mounted (save success, langchange) call ``loadCtxDetail``
  // AFTER this, taking a fresh seq.
  _ctxDetailSeq[type] += 1;
  // Same reasoning extends to the abort (#1286): the wipe orphans any in-flight
  // detail fetch for this type, so cancel it rather than let it resolve into the
  // hidden pane. The next loadCtxDetail mints a fresh controller.
  try { _ctxDetailAbort[type]?.abort(); } catch { /* no-op */ }
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
    const result = await _ctxFetchProjectsData({ signal });
    if (seq !== _ctxListSeq[type]) return;
    const data = _ctxCommitProjects(result);
    const scopes = data.scopes || [];
    if (!scopes.length) {
      // Should never happen — server cwd always present — so treat it as a
      // failed load (with Retry), not an empty inventory (#1287).
      _ctxScopesLoadError(listEl, t('settings.ctx.scopes_load_failed'), '', () => loadCtxList(type));
      return;
    }

    // rank 2c: default to the active project only — the Projects portal owns
    // the full roster, so re-painting every scope as a collapsed accordion in
    // each artifact section just buries the one project the user is acting on.
    // The "Show all projects" toggle opts back into the full list. Fall back
    // to all scopes if no active scope resolves (``_ctxCommitProjects``
    // normalizes one, but never blank the panel) so a stale active-scope id
    // can't strand the user on an empty section.
    const activeScopes = scopes.filter(_ctxScopeIsActive);
    const visibleScopes = (_ctxListShowAllScopes || !activeScopes.length) ? scopes : activeScopes;

    // rank 11: the active-project + tier controls are no longer emitted into each
    // section's list — they render once into the persistent header bar
    // (``_ctxRenderControlBar`` below). The list keeps only the rank-2c "Show all
    // projects" toggle + the per-scope groups for the visible scope(s).
    let html = _ctxShowAllScopesControl(type, scopes);
    for (const scope of visibleScopes) {
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
      // The remove (×) button is a SIBLING of <details>, not a child of
      // <summary> (rank 15). A real <button> nested inside the <summary>
      // activation control is the banned nested-interactive antipattern
      // (#1003) and made keyboard activation of × ambiguous against the
      // disclosure's native toggle. It can't be a plain child of <details>
      // either — native <details> hides every non-<summary> child while
      // collapsed, which would make × disappear on closed groups. So wrap
      // both and pin × over the summary's right edge via CSS
      // (``.ctx-scope-group-wrap``), keeping × always visible and a
      // standalone button with unambiguous keyboard activation.
      html += `<div class="ctx-scope-group-wrap">
        <details class="ctx-scope-group" data-scope-id="${escapeHtml(scope.scope_id)}" data-tier="${escapeHtml(scope.tier)}"${isActive ? ' open' : ''}>
          <summary class="ctx-scope-summary" ${rootTitle}>
            <span class="ctx-scope-summary-label">${escapeHtml(_ctxScopeDisplayLabel(scope))}</span>
            <span class="ctx-scope-summary-count">${count}</span>
            ${_ctxScopeBadges(scope)}
          </summary>
          <div class="ctx-scope-items" id="${groupId}" data-loaded="false"></div>
        </details>
        ${removeBtn}
      </div>`;
    }
    listEl.innerHTML = html;
    // rank 11: repaint the shared header bar (self-sources from the active
    // section so a stale list render can't hijack it). rank 2c: wire the
    // "Show all projects" toggle that still lives in the list.
    _ctxRenderControlBar();
    _ctxWireShowAllScopes(type, listEl);

    // Tier-aware banner (issue #943, reframed by #1263): inserted at the
    // top of the list whenever the canonical-tier filter is set to a
    // non-shared tier. project_local keeps the read-only framing; the
    // user tier is writable since #1302 with a confirm-first contract,
    // so its copy explains the disclosure instead of claiming read-only.
    // Sits ABOVE the runtime-only banner that ``_ctxRefreshSectionState``
    // may insert later.
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
    // Iterate the same ``visibleScopes`` the render loop used so we never
    // try to wire a group that wasn't painted (rank 2c).
    for (const scope of visibleScopes) {
      const groupEl = listEl.querySelector(`details[data-scope-id="${CSS.escape(scope.scope_id)}"]`);
      if (!groupEl) continue;
      const itemsEl = groupEl.querySelector('.ctx-scope-items');
      const fetchOnce = () => {
        if (itemsEl.dataset.loaded === 'true') return;
        itemsEl.dataset.loaded = 'true';
        // ``signal`` is this load's controller (#1286): a group expanded after a
        // supersede fires with an already-aborted signal, so its fetch rejects
        // immediately instead of racing the fresh list (the seq guard inside
        // catches it either way).
        _loadScopeGroupItems(type, scope, itemsEl, seq, signal);
      };
      if (groupEl.open) fetchOnce();
      groupEl.addEventListener('toggle', () => { if (groupEl.open) fetchOnce(); });

      // × is a sibling of <details> inside ``.ctx-scope-group-wrap`` (rank
      // 15), so reach it through the wrapper rather than ``groupEl`` itself.
      const removeBtn = groupEl.parentElement?.querySelector(':scope > .ctx-scope-remove');
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
          // In-flight disable (#1247 id 27): mirrors the detail Delete
          // button — a slow DELETE otherwise allows a second confirm whose
          // duplicate request error-toasts right after the success toast.
          // The success path's ``loadCtxList`` repaints the whole list
          // (fresh, enabled ×), so the ``finally`` restore only matters on
          // the error paths.
          btnLoading(removeBtn, true);
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
          } finally { btnLoading(removeBtn, false); }
        });
      }
    }
  } catch (err) {
    // Aborted = superseded by a newer list load (#1286); its seq guard owns the
    // panel, so don't paint a load-error + Retry over it.
    if (_ctxIsAbortError(err) || seq !== _ctxListSeq[type]) return;
    _ctxScopesLoadError(
      listEl, t('settings.ctx.load_failed', { type: _ctxTypeName(type) }), err.message,
      () => loadCtxList(type),
    );
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
// agents only — mcp-servers transfer (copy-only/cross-project/no-rename) is a
// follow-up, and its detail pane omits the button.
const _CTX_TRANSFER_TYPES = new Set(['skills', 'commands', 'agents']);

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
function _ctxSyncMoveCopyVisibility() {
  const modalEl = qs('ctx-move-copy-modal');
  if (!modalEl) return;
  const mode = modalEl.querySelector('input[name="ctx-mc-mode"]:checked')?.value || 'copy';
  const tier = modalEl.querySelector('input[name="ctx-mc-tier"]:checked')?.value || 'project_shared';
  const projRow = qs('ctx-mc-project-row');
  const renameRow = qs('ctx-mc-rename-row');
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
  const subjectEl = qs('ctx-mc-subject');
  const previewEl = qs('ctx-mc-preview');
  const warnEl = qs('ctx-mc-warning');
  const applyBtn = qs('ctx-mc-apply-btn');
  if (!previewEl || !warnEl || !applyBtn) return;
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
        const ok = await showConfirm({
          title: t('settings.ctx.move_copy_shared_confirm_title'),
          message: data.reason || t('settings.ctx.move_copy_shared_confirm_message'),
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
// (ADR-0021 §C). skills/commands/agents only.
function _ctxOpenMoveCopyModal(srcType, srcName) {
  const modalEl = qs('ctx-move-copy-modal');
  if (!modalEl || !_CTX_TRANSFER_TYPES.has(srcType)) return;
  // Clear any stale disabled/loading DOM left by an interrupted prior session.
  _ctxResetMoveCopyControls(modalEl);
  const srcScope = (_ctxProjectsCache || []).find(_ctxScopeIsActive);
  const state = {
    srcType,
    srcName,
    srcTier: _ctxTargetScope,
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

  // Destination project options: sync-eligible scopes plus always the source
  // project (same-project promote must be offered; a paused source surfaces
  // inline at dry-run). Missing scopes excluded.
  const projSel = qs('ctx-mc-project');
  if (projSel) {
    const list = (_ctxProjectsCache || []).filter(s =>
      s && !s.missing && (_ctxScopeSyncEligible(s) || _ctxScopeIsActive(s)));
    projSel.innerHTML = list.map(s => {
      const sel = (s.scope_id === state.srcScopeIdRaw) ? ' selected' : '';
      return `<option value="${escapeHtml(s.scope_id)}"${sel}>${escapeHtml(_ctxScopeDisplayLabel(s))}</option>`;
    }).join('');
  }

  // Defaults: copy + a destination tier that differs from the source so the
  // first preview isn't a same-store no-op in the common case.
  const defaultTier = state.srcTier === 'project_shared' ? 'project_local' : 'project_shared';
  modalEl.querySelectorAll('input[name="ctx-mc-mode"]').forEach(el => { el.checked = el.value === 'copy'; });
  modalEl.querySelectorAll('input[name="ctx-mc-tier"]').forEach(el => { el.checked = el.value === defaultTier; });
  const renameEl = qs('ctx-mc-rename');
  if (renameEl) renameEl.value = '';
  _ctxSyncMoveCopyVisibility();

  const applyBtn = qs('ctx-mc-apply-btn');
  const cancelBtn = qs('ctx-mc-cancel-btn');
  const onChange = () => { _ctxSyncMoveCopyVisibility(); _ctxMoveCopyPreview(state); };
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

  _ctxRenderMoveCopyPreview(state);   // paint subject + reset Apply
  _ctxMoveCopyPreview(state);         // kick the first dry-run
  // Initial focus goes to the first control, NOT Apply — Apply starts disabled
  // (pending the first dry-run), and focusing a disabled button drops focus to
  // the now-inert background. The Tab trap (openModalA11y) cycles from here.
  (modalEl.querySelector('input[name="ctx-mc-mode"]:checked')
    || modalEl.querySelector('input, select, button'))?.focus();
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
    // ``mtime_ns`` is the CANONICAL file's mtime — label it "Modified", not
    // "Last synced": sync timing is a different fact this row never knew
    // (#1247 id 37, the same overstatement #1076 renamed on the dashboard).
    const ts = Number(data.mtime_ns) / 1e6;
    if (Number.isFinite(ts)) {
      rows.push({
        label: t('settings.ctx.detail.meta_modified'),
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
  } else if (type === 'mcp-servers') {
    // #1247 id 36: read_mcp_server has always returned these — they just
    // never rendered. Numeric 0 passes the chips filter intentionally:
    // "Args 0" confirms the definition parsed (an unparseable canonical
    // yields fields == {} and no chips at all).
    const chips = [
      ['mcp_command', fields.command],
      ['mcp_args_count', fields.args_count],
      ['mcp_env_count', fields.env_count],
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


// ── Version snapshots + label pointers (ADR-0022) ────────────────────────
// Surfaces the per-artifact version store in the detail panel for agents and
// commands in directory layout. Backed by the context_versions routes:
//   GET    /api/context/<type>/<name>/versions       list versions + labels
//   POST   .../versions                              freeze working canonical
//   PUT    .../labels/<label>                         promote (== rollback)
//   DELETE .../labels/<label>                         drop a label pointer
// Skills and flat-layout artifacts have no version store — the GET returns
// ``migrate_required`` and the section renders a hint instead of controls
// (ADR-0022 invariants 3 + 7).
const _CTX_VERSIONABLE_TYPES = new Set(['agents', 'commands']);
// Label names always offered in the promote picker, unioned with whatever
// labels already exist on the artifact (ADR-0022 allows arbitrary names).
const _CTX_DEFAULT_LABELS = ['production', 'staging'];

function _ctxLabelsByTag(labels) {
  // Invert {label: tag} → {tag: [label, …]} so each version row can show the
  // pointers that land on it.
  const byTag = {};
  for (const [label, tag] of Object.entries(labels || {})) {
    (byTag[tag] = byTag[tag] || []).push(label);
  }
  return byTag;
}

function _ctxRenderVersionsInner(data) {
  const versions = Array.isArray(data.versions) ? data.versions : [];
  const labels = data.labels || {};
  const byTag = _ctxLabelsByTag(labels);

  let html = '<div class="ctx-detail-versions-header">';
  html += `<span class="ctx-detail-versions-title" data-i18n="settings.ctx.versions.title">${escapeHtml(t('settings.ctx.versions.title'))}</span>`;
  html += `<button class="btn-ghost ctx-version-freeze-btn" data-i18n="settings.ctx.versions.freeze" data-i18n-title="settings.ctx.versions.freeze_tooltip" title="${escapeHtml(t('settings.ctx.versions.freeze_tooltip'))}">${escapeHtml(t('settings.ctx.versions.freeze'))}</button>`;
  html += '</div>';

  if (!versions.length) {
    html += `<div class="ctx-version-empty text-muted" data-i18n="settings.ctx.versions.empty">${escapeHtml(t('settings.ctx.versions.empty'))}</div>`;
    return html;
  }

  // Every promote picker offers the same option set: defaults ∪ existing labels.
  const labelOptions = Array.from(new Set([..._CTX_DEFAULT_LABELS, ...Object.keys(labels)]));
  const opts = labelOptions
    .map(l => `<option value="${escapeHtml(l)}">${escapeHtml(l)}</option>`)
    .join('');

  html += '<ul class="ctx-version-list">';
  for (const v of versions) {
    const tag = String(v.tag || '');
    const here = byTag[tag] || [];
    const labelChips = here
      .map(l =>
        `<span class="ctx-version-label-chip" data-label="${escapeHtml(l)}">`
        + `<span class="ctx-version-label-name">${escapeHtml(l)}</span>`
        + `<button class="ctx-version-label-remove" data-label="${escapeHtml(l)}" `
        + `data-i18n-title="settings.ctx.versions.remove_label_tooltip" `
        + `title="${escapeHtml(t('settings.ctx.versions.remove_label_tooltip'))}" `
        + `aria-label="${escapeHtml(t('settings.ctx.versions.remove_label_tooltip'))}">×</button>`
        + `</span>`,
      )
      .join('');
    const date = v.created_at ? escapeHtml(String(v.created_at)) : '';
    const note = v.note
      ? `<span class="ctx-version-note">${escapeHtml(String(v.note))}</span>`
      : '';
    html += `<li class="ctx-version-row" data-tag="${escapeHtml(tag)}">`
      + `<div class="ctx-version-main">`
      + `<span class="ctx-version-tag">${escapeHtml(tag)}</span>`
      + `<span class="ctx-version-labels-inline">${labelChips}</span>`
      + note
      + `<span class="ctx-version-date text-muted">${date}</span>`
      + `</div>`
      + `<div class="ctx-version-actions">`
      + `<select class="ctx-version-label-select" aria-label="${escapeHtml(t('settings.ctx.versions.promote'))}">${opts}</select>`
      + `<button class="btn-ghost ctx-version-promote-btn" data-tag="${escapeHtml(tag)}" `
      + `data-i18n="settings.ctx.versions.promote" `
      + `data-i18n-title="settings.ctx.versions.promote_tooltip" `
      + `title="${escapeHtml(t('settings.ctx.versions.promote_tooltip'))}">${escapeHtml(t('settings.ctx.versions.promote'))}</button>`
      + `</div>`
      + `</li>`;
  }
  html += '</ul>';
  return html;
}

async function _ctxLoadVersions(type, name, detailEl, seq) {
  // Paints the version store into the hidden ``.ctx-detail-versions``
  // placeholder mounted by ``loadCtxDetail``. ``seq`` guards against a
  // superseded detail mount (shares ``_ctxDetailSeq[type]`` with the parent).
  const container = detailEl.querySelector('.ctx-detail-versions');
  if (!container) return;
  // Early bail: a newer detail mount already superseded us, no need to fetch.
  if (seq != null && seq !== _ctxDetailSeq[type]) return;
  let data;
  try {
    const res = await fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/versions`),
    );
    if (seq != null && seq !== _ctxDetailSeq[type]) return;
    if (!res.ok) { container.hidden = true; return; }
    data = await res.json();
  } catch (_) {
    container.hidden = true;
    return;
  }
  if (seq != null && seq !== _ctxDetailSeq[type]) return;

  if (data.migrate_required) {
    // Flat-layout artifact: no per-artifact directory to hold a versions/
    // store (ADR-0022 inv 3). Offer an explicit "Enable versioning" action
    // that adopts the flat canonical into directory layout (rank 6) — the CLI
    // ``mm context migrate`` refuses web-created flat files (no lockfile entry
    // ⇒ skip_manual), so this button is the supported escape hatch.
    container.hidden = false;
    container.innerHTML =
      '<div class="ctx-detail-versions-header">'
      + `<span class="ctx-detail-versions-title" data-i18n="settings.ctx.versions.title">${escapeHtml(t('settings.ctx.versions.title'))}</span>`
      + `<button class="btn-ghost ctx-version-enable-btn" data-i18n="settings.ctx.versions.enable" `
      + `data-i18n-title="settings.ctx.versions.enable_tooltip" `
      + `title="${escapeHtml(t('settings.ctx.versions.enable_tooltip'))}">${escapeHtml(t('settings.ctx.versions.enable'))}</button>`
      + '</div>'
      + `<div class="ctx-version-empty text-muted" data-i18n="settings.ctx.versions.migrate_required">${escapeHtml(t('settings.ctx.versions.migrate_required'))}</div>`;
    _ctxWireEnableVersioning(type, name, detailEl, seq);
    // The enable button just landed — run the tier gate so it picks up
    // ``data-write-blocked`` on non-shared tiers (it's a project_shared-only
    // canonical write, like freeze/promote).
    _ctxRefreshWriteBlockedState();
    return;
  }

  container.hidden = false;
  container.innerHTML = _ctxRenderVersionsInner(data);
  _ctxWireVersionControls(type, name, detailEl, seq);
  // The freeze/promote/remove buttons just landed — re-run the tier gate so
  // they pick up ``data-write-blocked`` without a full detail re-render (#943).
  _ctxRefreshWriteBlockedState();
}

function _ctxWireEnableVersioning(type, name, detailEl, seq) {
  // Wires the "Enable versioning" button shown for a flat-layout artifact: it
  // adopts the flat canonical into directory layout (ADR-0022 rank 6) so the
  // version store becomes available. On success the layout changed flat→dir,
  // so reload the WHOLE detail panel (not just the versions section) — the meta
  // header's layout / file-count chips are now stale too.
  const btn = detailEl.querySelector('.ctx-version-enable-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btnLoading(btn, true);
    try {
      const csrf = await ensureCsrfToken();
      // Inline-binding CSRF thread (shape B in test_web_invariants_registry).
      const headers = csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' };
      const r = await fetch(
        _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/versions/enable`),
        { method: 'POST', headers, body: JSON.stringify({}) },
      );
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
        return;
      }
      showToast(t('settings.ctx.versions.enable_success', { name }));
      // Bail if the user navigated to another artifact while the POST was in
      // flight — re-rendering the detail would yank them back to this one.
      if (seq != null && seq !== _ctxDetailSeq[type]) return;
      loadCtxDetail(type, name);
    } catch (_) {
      showToast(t('toast.request_failed'), 'error');
    } finally {
      btnLoading(btn, false);
    }
  });
}

function _ctxWireVersionControls(type, name, detailEl, seq) {
  // Capture the mount's ``seq`` at definition time so a post-mutation reload
  // bails if the user has since navigated to another artifact (re-reading
  // ``_ctxDetailSeq[type]`` fresh would paint into a superseded panel).
  const reload = () => _ctxLoadVersions(type, name, detailEl, seq);

  detailEl.querySelector('.ctx-version-freeze-btn')?.addEventListener('click', async () => {
    const btn = detailEl.querySelector('.ctx-version-freeze-btn');
    btnLoading(btn, true);
    try {
      const csrf = await ensureCsrfToken();
      // Inline-binding CSRF thread (shape B in test_web_invariants_registry).
      const headers = csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' };
      const r = await fetch(
        _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/versions`),
        { method: 'POST', headers, body: JSON.stringify({}) },
      );
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
        return;
      }
      const result = await r.json();
      const tag = (result.version && result.version.tag) || '';
      showToast(t('settings.ctx.versions.freeze_success', { name, tag }));
      reload();
    } catch (_) {
      showToast(t('toast.request_failed'), 'error');
    } finally {
      btnLoading(btn, false);
    }
  });

  detailEl.querySelectorAll('.ctx-version-promote-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const tag = btn.dataset.tag || '';
      const row = btn.closest('.ctx-version-row');
      const select = row && row.querySelector('.ctx-version-label-select');
      const label = select ? select.value : '';
      if (!label) return;
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        // Inline-binding CSRF thread (shape B in test_web_invariants_registry).
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const r = await fetch(
          _ctxWithTargetScope(
            `/api/context/${type}/${encodeURIComponent(name)}/labels/${encodeURIComponent(label)}`,
          ),
          { method: 'PUT', headers, body: JSON.stringify({ version: tag }) },
        );
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        showToast(t('settings.ctx.versions.promote_success', { label, tag }));
        reload();
      } catch (_) {
        showToast(t('toast.request_failed'), 'error');
      } finally {
        btnLoading(btn, false);
      }
    });
  });

  detailEl.querySelectorAll('.ctx-version-label-remove').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const label = btn.dataset.label || '';
      if (!label) return;
      try {
        const csrf = await ensureCsrfToken();
        const r = await fetch(
          _ctxWithTargetScope(
            `/api/context/${type}/${encodeURIComponent(name)}/labels/${encodeURIComponent(label)}`,
          ),
          { method: 'DELETE', headers: csrf ? { 'X-Memtomem-CSRF': csrf } : {} },
        );
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        showToast(t('settings.ctx.versions.remove_label_success', { label }));
        reload();
      } catch (_) {
        showToast(t('toast.request_failed'), 'error');
      }
    });
  });
}


// A card click renders the detail panel as a DOM sibling AFTER the full
// project roster, so it paints ~2000px below the click — without a scroll +
// focus move the interaction reads as dead, and keyboard/SR users are left
// stranded high in the list. Both detail loaders call this once painted, but
// only on user navigation (``opts.focusOnLoad``); the langchange re-render
// path leaves it unset so a language toggle never yanks the viewport down.
function _ctxFocusDetail(detailEl) {
  try {
    detailEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (_e) {
    detailEl.scrollIntoView();
  }
  // ``preventScroll`` so moving focus to the heading doesn't fight the
  // smooth scroll above (the heading is tabindex=-1 + the region's label).
  const heading = detailEl.querySelector('.ctx-detail-name');
  if (heading) heading.focus({ preventScroll: true });
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
  // Abort a superseding detail mount's in-flight fetch (#1286) — shares the
  // per-type controller with ``_ctxLoadRuntimeOnlyDetail`` since both paint the
  // same ``detailEl``.
  _ctxDetailAbort[type] = _ctxSwapAbort(_ctxDetailAbort[type]);
  const signal = _ctxDetailAbort[type]?.signal;
  const autoOpenDiff = opts.autoOpenDiff === true;
  const detailEl = qs(`ctx-${type}-detail`);
  detailEl.hidden = false;
  _ctxCurrentDetail = { type, name, runtimeOnly: false };
  panelLoading(detailEl);

  try {
    const res = await fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}`),
      { signal },
    );
    if (res.status === 404) {
      if (seq !== _ctxDetailSeq[type]) return;
      detailEl.innerHTML = emptyState('', t('settings.ctx.not_found', { name }), t('settings.ctx.no_artifacts_hint'));
      return;
    }
    if (!res.ok) throw new Error(_ctxErrDetail((await res.json().catch(() => ({}))).detail, `Failed to load ${name}`));
    const data = await res.json();
    // Bail if a newer ``loadCtxDetail`` (or ``_ctxLoadRuntimeOnlyDetail``)
    // has superseded us — both share ``_ctxDetailSeq[type]`` because
    // they paint into the same detailEl. Without this, a slow first
    // toggle's response would overwrite a freshly-mounted second
    // toggle's render and silently drop any edit buffer the second
    // toggle's `.then()` had just rehydrated (review P2).
    if (seq !== _ctxDetailSeq[type]) return;

    let html = `<div class="ctx-detail" role="region" aria-labelledby="ctx-detail-name-${type}">`;
    // Move/Copy is offered for the transfer kinds only (skills/commands/agents);
    // mcp-servers transfer is a follow-up. The button is NOT in
    // ``_CTX_WRITE_BUTTON_SELECTOR``: transfer gates the DESTINATION tier (chosen
    // in the modal), not the source tier the write-block sweep keys on — and
    // Move/Copy is the escape hatch FROM project_local/user, so source-tier
    // blocking would hide it exactly where it is most useful.
    const _mcBtn = _CTX_TRANSFER_TYPES.has(type)
      ? `<button class="btn-ghost ctx-detail-move-copy-btn" data-i18n="settings.ctx.move_copy" data-i18n-title="settings.ctx.move_copy_tooltip" title="${escapeHtml(t('settings.ctx.move_copy_tooltip'))}">${t('settings.ctx.move_copy')}</button>`
      : '';
    html += `<div class="ctx-detail-header">
      <h2 class="ctx-detail-name" id="ctx-detail-name-${type}" tabindex="-1">${escapeHtml(name)}</h2>
      <div style="display:flex;gap:6px">
        <button class="btn-ghost ctx-detail-edit-btn" data-i18n="settings.ctx.edit" data-i18n-title="settings.ctx.edit_tooltip" title="${escapeHtml(t('settings.ctx.edit_tooltip'))}">${t('settings.ctx.edit')}</button>
        ${_mcBtn}
        <button class="btn-ghost btn-danger ctx-detail-delete-btn" data-i18n="settings.ctx.delete" data-i18n-title="settings.ctx.delete_tooltip" title="${escapeHtml(t('settings.ctx.delete_tooltip'))}">${t('settings.ctx.delete')}</button>
      </div>
    </div>`;

    // Detail meta header (#962). Surfaces fields the backend already
    // exposes but the canonical pane buried inside the raw file content:
    // description (from frontmatter), scope tier, layout (flat/dir),
    // file count (dir layout), and parsed-field chips for agents and
    // commands. Skills get the meta only — no chip row.
    html += _ctxRenderDetailMetaHeader(type, data);

    // ADR-0022 version/label manager — agents + commands only. The
    // placeholder mounts hidden and is filled asynchronously by
    // ``_ctxLoadVersions`` (a separate GET), so a flat-layout / skill /
    // no-versions artifact never flashes an empty box before the store
    // resolves.
    if (_CTX_VERSIONABLE_TYPES.has(type)) {
      html += '<div class="ctx-detail-versions" hidden></div>';
    }

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
    if (opts.focusOnLoad) _ctxFocusDetail(detailEl);
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
        const putOnce = (extra) => fetch(
          _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}`),
          {
            method: 'PUT',
            headers,
            body: JSON.stringify({ content, mtime_ns, ...extra }),
          },
        );
        let r = await putOnce({});
        if (r.status === 409) {
          await _ctxHandleConflict(type, name, content, mtime_ns, detailEl);
          return;
        }
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        let result = await r.json();
        if (_ctxIsHostWriteEnvelope(result)) {
          // #1263 user-tier save: disclose the host path, re-PUT with the
          // flag. The confirmed leg can still lose an mtime race — give it
          // the same 409 → conflict-dialog path as the first attempt.
          r = await _ctxConfirmHostWrite(result, () => putOnce({ allow_host_writes: true }));
          if (!r) return;
          if (r.status === 409) {
            await _ctxHandleConflict(type, name, content, mtime_ns, detailEl);
            return;
          }
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
            return;
          }
          result = await r.json();
        }
        if (result.name) {
          showToast(t('settings.ctx.save_success').replace('{name}', name));
          detailEl.dataset.mtimeNs = result.mtime_ns || '';
          // Clear any stashed draft now that the buffer is durable on disk.
          _ctxClearDraft(detailEl.dataset.draftKey, type, name);
          // Refresh the LIST too, not just the detail (#1247 id 22): the
          // canonical just changed on disk, so the card's sync badges /
          // ``data-out-of-sync`` are stale — leaving them reads as "in sync"
          // and hides the now-required Sync step (a re-click wouldn't even
          // auto-open the Diff). Same list+detail re-mount order as the
          // langchange path; ``loadCtxDetail`` takes a fresh detail seq
          // AFTER ``loadCtxList``'s wipe-side bump, so the mounts can't
          // cancel each other.
          loadCtxList(type);
          loadCtxDetail(type, name);
        }
      } catch (err) {
        showToast(t('toast.save_failed', { error: err.message }), 'error');
      } finally { btnLoading(btn, false); }
    });

    // Delete
    detailEl.querySelector('.ctx-detail-delete-btn')?.addEventListener('click', async () => {
      // Captured synchronously at click dispatch — the bound button is
      // guaranteed live here, whereas a post-``showConfirm`` querySelector
      // could return null if a re-mount wiped the detail mid-dialog.
      const delBtn = detailEl.querySelector('.ctx-detail-delete-btn');
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
      // In-flight disable (#1247 id 27): a slow DELETE left the button live,
      // so a second click could stack another confirm whose duplicate DELETE
      // 404s — an error toast right after the success toast. Restored in
      // ``finally``; the success path hides/replaces the detail anyway, so
      // re-enabling a detached button is harmless.
      btnLoading(delBtn, true);
      try {
        const csrf = await ensureCsrfToken();
        const deleteOnce = (confirmed) => fetch(
          _ctxWithTargetScope(
            `/api/context/${type}/${encodeURIComponent(name)}?cascade=${cascade}`
            + (confirmed ? '&allow_host_writes=true' : ''),
          ),
          { method: 'DELETE', headers: csrf ? { 'X-Memtomem-CSRF': csrf } : {} },
        );
        let r = await deleteOnce(false);
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        let data = await r.json();
        if (_ctxIsHostWriteEnvelope(data)) {
          // #1263 user-tier delete: the envelope's host_targets carry the
          // canonical dir plus — on cascade — the user-tier runtime copies
          // (the flag rides the query string: DELETE bodies are
          // client-hostile, same reason the route takes it that way).
          r = await _ctxConfirmHostWrite(data, () => deleteOnce(true));
          if (!r) return;
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
            return;
          }
          data = await r.json();
        }
        // Branch on lengths, not truthiness — ``deleted``/``skipped`` are
        // always arrays and ``[]`` is truthy, so a fully-failed delete
        // (every unlink skipped) used to show the success toast and hide
        // the detail (#1247 id 33). Reasons are sanitized server-side.
        const skipped = Array.isArray(data.skipped) ? data.skipped : [];
        if (skipped.length) {
          // Partial/failed delete: name the first failure, keep the detail
          // open (the artifact may still exist — matches the error path
          // above), and reload so list badges repaint to on-disk truth.
          showToast(t('settings.ctx.delete_partial', {
            count: skipped.length,
            path: (skipped[0] && skipped[0].path) || '',
            reason: (skipped[0] && skipped[0].reason) || '',
          }), 'warning');
          loadCtxList(type);
        } else {
          // Includes the zero-deleted idempotent no-op: nothing existed,
          // which is the state the user asked for.
          showToast(t('settings.ctx.delete_success').replace('{name}', name));
          detailEl.hidden = true;
          loadCtxList(type);
        }
      } catch (err) {
        showToast(t('toast.delete_failed', { error: err.message }), 'error');
      } finally { btnLoading(delBtn, false); }
    });

    // Move / Copy to… (B-6 #1289). The modal self-manages its dry-run preview,
    // gate round-trips, and the success refresh, so the click just opens it
    // (no btnLoading — the modal opens synchronously). Rendered for the
    // transfer kinds only; ``?.`` no-ops for mcp-servers.
    detailEl.querySelector('.ctx-detail-move-copy-btn')?.addEventListener('click', () => {
      _ctxOpenMoveCopyModal(type, name);
    });

    // Per-item Edit / Delete buttons just landed in ``detailEl``; mirror
    // the section-level gate so they pick up the tier filter without
    // requiring a list re-render (#943).
    _ctxRefreshWriteBlockedState();

    // Fire-and-forget the version-store load (ADR-0022); it paints into the
    // hidden placeholder and re-runs the write-block gate for its own
    // buttons. Not awaited so the detail render returns promptly; the seq
    // guard inside drops the paint if a newer detail mount supersedes us.
    if (_CTX_VERSIONABLE_TYPES.has(type)) {
      _ctxLoadVersions(type, name, detailEl, seq);
    }

  } catch (err) {
    // Aborted = a newer detail mount / scope switch superseded us (#1286); its
    // seq guard owns the pane, so don't paint a load error over it.
    if (_ctxIsAbortError(err) || seq !== _ctxDetailSeq[type]) return;
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
    if (!res.ok) throw new Error(_ctxErrDetail((await res.json().catch(() => ({}))).detail, t('settings.ctx.diff_failed')));
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
        html += _ctxDiagnosticDetail(rt, data.canonical_path);
        // Per-field kept/dropped state is NOT in the /diff payload — it
        // surfaces via the ``field_map`` matrix rendered above from the
        // parallel /rendered fetch (#1247 id 35).
        // ``expected_content`` (commands/agents, #1247 id 30) is what sync
        // would actually write — vendor override or rendered output — so the
        // diff shows the real pending change instead of a raw canonical-vs-
        // runtime compare (permanently red for TOML/YAML-rendering runtimes).
        // Skills/mcp-servers responses don't send it; fall back to canonical.
        const expected = rt.expected_content != null ? rt.expected_content : data.canonical_content;
        if (rt.status === 'out of sync' && expected != null && rt.runtime_content != null) {
          const ops = diffLines(expected, rt.runtime_content);
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
  // Shares the per-type detail controller with ``loadCtxDetail`` (#1286) — a
  // canonical mount racing a runtime-only mount aborts the loser's fetch.
  _ctxDetailAbort[type] = _ctxSwapAbort(_ctxDetailAbort[type]);
  const signal = _ctxDetailAbort[type]?.signal;
  detailEl.hidden = false;
  _ctxCurrentDetail = { type, name, runtimeOnly: true };
  panelLoading(detailEl);

  try {
    const res = await fetch(
      _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/diff`),
      { signal },
    );
    if (!res.ok) {
      throw new Error(_ctxErrDetail((await res.json().catch(() => ({}))).detail, `Failed to load ${name}`));
    }
    const data = await res.json();
    if (seq !== _ctxDetailSeq[type]) return;

    let html = `<div class="ctx-detail" role="region" aria-labelledby="ctx-detail-name-${type}">`;
    html += `<div class="ctx-detail-header">
      <h2 class="ctx-detail-name" id="ctx-detail-name-${type}" tabindex="-1">${escapeHtml(name)}</h2>
      ${_ctxBadge('missing canonical')}
    </div>`;
    html += `<div class="text-muted" style="margin:6px 0 12px">${t('settings.ctx.runtime_only_detail_hint')}</div>`;

    if (!data.runtimes || !data.runtimes.length) {
      html += `<div class="text-muted">${t('settings.ctx.no_artifacts_hint')}</div>`;
    } else {
      for (const rt of data.runtimes) {
        html += `<div style="margin-bottom:12px">`;
        html += `<strong>${escapeHtml(rt.runtime)}</strong> ${_ctxBadge(rt.status)}`;
        html += _ctxDiagnosticDetail(rt, data.canonical_path);
        if (rt.runtime_content != null) {
          html += `<pre class="ctx-content-pre" style="margin-top:6px">${escapeHtml(rt.runtime_content)}</pre>`;
        }
        html += '</div>';
      }
    }

    // ``type`` is always the plural section slug (skills/commands/agents);
    // the localized singular display name feeds the one-artifact CTA copy
    // ("Import this skill" / "이 스킬 가져오기"). Gated on the same capability
    // map as the section toolbar: mcp-servers has NO /import route, and once
    // runtime-only rows exist for it (#1247 id 31) an ungated button here
    // would 404 — the residual dead-import leg #1223's toolbar map missed.
    const caps = _CTX_TOOLBAR_CAPS[type] || {};
    if (caps.import !== false) {
      html += `<div class="ctx-edit-actions" style="margin-top:12px">
        <button class="btn-primary ctx-runtime-only-import" data-type="${escapeHtml(type)}">
          ${escapeHtml(t('settings.ctx.import_this').replace('{type}', _ctxTypeNameSingular(type)))}
        </button>
      </div>`;
    }

    html += '</div>';
    detailEl.innerHTML = html;
    if (opts.focusOnLoad) _ctxFocusDetail(detailEl);

    detailEl.querySelector('.ctx-runtime-only-import')?.addEventListener('click', async () => {
      const btn = detailEl.querySelector('.ctx-runtime-only-import');
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const importOnce = (extra) => fetch(
          _ctxWithTargetScope(`/api/context/${type}/${encodeURIComponent(name)}/import`),
          {
            method: 'POST',
            headers,
            body: JSON.stringify({ ...extra }),
          },
        );
        let r = await importOnce({});
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        let data = await r.json();
        if (_ctxIsHostWriteEnvelope(data)) {
          // #1263 user-tier single import (see the section Import above).
          r = await _ctxConfirmHostWrite(data, () => importOnce({ allow_host_writes: true }));
          if (!r) return;
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
            return;
          }
          data = await r.json();
        }
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
    // Aborted = a newer mount / scope switch superseded us (#1286); leave the
    // pane to the winning mount rather than painting an error.
    if (_ctxIsAbortError(err) || seq !== _ctxDetailSeq[type]) return;
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
        : t('settings.ctx.sync_disabled_tooltip').replace('{type}', _ctxTypeName(type));
      showToast(message, 'info');
      return;
    }
    // U4 (#1229): pin scope + tier ONCE at click time (the Import preview
    // pattern) and reuse the snapshot for the impact fetch AND the sync
    // POST, so the counts and the write can't disagree after a mid-flight
    // project/tier switch. The impact preview is best-effort — any failure
    // falls back to the count-only confirm; Sync is never blocked by it.
    const pinnedScopeOpts = {
      scopeId: _ctxEffectiveScopeId(_ctxActiveScopeId),
      scopeResolved: true,
      targetScope: _ctxTargetScope,
    };
    // Snapshot the count WITH the pin — a project/tier switch during the
    // preview fetch re-renders the section dataset, and the confirm must
    // describe the same (project, tier) the POST writes to (Codex review).
    const canonicalCount = section?.dataset.canonicalCount || '0';
    btnLoading(btn, true);
    let impact = null;
    try {
      const pr = await fetch(_ctxWithTargetScope(`/api/context/${type}`, pinnedScopeOpts));
      if (pr.ok) {
        const data = await pr.json();
        if (Array.isArray(data?.[type])) impact = _ctxSyncImpact(data[type]);
      }
    } catch {
      /* best-effort impact preview */
    } finally {
      btnLoading(btn, false);
    }
    let message = t('settings.ctx.confirm_sync', {
      type: _ctxTypeName(type),
      count: canonicalCount,
    });
    if (impact) message += ' ' + _ctxSyncImpactMessage(impact);
    const ok = await showConfirm({
      title: t('settings.ctx.sync'),
      message,
      warningText: _ctxSyncOverwriteWarning(impact),
      confirmText: t('settings.ctx.sync'),
      danger: false,
    });
    if (!ok) return;
    btnLoading(btn, true);
    try {
      const csrf = await ensureCsrfToken();
      const headers = csrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
        : { 'Content-Type': 'application/json' };
      const syncOnce = (extra) => fetch(
        _ctxWithTargetScope(`/api/context/${type}/sync`, pinnedScopeOpts),
        {
          method: 'POST',
          headers,
          // No-body POST stays the project-tier wire shape; the body only
          // appears on the #1263 confirmed user-tier leg.
          ...(extra ? { body: JSON.stringify(extra) } : {}),
        },
      );
      let r = await syncOnce(null);
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
        return;
      }
      let data = await r.json();
      if (_ctxIsHostWriteEnvelope(data)) {
        // #1263 user-tier sync: host_targets list the ~/.claude-family
        // fan-out destinations (parsed-name keyed, upper bound).
        r = await _ctxConfirmHostWrite(data, () => syncOnce({ allow_host_writes: true }));
        if (!r) return;
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        data = await r.json();
      }
      const generated = data.generated || [];
      const dropped = data.dropped || [];
      const skipped = data.skipped || [];
      const emptyCanonical = generated.length === 0
        && skipped.some(s => s && s.reason_code === 'no_canonical_root');
      const lockTimeout = skipped.some(s => s && s.reason_code === 'lock_timeout');
      const targetConflict = skipped.find(s => s && s.reason_code === 'target_conflict');
      if (emptyCanonical) {
        // ``{canonical}`` is a real path — keep the raw slug there.
        const msg = t('settings.ctx.sync_empty_canonical')
          .replace('{type}', _ctxTypeName(type))
          .replace('{canonical}', data.canonical_root || `.memtomem/${type}`);
        showToast(msg, 'info');
      } else if (lockTimeout) {
        // A foreign process held a destination lock past the engine's
        // acquisition budget — none (batch tier) or only part (per-dst
        // tiers) of the fan-out happened. Falling through to
        // ``sync_success`` would report a sync that didn't run.
        showToast(t('settings.ctx.sync_lock_timeout'), 'warning');
      } else if (targetConflict) {
        // A fan-out destination holds non-skill content the engine refuses
        // to overwrite — that destination was skipped (typed
        // ``target_conflict``) while the rest fanned out. Falling through
        // to ``sync_success`` would hide the skip; the backend reason names
        // the conflicting path and how to resolve it (kept raw — it carries
        // a real filesystem path).
        showToast(
          t('settings.ctx.sync_target_conflict')
            .replace('{reason}', targetConflict.reason || ''),
          'warning',
        );
      } else if (skipped.some(_ctxIsAttentionSkip)) {
        // Remaining failure-class skips — parse_error / unknown_runtime /
        // duplicate_name (#1247 id 21 + B4) / any future non-benign code —
        // previously fell through to ``sync_success``, reporting a sync
        // that silently left items behind. lock_timeout / target_conflict
        // can't reach here (dedicated branches above). Ranked above
        // ``dropped``: a whole item skipped outranks field-level loss.
        const items = [...new Set(
          skipped.filter(_ctxIsAttentionSkip).map(_ctxAttentionSkipLabel),
        )];
        showToast(
          t('settings.ctx.sync_skipped_attention', {
            count: items.length,
            items: items.join(', '),
          }),
          'warning',
        );
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
    // rank-10: run a dry-run preview first so the confirm names the
    // destination (active project · project_shared) and how many artifacts
    // would import vs already exist. The preview is best-effort — any failure
    // (offline, gateway-lock contention, a non-shared-tier 400) falls back to
    // the destination-only confirm so Import is never blocked by it.
    //
    // Pin the effective scope + tier ONCE at click time and reuse the snapshot
    // for the preview, the destination label, AND the final import POST. The
    // gateway UI isn't inert until the modal opens, so a mid-flight active-
    // project/tier change during the preview fetch could otherwise make the
    // preview counts, the named destination, and the real write disagree — the
    // same one-(project, tier)-per-invocation pinning Sync All enforces
    // (ADR-0016 §5 / ADR-0021 §C; Codex review).
    const pinnedScopeOpts = {
      scopeId: _ctxEffectiveScopeId(_ctxActiveScopeId),
      scopeResolved: true,
      targetScope: _ctxTargetScope,
    };
    btnLoading(btn, true);
    let preview = null;
    try {
      const previewCsrf = await ensureCsrfToken();
      const previewHeaders = previewCsrf
        ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': previewCsrf }
        : { 'Content-Type': 'application/json' };
      // ``_ctxWithTargetScope`` appends scope/tier with ``&`` since the URL
      // already carries ``?dry_run=1`` (it branches on ``url.includes('?')``).
      const pr = await fetch(
        _ctxWithTargetScope(`/api/context/${type}/import?dry_run=1`, pinnedScopeOpts),
        { method: 'POST', headers: previewHeaders, body: JSON.stringify({ overwrite: false }) },
      );
      if (pr.ok) {
        const data = await pr.json();
        // Only trust a well-shaped preview; a malformed / empty body falls back
        // to the destination-only confirm rather than a misleading "0 / 0".
        if (Array.isArray(data?.imported) && Array.isArray(data?.skipped)) preview = data;
      }
    } catch {
      /* best-effort preview — fall through to the destination-only confirm */
    } finally {
      btnLoading(btn, false);
    }
    const importDest = _ctxImportDestinationLabel(
      pinnedScopeOpts.scopeId, pinnedScopeOpts.targetScope,
    );
    let importMessage = t('settings.ctx.confirm_import', {
      type: _ctxTypeName(type),
      dest: importDest,
    });
    if (preview) {
      const wouldImport = preview.imported.length;
      // Only ``canonical_exists`` skips are what the Overwrite checkbox governs.
      // The skipped list also carries ``already_imported`` (cross-runtime
      // dedup), ``invalid_name``, parse errors, and privacy blocks — counting
      // those as "already exist" would misstate both the count and the overwrite
      // hint (Codex review). The other skips surface in the post-import result
      // panel, not this pre-commit summary.
      const wouldOverwrite = preview.skipped.filter(
        s => s && s.reason_code === 'canonical_exists',
      ).length;
      importMessage += ' ' + t('settings.ctx.confirm_import_preview', {
        imported: wouldImport,
        skipped: wouldOverwrite,
      });
      // The preview runs with overwrite:false, so ``wouldOverwrite`` is exactly
      // the set the Overwrite checkbox below would replace. Say so explicitly —
      // otherwise the "already exist" count reads as "will be skipped" even when
      // the user then enables Overwrite (rank-10 review).
      if (wouldOverwrite > 0) {
        importMessage += ' ' + t('settings.ctx.confirm_import_overwrite_hint');
      }
    }
    // Overwrite is opt-in: the default skip-when-canonical-exists rule
    // protects user-maintained canonicals from a stray Import wiping
    // them out with a stale runtime copy. The checkbox lets the user
    // explicitly say "yes, the runtime is the source of truth this
    // round" — used after editing in-place in ``~/.claude/skills/``
    // and wanting to flow that back into ``.memtomem/``.
    const result = await showConfirm({
      title: t('settings.ctx.import'),
      message: importMessage,
      confirmText: t('settings.ctx.import'),
      // Import is non-destructive by default (the destructive path is the
      // opt-in "overwrite" checkbox below) — keep the button primary-blue.
      danger: false,
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
      // Same click-time (project, tier) snapshot as the preview so the write
      // can't land somewhere the preview/label didn't describe.
      const importOnce = (extra) => fetch(
        _ctxWithTargetScope(`/api/context/${type}/import`, pinnedScopeOpts),
        {
          method: 'POST',
          headers,
          body: JSON.stringify({ overwrite, ...extra }),
        },
      );
      let r = await importOnce({});
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
        return;
      }
      let data = await r.json();
      if (_ctxIsHostWriteEnvelope(data)) {
        // #1263 user-tier import: the envelope nests the engine's dry-run
        // under ``plan``; host_targets are the would-import canonical
        // destinations under ~/.memtomem/. Second dialog after the impact
        // confirm above — the first names counts, this one names paths.
        r = await _ctxConfirmHostWrite(data, () => importOnce({ allow_host_writes: true }));
        if (!r) return;
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        data = await r.json();
      }
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
          .replace('{type}', _ctxTypeName(type))
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

// Label-based destination for the Create form — "{active project} · {tier}".
// Built from the active scope label + selected tier (not a reconstructed FS
// path) so it can't drift from the server's canonicalization; it just tells the
// user WHERE the new artifact will land before they submit (it POSTs to the
// active scope + ``_ctxTargetScope``).
function _ctxCreateDestinationLabel() {
  const activeScope = (_ctxProjectsCache || []).find(
    s => (s.scope_id || '') === (_ctxActiveScopeId || ''),
  );
  const project = activeScope
    ? _ctxScopeDisplayLabel(activeScope)
    : t('settings.ctx.create_dest_active_project');
  const tierKey = _ctxTargetScope === 'user'
    ? 'settings.ctx.tier_option_user'
    : _ctxTargetScope === 'project_local'
      ? 'settings.ctx.tier_option_project_local'
      : 'settings.ctx.tier_option_project_shared';
  return t('settings.ctx.create_destination', { project, tier: t(tierKey) });
}

// Destination label for the Import confirm — "{project} · {tier}".
// Takes the click-time pinned ``scopeId`` so the named destination matches the
// project the preview + final POST target (not a live read that could drift if
// the active project changes mid-flight). Import only ever writes to the
// ``project_shared`` canonical tier (non-shared tiers are rejected server-side
// AND ``.ctx-import-btn`` rides the write-block sweep, so it's only clickable on
// project_shared), so the tier is fixed — naming it answers rank-10's "Import
// hides the destination tier" gap. Returns just "{project} · {tier}" (no
// "Destination:" prefix) so it reads inline inside the confirm sentence.
function _ctxImportDestinationLabel(scopeId = _ctxActiveScopeId, targetScope = _ctxTargetScope) {
  // Name the tier the pinned POST actually writes (Codex review — the
  // hardcoded shared label lied once #1263 opened the user tier). The
  // user tier is global (~/.memtomem), not per-project, so naming the
  // active project there would claim a residency the import doesn't have.
  if (targetScope === 'user') return t('settings.ctx.tier_option_user');
  const tierKey = targetScope === 'project_local'
    ? 'settings.ctx.tier_option_project_local'
    : 'settings.ctx.tier_option_project_shared';
  const project = _ctxScopeDisplayLabelById(scopeId);
  return `${project} · ${t(tierKey)}`;
}

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
      <div class="ctx-create-destination">${escapeHtml(_ctxCreateDestinationLabel())}</div>
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
        const createOnce = (extra) => fetch(_ctxWithTargetScope(`/api/context/${type}`), {
          method: 'POST',
          headers,
          body: JSON.stringify({ name: nameInput, content, ...extra }),
        });
        let r = await createOnce({});
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
          return;
        }
        const created = await r.json().catch(() => ({}));
        if (_ctxIsHostWriteEnvelope(created)) {
          // #1263 user-tier create: canonical lands under ~/.memtomem/.
          r = await _ctxConfirmHostWrite(created, () => createOnce({ allow_host_writes: true }));
          if (!r) return;
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            showToast(_ctxErrDetail(err.detail, t('toast.request_failed')), 'error');
            return;
          }
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

/**
 * Context Gateway — Skills / Commands / Agents CRUD, diff, sync, import.
 *
 * Depends on globals from app.js: qs, show, hide, escapeHtml, t, showConfirm,
 * showToast, panelLoading, btnLoading, emptyState, diffLines, renderDiff,
 * switchSettingsSection.  Loaded AFTER app.js in index.html.
 *
 * #1517: this module ships as SEVEN ordered classic-script fragments
 * (context-gateway-{core,controls,overview,list,conflict,detail,actions}.js).
 * They are a pure line-partition of the former single file — no ES modules, no
 * wrappers; every fragment shares the page's global lexical env and loads in
 * order before context-portal.js / wiki.js (which consume gateway globals).
 *
 * Part 1/7 — core. Status/badge helpers, Overview state, and Simple mode.
 *   depends on: app.js globals (above)
 *   provides:   the shared mutable state (_ctxTargetScope, _CTX_ACTIVE_SCOPE_KEY,
 *               _ctxActiveScopeId, _ctxProjectsCache, _ctxOverviewCache, ...),
 *               scope helpers (_ctxScopeParam, _ctxScopeDisplayLabel[ById],
 *               _ctxScopeIsActive, _ctxNormalizeActiveScope, _ctxFetchProjects,
 *               _ctxBumpActiveScopeDetailSeq — consumed by context-portal.js /
 *               wiki.js), and Simple-mode (_ctxApplySimpleMode, runs at load).
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

// Localized toast copy for a single-item import skip (#1646 item 2). The skip
// payload carries a stable ``reason_code`` from the closed set in
// ``memtomem/context/_skip_reasons.py`` (import codes: already_imported /
// canonical_exists / invalid_name / parse_error / toml_parse_error /
// privacy_blocked / privacy_blocked_project_shared), so map code→i18n the
// same way ``_ctxErrDetail`` does for sync reason codes (#1350). The raw
// backend ``reason`` stays as the fallback for codes without copy — a future
// engine code degrades to today's behavior (visible English), never to a
// silent toast.
function _ctxImportSkipText(skip) {
  const code = skip && skip.reason_code;
  // The shared-tier privacy block already has user-facing remediation copy —
  // reuse it rather than duplicating the wording under an import_skip_* key.
  if (code === 'privacy_blocked_project_shared') {
    return t('settings.ctx.privacy_blocked_shared_hint');
  }
  if (code) {
    const key = `settings.ctx.import_skip_${code}`;
    const label = t(key);
    if (label !== key) return label;
  }
  return (skip && skip.reason) || t('toast.request_failed');
}

// Display label for a runtime key. Branded names for the full runtime set
// (#1646 item 3): raw ids next to "Antigravity" read as a mixed register
// ("claude", "kimi" chips beside a brand name), and the glossary tooltips
// already say "Claude Code, Codex, Kimi". ``gemini`` maps to the Antigravity
// client (RUNTIME_TO_CLIENT: gemini→antigravity). The on-disk marker paths
// (.gemini/, .claude/, …) keep the raw keys — this maps the *label* only;
// diagnostic tooltips keep the raw id (see ``renderRuntimeBadges``). Proper-
// noun product names are identical across locales, so intentionally not i18n
// (same rationale as ``_ctxPortalRuntimeLabel``).
const _CTX_RUNTIME_LABEL = {
  claude: 'Claude Code',
  gemini: 'Antigravity',
  codex: 'Codex',
  kimi: 'Kimi',
};
function _ctxRuntimeLabel(name) {
  return _CTX_RUNTIME_LABEL[name] || name;
}

function renderRuntimeBadges(runtimes) {
  if (!runtimes || !runtimes.length) return '';
  return '<div class="ctx-runtime-badges">' +
    runtimes.map(r => {
      // U7 (#1229): surface the server-sanitized diagnostic reason as a
      // tooltip on the list-card badge — the leaf pane carries the full
      // rendering; the card gets the at-a-glance cause. The tooltip keeps
      // the raw generator id (diagnostic surface); the visible label routes
      // through ``_ctxImpactRuntimeLabel`` so the MCP fan-out's internal
      // ``project_mcp`` renders as its target ".mcp.json" — the same name
      // the Sync button uses — instead of leaking the id (#1646 item 3).
      const title = r.reason ? `${r.runtime} — ${r.reason}` : r.runtime;
      return `<span class="ctx-runtime-badge ${_ctxStatusCls[r.status] || ''}" title="${escapeHtml(title)}">${escapeHtml(_ctxImpactRuntimeLabel(r.runtime))}: ${escapeHtml(_ctxStatusText(r.status))}</span>`;
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

// B-5 (#1288): per-type × per-runtime breakdown for the Sync All confirms.
// Same (runtime, name) FILE-copy semantics as ``_ctxSyncImpact`` above —
// 'missing target' → create, 'out of sync' → overwrite; every other status is
// not a sync write and never contributes. The breakdown is ADDITIVE:
// ``_ctxSyncImpact`` / ``_ctxSyncImpactMessage`` stay the per-type Sync button's
// copy AND the Sync All degraded fallback, so this never changes their output.
const _CTX_SYNC_BREAKDOWN_CAP = 4;
const _CTX_SYNC_BREAKDOWN_SEP = ' · ';
// Artifact render order — the ``_CTX_SYNC_PHASES`` order minus settings, which
// has no per-runtime list payload and rides the separate counts-only
// ``confirm_sync_settings_impact`` segment.
const _CTX_SYNC_BREAKDOWN_TYPES = ['skills', 'commands', 'agents', 'mcp-servers'];

// Title-case the first character of a type label for a segment head. No-op for
// non-Latin scripts (Korean labels pass through unchanged).
function _ctxCapitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// Per (type, action) segments: [{type, action:'create'|'overwrite', count,
// runtimes:[sorted display labels]}] in a deterministic type×action order.
// ``listsByType`` maps each artifact type to its /api/context/<type> list body.
function _ctxSyncBreakdownSegments(listsByType) {
  const segments = [];
  for (const type of _CTX_SYNC_BREAKDOWN_TYPES) {
    const create = { count: 0, runtimes: new Set() };
    const overwrite = { count: 0, runtimes: new Set() };
    for (const item of listsByType[type] || []) {
      for (const rt of item.runtimes || []) {
        if (rt.status === 'missing target') {
          create.count += 1;
          create.runtimes.add(_ctxImpactRuntimeLabel(rt.runtime));
        } else if (rt.status === 'out of sync') {
          overwrite.count += 1;
          overwrite.runtimes.add(_ctxImpactRuntimeLabel(rt.runtime));
        }
      }
    }
    if (create.count > 0) {
      segments.push({
        type, action: 'create', count: create.count, runtimes: [...create.runtimes].sort(),
      });
    }
    if (overwrite.count > 0) {
      segments.push({
        type, action: 'overwrite', count: overwrite.count, runtimes: [...overwrite.runtimes].sort(),
      });
    }
  }
  return segments;
}

// Render the capped breakdown line: up to ``_CTX_SYNC_BREAKDOWN_CAP`` segments
// plus a localized "…and N more" when truncated. Returns '' for an empty
// breakdown so callers can omit the line entirely. Plural/count variants derive
// live from the ``{count}`` param (i18n checklist) — the modal is rebuilt per
// click, so no langchange re-render is needed.
function _ctxSyncBreakdownMessage(segments, cap = _CTX_SYNC_BREAKDOWN_CAP) {
  if (!segments || !segments.length) return '';
  const shown = segments.slice(0, cap);
  const parts = shown.map((s) =>
    t(`settings.ctx.confirm_sync_breakdown_${s.action}`, {
      type: _ctxCapitalize(_ctxTypeName(s.type)),
      count: s.count,
      runtimes: s.runtimes.join(', '),
    }),
  );
  const omitted = segments.length - shown.length;
  if (omitted > 0) {
    parts.push(t('settings.ctx.confirm_sync_breakdown_more', { count: omitted }));
  }
  return parts.join(_CTX_SYNC_BREAKDOWN_SEP);
}

// Best-effort Sync All preview under a pinned (project, tier). Never throws —
// returns the richest tier the network allowed:
//   haveBreakdown : all four artifact lists loaded → per-type×per-runtime
//                   segments + aggregate totals from those lists.
//   haveCounts    : at least the overview loaded → aggregate artifact + settings
//                   counts (no per-runtime split — the degraded copy).
//   neither       : caller keeps the base confirm.
// (#1288 / Codex B: the portal card may target a NON-active scope, so this is a
// pure enhancement — any list 404/abort/parse failure drops to the overview
// counts the portal already rendered; an overview failure drops to the base
// copy. ``Promise.allSettled`` keeps one failed list from erasing all counts.)
async function _ctxSyncAllPreview(pinnedScopeOpts) {
  const urls = [
    ..._CTX_SYNC_BREAKDOWN_TYPES.map((typ) =>
      _ctxWithTargetScope(`/api/context/${typ}`, pinnedScopeOpts)),
    _ctxWithTargetScope('/api/context/overview', pinnedScopeOpts),
  ];
  const settled = await Promise.allSettled(urls.map((u) => fetch(u)));
  const bodyOf = async (i) => {
    const r = settled[i];
    if (r.status !== 'fulfilled' || !r.value || !r.value.ok) return null;
    try {
      return await r.value.json();
    } catch {
      return null;
    }
  };
  const listBodies = await Promise.all(_CTX_SYNC_BREAKDOWN_TYPES.map((_, i) => bodyOf(i)));
  const overview = await bodyOf(_CTX_SYNC_BREAKDOWN_TYPES.length);

  const preview = {
    segments: [],
    totals: { create: 0, overwrite: 0 },
    settings: { create: 0, overwrite: 0 },
    haveBreakdown: false,
    haveCounts: false,
  };

  // The overview is the floor: it is the ONLY source of settings counts
  // (settings have no per-runtime list payload). Without it, an artifact-only
  // breakdown would silently omit settings writes and under-count the overwrite
  // warning, so a missing overview drops to the base copy — matching the
  // pre-#1288 all-or-nothing behavior (Codex impl-review Major). The four lists
  // then add the per-type breakdown on top of that floor.
  if (!overview) return preview;
  preview.haveCounts = true;
  if (overview.settings) {
    preview.settings = {
      create: overview.settings.missing_target || 0,
      overwrite: overview.settings.out_of_sync || 0,
    };
  }

  const listsAllOk = listBodies.every(
    (b, i) => b && Array.isArray(b[_CTX_SYNC_BREAKDOWN_TYPES[i]]),
  );
  if (listsAllOk) {
    const listsByType = {};
    const allItems = [];
    _CTX_SYNC_BREAKDOWN_TYPES.forEach((typ, i) => {
      const items = listBodies[i][typ] || [];
      listsByType[typ] = items;
      allItems.push(...items);
    });
    preview.segments = _ctxSyncBreakdownSegments(listsByType);
    const agg = _ctxSyncImpact(allItems);
    preview.totals = { create: agg.create, overwrite: agg.overwrite };
    preview.haveBreakdown = true;
  } else {
    // Degraded: a list 404/abort/parse failure → aggregate per-type artifact
    // counts from the overview (no per-runtime split). Overview keys are
    // underscore-cased (``mcp_servers``) where the list routes are hyphenated.
    for (const key of ['skills', 'commands', 'agents', 'mcp_servers']) {
      const d = overview[key];
      if (!d) continue;
      preview.totals.create += d.missing_target || 0;
      preview.totals.overwrite += d.out_of_sync || 0;
    }
  }
  return preview;
}

// Compose the Sync All confirm copy + overwrite warning from a preview. Shared
// by the dashboard Sync All and the portal per-project card so the two dialogs
// stay byte-identical (#1288). ``baseMessage`` is the pre-built
// ``confirm_sync_all`` lead naming the destination.
function _ctxSyncAllConfirmCopy(baseMessage, preview) {
  let message = baseMessage;
  let warningText = '';
  if (!preview || !preview.haveCounts) {
    return { message, warningText };
  }
  const aCreate = preview.totals.create || 0;
  const aOverwrite = preview.totals.overwrite || 0;
  const sCreate = preview.settings.create || 0;
  const sOverwrite = preview.settings.overwrite || 0;
  if (aCreate + aOverwrite + sCreate + sOverwrite === 0) {
    // Combined artifact + settings no-op — a bare "already in sync" (an
    // artifact-only no-change line followed by a settings segment would
    // contradict itself).
    message += ' ' + t('settings.ctx.confirm_sync_no_changes');
  } else {
    if (aCreate + aOverwrite > 0) {
      // Uncapped totals lead, then the capped per-type×per-runtime breakdown
      // (only when all four lists loaded). Degraded previews keep totals only.
      message += ' ' + t('settings.ctx.confirm_sync_counts', {
        create: aCreate,
        overwrite: aOverwrite,
      });
      if (preview.haveBreakdown) {
        const breakdown = _ctxSyncBreakdownMessage(preview.segments);
        if (breakdown) message += ' ' + breakdown;
      }
    }
    if (sCreate + sOverwrite > 0) {
      message += ' ' + t('settings.ctx.confirm_sync_settings_impact', {
        create: sCreate,
        overwrite: sOverwrite,
      });
    }
  }
  const totalOverwrite = aOverwrite + sOverwrite;
  if (totalOverwrite > 0) {
    warningText = t('settings.ctx.confirm_sync_overwrite_warning', {
      overwrite: totalOverwrite,
    });
  }
  return { message, warningText };
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
      const candidates = Array.isArray(item.duplicate_candidates) ? item.duplicate_candidates : [];
      const source = item.source_runtime;
      const provenance = source
        ? `<span class="ctx-import-source">${escapeHtml(t('settings.ctx.import_selected_runtime', { runtime: source }))}</span>`
        : '';
      const duplicates = candidates.length > 1
        ? `<span class="ctx-import-duplicates">${escapeHtml(t('settings.ctx.import_duplicate_candidates', { runtimes: candidates.join(', ') }))}</span>`
        : '';
      html += `<div class="ctx-import-item"><span class="badge badge-success">${escapeHtml(item.name)}</span>${provenance}${duplicates}</div>`;
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
    // #1409: the per-type sync privacy 422 hoists ``reason_code`` to a top-level
    // sibling of its path-free string ``detail``. Map it to the localized sync
    // hint here so the Sync All per-phase summary localizes the block the same
    // way the per-row Sync button does (``_ctxSyncErrToast``), instead of
    // surfacing the raw locale-unaware English ``detail``.
    if (err && err.reason_code === 'privacy_blocked') {
      return t('settings.ctx.privacy_blocked_shared_sync_hint');
    }
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

// -- Simple mode (ADR-0026 P1a, #1353) ----------------------------------------
// Progressive-disclosure flag. Simple hides the four-axis control bar + the
// section nav and renders the Overview as a one-line verdict + per-type rows.
// P1b: each fixable row runs Sync/Import inline (same confirm flow as Advanced);
// clean rows show a check, and rows with no safe one-click fix keep the Manage
// deep-link into Advanced. Advanced — reachable via the toggle (the reversible
// rollback) — restores today's UI verbatim. Persisted like the active-scope
// flag; Simple is the default-when-unset since the D-F flip, so power users who
// prefer Advanced toggle once and stay there.
const _CTX_SIMPLE_MODE_KEY = 'memtomem_ctx_simple_mode';
// D-F flip switch point (ADR-0026 §"Implementation status"). This is the default
// used WHEN NO value is stored. Flipped to ``true`` 2026-06-18 — Simple is the
// default-when-unset — as a REVERSIBLE experiment: the §Validation naive user
// test was skipped as impractical (6 naive participants out of reach), so the
// rollback is the safety net — the Advanced toggle (per-user, persisted) and
// setting this back to ``false`` (global). P2 (the irreversible Push/Pull
// re-frame) stays deferred and still needs genuine naive evidence.
const _CTX_SIMPLE_DEFAULT = true;
let _ctxSimpleMode = _CTX_SIMPLE_DEFAULT;
try {
  const stored = localStorage.getItem(_CTX_SIMPLE_MODE_KEY);
  _ctxSimpleMode = stored !== null ? stored === '1' : _CTX_SIMPLE_DEFAULT;
} catch {
  _ctxSimpleMode = _CTX_SIMPLE_DEFAULT;
}

// Toggle the ``.ctx-simple`` class on the gateway tab (CSS hides the nav +
// control bar + tile grid) and keep the toggle button's ``aria-pressed`` in
// sync. Idempotent — safe to call on load and on every flip.
function _ctxApplySimpleMode() {
  const tab = document.getElementById('tab-context-gateway');
  if (tab) tab.classList.toggle('ctx-simple', _ctxSimpleMode);
  const toggle = document.getElementById('ctx-mode-toggle');
  if (toggle) {
    toggle.setAttribute('aria-pressed', _ctxSimpleMode ? 'true' : 'false');
    toggle.textContent = t(_ctxSimpleMode ? 'settings.ctx.open_advanced' : 'settings.ctx.back_to_simple');
  }
  const chip = document.getElementById('ctx-simple-active-chip');
  if (chip) {
    chip.textContent = t('settings.ctx.active_store_chip', {
      project: _ctxScopeDisplayLabelById(_ctxActiveScopeId),
      tier: t(`settings.ctx.tier_option_${_ctxTargetScope}`),
    });
  }
}

// Flip Simple mode, persist, and re-apply the class. Callers staying on the
// Overview re-render the body (loadCtxOverview) so the grid / simple-rows swap;
// ``_ctxOpenInAdvanced`` skips that since ``switchSettingsSection`` repaints
// the section it navigates to.
function _ctxSetSimpleMode(on) {
  _ctxSimpleMode = !!on;
  try {
    localStorage.setItem(_CTX_SIMPLE_MODE_KEY, _ctxSimpleMode ? '1' : '0');
  } catch { /* private-mode / disabled storage — in-memory flag still applies */ }
  _ctxApplySimpleMode();
}

// Leave Simple mode and (optionally) deep-link into the Advanced section that
// owns an artifact type, where the full Sync/Import/create/edit/delete controls
// live. Used by the Manage buttons (rows with no safe one-click fix) and the
// empty-state CTA; P1b's inline Sync/Import act in place without leaving Simple.
function _ctxOpenInAdvanced(section) {
  _ctxSetSimpleMode(false);
  if (section) {
    switchSettingsSection(section);
  } else {
    loadCtxOverview();
  }
}

// Apply the persisted flag once at load. The script tag sits at the end of
// ``index.html`` so the gateway tab markup already exists; the tab itself is
// ``hidden`` until activated, so toggling the class now causes no flash.
// The toggle label and active chip are written with ``t()``, and this call
// runs before ``I18N.init()``'s locale fetch resolves — so their first paint
// is the raw-key fallback. ``init`` dispatches ``langchange`` once the cache
// is populated, and the listener below re-renders them (and again on every
// locale flip — neither element carries ``data-i18n``, so ``applyDOM`` can't).
_ctxApplySimpleMode();
window.addEventListener('langchange', _ctxApplySimpleMode);

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
  if (_ctxScopeIsServerCwd(scope)) {
    // Show the cwd folder's real identity + a "(current folder)" marker
    // rather than masking every server-cwd scope as a flat "Server CWD"
    // (which gave the same directory two names — one here, one under its
    // basename as a known project, #1353). A user/stored label wins;
    // otherwise the cwd basename. The backend auto-labels an unnamed cwd
    // with the literal "Server CWD" (projects.py) and the offline fallback
    // (``_ctxServerCwdFallback``, root='') uses the localized server_cwd
    // string — treat both as "no real name" and only then fall back to the
    // localized "Server CWD".
    const generic = !scope.label
      || scope.label === 'Server CWD'
      || scope.label === t('settings.ctx.server_cwd');
    const name = generic ? _ctxBasename(scope.root) : scope.label;
    if (!name) return t('settings.ctx.server_cwd');
    return `${name} ${t('settings.ctx.cwd_marker')}`;
  }
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
    const include = opts.includeCounts === false
      ? ''
      : (opts.includeCoverage ? 'counts,runtime_coverage' : 'counts');
    const query = include ? `?include=${include}` : '';
    const res = await fetch(_ctxWithTargetScope(`/api/context/projects${query}`, { includeScope: false, targetScope: opts.targetScope }), { signal: opts.signal });
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
  // The Simple-mode active chip names the active project via the cache this
  // commit just (possibly) rewrote — at boot it rendered the Server-CWD
  // fallback against an empty cache, so re-render it now that the roster and
  // the normalized active scope are known.
  _ctxApplySimpleMode();
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

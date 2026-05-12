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
    html += `<h4 style="margin-top:8px">Skipped</h4>`;
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
    if (typeof detail === 'string' && detail) return detail;
    // Defensive fallback for structured payloads shaped like `{detail: "..."}`.
    if (detail && typeof detail === 'object' && typeof detail.detail === 'string' && detail.detail) {
      return detail.detail;
    }
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

function _ctxTargetScopeParam() {
  if (_ctxTargetScope === 'project_shared') return '';
  return `target_scope=${encodeURIComponent(_ctxTargetScope)}`;
}

function _ctxWithTargetScope(url) {
  const param = _ctxTargetScopeParam();
  if (!param) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${param}`;
}

function _ctxTierControls(type) {
  return `<div class="ctx-tier-filter" data-type="${escapeHtml(type)}" role="group" aria-label="${escapeHtml(t('settings.ctx.tier_filter'))}">
    <button type="button" data-scope="user" class="${_ctxTargetScope === 'user' ? 'active' : ''}">user</button>
    <button type="button" data-scope="project_shared" class="${_ctxTargetScope === 'project_shared' ? 'active' : ''}">project_shared</button>
    <button type="button" data-scope="project_local" class="${_ctxTargetScope === 'project_local' ? 'active' : ''}">project_local</button>
  </div>`;
}

function _ctxWireTierControls() {
  document.querySelectorAll('.ctx-tier-filter button').forEach(btn => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.scope;
      if (!next || next === _ctxTargetScope) return;
      _ctxTargetScope = next;
      // Update write-blocked affordances synchronously so the user sees
      // the dim/banner change immediately, before the async list refetch
      // settles. ``loadCtxList`` / ``loadCtxOverview`` re-apply on success
      // (their callees call ``_ctxRefreshWriteBlockedState`` post-render).
      _ctxRefreshWriteBlockedState();
      const type = btn.closest('.ctx-tier-filter')?.dataset.type || '';
      if (type === 'overview') {
        loadCtxOverview();
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
  if (!section && !filter && !artifact) return null;
  return {
    section,
    filter: _CTX_DEEP_LINK_FILTERS.has(filter) ? filter : '',
    artifact,
  };
}

function _ctxBuildDeepLinkUrl({ section, filter, artifact }) {
  // Build the URL by mutating the *current* URL's search params rather
  // than constructing a fresh string — preserves any unrelated query
  // params the SPA might be using (or future feature might add) and
  // keeps the path/hash intact.
  const url = new URL(window.location.href);
  url.searchParams.delete('section');
  url.searchParams.delete('filter');
  url.searchParams.delete('artifact');
  if (section) url.searchParams.set('section', section);
  if (filter) url.searchParams.set('filter', filter);
  if (artifact) url.searchParams.set('artifact', artifact);
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
  _ctxSetDeepLink({ section: '', filter: '', artifact: '' });
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

  // Custom Commands sidebar leaf is dev-tier (Anthropic merged
  // .claude/commands/ into Skills); the overview tile mirrors that —
  // surfacing it in prod would let users click through and trigger
  // the dev-only-section toast in switchSettingsSection.
  const types = [
    { key: 'skills',   label: t('settings.ctx.skills_title'),   section: 'ctx-skills' },
    { key: 'commands', label: t('settings.ctx.commands_title'), section: 'ctx-commands', devOnly: true },
    { key: 'agents',   label: t('settings.ctx.agents_title'),   section: 'ctx-agents' },
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

  // Issue #832 / ADR-0009 §1.c: surface "Last sync: 5 min ago" sourced from
  // canonical-source mtime. Suppress the line when the backend returns null
  // (fresh / empty project — no canonical files yet); rendering "Last sync:
  // never" or epoch-zero would be more confusing than silent absence. The
  // raw ISO timestamp is exposed via ``title=`` on the row for the
  // copy/diagnose case (mtime drift, timezone weirdness) — the relative
  // form alone hides the absolute value that's sometimes what the user
  // actually needs.
  const lastSyncedAt = typeof data.last_synced_at === 'string' && data.last_synced_at
    ? data.last_synced_at
    : '';
  let lastSyncHtml = '';
  if (lastSyncedAt) {
    const rel = escapeHtml(relativeTime(lastSyncedAt));
    const iso = escapeHtml(lastSyncedAt);
    lastSyncHtml = `<div class="ctx-overview-last-sync" title="${iso}">
        <span class="ctx-overview-last-sync-label">${escapeHtml(t('settings.ctx.last_synced_label'))}</span>
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
  html += _ctxTierControls('overview');
  html += '<div class="ctx-overview-grid">';
  for (const typ of types) {
      if (typ.devOnly && STATE.uiMode !== 'dev') continue;
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
      html += `<div class="ctx-overview-stat" data-section="${typ.section}" data-tile-key="${typ.key}">
        <div class="ctx-overview-count">${total}</div>
        <div class="ctx-overview-label">${escapeHtml(typ.label)}</div>
        <div class="ctx-overview-badge"><span class="badge ${badgeCls}">${escapeHtml(badgeText)}</span></div>
        ${pointersHtml}
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;
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
    } else {
      // Mirror the overview-tile gate: in prod the Custom Commands
      // surface is hidden, so its missing_canonical count must not
      // gate the Sync All button (otherwise prod users would see Sync
      // All disabled because of an artifact set they can't even see).
      const syncKinds = STATE.uiMode === 'dev'
        ? ['skills', 'commands', 'agents']
        : ['skills', 'agents'];
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
  el.querySelectorAll('.ctx-overview-stat').forEach(card => {
    const tileKey = card.dataset.tileKey;
    const tileData = tileKey ? (data[tileKey] || {}) : null;
    const filter = tileData ? _ctxTileDominantFilter(tileData) : null;
    const section = card.dataset.section;
    card.addEventListener('click', () => {
      if (filter) {
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
  const el = qs('ctx-overview-content');
  panelLoading(el);
  try {
    const res = await fetch(_ctxWithTargetScope('/api/context/overview'));
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to load overview');
    const data = await res.json();
    if (seq !== _ctxOverviewSeq) return;
    _ctxOverviewCache = data;
    _renderCtxOverview(data);
  } catch (err) {
    if (seq !== _ctxOverviewSeq) return;
    _ctxOverviewCache = null;
    el.innerHTML = emptyState('', 'Failed to load overview', err.message);
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
    // ``_renderCtxOverview`` sets ``dataset.runtimeOnly='true'`` in two
    // cases: (1) project_local tier (canonical drafts have no fan-out)
    // and (2) all-canonicals-empty for any tier. Mirror its tier-aware
    // tooltip choice here so an EN→KO→EN locale flip doesn't revert the
    // hover text to the wrong copy for project_local. User-tier writes
    // are gated by ``_ctxRefreshWriteBlockedState`` below — that path
    // owns the user-tier tooltip refresh now (#943).
    btn.title = _ctxTargetScope === 'project_local'
      ? t('settings.ctx.project_local_no_fanout_tooltip')
      : t('settings.ctx.sync_all_disabled_tooltip');
  }
  // Re-translate write-blocked button tooltips on every locale flip so
  // the dim button's hover copy stays consistent with the active
  // locale. The banner text (set via ``textContent`` inside
  // ``loadCtxList``) is re-rendered by the ``loadCtxList`` re-issue
  // below — no separate handling needed.
  _ctxRefreshWriteBlockedState();
  const settingsTab = document.getElementById('tab-settings');
  if (!settingsTab || !settingsTab.classList.contains('active')) return;

  const overviewSection = document.getElementById('settings-ctx-overview');
  if (overviewSection && overviewSection.classList.contains('active')) {
    if (_ctxOverviewCache) {
      _renderCtxOverview(_ctxOverviewCache);
    } else {
      loadCtxOverview();
    }
    // The Context Gateway sub-sections are mutually exclusive — if the
    // overview is active, none of the per-type list sections can be.
    return;
  }

  for (const type of ['skills', 'commands', 'agents']) {
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
            const newCanonPane = detailEl.querySelector('#ctx-pane-canonical');
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

// Sync All button
document.getElementById('ctx-sync-all-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('ctx-sync-all-btn');
  if (btn.dataset.runtimeOnly === 'true') {
    showToast(t('settings.ctx.sync_all_disabled_tooltip'),
      'info');
    return;
  }
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all'),
    message: t('settings.ctx.confirm_sync_all'),
    confirmText: t('settings.ctx.sync'),
  });
  if (!ok) return;
  btnLoading(btn, true);
  try {
    const csrf = await ensureCsrfToken();
    const headers = csrf
      ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
      : { 'Content-Type': 'application/json' };
    // Skip the dev-tier Custom Commands surface in prod — calling its
    // backend route still 200s, but Sync All advertises itself as
    // synchronizing what the user can see, and the prod sidebar /
    // overview no longer expose ``ctx-commands``.
    const types = STATE.uiMode === 'dev'
      ? ['skills', 'commands', 'agents']
      : ['skills', 'agents'];
    for (const typ of types) {
      const resp = await fetch(
        _ctxWithTargetScope(`/api/context/${typ}/sync`),
        { method: 'POST', headers },
      );
      if (!resp.ok) {
        throw new Error(await _ctxErrorMessageFromResponse(resp, `Sync ${typ} failed`));
      }
    }
    // Settings hooks sync (additive merge) — appends memtomem-owned hook
    // entries to ~/.claude/settings.json without clobbering user-authored
    // entries. Promoted from dev-only via RFC #761 (ADR-0001 §5 criteria
    // + HTTP-layer test fixtures).
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
    const settingsResp = await fetch('/api/context/settings/sync', { method: 'POST', headers });
    if (!settingsResp.ok) {
      throw new Error(await _ctxErrorMessageFromResponse(settingsResp, 'Settings sync failed'));
    }
    const settingsData = await settingsResp.json().catch(() => ({}));
    const settingsResults = settingsData.results || [];
    const firstWithStatus = (s) => settingsResults.find(r => r && r.status === s);
    const errored = firstWithStatus('error');
    const aborted = firstWithStatus('aborted');
    const needsConfirmation = firstWithStatus('needs_confirmation');
    if (errored) {
      showToast(t('toast.sync_failed', { error: errored.reason || '' }), 'error');
    } else if (aborted) {
      showToast(t('settings.ctx.mtime_conflict'), 'warning');
    } else if (needsConfirmation) {
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
    loadCtxOverview();
  } catch (err) {
    showToast(t('toast.sync_failed', { error: err.message }), 'error');
  } finally { btnLoading(btn, false); }
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
let _ctxListSeq = { skills: 0, commands: 0, agents: 0 };

// Sibling guard for ``loadCtxDetail`` and ``_ctxLoadRuntimeOnlyDetail``
// races. Both write to the same ``detailEl``, so they share one
// per-type counter. Rapid langchange / card-click bursts can put
// multiple detail fetches in flight; the guard ensures only the newest
// response paints into the live DOM, both for the locale-stale window
// and the Edit-mode buffer-restore race (where an older fetch would
// otherwise overwrite a textarea that the listener just rehydrated).
let _ctxDetailSeq = { skills: 0, commands: 0, agents: 0 };

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
    html += `<div class="${cardClass}" data-name="${escapeHtml(item.name)}"${canonAttr} data-out-of-sync="${outOfSync}"${statusesAttr}>
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
    if (!_ctxScopeIsServerCwd(scope)) params.set('scope_id', scope.scope_id);
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
    // Cards on the cwd scope are clickable across all tiers — the detail /
    // rendered / diff / edit / delete endpoints now accept ``target_scope=``
    // (#940 r3), so a click on a project_local draft opens the project_local
    // canonical, not a same-named project_shared one. Writes on non-shared
    // tiers are rejected at the server with HTTP 400 (the route's
    // ``_reject_non_shared_write`` helper); the JS surfaces those as
    // toasts via the existing ``err.detail`` path.
    const clickable = _ctxScopeIsServerCwd(scope);
    container.innerHTML = _ctxRenderItemsHtml(
      items,
      type,
      scope.root,
      data.scanned_dirs || [],
      { clickable },
    );

    if (_ctxScopeIsServerCwd(scope)) {
      // Only the cwd is mutable, so its canonical/runtime split drives the
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
        });
      }

      // ADR-0009 §3 deep-link applier. Runs only on the cwd group — the
      // dashboard's tile counts roll up the cwd canonical/runtime split,
      // so a deep-link landing on a non-cwd scope's container would point
      // at a list the user did not click into. Non-cwd groups stay
      // unfiltered and lazy-loaded as today.
      _ctxApplyDeepLinkToContainer(type, container);
    }
  } catch (err) {
    // Late-failing fetch from a previous invocation must not paint
    // ``emptyState`` over the fresh container the newer ``loadCtxList``
    // rebuilt — same false-overwrite class as the success path above.
    if (seq !== _ctxListSeq[type]) return;
    container.innerHTML = emptyState('', 'Failed to load ' + type, err.message);
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
  if (canonicalCount === 0 && cwdItems.length > 0) {
    const scanList = (scannedDirs || []).join(', ') || `.${type}/`;
    const msg = t('settings.ctx.runtime_only_banner')
      .replace('{count}', cwdItems.length)
      .replace(/\{type\}/g, type)
      .replace('{scan_dirs}', scanList);
    const banner = document.createElement('div');
    banner.className = 'ctx-runtime-only-banner';
    banner.textContent = msg;
    // Keep the tier-aware read-only banner (#943) at the very top of
    // the list — its copy explains *why* the Import button below is
    // dim, so a runtime-only "Click Import to canonicalize" prompt
    // landing above it would contradict the gate. Insert this banner
    // immediately AFTER the write-blocked banner when present;
    // otherwise fall back to the legacy first-child position.
    const writeBlocked = listEl.querySelector('.ctx-write-blocked-banner');
    const anchor = writeBlocked ? writeBlocked.nextSibling : listEl.firstChild;
    listEl.insertBefore(banner, anchor);
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
    const res = await fetch(_ctxWithTargetScope('/api/context/projects'));
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to load projects');
    const data = await res.json();
    if (seq !== _ctxListSeq[type]) return;
    const scopes = data.scopes || [];
    if (!scopes.length) {
      // Should never happen — server cwd always present — but render
      // something instead of leaving the panel blank.
      listEl.innerHTML = emptyState('', 'No project scopes', '');
      return;
    }

    let html = _ctxTierControls(type);
    for (const scope of scopes) {
      const isCwd = _ctxScopeIsServerCwd(scope);
      const count = _ctxScopeCount(scope, type);
      const groupId = `ctx-${type}-group-${escapeHtml(scope.scope_id)}`;
      const removable = !isCwd;
      const removeBtn = removable
        ? `<button class="ctx-scope-remove" data-scope-id="${escapeHtml(scope.scope_id)}" title="${escapeHtml(t('settings.ctx.remove_project'))}">×</button>`
        : '';
      // Full root path on the summary's title attribute lets the user
      // disambiguate same-name scopes (``Edu/inflearn`` vs ``Work/inflearn``)
      // on hover without inflating the visible label.
      const rootTitle = scope.root ? `title="${escapeHtml(scope.root)}"` : '';
      html += `<details class="ctx-scope-group" data-scope-id="${escapeHtml(scope.scope_id)}" data-tier="${escapeHtml(scope.tier)}"${isCwd ? ' open' : ''}>
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
            message: t('settings.ctx.confirm_remove_project')
              .replace('{label}', scope.label),
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
              showToast(err.detail || t('toast.request_failed'), 'error');
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
    listEl.innerHTML = emptyState('', 'Failed to load ' + type, err.message);
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
  return `m2m-ctx-conflict-buffer:${type}:${encodeURIComponent(name)}`;
}
function _ctxStashDraft(type, name, content) {
  try { sessionStorage.setItem(_ctxStashKey(type, name), content); } catch (_e) { /* quota / private mode */ }
}
function _ctxRestoreDraft(type, name) {
  try { return sessionStorage.getItem(_ctxStashKey(type, name)); } catch (_e) { return null; }
}
function _ctxClearDraft(type, name) {
  try { sessionStorage.removeItem(_ctxStashKey(type, name)); } catch (_e) { /* */ }
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
    const modal = qs('ctx-conflict-modal');
    qs('ctx-conflict-yours').textContent = userBuffer;
    qs('ctx-conflict-theirs').textContent = freshContent;
    const reloadBtn = qs('ctx-conflict-reload-btn');
    const diffBtn = qs('ctx-conflict-diff-btn');
    const forceBtn = qs('ctx-conflict-force-btn');
    show(modal);
    // Focus the safest choice. Force-save is destructive (overwrites the
    // other writer's edits) and the modal exists precisely to make that
    // choice explicit — auto-focusing the danger button would let a
    // reflexive Enter-press silently overwrite work. Reload preserves
    // the on-disk content; the user can still tab to Force.
    reloadBtn.focus();

    function cleanup(choice) {
      hide(modal);
      modal.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      reloadBtn.onclick = null;
      diffBtn.onclick = null;
      forceBtn.onclick = null;
      resolve(choice);
    }
    function onBackdrop(e) { if (e.target === modal) cleanup(null); }
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); cleanup(null); }
    }
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    reloadBtn.onclick = () => cleanup('reload');
    diffBtn.onclick = () => cleanup('diff');
    forceBtn.onclick = () => cleanup('force');
  });
}

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

async function _ctxHandleConflict(type, name, userBuffer, staleMtimeNs, detailEl, headers) {
  // ``staleMtimeNs`` is the mtime_ns the user's first Save was already
  // racing against — i.e. what they thought disk was. We thread it
  // through to the force PUT body so the server-side WARNING log
  // captures distinct ``client_mtime_ns`` / ``server_mtime_ns`` values;
  // sending ``fresh.mtime_ns`` would make the two values nearly equal
  // and defeat the audit trail's "what was being overridden" purpose.
  //
  // Stash early so the buffer survives an Escape-out / tab close.
  _ctxStashDraft(type, name, userBuffer);
  const fresh = await _ctxFetchFresh(type, name);
  if (fresh == null) return;
  const choice = await _ctxResolveConflict(userBuffer, fresh.content);
  if (choice === 'reload') {
    _ctxClearDraft(type, name);
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
        _ctxClearDraft(type, name);
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
      detailEl.innerHTML = emptyState('', `"${name}" not found`, t('settings.ctx.no_artifacts_hint'));
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

    html += '<div class="ctx-detail-tabs">';
    html += `<div class="ctx-detail-tab active" data-pane="canonical">${t('settings.ctx.canonical_source')}</div>`;
    html += `<div class="ctx-detail-tab" data-pane="diff">${t('settings.ctx.diff_view')}</div>`;
    html += '</div>';

    html += '<div class="ctx-detail-pane active" id="ctx-pane-canonical">';
    html += `<pre class="ctx-content-pre">${escapeHtml(data.content || '')}</pre>`;
    if (data.files && data.files.length) {
      html += `<div style="margin-top:8px"><strong>${t('settings.ctx.auxiliary_files')}</strong>`;
      for (const f of data.files) {
        html += `<div class="text-muted" style="font-size:0.78rem">${escapeHtml(f.path)} (${f.size} bytes)</div>`;
      }
      html += '</div>';
    }
    html += '</div>';

    html += '<div class="ctx-detail-pane" id="ctx-pane-diff"><div class="text-muted">Click Diff tab to load...</div></div>';

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

    // Draft restore (issue #763): if the user closed a conflict modal
    // without resolving (Escape / backdrop / tab close-and-reopen) their
    // unsaved buffer is in sessionStorage. Rehydrate the textarea, open
    // the edit pane, and toast so they know we kept their work.
    const stashed = _ctxRestoreDraft(type, name);
    if (stashed != null) {
      const ta = detailEl.querySelector('#ctx-edit-content');
      if (ta) ta.value = stashed;
      const canonPane = detailEl.querySelector('#ctx-pane-canonical');
      const editPane = detailEl.querySelector('#ctx-pane-edit');
      if (canonPane) canonPane.hidden = true;
      if (editPane) editPane.hidden = false;
      detailEl.querySelectorAll('.ctx-detail-tab').forEach(tab => { tab.style.display = 'none'; });
      showToast(t('settings.ctx.conflict_draft_restored'), 'info');
    }

    // Tab switching
    detailEl.querySelectorAll('.ctx-detail-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        detailEl.querySelectorAll('.ctx-detail-tab').forEach(t => t.classList.remove('active'));
        detailEl.querySelectorAll('.ctx-detail-pane').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        const pane = detailEl.querySelector(`#ctx-pane-${tab.dataset.pane}`);
        if (pane) pane.classList.add('active');
        if (tab.dataset.pane === 'diff') _ctxLoadDiff(type, name, detailEl);
      });
    });

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
      const canonPane = detailEl.querySelector('#ctx-pane-canonical');
      const editPane = detailEl.querySelector('#ctx-pane-edit');
      if (canonPane) canonPane.hidden = true;
      if (editPane) editPane.hidden = false;
      detailEl.querySelectorAll('.ctx-detail-tab').forEach(t => t.style.display = 'none');
    });

    // Cancel edit
    detailEl.querySelector('.ctx-edit-cancel')?.addEventListener('click', () => {
      const canonPane = detailEl.querySelector('#ctx-pane-canonical');
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
      _ctxClearDraft(type, name);
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
          await _ctxHandleConflict(type, name, content, mtime_ns, detailEl, headers);
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
          _ctxClearDraft(type, name);
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
      const result = await showConfirm({
        title: t('settings.ctx.confirm_delete').replace('{name}', name),
        message: t('settings.ctx.confirm_delete_msg'),
        confirmText: t('settings.ctx.delete'),
        extraOption: {
          id: 'cascade',
          label: t('settings.ctx.cascade_delete'),
          defaultChecked: false,
        },
      });
      if (!result || !result.ok) return;
      const cascade = !!(result.extras && result.extras.cascade);
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
          showToast(err.detail || t('toast.request_failed'), 'error');
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
    detailEl.innerHTML = emptyState('', 'Failed to load detail', err.message);
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
  const pane = detailEl.querySelector('#ctx-pane-diff');
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
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Diff failed');
    const data = await res.json();
    const fieldMapData = await fieldMapPromise;

    let html = '';
    if (fieldMapData) {
      html += _ctxRenderFieldMapHtml(fieldMapData.fieldMap, fieldMapData.runtimes);
    }
    if (!data.runtimes || !data.runtimes.length) {
      html += '<div class="text-muted">No runtime targets found.</div>';
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
    pane.innerHTML = `<div class="text-muted">Diff failed: ${escapeHtml(err.message)}</div>`;
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
          showToast(err.detail || t('toast.request_failed'), 'error');
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
    detailEl.innerHTML = emptyState('', 'Failed to load detail', err.message);
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
        showToast(err.detail || t('toast.request_failed'), 'error');
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
        showToast(err.detail || t('toast.request_failed'), 'error');
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
    form.innerHTML = `
      <label>Name</label>
      <input type="text" class="ctx-create-name" placeholder="my-${type.slice(0, -1)}" style="width:100%" />
      <label style="margin-top:8px">Content</label>
      <textarea class="ctx-edit-area ctx-create-content" rows="6" placeholder="# ${type.slice(0, -1).charAt(0).toUpperCase() + type.slice(0, -1).slice(1)} content..."></textarea>
      <div class="ctx-edit-actions">
        <button class="btn-ghost ctx-create-cancel">${t('settings.ctx.cancel')}</button>
        <button class="btn-primary ctx-create-submit">${t('settings.ctx.create')}</button>
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
          const err = await r.json();
          showToast(err.detail || t('toast.request_failed'), 'error');
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
          showToast(err.detail || t('toast.request_failed'), 'error');
          return;
        }
        const data = await r.json();
        if (data.warning) {
          showToast(data.warning, 'warning');
        } else {
          showToast(t('settings.ctx.add_project_success'), 'success');
        }
        loadCtxList(type);
      } catch (err) {
        showToast(t('toast.request_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    };
    if (window.PathPicker && typeof window.PathPicker.open === 'function') {
      window.PathPicker.open({ onSelect });
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

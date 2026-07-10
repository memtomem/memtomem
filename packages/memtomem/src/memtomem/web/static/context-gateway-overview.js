/**
 * Context Gateway — part 3/7: overview. The Simple-mode Overview body and the
 * Sync-All per-phase progress + result summary. Classic script (#1517).
 *
 * NOTE: both the `langchange` listener registration and the `// Sync All button`
 * comment that test_i18n's _langchange_listener_body sentinel slice spans MUST
 * stay together in this fragment — do not move the Sync-All section out.
 *
 *   depends on: app.js globals; context-gateway-core.js (state, badges, scope
 *               helpers); context-gateway-controls.js (write-block state)
 *   provides:   loadCtxOverview, _ctxSyncProjectScope (consumed by context-portal.js)
 */

// -- Simple-mode Overview body (ADR-0026 P1a, #1353) --------------------------

// Map a type's raw overview counts to one of three Simple display states plus
// the direction that resolves it. Display-only: no wire status string is
// mutated (Advanced still renders the full four-status ladder). The precedence
// MUST match the Advanced badge ladder (the ``badgeText`` cascade in
// ``_renderCtxOverview``): missing_target → missing_canonical → out_of_sync, so
// a mixed multi-runtime item shows the SAME dominant state in both modes
// (ADR-0026 D-C, worst-status-wins). missing_target and out_of_sync both
// resolve by Sync (push); missing_canonical resolves by Import (pull) and
// outranks out_of_sync, exactly as Advanced does.
function _ctxSimpleVerdict(d) {
  d = d || {};
  const total = d.total || 0;
  if (d.error || d.status === 'error' || (d.parse_error || 0) > 0 || (d.invalid_name || 0) > 0) {
    return { state: 'attention', labelKey: 'settings.ctx.status_simple_attention', tone: 'warn' };
  }
  if (total === 0) {
    return { state: 'empty', labelKey: 'settings.ctx.status_simple_empty', tone: 'muted' };
  }
  const needsSync = { state: 'needs_sync', labelKey: 'settings.ctx.status_simple_needs_sync', tone: 'warn' };
  if ((d.missing_target || 0) > 0) {
    return needsSync;
  }
  if ((d.missing_canonical || 0) > 0) {
    return { state: 'not_saved', labelKey: 'settings.ctx.status_simple_not_saved', tone: 'info' };
  }
  // Settings/hooks report drift through ``status`` rather than the count
  // fields (``_SETTINGS_STATUS_I18N`` in the Advanced badge), so honor the
  // status string here too — otherwise a settings-only ``out_of_sync`` reads
  // as in-sync in Simple mode.
  if ((d.out_of_sync || 0) > 0 || d.status === 'out_of_sync') {
    return needsSync;
  }
  // A project_local canonical with no runtime fan-out is an intentional draft,
  // not an in-tools success — surface it as its own muted state so it never
  // earns the green check or the all-clear verdict (it ranks below every real
  // issue above, matching the Advanced ``badge_local_draft`` position).
  if ((d.local_draft || 0) > 0) {
    return { state: 'local_draft', labelKey: 'settings.ctx.status_simple_local_draft', tone: 'muted' };
  }
  return { state: 'in_tools', labelKey: 'settings.ctx.status_simple_in_tools', tone: 'ok' };
}

// Build the Simple Overview body: a one-line aggregate verdict + a row per
// artifact type (skills/commands/agents/mcp AND hooks/settings — the latter now
// participates so a settings-only drift or error can't hide behind an all-clear
// verdict). Each row shows the 4-state status (text is always present — never
// color-only, ADR-0026 D-G) plus one P1b control: an inline Sync (needs_sync) /
// Import (not_saved, importable) button running the SAME flow as Advanced, a
// decorative check (in_tools), or a read-only Manage deep-link (attention,
// empty, local_draft, settings, and mcp-servers not_saved — no /import route).
// Inline ``t()`` matches the langchange re-render ordering of this render path.
function _ctxSimpleOverviewBody(data, types) {
  let anyAttention = false, anyAction = false, anyItems = false, anyDraft = false;
  const rowsHtml = types.map(typ => {
    const d = data[typ.key] || {};
    const v = _ctxSimpleVerdict(d);
    if ((d.total || 0) > 0) anyItems = true;
    if (v.state === 'attention') anyAttention = true;
    if (v.state === 'needs_sync' || v.state === 'not_saved') anyAction = true;
    if (v.state === 'local_draft') anyDraft = true;
    const statusText = t(v.labelKey);
    // P1b (#1353): one control per row (minimal layout). A fixable row carries
    // the resolving verb inline and runs the SAME confirm + impact-preview +
    // host-write flow as Advanced (``_ctxRunSync`` / ``_ctxRunImport``); a clean
    // row shows a decorative check (the status text already names the state —
    // never glyph-only, D-G); everything else (empty, attention, and the
    // not_saved case for mcp-servers, which has no ``/import`` route) keeps the
    // read-only Manage deep-link into Advanced.
    const apiType = typ.apiType;   // 'skills', 'mcp-servers', 'settings', …
    // Settings/hooks has neither an ``/import`` route nor the plain per-type
    // sync semantics the inline flow assumes (it merges per-runtime with a
    // host-write gate), so its actionable states route to Manage rather than an
    // inline Sync/Import — the row still counts toward the verdict.
    const isSettings = apiType === 'settings';
    const canImport = apiType !== 'mcp-servers' && !isSettings;
    let control;
    if (v.state === 'needs_sync' && !isSettings) {
      const syncAria = t('settings.ctx.simple_sync_aria', { type: typ.label });
      // Best-effort canonical count for the confirm headline; ``_ctxRunSync``
      // re-fetches the real impact before committing. needs_sync ⟹ canonical
      // items exist (missing_target / out_of_sync > 0), so the empty-guard never
      // misfires here. project_local forces 0 — mirroring the Advanced section
      // dataset (``_ctxTargetScope === 'project_local' ? 0 : …``) so the
      // in-function no-write guard stays a backstop to the write-block sweep.
      const canonicalCount = _ctxTargetScope === 'project_local'
        ? '0'
        : String(Math.max((d.total || 0) - (d.missing_canonical || 0), 0) || (d.total || 0));
      control = `<button type="button" class="btn-primary ctx-simple-action"
                data-ctx-action="sync" data-type="${escapeHtml(apiType)}"
                data-canonical-count="${escapeHtml(canonicalCount)}"
                data-no-fanout="${_ctxTargetScope === 'project_local' ? 'true' : 'false'}"
                aria-label="${escapeHtml(syncAria)}">${escapeHtml(t('settings.ctx.sync'))}</button>`;
    } else if (v.state === 'not_saved' && canImport) {
      const importAria = t('settings.ctx.simple_import_aria', { type: typ.label });
      control = `<button type="button" class="btn-ghost ctx-simple-action"
                data-ctx-action="import" data-type="${escapeHtml(apiType)}"
                aria-label="${escapeHtml(importAria)}">${escapeHtml(t('settings.ctx.import'))}</button>`;
    } else if (v.state === 'in_tools') {
      control = `<span class="ctx-simple-check" aria-hidden="true">✓</span>`;
    } else {
      const manageAria = t('settings.ctx.simple_manage_aria', { type: typ.label });
      control = `<button type="button" class="btn-ghost ctx-simple-manage"
                data-ctx-advance data-section="${typ.section}"
                aria-label="${escapeHtml(manageAria)}">${escapeHtml(t('settings.ctx.simple_manage'))}</button>`;
    }
    return `<div class="ctx-simple-row ctx-simple-row--${v.tone}" data-section="${typ.section}" data-state="${v.state}">
        <span class="ctx-simple-row-type">${escapeHtml(typ.label)}</span>
        <span class="ctx-simple-row-status">
          <span class="ctx-simple-dot" aria-hidden="true"></span>
          <span class="ctx-simple-status-text">${escapeHtml(statusText)}</span>
        </span>
        ${control}
      </div>`;
  }).join('');

  let verdictKey, verdictTone;
  if (anyAttention) { verdictKey = 'settings.ctx.simple_verdict_attention'; verdictTone = 'warn'; }
  else if (anyAction) { verdictKey = 'settings.ctx.simple_verdict_action'; verdictTone = 'warn'; }
  else if (!anyItems) { verdictKey = 'settings.ctx.simple_verdict_empty'; verdictTone = 'muted'; }
  // Drafts are intentional, so they rank below real actions but must still
  // block the all-clear verdict — a project_local draft is NOT "in your tools".
  else if (anyDraft) { verdictKey = 'settings.ctx.simple_verdict_draft'; verdictTone = 'info'; }
  else { verdictKey = 'settings.ctx.simple_verdict_clear'; verdictTone = 'ok'; }

  // Plain ``<p>`` (no ``role=status``): the verdict is static render output,
  // not a live announcement — a second live region would compete with
  // ``#ctx-sync-status`` (ADR-0026 D-G).
  let html = `<div class="ctx-overview-simple">
      <p class="ctx-simple-verdict ctx-simple-verdict--${verdictTone}">${escapeHtml(t(verdictKey))}</p>
      <div class="ctx-simple-rows">${rowsHtml}</div>`;
  if (!anyItems) {
    html += `<div class="ctx-simple-empty-hint">
        <span>${escapeHtml(t('settings.ctx.simple_empty_hint'))}</span>
        <button type="button" class="btn-ghost ctx-simple-advanced-cta" data-ctx-advance data-section="ctx-skills" data-focus-target=".ctx-import-btn">${escapeHtml(t('settings.ctx.simple_empty_import'))}</button>
        <button type="button" class="btn-primary ctx-simple-advanced-cta" data-ctx-advance data-section="ctx-skills" data-focus-target=".ctx-create-btn">${escapeHtml(t('settings.ctx.simple_empty_create'))}</button>
      </div>`;
  }
  html += '</div>';
  return html;
}

// Wire the Simple rows: ``[data-ctx-advance]`` (Manage buttons + the empty-state
// CTA) leaves Simple mode and, when a section is named, deep-links into Advanced;
// ``[data-ctx-action]`` (P1b inline Sync/Import) runs the SAME flow as the
// Advanced toolbar buttons — full confirm + impact preview + host-write
// disclosure via ``_ctxRunSync`` / ``_ctxRunImport`` — then refreshes the Simple
// Overview in place on success. No-op in Advanced (neither selector matches).
function _ctxWireSimpleRows(el) {
  el.querySelectorAll('[data-ctx-advance]').forEach(btn => {
    btn.addEventListener('click', () => {
      _ctxOpenInAdvanced(btn.dataset.section || '');
      const target = btn.dataset.focusTarget;
      if (target) requestAnimationFrame(() => document.querySelector(`#settings-ctx-skills ${target}`)?.focus());
    });
  });
  el.querySelectorAll('[data-ctx-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const type = btn.dataset.type;
      if (btn.dataset.ctxAction === 'sync') {
        _ctxRunSync(type, {
          btn,
          canonicalCount: btn.dataset.canonicalCount || '0',
          noFanout: btn.dataset.noFanout === 'true',
          onComplete: () => loadCtxOverview(),
        });
      } else if (btn.dataset.ctxAction === 'import') {
        _ctxRunImport(type, { btn, onComplete: () => loadCtxOverview() });
      }
    });
  });
}

// ADR-0026 P1b D-D (#1353): count items in the tiers OTHER than the active one
// and, when any hold artifacts, replace the generic empty hint with a summary
// that names them ("Stored elsewhere: 3 in User"). The Overview route
// summarizes a single tier per call (this layer adds NO backend route — ADR
// non-goal), so this fans out one best-effort read per other tier. Seq-guarded
// so a tier/mode switch (or a second empty render) can't patch stale counts in;
// any fetch failure or all-empty result silently leaves the generic hint.
let _ctxCrossTierSeq = 0;
const _CTX_CROSS_TIER_TYPES = ['skills', 'commands', 'agents', 'mcp_servers'];
const _CTX_TIER_LABEL_KEY = {
  user: 'settings.ctx.tier_option_user',
  project_shared: 'settings.ctx.tier_option_project_shared',
  project_local: 'settings.ctx.tier_option_project_local',
};

async function _ctxRenderCrossTierSummary(activeTier, scopeId) {
  const seq = ++_ctxCrossTierSeq;
  const others = ['user', 'project_shared', 'project_local'].filter(tier => tier !== activeTier);
  const counts = await Promise.all(others.map(async tier => {
    try {
      const res = await fetch(_ctxWithTargetScope('/api/context/overview', {
        targetScope: tier,
        scopeId,
        scopeResolved: true,
      }));
      if (!res.ok) return { tier, total: 0 };
      const data = await res.json();
      if (data === null || typeof data !== 'object') return { tier, total: 0 };
      const total = _CTX_CROSS_TIER_TYPES.reduce(
        (sum, key) => sum + ((data[key] && data[key].total) || 0), 0,
      );
      return { tier, total };
    } catch {
      return { tier, total: 0 };
    }
  }));
  // A newer render (tier flip, mode toggle, refresh) superseded this fan-out —
  // its own summary owns the hint now; never patch over it.
  if (seq !== _ctxCrossTierSeq || !_ctxSimpleMode) return;
  const container = qs('ctx-overview-content');
  const span = container?.querySelector('.ctx-simple-empty-hint > span');
  if (!span) return;
  const entries = counts
    .filter(c => c.total > 0)
    .map(c => t('settings.ctx.simple_cross_tier_entry', {
      count: c.total,
      tier: t(_CTX_TIER_LABEL_KEY[c.tier] || c.tier),
    }));
  if (!entries.length) return;   // truly empty everywhere — keep the generic hint
  span.textContent = `${t('settings.ctx.simple_cross_tier_label')} ${entries.join(' · ')}`;
}

function _renderCtxOverview(data) {
  const el = qs('ctx-overview-content');
  if (!el) return;

  const types = [
    { key: 'skills',   label: t('settings.ctx.skills_title'),   section: 'ctx-skills',      apiType: 'skills' },
    { key: 'commands', label: t('settings.ctx.commands_title'), section: 'ctx-commands',    apiType: 'commands' },
    { key: 'agents',   label: t('settings.ctx.agents_title'),   section: 'ctx-agents',      apiType: 'agents' },
    { key: 'mcp_servers', label: t('settings.ctx.mcp_servers_title'), section: 'ctx-mcp-servers', apiType: 'mcp-servers' },
    { key: 'settings', label: t('settings.hooks.title'),        section: 'hooks-sync',      apiType: 'settings' },
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

  // Wiki update-available badge — the lockfile↔wiki staleness axis from the
  // overview payload's ``wiki_installs`` block. Renders only when actionable
  // (behind > 0); an absent/null block (older cached payloads replaying
  // through langchange, or a degraded backend classifier) reads as no badge —
  // the same defensive-reader posture as ``detected_runtimes`` above. Plain
  // flex child of the header, no wrapper: the ``badge`` primitives already
  // carry the styling (no style.css change needed).
  const wikiInstalls = (data.wiki_installs && typeof data.wiki_installs === 'object')
    ? data.wiki_installs : null;
  const wikiBehind = (wikiInstalls && typeof wikiInstalls.behind === 'number')
    ? wikiInstalls.behind : 0;
  let wikiBehindHtml = '';
  if (wikiBehind > 0) {
    const behindTip = escapeHtml(t('settings.ctx.wiki_behind_tip', { n: wikiBehind }));
    const behindLabel = escapeHtml(t('settings.ctx.wiki_behind_badge', { n: wikiBehind }));
    wikiBehindHtml = `<span class="badge badge-warning ctx-overview-wiki-behind" title="${behindTip}">${behindLabel}</span>`;
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
      <div class="ctx-flow-diagram" role="img" aria-label="${escapeHtml(t('settings.ctx.flow_aria'))}">
        <span class="ctx-flow-node">${escapeHtml(t('settings.ctx.store_label'))}</span>
        <span class="ctx-flow-arrow" aria-hidden="true">── ${escapeHtml(t('settings.ctx.sync'))} →</span>
        <span class="ctx-flow-node">${escapeHtml(t('settings.ctx.runtimes_label'))}</span>
      </div>
      <div class="ctx-overview-runtimes">
        <span class="ctx-overview-runtimes-label">${escapeHtml(t('settings.ctx.runtimes_label'))}</span><span class="help-tip" data-help="${escapeHtml(t('settings.ctx.runtimes_glossary'))}" tabindex="0" role="img" aria-label="${escapeHtml(t('settings.ctx.runtimes_glossary'))}">i</span>
        ${chips}
      </div>
      ${lastSyncHtml}
      ${wikiBehindHtml}
    </div>`;
  // ADR-0026 P1a (#1353): in Simple mode, render a one-line verdict + per-type
  // rows ABOVE the grid; ``.ctx-simple`` CSS hides the grid so the Advanced
  // path below stays byte-identical (D-F: Advanced == today's UI verbatim).
  // Present-but-hidden mirrors the existing ``#ctx-control-bar`` hoist idiom.
  if (_ctxSimpleMode) html += _ctxSimpleOverviewBody(data, types);
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
      // content. The all-clear fallthrough keeps ``{inSync}/{total} synced``
      // when the two counts agree and spells out both axes otherwise (#1646).
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
      } else if (inSync === total) {
        badgeText = `${inSync}/${total} ${t('settings.ctx.badge_synced')}`;
      } else {
        // ``inSync`` counts (runtime, name) copies while ``total`` counts
        // stored artifacts (see the count-semantics comment above), so with
        // >1 runtime the fraction exceeds 1× ("4/1 synced") and reads as
        // nonsense (#1646). Spell out both axes instead. Reaching here with
        // zero issue counts means every tracked copy IS in sync, so the
        // two-axis copy stays truthful to the #692 divergence guard.
        badgeText = t('settings.ctx.badge_synced_two_axis', { stored: total, copies: inSync });
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

  // ADR-0026 P1a/P1b: wire the Simple-mode rows (no-op in Advanced — neither
  // selector matches anything). Manage rows route into Advanced; fixable rows
  // run Sync/Import inline.
  _ctxWireSimpleRows(el);

  // ADR-0026 P1b D-D (#1353): when the active tier is all-empty in Simple mode,
  // name which OTHER tier(s) hold items instead of only the generic empty hint.
  // Keyed off the empty-hint marker the Simple body renders only when !anyItems.
  if (_ctxSimpleMode && el.querySelector('.ctx-simple-empty-hint')) {
    _ctxRenderCrossTierSummary(targetScope, _ctxEffectiveScopeId());
  }

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
  // apply — fetch the four artifact lists + overview for the pinned (project,
  // tier) best-effort: the lists drive the per-type×per-runtime breakdown, the
  // overview the settings counts (#1288). A failed list degrades to the overview
  // counts; a failed overview to the base copy (Codex B — pure enhancement, the
  // lists are read-only + scope-aware so targeting a non-active scope is safe).
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
  let preview;
  try {
    preview = await _ctxSyncAllPreview(pinnedScopeOpts);
  } finally {
    btnLoading(btn, false);
  }
  // rank-10: name the specific project this portal card syncs (the raw
  // ``scopeId`` resolves to its label; '' === Server CWD).
  const { message, warningText } = _ctxSyncAllConfirmCopy(
    t('settings.ctx.confirm_sync_all', { dest: _ctxScopeDisplayLabelById(scopeId) }),
    preview,
  );
  const ok = await showConfirm({
    title: t('settings.ctx.sync_all'),
    message,
    warningText,
    confirmText: t('settings.ctx.sync'),
    danger: false,
  });
  if (!ok) return;

  btnLoading(btn, true);
  showToast(t('settings.ctx.sync_started') || 'Syncing project...', 'info');

  const succeeded = [];
  let failed = null;
  // Every failed phase (this card fan-out no longer aborts on the first
  // per-phase HTTP error — #1396), so the partial toast can name them all.
  const failedPhases = [];
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
        if (!failed) failed = { phase: typ, reason: err.message };
        failedPhases.push(typ);
        break;
      }
      if (!resp.ok) {
        // Recoverable per-phase HTTP error (e.g. a project_shared privacy-block
        // 422): the server is up and the other types have no issue, so keep
        // going — mirror the backend ``/sync-all`` per-phase isolation (#1396).
        // Only a transport failure (above) aborts the run. ``failed`` keeps the
        // FIRST failure (drives the toast reason + gates the settings phase).
        // The fallback is localized (#1646): it gets interpolated into the
        // localized ``toast.sync_failed``, so an English literal here would
        // leak into Korean toasts whenever the response has no structured
        // detail. Structured-path passthrough above stays verbatim — the
        // backend reason (e.g. the parse-error filename) is remediation-
        // critical.
        const reason = await _ctxErrorMessageFromResponse(
          resp, t('settings.ctx.sync_phase_failed_fallback', { type: _ctxTypeName(typ) }));
        if (!failed) failed = { phase: typ, reason };
        failedPhases.push(typ);
        continue;
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
            reason: await _ctxErrorMessageFromResponse(
              settingsResp, t('settings.ctx.sync_settings_failed_fallback')),
          };
          failedPhases.push('settings');
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
        failedPhases.push('settings');
      }
    }

    const phaseLabel = (p) => t(`settings.ctx.${String(p).replace(/-/g, '_')}_phase_title`);
    if (failed) {
      if (succeeded.length === 0) {
        showToast(t('toast.sync_failed', { error: failed.reason }), 'error');
      } else {
        // Name EVERY failed phase — the loop no longer aborts on the first
        // per-phase HTTP error (#1396); ``failed.reason`` stays the first one.
        showToast(
          t('toast.sync_partial_failed', {
            succeeded: succeeded.map(phaseLabel).join(', '),
            failed_phase: failedPhases.map(phaseLabel).join(', '),
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
  // The Simple-mode toggle label + active chip are inline ``t()`` writes with
  // no ``data-i18n`` (context-gateway-core.js) — their load-time paint runs
  // before the locale cache exists, so the ``langchange`` that ``I18N.init``
  // dispatches (and every later flip) must re-run the renderer. BEFORE the
  // ``hostActive`` gate: the boot dispatch usually fires with another tab
  // active, and the raw-key labels must be repaired before the user first
  // opens the gateway.
  _ctxApplySimpleMode();
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
  // Best-effort Sync All preview under the pinned (project, tier): the four
  // artifact lists (per-type×per-runtime breakdown) + the overview (settings
  // counts — settings have no per-runtime list payload, so they ride a separate
  // counts-only segment). A failed list degrades to the aggregate counts; a
  // failed overview degrades to the base copy. Never blocks the dialog (#1288).
  btnLoading(btn, true);
  let preview;
  try {
    preview = await _ctxSyncAllPreview(pinnedScopeOpts);
  } finally {
    btnLoading(btn, false);
  }
  const { message, warningText } = _ctxSyncAllConfirmCopy(
    t('settings.ctx.confirm_sync_all', { dest: syncAllDestLabel }),
    preview,
  );
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
    if (syncAllTier === 'project_shared') {
      anyPhaseStarted = true;
      for (const phase of _CTX_SYNC_PHASES) setPhase(phase, 'syncing');
      const response = await fetch(
        _ctxWithTargetScope('/api/context/sync-all', pinnedScopeOpts),
        { method: 'POST', headers },
      );
      if (!response.ok) {
        const reason = await _ctxErrorMessageFromResponse(
          response, t('settings.ctx.sync_settings_failed_fallback'));
        for (const phase of _CTX_SYNC_PHASES) setPhase(phase, 'failed');
        showToast(t('toast.sync_failed', { error: reason }), 'error');
        return;
      }
      const report = await response.json();
      if (Array.isArray(report.phases)) {
        // Skip-row classification deliberately stays client-side (#1262): the
        // route reports raw ``reason_code`` rows, so run the same
        // ``_ctxIsAttentionSkip`` sweep as the legacy per-type loop — a
        // failure-class skip (parse_error / duplicate_name / …) demotes its
        // row to ``attention`` and gates the success toast (#1247 id 21).
        const batchAttentionSkips = [];
        for (const phase of report.phases) {
          const counts = phase.type === 'settings'
            ? undefined
            : _ctxSyncArtifactCounts(phase);
          const phaseSkips = Array.isArray(phase.skipped) ? phase.skipped : [];
          const phaseAttention = phaseSkips.filter(_ctxIsAttentionSkip);
          batchAttentionSkips.push(...phaseAttention.map(_ctxAttentionSkipLabel));
          const state = phase.status === 'failed'
            ? 'failed'
            : phase.status === 'needs_confirmation' || phaseAttention.length
              ? 'attention'
              : 'done';
          setPhase(phase.type, state, counts);
        }
        // Toast ladder — mirrors the legacy orchestrator's severity order
        // (this file, lines ~1794): failed > failure-class skips (warning,
        // #1247) > settings needs_confirmation (info + Open Settings action,
        // #774) > no-op > success. Warning outranks info: when a run both
        // skips a broken artifact AND needs host-write confirmation, the more
        // severe artifact warning must win, not the settings info toast.
        const failedPhases = report.phases.filter((phase) => phase.status === 'failed');
        const needsConfirmation = report.phases.some(
          (phase) => phase.status === 'needs_confirmation');
        if (failedPhases.length) {
          showToast(t('toast.sync_partial_failed', {
            succeeded: report.phases.filter((phase) => phase.status === 'ok').map((phase) => _ctxSyncPhaseLabel(phase.type)).join(', '),
            failed_phase: failedPhases.map((phase) => _ctxSyncPhaseLabel(phase.type)).join(', '),
            reason: failedPhases[0].error?.message || t('settings.ctx.sync_settings_failed_fallback'),
          }), 'error');
        } else if (batchAttentionSkips.length) {
          const items = [...new Set(batchAttentionSkips)];
          showToast(
            t('settings.ctx.sync_skipped_attention', {
              count: items.length,
              items: items.join(', '),
            }),
            'warning',
          );
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
        } else if (report.summary?.changed === false || report.summary?.outcome === 'noop') {
          showToast(t('settings.ctx.sync_all_nothing_synced'), 'info');
        } else {
          showToast(t('settings.ctx.sync_success'));
        }
        return;
      }
      // An older proxy/test double can answer the new route with a generic
      // object. Treat that as unsupported and retain the established per-type
      // orchestration instead of leaving five rows stuck on “Syncing”. If the
      // 200 actually came from a real (older) server that DID run the batch,
      // the legacy loop re-POSTs the five phases — accepted: each sync is a
      // regenerate-from-canonical, so the second pass is an idempotent no-op.
      for (const phase of _CTX_SYNC_PHASES) setPhase(phase, 'pending');
    }
    // Re-bind the CSRF headers next to the legacy fan-out: the batch branch
    // above pushed these fetches past the CSRF invariant's binding-lookback
    // window (test_spa_api_fetch_threads_csrf_token), same rationale as the
    // settings phase's inline binding below. ``csrf`` is the run-level token.
    const legacyHeaders = csrf
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
          { method: 'POST', headers: legacyHeaders },
        );
      } catch (err) {
        failed = { phase: typ, reason: err.message };
        setPhase(typ, 'failed');
        break;
      }
      if (!resp.ok) {
        // A per-phase HTTP error (e.g. a project_shared privacy-block 422) is
        // recoverable for the run: the other types must still sync, so mirror
        // the backend ``/sync-all`` per-phase isolation — CONTINUE rather than
        // abort the whole fan-out (only a transport failure, above, aborts). The
        // FIRST failure drives the toast reason + gates settings; the failed
        // list comes from ``phaseStates`` (#1396). Localized fallback — it is
        // interpolated into ``toast.sync_failed`` (#1646).
        const reason = await _ctxErrorMessageFromResponse(
          resp, t('settings.ctx.sync_phase_failed_fallback', { type: _ctxTypeName(typ) }));
        if (!failed) failed = { phase: typ, reason };
        setPhase(typ, 'failed');
        continue;
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
        // Inline the CSRF headers at the call site (rather than the shared
        // run-level ``headers`` var): this phase's fetch sits beyond the
        // invariant guard's binding-lookback window, so thread the token here.
        // ``csrf`` is the run-level token bound above.
        settingsResp = await fetch(
          _ctxWithTargetScope('/api/context/settings/sync', {
            scopeId: syncAllScopeId,
            scopeResolved: true,
            targetScope: syncAllTier,
          }),
          {
            method: 'POST',
            headers: csrf
              ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
              : { 'Content-Type': 'application/json' },
          },
        );
      } catch (err) {
        failed = { phase: 'settings', reason: err.message };
      }
      if (!failed) {
        if (!settingsResp.ok) {
          failed = {
            phase: 'settings',
            reason: await _ctxErrorMessageFromResponse(
              settingsResp, t('settings.ctx.sync_settings_failed_fallback')),
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
        // The loop no longer aborts on the first per-phase HTTP error (#1396),
        // so name EVERY failed phase — derived from the authoritative per-phase
        // status so the toast stays in lockstep with the summary rows. The
        // reason stays the FIRST failure's (the summary carries the rest).
        const failedPhaseLabels = _CTX_SYNC_PHASES
          .filter((p) => phaseStates[p].state === 'failed')
          .map(_ctxSyncPhaseLabel);
        showToast(
          t('toast.sync_partial_failed', {
            succeeded: succeeded.map(_ctxSyncPhaseLabel).join(', '),
            failed_phase: failedPhaseLabels.join(', '),
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
// ADR-0026 P1a (#1353): Simple/Advanced toggle. Flips the persisted flag,
// re-applies the ``.ctx-simple`` class, and re-renders the Overview body so the
// tile grid and the Simple verdict+rows swap in place. The toggle lives in the
// Overview header (always visible while the Overview is shown), so it is
// reachable in both modes.
document.getElementById('ctx-mode-toggle')?.addEventListener('click', () => {
  _ctxSetSimpleMode(!_ctxSimpleMode);
  loadCtxOverview();
});

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

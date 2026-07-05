/**
 * Context Gateway — part 4/7: list. The Skills / Commands / Agents list view
 * and its scope-eligibility predicates. Classic script (#1517).
 *
 *   depends on: app.js globals; context-gateway-core.js (state, scope helpers);
 *               context-gateway-controls.js (_ctxRefreshWriteBlockedState)
 *   provides:   loadCtxList, _ctxScopeIsServerCwd, _ctxScopeIsEnrolled,
 *               _ctxScopeSyncEligible (consumed by context-portal.js)
 */

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
  // Split on BOTH separators so a Windows server-cwd root (``C:\work\proj``)
  // yields ``proj``, not the whole path — P0-5 (#1353/#1356) now routes the
  // visible "(current folder)" label through this, so the basename must be
  // OS-agnostic. Trailing separators of either kind are trimmed first.
  return String(p).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || String(p);
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
//   2. localized reason_code 409s ``{reason_code: sync_paused|sync_not_enrolled
//      |no_memtomem_store}`` → the specific localized copy — checked BEFORE
//      error_kind because those 409s carry BOTH error_kind:"conflict" and the
//      reason_code, and the reason_code copy is more precise than the generic
//      "conflict" label (#1210 paused/not-enrolled; #1350 no-store).
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
    if (rc === 'no_memtomem_store') return t('settings.ctx.error_no_memtomem_store');
    if (typeof detail.error_kind === 'string' && detail.error_kind) {
      const composed = _ctxKindDetailText(detail.error_kind, detail.message);
      if (composed) return composed;
    }
    if (typeof detail.message === 'string') return detail.message;
  }
  return fallback;
}

// The write-time Gate A privacy block (#1509) is the one editor 422 that ships a
// path-free but raw-ENGLISH, jargon-heavy string ``detail`` ("Gate A: … ADR-0011
// §5 … target_scope=user"). The server hoists ``reason_code: "privacy_blocked"``
// to a top-level sibling of that string (``_sync_phase`` handler, #1409), so we
// can show a localized, jargon-free hint here and keep the raw English detail in
// a tooltip for fidelity (#1651). ``err`` is the parsed 422 body. Returns true
// when it handled a privacy block (caller should stop and not toast again).
function _ctxMaybePrivacyToast(err) {
  if (err && err.reason_code === 'privacy_blocked') {
    showToast(t('settings.ctx.privacy_blocked_editor_hint'), 'error', {
      title: typeof err.detail === 'string' ? err.detail : undefined,
    });
    return true;
  }
  return false;
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
  // This is a MISSING-canonical banner: the generic snippets bootstrap a
  // canonical from runtime (init/generate) for skills/agents/commands. MCP
  // canonicals are not created that way (cross-project copy or hand-authored),
  // and `mm context sync --include=mcp-servers` only fans an EXISTING canonical
  // into .mcp.json — useless when the canonical is the thing that's missing —
  // so the MCP row suppresses the snippets rather than prescribing a no-op.
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

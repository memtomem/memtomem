/**
 * Context Gateway — part 6/7: detail. The per-artifact Detail pane (read/diff/
 * edit/version/migrate). Classic script (#1517).
 *
 *   depends on: app.js globals; context-gateway-core.js (state, scope helpers);
 *               context-gateway-conflict.js (conflict modal on stale-mtime save)
 *   provides:   loadCtxDetail and the detail-pane renderers
 */

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
    // Artifact detail shows canonical residency (tier), not the hooks fan-out
    // target — so source the value from the residency labels (settings.ctx.
    // tier_option_*), NOT settings.hooks.target_label_* (which is fan-out
    // wording: "User target:"). See ADR-0016 §2 (tier vs runtime scope).
    rows.push({
      label: t('settings.ctx.detail.meta_scope'),
      value: t(`settings.ctx.tier_option_${scope}`) !== `settings.ctx.tier_option_${scope}`
        ? t(`settings.ctx.tier_option_${scope}`)
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
    // Move/Copy is offered for the full transfer kinds (skills/commands/agents)
    // and the constrained mcp-servers variant (#1314 — copy-only/cross-project).
    // The button is NOT in ``_CTX_WRITE_BUTTON_SELECTOR``: transfer gates the
    // DESTINATION tier (chosen in the modal), not the source tier the
    // write-block sweep keys on — and Move/Copy is the escape hatch FROM
    // project_local/user, so source-tier blocking would hide it exactly where it
    // is most useful. mcp-servers reach this branch only with a canonical (the
    // runtime-only detail path has no button — there is nothing to copy).
    const _mcBtn = _ctxCanMoveCopy(type)
      ? `<button class="btn-ghost ctx-detail-move-copy-btn" data-i18n="settings.ctx.move_copy" data-i18n-title="settings.ctx.move_copy_tooltip" title="${escapeHtml(t('settings.ctx.move_copy_tooltip'))}">${t('settings.ctx.move_copy')}</button>`
      : '';
    // "Pull from a tool…" (ADR-0030 PR-D2): source-selectable Pull of a possibly
    // fresher tool copy over the existing canonical (overwrite). Offered for the
    // Pull-eligible kinds (skills/agents/commands); the picker itself gates the
    // destination tier + privacy, so this is NOT in the write-block sweep.
    const _pullBtn = (typeof _ctxCanPull === 'function' && _ctxCanPull(type))
      ? `<button class="btn-ghost ctx-detail-pull-btn" data-i18n="settings.ctx.pull" data-i18n-title="settings.ctx.pull_tooltip" title="${escapeHtml(t('settings.ctx.pull_tooltip'))}">${t('settings.ctx.pull')}</button>`
      : '';
    html += `<div class="ctx-detail-header">
      <h2 class="ctx-detail-name" id="ctx-detail-name-${type}" tabindex="-1">${escapeHtml(name)}</h2>
      <div style="display:flex;gap:6px">
        <button class="btn-ghost ctx-detail-edit-btn" data-i18n="settings.ctx.edit" data-i18n-title="settings.ctx.edit_tooltip" title="${escapeHtml(t('settings.ctx.edit_tooltip'))}">${t('settings.ctx.edit')}</button>
        ${_pullBtn}
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
          if (_ctxMaybePrivacyToast(err, type)) return;
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
            if (_ctxMaybePrivacyToast(err, type)) return;
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
      const _cascadeOffered = type !== 'mcp-servers'
        && (!_delScope || _ctxScopeSyncEligible(_delScope));
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
    detailEl.querySelector('.ctx-detail-pull-btn')?.addEventListener('click', () => {
      if (typeof window.ctxOpenPullModal === 'function') window.ctxOpenPullModal(type, name);
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
  pane.innerHTML = `<div class="empty-state"><div class="spinner-panel"></div>${srLoading()}</div>`;
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
      // Cross-tier "import to user library" escape hatch (skills only, outside
      // the user tier). The plain Import targets the current project tier,
      // which may hard-block a Gate A false positive (project_shared, no bypass)
      // or reject the write (project_local); this button reads the PROJECT
      // runtime but writes the force-bypassable USER library instead. It is
      // intentionally tier-independent, so it is NOT in the write-block sweep.
      const userLibBtn = (type === 'skills' && _ctxTargetScope !== 'user')
        ? `<button class="btn-ghost ctx-runtime-import-to-user">
             ${escapeHtml(t('settings.ctx.import_to_user'))}
           </button>`
        : '';
      // "Pull from a tool…" (ADR-0030 PR-D2) — the source-selectable, preview-
      // first path for the runtime-only artifact (the ADR's motivating case: a
      // stale copy silently beating a fresher one on a plain first-runtime-wins
      // import). Offered alongside the legacy one-click Import.
      const pullBtn = (typeof _ctxCanPull === 'function' && _ctxCanPull(type))
        ? `<button class="btn-ghost ctx-runtime-pull-btn">${escapeHtml(t('settings.ctx.pull'))}</button>`
        : '';
      html += `<div class="ctx-edit-actions" style="margin-top:12px">
        <button class="btn-primary ctx-runtime-only-import" data-type="${escapeHtml(type)}">
          ${escapeHtml(t('settings.ctx.import_this').replace('{type}', _ctxTypeNameSingular(type)))}
        </button>
        ${pullBtn}
        ${userLibBtn}
      </div>`;
    }

    html += '</div>';
    detailEl.innerHTML = html;
    if (opts.focusOnLoad) _ctxFocusDetail(detailEl);

    detailEl.querySelector('.ctx-runtime-pull-btn')?.addEventListener('click', () => {
      if (typeof window.ctxOpenPullModal === 'function') window.ctxOpenPullModal(type, name);
    });

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
        const data = await _ctxRunRuntimeImportFlow(importOnce);
        if (!data) return;
        if (data.imported && data.imported.length) {
          showToast(t('settings.ctx.import_success'));
        } else if (data.skipped && data.skipped.length) {
          // The skip's ``reason_code`` maps to localized copy; unknown codes
          // fall back to the raw backend reason (#1646 item 2).
          showToast(_ctxImportSkipText(data.skipped[0]), 'warning');
        }
        loadCtxList(type);
      } catch (err) {
        showToast(t('toast.import_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    });

    // "Import to user library" (skills only): reads the PROJECT runtime, writes
    // the force-bypassable USER canonical. The route pins the dest tier
    // (scope=user) and source (source_scope=project_shared) itself, so we must
    // NOT send a ``target_scope`` — but we DO need the active ``scope_id`` or
    // ``resolve_scope_root`` falls back to server CWD and reads the wrong
    // project's runtime. ``targetScope: 'project_shared'`` is the default tier,
    // which ``_ctxTargetScopeParam`` omits from the URL, so this appends
    // ``scope_id`` alone. Reviewed force + host-write run in the shared flow.
    detailEl.querySelector('.ctx-runtime-import-to-user')?.addEventListener('click', async () => {
      const btn = detailEl.querySelector('.ctx-runtime-import-to-user');
      btnLoading(btn, true);
      try {
        const csrf = await ensureCsrfToken();
        const headers = csrf
          ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }
          : { 'Content-Type': 'application/json' };
        const importOnce = (extra) => fetch(
          _ctxWithTargetScope(
            `/api/context/skills/${encodeURIComponent(name)}/import-to-user`,
            { targetScope: 'project_shared' },
          ),
          {
            method: 'POST',
            headers,
            body: JSON.stringify({ ...extra }),
          },
        );
        const data = await _ctxRunRuntimeImportFlow(importOnce);
        if (!data) return;
        if (data.imported && data.imported.length) {
          showToast(t('settings.ctx.import_to_user_success'));
          loadCtxList('skills');
        } else if (data.skipped && data.skipped.length) {
          showToast(_ctxImportSkipText(data.skipped[0]), 'warning');
        }
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

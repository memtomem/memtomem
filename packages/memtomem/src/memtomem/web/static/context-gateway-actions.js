/**
 * Context Gateway — part 7/7: actions. The delegated top-level button wiring
 * (Sync / Import / Create / Add Project). Classic script (#1517).
 *
 * These querySelectorAll(...).forEach(addEventListener) registrations run at
 * load and wire buttons the earlier fragments' toolbars minted — so this
 * fragment must load LAST among the gateway files (it does; see index.html).
 *
 *   depends on: app.js globals; context-gateway-core.js (state, scope helpers);
 *               context-gateway-controls.js (toolbars);
 *               context-gateway-list.js / -detail.js (loadCtxList / loadCtxDetail)
 *   provides:   the delegated click handlers (no new consumed globals)
 */

// -- Sync / Import buttons (delegated) ----------------------------------------

// ADR-0026 P1b (#1353): the per-type Sync flow, lifted VERBATIM out of the
// delegated ``.ctx-sync-btn`` binding (the lifted body keeps its original
// indentation so the diff reads as a pure extract — no logic changed) so the
// Simple-mode rows can run it inline with the SAME confirm + impact preview +
// host-write disclosure that the read-only P1a rows only reached by routing
// into Advanced. ``btn`` is the element to spin; ``canonicalCount`` / ``noFanout``
// are the click-time snapshot the confirm describes (Advanced: section dataset;
// Simple: the row's overview counts); ``onComplete(type)`` is the post-sync
// refresh (Advanced → loadCtxList, Simple → loadCtxOverview).
async function _ctxRunSync(type, { btn, canonicalCount, noFanout, onComplete }) {
    // Guard against pressing Sync when the cwd has no canonical artifacts —
    // the request would resolve to a `no_canonical_root` skip with an info
    // toast, but that arrives after a confirm dialog, which is the wrong
    // shape of feedback for "this button does nothing right now."
    if (canonicalCount === '0') {
      const message = noFanout
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
    // ``canonicalCount`` is the caller's click-time snapshot, pinned alongside
    // the scope/tier above so the confirm describes the same (project, tier) the
    // POST writes to (Codex review).
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
        showToast(_ctxSyncErrToast(err), 'error');
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
          showToast(_ctxSyncErrToast(err), 'error');
          return;
        }
        data = await r.json();
      }
      // A user-tier sync may skip files on Gate A's secret-shape heuristic;
      // offer the reviewed force valve and re-sync on consent. The host paths
      // were already disclosed in the host-write confirm above, so the helper
      // resends with both flags (see its consent note).
      const forced = await _ctxMaybeForceUnsafeSync(data, syncOnce);
      if (forced) data = forced;
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
      if (onComplete) onComplete(type);
    } catch (err) {
      showToast(t('toast.sync_failed', { error: err.message }), 'error');
    } finally { btnLoading(btn, false); }
}

// Advanced (delegated): each per-section ``.ctx-sync-btn`` reads its click-time
// section snapshot and refreshes that section's list on success. The Simple-mode
// rows call ``_ctxRunSync`` directly with the overview counts + an Overview
// refresh (see ``_ctxWireSimpleRows``).
document.querySelectorAll('.ctx-sync-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const section = btn.closest('.settings-section');
    _ctxRunSync(btn.dataset.type, {
      btn,
      canonicalCount: section?.dataset.canonicalCount || '0',
      noFanout: section?.dataset.noFanout === 'true',
      onComplete: loadCtxList,
    });
  });
});

// ADR-0026 P1b (#1353): the per-type Import flow, lifted VERBATIM out of the
// delegated ``.ctx-import-btn`` binding (the lifted body keeps its original
// indentation so the diff reads as a pure extract) so the Simple-mode rows can
// run it inline with the SAME dry-run preview + overwrite opt-in + host-write
// disclosure. ``onComplete(type)`` is the post-import refresh (Advanced →
// loadCtxList, Simple → loadCtxOverview).
async function _ctxRunImport(type, { btn, onComplete }) {
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
        showToast(_ctxImportErrToast(r.status, err.detail), 'error');
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
          showToast(_ctxImportErrToast(r.status, err.detail), 'error');
          return;
        }
        data = await r.json();
      }
      // A user-tier import may skip files on Gate A's secret-shape heuristic;
      // offer the reviewed force valve. The helper re-imports through
      // ``importOnce`` (carrying its ``overwrite`` base body) and runs its own
      // host-write disclosure for the forced destinations before writing.
      const forced = await _ctxMaybeForceUnsafeImport(data, importOnce);
      if (forced) data = forced;
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
      if (onComplete) onComplete(type);
    } catch (err) {
      showToast(t('toast.import_failed', { error: err.message }), 'error');
    } finally { btnLoading(btn, false); }
}

// Advanced (delegated): each per-section ``.ctx-import-btn`` refreshes that
// section's list on success. Simple-mode rows call ``_ctxRunImport`` directly.
document.querySelectorAll('.ctx-import-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    _ctxRunImport(btn.dataset.type, { btn, onComplete: loadCtxList });
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
          if (_ctxMaybePrivacyToast(err)) return;
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
            if (_ctxMaybePrivacyToast(err)) return;
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
        if (data.created === false) {
          // ``created === false`` means the POST was a no-op: this root was
          // already tracked (the route is idempotent — #1292). Surface that as
          // an info toast BEFORE the warning branches — the no_runtime_marker
          // warning was already shown on the original add, so re-warning on a
          // no-op re-add is noise; the signal the user needs is "nothing
          // changed". ``=== false`` (not falsy) so an older server that omits
          // ``created`` keeps the prior success/warning behavior.
          showToast(t('settings.ctx.add_project_already_tracked'), 'info');
        } else if (warningKey) {
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

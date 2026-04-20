/**
 * Sources tab — Memory Dirs panel.
 *
 * Moved from the Config tab in issue #297: a full-width panel gives the
 * dir list enough horizontal space that paths don't truncate under the
 * 2-column config-table grid, and the immediate-persist action model
 * (add/remove/reindex each hit the server) is no longer an inconsistent
 * island inside the Config tab's batched Save/dirty flow.
 *
 * Depends on globals from app.js (api, showToast, showConfirm, t, qs,
 * STATE, btnLoading, loadStats). Loaded AFTER app.js.
 *
 * Classification is server-owned: each entry on
 * ``GET /api/memory-dirs/status`` carries ``category`` and ``provider``
 * fields from ``categorize_memory_dir`` / ``provider_for_category`` in
 * ``config.py``. The constants below are presentation-only (render
 * order, i18n label keys, default-collapse set).
 *
 * Layout: vendor → product tree per RFC #304 Phase 2. Single-leaf
 * vendors (``user``, ``openai``→``codex``) render as a flat one-row
 * ``<details>`` keyed by the product label, matching the previous
 * one-level UI. Multi-leaf vendors (``claude`` → ``claude-memory`` +
 * ``claude-plans``) render the vendor label at the outer summary with
 * each product as an inner section carrying its own reindex button.
 * Per-child collapse state is intentionally removed (Q4 resolution):
 * opening the ``claude`` vendor reveals both products without a second
 * click.
 */

const _MEMORY_DIR_CATEGORY_ORDER = ['user', 'claude-memory', 'claude-plans', 'codex'];
const _MEMORY_DIR_CATEGORY_LABEL_KEY = {
  'user': 'sources.memory_dirs.category.user',
  'claude-memory': 'sources.memory_dirs.category.claude_memory',
  'claude-plans': 'sources.memory_dirs.category.claude_plans',
  'codex': 'sources.memory_dirs.category.codex',
};
// Render order for vendor groups. See ``_CATEGORY_TO_PROVIDER`` in
// ``config.py`` for the category→provider mapping (server-owned).
const _MEMORY_DIR_PROVIDER_ORDER = ['user', 'claude', 'openai'];
const _MEMORY_DIR_PROVIDER_LABEL_KEY = {
  'user': 'sources.memory_dirs.provider.user',
  'claude': 'sources.memory_dirs.provider.claude',
  'openai': 'sources.memory_dirs.provider.openai',
};
// Vendor groups that start collapsed. ``user`` is open by default
// because it is usually short; auto-discovered vendor groups can have
// 20+ entries and would push the file list below the fold.
const _MEMORY_DIR_PROVIDER_COLLAPSED = new Set(['claude', 'openai']);
// Forward-compat: an ``/api/memory-dirs/status`` response carrying a
// ``provider`` value the client doesn't recognize (e.g., a newer server
// adds a vendor before the client deploys) falls through to ``user`` so
// the dirs stay visible under the tree. Missing i18n keys fall back to
// the raw key string via ``t()``'s built-in ``|| key`` path — no extra
// guard needed here.
const _MEMORY_DIR_PROVIDER_FALLBACK = 'user';

function _buildMemoryDirsPanel(initialDirs) {
  const wrap = document.createElement('div');
  wrap.className = 'memory-dirs-widget';

  let dirs = Array.isArray(initialDirs) ? [...initialDirs] : [];
  let statusByPath = {};
  let statusLoaded = false;

  function _apiErrorText(err) {
    return (err && err.message) ? err.message : String(err);
  }

  async function fetchStatus() {
    try {
      const resp = await api('GET', '/api/memory-dirs/status');
      const next = {};
      for (const entry of (resp && resp.dirs) || []) {
        if (entry && typeof entry.path === 'string') next[entry.path] = entry;
      }
      statusByPath = next;
      statusLoaded = true;
    } catch (err) {
      console.warn('memory-dirs/status fetch failed:', err);
      statusByPath = {};
      statusLoaded = true;
    }
    render();
  }

  function refreshDirs(newDirs) {
    if (Array.isArray(newDirs)) dirs = [...newDirs];
    if (STATE.serverConfig?.indexing) {
      STATE.serverConfig.indexing.memory_dirs = [...dirs];
    }
    render();
    fetchStatus();
  }

  async function handleAdd(path) {
    const trimmed = path.trim();
    if (!trimmed) return;
    try {
      const resp = await api('POST', '/api/memory-dirs/add', { path: trimmed });
      if (resp && Array.isArray(resp.memory_dirs)) {
        refreshDirs(resp.memory_dirs);
      }
      showToast(t('toast.memory_dir.added', { path: trimmed }), 'success');
    } catch (err) {
      showToast(t('toast.memory_dir.add_failed', { error: _apiErrorText(err) }), 'error');
    }
  }

  async function handleRemove(path) {
    const ok = await showConfirm({
      title: t('confirm.memory_dir_remove_title'),
      message: t('confirm.memory_dir_remove_msg', { path }),
    });
    if (!ok) return;
    try {
      const resp = await api('POST', '/api/memory-dirs/remove', { path });
      if (resp && Array.isArray(resp.memory_dirs)) {
        refreshDirs(resp.memory_dirs);
      }
      showToast(t('toast.memory_dir.removed', { path }), 'success');
    } catch (err) {
      showToast(t('toast.memory_dir.remove_failed', { error: _apiErrorText(err) }), 'error');
    }
  }

  async function handleReindexOne(path, btn) {
    if (btn) btnLoading(btn, true);
    showToast(t('toast.memory_dir.reindex_started', { path }), 'info');
    try {
      const resp = await api(
        'POST', '/api/index',
        { path, recursive: true, force: false },
        { timeout: 300_000 },
      );
      const count = (resp && resp.indexed_chunks) || 0;
      showToast(
        t('toast.memory_dir.reindex_done', { path, count }),
        (resp && resp.errors && resp.errors.length) ? 'error' : 'success',
      );
      if (typeof _markDataStale === 'function') _markDataStale();
      if (typeof loadStats === 'function') loadStats();
    } catch (err) {
      showToast(t('toast.memory_dir.reindex_failed', { error: _apiErrorText(err) }), 'error');
    } finally {
      if (btn) btnLoading(btn, false);
      fetchStatus();
    }
  }

  async function handleReindexGroup(category, btn) {
    const targets = dirs.filter(d => {
      const st = statusByPath[d];
      return ((st && st.category) || 'user') === category;
    });
    if (!targets.length) return;
    if (btn) btnLoading(btn, true);
    try {
      for (const path of targets) {
        await handleReindexOne(path, null);
      }
    } finally {
      if (btn) btnLoading(btn, false);
    }
  }

  async function handleReindexAll(btn) {
    if (btn) btnLoading(btn, true);
    try {
      const resp = await api('POST', '/api/reindex', undefined, { timeout: 300_000 });
      if (resp.errors && resp.errors.length) {
        showToast(
          t('toast.reindex_partial', { count: resp.errors.length, first: resp.errors[0] }),
          'error',
        );
      } else {
        const total = (resp.results || []).reduce((s, r) => s + (r.indexed_chunks || 0), 0);
        showToast(t('toast.reindex_complete', { count: total }), 'success');
      }
      if (typeof _markDataStale === 'function') _markDataStale();
      if (typeof loadStats === 'function') loadStats();
    } catch (err) {
      showToast(t('toast.reindex_failed', { error: _apiErrorText(err) }), 'error');
    } finally {
      if (btn) btnLoading(btn, false);
      fetchStatus();
    }
  }

  let _addOpen = false;
  function render() {
    wrap.innerHTML = '';

    // Single-row header: title · total summary · [+ Add] [↻ Reindex all]
    const header = document.createElement('div');
    header.className = 'memory-dirs-header';

    const titleGroup = document.createElement('div');
    titleGroup.className = 'memory-dirs-header-title';
    const title = document.createElement('h3');
    title.className = 'memory-dirs-title';
    title.textContent = t('sources.memory_dirs.title');
    titleGroup.appendChild(title);

    // Inline total count: "1 dir" / "29 dirs" — keeps the user oriented
    // when every group is collapsed.
    const totalCount = document.createElement('span');
    totalCount.className = 'memory-dirs-total';
    totalCount.textContent = t(
      dirs.length === 1 ? 'sources.memory_dirs.total_one' : 'sources.memory_dirs.total_many',
      { count: dirs.length },
    );
    titleGroup.appendChild(totalCount);
    header.appendChild(titleGroup);

    const actions = document.createElement('div');
    actions.className = 'memory-dirs-actions';

    const addToggleBtn = document.createElement('button');
    addToggleBtn.type = 'button';
    addToggleBtn.className = 'btn btn-sm btn-ghost memory-dirs-add-toggle';
    addToggleBtn.textContent = t('sources.memory_dirs.add_btn');
    addToggleBtn.addEventListener('click', () => {
      _addOpen = !_addOpen;
      render();
      if (_addOpen) {
        const nextInput = wrap.querySelector('.memory-dirs-add-input');
        if (nextInput) nextInput.focus();
      }
    });
    actions.appendChild(addToggleBtn);

    const reindexAllBtn = document.createElement('button');
    reindexAllBtn.type = 'button';
    reindexAllBtn.className = 'btn btn-sm btn-ghost';
    reindexAllBtn.textContent = t('sources.memory_dirs.reindex_all');
    reindexAllBtn.addEventListener('click', () => handleReindexAll(reindexAllBtn));
    actions.appendChild(reindexAllBtn);
    header.appendChild(actions);
    wrap.appendChild(header);

    // Inline add-path form, toggled via the "+ Add" header button.
    if (_addOpen) {
      const addRow = document.createElement('div');
      addRow.className = 'memory-dirs-add';
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'memory-dirs-add-input';
      input.placeholder = t('sources.memory_dirs.add_placeholder');
      const submit = document.createElement('button');
      submit.type = 'button';
      submit.className = 'btn btn-sm btn-primary';
      submit.textContent = t('sources.memory_dirs.add_submit');
      submit.addEventListener('click', async () => {
        const val = input.value;
        input.value = '';
        await handleAdd(val);
        _addOpen = false;
        render();
      });
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'btn btn-sm btn-ghost';
      cancel.textContent = t('sources.memory_dirs.add_cancel');
      cancel.addEventListener('click', () => { _addOpen = false; render(); });
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); submit.click(); }
        else if (ev.key === 'Escape') { ev.preventDefault(); cancel.click(); }
      });
      addRow.appendChild(input);
      addRow.appendChild(submit);
      addRow.appendChild(cancel);
      wrap.appendChild(addRow);
    }

    // Group dirs by provider → category. Server delivers both fields on
    // ``/api/memory-dirs/status``; before the first fetch resolves we
    // fall back to ``user`` so the tree layout renders without crashing
    // and the next render settles each entry.
    const byProvider = {};
    for (const p of _MEMORY_DIR_PROVIDER_ORDER) byProvider[p] = { order: [], byCategory: {} };
    for (const d of dirs) {
      const st = statusByPath[d];
      const cat = (st && _MEMORY_DIR_CATEGORY_LABEL_KEY[st.category]) ? st.category : 'user';
      const rawProvider = st && st.provider;
      const provider = byProvider[rawProvider] ? rawProvider : _MEMORY_DIR_PROVIDER_FALLBACK;
      const bucket = byProvider[provider];
      if (!bucket.byCategory[cat]) {
        bucket.byCategory[cat] = [];
        bucket.order.push(cat);
      }
      bucket.byCategory[cat].push(d);
    }

    function _buildItemRow(path, st) {
      const item = document.createElement('li');
      item.className = 'memory-dirs-item';
      if (statusLoaded && st) {
        if (st.chunk_count === 0) item.classList.add('memory-dirs-item-empty');
        if (st.exists === false) item.classList.add('memory-dirs-item-missing');
      }

      const pathSpan = document.createElement('span');
      pathSpan.className = 'memory-dirs-path';
      pathSpan.textContent = path;
      pathSpan.title = path;
      item.appendChild(pathSpan);

      if (statusLoaded && st) {
        const badge = document.createElement('span');
        badge.className = 'memory-dirs-status';
        if (st.exists === false) {
          badge.classList.add('missing');
          badge.textContent = t('sources.memory_dirs.status_missing');
        } else if ((st.chunk_count || 0) === 0) {
          badge.classList.add('empty');
          badge.textContent = t('sources.memory_dirs.status_empty');
        } else {
          badge.textContent = t(
            'sources.memory_dirs.status_chunks',
            { count: st.chunk_count },
          );
        }
        item.appendChild(badge);
      } else {
        // Placeholder so the action buttons line up before status loads.
        const ph = document.createElement('span');
        ph.className = 'memory-dirs-status placeholder';
        item.appendChild(ph);
      }

      const reindexBtn = document.createElement('button');
      reindexBtn.type = 'button';
      reindexBtn.className = 'btn btn-xs btn-ghost memory-dirs-reindex-btn';
      // Label tracks state: "Index" before first index / when missing or
      // empty, "Reindex" once chunks exist. Tooltip stays generic since
      // the action is a reindex in both cases (force:false, recursive).
      const hasChunks =
        statusLoaded && st && st.exists !== false && (st.chunk_count || 0) > 0;
      reindexBtn.textContent = t(
        hasChunks ? 'sources.memory_dirs.action_reindex' : 'sources.memory_dirs.action_index',
      );
      reindexBtn.title = t('sources.memory_dirs.reindex_title');
      reindexBtn.addEventListener('click', () => handleReindexOne(path, reindexBtn));
      item.appendChild(reindexBtn);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'btn btn-xs btn-ghost memory-dirs-remove-btn';
      removeBtn.textContent = t('sources.memory_dirs.action_delete');
      removeBtn.title = t('sources.memory_dirs.delete_title');
      removeBtn.setAttribute('aria-label', t('sources.memory_dirs.delete_title'));
      if (dirs.length <= 1) removeBtn.disabled = true;
      removeBtn.addEventListener('click', () => handleRemove(path));
      item.appendChild(removeBtn);

      return item;
    }

    function _buildList(cat, entries) {
      const list = document.createElement('ul');
      list.className = 'memory-dirs-list';
      // ``.memory-dirs-list-scroll`` stays bound to the ``claude-memory``
      // *leaf*, not the new vendor wrapper — per-project auto-memory
      // dirs can be 20+ entries and need the 280px scrollbox; other
      // leaves stay un-capped.
      if (cat === 'claude-memory') list.classList.add('memory-dirs-list-scroll');
      for (const path of entries) list.appendChild(_buildItemRow(path, statusByPath[path]));
      return list;
    }

    function _aggregateStatus(entries) {
      let chunks = 0;
      let files = 0;
      let any = false;
      for (const path of entries) {
        const st = statusByPath[path];
        if (st) {
          any = true;
          chunks += st.chunk_count || 0;
          files += st.source_file_count || 0;
        }
      }
      return { chunks, files, any };
    }

    function _buildStatusBadge(aggregate) {
      const badge = document.createElement('span');
      badge.className = 'memory-dirs-status-group';
      if (aggregate.chunks === 0) badge.classList.add('empty');
      badge.textContent = t(
        'sources.memory_dirs.status_group',
        { files: aggregate.files, chunks: aggregate.chunks },
      );
      return badge;
    }

    function _buildGroupReindexButton(cat) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-xs btn-ghost memory-dirs-group-reindex';
      btn.textContent = t('sources.memory_dirs.action_reindex_group');
      btn.title = t('sources.memory_dirs.reindex_group');
      btn.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        handleReindexGroup(cat, btn);
      });
      return btn;
    }

    for (const provider of _MEMORY_DIR_PROVIDER_ORDER) {
      const bucket = byProvider[provider];
      if (!bucket.order.length) continue;

      // Categories within this vendor rendered in the global category
      // order (``user`` → ``claude-memory`` → ``claude-plans`` → ``codex``)
      // so the product rows stay stable as the user adds/removes dirs.
      const categories = _MEMORY_DIR_CATEGORY_ORDER.filter(c => bucket.byCategory[c]);
      const allEntries = categories.flatMap(c => bucket.byCategory[c]);
      const isSingleLeaf = categories.length === 1;

      const group = document.createElement('details');
      group.className = 'memory-dirs-group';
      if (!isSingleLeaf) group.classList.add('memory-dirs-vendor-group');
      group.dataset.provider = provider;
      if (!_MEMORY_DIR_PROVIDER_COLLAPSED.has(provider)) group.open = true;

      const summary = document.createElement('summary');
      summary.className = 'memory-dirs-summary';

      const label = document.createElement('span');
      label.className = 'memory-dirs-summary-label';
      // Single-leaf vendors collapse to one row labeled by the product
      // (keeps "Codex" readable rather than the distant "OpenAI"); multi-
      // leaf vendors use the vendor label at the top and move product
      // labels to inner sections (Q4 — no per-child collapse).
      const summaryLabelKey = isSingleLeaf
        ? _MEMORY_DIR_CATEGORY_LABEL_KEY[categories[0]]
        : _MEMORY_DIR_PROVIDER_LABEL_KEY[provider];
      label.textContent = t(summaryLabelKey);
      summary.appendChild(label);

      const count = document.createElement('span');
      count.className = 'memory-dirs-summary-count';
      count.textContent = String(allEntries.length);
      summary.appendChild(count);

      const vendorAgg = _aggregateStatus(allEntries);
      if (statusLoaded && vendorAgg.any) summary.appendChild(_buildStatusBadge(vendorAgg));

      // Per-product reindex stays at the product level; no vendor bulk
      // button (plan Q5). For single-leaf vendors the product *is* the
      // vendor, so the button belongs on the summary row.
      if (isSingleLeaf) summary.appendChild(_buildGroupReindexButton(categories[0]));
      group.appendChild(summary);

      if (isSingleLeaf) {
        group.appendChild(_buildList(categories[0], bucket.byCategory[categories[0]]));
      } else {
        const products = document.createElement('div');
        products.className = 'memory-dirs-products';
        for (const cat of categories) {
          const entries = bucket.byCategory[cat];
          const section = document.createElement('section');
          section.className = 'memory-dirs-product';
          section.dataset.category = cat;

          const header = document.createElement('div');
          header.className = 'memory-dirs-product-header';

          const productLabel = document.createElement('span');
          productLabel.className = 'memory-dirs-product-label';
          productLabel.textContent = t(_MEMORY_DIR_CATEGORY_LABEL_KEY[cat]);
          header.appendChild(productLabel);

          const productCount = document.createElement('span');
          productCount.className = 'memory-dirs-summary-count';
          productCount.textContent = String(entries.length);
          header.appendChild(productCount);

          const productAgg = _aggregateStatus(entries);
          if (statusLoaded && productAgg.any) {
            header.appendChild(_buildStatusBadge(productAgg));
          }

          header.appendChild(_buildGroupReindexButton(cat));
          section.appendChild(header);
          section.appendChild(_buildList(cat, entries));
          products.appendChild(section);
        }
        group.appendChild(products);
      }

      wrap.appendChild(group);
    }
  }

  render();
  fetchStatus();
  return wrap;
}

// Public entry — called from app.js when the Sources tab activates.
// Idempotent: replaces panel contents each call so the widget picks up
// config mutations made in the Config tab (e.g., memory_dirs toggled via
// ``mm config set`` then reflected after external reload).
function renderMemoryDirsPanel() {
  const container = qs('memory-dirs-panel');
  if (!container) return;
  const dirs = STATE.serverConfig?.indexing?.memory_dirs || [];
  container.innerHTML = '';
  container.appendChild(_buildMemoryDirsPanel(dirs));
}

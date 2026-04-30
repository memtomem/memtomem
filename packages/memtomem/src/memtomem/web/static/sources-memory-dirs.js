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

// Sort dropdown only renders once a product leaf has at least this many
// entries — short leaves (1-3 dirs) don't benefit and the dropdown would
// add visual noise. ``claude-memory`` is the dominant case (one entry per
// project under ``~/.claude/projects``).
const _MEMORY_DIRS_SORT_THRESHOLD = 6;
const _MEMORY_DIRS_SORT_KEYS = [
  'created_desc', 'created_asc', 'path_asc',
  'files_desc', 'chunks_desc', 'last_indexed_desc',
];
const _MEMORY_DIRS_SORT_DEFAULT = 'created_desc';
const _MEMORY_DIRS_SORT_LS_PREFIX = 'memtomem.memory_dirs.sort.';

function _buildMemoryDirsPanel(initialDirs) {
  const wrap = document.createElement('div');
  wrap.className = 'memory-dirs-widget';

  let dirs = Array.isArray(initialDirs) ? [...initialDirs] : [];
  let statusByPath = {};
  let statusLoaded = false;
  // Memoized ``GET /api/sources?kind=memory`` response, indexed by
  // ``memory_dir``. The cache scope is intentionally per-render: it
  // lives in this closure, so re-rendering the panel (mode toggle,
  // tab switch, ``refreshDirs`` after add/remove) drops it on the
  // floor and the next render refetches against current config —
  // worth the extra round trip to avoid stale cache after off-panel
  // changes. Within one render generation, drill-ins after the first
  // reuse the cached response. Reindex also invalidates explicitly
  // so the file list picks up new chunks without a full re-render.
  let _memorySourcesByDir = null;
  let _memorySourcesPromise = null;
  function _invalidateSourcesCache() {
    _memorySourcesByDir = null;
    _memorySourcesPromise = null;
  }

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
    _invalidateSourcesCache();
    render();
    fetchStatus();
  }

  async function handleAdd(path) {
    const trimmed = path.trim();
    if (!trimmed) return;
    try {
      // ``auto_index=true`` collapses the historic two-step (register →
      // manually click Index) into a single click. The server registers,
      // then runs ``index_path`` outside the config lock so the request
      // can stream back chunk stats without blocking other config writes.
      const resp = await api('POST', '/api/memory-dirs/add', {
        path: trimmed,
        auto_index: true,
      });
      if (resp && Array.isArray(resp.memory_dirs)) {
        refreshDirs(resp.memory_dirs);
      }
      const stats = resp && resp.indexed;
      if (stats && typeof stats.indexed_chunks === 'number') {
        showToast(
          t('toast.memory_dir.added_indexed', {
            path: trimmed,
            chunks: stats.indexed_chunks,
            files: stats.total_files,
          }),
          'success',
        );
      } else {
        showToast(t('toast.memory_dir.added', { path: trimmed }), 'success');
      }
    } catch (err) {
      showToast(t('toast.memory_dir.add_failed', { error: _apiErrorText(err) }), 'error');
    }
  }

  async function _fetchMemorySources() {
    // Single in-flight promise — avoids racing fetches when the user
    // mashes multiple path rows in quick succession before the first
    // one has a chance to populate the cache.
    if (_memorySourcesByDir !== null) return _memorySourcesByDir;
    if (_memorySourcesPromise) return _memorySourcesPromise;
    _memorySourcesPromise = (async () => {
      // ``limit=10000`` matches the route's hard cap. With the unified
      // single-panel view we ask for every indexed source (no kind
      // filter) so user-added dirs that classify as ``general`` still
      // surface their file rows in their vendor group. Hitting the cap
      // would be a pathological config and the user would see truncated
      // drill-ins, not a crash.
      const data = await api('GET', '/api/sources?limit=10000');
      const byDir = {};
      for (const s of (data && data.sources) || []) {
        const key = s.memory_dir || '';
        if (!byDir[key]) byDir[key] = [];
        byDir[key].push(s);
      }
      _memorySourcesByDir = byDir;
      return byDir;
    })();
    try {
      return await _memorySourcesPromise;
    } finally {
      _memorySourcesPromise = null;
    }
  }

  async function _toggleDirExpand(path, item, pathBtn) {
    // Second click collapses without re-fetching — a no-op on the
    // network even when the cache later becomes invalid (next render
    // rebuilds rows fresh anyway).
    if (item.classList.contains('memory-dirs-item-expanded')) {
      item.classList.remove('memory-dirs-item-expanded');
      pathBtn.setAttribute('aria-expanded', 'false');
      const existing = item.querySelector('.memory-dirs-files');
      if (existing) existing.remove();
      return;
    }

    const filesWrap = document.createElement('div');
    filesWrap.className = 'memory-dirs-files';
    const loader = document.createElement('div');
    loader.className = 'memory-dirs-files-loading';
    loader.textContent = t('common.loading');
    filesWrap.appendChild(loader);
    item.appendChild(filesWrap);
    item.classList.add('memory-dirs-item-expanded');
    pathBtn.setAttribute('aria-expanded', 'true');

    let byDir;
    try {
      byDir = await _fetchMemorySources();
    } catch (err) {
      filesWrap.innerHTML = '';
      const errEl = document.createElement('div');
      errEl.className = 'memory-dirs-files-empty';
      errEl.textContent = _apiErrorText(err);
      filesWrap.appendChild(errEl);
      return;
    }

    // ``memory_dir`` on each ``SourceOut`` is the configured dir
    // after ``Path(d).expanduser().resolve()`` on the server — so
    // ``~/memories`` in config arrives here as ``/Users/x/memories``.
    // Exact-match works for already-absolute entries; tilde-prefixed
    // and trailing-slash forms need the suffix fallback below.
    let entries = byDir[path] || [];
    if (!entries.length) {
      // Strip leading ``~`` and trailing slashes, then look for any
      // configured dir whose resolved key matches that suffix. JS has
      // no ``expanduser`` so we lean on the suffix-uniqueness of
      // absolute paths in practice (``~/memories`` matches
      // ``/Users/x/memories`` but not ``/code/memory-game``). First
      // match wins; collisions would require two dirs sharing the
      // same trailing segments, which would already be ambiguous in
      // the panel UI.
      const trimmed = path.replace(/^~/, '').replace(/\/+$/, '');
      if (trimmed) {
        for (const [dirKey, list] of Object.entries(byDir)) {
          if (!dirKey) continue;
          if (dirKey === trimmed || dirKey.endsWith(trimmed)) {
            entries = list;
            break;
          }
        }
      }
    }

    filesWrap.innerHTML = '';
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'memory-dirs-files-empty';
      empty.textContent = t('sources.memory_dirs.files_empty');
      filesWrap.appendChild(empty);
      return;
    }

    entries.sort((a, b) => a.path.localeCompare(b.path));
    const list = document.createElement('ul');
    list.className = 'memory-dirs-files-list';
    for (const s of entries) {
      const li = document.createElement('li');
      li.className = 'memory-dirs-file';
      li.title = s.path;
      // Keyboard-activatable row: ``role="button"`` + ``tabindex=0`` puts
      // the row in tab order, and the keydown handler accepts Enter and
      // Space the same way a native ``<button>`` does. Mirrors the
      // source-tree rows in ``app.js:_renderSourceTree`` so the drill-in
      // feels consistent with the General view.
      li.setAttribute('role', 'button');
      li.setAttribute('tabindex', '0');
      const filename = s.path.split('/').pop() || s.path;
      const name = document.createElement('span');
      name.className = 'memory-dirs-file-name';
      name.textContent = filename;
      const meta = document.createElement('span');
      meta.className = 'memory-dirs-file-meta';
      meta.textContent = t(
        'sources.memory_dirs.file_meta',
        { chunks: s.chunk_count || 0 },
      );
      li.appendChild(name);
      li.appendChild(meta);
      const activate = () => {
        // Single Sources panel: just open the source in the shared
        // chunks-browser pane.
        if (typeof browseSource === 'function') {
          browseSource(s.path);
        }
      };
      li.addEventListener('click', activate);
      li.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          activate();
        }
      });
      list.appendChild(li);
    }
    filesWrap.appendChild(list);
  }

  async function handleRemove(path) {
    // Offer chunk cleanup as an opt-in checkbox when the dir actually
    // has indexed chunks. Default unchecked — the destructive path
    // requires a deliberate click, mirroring the existing safe-by-
    // default remove semantics. Dirs with zero chunks fall back to the
    // simple boolean confirm.
    const st = statusByPath[path];
    const chunkCount = (st && st.chunk_count) || 0;
    const extraOption = chunkCount > 0
      ? {
          id: 'deleteChunks',
          label: t('confirm.memory_dir_delete_chunks_label', { count: chunkCount }),
          defaultChecked: false,
        }
      : null;
    const result = await showConfirm({
      title: t('confirm.memory_dir_remove_title'),
      message: t('confirm.memory_dir_remove_msg', { path }),
      extraOption,
    });
    const ok = extraOption ? result && result.ok : result;
    if (!ok) return;
    const deleteChunks = !!(extraOption && result && result.extras && result.extras.deleteChunks);
    try {
      const resp = await api('POST', '/api/memory-dirs/remove', {
        path, delete_chunks: deleteChunks,
      });
      if (resp && Array.isArray(resp.memory_dirs)) {
        refreshDirs(resp.memory_dirs);
      }
      const deleted = (resp && resp.deleted_chunks) || 0;
      if (deleteChunks && deleted > 0) {
        showToast(
          t('toast.memory_dir.removed_with_chunks', { path, count: deleted }),
          'success',
        );
      } else {
        showToast(t('toast.memory_dir.removed', { path }), 'success');
      }
    } catch (err) {
      showToast(t('toast.memory_dir.remove_failed', { error: _apiErrorText(err) }), 'error');
    }
  }

  async function handleOpenOne(path, btn) {
    if (btn) btnLoading(btn, true);
    try {
      await api('POST', '/api/memory-dirs/open', { path });
      showToast(t('toast.memory_dir.opened', { path }), 'success');
    } catch (err) {
      showToast(t('toast.memory_dir.open_failed', { error: _apiErrorText(err) }), 'error');
    } finally {
      if (btn) btnLoading(btn, false);
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
      _invalidateSourcesCache();
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
    // Capture current vendor-group open state so a sort-dropdown change
    // (or any other mid-flight render) doesn't collapse the group the
    // user is actively looking at. Falls through to the default-collapse
    // set on first render or when the group isn't in the DOM yet.
    const openByProvider = {};
    for (const detail of wrap.querySelectorAll('details.memory-dirs-group')) {
      const p = detail.dataset.provider;
      if (p) openByProvider[p] = detail.open;
    }
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
      dirs.length === 1 ? 'sources.memory_dirs.total_one' : 'sources.memory_dirs.total_other',
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

    function _formatDateTime(iso) {
      // Locale-formatted "{date} {time}" — drops the year for current-year
      // dates so the row stays compact, keeps the year for older entries
      // so historical rows remain unambiguous. Time is always shown so
      // sort modes that hinge on minutes (e.g. burst indexing runs) can
      // be verified at a glance.
      if (!iso) return '';
      const d = new Date(iso);
      if (isNaN(d.getTime())) return '';
      const now = new Date();
      const sameYear = d.getFullYear() === now.getFullYear();
      const dateOpts = sameYear
        ? { month: 'short', day: 'numeric' }
        : { year: 'numeric', month: 'short', day: 'numeric' };
      const timeOpts = { hour: '2-digit', minute: '2-digit' };
      return `${d.toLocaleDateString(undefined, dateOpts)} `
        + `${d.toLocaleTimeString(undefined, timeOpts)}`;
    }

    function _buildItemMetaText(st) {
      // Single-line meta string: ``created {dt} · indexed {dt} ·
      // {files} files · {chunks} chunks``. ``file_count`` (from disk)
      // is the source of truth for "files" — ``source_file_count``
      // only counts files that already have chunks, so un-indexed
      // dirs would read "0 files" without it. Showing the disk count
      // also gives the user a heads-up about how big a Reindex is
      // about to be.
      if (!st) return '';
      if (st.exists === false) return t('sources.memory_dirs.status_missing');

      const parts = [];
      if (st.created_at) {
        parts.push(t(
          'sources.memory_dirs.meta_created',
          { dt: _formatDateTime(st.created_at) },
        ));
      }
      if (st.last_indexed) {
        parts.push(t(
          'sources.memory_dirs.meta_indexed',
          { dt: _formatDateTime(st.last_indexed) },
        ));
      }
      const fileCount = (typeof st.file_count === 'number') ? st.file_count : 0;
      const chunkCount = st.chunk_count || 0;
      const indexedCount = st.source_file_count || 0;
      if (chunkCount > 0) {
        // Indexed: show indexed/disk files + chunks together. Same
        // ``{indexed}/{files}`` shape as the group label so the row's
        // own progress (e.g. ``18/18`` fully indexed) is visible at a
        // glance.
        parts.push(t(
          'sources.memory_dirs.status_group',
          { files: fileCount, indexed: indexedCount, chunks: chunkCount },
        ));
      } else if (fileCount > 0) {
        // Un-indexed but has files on disk → "{N} files · not indexed"
        // so the user sees both how much content is waiting and the
        // current state.
        parts.push(t('sources.memory_dirs.status_files_only', { count: fileCount }));
        parts.push(t('sources.memory_dirs.status_empty'));
      } else {
        // Truly empty dir (no supported files on disk).
        parts.push(t('sources.memory_dirs.status_empty'));
      }
      return parts.join(' · ');
    }

    function _buildItemRow(path, st) {
      // Two-row layout: ``main`` carries the path + action buttons,
      // ``meta`` carries created/indexed timestamps and counts so the
      // user can verify any sort mode (newest first, recently indexed,
      // most files / chunks) directly on each row. The meta sub-row
      // tucks under the path with mono-font muted styling so it doesn't
      // compete with the actionable controls visually.
      const item = document.createElement('li');
      item.className = 'memory-dirs-item';
      if (statusLoaded && st) {
        if (st.chunk_count === 0) item.classList.add('memory-dirs-item-empty');
        if (st.exists === false) item.classList.add('memory-dirs-item-missing');
      }

      const mainRow = document.createElement('div');
      mainRow.className = 'memory-dirs-item-main';

      // Path is the row's primary affordance — clicking it expands a
      // file list inline so the user can drill down to chunks without
      // hopping to the General view. ``<button>`` rather than a span so
      // it gets keyboard activation and a focus ring for free.
      const pathBtn = document.createElement('button');
      pathBtn.type = 'button';
      pathBtn.className = 'memory-dirs-path';
      pathBtn.textContent = path;
      pathBtn.title = path;
      pathBtn.setAttribute('aria-expanded', 'false');
      pathBtn.addEventListener('click', () => _toggleDirExpand(path, item, pathBtn));
      mainRow.appendChild(pathBtn);

      const openBtn = document.createElement('button');
      openBtn.type = 'button';
      openBtn.className = 'btn btn-xs btn-ghost memory-dirs-open-btn';
      openBtn.textContent = t('sources.memory_dirs.action_open');
      openBtn.title = t('sources.memory_dirs.open_title');
      // Disable when the dir isn't on disk — spawning a file manager
      // pointed at a missing path produces a confusing OS error popup
      // on macOS / a "location is not available" dialog on Windows.
      if (statusLoaded && st && st.exists === false) openBtn.disabled = true;
      openBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        handleOpenOne(path, openBtn);
      });
      mainRow.appendChild(openBtn);

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
      reindexBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        handleReindexOne(path, reindexBtn);
      });
      mainRow.appendChild(reindexBtn);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'btn btn-xs btn-ghost memory-dirs-remove-btn';
      removeBtn.textContent = t('sources.memory_dirs.action_delete');
      removeBtn.title = t('sources.memory_dirs.delete_title');
      removeBtn.setAttribute('aria-label', t('sources.memory_dirs.delete_title'));
      if (dirs.length <= 1) removeBtn.disabled = true;
      removeBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        handleRemove(path);
      });
      mainRow.appendChild(removeBtn);

      item.appendChild(mainRow);

      // Always render the meta row (even when status hasn't loaded yet)
      // so the row height stays stable across the initial fetch — no
      // jumpy reflow when ``/api/memory-dirs/status`` settles.
      const metaRow = document.createElement('div');
      metaRow.className = 'memory-dirs-item-meta';
      if (statusLoaded && st) {
        if (st.exists === false) metaRow.classList.add('missing');
        else if ((st.chunk_count || 0) === 0) metaRow.classList.add('empty');
        metaRow.textContent = _buildItemMetaText(st);
      } else {
        // U+00A0 reserves vertical space without showing visible text.
        metaRow.textContent = ' ';
      }
      item.appendChild(metaRow);

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

    function _readSortPref(productKey) {
      try {
        const stored = localStorage.getItem(_MEMORY_DIRS_SORT_LS_PREFIX + productKey);
        if (stored && _MEMORY_DIRS_SORT_KEYS.includes(stored)) return stored;
      } catch (_err) { /* localStorage may be unavailable in private modes */ }
      return _MEMORY_DIRS_SORT_DEFAULT;
    }

    function _writeSortPref(productKey, sortKey) {
      try {
        localStorage.setItem(_MEMORY_DIRS_SORT_LS_PREFIX + productKey, sortKey);
      } catch (_err) { /* ignore — sort preference is best-effort */ }
    }

    function _sortEntries(entries, sortKey) {
      // Returns a new array — never mutates the input. ``null`` /
      // missing values always sink to the bottom regardless of asc/desc
      // so un-indexed dirs don't dominate "Recently indexed" or jump
      // ahead of real timestamps in "Newest first".
      const arr = [...entries];
      const pathCmp = (a, b) => a.localeCompare(b);
      const byNumDesc = (key) => (a, b) => {
        const sa = statusByPath[a];
        const sb = statusByPath[b];
        const va = (sa && typeof sa[key] === 'number') ? sa[key] : 0;
        const vb = (sb && typeof sb[key] === 'number') ? sb[key] : 0;
        if (vb !== va) return vb - va;
        return pathCmp(a, b);
      };
      const byStrDesc = (key) => (a, b) => {
        const sa = statusByPath[a];
        const sb = statusByPath[b];
        const va = sa && sa[key];
        const vb = sb && sb[key];
        if (va && vb) return vb < va ? -1 : (vb > va ? 1 : pathCmp(a, b));
        if (va) return -1; // a has value, b doesn't → a first
        if (vb) return 1;
        return pathCmp(a, b);
      };
      const byStrAsc = (key) => (a, b) => {
        const sa = statusByPath[a];
        const sb = statusByPath[b];
        const va = sa && sa[key];
        const vb = sb && sb[key];
        if (va && vb) return va < vb ? -1 : (va > vb ? 1 : pathCmp(a, b));
        if (va) return -1;
        if (vb) return 1;
        return pathCmp(a, b);
      };

      switch (sortKey) {
        case 'path_asc': arr.sort(pathCmp); break;
        // ``file_count`` reflects the disk truth (matches the badge),
        // so "Most files" sorts by what the user actually sees rather
        // than the indexed-only ``source_file_count`` which is 0
        // until the dir is reindexed.
        case 'files_desc': arr.sort(byNumDesc('file_count')); break;
        case 'chunks_desc': arr.sort(byNumDesc('chunk_count')); break;
        case 'created_desc': arr.sort(byStrDesc('created_at')); break;
        case 'created_asc': arr.sort(byStrAsc('created_at')); break;
        case 'last_indexed_desc': arr.sort(byStrDesc('last_indexed')); break;
        default: arr.sort(byStrDesc('created_at'));
      }
      return arr;
    }

    function _buildSortDropdown(productKey, currentValue) {
      const select = document.createElement('select');
      select.className = 'memory-dirs-sort';
      select.setAttribute('aria-label', t('sources.memory_dirs.sort_label'));
      select.title = t('sources.memory_dirs.sort_label');
      for (const key of _MEMORY_DIRS_SORT_KEYS) {
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = t('sources.memory_dirs.sort.' + key);
        if (key === currentValue) opt.selected = true;
        select.appendChild(opt);
      }
      select.addEventListener('change', (ev) => {
        ev.stopPropagation();
        _writeSortPref(productKey, select.value);
        render();
      });
      // Stop click bubbling so opening the dropdown inside a ``<summary>``
      // doesn't toggle the parent ``<details>``.
      select.addEventListener('click', (ev) => ev.stopPropagation());
      return select;
    }

    function _aggregateStatus(entries) {
      // ``file_count`` is the disk truth (matches the per-row badge);
      // ``source_file_count`` is the indexed subset. Showing both in
      // the group label (``{indexed}/{files}``) lets users tell at a
      // glance how much of a memory_dir cluster has been indexed vs
      // still on disk waiting — without it, "27 files" was ambiguous
      // (was that "27 on disk" or "27 indexed"?) and disagreed with
      // the row sum when most dirs were unindexed.
      let chunks = 0;
      let files = 0;
      let indexed = 0;
      let any = false;
      for (const path of entries) {
        const st = statusByPath[path];
        if (st) {
          any = true;
          chunks += st.chunk_count || 0;
          files += (typeof st.file_count === 'number') ? st.file_count : 0;
          indexed += st.source_file_count || 0;
        }
      }
      return { chunks, files, indexed, any };
    }

    function _buildStatusBadge(aggregate) {
      const badge = document.createElement('span');
      badge.className = 'memory-dirs-status-group';
      if (aggregate.chunks === 0) badge.classList.add('empty');
      badge.textContent = t(
        'sources.memory_dirs.status_group',
        {
          files: aggregate.files,
          indexed: aggregate.indexed,
          chunks: aggregate.chunks,
        },
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
      // Existing user choice wins over the static default-collapse set —
      // otherwise picking a sort option closes the group the user just
      // opened.
      if (provider in openByProvider) {
        group.open = openByProvider[provider];
      } else if (!_MEMORY_DIR_PROVIDER_COLLAPSED.has(provider)) {
        group.open = true;
      }

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
      if (isSingleLeaf) {
        const onlyEntries = bucket.byCategory[categories[0]];
        const productKey = `${provider}:${categories[0]}`;
        const sortKey = _readSortPref(productKey);
        if (onlyEntries.length >= _MEMORY_DIRS_SORT_THRESHOLD) {
          summary.appendChild(_buildSortDropdown(productKey, sortKey));
        }
        summary.appendChild(_buildGroupReindexButton(categories[0]));
        group.appendChild(summary);
        const sortedEntries = _sortEntries(onlyEntries, sortKey);
        group.appendChild(_buildList(categories[0], sortedEntries));
      } else {
        group.appendChild(summary);
        const products = document.createElement('div');
        products.className = 'memory-dirs-products';
        for (const cat of categories) {
          const entries = bucket.byCategory[cat];
          const productKey = `${provider}:${cat}`;
          const sortKey = _readSortPref(productKey);
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

          if (entries.length >= _MEMORY_DIRS_SORT_THRESHOLD) {
            header.appendChild(_buildSortDropdown(productKey, sortKey));
          }
          header.appendChild(_buildGroupReindexButton(cat));
          section.appendChild(header);
          const sortedEntries = _sortEntries(entries, sortKey);
          section.appendChild(_buildList(cat, sortedEntries));
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

// Public entry — historical wrapper kept so external callers (older
// bookmarks, tests) that referenced ``renderMemoryDirsPanel`` don't
// break. With the Sources view unification (Memory & General share one
// ``.sources-layout`` driven by ``app.js: _renderMemorySourceTree``),
// the legacy panel's container ``#memory-dirs-panel`` is no longer in
// the DOM, so this function no-ops on a fresh page load. Triggers a
// memory reload instead so callers get the new view refreshed.
function renderMemoryDirsPanel() {
  const container = qs('memory-dirs-panel');
  if (container) {
    const dirs = STATE.serverConfig?.indexing?.memory_dirs || [];
    container.innerHTML = '';
    container.appendChild(_buildMemoryDirsPanel(dirs));
    return;
  }
  if (typeof loadSources === 'function') loadSources();
}

// ── Module-level memory-dir actions ───────────────────────────────
// Wired from the per-row action buttons rendered by
// ``app.js: _renderMemoryDirGroup`` (and the +Add path / Reindex all
// header buttons in the unified sources sidebar). They reload the
// memory view on success so the panel reflects the new state without
// a tab toggle. Closure-local ``handleAdd`` / ``handleRemove`` /
// ``handleReindex*`` above remain wired to the legacy
// ``_buildMemoryDirsPanel`` callsite (kept for backward compatibility);
// these wrappers exist because the new render path lives outside that
// closure.
function _mdApiErrorText(err) {
  return (err && err.message) ? err.message : String(err);
}

async function mdAdd(path) {
  const trimmed = (path || '').trim();
  if (!trimmed) return;
  try {
    // ``auto_index=true`` — see ``handleAdd`` above for rationale.
    const resp = await api('POST', '/api/memory-dirs/add', {
      path: trimmed,
      auto_index: true,
    });
    if (resp && Array.isArray(resp.memory_dirs)) {
      if (STATE.serverConfig?.indexing) {
        STATE.serverConfig.indexing.memory_dirs = [...resp.memory_dirs];
      }
      STATE.memoryDirs = [...resp.memory_dirs];
    }
    const stats = resp && resp.indexed;
    if (stats && typeof stats.indexed_chunks === 'number') {
      showToast(
        t('toast.memory_dir.added_indexed', {
          path: trimmed,
          chunks: stats.indexed_chunks,
          files: stats.total_files,
        }),
        'success',
      );
    } else {
      showToast(t('toast.memory_dir.added', { path: trimmed }), 'success');
    }
    if (typeof loadSources === 'function') loadSources();
  } catch (err) {
    showToast(t('toast.memory_dir.add_failed', { error: _mdApiErrorText(err) }), 'error');
  }
}

async function mdRemove(path) {
  const st = (STATE.memoryStatusByPath || {})[path];
  const chunkCount = (st && st.chunk_count) || 0;
  const extraOption = chunkCount > 0
    ? {
        id: 'deleteChunks',
        label: t('confirm.memory_dir_delete_chunks_label', { count: chunkCount }),
        defaultChecked: false,
      }
    : null;
  const result = await showConfirm({
    title: t('confirm.memory_dir_remove_title'),
    message: t('confirm.memory_dir_remove_msg', { path }),
    extraOption,
  });
  const ok = extraOption ? result && result.ok : result;
  if (!ok) return;
  const deleteChunks = !!(extraOption && result && result.extras && result.extras.deleteChunks);
  try {
    const resp = await api('POST', '/api/memory-dirs/remove', {
      path, delete_chunks: deleteChunks,
    });
    if (resp && Array.isArray(resp.memory_dirs)) {
      if (STATE.serverConfig?.indexing) {
        STATE.serverConfig.indexing.memory_dirs = [...resp.memory_dirs];
      }
      STATE.memoryDirs = [...resp.memory_dirs];
    }
    const deleted = (resp && resp.deleted_chunks) || 0;
    if (deleteChunks && deleted > 0) {
      showToast(t('toast.memory_dir.removed_with_chunks', { path, count: deleted }), 'success');
    } else {
      showToast(t('toast.memory_dir.removed', { path }), 'success');
    }
    if (typeof hideBrowser === 'function') hideBrowser();
    if (typeof loadSources === 'function') loadSources();
    if (typeof loadStats === 'function') loadStats();
  } catch (err) {
    showToast(t('toast.memory_dir.remove_failed', { error: _mdApiErrorText(err) }), 'error');
  }
}

async function mdOpenOne(path, btn) {
  if (btn) btnLoading(btn, true);
  try {
    await api('POST', '/api/memory-dirs/open', { path });
    showToast(t('toast.memory_dir.opened', { path }), 'success');
  } catch (err) {
    showToast(t('toast.memory_dir.open_failed', { error: _mdApiErrorText(err) }), 'error');
  } finally {
    if (btn) btnLoading(btn, false);
  }
}

async function mdReindexOne(path, btn) {
  if (typeof _indexingTryStart === 'function' && !_indexingTryStart()) return;
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
    showToast(t('toast.memory_dir.reindex_failed', { error: _mdApiErrorText(err) }), 'error');
  } finally {
    if (btn) btnLoading(btn, false);
    if (typeof _indexingEnd === 'function') _indexingEnd();
    if (typeof loadSources === 'function') loadSources();
  }
}

async function mdReindexAll(btn) {
  if (typeof _indexingTryStart === 'function' && !_indexingTryStart()) return;
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
    showToast(t('toast.reindex_failed', { error: _mdApiErrorText(err) }), 'error');
  } finally {
    if (btn) btnLoading(btn, false);
    if (typeof _indexingEnd === 'function') _indexingEnd();
    if (typeof loadSources === 'function') loadSources();
  }
}

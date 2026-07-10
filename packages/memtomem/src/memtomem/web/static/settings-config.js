/**
 * Config tab — server config display, editable fields, config guide, save.
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

// ---------------------------------------------------------------------------
// Config tab
// ---------------------------------------------------------------------------

// Known config fields per section. Each field's row label renders through
// ``t('settings.config.label.<section>.<field>')`` (localized); a server field
// not listed here falls back to its raw snake_case key name. Keeping this as a
// field LIST (not an English {field: label} map) guarantees the Config panel
// carries no hard-coded English labels — enforced by
// ``test_no_hardcoded_config_labels`` in tests/test_i18n.py.
const _CONFIG_LABEL_FIELDS = {
  embedding: ['provider', 'model', 'dimension', 'base_url', 'batch_size', 'api_key', 'threads'],
  storage:   ['backend', 'sqlite_path', 'collection_name'],
  search:    ['default_top_k', 'bm25_candidates', 'dense_candidates', 'rrf_k',
              'enable_bm25', 'enable_dense', 'tokenizer', 'rrf_weights'],
  decay:     ['enabled', 'half_life_days'],
  mmr:       ['enabled', 'lambda_param'],
  rerank:    ['enabled', 'provider', 'model', 'api_key', 'oversample', 'min_pool', 'max_pool'],
  indexing:  ['supported_extensions', 'exclude_patterns', 'max_chunk_tokens', 'min_chunk_tokens',
              'target_chunk_tokens', 'chunk_overlap_tokens', 'structured_chunk_mode'],
  namespace: ['default_namespace', 'enable_auto_ns'],
};

// Sections that are fully read-only (require restart)
const _READONLY_SECTIONS = new Set(['embedding', 'storage']);

// Individual read-only fields within editable sections
const _READONLY_FIELDS = {
  indexing: new Set([]),
};

// Fields that use a custom widget which persists each change immediately
// (not through the section-level Save button). The reset-to-default ↺ button
// is a no-op for these, so suppress it to avoid a confusing disabled icon.
const _NO_RESET_FIELDS = {};

// Fields that the server config includes but the Config tab skips rendering
// for — either because they are managed elsewhere in the UI (like the
// Sources tab taking over ``memory_dirs``) or have no meaningful scalar form.
const _HIDDEN_CONFIG_FIELDS = {
  indexing: new Set(['memory_dirs']),
};

// STATE.serverConfig now in STATE

// Response fields that live alongside config sections but describe the
// hot-reload state rather than user-editable config. Kept out of the
// section iteration below so they don't render as empty cards.
const _CONFIG_META_FIELDS = new Set(['config_mtime_ns', 'config_reload_error']);
const _CONFIG_SECTION_STORAGE_KEY = 'm2m-config-section';
const _CONFIG_ALL_SECTION = '__all__';
let _activeConfigSection = null;
const _dirtyConfigSections = new Set();

function _configSectionName(section) {
  const key = `settings.config.section.${section}`;
  const localized = t(key);
  return localized === key
    ? section.replaceAll('_', ' ').replace(/\b\w/g, (c) => c.toUpperCase())
    : localized;
}

function _readStoredConfigSection() {
  try { return localStorage.getItem(_CONFIG_SECTION_STORAGE_KEY); }
  catch { return null; }
}

function _storeConfigSection(section) {
  try { localStorage.setItem(_CONFIG_SECTION_STORAGE_KEY, section); }
  catch { /* localStorage is optional */ }
}

function _renderConfigSectionSwitcher(sections) {
  const browser = qs('config-browser');
  const switcher = qs('config-section-switcher');
  if (!browser || !switcher || !sections.length) return;
  const valid = new Set([_CONFIG_ALL_SECTION, ...sections]);
  const stored = _readStoredConfigSection();
  if (!_activeConfigSection || !valid.has(_activeConfigSection)) {
    _activeConfigSection = valid.has(stored) ? stored : sections[0];
  }
  switcher.replaceChildren();
  [_CONFIG_ALL_SECTION, ...sections].forEach((section) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'config-section-chip';
    button.dataset.section = section;
    button.setAttribute('role', 'tab');
    const label = section === _CONFIG_ALL_SECTION ? t('settings.config.section_all') : _configSectionName(section);
    button.append(document.createTextNode(label));
    if (_dirtyConfigSections.has(section)) {
      const dirty = document.createElement('span');
      dirty.className = 'config-section-dirty';
      dirty.setAttribute('aria-label', t('settings.config.unsaved'));
      dirty.textContent = '•';
      button.appendChild(dirty);
    }
    button.addEventListener('click', () => {
      _activeConfigSection = section;
      _storeConfigSection(section);
      _applyConfigFilter();
    });
    switcher.appendChild(button);
  });
  show(browser);
}

function _applyConfigFilter() {
  const query = (qs('config-search')?.value || '').trim().toLocaleLowerCase();
  const searching = query.length > 0;
  document.querySelectorAll('.config-card[data-section]').forEach((card) => {
    const selected = _activeConfigSection === _CONFIG_ALL_SECTION || card.dataset.section === _activeConfigSection;
    let rowMatch = false;
    card.querySelectorAll('.config-table tr').forEach((row) => {
      const matches = !searching || (row.dataset.searchText || '').includes(query);
      row.hidden = !matches;
      if (matches) rowMatch = true;
    });
    card.hidden = searching ? !rowMatch : !selected;
  });
  document.querySelectorAll('.config-section-chip').forEach((button) => {
    const active = button.dataset.section === _activeConfigSection;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  const firstVisible = document.querySelector('.config-card[data-section]:not([hidden])');
  if (firstVisible) _showConfigGuide(firstVisible.dataset.section);
}

qs('config-search')?.addEventListener('input', _applyConfigFilter);

// Last-seen ``config_mtime_ns`` — used to detect when disk changed between
// visibility changes (e.g., user ran ``mm config set`` in a terminal while
// the browser tab was hidden) and render the "Config file changed
// externally" banner.
let _lastConfigMtimeNs = null;

function _renderReloadBanner(data) {
  const el = qs('config-reload-banner');
  if (!el) return;
  const err = data.config_reload_error;
  if (err) {
    el.textContent = t('settings.config.reload_invalid', { error: err });
    el.className = 'config-reload-banner err';
    show(el);
    return;
  }
  const mtime = data.config_mtime_ns;
  if (_lastConfigMtimeNs !== null && mtime !== _lastConfigMtimeNs && mtime > 0) {
    el.textContent = t('settings.config.reload_external');
    el.className = 'config-reload-banner info';
    show(el);
    setTimeout(() => hide(el), 5000);
  } else {
    hide(el);
  }
  if (typeof mtime === 'number') _lastConfigMtimeNs = mtime;
}

async function fetchServerConfig() {
  try {
    STATE.serverConfig = await api('GET', '/api/config');
    _syncConfigToUI();
  } catch (e) {
    console.warn('Config fetch failed, using defaults:', e);
  }
}

function _syncSearchDefaults() {
  if (!STATE.serverConfig?.search) return;
  const topK = STATE.serverConfig.search.default_top_k;
  if (topK) {
    const sel = qs('top-k');
    if (![...sel.options].some(o => o.value == topK)) {
      const opt = document.createElement('option');
      opt.value = topK;
      opt.textContent = `Top ${topK}`;
      sel.appendChild(opt);
    }
    sel.value = String(topK);
    STATE.currentTopK = topK;
  }
}

// Pipeline badges merged into _syncSearchConfig — no separate function needed

// ── A2: Context-Window "Off" label ──
function _updateContextWindowLabel() {
  const sel = qs('context-window');
  const offOpt = sel?.querySelector('option[value="0"]');
  if (!offOpt) return;
  const ctx = STATE.serverConfig?.context;
  if (ctx?.enabled && (ctx.window_before > 0 || ctx.window_after > 0)) {
    offOpt.textContent = `Config (${ctx.window_before}\u2191${ctx.window_after}\u2193)`;
  } else {
    offOpt.textContent = 'Off';
  }
}

// JS-owned (no data-i18n-placeholder) so the lang toggle can't write the
// raw `{date}` token back into the input — i18n.js applyDOM would otherwise
// reset it on every switch.
function _refreshAddFilePlaceholder() {
  const el = qs('add-file');
  if (!el) return;
  const today = new Date().toISOString().slice(0, 10);
  el.placeholder = t('index.add_file_placeholder_prefix') + today + t('index.add_file_placeholder_suffix');
}
window.addEventListener('langchange', () => {
  _refreshAddFilePlaceholder();
  _syncHeaderConfig();
  // ``index-namespace`` / ``add-namespace`` placeholders are JS-owned
  // (no ``data-i18n-placeholder``) so the dynamic ``(auto-determined
  // from path)`` / ``(from config)`` suffix translates on toggle. Without
  // this re-sync the placeholder stays in the previous locale's wording.
  _syncIndexHints();
  // The search-config status line is rendered imperatively via ``t()`` (it
  // interpolates a {count}), so it must re-render on langchange too — both to
  // pick up the active locale after init and to follow the language toggle.
  // Skipped harmlessly when serverConfig hasn't loaded yet.
  _syncSearchConfig();
  // The Home embedding/storage line is likewise t()-rendered (S2.3); relocalize
  // it on toggle. Harmless no-op until serverConfig has loaded.
  _syncHomeConfig();
  // The Config-tab decay / namespace status lines are t()-rendered (S2.4) and
  // draft-free, so they follow the toggle in place like the siblings above.
  // (The editable config CARDS are NOT re-synced here on purpose: a full
  // _syncConfigToUI() re-render would discard an in-progress field edit — they
  // relocalize on the next tab render instead. Documented deferral, #1436.)
  _syncDecayStatus();
  _syncNamespaceInfo();
  const configSections = Array.from(document.querySelectorAll('.config-card[data-section]'), (card) => card.dataset.section);
  if (configSections.length) {
    _renderConfigSectionSwitcher(configSections);
    _applyConfigFilter();
  }
});

// Compute the config-derived placeholder (no path entered yet). Pulled
// out of ``_syncIndexHints`` so the preview-invalidation handler can
// reset to it after the user clears the path or namespace input.
function _configDerivedNsPlaceholder() {
  const nsCfg = STATE.serverConfig?.namespace;
  if (!nsCfg?.default_namespace) return null;
  const suffix = nsCfg.enable_auto_ns
    ? t('index.ns_suffix.auto')
    : t('index.ns_suffix.config');
  return `${nsCfg.default_namespace} (${suffix})`;
}

// ── B1-B3: Index tab hints ──
function _syncIndexHints() {
  // Sync namespace placeholder from config. The suffix moved to i18n
  // (``index.ns_suffix.{auto,config}``) — the previous hard-coded
  // "auto-ns active" sounded like the default itself; "auto-determined
  // from path" describes what auto-NS will actually do.
  const placeholder = _configDerivedNsPlaceholder();
  if (placeholder) {
    const indexNs = qs('index-namespace');
    if (indexNs) indexNs.placeholder = placeholder;
    const addNs = qs('add-namespace');
    if (addNs) addNs.placeholder = placeholder;
  }
  _refreshAddFilePlaceholder();

  if (!STATE.serverConfig?.indexing) return;
  const idx = STATE.serverConfig.indexing;
  // B1: placeholder
  const pathInput = qs('index-path');
  if (pathInput && idx.memory_dirs) {
    const dirs = Array.isArray(idx.memory_dirs) ? idx.memory_dirs : [idx.memory_dirs];
    if (dirs[0]) pathInput.placeholder = dirs[0];
  }
  // B2+B3: extensions + chunk size hint
  const hintEl = qs('index-config-hint');
  if (hintEl) {
    const parts = [];
    if (idx.supported_extensions) {
      const exts = Array.isArray(idx.supported_extensions) ? idx.supported_extensions : [idx.supported_extensions];
      parts.push(t('settings.config.hint_extensions', { exts: exts.join(', ') }));
    }
    if (idx.max_chunk_tokens) {
      parts.push(t('settings.config.hint_max_chunk', { tokens: idx.max_chunk_tokens }));
    }
    if (parts.length) {
      hintEl.textContent = parts.join(' \u00B7 ');
      show(hintEl);
    }
  }
}

// ── C1: Decay tab config status ──
function _syncDecayStatus() {
  const el = qs('decay-config-status');
  if (!el) return;
  const decay = STATE.serverConfig?.decay;
  if (!decay) { hide(el); return; }
  if (decay.enabled) {
    el.textContent = t('settings.config.decay_status_active', { days: decay.half_life_days });
    el.className = 'config-status config-status-on';
  } else {
    el.textContent = t('settings.config.decay_status_inactive');
    el.className = 'config-status config-status-off';
  }
  show(el);
}

// ── D1: Namespace tab config info ──
function _syncNamespaceInfo() {
  const el = qs('ns-config-info');
  if (!el) return;
  const ns = STATE.serverConfig?.namespace;
  if (!ns) { hide(el); return; }
  const parts = [];
  if (ns.default_namespace) parts.push(t('settings.config.ns_default', { ns: ns.default_namespace }));
  if (ns.enable_auto_ns) parts.push(t('settings.config.ns_auto_active'));
  if (parts.length) {
    el.textContent = parts.join(' \u00B7 ');
    show(el);
  }
}

// ── Home: system info banner ──
function _syncHomeConfig() {
  const el = qs('home-config-info');
  if (!el) return;
  const parts = [];
  const emb = STATE.serverConfig?.embedding;
  if (emb) {
    const model = emb.model || 'unknown';
    const provider = emb.provider || 'unknown';
    const dim = emb.dimension || '?';
    parts.push(`${t('home.config.embedding_label')}: ${provider}/${model} (${dim}d)`);
  }
  const stor = STATE.serverConfig?.storage;
  if (stor?.backend) parts.push(`${t('home.config.storage_label')}: ${stor.backend}`);
  if (parts.length) {
    el.textContent = parts.join(' \u00B7 ');
    show(el);
  } else { hide(el); }
}

// ── Search: config defaults banner (merged with pipeline badges) ──
function _syncSearchConfig() {
  const el = qs('search-config-info');
  if (!el) return;
  const s = STATE.serverConfig?.search;
  if (!s) { hide(el); return; }

  // Always-on text: plain-language summary only. Retrieval internals (the
  // BM25/Dense/RRF/rerank acronyms) are jargon for first-time users, so the
  // default config stays silent here — non-default tweaks still surface as the
  // clickable badges below, and the full knobs live in Settings → Config.
  const textParts = [];
  if (s.default_top_k) textParts.push(t('search.status_results', { count: s.default_top_k }));
  if (s.enable_bm25 !== false && s.enable_dense !== false) {
    textParts.push(t('search.status_hybrid'));
  }
  const rerank = STATE.serverConfig?.rerank;
  if (rerank?.enabled) textParts.push(t('search.status_rerank_on'));

  // Non-default settings (shown as clickable badges)
  const badges = [];
  if (s.enable_bm25 === false) badges.push({ label: 'BM25 Off', section: 'search' });
  if (s.enable_dense === false) badges.push({ label: 'Dense Off', section: 'search' });
  if (s.tokenizer && s.tokenizer !== 'unicode61') badges.push({ label: `Tok: ${s.tokenizer}`, section: 'search' });
  const w = s.rrf_weights;
  if (w && (w[0] !== 1.0 || w[1] !== 1.0)) badges.push({ label: `RRF ${w[0]}:${w[1]}`, section: 'search' });
  const dc = STATE.serverConfig?.decay;
  if (dc?.enabled) badges.push({ label: `Decay ${dc.half_life_days}d`, section: 'decay' });
  const mmr = STATE.serverConfig?.mmr;
  if (mmr?.enabled) badges.push({ label: `MMR λ=${mmr.lambda_param}`, section: 'mmr' });
  if (rerank?.enabled) {
    badges.push({
      label: `Pool ${rerank.min_pool}-${rerank.max_pool} ×${rerank.oversample}`,
      section: 'rerank',
    });
  }

  const badgeHtml = badges.map(b =>
    `<span class="pipeline-badge" data-section="${b.section}" title="Click to configure">${b.label}</span>`
  ).join('');

  el.innerHTML = textParts.join(' · ') + (badgeHtml ? ' ' + badgeHtml : '');

  // Wire badge clicks to config tab
  el.querySelectorAll('.pipeline-badge').forEach(badge => {
    badge.addEventListener('click', () => {
      activateTab('settings');
      switchSettingsSection('config');
      setTimeout(() => {
        const card = document.querySelector(`.config-card[data-section="${badge.dataset.section}"]`);
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }, 200);
    });
  });

  show(el);
}

// ── Header system info sync ──
function _syncHeaderConfig() {
  const el = qs('header-sys-info');
  const sep = qs('header-sep');
  if (!el) return;
  const parts = [];
  const emb = STATE.serverConfig?.embedding;
  if (emb) parts.push(`${emb.provider || 'unknown'}/${emb.model || 'unknown'}`);
  const stor = STATE.serverConfig?.storage;
  if (stor?.backend) parts.push(stor.backend);
  if (parts.length) {
    el.textContent = parts.join(' \u00B7 ');
    const tip = [];
    if (emb) tip.push(`Embedding: ${emb.provider}/${emb.model} (${emb.dimension || '?'}d)`);
    if (stor?.backend) tip.push(`Storage: ${stor.backend}`);
    tip.push(t('header.sys_info_jump_title'));
    el.title = tip.join('\n');
    el.setAttribute('aria-label', t('header.sys_info_jump_title'));
    if (sep) { sep.textContent = '|'; show(sep); }
  } else {
    el.textContent = '';
    el.removeAttribute('aria-label');
    if (sep) hide(sep);
  }
}

// ── Unified sync ──
function _syncConfigToUI() {
  if (!STATE.serverConfig) return;
  _syncHeaderConfig();
  _syncSearchDefaults();
  _syncHomeConfig();
  _syncSearchConfig();
  _updateContextWindowLabel();
  _syncIndexHints();
  _syncDecayStatus();
  _syncNamespaceInfo();
}

async function loadConfig() {
  const loadingEl = qs('config-loading');
  const contentEl = qs('config-content');
  loadingEl.innerHTML = `<div class="spinner-panel"></div>${srLoading()}`;
  show(loadingEl); hide(contentEl);

  try {
    // Fetch live config + comparand defaults in parallel. ``/config/defaults``
    // returns the value each field would revert to if the user cleared their
    // ``config.json`` override (defaults + env + fragments), powering the per-
    // field ↺ button below. Missing it is non-fatal — reset buttons simply
    // stay disabled.
    const [live, defaults] = await Promise.all([
      api('GET', '/api/config'),
      api('GET', '/api/config/defaults').catch(() => null),
    ]);
    STATE.serverConfig = live;
    STATE.serverDefaults = defaults;
    contentEl.innerHTML = '';
    _renderReloadBanner(STATE.serverConfig);

    Object.entries(STATE.serverConfig).forEach(([section, values]) => {
      if (_CONFIG_META_FIELDS.has(section)) return;
      const isReadonly = _READONLY_SECTIONS.has(section);
      const card = document.createElement('div');
      card.className = 'config-card card';
      card.dataset.section = section;

      // Header: title + Save button (editable sections) or Read-only badge
      const header = document.createElement('div');
      header.className = 'config-card-header';
      const title = document.createElement('h3');
      title.className = 'config-section-title';
      // Section header: localized name keyed by section id, with a title-cased
      // fallback for any unmapped section the server might add.
      title.textContent = _configSectionName(section);
      header.appendChild(title);
      if (isReadonly) {
        const badge = document.createElement('span');
        badge.className = 'config-readonly-badge';
        badge.textContent = t('settings.config.readonly_badge');
        header.appendChild(badge);
      } else {
        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn btn-sm btn-primary config-save-btn';
        saveBtn.dataset.section = section;
        saveBtn.disabled = true;
        saveBtn.textContent = t('common.save');
        saveBtn.addEventListener('click', () => _saveSection(section));
        header.appendChild(saveBtn);
      }
      card.appendChild(header);

      const table = document.createElement('table');
      table.className = 'config-table';
      const labelFields = new Set(_CONFIG_LABEL_FIELDS[section] || []);
      const readonlyFields = _READONLY_FIELDS[section] || new Set();

      const hiddenFields = _HIDDEN_CONFIG_FIELDS[section] || new Set();
      Object.entries(values).forEach(([key, val]) => {
        if (hiddenFields.has(key)) return;
        const fieldReadonly = isReadonly || readonlyFields.has(key);
        const label = labelFields.has(key) ? t(`settings.config.label.${section}.${key}`) : key;
        const tr = document.createElement('tr');
        tr.dataset.searchText = `${_configSectionName(section)} ${label}`.toLocaleLowerCase();
        tr.innerHTML = `<td class="config-key">${escapeHtml(label)}</td>`;
        const td = document.createElement('td');
        td.className = 'config-val';

        if (fieldReadonly) {
          const display = Array.isArray(val) ? val.join(', ') : String(val);
          td.textContent = display || '—';
          if (display === '***') td.classList.add('config-masked');
          else td.classList.add('config-readonly');
        } else {
          td.appendChild(_buildConfigInput(section, key, val));
        }
        tr.appendChild(td);

        // Reset-to-default button (↺): pre-fills the field with the comparand
        // value so the user sees the new value before pressing Save. Skipped
        // for read-only rows, fields in ``_NO_RESET_FIELDS`` (custom widgets
        // that persist per-action), and when the comparand fetch failed.
        const resetTd = document.createElement('td');
        resetTd.className = 'config-reset';
        const noReset = (_NO_RESET_FIELDS[section] || new Set()).has(key);
        if (!fieldReadonly && !noReset && STATE.serverDefaults) {
          const btn = _buildResetButton(section, key);
          if (btn) resetTd.appendChild(btn);
        }
        tr.appendChild(resetTd);

        table.appendChild(tr);
      });

      card.appendChild(table);
      if (section === 'indexing') {
        const note = document.createElement('div');
        note.className = 'config-breadcrumb';
        const txt = document.createElement('span');
        txt.textContent = t('settings.memory_dirs.moved_notice');
        note.appendChild(txt);
        note.appendChild(document.createTextNode(' '));
        const link = document.createElement('a');
        link.href = '#sources';
        link.className = 'config-breadcrumb-link';
        link.textContent = t('settings.memory_dirs.moved_notice_action');
        link.addEventListener('click', (ev) => {
          ev.preventDefault();
          activateTab('sources');
        });
        note.appendChild(link);
        card.appendChild(note);
      }
      card.addEventListener('mouseenter', () => _showConfigGuide(section));
      card.addEventListener('focusin', () => _showConfigGuide(section));
      contentEl.appendChild(card);
    });

    const sections = Array.from(contentEl.querySelectorAll('.config-card[data-section]'), (card) => card.dataset.section);
    _renderConfigSectionSwitcher(sections);
    _applyConfigFilter();

    // Show first section guide by default (skip meta fields).
    const firstSection = Object.keys(STATE.serverConfig).find(
      (k) => !_CONFIG_META_FIELDS.has(k),
    );
    if (firstSection) _showConfigGuide(firstSection);

    hide(loadingEl);
    show(contentEl);
  } catch (err) {
    loadingEl.innerHTML = emptyState('⚙', t('settings.config.load_failed', { error: err.message }));
  }
}

// Per-section Config guide content. ``title``/``desc`` and each item's
// label+text render through ``t('settings.config.guide.<section>.*')`` and are
// fully localized (the ``items`` array holds id slugs, not English prose). The
// per-step ``howto.steps`` and the ``envs`` snippets are DELIBERATELY kept as
// English literals — they are deep procedural reference and ``MEMTOMEM_*`` /
// command snippets where a translation would drift from the exact tab names,
// commands, and identifiers a user must type. A localized one-line
// ``howto.summary`` (keyed) renders above the English steps so a Korean reader
// gets the gist; ``howto.warn`` (when ``warn: true``) and ``howto.title`` ARE
// localized. This English-island choice is asserted in tests/test_i18n.py
// (``test_no_hardcoded_config_labels`` only scans the localized fields).
const _CONFIG_GUIDES = {
  embedding: {
    items: ['provider', 'model', 'dimension', 'base_url', 'batch_size', 'api_key', 'threads'],
    envs: [
      'MEMTOMEM_EMBEDDING__PROVIDER=ollama',
      'MEMTOMEM_EMBEDDING__MODEL=bge-m3',
      'MEMTOMEM_EMBEDDING__DIMENSION=1024',
      'MEMTOMEM_EMBEDDING__BASE_URL=http://localhost:11434',
      'MEMTOMEM_EMBEDDING__API_KEY=sk-...',
      'MEMTOMEM_EMBEDDING__BATCH_SIZE=64',
      'MEMTOMEM_EMBEDDING__THREADS=4',
    ],
    howto: {
      restart: true,
      warn: true,
      steps: [
        'Set env vars (PROVIDER, MODEL, DIMENSION, BASE_URL)',
        'Restart the server — config auto-syncs to new model',
        'Use Settings > Embedding Status to check for mismatch',
        'Reset embedding metadata, then re-index all (Index tab > Force)',
      ],
    },
  },
  storage: {
    items: ['backend', 'sqlite_path', 'collection'],
    envs: [
      'MEMTOMEM_STORAGE__SQLITE_PATH=~/.memtomem/memtomem.db',
      'MEMTOMEM_STORAGE__COLLECTION_NAME=memories',
    ],
    howto: {
      restart: true,
      steps: [
        'Set MEMTOMEM_STORAGE__SQLITE_PATH env var',
        'Restart the server',
        'New DB will be created automatically. Re-index to populate.',
      ],
    },
  },
  search: {
    items: ['top_k', 'retrievers', 'candidates', 'rrf_k', 'rrf_weights', 'tokenizer'],
    envs: [
      'MEMTOMEM_SEARCH__DEFAULT_TOP_K=10',
      'MEMTOMEM_SEARCH__ENABLE_BM25=true',
      'MEMTOMEM_SEARCH__ENABLE_DENSE=true',
      'MEMTOMEM_SEARCH__BM25_CANDIDATES=50',
      'MEMTOMEM_SEARCH__DENSE_CANDIDATES=50',
      'MEMTOMEM_SEARCH__RRF_K=60',
      'MEMTOMEM_SEARCH__TOKENIZER=unicode61',
    ],
    howto: {
      restart: false,
      warn: true,
      steps: [
        'Adjust weights: slide toward BM25 for exact matches, Dense for semantic similarity',
        'Increase candidates for better recall at the cost of latency',
        'Click Save — applies immediately to all searches',
        'Settings persist to ~/.memtomem/config.json',
      ],
    },
  },
  indexing: {
    items: ['extensions', 'exclude_patterns', 'max_chunk', 'target_chunk', 'min_chunk',
            'overlap', 'structured_mode'],
    envs: [
      'MEMTOMEM_INDEXING__SUPPORTED_EXTENSIONS=\'[".md",".json",".yaml",".yml",".toml",".py",".js",".ts",".tsx",".jsx"]\'',
      'MEMTOMEM_INDEXING__MAX_CHUNK_TOKENS=512',
      'MEMTOMEM_INDEXING__TARGET_CHUNK_TOKENS=384',
      'MEMTOMEM_INDEXING__MIN_CHUNK_TOKENS=128',
      'MEMTOMEM_INDEXING__CHUNK_OVERLAP_TOKENS=0',
      'MEMTOMEM_INDEXING__STRUCTURED_CHUNK_MODE=original',
    ],
    howto: {
      restart: false,
      warn: true,
      steps: [
        'Extensions / Exclude Patterns: edit inline; each change persists immediately. Changing Extensions also surfaces a re-index hint toast.',
        'Chunk token settings: edit here + Save (immediate, no restart)',
        'After changing chunk settings, re-index to apply to existing data',
      ],
    },
  },
  decay: {
    items: ['enabled', 'half_life'],
    envs: [
      'MEMTOMEM_DECAY__ENABLED=true',
      'MEMTOMEM_DECAY__HALF_LIFE_DAYS=30',
    ],
    howto: {
      restart: false,
      steps: [
        'Check "Enabled" and set Half-life',
        'Click Save — applies immediately to all searches',
        'Use Settings > Decay Scan to find and expire stale chunks',
      ],
    },
  },
  mmr: {
    items: ['enabled', 'lambda'],
    envs: [
      'MEMTOMEM_MMR__ENABLED=true',
      'MEMTOMEM_MMR__LAMBDA_PARAM=0.7',
    ],
    howto: {
      restart: false,
      steps: [
        'Check "Enabled" and adjust Lambda',
        'Click Save — applies immediately to all searches',
        'Lower Lambda if you see too many similar chunks in results',
      ],
    },
  },
  namespace: {
    items: ['default_ns', 'auto_ns'],
    envs: [
      'MEMTOMEM_NAMESPACE__DEFAULT_NAMESPACE=default',
      'MEMTOMEM_NAMESPACE__ENABLE_AUTO_NS=false',
    ],
    howto: {
      restart: false,
      warn: true,
      steps: [
        'Set Default NS (e.g., "work", "personal") for auto-tagging',
        'Or enable Auto NS — parent folder names become namespaces',
        'Click Save — applies to next indexing operation',
        'Manage namespaces in Settings > Namespaces tab',
      ],
    },
  },
};

function _showConfigGuide(section) {
  const guide = qs('config-guide');
  if (!guide) return;
  const info = _CONFIG_GUIDES[section];
  if (!info) {
    // No guide for this section (e.g. rerank): show the localized section
    // name as the heading and a "no guide" note.
    const secKey = `settings.config.section.${section}`;
    const secName = t(secKey);
    guide.querySelector('.config-guide-inner').innerHTML =
      `<h4 class="config-guide-title">${escapeHtml(secName === secKey ? section : secName)}</h4>` +
      `<p class="config-guide-text">${escapeHtml(t('settings.config.no_guide'))}</p>`;
    return;
  }
  const base = `settings.config.guide.${section}`;
  let html = `<h4 class="config-guide-title">${escapeHtml(t(`${base}.title`))}</h4>`;
  html += `<p class="config-guide-text">${escapeHtml(t(`${base}.desc`))}</p>`;

  // Field descriptions (localized label + text per item id slug)
  if (info.items) {
    info.items.forEach(item => {
      const label = t(`${base}.item.${item}.label`);
      const text = t(`${base}.item.${item}.text`);
      html += `<div class="config-guide-section"><h5>${escapeHtml(label)}</h5><p>${escapeHtml(text)}</p></div>`;
    });
  }

  // How-to: localized title + 1-line summary, then the English step list
  // (deep procedural reference kept verbatim — see _CONFIG_GUIDES note).
  if (info.howto) {
    const h = info.howto;
    html += '<div class="config-guide-howto">';
    html += `<h5>${escapeHtml(t(`${base}.howto.title`))}`;
    html += h.restart
      ? ` <span class="config-guide-badge restart">${escapeHtml(t('settings.config.badge_restart'))}</span>`
      : ` <span class="config-guide-badge live">${escapeHtml(t('settings.config.badge_live'))}</span>`;
    html += '</h5>';
    html += `<p class="config-guide-howto-summary">${escapeHtml(t(`${base}.howto.summary`))}</p>`;
    html += '<ol class="config-guide-steps">';
    h.steps.forEach(s => { html += `<li>${escapeHtml(s)}</li>`; });
    html += '</ol>';
    if (h.warn) {
      html += `<p class="config-guide-warn">${escapeHtml(t(`${base}.howto.warn`))}</p>`;
    }
    html += '</div>';
  }

  // Env var examples (English — code/identifiers, intentionally not localized)
  if (info.envs) {
    html += '<div class="config-guide-env">';
    html += `<h5>${escapeHtml(t('settings.config.env_vars_title'))}</h5>`;
    html += '<pre class="config-guide-pre">';
    info.envs.forEach(e => { html += escapeHtml(e) + '\n'; });
    html += '</pre>';
    html += '</div>';
  }

  guide.querySelector('.config-guide-inner').innerHTML = html;
}

// Fields that should render as <select> dropdowns: key → [options, descriptions]
// Fields rendered as <select>. ``descriptions`` map each option value to an
// i18n key resolved through ``t()`` at render time (localized hint text); the
// option values themselves (unicode61, kiwipiepy, …) are identifiers and stay
// verbatim.
const _CONFIG_SELECT_OPTIONS = {
  'search.tokenizer': {
    options: ['unicode61', 'kiwipiepy'],
    descriptions: {
      unicode61: 'settings.config.select.tokenizer.unicode61',
      kiwipiepy: 'settings.config.select.tokenizer.kiwipiepy',
    },
  },
  'indexing.structured_chunk_mode': {
    options: ['original', 'recursive'],
    descriptions: {
      original: 'settings.config.select.structured_mode.original',
      recursive: 'settings.config.select.structured_mode.recursive',
    },
  },
};

// Custom widget builders for specific config keys
const _CONFIG_CUSTOM_WIDGETS = {
  'search.rrf_weights': _buildRRFWeightsWidget,
  'indexing.exclude_patterns': _buildExcludePatternsWidget,
  'indexing.supported_extensions': _buildSupportedExtensionsWidget,
};

// Cached {secret, noise} from GET /api/indexing/builtin-exclude-patterns.
let _BUILTIN_EXCLUDE_PATTERNS = null;
async function _fetchBuiltinExcludePatterns() {
  if (_BUILTIN_EXCLUDE_PATTERNS) return _BUILTIN_EXCLUDE_PATTERNS;
  try {
    _BUILTIN_EXCLUDE_PATTERNS = await api('GET', '/api/indexing/builtin-exclude-patterns');
  } catch (e) {
    console.warn('Failed to load built-in exclude patterns:', e);
    _BUILTIN_EXCLUDE_PATTERNS = { secret: [], noise: [] };
  }
  return _BUILTIN_EXCLUDE_PATTERNS;
}

// Reject patterns that pathspec GitIgnoreSpec.from_lines will fail on.
// The authoritative check runs server-side; this is client-side UX only.
function _validateExcludePatternClient(pattern) {
  const p = pattern.trim();
  if (!p) return t('settings.exclude_patterns.err_empty');
  if (p === '!' || p === '\\') return t('settings.exclude_patterns.err_syntax', { pattern: p });
  return null;
}

function _buildRRFWeightsWidget(section, key, val) {
  const wrap = document.createElement('div');
  wrap.className = 'rrf-weights-widget';
  const bm25W = Array.isArray(val) ? val[0] : 1.0;
  const denseW = Array.isArray(val) ? val[1] : 1.0;
  const total = bm25W + denseW || 2;
  const pct = Math.round((denseW / total) * 100); // 0=BM25 only, 100=Dense only

  const labels = document.createElement('div');
  labels.className = 'rrf-balance-labels';
  labels.innerHTML = '<span>BM25</span><span>Dense</span>';
  wrap.appendChild(labels);

  const slider = document.createElement('input');
  slider.type = 'range'; slider.min = '0'; slider.max = '100'; slider.step = '5';
  slider.value = pct; slider.className = 'rrf-balance-slider';
  wrap.appendChild(slider);

  const display = document.createElement('div');
  display.className = 'rrf-balance-display';
  function updateDisplay(v) {
    const bm25Pct = 100 - v;
    const densePct = v;
    if (v === 50) display.textContent = t('settings.config.rrf_balanced');
    else if (v === 0) display.textContent = t('settings.config.rrf_bm25_only');
    else if (v === 100) display.textContent = t('settings.config.rrf_dense_only');
    else display.textContent = t('settings.config.rrf_mix', { bm25: bm25Pct, dense: densePct });
  }
  updateDisplay(pct);
  wrap.appendChild(display);

  // Hidden input for _saveSection
  const hidden = document.createElement('input');
  hidden.type = 'hidden';
  hidden.dataset.section = section; hidden.dataset.key = key;
  hidden.dataset.valType = 'array';
  const origStr = `${bm25W}, ${denseW}`;
  hidden.dataset.original = origStr;
  hidden.value = origStr;

  slider.addEventListener('input', () => {
    const v = Number(slider.value);
    updateDisplay(v);
    // Convert percentage to weights (scale so total = 2.0)
    const dW = (v / 50).toFixed(1);
    const bW = ((100 - v) / 50).toFixed(1);
    hidden.value = `${bW}, ${dW}`;
    _markConfigDirty(section);
  });
  wrap.appendChild(hidden);

  // Reset-to-default hook for the ↺ button (comparandVal = [bm25W, denseW]).
  // Projects the weights back onto the 0..100 slider, updates the display
  // and the ``_saveSection``-backing hidden input.
  hidden._reset = (comparandVal) => _resetRRFWeights(comparandVal, slider, hidden, updateDisplay);

  return wrap;
}

function _resetRRFWeights(comparandVal, slider, hidden, updateDisplay) {
  const bW = Array.isArray(comparandVal) ? Number(comparandVal[0]) : 1.0;
  const dW = Array.isArray(comparandVal) ? Number(comparandVal[1]) : 1.0;
  const total = bW + dW || 2;
  const pct = Math.round((dW / total) * 100);
  slider.value = String(pct);
  updateDisplay(pct);
  hidden.value = `${bW}, ${dW}`;
}

function _buildExcludePatternsWidget(section, key, val) {
  const wrap = document.createElement('div');
  wrap.className = 'exclude-patterns-widget';

  const userPatterns = Array.isArray(val) ? [...val] : [];

  // Hidden input backing _saveSection. JSON-encoded so comma-containing
  // patterns don't get split by the default array parser.
  const hidden = document.createElement('input');
  hidden.type = 'hidden';
  hidden.dataset.section = section;
  hidden.dataset.key = key;
  hidden.dataset.valType = 'json';
  const origStr = JSON.stringify(userPatterns);
  hidden.dataset.original = origStr;
  hidden.value = origStr;

  const builtinBlock = document.createElement('div');
  builtinBlock.className = 'exclude-builtin-block';
  builtinBlock.innerHTML = `
    <div class="exclude-group-header">
      <span data-i18n="settings.exclude_patterns.builtin_title">Built-in (read-only)</span>
      <span class="exclude-group-hint" data-i18n="settings.exclude_patterns.builtin_hint"></span>
    </div>
    <div class="exclude-builtin-list" aria-busy="true"></div>
  `;
  wrap.appendChild(builtinBlock);

  const userBlock = document.createElement('div');
  userBlock.className = 'exclude-user-block';
  userBlock.innerHTML = `
    <div class="exclude-group-header">
      <span data-i18n="settings.exclude_patterns.user_title">User patterns</span>
    </div>
    <div class="exclude-user-list"></div>
    <button type="button" class="btn-ghost btn-sm exclude-add-btn">
      <span data-i18n="settings.exclude_patterns.add">+ Add pattern</span>
    </button>
  `;
  wrap.appendChild(userBlock);
  wrap.appendChild(hidden);

  const listEl = userBlock.querySelector('.exclude-user-list');

  function _syncHidden() {
    // Serialize current inputs to JSON; _markConfigDirty fires if changed.
    const rows = listEl.querySelectorAll('input.exclude-user-input');
    const patterns = Array.from(rows).map(r => r.value);
    hidden.value = JSON.stringify(patterns);
    _markConfigDirty(section);
  }

  function _validateRow(row) {
    const input = row.querySelector('input.exclude-user-input');
    const errEl = row.querySelector('.exclude-row-err');
    const pattern = input.value.trim();

    let err = _validateExcludePatternClient(input.value);
    if (!err) {
      // Duplicate check against other user rows.
      const others = Array.from(listEl.querySelectorAll('input.exclude-user-input'))
        .filter(r => r !== input)
        .map(r => r.value.trim());
      if (pattern && others.includes(pattern)) {
        err = t('settings.exclude_patterns.err_duplicate', { pattern });
      }
    }
    errEl.textContent = err || '';
    input.classList.toggle('exclude-row-invalid', Boolean(err));
    return !err;
  }

  function _addRow(initial = '') {
    const row = document.createElement('div');
    row.className = 'exclude-user-row';
    row.innerHTML = `
      <input type="text" class="exclude-user-input"
             data-i18n-placeholder="settings.exclude_patterns.placeholder" />
      <button type="button" class="btn-ghost btn-sm exclude-remove-btn"
              data-i18n-aria-label="settings.exclude_patterns.remove"
              title="">−</button>
      <div class="exclude-row-err"></div>
    `;
    listEl.appendChild(row);
    const input = row.querySelector('input.exclude-user-input');
    input.value = initial;
    input.addEventListener('input', () => {
      _validateRow(row);
      _syncHidden();
    });
    row.querySelector('.exclude-remove-btn').addEventListener('click', () => {
      row.remove();
      // Re-validate remaining rows in case removing a dupe cleared errors.
      listEl.querySelectorAll('.exclude-user-row').forEach(r => _validateRow(r));
      _syncHidden();
    });
    if (typeof I18N !== 'undefined') I18N.applyDOM();
  }

  userBlock.querySelector('.exclude-add-btn').addEventListener('click', () => {
    _addRow('');
  });

  userPatterns.forEach(p => _addRow(p));

  // Reset-to-default hook for the ↺ button (comparandVal = string[] of user
  // patterns — typically ``[]`` for a fresh install). Clears all rows and
  // rebuilds from the comparand so validation + sync state stay consistent.
  hidden._reset = (comparandVal) => _resetExcludePatterns(comparandVal, listEl, _addRow, _syncHidden);

  _fetchBuiltinExcludePatterns().then(data => {
    const builtinList = builtinBlock.querySelector('.exclude-builtin-list');
    builtinList.removeAttribute('aria-busy');
    const mkGroup = (labelKey, patterns) => {
      if (!patterns.length) return '';
      const rows = patterns.map(p =>
        `<div class="exclude-builtin-row"><code>${escapeHtml(p)}</code></div>`
      ).join('');
      return `<div class="exclude-builtin-subgroup">
        <div class="exclude-builtin-sublabel" data-i18n="${labelKey}"></div>
        ${rows}
      </div>`;
    };
    builtinList.innerHTML =
      mkGroup('settings.exclude_patterns.group_secret', data.secret) +
      mkGroup('settings.exclude_patterns.group_noise', data.noise);
    if (typeof I18N !== 'undefined') I18N.applyDOM();
  });

  if (typeof I18N !== 'undefined') I18N.applyDOM();

  return wrap;
}

function _resetExcludePatterns(comparandVal, listEl, addRow, syncHidden) {
  while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
  const patterns = Array.isArray(comparandVal) ? comparandVal : [];
  patterns.forEach(p => addRow(p));
  syncHidden();
}

// Cap on the number of extensions a user can configure through the GUI.
// Soft guardrail — backend has no equivalent limit, so CLI/config.json
// edits are unaffected. Sized so legitimate language sets still fit.
const _SUPPORTED_EXTENSIONS_CAP = 20;

// ``raw`` -> ``.lower``; lossy on purpose (forgiving auto-normalize).
// Returns null for empty/whitespace input so the caller can silently skip.
function _normalizeExtension(raw) {
  const s = String(raw == null ? '' : raw).trim().toLowerCase();
  if (!s) return null;
  return s.startsWith('.') ? s : '.' + s;
}

function _buildSupportedExtensionsWidget(section, key, val) {
  const wrap = document.createElement('div');
  wrap.className = 'supported-ext-widget';

  // chips is the canonical state; always kept sorted + deduped so the UI
  // matches the post-reload server view (system.py serializes sorted()).
  let chips = (Array.isArray(val) ? val : [])
    .map(_normalizeExtension)
    .filter(Boolean);
  chips = Array.from(new Set(chips)).sort();

  const hidden = document.createElement('input');
  hidden.type = 'hidden';
  hidden.dataset.section = section;
  hidden.dataset.key = key;
  hidden.dataset.valType = 'json';
  const origStr = JSON.stringify(chips);
  hidden.dataset.original = origStr;
  hidden.value = origStr;

  const listEl = document.createElement('div');
  listEl.className = 'supported-ext-chips';
  wrap.appendChild(listEl);

  const addRow = document.createElement('div');
  addRow.className = 'supported-ext-add-row';
  addRow.innerHTML = `
    <input type="text" class="supported-ext-input"
           data-i18n-placeholder="settings.supported_extensions.placeholder" />
    <button type="button" class="btn-ghost btn-sm supported-ext-add-btn">
      <span data-i18n="settings.supported_extensions.add">+ Add</span>
    </button>
  `;
  wrap.appendChild(addRow);

  const errEl = document.createElement('div');
  errEl.className = 'supported-ext-err';
  errEl.setAttribute('role', 'alert');
  wrap.appendChild(errEl);

  wrap.appendChild(hidden);

  const inputEl = addRow.querySelector('.supported-ext-input');

  function _syncHidden() {
    hidden.value = JSON.stringify(chips);
    _markConfigDirty(section);
  }

  function _showError(msg) {
    errEl.textContent = msg || '';
    errEl.classList.toggle('supported-ext-err-visible', Boolean(msg));
  }

  function _renderChips() {
    listEl.innerHTML = '';
    const minReached = chips.length <= 1;
    chips.forEach(ext => {
      const chip = document.createElement('span');
      chip.className = 'supported-ext-chip';
      const label = document.createElement('span');
      label.className = 'supported-ext-chip-label';
      label.textContent = ext;
      chip.appendChild(label);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'supported-ext-chip-remove';
      removeBtn.textContent = '×';
      removeBtn.setAttribute(
        'aria-label',
        t('settings.supported_extensions.remove') + ': ' + ext,
      );
      if (minReached) {
        removeBtn.disabled = true;
        removeBtn.title = t('settings.supported_extensions.min_required');
      }
      removeBtn.addEventListener('click', () => {
        if (chips.length <= 1) return;
        chips = chips.filter(e => e !== ext);
        _renderChips();
        _syncHidden();
      });
      chip.appendChild(removeBtn);
      listEl.appendChild(chip);
    });
  }

  function _tryAdd() {
    const normalized = _normalizeExtension(inputEl.value);
    if (!normalized) {
      // Empty/whitespace — silent skip. Keep input clean.
      inputEl.value = '';
      inputEl.focus();
      return;
    }
    if (chips.includes(normalized)) {
      // Forgiving dedup: silent ignore, just clear the input.
      inputEl.value = '';
      inputEl.focus();
      return;
    }
    if (chips.length >= _SUPPORTED_EXTENSIONS_CAP) {
      _showError(t('settings.supported_extensions.err_cap'));
      return;
    }
    chips = [...chips, normalized].sort();
    _renderChips();
    _syncHidden();
    _showError('');
    inputEl.value = '';
    inputEl.focus();
  }

  addRow.querySelector('.supported-ext-add-btn').addEventListener('click', _tryAdd);
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      _tryAdd();
    }
  });

  _renderChips();

  // Reset-to-default hook (↺): server-provided defaults from
  // STATE.serverDefaults flow through here. Confirm before wiping user list
  // since defaults may overwrite a curated set; the value still lands in
  // the hidden input only — user must press Save to persist.
  hidden._reset = (comparandVal) => {
    if (!confirm(t('settings.supported_extensions.reset_confirm'))) return;
    const next = (Array.isArray(comparandVal) ? comparandVal : [])
      .map(_normalizeExtension)
      .filter(Boolean);
    chips = Array.from(new Set(next)).sort();
    _renderChips();
    _syncHidden();
    _showError('');
  };

  if (typeof I18N !== 'undefined') I18N.applyDOM();
  return wrap;
}

function _buildConfigInput(section, key, val) {
  const id = `cfg-${section}-${key}`;
  const fullKey = `${section}.${key}`;

  // Custom widgets (e.g., RRF weights slider)
  if (_CONFIG_CUSTOM_WIDGETS[fullKey]) {
    return _CONFIG_CUSTOM_WIDGETS[fullKey](section, key, val);
  }

  if (typeof val === 'boolean') {
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.id = id;
    cb.checked = val;
    cb.dataset.section = section; cb.dataset.key = key;
    cb.dataset.original = String(val);
    cb.addEventListener('change', () => _markConfigDirty(section));
    return cb;
  }

  // Select dropdown with descriptions
  if (_CONFIG_SELECT_OPTIONS[fullKey]) {
    const cfg = _CONFIG_SELECT_OPTIONS[fullKey];
    const wrap = document.createElement('div');
    const sel = document.createElement('select');
    sel.id = id;
    sel.dataset.section = section; sel.dataset.key = key;
    sel.dataset.original = String(val);
    cfg.options.forEach(opt => {
      const o = document.createElement('option');
      o.value = opt; o.textContent = opt;
      if (opt === val) o.selected = true;
      sel.appendChild(o);
    });
    wrap.appendChild(sel);

    if (cfg.descriptions) {
      const hint = document.createElement('div');
      hint.className = 'config-select-hint';
      const descFor = (v) => { const k = cfg.descriptions[v]; return k ? t(k) : ''; };
      hint.textContent = descFor(val);
      sel.addEventListener('change', () => {
        hint.textContent = descFor(sel.value);
        _markConfigDirty(section);
      });
      wrap.appendChild(hint);
    } else {
      sel.addEventListener('change', () => _markConfigDirty(section));
    }
    return wrap;
  }

  if (typeof val === 'number') {
    const inp = document.createElement('input');
    inp.type = 'number'; inp.id = id;
    inp.value = val;
    inp.step = Number.isInteger(val) ? '1' : '0.01';
    inp.dataset.section = section; inp.dataset.key = key;
    inp.dataset.original = String(val);
    inp.addEventListener('input', () => _markConfigDirty(section));
    return inp;
  }

  // Array: mark with data-type so _saveSection can parse it back
  if (Array.isArray(val)) {
    const inp = document.createElement('input');
    inp.type = 'text'; inp.id = id;
    inp.value = val.join(', ');
    inp.dataset.section = section; inp.dataset.key = key;
    inp.dataset.original = inp.value;
    inp.dataset.valType = 'array';
    inp.addEventListener('input', () => _markConfigDirty(section));
    return inp;
  }

  const inp = document.createElement('input');
  inp.type = 'text'; inp.id = id;
  inp.value = String(val);
  inp.dataset.section = section; inp.dataset.key = key;
  inp.dataset.original = inp.value;
  inp.addEventListener('input', () => _markConfigDirty(section));
  return inp;
}

function _markConfigDirty(section) {
  _dirtyConfigSections.add(section);
  const sections = Array.from(document.querySelectorAll('.config-card[data-section]'), (card) => card.dataset.section);
  _renderConfigSectionSwitcher(sections);
  _applyConfigFilter();
  const btn = document.querySelector(`.config-save-btn[data-section="${section}"]`);
  if (btn) btn.disabled = false;
  // Keep each row's ↺ button in sync with the live value: disabled when
  // the current value already matches the comparand (nothing to reset).
  _refreshResetButtons(section);
}

// ── Reset-to-default (↺) ──────────────────────────────────────────────────
//
// Each editable row gets a ↺ button that pre-fills the field with the
// comparand value (``GET /api/config/defaults`` — defaults + env +
// ``config.d/`` fragments). The user still has to press Save; after save,
// ``save_config_overrides`` drops the entry because it now equals the
// comparand, so env/fragment values continue to flow through.
//
// Deliberately *not* an auto-PATCH: same-section dirty edits stay safe, and
// the user previews the value before committing.

function _resolveComparand(section, key) {
  const defaults = STATE.serverDefaults;
  if (!defaults) return undefined;
  const sec = defaults[section];
  if (!sec || typeof sec !== 'object') return undefined;
  return sec[key];
}

function _valuesEqual(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }
  return a === b;
}

function _currentInputValue(input) {
  if (!input) return undefined;
  if (input.type === 'checkbox') return input.checked;
  if (input.type === 'number') return parseFloat(input.value);
  if (input.dataset.valType === 'json') {
    try { return JSON.parse(input.value); } catch { return input.value; }
  }
  if (input.dataset.valType === 'array') {
    return input.value.split(',').map(s => {
      const n = parseFloat(s.trim());
      return isNaN(n) ? s.trim() : n;
    });
  }
  return input.value;
}

function _buildResetButton(section, key) {
  const comparand = _resolveComparand(section, key);
  if (comparand === undefined) return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-ghost btn-sm config-reset-btn';
  btn.dataset.section = section;
  btn.dataset.key = key;
  btn.textContent = '↺';
  btn.setAttribute('aria-label', t('settings.reset.aria_label'));
  btn.title = t('settings.reset.title');
  btn.addEventListener('click', () => _resetField(section, key));
  // Initial disabled state: computed after the input is in the DOM.
  queueMicrotask(() => _updateResetButton(btn));
  return btn;
}

function _findFieldInput(section, key) {
  const card = document.querySelector(`.config-card[data-section="${section}"]`);
  if (!card) return null;
  return card.querySelector(
    `input[data-section="${section}"][data-key="${key}"],` +
    `select[data-section="${section}"][data-key="${key}"]`
  );
}

function _updateResetButton(btn) {
  const section = btn.dataset.section;
  const key = btn.dataset.key;
  const comparand = _resolveComparand(section, key);
  if (comparand === undefined) { btn.disabled = true; return; }
  const input = _findFieldInput(section, key);
  if (!input) { btn.disabled = true; return; }
  btn.disabled = _valuesEqual(_currentInputValue(input), comparand);
}

function _refreshResetButtons(section) {
  const card = document.querySelector(`.config-card[data-section="${section}"]`);
  if (!card) return;
  card.querySelectorAll('.config-reset-btn').forEach(_updateResetButton);
}

function _resetField(section, key) {
  const comparand = _resolveComparand(section, key);
  if (comparand === undefined) return;
  const input = _findFieldInput(section, key);
  if (!input) return;

  // Custom widgets opt in by attaching ``_reset`` to their hidden input.
  if (typeof input._reset === 'function') {
    input._reset(comparand);
  } else if (input.type === 'checkbox') {
    input.checked = Boolean(comparand);
  } else if (Array.isArray(comparand) && input.dataset.valType === 'array') {
    input.value = comparand.join(', ');
  } else {
    input.value = String(comparand);
  }
  _markConfigDirty(section);
}

async function _saveSection(section) {
  const card = document.querySelector(`.config-card[data-section="${section}"]`);
  // Exclude-patterns widget owns its own row validation; refuse save if any
  // row still has a visible error so the user sees the problem inline.
  const invalidRows = card.querySelectorAll('.exclude-row-invalid');
  if (invalidRows.length) {
    showToast(t('settings.exclude_patterns.err_save_blocked'), 'error');
    return;
  }
  const inputs = card.querySelectorAll('input[data-section], select[data-section]');
  const patch = {};

  inputs.forEach(inp => {
    const key = inp.dataset.key;
    let val;
    if (inp.type === 'checkbox') val = inp.checked;
    else if (inp.type === 'number') val = parseFloat(inp.value);
    else if (inp.dataset.valType === 'json') {
      try {
        val = JSON.parse(inp.value);
      } catch {
        val = inp.value;
      }
    }
    else if (inp.dataset.valType === 'array') {
      val = inp.value.split(',').map(s => {
        const n = parseFloat(s.trim());
        return isNaN(n) ? s.trim() : n;
      });
    }
    else val = inp.value;

    const orig = inp.dataset.original;
    const current = inp.type === 'checkbox' ? String(inp.checked) : inp.value;
    if (current !== orig) {
      patch[key] = val;
    }
  });

  if (Object.keys(patch).length === 0) return;

  const btn = card.querySelector('.config-save-btn');
  try {
    btnLoading(btn, true);
    const resp = await api('PATCH', '/api/config?persist=true', { [section]: patch });

    if (resp.rejected?.length) {
      showToast(t('toast.fields_rejected', { fields: resp.rejected.join(', ') }), 'error');
    }
    if (resp.applied?.length) {
      showToast(t('toast.settings_updated_count', { count: resp.applied.length }), 'success');
      resp.applied.forEach(c => {
        const [sec, key] = c.field.split('.');
        // Use dataset-based lookup (covers both regular inputs and the
        // hidden inputs of custom widgets, which don't set ``id``). Falling
        // back to ``getElementById`` here would leave custom-widget
        // ``dataset.original`` stale across saves — the next ↺+Save cycle
        // would see current === original and silently skip the patch.
        const inp = _findFieldInput(sec, key);
        if (inp) inp.dataset.original = inp.type === 'checkbox' ? String(inp.checked) : inp.value;
      });
      _dirtyConfigSections.delete(section);
      const sections = Array.from(document.querySelectorAll('.config-card[data-section]'), (card) => card.dataset.section);
      _renderConfigSectionSwitcher(sections);
      _applyConfigFilter();
      // Re-sync all UI from updated config
      STATE.serverConfig = await api('GET', '/api/config');
      _syncConfigToUI();
      // Check if changed fields need reindex/FTS rebuild
      _showReindexWarning(resp.applied);
      // Surface the supported_extensions reindex nudge as a toast — the
      // ``applied`` list already filters to actual changes, so no extra
      // diff-tracking needed.
      if (resp.applied.some(c => c.field === 'indexing.supported_extensions')) {
        showToast(t('settings.supported_extensions.reindex_hint'), 'info');
      }
    }

    if (btn) btn.disabled = true;
  } catch (err) {
    showToast(t('toast.config_save_failed', { error: err.message }), 'error');
  } finally {
    btnLoading(btn, false);
  }
}

// Fields that require reindex or FTS rebuild after change
const _REINDEX_FIELDS = new Set([
  'indexing.max_chunk_tokens', 'indexing.min_chunk_tokens', 'indexing.target_chunk_tokens',
  'indexing.chunk_overlap_tokens', 'indexing.structured_chunk_mode',
]);
const _FTS_REBUILD_FIELDS = new Set([
  'search.tokenizer',
]);

function _showReindexWarning(applied) {
  const needsReindex = applied.some(c => _REINDEX_FIELDS.has(c.field));
  const needsFtsRebuild = applied.some(c => _FTS_REBUILD_FIELDS.has(c.field));
  if (!needsReindex && !needsFtsRebuild) return;

  // Remove existing warning if any
  const existing = document.querySelector('.config-reindex-warn');
  if (existing) existing.remove();

  const warn = document.createElement('div');
  warn.className = 'config-reindex-warn';

  let msg = '';
  if (needsFtsRebuild && needsReindex) {
    msg = t('settings.config.reindex_both');
  } else if (needsFtsRebuild) {
    msg = t('settings.config.reindex_fts');
  } else {
    msg = t('settings.config.reindex_chunk');
  }

  warn.innerHTML = `
    <div class="config-reindex-warn-text">${escapeHtml(msg)}</div>
    <div class="config-reindex-warn-actions">
      ${needsFtsRebuild ? `<button class="btn-primary btn-sm" id="cfg-fts-rebuild-btn">${escapeHtml(t('settings.config.reindex_fts_btn'))}</button>` : ''}
      ${needsReindex ? `<button class="btn-primary btn-sm" id="cfg-reindex-btn">${escapeHtml(t('settings.config.reindex_all_btn'))}</button>` : ''}
      <button class="btn-ghost btn-sm config-reindex-dismiss">${escapeHtml(t('common.dismiss'))}</button>
    </div>
  `;

  // Insert at top of config content
  const content = qs('config-content');
  content.parentElement.insertBefore(warn, content);

  warn.querySelector('.config-reindex-dismiss').addEventListener('click', () => warn.remove());

  if (needsFtsRebuild) {
    warn.querySelector('#cfg-fts-rebuild-btn').addEventListener('click', async (e) => {
      const btn = e.target;
      btnLoading(btn, true);
      try {
        const res = await api('POST', '/api/fts-rebuild', undefined, { timeout: 120_000 });
        showToast(res.message || t('toast.fts_rebuilt', { count: res.rebuilt_rows }), 'success');
        btn.textContent = t('common.done');
        btn.disabled = true;
      } catch (err) {
        showToast(t('toast.fts_rebuild_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    });
  }
  if (needsReindex) {
    warn.querySelector('#cfg-reindex-btn').addEventListener('click', async (e) => {
      const btn = e.target;
      btnLoading(btn, true);
      try {
        const res = await api('POST', '/api/reindex?force=true', undefined, { timeout: 300_000 });
        if (res.errors && res.errors.length) {
          showToast(t('toast.reindex_partial', { count: res.errors.length, first: res.errors[0] }), 'error');
        } else {
          const total = (res.results || []).reduce((s, r) => s + (r.indexed_chunks || 0), 0);
          showToast(t('toast.reindex_complete', { count: total }), 'success');
        }
        btn.textContent = t('common.done');
        btn.disabled = true;
        _markDataStale();
        loadStats();
      } catch (err) {
        showToast(t('toast.reindex_failed', { error: err.message }), 'error');
      } finally {
        btnLoading(btn, false);
      }
    });
  }
}

qs('exp-preview-btn').addEventListener('click', () => runExportPreview());
qs('exp-download-btn').addEventListener('click', () => runExportDownload());
qs('imp-file-trigger')?.addEventListener('click', () => qs('imp-file')?.click());
qs('imp-file').addEventListener('change', () => {
  const files = qs('imp-file').files;
  qs('imp-btn').disabled = !files?.length;
  const nameEl = qs('imp-file-name');
  if (nameEl) nameEl.textContent = files?.length ? files[0].name : 'No file chosen';
});
qs('imp-btn').addEventListener('click', () => runImport());

// ── Index tab: preview-namespace wiring ──
//
// Three event surfaces, only one debounced. Spec:
//   • namespace input ``focus``   → immediate preview (discrete event;
//                                    debouncing a one-shot is meaningless)
//   • path input     ``input``    → 300ms-debounced preview, but only if
//                                    the namespace input is empty
//                                    (otherwise the user has typed an
//                                    explicit override and we'd stomp it)
//   • namespace input ``input``   → invalidate any cached preview state
//                                    immediately so emptying the field
//                                    after a stale preview doesn't echo
//                                    a value that no longer matches the
//                                    current path
async function _fetchPreviewNamespace(pathValue) {
  if (!pathValue) return null;
  try {
    const params = new URLSearchParams({ path: pathValue });
    return await api('GET', `/api/index/preview-namespace?${params.toString()}`);
  } catch (_) {
    // 403 (out of memory_dirs) / network errors / route absent — fall
    // through to the config-derived placeholder. The form submit will
    // surface a real error if the path is genuinely invalid.
    return null;
  }
}

function _setNsPreviewPlaceholder(nsInput, resp) {
  if (!nsInput || !resp) return;
  nsInput.placeholder = renderResolvedNamespaces(resp.resolved_namespaces, {
    truncated: resp.truncated,
    scanned: resp.scanned_files,
    mode: 'preview',
  });
}

async function _runNsPreview(nsInput, pathInput) {
  if (!nsInput || !pathInput) return;
  const pathValue = pathInput.value.trim();
  if (!pathValue) {
    // Nothing to preview against — restore the config-derived hint.
    const fallback = _configDerivedNsPlaceholder();
    if (fallback) nsInput.placeholder = fallback;
    return;
  }
  // Race-guard: ``dataset.previewPath`` is the in-flight request's path.
  // The post-await check below drops responses that arrived after the
  // user typed a different path.
  nsInput.dataset.previewPath = pathValue;
  const resp = await _fetchPreviewNamespace(pathValue);
  if (nsInput.dataset.previewPath !== pathValue) return;
  if (!resp) {
    const fallback = _configDerivedNsPlaceholder();
    if (fallback) nsInput.placeholder = fallback;
    return;
  }
  _setNsPreviewPlaceholder(nsInput, resp);
}

function _wirePreviewNamespace(nsInputId, pathInputId) {
  const nsInput = qs(nsInputId);
  const pathInput = qs(pathInputId);
  if (!nsInput || !pathInput) return;

  // (1) namespace focus → fire preview immediately
  nsInput.addEventListener('focus', () => { _runNsPreview(nsInput, pathInput); });

  // (2) path typing → 300ms-debounced; skip when namespace has an explicit value
  const debouncedPathPreview = debounce(() => {
    if (nsInput.value.trim()) return;
    _runNsPreview(nsInput, pathInput);
  }, 300);
  pathInput.addEventListener('input', debouncedPathPreview);

  // (3) namespace typing → invalidate the in-flight race-guard. When the
  // user clears the field after typing, the next focus should re-preview
  // from the *current* path, not echo a value frozen at a prior call.
  nsInput.addEventListener('input', () => {
    delete nsInput.dataset.previewPath;
    if (!nsInput.value.trim()) {
      const fallback = _configDerivedNsPlaceholder();
      if (fallback) nsInput.placeholder = fallback;
    }
  });
}

_wirePreviewNamespace('index-namespace', 'index-path');
// Compose tab's ``add-namespace`` input is intentionally not wired — its
// counterpart ``add-file`` is the *target save path* of a memory being
// composed, which doesn't exist on disk yet. ``discover_indexable_files``
// only enumerates existing files, so the preview would return ``[]`` and
// render a misleading ``(untagged) (preview)`` for what auto-NS would
// actually produce. Issue #581 scopes the cluster to the Index tab; a
// "phantom-path" preview API for compose is a follow-up.

fetchServerConfig();

// Re-fetch on tab visibility gain so CLI edits made while the tab was
// hidden (e.g., ``mm config set mmr.enabled true`` in a terminal) become
// visible on next focus without a manual reload. Only triggers when the
// Config tab is the active settings section.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  const configSection = qs('settings-config');
  if (!configSection || !configSection.classList.contains('active')) return;
  fetchServerConfig();
});

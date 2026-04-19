/**
 * Config tab — server config display, editable fields, config guide, save.
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

// ---------------------------------------------------------------------------
// Config tab
// ---------------------------------------------------------------------------

const _CONFIG_LABELS = {
  embedding: { provider: 'Provider', model: 'Model', dimension: 'Dimension',
                base_url: 'Base URL', batch_size: 'Batch Size', api_key: 'API Key' },
  storage:   { backend: 'Backend', sqlite_path: 'SQLite Path', collection_name: 'Collection' },
  search:    { default_top_k: 'Default top-k', bm25_candidates: 'BM25 Candidates',
                dense_candidates: 'Dense Candidates', rrf_k: 'RRF k',
                enable_bm25: 'BM25 Enabled', enable_dense: 'Dense Enabled',
                tokenizer: 'Tokenizer', rrf_weights: 'RRF Weights (BM25, Dense)' },
  decay:     { enabled: 'Enabled', half_life_days: 'Half-life (days)' },
  mmr:       { enabled: 'Enabled', lambda_param: 'Lambda' },
  indexing:  { memory_dirs: 'Memory Dirs', supported_extensions: 'Extensions',
                max_chunk_tokens: 'Max Chunk Tokens', min_chunk_tokens: 'Min Chunk Tokens',
                target_chunk_tokens: 'Target Chunk Tokens',
                chunk_overlap_tokens: 'Chunk Overlap', structured_chunk_mode: 'Structured Chunk Mode' },
  namespace: { default_namespace: 'Default NS', enable_auto_ns: 'Auto NS' },
};

// Sections that are fully read-only (require restart)
const _READONLY_SECTIONS = new Set(['embedding', 'storage']);

// Individual read-only fields within editable sections
const _READONLY_FIELDS = {
  indexing: new Set(['supported_extensions']),
};

// Fields that use a custom widget which persists each change immediately
// (not through the section-level Save button). The reset-to-default ↺ button
// is a no-op for these, so suppress it to avoid a confusing disabled icon.
const _NO_RESET_FIELDS = {
  indexing: new Set(['memory_dirs']),
};

// STATE.serverConfig now in STATE

// Response fields that live alongside config sections but describe the
// hot-reload state rather than user-editable config. Kept out of the
// section iteration below so they don't render as empty cards.
const _CONFIG_META_FIELDS = new Set(['config_mtime_ns', 'config_reload_error']);

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
    el.textContent = 'Config file invalid on disk: ' + err +
      ' — fix it (or run `mm init --fresh`) before saving from the UI.';
    el.className = 'config-reload-banner err';
    show(el);
    return;
  }
  const mtime = data.config_mtime_ns;
  if (_lastConfigMtimeNs !== null && mtime !== _lastConfigMtimeNs && mtime > 0) {
    el.textContent = 'Config file changed externally — reloaded from disk.';
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

// ── B1-B3: Index tab hints ──
function _syncIndexHints() {
  // Sync namespace placeholder from config
  const nsCfg = STATE.serverConfig?.namespace;
  if (nsCfg?.default_namespace) {
    const nsPlaceholder = nsCfg.default_namespace + (nsCfg.enable_auto_ns ? ' (auto-ns active)' : ' (from config)');
    const indexNs = qs('index-namespace');
    if (indexNs) indexNs.placeholder = nsPlaceholder;
    const addNs = qs('add-namespace');
    if (addNs) addNs.placeholder = nsPlaceholder;
  }

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
      parts.push(`Extensions: ${exts.join(', ')}`);
    }
    if (idx.max_chunk_tokens) parts.push(`Max chunk: ${idx.max_chunk_tokens} tokens`);
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
    el.textContent = `Score Decay: Active (half-life ${decay.half_life_days}d)`;
    el.className = 'config-status config-status-on';
  } else {
    el.textContent = 'Score Decay: Inactive';
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
  if (ns.default_namespace) parts.push(`Default NS: ${ns.default_namespace}`);
  if (ns.enable_auto_ns) parts.push('Auto-NS: Active');
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
    parts.push(`Embedding: ${provider}/${model} (${dim}d)`);
  }
  const stor = STATE.serverConfig?.storage;
  if (stor?.backend) parts.push(`Storage: ${stor.backend}`);
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

  // Standard info (always shown as text)
  const textParts = [];
  if (s.default_top_k) textParts.push(`Top-K: ${s.default_top_k}`);
  const retrievers = [];
  if (s.enable_bm25 !== false) retrievers.push('BM25');
  if (s.enable_dense !== false) retrievers.push('Dense');
  textParts.push(retrievers.length ? retrievers.join('+') : 'No retriever');
  if (s.rrf_k) textParts.push(`RRF k=${s.rrf_k}`);

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
    el.title = tip.join('\n');
    if (sep) { sep.textContent = '|'; show(sep); }
  } else {
    el.textContent = '';
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
  loadingEl.innerHTML = '<div class="spinner-panel"></div>';
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
      const _SECTION_TITLES = { mmr: 'MMR', namespace: 'Namespace', rrf: 'RRF' };
      title.textContent = _SECTION_TITLES[section] || section.charAt(0).toUpperCase() + section.slice(1);
      header.appendChild(title);
      if (isReadonly) {
        const badge = document.createElement('span');
        badge.className = 'config-readonly-badge';
        badge.textContent = 'Read-only';
        header.appendChild(badge);
      } else {
        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn btn-sm btn-primary config-save-btn';
        saveBtn.dataset.section = section;
        saveBtn.disabled = true;
        saveBtn.textContent = 'Save';
        saveBtn.addEventListener('click', () => _saveSection(section));
        header.appendChild(saveBtn);
      }
      card.appendChild(header);

      const table = document.createElement('table');
      table.className = 'config-table';
      const labels = _CONFIG_LABELS[section] || {};
      const readonlyFields = _READONLY_FIELDS[section] || new Set();

      Object.entries(values).forEach(([key, val]) => {
        const fieldReadonly = isReadonly || readonlyFields.has(key);
        const label = labels[key] || key;
        const tr = document.createElement('tr');
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
      card.addEventListener('mouseenter', () => _showConfigGuide(section));
      card.addEventListener('focusin', () => _showConfigGuide(section));
      contentEl.appendChild(card);
    });

    // Show first section guide by default (skip meta fields).
    const firstSection = Object.keys(STATE.serverConfig).find(
      (k) => !_CONFIG_META_FIELDS.has(k),
    );
    if (firstSection) _showConfigGuide(firstSection);

    hide(loadingEl);
    show(contentEl);
  } catch (err) {
    loadingEl.innerHTML = emptyState('⚙', 'Config load failed: ' + err.message);
  }
}

const _CONFIG_GUIDES = {
  embedding: {
    title: 'Embedding',
    desc: 'Controls how text is converted to vector representations for semantic search.',
    items: [
      { label: 'Provider', text: 'ollama (local, free) or openai (cloud, paid). Determines which API to call for embeddings.' },
      { label: 'Model', text: 'nomic-embed-text (768d, English-optimized), bge-m3 (1024d, multilingual) etc. Must match Provider.' },
      { label: 'Dimension', text: 'Vector dimension. Must match the model output. Changing this requires re-indexing all data.' },
      { label: 'Base URL', text: 'API endpoint. Ollama: http://localhost:11434. OpenAI: https://api.openai.com/v1 (or compatible endpoint).' },
      { label: 'Batch Size', text: 'Number of texts embedded per API call. Higher = faster indexing but more memory. Default 64.' },
      { label: 'API Key', text: 'Required for OpenAI provider. Not needed for local Ollama. Masked in UI for security.' },
    ],
    envs: [
      'MEMTOMEM_EMBEDDING__PROVIDER=ollama',
      'MEMTOMEM_EMBEDDING__MODEL=bge-m3',
      'MEMTOMEM_EMBEDDING__DIMENSION=1024',
      'MEMTOMEM_EMBEDDING__BASE_URL=http://localhost:11434',
      'MEMTOMEM_EMBEDDING__API_KEY=sk-...',
      'MEMTOMEM_EMBEDDING__BATCH_SIZE=64',
    ],
    howto: {
      title: 'Embedding Model Change',
      restart: true,
      steps: [
        'Set env vars (PROVIDER, MODEL, DIMENSION, BASE_URL)',
        'Restart the server — config auto-syncs to new model',
        'Use Settings > Embedding Status to check for mismatch',
        'Reset embedding metadata, then re-index all (Index tab > Force)',
      ],
      warn: 'Changing model/dimension invalidates all existing vectors. Must reset + re-index after restart.',
    },
  },
  storage: {
    title: 'Storage',
    desc: 'Database backend for storing chunks, vectors, and FTS index.',
    items: [
      { label: 'Backend', text: 'Currently only sqlite is supported. Single-file DB, no external dependencies.' },
      { label: 'SQLite Path', text: 'Full path to the database file. Contains all chunks, embeddings, FTS index, and metadata.' },
      { label: 'Collection', text: 'Logical table name for storing chunks. Change to use separate storage within the same DB.' },
    ],
    envs: [
      'MEMTOMEM_STORAGE__SQLITE_PATH=~/.memtomem/memtomem.db',
      'MEMTOMEM_STORAGE__COLLECTION_NAME=memories',
    ],
    howto: {
      title: 'Change DB Path',
      restart: true,
      steps: [
        'Set MEMTOMEM_STORAGE__SQLITE_PATH env var',
        'Restart the server',
        'New DB will be created automatically. Re-index to populate.',
      ],
    },
  },
  search: {
    title: 'Search Pipeline',
    desc: 'Hybrid search: BM25 (keyword) + Dense (semantic) fused via Reciprocal Rank Fusion.',
    items: [
      { label: 'Default top-k', text: 'Number of results returned by default. Can be overridden per query.' },
      { label: 'BM25/Dense Enabled', text: 'Toggle retrievers independently. BM25 = exact keyword matching (FTS5). Dense = semantic vector similarity.' },
      { label: 'Candidates', text: 'BM25/Dense Candidates control how many results each retriever fetches before fusion. Higher = better recall but slower.' },
      { label: 'RRF k', text: 'Smoothing constant for rank fusion. Lower (10-30) = top ranks dominate. Higher (60-100) = more uniform blending.' },
      { label: 'RRF Weights', text: 'Balance slider between BM25 and Dense. Center = equal weight. Left = keyword heavier. Right = semantic heavier.' },
      { label: 'Tokenizer', text: 'FTS5 tokenizer for keyword search. unicode61: built-in (all languages). kiwipiepy: Korean morphological analysis.' },
    ],
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
      title: 'Tune Search Quality',
      restart: false,
      steps: [
        'Adjust weights: slide toward BM25 for exact matches, Dense for semantic similarity',
        'Increase candidates for better recall at the cost of latency',
        'Click Save — applies immediately to all searches',
        'Settings persist to ~/.memtomem/config.json',
      ],
      warn: 'Changing tokenizer to kiwipiepy requires: pip install kiwipiepy. Falls back to unicode61 if unavailable.',
    },
  },
  indexing: {
    title: 'Indexing',
    desc: 'Controls how files are discovered, chunked, and stored as searchable units.',
    items: [
      { label: 'Memory Dirs', text: 'Directories that can be indexed. Add or remove entries inline; grouped by origin (user-chosen, Claude projects, Claude plans, Codex). Each change persists immediately.' },
      { label: 'Extensions', text: 'File types recognized for chunking: .md, .py, .js, .ts, .tsx, .jsx, .json, .yaml, .yml, .toml.' },
      { label: 'Max Chunk Tokens', text: 'Upper bound for chunk size. Long sections are split to stay under this limit.' },
      { label: 'Min Chunk Tokens', text: 'Short chunks below this threshold are merged with their neighbor. 0 = no merging.' },
      { label: 'Chunk Overlap', text: 'Token overlap between adjacent chunks. Adds shared context at boundaries for better retrieval. 0 = no overlap.' },
      { label: 'Structured Chunk Mode', text: 'For JSON/YAML/TOML files. "original": extracts raw text lines per key (preserves formatting, line numbers). "recursive": serializes via json.dumps, recursively splits deep nested structures by sub-keys.' },
    ],
    envs: [
      'MEMTOMEM_INDEXING__MAX_CHUNK_TOKENS=512',
      'MEMTOMEM_INDEXING__MIN_CHUNK_TOKENS=128',
      'MEMTOMEM_INDEXING__CHUNK_OVERLAP_TOKENS=0',
      'MEMTOMEM_INDEXING__STRUCTURED_CHUNK_MODE=original',
    ],
    howto: {
      title: 'Indexing Settings',
      restart: false,
      steps: [
        'Memory Dirs: use the inline [+ Add] / [✕] / [↻] controls; each change hits the server immediately',
        'Chunk token settings: edit here + Save (immediate, no restart)',
        'After changing chunk settings, re-index to apply to existing data',
        'Or set env: MEMTOMEM_INDEXING__MEMORY_DIRS=\'["/path1","/path2"]\'',
      ],
      warn: 'Extensions are read-only in UI. Chunk setting changes require re-index to take effect on existing data.',
    },
  },
  decay: {
    title: 'Time Decay',
    desc: 'Reduces search scores of older chunks over time, prioritizing recent information.',
    items: [
      { label: 'Enabled', text: 'When active, search scores are multiplied by a time-based decay factor. Newer chunks rank higher.' },
      { label: 'Half-life (days)', text: 'Days until decay factor reaches 0.5. A 30-day half-life means month-old chunks score ~50% of fresh ones.' },
    ],
    envs: [
      'MEMTOMEM_DECAY__ENABLED=true',
      'MEMTOMEM_DECAY__HALF_LIFE_DAYS=30',
    ],
    howto: {
      title: 'Enable Decay',
      restart: false,
      steps: [
        'Check "Enabled" and set Half-life',
        'Click Save — applies immediately to all searches',
        'Use Settings > Decay Scan to find and expire stale chunks',
      ],
    },
  },
  mmr: {
    title: 'Maximal Marginal Relevance',
    desc: 'Diversifies search results by penalizing chunks too similar to already-selected ones.',
    items: [
      { label: 'Enabled', text: 'When active, results are re-ranked after retrieval to reduce redundancy.' },
      { label: 'Lambda', text: 'Balance between relevance and diversity. 1.0 = pure relevance (no MMR effect). 0.0 = max diversity. Default 0.7.' },
    ],
    envs: [
      'MEMTOMEM_MMR__ENABLED=true',
      'MEMTOMEM_MMR__LAMBDA_PARAM=0.7',
    ],
    howto: {
      title: 'Enable MMR',
      restart: false,
      steps: [
        'Check "Enabled" and adjust Lambda',
        'Click Save — applies immediately to all searches',
        'Lower Lambda if you see too many similar chunks in results',
      ],
    },
  },
  namespace: {
    title: 'Namespace',
    desc: 'Organize chunks into logical groups for scoped search and management.',
    items: [
      { label: 'Default NS', text: 'Applied when no namespace is specified during indexing or memory add. Set "default" to leave chunks untagged.' },
      { label: 'Auto NS', text: 'When enabled, derives namespace from the parent folder name during indexing. Overrides Default NS.' },
    ],
    envs: [
      'MEMTOMEM_NAMESPACE__DEFAULT_NAMESPACE=default',
      'MEMTOMEM_NAMESPACE__ENABLE_AUTO_NS=false',
    ],
    howto: {
      title: 'Use Namespaces',
      restart: false,
      steps: [
        'Set Default NS (e.g., "work", "personal") for auto-tagging',
        'Or enable Auto NS — parent folder names become namespaces',
        'Click Save — applies to next indexing operation',
        'Manage namespaces in Settings > Namespaces tab',
      ],
      warn: 'Auto NS takes priority over Default NS. Existing chunks keep their namespace until re-indexed.',
    },
  },
};

function _showConfigGuide(section) {
  const guide = qs('config-guide');
  if (!guide) return;
  const info = _CONFIG_GUIDES[section];
  if (!info) {
    guide.querySelector('.config-guide-inner').innerHTML =
      `<h4 class="config-guide-title">${section}</h4><p class="config-guide-text">No guide available.</p>`;
    return;
  }
  let html = `<h4 class="config-guide-title">${escapeHtml(info.title)}</h4>`;
  html += `<p class="config-guide-text">${escapeHtml(info.desc)}</p>`;

  // Field descriptions
  if (info.items) {
    info.items.forEach(it => {
      html += `<div class="config-guide-section"><h5>${escapeHtml(it.label)}</h5><p>${escapeHtml(it.text)}</p></div>`;
    });
  }

  // How-to steps
  if (info.howto) {
    const h = info.howto;
    html += '<div class="config-guide-howto">';
    html += `<h5>${escapeHtml(h.title)}`;
    html += h.restart
      ? ' <span class="config-guide-badge restart">Restart Required</span>'
      : ' <span class="config-guide-badge live">Live Update</span>';
    html += '</h5>';
    html += '<ol class="config-guide-steps">';
    h.steps.forEach(s => { html += `<li>${escapeHtml(s)}</li>`; });
    html += '</ol>';
    if (h.warn) {
      html += `<p class="config-guide-warn">${escapeHtml(h.warn)}</p>`;
    }
    html += '</div>';
  }

  // Env var examples
  if (info.envs) {
    html += '<div class="config-guide-env">';
    html += '<h5>Environment Variables</h5>';
    html += '<pre class="config-guide-pre">';
    info.envs.forEach(e => { html += escapeHtml(e) + '\n'; });
    html += '</pre>';
    html += '</div>';
  }

  guide.querySelector('.config-guide-inner').innerHTML = html;
}

// Fields that should render as <select> dropdowns: key → [options, descriptions]
const _CONFIG_SELECT_OPTIONS = {
  'search.tokenizer': {
    options: ['unicode61', 'kiwipiepy'],
    descriptions: {
      unicode61: 'Built-in FTS5 Unicode tokenizer (all languages)',
      kiwipiepy: 'Korean morphological analyzer (pip install kiwipiepy)',
    },
  },
  'indexing.structured_chunk_mode': {
    options: ['original', 'recursive'],
    descriptions: {
      original: 'Extract original text lines per key, split large sections by line count',
      recursive: 'Serialize via json.dumps, recursively split by sub-keys',
    },
  },
};

// Custom widget builders for specific config keys
const _CONFIG_CUSTOM_WIDGETS = {
  'search.rrf_weights': _buildRRFWeightsWidget,
  'indexing.exclude_patterns': _buildExcludePatternsWidget,
  'indexing.memory_dirs': _buildMemoryDirsWidget,
};

// Categorize an absolute memory_dir path. Mirrors ``_detect_provider_dirs``
// in config.py so the UI groups match the wizard's opt-in categories.
function _categorizeMemoryDir(p) {
  const s = String(p).replace(/\/+$/, '');
  if (/\/\.claude\/projects\/[^/]+\/memory$/.test(s)) return 'claude-memory';
  if (/\/\.claude\/plans$/.test(s)) return 'claude-plans';
  if (/\/\.codex\/memories$/.test(s)) return 'codex';
  return 'user';
}

const _MEMORY_DIR_CATEGORY_ORDER = ['user', 'claude-memory', 'claude-plans', 'codex'];
const _MEMORY_DIR_CATEGORY_LABEL_KEY = {
  'user': 'settings.memory_dirs.category.user',
  'claude-memory': 'settings.memory_dirs.category.claude_memory',
  'claude-plans': 'settings.memory_dirs.category.claude_plans',
  'codex': 'settings.memory_dirs.category.codex',
};
// Categories that start collapsed. User is open by default because it's
// usually short; Claude projects can have 20+ entries from the one-shot
// ``auto_discover`` migration, so it's closed to keep the card short.
const _MEMORY_DIR_CATEGORY_COLLAPSED = new Set(['claude-memory', 'claude-plans', 'codex']);

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
    if (v === 50) display.textContent = 'Balanced (1 : 1)';
    else if (v === 0) display.textContent = 'BM25 only';
    else if (v === 100) display.textContent = 'Dense only';
    else display.textContent = `BM25 ${bm25Pct}% : Dense ${densePct}%`;
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

function _buildMemoryDirsWidget(section, key, val) {
  // memory_dirs uses a different editing model than the rest of the config
  // card: each add/remove/reindex hits the server immediately (POST
  // /api/memory-dirs/add, /remove, /reindex, /index). That's because these
  // actions carry side effects beyond a config value (watcher schedule,
  // DB chunk state) that need to land atomically on the server.
  //
  // Consequence: the widget intentionally returns NO input with
  // ``data-section``/``data-key`` set, so it sits outside the per-section
  // Save dirty/reset flow. ``_NO_RESET_FIELDS`` suppresses the reset button
  // for the same reason.
  const wrap = document.createElement('div');
  wrap.className = 'memory-dirs-widget';

  let dirs = Array.isArray(val) ? [...val] : [];

  function _apiErrorText(err) {
    return (err && err.message) ? err.message : String(err);
  }

  function refreshDirs(newDirs) {
    if (Array.isArray(newDirs)) dirs = [...newDirs];
    if (STATE.serverConfig?.indexing) {
      STATE.serverConfig.indexing.memory_dirs = [...dirs];
    }
    render();
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
    }
  }

  async function handleReindexGroup(category, btn) {
    const targets = dirs.filter(d => _categorizeMemoryDir(d) === category);
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
    }
  }

  function render() {
    wrap.innerHTML = '';

    // Top action bar: Reindex-all button.
    const actions = document.createElement('div');
    actions.className = 'memory-dirs-actions';
    const reindexAllBtn = document.createElement('button');
    reindexAllBtn.type = 'button';
    reindexAllBtn.className = 'btn btn-sm btn-ghost';
    reindexAllBtn.textContent = t('settings.memory_dirs.reindex_all');
    reindexAllBtn.addEventListener('click', () => handleReindexAll(reindexAllBtn));
    actions.appendChild(reindexAllBtn);
    wrap.appendChild(actions);

    // Group by category.
    const byCategory = { 'user': [], 'claude-memory': [], 'claude-plans': [], 'codex': [] };
    for (const d of dirs) {
      const cat = _categorizeMemoryDir(d);
      byCategory[cat].push(d);
    }

    for (const cat of _MEMORY_DIR_CATEGORY_ORDER) {
      const entries = byCategory[cat];
      if (!entries.length) continue;

      const group = document.createElement('details');
      group.className = 'memory-dirs-group';
      group.dataset.category = cat;
      if (!_MEMORY_DIR_CATEGORY_COLLAPSED.has(cat)) group.open = true;

      const summary = document.createElement('summary');
      summary.className = 'memory-dirs-summary';
      const label = document.createElement('span');
      label.className = 'memory-dirs-summary-label';
      label.textContent = `${t(_MEMORY_DIR_CATEGORY_LABEL_KEY[cat])} (${entries.length})`;
      summary.appendChild(label);

      const groupReindex = document.createElement('button');
      groupReindex.type = 'button';
      groupReindex.className = 'btn btn-xs btn-ghost memory-dirs-group-reindex';
      groupReindex.textContent = '↻';
      groupReindex.title = t('settings.memory_dirs.reindex_group');
      groupReindex.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        handleReindexGroup(cat, groupReindex);
      });
      summary.appendChild(groupReindex);
      group.appendChild(summary);

      const list = document.createElement('ul');
      list.className = 'memory-dirs-list';
      if (cat === 'claude-memory') list.classList.add('memory-dirs-list-scroll');

      for (const path of entries) {
        const item = document.createElement('li');
        item.className = 'memory-dirs-item';

        const pathSpan = document.createElement('span');
        pathSpan.className = 'memory-dirs-path';
        pathSpan.textContent = path;
        pathSpan.title = path;
        item.appendChild(pathSpan);

        const reindexBtn = document.createElement('button');
        reindexBtn.type = 'button';
        reindexBtn.className = 'btn btn-xs btn-ghost memory-dirs-reindex-btn';
        reindexBtn.textContent = '↻';
        reindexBtn.title = t('settings.memory_dirs.reindex_title');
        reindexBtn.addEventListener('click', () => handleReindexOne(path, reindexBtn));
        item.appendChild(reindexBtn);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn btn-xs btn-ghost memory-dirs-remove-btn';
        removeBtn.textContent = '✕';
        removeBtn.title = t('settings.memory_dirs.delete_title');
        removeBtn.setAttribute('aria-label', t('settings.memory_dirs.delete_title'));
        // Disable removing the last dir (server also enforces 400).
        if (dirs.length <= 1) removeBtn.disabled = true;
        removeBtn.addEventListener('click', () => handleRemove(path));
        item.appendChild(removeBtn);

        list.appendChild(item);
      }
      group.appendChild(list);
      wrap.appendChild(group);
    }

    // Add-input row.
    const addRow = document.createElement('div');
    addRow.className = 'memory-dirs-add';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'memory-dirs-add-input';
    input.placeholder = t('settings.memory_dirs.add_placeholder');
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-sm';
    addBtn.textContent = t('settings.memory_dirs.add_btn');
    addBtn.addEventListener('click', async () => {
      const val = input.value;
      input.value = '';
      await handleAdd(val);
    });
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); addBtn.click(); }
    });
    addRow.appendChild(input);
    addRow.appendChild(addBtn);
    wrap.appendChild(addRow);
  }

  render();
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
      hint.textContent = cfg.descriptions[val] || '';
      sel.addEventListener('change', () => {
        hint.textContent = cfg.descriptions[sel.value] || '';
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
      // Re-sync all UI from updated config
      STATE.serverConfig = await api('GET', '/api/config');
      _syncConfigToUI();
      // Check if changed fields need reindex/FTS rebuild
      _showReindexWarning(resp.applied);
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
    msg = 'Tokenizer and chunk settings changed. FTS index rebuild and re-indexing recommended for full effect.';
  } else if (needsFtsRebuild) {
    msg = 'Tokenizer changed. FTS index rebuild is recommended so existing data uses the new tokenizer.';
  } else {
    msg = 'Chunk settings changed. Re-indexing is recommended to apply new chunking to existing files.';
  }

  warn.innerHTML = `
    <div class="config-reindex-warn-text">${escapeHtml(msg)}</div>
    <div class="config-reindex-warn-actions">
      ${needsFtsRebuild ? '<button class="btn-primary btn-sm" id="cfg-fts-rebuild-btn">Rebuild FTS Index</button>' : ''}
      ${needsReindex ? '<button class="btn-primary btn-sm" id="cfg-reindex-btn">Re-index All</button>' : ''}
      <button class="btn-ghost btn-sm config-reindex-dismiss">Dismiss</button>
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
        showToast(res.message || `FTS rebuilt: ${res.rebuilt_rows} chunks`, 'success');
        btn.textContent = 'Done';
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
        btn.textContent = 'Done';
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


/* memtomem Web UI — Vanilla JS SPA */
'use strict';

const API = '';  // same origin

// ── Early declarations (referenced before their section) ──
let _stmPollId = null;
const _HELP_VISIBLE_KEY = 'm2m-help-visible';

// ── Unified global state ──
const STATE = {
  lastSettingsSection: 'config',
  selectedChunkId: null,
  selectedOriginal: '',
  lastQuery: '',
  selectedIds: new Set(),
  lastResults: [],
  currentTopK: 10,
  viewMode: 'card',
  scoreMin: 0,
  currentSortMode: 'score',
  maxResultScore: 0,
  sourcesBrowserStale: false,
  tagsTabStale: false,
  homeStale: false,
  detailViewSource: '',
  detailViewMode: 'view',
  allSources: [],
  sourcesSortBy: 'name',
  sourcesNsFilter: '',
  dedupScanActive: false,
  dedupAbortCtrl: null,
  lastTagsData: [],
  tagsView: 'cloud',
  tagsSortBy: 'count-desc',
  serverConfig: null,
  lastRetrievalStats: null,
  groupMode: false,
  cmdPaletteOpen: false,
  pendingGKey: false,
  touchStartX: 0,
  touchStartY: 0,
  helpVisible: true,
};

// ── C3: Theme init ──
(function initTheme() {
  const saved = localStorage.getItem('m2m-theme');
  const el = document.documentElement;
  if (saved === 'light') {
    el.setAttribute('data-theme', 'light');
  } else if (!saved && window.matchMedia('(prefers-color-scheme: light)').matches) {
    el.setAttribute('data-theme', 'light');
  }
  // Update toggle icon and finalize initialization on DOM ready
  document.addEventListener('DOMContentLoaded', () => {
    const isDark = el.getAttribute('data-theme') !== 'light';
    qs('theme-toggle').textContent = isDark ? '🌙' : '☀️';
    renderRecentChips();
    _initTabHelp();
  });
})();

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function qs(id) { return document.getElementById(id); }
function show(el)  { if (el) { el.hidden = false; el.style.display = ''; } }
function hide(el)  { if (el) el.hidden = true; }
function setMsg(el, text, isErr) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status-msg ' + (isErr ? 'err' : 'ok');
  show(el);
  setTimeout(() => hide(el), 4000);
}
function truncate(str, n) { return str.length > n ? str.slice(0, n) + '…' : str; }
function basename(path) { return path.split('/').pop() || path; }
function shortDir(dir) {
  const parts = dir.split('/').filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : dir;
}
function formatBytes(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
function fileIcon(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  const map = {
    md: '📝', markdown: '📝',
    py: '🐍',
    js: '🟨', ts: '🔷', jsx: '🟨', tsx: '🔷',
    json: '{}',
    txt: '📄', text: '📄',
    rs: '🦀', go: '🐹',
    sh: '💲', bash: '💲',
    yaml: '⚙️', yml: '⚙️', toml: '⚙️',
    html: '🌐', css: '🎨',
    csv: '📊',
  };
  return map[ext] || '📄';
}

function fileTypeColor(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  const map = {
    md: 'var(--accent)', markdown: 'var(--accent)',
    py: 'var(--green)',
    js: '#e0a800', ts: '#e0a800', jsx: '#e0a800', tsx: '#e0a800',
    json: '#a29bfe', yaml: '#a29bfe', yml: '#a29bfe', toml: '#a29bfe',
    html: '#e17055', css: '#e17055',
  };
  return map[ext] || 'var(--muted)';
}

function relativeTime(dateStr) {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

// ── B1: Debounce ──
function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// ── B2: Copy to Clipboard ──
async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  showToast('Copied!', 'info');
}

// ── B3: Language Detection ──
function getLanguage(sourceFile) {
  const ext = (sourceFile || '').split('.').pop().toLowerCase();
  return { py: 'python', js: 'javascript', ts: 'typescript', json: 'json',
           sh: 'bash', bash: 'bash', yaml: 'yaml', yml: 'yaml',
           css: 'css', html: 'markup' }[ext] || null;
}

// ── D2: Line Diff ──
function diffLines(oldText, newText) {
  const a = oldText.split('\n'), b = newText.split('\n');
  const m = a.length, n = b.length;
  const dp = Array.from({length: m + 1}, () => new Uint16Array(n + 1));
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1] + 1 : Math.max(dp[i-1][j], dp[i][j-1]);
  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i-1] === b[j-1]) { ops.push({t:'=', l:a[i-1]}); i--; j--; }
    else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) { ops.push({t:'+', l:b[j-1]}); j--; }
    else { ops.push({t:'-', l:a[i-1]}); i--; }
  }
  return ops.reverse();
}
function renderDiff(ops) {
  return ops.map(op => {
    const cls = op.t === '+' ? 'diff-add' : op.t === '-' ? 'diff-del' : 'diff-eq';
    const prefix = op.t === '+' ? '+' : op.t === '-' ? '-' : ' ';
    return `<div class="diff-line ${cls}"><span class="diff-prefix">${prefix}</span>${escapeHtml(op.l)}</div>`;
  }).join('');
}

// ── A4: Loading Spinner ──
function btnLoading(btn, loading) {
  if (loading) {
    btn.disabled = true;
    btn.classList.add('btn-loading');
  } else {
    btn.disabled = false;
    btn.classList.remove('btn-loading');
  }
}
function panelLoading(container) {
  container.innerHTML = '<div class="loading-panel"><div class="spinner-panel"></div></div>';
}

// ── A5: Empty State ──
function emptyState(icon, message, hint) {
  const h = hint ? `<span class="empty-state-hint">${escapeHtml(hint)}</span>` : '';
  return `<span class="empty-state-icon">${icon}</span><span>${escapeHtml(message)}</span>${h}`;
}

// ── A1: Toast Notifications ──
function showToast(message, type = 'success') {
  const container = qs('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<span class="toast-msg">${escapeHtml(message)}</span><button class="toast-close" title="Close">✕</button>`;
  const delay = type === 'error' ? 5000 : 3000;
  let timer;
  function dismiss() {
    clearTimeout(timer);
    toast.classList.add('toast-out');
    toast.addEventListener('animationend', () => toast.remove(), { once: true });
  }
  toast.querySelector('.toast-close').addEventListener('click', dismiss);
  timer = setTimeout(dismiss, delay);
  container.appendChild(toast);
}

// ── A3: Confirm Dialog ──
function showConfirm({ title, message = '', confirmText = 'Confirm' }) {
  return new Promise(resolve => {
    const modal = qs('confirm-modal');
    qs('confirm-title').textContent = title;
    qs('confirm-message').textContent = message;
    qs('confirm-ok-btn').textContent = confirmText;
    show(modal);
    const focusables = [qs('confirm-cancel-btn'), qs('confirm-ok-btn')];
    focusables[1].focus();

    function cleanup(result) {
      hide(modal);
      modal.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      resolve(result);
    }
    function onBackdrop(e) { if (e.target === modal) cleanup(false); }
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); cleanup(false); }
      if (e.key === 'Tab') {
        e.preventDefault();
        const idx = focusables.indexOf(document.activeElement);
        focusables[(idx + (e.shiftKey ? -1 : 1) + focusables.length) % focusables.length].focus();
      }
    }
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    qs('confirm-cancel-btn').onclick = () => cleanup(false);
    qs('confirm-ok-btn').onclick = () => cleanup(true);
  });
}

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------

function activateTab(tabName) {
  // Deactivate all main tabs
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.remove('active');
    b.setAttribute('aria-selected', 'false');
  });

  // Hide all panels
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

  // Activate the correct button
  const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
  if (btn) {
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
  }

  // Show panel
  const panel = qs(`tab-${tabName}`);
  if (panel) {
    panel.hidden = false;
    panel.classList.add('active');
    // Focus first focusable element in new panel
    const focusable = panel.querySelector('input:not([hidden]):not([disabled]), button:not([hidden]):not([disabled]), [tabindex="0"]');
    if (focusable) focusable.focus();
  }

  // History API — enable back button and deep linking
  if (location.hash !== `#${tabName}`) {
    history.pushState({ tab: tabName }, '', `#${tabName}`);
  }

  // Tab-specific loads
  if (tabName === 'home') { STATE.homeStale = false; loadDashboard(); renderPinnedSection(); }
  if (tabName === 'sources') { STATE.sourcesBrowserStale = false; loadSources(); }
  if (tabName === 'index') loadStats();
  if (tabName === 'tags') { STATE.tagsTabStale = false; loadTags(); }
  if (tabName === 'timeline') loadTimeline();
  if (tabName === 'stm') { loadStmDashboard(); _startStmPolling(); } else { _stopStmPolling(); }
  if (tabName === 'settings') switchSettingsSection(STATE.lastSettingsSection || 'config');
  if (['search', 'timeline'].includes(tabName)) loadNamespaceDropdowns();
}

// Settings Hub section switching

function switchSettingsSection(sectionName) {
  STATE.lastSettingsSection = sectionName;
  document.querySelectorAll('.settings-nav-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.settings-section').forEach(s => s.classList.remove('active'));
  const btn = document.querySelector(`.settings-nav-btn[data-section="${sectionName}"]`);
  const section = document.getElementById(`settings-${sectionName}`);
  if (btn) btn.classList.add('active');
  if (section) section.classList.add('active');
  // Section-specific loads (reuse existing functions)
  if (sectionName === 'config') loadConfig();
  if (sectionName === 'namespaces') loadNamespacesTab();
  if (sectionName === 'dedup') resetDedupPanel();
  if (sectionName === 'decay') resetDecayPanel();
  if (sectionName === 'export') { resetExportPanel(); loadNamespaceDropdowns(); }
  if (sectionName === 'harness-sessions') loadHarnessSessions();
  if (sectionName === 'harness-scratch') loadHarnessScratch();
  if (sectionName === 'harness-procedures') loadHarnessProcedures();
  if (sectionName === 'harness-health') loadHarnessHealth();
  if (sectionName === 'harness-watchdog') loadWatchdogStatus();
}

// Settings nav buttons
document.querySelectorAll('.settings-nav-btn').forEach(btn => {
  btn.addEventListener('click', () => switchSettingsSection(btn.dataset.section));
});

// Main tab buttons
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});

// ── E1: ARIA init ──
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.setAttribute('role', 'tab');
  btn.setAttribute('aria-controls', `tab-${btn.dataset.tab}`);
  btn.setAttribute('aria-selected', btn.classList.contains('active') ? 'true' : 'false');
});
document.querySelectorAll('.tab-panel').forEach(p => p.setAttribute('role', 'tabpanel'));
document.querySelector('.tab-nav').setAttribute('role', 'tablist');

// ── C3: Theme toggle ──
qs('theme-toggle').addEventListener('click', () => {
  const el = document.documentElement;
  const goLight = el.getAttribute('data-theme') !== 'light';
  el.setAttribute('data-theme', goLight ? 'light' : 'dark');
  qs('theme-toggle').textContent = goLight ? '☀️' : '🌙';
  localStorage.setItem('m2m-theme', goLight ? 'light' : 'dark');
});

// ── C1: Mobile back button ──
qs('mobile-back-btn').addEventListener('click', () => {
  document.querySelector('.results-layout').classList.remove('mobile-detail');
});

// ── History API: back/forward navigation + hash deep link ──
window.addEventListener('popstate', (e) => {
  if (e.state?.tab) activateTab(e.state.tab);
});
// Activate tab from URL hash on initial load (e.g. #sources)
{
  const hash = location.hash.slice(1);
  const validTabs = ['home', 'search', 'sources', 'index', 'tags', 'timeline', 'settings'];
  if (hash && validTabs.includes(hash)) {
    activateTab(hash);
  }
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

async function loadStats() {
  try {
    const data = await api('GET', '/api/stats');
    qs('stat-chunks').textContent = `${data.total_chunks} chunks`;
    qs('stat-sources').textContent = `${data.total_sources} sources`;
    qs('card-chunks').textContent = data.total_chunks;
    qs('card-sources').textContent = data.total_sources;
  } catch (_) { /* non-critical */ }
}

loadStats();
fetchServerConfig();
checkEmbeddingMismatch();

// ---------------------------------------------------------------------------
// Embedding mismatch banner
// ---------------------------------------------------------------------------

async function checkEmbeddingMismatch() {
  try {
    const data = await api('GET', '/api/embedding-status');
    if (!data.has_mismatch) return;

    // Session dismiss — only show once per session
    if (sessionStorage.getItem('m2m-emb-banner-dismissed')) return;

    const banner = qs('embedding-mismatch-banner');
    const msgEl = qs('emb-banner-msg');

    const parts = [];
    if (data.dimension_mismatch) {
      parts.push(`dimension: DB ${data.stored.dimension} ≠ config ${data.configured.dimension}`);
    }
    if (data.model_mismatch) {
      parts.push(`model: DB ${data.stored.provider}/${data.stored.model} ≠ config ${data.configured.provider}/${data.configured.model}`);
    }
    msgEl.textContent = `Embedding mismatch — ${parts.join(' / ')}. Search may not work until resolved.`;
    show(banner);

    // Dismiss button
    const dismissBtn = banner.querySelector('.emb-banner-dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', () => {
        hide(banner);
        sessionStorage.setItem('m2m-emb-banner-dismissed', '1');
      }, { once: true });
    }

    qs('emb-reset-btn').addEventListener('click', async () => {
      const warned = confirm(
        'WARNING: All indexed vectors will be deleted.\n' +
        `New config will be applied: ${data.configured.provider}/${data.configured.model} (dim ${data.configured.dimension})\n\n` +
        'Re-indexing will be required afterwards. Continue?'
      );
      if (!warned) return;
      try {
        const res = await api('POST', '/api/embedding-reset');
        hide(banner);
        sessionStorage.removeItem('m2m-emb-banner-dismissed');
        await fetchServerConfig();
        showToast(res.message, 'success');
      } catch (err) {
        showToast('Reset failed: ' + err.message, 'error');
      }
    }, { once: true });
  } catch (_) { /* non-critical */ }
}

// ---------------------------------------------------------------------------
// Home Dashboard (D3)
// ---------------------------------------------------------------------------

async function loadDashboard() {
  try {
    const [stats, sourcesData, nsData, configData, embStatus] = await Promise.all([
      api('GET', '/api/stats'),
      api('GET', '/api/sources'),
      api('GET', '/api/namespaces'),
      api('GET', '/api/config'),
      api('GET', '/api/embedding-status').catch(() => null),
    ]);

    const allSources = sourcesData.sources || [];
    const namespaces = nsData.namespaces || [];

    // A. Stats cards
    qs('home-chunks').textContent = stats.total_chunks.toLocaleString();
    qs('home-sources').textContent = stats.total_sources.toLocaleString();
    qs('home-namespaces').textContent = namespaces.length;
    const totalSize = allSources.reduce((sum, s) => sum + (s.file_size || 0), 0);
    qs('home-total-size').textContent = formatBytes(totalSize) || '0 B';

    // Harness stats (sessions + scratch)
    try {
      const [sessData, scratchData] = await Promise.all([
        api('GET', '/api/sessions?limit=1').catch(() => ({ total: 0 })),
        api('GET', '/api/scratch').catch(() => ({ total: 0 })),
      ]);
      qs('home-sessions').textContent = sessData.total;
      qs('home-scratch').textContent = scratchData.total;
    } catch { /* non-critical */ }

    // B. Activity Heatmap
    _renderActivityMap(allSources);

    // D. File Type Distribution
    _renderFileTypeChart(allSources);

    // G. Namespace Summary
    _renderNsChart(namespaces);

    // Chunk Size Distribution
    _renderChunkDist(stats.chunk_size_distribution || []);

    // E. Recent Sources (improved)
    _renderHomeRecent(allSources);

    // H. Storage Health — use DB-stored values when available
    _renderStorageHealth(configData, allSources, embStatus);

    // Pinned chunks
    renderPinnedSection();
  } catch (err) {
    qs('home-recent-list').innerHTML = `<p style="color:var(--danger);font-size:.83rem">Error: ${escapeHtml(err.message)}</p>`;
  }
}

// B. Activity Heatmap — last 14 days
function _renderActivityMap(sources) {
  const map = qs('home-activity-map');
  const now = new Date();
  const days = 14;
  const counts = [];
  const labels = [];

  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    const key = d.toISOString().slice(0, 10);
    labels.push(key);
    let cnt = 0;
    sources.forEach(s => {
      if (s.last_indexed_at && s.last_indexed_at.slice(0, 10) === key) cnt++;
    });
    counts.push(cnt);
  }

  const maxCount = Math.max(1, ...counts);
  map.innerHTML = counts.map((c, i) => {
    const h = Math.max(4, Math.round((c / maxCount) * 32));
    const weekday = new Date(labels[i]).toLocaleDateString('en', { weekday: 'short' });
    const opacity = c === 0 ? 0.15 : 0.3 + (c / maxCount) * 0.7;
    return `<div class="home-activity-day" style="height:${h}px;background:var(--accent);opacity:${opacity}" data-tooltip="${weekday} ${labels[i]}: ${c} files"></div>`;
  }).join('');
}

// D. File Type Distribution
function _renderFileTypeChart(sources) {
  const chart = qs('home-type-chart');
  const typeCounts = {};
  sources.forEach(s => {
    const ext = (s.path.split('.').pop() || 'other').toLowerCase();
    typeCounts[ext] = (typeCounts[ext] || 0) + 1;
  });

  const sorted = Object.entries(typeCounts).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const max = sorted[0]?.[1] || 1;

  if (!sorted.length) {
    chart.innerHTML = '<span style="color:var(--muted);font-size:0.78rem">No files indexed</span>';
    return;
  }

  chart.innerHTML = sorted.map(([ext, count]) => {
    const pct = Math.round((count / max) * 100);
    const color = fileTypeColor('x.' + ext);
    return `<div class="home-bar-row">
      <span class="home-bar-label">.${escapeHtml(ext)}</span>
      <div class="home-bar-track"><div class="home-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="home-bar-count">${count}</span>
    </div>`;
  }).join('');
}

// Chunk Size Distribution (token buckets)
function _renderChunkDist(distribution) {
  const chart = qs('home-chunk-dist');
  if (!distribution.length) {
    chart.innerHTML = '<span style="color:var(--muted);font-size:0.78rem">No data</span>';
    return;
  }
  const total = distribution.reduce((s, d) => s + d.count, 0);
  const max = Math.max(1, ...distribution.map(d => d.count));

  chart.innerHTML = distribution.map(d => {
    const pct = Math.round((d.count / max) * 100);
    const ratio = total ? Math.round((d.count / total) * 100) : 0;
    const color = d.count === 0 ? 'var(--muted)' : 'var(--accent)';
    return `<div class="home-bar-row">
      <span class="home-bar-label">${escapeHtml(d.bucket)}</span>
      <div class="home-bar-track"><div class="home-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="home-bar-count">${d.count} <span class="muted-sm">(${ratio}%)</span></span>
    </div>`;
  }).join('');
}

// G. Namespace Summary
function _renderNsChart(namespaces) {
  const chart = qs('home-ns-chart');
  if (!namespaces.length) {
    chart.innerHTML = '<span style="color:var(--muted);font-size:0.78rem">No namespaces</span>';
    return;
  }

  const sorted = [...namespaces].sort((a, b) => b.chunk_count - a.chunk_count).slice(0, 6);
  const max = sorted[0]?.chunk_count || 1;
  const palette = ['var(--accent)', 'var(--green)', '#e0a800', '#a29bfe', '#e17055', '#00cec9'];

  chart.innerHTML = sorted.map((ns, i) => {
    const pct = Math.round((ns.chunk_count / max) * 100);
    const color = ns.color || palette[i % palette.length];
    return `<div class="home-bar-row">
      <span class="home-bar-label">${escapeHtml(ns.namespace)}</span>
      <div class="home-bar-track"><div class="home-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="home-bar-count">${ns.chunk_count.toLocaleString()}</span>
    </div>`;
  }).join('');
}

// E. Recent Sources — color dot + 2-row layout
function _renderHomeRecent(allSources) {
  const recentList = qs('home-recent-list');
  if (!allSources.length) {
    recentList.innerHTML = '<div class="empty-state">' + emptyState('📁', 'No sources indexed yet', 'Add files from the Index tab') + '</div>';
    return;
  }

  const sorted = [...allSources].sort((a, b) => {
    const ta = a.last_indexed_at ? new Date(a.last_indexed_at).getTime() : 0;
    const tb = b.last_indexed_at ? new Date(b.last_indexed_at).getTime() : 0;
    return tb - ta || b.chunk_count - a.chunk_count;
  }).slice(0, 8);

  recentList.innerHTML = sorted.map(s => {
    const name = basename(s.path);
    const size = s.file_size != null ? formatBytes(s.file_size) : '';
    const age = s.last_indexed_at ? relativeTime(s.last_indexed_at) : '';
    const nsBadges = (s.namespaces || [])
      .filter(ns => ns !== 'default')
      .map(ns => `<span class="badge badge-ns source-ns-badge">${escapeHtml(ns)}</span>`)
      .join('');
    return `
      <div class="home-source-item home-recent-item" data-path="${escapeAttr(s.path)}" title="${escapeAttr(s.path)}" tabindex="0" role="button">
        <div class="home-source-row1">
          <span class="home-source-dot" style="background:${fileTypeColor(s.path)}"></span>
          <span class="home-source-name">${escapeHtml(name)}</span>
          ${nsBadges}
          <span class="badge badge-blue">${s.chunk_count} chunks</span>
        </div>
        <div class="home-source-row2">
          ${size}${size && age ? ' \u00b7 ' : ''}${age}
        </div>
      </div>`;
  }).join('');

  recentList.querySelectorAll('.home-source-item').forEach(el => {
    const go = () => _navigateToSource(el.dataset.path);
    el.addEventListener('click', go);
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
}

// H. Storage Health
function _renderStorageHealth(config, sources, embStatus) {
  const info = qs('home-health-info');
  const lastIndexed = sources
    .filter(s => s.last_indexed_at)
    .sort((a, b) => new Date(b.last_indexed_at).getTime() - new Date(a.last_indexed_at).getTime())[0];
  const lastTime = lastIndexed ? relativeTime(lastIndexed.last_indexed_at) : 'Never';

  // Use DB-stored embedding values when available, fall back to config
  const stored = embStatus && embStatus.stored;
  const embProvider = stored ? stored.provider : config.embedding.provider;
  const embModel = stored ? stored.model : config.embedding.model;
  const embDim = stored ? stored.dimension : config.embedding.dimension;
  const hasMismatch = embStatus && embStatus.has_mismatch;
  const warnIcon = hasMismatch ? ' ⚠' : '';

  info.innerHTML = `
    <div class="home-health-item">
      <span class="home-health-label">Embedding</span>
      <span class="home-health-value">${escapeHtml(embProvider)}/${escapeHtml(embModel)}${warnIcon}</span>
    </div>
    <div class="home-health-item">
      <span class="home-health-label">Dimension</span>
      <span class="home-health-value">${embDim}${warnIcon}</span>
    </div>
    <div class="home-health-item">
      <span class="home-health-label">Storage</span>
      <span class="home-health-value">${escapeHtml(config.storage.backend)}</span>
    </div>
    <div class="home-health-item">
      <span class="home-health-label">Last Indexed</span>
      <span class="home-health-value">${escapeHtml(lastTime)}</span>
    </div>
  `;
}

function _navigateToSource(path) {
  activateTab('sources');
  setTimeout(() => {
    document.querySelectorAll('.source-item').forEach(el => {
      if (el.title === path) {
        el.classList.add('active');
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        browseSource(path);
      }
    });
  }, 300);
}

// C. Quick Search from Home
qs('home-search-go').addEventListener('click', () => {
  const q = qs('home-search-input').value.trim();
  if (!q) return;
  activateTab('search');
  qs('search-input').value = q;
  qs('search-btn').click();
});
qs('home-search-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') qs('home-search-go').click();
});

// F. Quick Actions
qs('home-search-btn').addEventListener('click', () => {
  activateTab('search');
  qs('search-input').focus();
});
qs('home-index-btn').addEventListener('click', () => {
  activateTab('index');
});
qs('home-reindex-btn').addEventListener('click', () => {
  activateTab('index');
  qs('index-force').checked = true;
});
qs('home-export-btn').addEventListener('click', () => {
  activateTab('settings');
  document.querySelector('.settings-nav-btn[data-section="export"]')?.click();
});
qs('home-dedup-btn').addEventListener('click', () => {
  activateTab('settings');
  document.querySelector('.settings-nav-btn[data-section="dedup"]')?.click();
});
qs('home-tags-btn').addEventListener('click', () => {
  activateTab('tags');
});

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

// Search and stale state now in STATE object

function _markDataStale() {
  STATE.sourcesBrowserStale = true;
  STATE.tagsTabStale = true;
  STATE.homeStale = true;
}

// Sync result content in STATE.lastResults cache and DOM after edit
function _syncResultContent(chunkId, newContent) {
  const cached = STATE.lastResults.find(r => String(r.chunk.id) === String(chunkId));
  if (cached) cached.chunk.content = newContent;
  const item = document.querySelector(`.result-item[data-id="${CSS.escape(String(chunkId))}"]`);
  if (!item) return;
  const snippet = item.querySelector('.result-snippet');
  if (snippet) snippet.innerHTML = highlightText(truncate(newContent, 200), STATE.lastQuery);
}

qs('search-btn').addEventListener('click', doSearch);
qs('search-input').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
qs('search-input').addEventListener('focus', () => renderHistoryDropdown());
qs('search-input').addEventListener('input', () => renderHistoryDropdown());
qs('search-input').addEventListener('input', debounce(() => {
  if (qs('search-input').value.trim().length >= 2) doSearch();
}, 400));
document.addEventListener('click', e => {
  const dropdown = qs('search-history-dropdown');
  if (dropdown && !dropdown.contains(e.target) && e.target !== qs('search-input')) {
    hide(dropdown);
  }
});

// E. Active filters display
function _renderActiveFilters() {
  const el = qs('active-filters');
  const chips = [];
  const ns = qs('ns-filter').value;
  if (ns) chips.push({ label: `ns: ${ns}`, clear: () => { qs('ns-filter').value = ''; } });
  const tag = qs('tag-filter').value.trim();
  if (tag) chips.push({ label: `tag: ${tag}`, clear: () => { qs('tag-filter').value = ''; } });
  const ct = qs('chunk-type-filter').value;
  if (ct) chips.push({ label: `type: ${ct.replace('_', ' ')}`, clear: () => { qs('chunk-type-filter').value = ''; } });
  if (STATE.scoreMin > 0) chips.push({ label: `score \u2265 ${STATE.scoreMin}`, clear: () => { qs('score-threshold').value = 0; STATE.scoreMin = 0; qs('score-val').textContent = '0.0'; } });

  if (!chips.length) { hide(el); return; }
  el.innerHTML = chips.map((c, i) =>
    `<span class="active-filter-chip">${escapeHtml(c.label)}<button class="active-filter-remove" data-idx="${i}">\u2715</button></span>`
  ).join('');
  el.querySelectorAll('.active-filter-remove').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      chips[parseInt(btn.dataset.idx)].clear();
      renderResults(STATE.lastResults);
      if (STATE.lastResults.length) doSearch();
    });
  });
  show(el);
}

async function doSearch() {
  const q = qs('search-input').value.trim();
  if (!q) return;
  STATE.lastQuery = q;
  saveToHistory(q);
  hide(qs('search-history-dropdown'));
  STATE.currentTopK = parseInt(qs('top-k').value, 10);
  const tf  = qs('tag-filter').value.trim();
  const nsFilter = qs('ns-filter').value;

  const params = new URLSearchParams({ q, top_k: STATE.currentTopK });
  if (tf) params.set('tag_filter', tf);
  if (nsFilter) params.set('namespace', nsFilter);
  const ctxWin = parseInt((qs('context-window') || {}).value || '0', 10);
  if (ctxWin > 0) params.set('context_window', ctxWin);
  // Pass source filter to backend for pre-query filtering
  const selectedSources = Array.from((qs('source-filter') || {selectedOptions:[]}).selectedOptions)
    .map(o => o.value).filter(Boolean);
  if (selectedSources.length) params.set('source_filter', selectedSources.join(','));

  const btn = qs('search-btn');
  btnLoading(btn, true);
  try {
    const data = await api('GET', `/api/search?${params}`);
    renderResults(data.results, data.retrieval_stats);
  } catch (err) {
    const list = qs('results-list');
    list.innerHTML = '';
    hide(qs('results-empty'));
    show(list);
    list.innerHTML = emptyState('⚠', 'Search failed', escapeHtml(err.message));
    clearDetail();
  } finally {
    btnLoading(btn, false);
  }
}

function updateBulkToolbar(total) {
  const count = STATE.selectedIds.size;
  qs('bulk-count').textContent = count > 0 ? `${count} selected` : '0 selected';
  qs('bulk-delete-btn').disabled = count === 0;
  qs('bulk-export-btn').disabled = count === 0;
  const allCb = qs('bulk-select-all');
  allCb.checked = total > 0 && count === total;
  allCb.indeterminate = count > 0 && count < total;
}

function _buildResultItem(r) {
  const list = qs('results-list');
  const item = document.createElement('div');
  item.className = 'result-item';
  item.dataset.id = r.chunk.id;
  item.setAttribute('tabindex', '0');

  const checkLabel = document.createElement('label');
  checkLabel.className = 'result-check-wrap';
  checkLabel.addEventListener('click', e => e.stopPropagation());
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.className = 'result-checkbox';
  checkbox.dataset.id = r.chunk.id;
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) STATE.selectedIds.add(r.chunk.id);
    else STATE.selectedIds.delete(r.chunk.id);
    updateBulkToolbar(list.querySelectorAll('.result-checkbox').length);
  });
  checkLabel.appendChild(checkbox);

  const fname = basename(r.chunk.source_file || '');
  const dir = shortDir((r.chunk.source_file || '').split('/').slice(0, -1).join('/') || '/');
  const age = relativeTime(r.chunk.created_at);
  const nsBadge = r.chunk.namespace && r.chunk.namespace !== 'default'
    ? ` <span class="badge badge-ns">${escapeHtml(r.chunk.namespace)}</span>` : '';
  const scorePct = STATE.maxResultScore > 0 ? Math.round((r.score / STATE.maxResultScore) * 100) : 0;
  const barColor = scorePct > 70 ? 'var(--green)' : scorePct > 40 ? 'var(--accent)' : 'var(--muted)';

  const body = document.createElement('div');
  body.className = 'result-body';
  body.innerHTML = `
    <div class="result-item-row1">
      <span class="result-type-dot" style="background:${fileTypeColor(r.chunk.source_file || '')}"></span>
      <span class="result-filename">${escapeHtml(fname)}</span>
      <span class="score-badge">${r.score.toFixed(3)}</span>
      <span class="badge badge-retrieval badge-retrieval--${escapeAttr(r.source)}">${escapeHtml(r.source)}</span>
      ${nsBadge}
    </div>
    <div class="result-item-meta">${escapeHtml(dir)} \u00b7 #${r.rank} \u00b7 ${escapeHtml(age)}</div>
    <div class="result-score-bar"><div class="result-score-fill" style="width:${scorePct}%;background:${barColor}"></div></div>
    <div class="result-snippet">${highlightText(truncate(r.chunk.content, 200), STATE.lastQuery)}</div>
  `;

  // Filename click → open source preview modal
  const fnameEl = body.querySelector('.result-filename');
  if (fnameEl) {
    fnameEl.style.cursor = 'pointer';
    fnameEl.title = 'View full source file';
    fnameEl.addEventListener('click', e => {
      e.stopPropagation();
      openSourcePreview(r.chunk.source_file, r.chunk.start_line, r.chunk.end_line);
    });
  }

  item.appendChild(checkLabel);
  item.appendChild(body);

  // Context window rendering — document order (before ↑ snippet ↓ after)
  if (r.context && (r.context.window_before?.length || r.context.window_after?.length)) {
    const snippet = body.querySelector('.result-snippet');
    const bLen = r.context.window_before?.length || 0;
    const aLen = r.context.window_after?.length || 0;

    // Before blocks — inserted above snippet
    let ctxBefore = null;
    if (bLen) {
      ctxBefore = document.createElement('div');
      ctxBefore.className = 'context-group context-group-before';
      ctxBefore.hidden = true;
      r.context.window_before.forEach(cb => {
        const pos = document.createElement('span');
        pos.className = 'context-pos';
        pos.textContent = `↑ L${cb.start_line || '?'}–${cb.end_line || '?'}`;
        const blk = document.createElement('div');
        blk.className = 'context-block context-block-before';
        blk.textContent = truncate(cb.content, 300);
        ctxBefore.appendChild(pos);
        ctxBefore.appendChild(blk);
      });
      snippet.before(ctxBefore);
    }

    // After blocks — inserted below snippet
    let ctxAfter = null;
    if (aLen) {
      ctxAfter = document.createElement('div');
      ctxAfter.className = 'context-group context-group-after';
      ctxAfter.hidden = true;
      r.context.window_after.forEach(ca => {
        const pos = document.createElement('span');
        pos.className = 'context-pos';
        pos.textContent = `↓ L${ca.start_line || '?'}–${ca.end_line || '?'}`;
        const blk = document.createElement('div');
        blk.className = 'context-block context-block-after';
        blk.textContent = truncate(ca.content, 300);
        ctxAfter.appendChild(pos);
        ctxAfter.appendChild(blk);
      });
      snippet.after(ctxAfter);
    }

    // Toggle button — below everything
    const toggleBtn = document.createElement('button');
    toggleBtn.className = 'context-toggle-btn';
    toggleBtn.textContent = `Context (${bLen}+${aLen})`;
    toggleBtn.addEventListener('click', e => {
      e.stopPropagation();
      const showing = ctxBefore ? !ctxBefore.hidden : !ctxAfter?.hidden;
      if (ctxBefore) ctxBefore.hidden = showing;
      if (ctxAfter) ctxAfter.hidden = showing;
      toggleBtn.textContent = showing ? `Context (${bLen}+${aLen})` : 'Hide context';
    });
    body.appendChild(toggleBtn);
  }

  // Tag chips in result item (click=filter, ✕=delete)
  _attachResultTagRow(r.chunk.id, [...(r.chunk.tags || [])], body);

  item.addEventListener('click', () => {
    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('selected'));
    item.classList.add('selected');
    showDetail(r);
  });
  item.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); item.click(); }
  });
  return item;
}

// Render (or re-render) a tag row inside a result-item body.
// liveTagsArr is a mutable array shared between chips so removals stay consistent.
function _attachResultTagRow(chunkId, liveTagsArr, bodyEl) {
  // Remove existing tag row if re-rendering
  const existing = bodyEl.querySelector('.result-tags');
  if (existing) existing.remove();
  if (!liveTagsArr.length) return;

  const tagRow = document.createElement('div');
  tagRow.className = 'result-tags';

  function _makeChip(tag) {
    const chip = document.createElement('span');
    chip.className = 'result-tag-chip';
    const label = document.createElement('span');
    label.className = 'result-tag-label';
    label.textContent = tag;
    label.title = `Filter by "${tag}"`;
    label.addEventListener('click', e => {
      e.stopPropagation();
      qs('tag-filter').value = tag;
      doSearch();
    });
    const removeBtn = document.createElement('button');
    removeBtn.className = 'result-tag-remove';
    removeBtn.textContent = '✕';
    removeBtn.title = `Remove tag "${tag}"`;
    removeBtn.addEventListener('click', async e => {
      e.stopPropagation();
      const idx = liveTagsArr.indexOf(tag);
      if (idx === -1) return;
      liveTagsArr.splice(idx, 1);
      chip.remove();
      if (qs('tag-filter').value === tag) qs('tag-filter').value = '';
      // Also update STATE.lastResults cache
      const cached = STATE.lastResults.find(r => String(r.chunk.id) === String(chunkId));
      if (cached) cached.chunk.tags = [...liveTagsArr];
      try {
        await api('PATCH', `/api/chunks/${chunkId}/tags`, { tags: [...liveTagsArr] });
        if (String(STATE.selectedChunkId) === String(chunkId)) renderTagChips([...liveTagsArr]);
      } catch (err) {
        liveTagsArr.splice(idx, 0, tag);
        if (cached) cached.chunk.tags = [...liveTagsArr];
        tagRow.appendChild(_makeChip(tag));
        showToast('Failed to remove tag: ' + err.message, 'error');
      }
    });
    chip.appendChild(label);
    chip.appendChild(removeBtn);
    return chip;
  }

  liveTagsArr.forEach(t => tagRow.appendChild(_makeChip(t)));
  bodyEl.appendChild(tagRow);
}

// Sync result item tag row after external tag save (e.g. detail panel Save Tags).
function _syncResultTags(chunkId, newTags) {
  // Update STATE.lastResults cache
  const cached = STATE.lastResults.find(r => String(r.chunk.id) === String(chunkId));
  if (cached) cached.chunk.tags = [...newTags];
  // Update DOM
  const item = document.querySelector(`.result-item[data-id="${CSS.escape(String(chunkId))}"]`);
  if (!item) return;
  const body = item.querySelector('.result-body');
  if (body) _attachResultTagRow(chunkId, [...newTags], body);
}

function renderResults(results, retrievalStats) {
  STATE.lastResults = results;
  let display = [...results];
  if (STATE.currentSortMode === 'date-desc') display.sort((a, b) => new Date(b.chunk.created_at) - new Date(a.chunk.created_at));
  else if (STATE.currentSortMode === 'date-asc') display.sort((a, b) => new Date(a.chunk.created_at) - new Date(b.chunk.created_at));
  else if (STATE.currentSortMode === 'source') display.sort((a, b) => (a.chunk.source_file || '').localeCompare(b.chunk.source_file || ''));
  const typeFilter = (qs('chunk-type-filter') || {}).value || '';
  const selectedSources = Array.from((qs('source-filter') || { selectedOptions: [] }).selectedOptions)
    .map(o => o.value).filter(Boolean);
  let filtered = typeFilter ? display.filter(r => r.chunk.chunk_type === typeFilter) : display;
  if (selectedSources.length) filtered = filtered.filter(r => selectedSources.includes(r.chunk.source_file));
  if (STATE.scoreMin > 0) filtered = filtered.filter(r => r.score >= STATE.scoreMin);
  // Date range filter
  const dateRange = _getDateRange();
  if (dateRange) {
    filtered = filtered.filter(r => {
      const t = new Date(r.chunk.created_at).getTime();
      return t >= dateRange.from && t <= dateRange.to;
    });
  }
  const list = qs('results-list');
  const empty = qs('results-empty');
  STATE.selectedIds.clear();

  if (!filtered.length) {
    hide(list);
    hide(qs('bulk-toolbar'));
    hide(qs('load-more-row'));
    show(empty);
    empty.innerHTML = emptyState('○', 'No results found', 'Try different keywords or filters');
    clearDetail();
    return;
  }
  hide(empty);
  show(list);

  // Compute max score for mini bars
  STATE.maxResultScore = Math.max(0.001, ...filtered.map(r => r.score));

  // Source breakdown summary + pipeline funnel
  const total = filtered.length;
  const counts = { fused: 0, bm25: 0, dense: 0, reranked: 0 };
  filtered.forEach(r => { if (r.source in counts) counts[r.source]++; });
  const sourceParts = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([src, n]) => `<span class="badge badge-retrieval badge-retrieval--${src}">${src} ${n}</span>`);
  let funnelHtml = '';
  if (retrievalStats) {
    const s = retrievalStats;
    const bm25Warn = s.bm25_error ? ` <span class="badge badge-yellow" title="${escapeHtml(s.bm25_error)}">!</span>` : '';
    funnelHtml = `<div class="results-funnel">
      <span class="help-tip" data-help="BM25: keyword matching. Dense: semantic embedding similarity. RRF: reciprocal rank fusion merges both. Final: after reranking and filters." tabindex="0" role="img" aria-label="BM25: keyword matching. Dense: semantic embedding similarity. RRF: reciprocal rank fusion merges both. Final: after reranking and filters.">i</span>
      <span class="funnel-stage"><span class="funnel-stage-label">BM25${bm25Warn}</span> <span class="funnel-stage-count">${s.bm25_candidates}</span></span>
      <span class="funnel-arrow">+</span>
      <span class="funnel-stage"><span class="funnel-stage-label">Dense</span> <span class="funnel-stage-count">${s.dense_candidates}</span></span>
      <span class="funnel-arrow">\u2192</span>
      <span class="funnel-stage"><span class="funnel-stage-label">RRF</span> <span class="funnel-stage-count">${s.fused_total}</span></span>
      <span class="funnel-arrow">\u2192</span>
      <span class="funnel-stage"><span class="funnel-stage-label">Final</span> <span class="funnel-stage-count">${s.final_total}</span></span>
    </div>`;
    // Cache retrieval stats for score detail computation
    STATE.lastRetrievalStats = s;
  }
  const summaryHtml = `<div class="results-summary"><span class="results-summary-total">${total} total</span>${sourceParts.join('')}${funnelHtml}</div>`;

  // E. Active filters display
  _renderActiveFilters();

  show(qs('bulk-toolbar'));
  if (results.length >= STATE.currentTopK) show(qs('load-more-row'));
  else hide(qs('load-more-row'));
  updateBulkToolbar(0);
  list.innerHTML = summaryHtml;
  list.classList.toggle('list-view', STATE.viewMode === 'list');

  if (STATE.groupMode) {
    const groups = {};
    filtered.forEach(r => {
      const key = r.chunk.source_file || '(unknown)';
      if (!groups[key]) groups[key] = [];
      groups[key].push(r);
    });
    let firstResult = null, firstItem = null;
    Object.entries(groups).forEach(([source, items]) => {
      const groupEl = document.createElement('div');
      groupEl.className = 'result-source-group';
      const header = document.createElement('div');
      header.className = 'result-group-header';
      header.innerHTML = `<span class="result-group-chevron">▼</span><span class="result-group-name">${escapeHtml(basename(source))}</span><span class="badge badge-blue">${items.length}</span>`;
      const groupItems = document.createElement('div');
      groupItems.className = 'result-group-items';
      header.addEventListener('click', () => {
        const isOpen = !groupItems.hidden;
        groupItems.hidden = isOpen;
        header.querySelector('.result-group-chevron').textContent = isOpen ? '▶' : '▼';
      });
      items.forEach(r => {
        const item = _buildResultItem(r);
        groupItems.appendChild(item);
        if (!firstResult) { firstResult = r; firstItem = item; }
      });
      groupEl.appendChild(header);
      groupEl.appendChild(groupItems);
      list.appendChild(groupEl);
    });
    if (firstItem) { firstItem.classList.add('selected'); showDetail(firstResult); }
  } else {
    filtered.forEach((r, i) => {
      const item = _buildResultItem(r);
      list.appendChild(item);
      if (i === 0) { item.classList.add('selected'); showDetail(r); }
    });
  }
}

function showDetail(r) {
  hide(qs('detail-empty'));
  const view = qs('detail-view');
  show(view);

  STATE.selectedChunkId = r.chunk.id;
  STATE.selectedOriginal = r.chunk.content;

  qs('d-score').textContent = `score ${r.score.toFixed(4)}`;
  qs('d-type').textContent = r.chunk.chunk_type.replace('_', ' ');
  const nsEl = qs('d-namespace');
  if (r.chunk.namespace && r.chunk.namespace !== 'default') {
    nsEl.textContent = r.chunk.namespace;
    show(nsEl);
  } else {
    hide(nsEl);
  }
  const srcEl = qs('d-source');
  srcEl.textContent = r.source;
  srcEl.className = `badge badge-retrieval badge-retrieval--${r.source}`;

  // Score detail row: rank + bar + pct of theoretical max RRF
  const rrfK = (STATE.serverConfig && STATE.serverConfig.search && STATE.serverConfig.search.rrf_k) || 60;
  const rs = STATE.lastRetrievalStats || {};
  const nSources = ((rs.bm25_candidates > 0) ? 1 : 0) + ((rs.dense_candidates > 0) ? 1 : 0) || 2;
  const maxRrf = nSources / (rrfK + 1);
  const pct = Math.min(r.score / maxRrf * 100, 100);
  qs('d-rank-label').textContent = `#${r.rank}`;
  qs('d-score-bar').style.width = `${pct.toFixed(1)}%`;
  qs('d-score-pct').textContent = `${pct.toFixed(0)}%`;
  const scoreDetailRow = qs('d-score-detail');
  scoreDetailRow.dataset.tooltip = `RRF ${r.score.toFixed(6)} / max ${maxRrf.toFixed(6)} (k=${rrfK}, ${nSources} sources)`;
  show(scoreDetailRow);
  qs('d-hierarchy').textContent = r.chunk.heading_hierarchy.join(' › ');
  qs('d-file').textContent = r.chunk.source_file;
  qs('d-lines').textContent = `lines ${r.chunk.start_line}–${r.chunk.end_line}`;
  qs('d-editor').value = r.chunk.content;
  hide(qs('detail-msg'));
  hide(qs('similar-panel'));
  hide(qs('source-chunks-panel'));
  hide(qs('history-panel'));

  renderTagChips(r.chunk.tags || []);
  updatePinBtn(r.chunk.id);
  _updateHistoryBtn(r.chunk.id);
  qs('d-created').textContent = relativeTime(r.chunk.created_at);
  _updateWordCount();
  _updateSourceNav();

  // Set source and apply current view mode (default: view)
  STATE.detailViewSource = r.chunk.source_file || '';
  hide(qs('d-preview'));
  hide(qs('d-preview-btn'));  // Preview merged into View
  _setDetailMode(STATE.detailViewMode || 'view');

  // Reset diff state
  hide(qs('d-diff'));
  hide(qs('d-diff-btn'));
  qs('d-diff-btn').dataset.mode = 'source';
  qs('d-diff-btn').textContent = 'Diff';

  // C1: On mobile, switch to detail panel view
  if (window.innerWidth <= 768) {
    document.querySelector('.results-layout').classList.add('mobile-detail');
  }
}

function clearDetail() {
  hide(qs('detail-view'));
  show(qs('detail-empty'));
  qs('detail-empty').querySelector('p').textContent = 'Select a result to view details';
  STATE.selectedChunkId = null;
}

// Edit / Delete / Reset
qs('d-save-btn').addEventListener('click', async () => {
  if (!STATE.selectedChunkId) return;
  const newContent = qs('d-editor').value;
  const btn = qs('d-save-btn');
  btnLoading(btn, true);
  try {
    _pushHistory(STATE.selectedChunkId, STATE.selectedOriginal);
    await api('PATCH', `/api/chunks/${STATE.selectedChunkId}`, { new_content: newContent });
    showToast('Chunk saved.', 'success');
    STATE.selectedOriginal = newContent;
    _syncResultContent(STATE.selectedChunkId, newContent);
    _updateHistoryBtn(STATE.selectedChunkId);
    _markDataStale();
    loadStats();
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
});

qs('d-delete-btn').addEventListener('click', async () => {
  if (!STATE.selectedChunkId) return;
  const r = STATE.lastResults.find(x => String(x.chunk.id) === String(STATE.selectedChunkId));
  const src = r ? r.chunk.source_file.split('/').pop() : '';
  const lines = r ? `lines ${r.chunk.start_line}–${r.chunk.end_line}` : '';
  const ok = await showConfirm({
    title: 'Delete Chunk',
    message: `This will permanently remove ${lines} from the source file "${src}" and delete the chunk from the index. This cannot be undone.`,
    confirmText: 'Delete',
  });
  if (!ok) return;
  try {
    await api('DELETE', `/api/chunks/${STATE.selectedChunkId}`);
    showToast('Chunk deleted.', 'success');
    clearDetail();
    _markDataStale();
    doSearch();
    loadStats();
  } catch (err) {
    showToast('Delete failed: ' + err.message, 'error');
  }
});

qs('d-reset-btn').addEventListener('click', () => {
  qs('d-editor').value = STATE.selectedOriginal;
  hide(qs('d-diff'));
  hide(qs('d-diff-btn'));
  qs('d-diff-btn').dataset.mode = 'source';
  _setDetailMode('edit');
  _updateWordCount();
  showToast('Content restored.', 'info');
});

// ── B2: Copy button ──
qs('d-copy-btn').addEventListener('click', () => copyToClipboard(qs('d-editor').value));

// ── B4: Markdown Preview toggle ──
// Preview merged into View — one toggle for both code highlighting and markdown rendering
// STATE.detailViewSource now in STATE
qs('d-view-btn').addEventListener('click', () => {
  const btn = qs('d-view-btn');
  const isViewing = btn.dataset.mode === 'view';
  if (isViewing) {
    _setDetailMode('edit');
  } else {
    _setDetailMode('view');
  }
});

function _setDetailMode(mode) {
  const btn = qs('d-view-btn');
  const codeView = qs('d-code-view');
  const editor = qs('d-editor');
  STATE.detailViewMode = mode;
  if (mode === 'edit') {
    hide(codeView);
    show(editor);
    btn.textContent = 'View';
    btn.dataset.mode = 'edit';
    // Show edit-only actions
    show(qs('d-save-btn'));
  } else {
    const content = editor.value;
    const lang = getLanguage(STATE.detailViewSource);
    const isMarkdown = (lang === 'markdown' || (STATE.detailViewSource || '').endsWith('.md'));
    if (isMarkdown && typeof marked !== 'undefined') {
      codeView.className = 'detail-code-view md-preview';
      codeView.innerHTML = DOMPurify.sanitize(marked.parse(content));
    } else if (lang && lang !== 'markdown' && window.Prism && Prism.languages[lang]) {
      codeView.className = 'detail-code-view';
      codeView.innerHTML = `<pre><code class="language-${lang}">${Prism.highlight(content, Prism.languages[lang], lang)}</code></pre>`;
    } else {
      codeView.className = 'detail-code-view';
      codeView.innerHTML = `<pre>${escapeHtml(content)}</pre>`;
    }
    hide(editor);
    show(codeView);
    btn.textContent = 'Edit';
    btn.dataset.mode = 'view';
    // Hide edit-only actions in view mode
    hide(qs('d-save-btn'));
  }
}

// ── B: Resizable results panel divider ──
(function initResizeDivider() {
  const divider = qs('results-divider');
  if (!divider) return;
  const layout = document.querySelector('.results-layout');
  const panel = qs('results-panel');
  let startX = 0, startW = 0;

  divider.addEventListener('mousedown', e => {
    e.preventDefault();
    startX = e.clientX;
    startW = panel.offsetWidth;
    divider.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  function onMove(e) {
    const newW = Math.max(250, Math.min(startW + (e.clientX - startX), window.innerWidth * 0.6));
    layout.style.setProperty('--results-width', newW + 'px');
  }

  function onUp() {
    divider.classList.remove('dragging');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }
})();

// ── B2: Resizable sources sidebar divider ──
(function initSourcesDivider() {
  const divider = qs('sources-divider');
  if (!divider) return;
  const layout = document.querySelector('.sources-layout');
  const sidebar = document.querySelector('.sources-sidebar');
  let startX = 0, startW = 0;

  divider.addEventListener('mousedown', e => {
    e.preventDefault();
    startX = e.clientX;
    startW = sidebar.offsetWidth;
    divider.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  function onMove(e) {
    const newW = Math.max(200, Math.min(startW + (e.clientX - startX), window.innerWidth * 0.6));
    layout.style.setProperty('--sources-width', newW + 'px');
  }

  function onUp() {
    divider.classList.remove('dragging');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }
})();

// ── D2: Diff toggle ──
qs('d-diff-btn').addEventListener('click', () => {
  const btn = qs('d-diff-btn');
  const isShowing = btn.dataset.mode === 'diff';
  if (isShowing) {
    hide(qs('d-diff'));
    show(qs('d-editor'));
    btn.textContent = 'Diff';
    btn.dataset.mode = 'source';
  } else {
    const ops = diffLines(STATE.selectedOriginal, qs('d-editor').value);
    qs('d-diff').innerHTML = renderDiff(ops);
    hide(qs('d-editor'));
    show(qs('d-diff'));
    btn.textContent = 'Edit';
    btn.dataset.mode = 'diff';
  }
});

qs('d-editor').addEventListener('input', () => {
  const diffBtn = qs('d-diff-btn');
  const changed = qs('d-editor').value !== STATE.selectedOriginal;
  if (changed) {
    show(diffBtn);
  } else {
    hide(diffBtn);
    if (diffBtn.dataset.mode === 'diff') {
      hide(qs('d-diff'));
      show(qs('d-editor'));
      diffBtn.textContent = 'Diff';
      diffBtn.dataset.mode = 'source';
    }
  }
});

// ── D1: Bulk select ──
qs('bulk-select-all').addEventListener('change', () => {
  const checked = qs('bulk-select-all').checked;
  const checkboxes = document.querySelectorAll('.result-checkbox');
  checkboxes.forEach(cb => {
    cb.checked = checked;
    if (checked) STATE.selectedIds.add(cb.dataset.id);
    else STATE.selectedIds.delete(cb.dataset.id);
  });
  updateBulkToolbar(checkboxes.length);
});

qs('bulk-delete-btn').addEventListener('click', async () => {
  const ids = [...STATE.selectedIds];
  if (!ids.length) return;
  const ok = await showConfirm({
    title: `Delete ${ids.length} Chunk${ids.length > 1 ? 's' : ''}`,
    message: `${ids.length} chunk${ids.length > 1 ? 's' : ''} will be permanently removed from source files and deleted from the index. This cannot be undone.`,
    confirmText: 'Delete',
  });
  if (!ok) return;
  const btn = qs('bulk-delete-btn');
  btnLoading(btn, true);
  let deleted = 0, failed = 0;
  for (const id of ids) {
    try { await api('DELETE', `/api/chunks/${id}`); deleted++; }
    catch (_) { failed++; }
  }
  btnLoading(btn, false);
  const msg = failed
    ? `${deleted} deleted, ${failed} failed`
    : `${deleted} chunk${deleted > 1 ? 's' : ''} deleted`;
  showToast(msg, failed ? 'error' : 'success');
  STATE.selectedIds.clear();
  updateBulkToolbar(0);
  clearDetail();
  _markDataStale();
  doSearch();
  loadStats();
});

// ---------------------------------------------------------------------------
// Tag Editor
// ---------------------------------------------------------------------------

let currentTags = [];

function renderTagChips(tags) {
  currentTags = [...tags];
  const container = qs('d-tag-chips');
  container.innerHTML = '';
  if (!tags.length) {
    container.innerHTML = '<span class="tag-empty-hint">No tags — type below to add</span>';
    return;
  }
  currentTags.forEach((tag, idx) => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.style.cursor = 'pointer';
    chip.innerHTML = `${escapeHtml(tag)}<button class="tag-chip-remove" data-idx="${idx}" title="Remove tag">✕</button>`;
    chip.querySelector('.tag-chip-remove').addEventListener('click', async () => {
      if (!STATE.selectedChunkId) return;
      currentTags.splice(idx, 1);
      renderTagChips(currentTags);
      _syncResultTags(STATE.selectedChunkId, [...currentTags]);
      STATE.tagsTabStale = true;
      if (qs('tag-filter').value === tag) qs('tag-filter').value = '';
      try {
        await api('PATCH', `/api/chunks/${STATE.selectedChunkId}/tags`, { tags: [...currentTags] });
      } catch (err) {
        showToast('Failed to remove tag: ' + err.message, 'error');
      }
    });
    chip.addEventListener('click', e => {
      if (e.target.closest('.tag-chip-remove')) return;
      qs('tag-filter').value = tag;
      doSearch();
    });
    container.appendChild(chip);
  });
}

function addTagFromInput() {
  const input = qs('d-tag-input');
  const val = input.value.trim();
  if (!val) return;
  if (!currentTags.includes(val)) {
    currentTags.push(val);
    renderTagChips(currentTags);
  }
  input.value = '';
}

qs('d-tag-add-btn').addEventListener('click', addTagFromInput);
qs('d-tag-input').addEventListener('keydown', e => { if (e.key === 'Enter') addTagFromInput(); });

qs('d-tag-save-btn').addEventListener('click', async () => {
  if (!STATE.selectedChunkId) return;
  const btn = qs('d-tag-save-btn');
  btnLoading(btn, true);
  try {
    const data = await api('PATCH', `/api/chunks/${STATE.selectedChunkId}/tags`, { tags: currentTags });
    renderTagChips(data.tags);
    _syncResultTags(STATE.selectedChunkId, data.tags);
    STATE.tagsTabStale = true;
    showToast('Tags saved.', 'success');
  } catch (err) {
    showToast('Failed to save tags: ' + err.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
});

// ---------------------------------------------------------------------------
// Sources (D: filter + directory tree view)
// ---------------------------------------------------------------------------

// STATE.allSources, STATE.sourcesSortBy now in STATE

function sortSources(sources) {
  const sorted = [...sources];
  switch (STATE.sourcesSortBy) {
    case 'chunks':
      sorted.sort((a, b) => (b.chunk_count || 0) - (a.chunk_count || 0));
      break;
    case 'size':
      sorted.sort((a, b) => (b.file_size || 0) - (a.file_size || 0));
      break;
    case 'recent':
      sorted.sort((a, b) => {
        const ta = a.last_indexed_at ? new Date(a.last_indexed_at).getTime() : 0;
        const tb = b.last_indexed_at ? new Date(b.last_indexed_at).getTime() : 0;
        return tb - ta;
      });
      break;
    default: // name
      sorted.sort((a, b) => a.path.localeCompare(b.path));
  }
  return sorted;
}

function _getFilteredSorted() {
  const q = qs('sources-filter').value.trim().toLowerCase();
  let filtered = q ? STATE.allSources.filter(s => s.path.toLowerCase().includes(q)) : STATE.allSources;
  if (STATE.sourcesNsFilter) {
    filtered = filtered.filter(s => (s.namespaces || []).includes(STATE.sourcesNsFilter));
  }
  return sortSources(filtered);
}

function _renderSourcesNsChip() {
  const chip = document.getElementById('sources-ns-chip');
  if (!chip) return;
  if (STATE.sourcesNsFilter) {
    chip.innerHTML = `<span class="sources-ns-chip">ns: ${escapeHtml(STATE.sourcesNsFilter)} <button class="sources-ns-chip-clear" title="Clear filter">\u2715</button></span>`;
    chip.querySelector('.sources-ns-chip-clear').addEventListener('click', () => {
      STATE.sourcesNsFilter = '';
      _renderSourcesNsChip();
      renderSourceTree(_getFilteredSorted());
    });
    chip.hidden = false;
  } else {
    chip.innerHTML = '';
    chip.hidden = true;
  }
}

function navigateToSourcesByNs(nsName) {
  STATE.sourcesNsFilter = nsName;
  activateTab('sources');
}

qs('refresh-sources-btn').addEventListener('click', loadSources);
qs('sources-filter').addEventListener('input', () => renderSourceTree(_getFilteredSorted()));

document.querySelectorAll('.sources-sort-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.sources-sort-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    STATE.sourcesSortBy = btn.dataset.sort;
    renderSourceTree(_getFilteredSorted());
  });
});

async function loadSources() {
  const list = qs('sources-list');
  panelLoading(list);
  try {
    const data = await api('GET', '/api/sources');
    STATE.allSources = data.sources;
    _renderSourcesNsChip();
    renderSourceTree(_getFilteredSorted());
  } catch (err) {
    list.innerHTML = `<div class="empty-state"><p>Error: ${escapeHtml(err.message)}</p></div>`;
  }
}

function renderSourceTree(sources) {
  const list = qs('sources-list');

  // C. Summary stats bar
  const statsEl = qs('sources-stats');
  if (sources.length) {
    const totalChunks = sources.reduce((sum, s) => sum + (s.chunk_count || 0), 0);
    const totalSize = sources.reduce((sum, s) => sum + (s.file_size || 0), 0);
    statsEl.textContent = `${sources.length} files \u00b7 ${totalChunks.toLocaleString()} chunks \u00b7 ${formatBytes(totalSize)}`;
    statsEl.hidden = false;
  } else {
    statsEl.hidden = true;
  }

  if (!sources.length) {
    list.innerHTML = '<div class="empty-state">' + emptyState('📁', 'No indexed sources', 'Index files from the Index tab') + '</div>';
    return;
  }
  list.innerHTML = '';

  // G. Max chunks for mini bar
  const maxChunks = Math.max(1, ...sources.map(s => s.chunk_count || 0));

  // Group by parent directory
  const groups = {};
  sources.forEach(s => {
    const parts = s.path.split('/');
    const dir = parts.slice(0, -1).join('/') || '/';
    if (!groups[dir]) groups[dir] = [];
    groups[dir].push(s);
  });

  Object.entries(groups).forEach(([dir, items]) => {
    const group = document.createElement('div');
    group.className = 'source-group';

    // D. Collapsible group header
    const header = document.createElement('div');
    header.className = 'source-group-header';
    header.title = dir;
    header.innerHTML = `<span class="source-group-chevron">\u25BC</span><span class="source-group-dir">${escapeHtml(shortDir(dir))}</span><span class="source-group-count">${items.length}</span>`;
    header.setAttribute('aria-expanded', 'true');
    header.addEventListener('click', () => {
      group.classList.toggle('collapsed');
      header.setAttribute('aria-expanded', !group.classList.contains('collapsed'));
    });
    group.appendChild(header);

    items.forEach(s => {
      const filename = s.path.split('/').pop() || s.path;
      const item = document.createElement('div');
      item.className = 'source-item';
      item.title = s.path;

      // A+B. 2-row layout with color dot
      const size = s.file_size != null ? formatBytes(s.file_size) : '';
      const age = s.last_indexed_at ? relativeTime(s.last_indexed_at) : '';
      const barPct = Math.round(((s.chunk_count || 0) / maxChunks) * 100);

      // F. Namespace badges (exclude "default")
      const nsBadges = (s.namespaces || [])
        .filter(ns => ns !== 'default')
        .map(ns => `<span class="badge badge-ns source-ns-badge">${escapeHtml(ns)}</span>`)
        .join('');

      item.innerHTML = `
        <div class="source-item-row1">
          <span class="source-type-dot" style="background:${fileTypeColor(s.path)}"></span>
          <span class="source-name">${escapeHtml(filename)}</span>
          ${nsBadges}
          <button class="source-del-btn remove-btn" data-path="${escapeAttr(s.path)}">✕</button>
        </div>
        <div class="source-item-row2">
          ${s.chunk_count ?? '?'} chunks${size ? ' \u00b7 ' + size : ''}${s.avg_tokens ? ' \u00b7 avg ' + s.avg_tokens + ' tok' : ''}${age ? ' \u00b7 ' + age : ''}
        </div>
        <div class="source-chunk-bar">
          <div class="source-chunk-bar-fill" style="width:${barPct}%"></div>
        </div>
      `;
      item.setAttribute('tabindex', '0');
      item.addEventListener('click', (e) => {
        if (e.target.classList.contains('remove-btn')) return;
        document.querySelectorAll('.source-item').forEach(el => el.classList.remove('active'));
        item.classList.add('active');
        browseSource(s.path);
      });
      item.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); item.click(); }
      });
      item.querySelector('.remove-btn').addEventListener('click', async (e) => {
        e.stopPropagation();
        const ok = await showConfirm({
          title: 'Delete Source',
          message: `Delete all chunks for:\n${s.path}`,
          confirmText: 'Delete',
        });
        if (!ok) return;
        try {
          await api('DELETE', `/api/sources?path=${encodeURIComponent(s.path)}`);
          showToast('Source deleted.', 'success');
          STATE.allSources = STATE.allSources.filter(x => x.path !== s.path);
          renderSourceTree(_getFilteredSorted());
          hideBrowser();
          STATE.lastResults = STATE.lastResults.filter(r => r.chunk.source_file !== s.path);
          renderResults(STATE.lastResults);
          _markDataStale();
          loadSourceFilter();
          loadStats();
        } catch (err) {
          showToast('Delete failed: ' + err.message, 'error');
        }
      });
      group.appendChild(item);
    });

    list.appendChild(group);
  });
}

function hideBrowser() {
  hide(qs('chunks-browser-content'));
  const browser = qs('chunks-browser');
  browser.innerHTML = '<div class="empty-state">' + emptyState('📄', 'Select a source to browse its chunks') + '</div>';
}

async function browseSource(path, limit = 100) {
  const browser = qs('chunks-browser');
  panelLoading(browser);
  try {
    const data = await api('GET', `/api/chunks?source=${encodeURIComponent(path)}&limit=${limit}`);
    browser.innerHTML = '';
    const content = document.createElement('div');
    content.id = 'chunks-browser-content';

    const header = document.createElement('div');
    header.className = 'chunks-browser-header';
    header.innerHTML = `
      <span class="file-path">${escapeHtml(path)}</span>
      <span class="badge badge-blue">${data.total} chunks</span>
      <span class="chunks-browser-info">${data.chunks.length} of ${data.total} shown</span>
    `;
    if (data.chunks.length < data.total) {
      const loadAllBtn = document.createElement('button');
      loadAllBtn.className = 'btn-ghost btn-xs chunks-load-all-btn';
      loadAllBtn.textContent = 'Load All';
      loadAllBtn.addEventListener('click', () => browseSource(path, 500));
      header.appendChild(loadAllBtn);
    }
    // View mode toggle: Chunks | Document
    const viewToggle = document.createElement('div');
    viewToggle.className = 'view-mode-toggle';
    const chunksBtn = document.createElement('button');
    chunksBtn.className = 'view-mode-btn active';
    chunksBtn.textContent = 'Chunks';
    const docBtn = document.createElement('button');
    docBtn.className = 'view-mode-btn';
    docBtn.textContent = 'Document';
    viewToggle.appendChild(chunksBtn);
    viewToggle.appendChild(docBtn);
    header.appendChild(viewToggle);
    content.appendChild(header);

    if (!data.chunks.length) {
      content.innerHTML += '<div class="empty-state" style="height:80px"><p>No chunks found</p></div>';
    } else {
      // Document view container (hidden initially)
      const docView = document.createElement('div');
      docView.className = 'document-view';
      docView.hidden = true;
      _renderDocumentView(data.chunks, docView, path);
      content.appendChild(docView);

      // Toggle handlers
      chunksBtn.addEventListener('click', () => {
        chunksBtn.classList.add('active'); docBtn.classList.remove('active');
        chunkList.hidden = false; docView.hidden = true;
      });
      docBtn.addEventListener('click', () => {
        docBtn.classList.add('active'); chunksBtn.classList.remove('active');
        chunkList.hidden = true; docView.hidden = false;
      });

      const chunkList = document.createElement('div');
      const lang = getLanguage(path);
      const cardPairs = [];  // [card, contentDiv] — accordion 활성화는 DOM 삽입 후 일괄 처리
      data.chunks.forEach(c => {
        const card = document.createElement('div');
        card.className = 'chunk-card';
        card.dataset.chunkId = c.id;
        card.innerHTML = `
          <div class="chunk-card-meta">
            <span class="badge badge-gray">${c.chunk_type.replace('_',' ')}</span>
            <span class="chunk-card-lines">lines ${c.start_line}–${c.end_line}</span>
            ${c.heading_hierarchy.length ? `<span class="hierarchy-trail">${escapeHtml(c.heading_hierarchy.join(' › '))}</span>` : ''}
            <div class="chunk-card-actions">
              <button class="btn-ghost btn-xs card-copy-btn" title="Copy content">Copy</button>
              <button class="btn-ghost btn-xs card-edit-btn" title="Edit chunk">Edit</button>
              <button class="btn-danger btn-xs card-delete-btn" title="Delete chunk">Delete</button>
            </div>
          </div>
        `;
        const contentDiv = document.createElement('div');
        contentDiv.className = 'chunk-card-content';
        if (lang && lang !== 'markdown' && window.Prism) {
          const pre = document.createElement('pre');
          const code = document.createElement('code');
          code.className = `language-${lang}`;
          code.textContent = c.content;
          pre.appendChild(code);
          contentDiv.appendChild(pre);
          Prism.highlightElement(code);
        } else {
          contentDiv.textContent = c.content;
        }
        card.appendChild(contentDiv);

        // Copy
        card.querySelector('.card-copy-btn').addEventListener('click', e => {
          e.stopPropagation();
          copyToClipboard(c.content);
        });

        // Edit
        card.querySelector('.card-edit-btn').addEventListener('click', e => {
          e.stopPropagation();
          _startChunkEdit(card, c, path);
        });

        // Delete
        card.querySelector('.card-delete-btn').addEventListener('click', async e => {
          e.stopPropagation();
          const ok = await showConfirm({
            title: 'Delete Chunk',
            message: `Delete this chunk (lines ${c.start_line}–${c.end_line})`,
            confirmText: 'Delete',
          });
          if (!ok) return;
          try {
            await api('DELETE', `/api/chunks/${c.id}`);
            card.remove();
            showToast('Chunk deleted.', 'success');
            // Update count badge
            const countEl = content.querySelector('.badge-blue');
            const remaining = content.querySelectorAll('.chunk-card').length;
            if (countEl) countEl.textContent = `${remaining} chunks`;
            STATE.lastResults = STATE.lastResults.filter(r => String(r.chunk.id) !== String(c.id));
            renderResults(STATE.lastResults);
            _markDataStale();
            loadStats();
          } catch (err) {
            showToast('Delete failed: ' + err.message, 'error');
          }
        });

        chunkList.appendChild(card);
        cardPairs.push([card, contentDiv]);
      });
      content.appendChild(chunkList);

      // 모든 카드가 DOM에 삽입된 뒤 단일 rAF로 accordion 활성화
      requestAnimationFrame(() => {
        cardPairs.forEach(([card, contentDiv]) => {
          if (contentDiv.scrollHeight > 120) {
            card.classList.add('chunk-card-collapsible');
            card.setAttribute('aria-expanded', 'false');
            let dragStartX = 0, dragStartY = 0;
            card.addEventListener('mousedown', e => { dragStartX = e.clientX; dragStartY = e.clientY; });
            card.addEventListener('click', e => {
              if (Math.abs(e.clientX - dragStartX) > 4 || Math.abs(e.clientY - dragStartY) > 4) return;
              if (e.target.closest('.chunk-card-edit-area')) return;
              contentDiv.classList.toggle('expanded');
              card.setAttribute('aria-expanded', contentDiv.classList.contains('expanded'));
            });
          }
        });
      });
    }
    browser.appendChild(content);
  } catch (err) {
    browser.innerHTML = `<div class="empty-state"><p>Error: ${escapeHtml(err.message)}</p></div>`;
  }
}

function _renderDocumentView(chunks, container, path) {
  const sorted = [...chunks].sort((a, b) => (a.start_line || 0) - (b.start_line || 0));
  const fullText = sorted.map(c => c.content).join('\n');
  const lang = getLanguage(path);

  // Copy All button
  const copyBtn = document.createElement('button');
  copyBtn.className = 'btn-ghost btn-xs document-copy-btn';
  copyBtn.textContent = 'Copy All';
  copyBtn.addEventListener('click', e => {
    e.stopPropagation();
    copyToClipboard(fullText);
  });
  container.appendChild(copyBtn);

  const contentDiv = document.createElement('div');
  contentDiv.className = 'document-content';

  // Render each chunk as a hoverable editable block (code or markdown/plain)
  sorted.forEach(c => {
    const block = document.createElement('div');
    block.className = 'doc-chunk-block';

    if (lang && lang !== 'markdown' && window.Prism) {
      const pre = document.createElement('pre');
      pre.style.margin = '0';
      const code = document.createElement('code');
      code.className = `language-${lang}`;
      code.textContent = c.content;
      pre.appendChild(code);
      block.appendChild(pre);
      Prism.highlightElement(code);
    } else {
      block.textContent = c.content;
    }

    const editBtn = document.createElement('button');
    editBtn.className = 'btn-ghost btn-xs doc-chunk-edit-btn';
    editBtn.textContent = 'Edit';
    editBtn.title = `lines ${c.start_line}–${c.end_line}`;
    editBtn.addEventListener('click', e => {
      e.stopPropagation();
      _startChunkEdit(block, c, path);
    });
    block.appendChild(editBtn);
    contentDiv.appendChild(block);
  });

  container.appendChild(contentDiv);
}

function _startChunkEdit(card, chunk, sourcePath) {
  // Prevent duplicate edit areas
  if (card.querySelector('.chunk-card-edit-area')) return;
  const editArea = document.createElement('div');
  editArea.className = 'chunk-card-edit-area';
  const ta = document.createElement('textarea');
  ta.value = chunk.content;
  editArea.appendChild(ta);

  const actionsDiv = document.createElement('div');
  actionsDiv.className = 'chunk-card-edit-actions';
  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn-primary btn-xs';
  saveBtn.textContent = 'Save';
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn-ghost btn-xs';
  cancelBtn.textContent = 'Cancel';
  actionsDiv.appendChild(saveBtn);
  actionsDiv.appendChild(cancelBtn);
  editArea.appendChild(actionsDiv);
  card.appendChild(editArea);

  ta.focus();

  cancelBtn.addEventListener('click', e => {
    e.stopPropagation();
    editArea.remove();
  });

  saveBtn.addEventListener('click', async e => {
    e.stopPropagation();
    const newContent = ta.value;
    if (newContent === chunk.content) { editArea.remove(); return; }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      await api('PATCH', `/api/chunks/${chunk.id}`, { new_content: newContent });
      showToast('Chunk updated.', 'success');
      _syncResultContent(chunk.id, newContent);
      _markDataStale();
      // Refresh the browser to show updated content
      browseSource(sourcePath, card.closest('#chunks-browser-content')?.querySelectorAll('.chunk-card').length > 100 ? 500 : 100);
    } catch (err) {
      showToast('Update failed: ' + err.message, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
}

// ---------------------------------------------------------------------------
// Index
// ---------------------------------------------------------------------------

qs('index-btn').addEventListener('click', async () => {
  const path    = qs('index-path').value.trim();
  if (!path) { setMsg(qs('index-msg'), 'Please enter a path to index.', true); return; }
  const recursive = qs('index-recursive').checked;
  const force   = qs('index-force').checked;
  const namespace = qs('index-namespace').value.trim() || null;
  const btn     = qs('index-btn');
  const msg     = qs('index-msg');
  const result  = qs('index-result');

  btnLoading(btn, true);
  hide(msg); hide(result);

  try {
    const data = await api('POST', '/api/index', { path, recursive, force, namespace });
    showToast(`Indexed ${data.indexed_chunks} chunks`, 'success');
    qs('r-files').textContent    = data.total_files;
    qs('r-chunks').textContent   = data.total_chunks;
    qs('r-indexed').textContent  = data.indexed_chunks;
    qs('r-skipped').textContent  = data.skipped_chunks;
    qs('r-deleted').textContent  = data.deleted_chunks;
    qs('r-duration').textContent = `${data.duration_ms.toFixed(0)} ms`;
    show(result);
    _markDataStale();
    loadNamespaceDropdowns();
    loadSourceFilter();
  } catch (err) {
    showToast('Indexing failed: ' + err.message, 'error');
  } finally {
    btnLoading(btn, false);
    loadStats();
  }
});

// ---------------------------------------------------------------------------
// Add Memory
// ---------------------------------------------------------------------------

qs('add-btn').addEventListener('click', async () => {
  const content = qs('add-content').value.trim();
  if (!content) { setMsg(qs('add-msg'), 'Content is required.', true); return; }

  const title = qs('add-title').value.trim() || null;
  const tagsRaw = qs('add-tags').value.trim();
  const tags = tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [];
  const file = qs('add-file').value.trim() || null;
  const namespace = qs('add-namespace').value.trim() || null;

  const btn = qs('add-btn');
  btnLoading(btn, true);
  hide(qs('add-msg'));

  try {
    const data = await api('POST', '/api/add', { content, title, tags, file, namespace });
    const n = data.indexed_chunks;
    showToast(`Saved — ${n} chunks indexed`, 'success');
    qs('add-content').value = '';
    _markDataStale();
    loadStats();
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
});

// ---------------------------------------------------------------------------
// File Upload
// ---------------------------------------------------------------------------

(function initUpload() {
  const drop     = qs('upload-drop');
  const input    = qs('upload-input');
  const list     = qs('upload-file-list');
  const btn      = qs('upload-btn');
  const msg      = qs('upload-msg');
  const result   = qs('upload-result');
  let selectedFiles = [];

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function renderFileList() {
    if (!selectedFiles.length) { hide(list); btn.disabled = true; return; }
    show(list);
    btn.disabled = false;
    list.innerHTML = '';
    selectedFiles.forEach((f, i) => {
      const row = document.createElement('div');
      row.className = 'upload-file-item';
      row.innerHTML = `
        <span class="upload-file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
        <span class="upload-file-size">${fmtSize(f.size)}</span>
        <button class="upload-file-remove" data-i="${i}" title="Remove">✕</button>
      `;
      list.appendChild(row);
    });
    list.querySelectorAll('.upload-file-remove').forEach(b => {
      b.addEventListener('click', () => {
        selectedFiles.splice(Number(b.dataset.i), 1);
        renderFileList();
      });
    });
  }

  function addFiles(files) {
    for (const f of files) {
      if (!selectedFiles.find(x => x.name === f.name && x.size === f.size)) {
        selectedFiles.push(f);
      }
    }
    renderFileList();
  }

  // Click on drop zone opens file picker
  drop.addEventListener('click', () => input.click());
  input.addEventListener('change', () => { addFiles(Array.from(input.files)); input.value = ''; });

  // Drag & drop
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('drag-over');
    addFiles(Array.from(e.dataTransfer.files));
  });

  btn.addEventListener('click', async () => {
    if (!selectedFiles.length) return;
    btnLoading(btn, true);
    hide(msg); hide(result);

    const form = new FormData();
    selectedFiles.forEach(f => form.append('files', f));

    try {
      const res = await fetch('/api/upload', { method: 'POST', body: form });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || res.statusText);
      }
      const data = await res.json();
      // Render per-file results
      show(result);
      result.innerHTML = '';
      data.files.forEach(r => {
        const row = document.createElement('div');
        row.className = 'upload-result-row';
        if (r.error) {
          row.innerHTML = `<span class="upload-result-err">✗</span><span>${escapeHtml(r.filename)}: ${escapeHtml(r.error)}</span>`;
        } else {
          row.innerHTML = `<span class="upload-result-ok">✓</span><span>${escapeHtml(r.filename)} — ${r.indexed_chunks} chunk${r.indexed_chunks !== 1 ? 's' : ''}</span>`;
        }
        result.appendChild(row);
      });
      showToast(`Upload complete — ${data.total_indexed} chunks indexed`, 'success');
      selectedFiles = [];
      renderFileList();
      _markDataStale();
      loadSourceFilter();
      loadStats();
    } catch (err) {
      showToast('Upload failed: ' + err.message, 'error');
    } finally {
      btnLoading(btn, false);
    }
  });
})();

// ---------------------------------------------------------------------------
// Dedup
// ---------------------------------------------------------------------------

qs('dedup-scan-btn').addEventListener('click', runDedupScan);

// STATE.dedupScanActive, STATE.dedupAbortCtrl now in STATE

function resetDedupPanel() {
  // Don't reset while a scan is still running — keep the UI consistent
  if (STATE.dedupScanActive) return;
  hide(qs('dedup-list'));
  const empty = qs('dedup-empty');
  empty.innerHTML = emptyState('📋', 'Run Scan to see duplicate candidates');
  show(empty);
}

async function runDedupScan() {
  const threshold = parseFloat(qs('dedup-threshold').value);
  const limit     = parseInt(qs('dedup-limit').value, 10);
  const maxScan   = parseInt(qs('dedup-max-scan').value, 10);
  const btn       = qs('dedup-scan-btn');
  const empty     = qs('dedup-empty');

  STATE.dedupScanActive = true;
  btnLoading(btn, true);
  hide(qs('dedup-list'));
  hide(qs('dedup-msg'));
  show(empty);
  empty.innerHTML = '<div class="spinner-panel"></div>';

  // Abort any previous request and set a 30 s timeout
  if (STATE.dedupAbortCtrl) STATE.dedupAbortCtrl.abort();
  STATE.dedupAbortCtrl = new AbortController();
  const timeoutId = setTimeout(() => STATE.dedupAbortCtrl.abort(), 30_000);

  try {
    const params = new URLSearchParams({ threshold, limit, max_scan: maxScan });
    const res = await fetch(`/api/dedup/candidates?${params}`, { signal: STATE.dedupAbortCtrl.signal });
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    renderDedupCandidates(data.candidates, threshold);
  } catch (err) {
    const msg = err.name === 'AbortError' ? 'Scan timed out (30 s). Try a smaller Max Scan.' : err.message;
    setMsg(qs('dedup-msg'), 'Scan error: ' + msg, true);
    empty.innerHTML = emptyState('📋', 'Scan failed');
  } finally {
    clearTimeout(timeoutId);
    STATE.dedupScanActive = false;
    btnLoading(btn, false);
  }
}

function renderDedupCandidates(candidates, threshold) {
  const list  = qs('dedup-list');
  const empty = qs('dedup-empty');

  if (!candidates.length) {
    hide(list);
    empty.innerHTML = emptyState('📋', `No duplicates found (threshold=${threshold})`);
    show(empty);
    return;
  }

  hide(empty);
  show(list);
  list.innerHTML = `<div class="dedup-summary">Found <strong>${candidates.length}</strong> candidate pair${candidates.length !== 1 ? 's' : ''}. Review each and choose which chunk to keep.</div>`;

  candidates.forEach((c, i) => {
    const row = document.createElement('div');
    row.className = 'dedup-row';

    const badge = c.exact
      ? `<span class="badge badge-danger">Exact</span>`
      : `<span class="badge badge-warn">~${c.score.toFixed(3)}</span>`;

    row.innerHTML = `
      <div class="dedup-row-header">
        <span class="dedup-index">#${i + 1}</span>
        ${badge}
        <div class="dedup-actions">
          <button class="btn-primary keep-a-btn" title="Keep A, delete B">Keep A</button>
          <button class="btn-ghost keep-b-btn" title="Keep B, delete A">Keep B</button>
          <button class="btn-ghost skip-btn">Skip</button>
        </div>
      </div>
      <div class="dedup-chunks">
        <div class="dedup-chunk">
          <div class="dedup-chunk-label">A — keep candidate</div>
          <div class="dedup-chunk-meta">
            <span class="file-path">${escapeHtml(c.chunk_a.source_file)}</span>
            <span class="lines-info">lines ${c.chunk_a.start_line}–${c.chunk_a.end_line}</span>
          </div>
          <div class="dedup-chunk-content">${escapeHtml(truncate(c.chunk_a.content, 240))}</div>
        </div>
        <div class="dedup-chunk">
          <div class="dedup-chunk-label">B — duplicate candidate</div>
          <div class="dedup-chunk-meta">
            <span class="file-path">${escapeHtml(c.chunk_b.source_file)}</span>
            <span class="lines-info">lines ${c.chunk_b.start_line}–${c.chunk_b.end_line}</span>
          </div>
          <div class="dedup-chunk-content">${escapeHtml(truncate(c.chunk_b.content, 240))}</div>
        </div>
      </div>
      <div class="dedup-row-msg status-msg" hidden></div>
    `;

    row.querySelector('.keep-a-btn').addEventListener('click', async () => {
      const ok = await showConfirm({ title: 'Merge Duplicate', message: 'Keep A and delete B.', confirmText: 'Merge' });
      if (ok) doMerge(row, c.chunk_a.id, [c.chunk_b.id]);
    });
    row.querySelector('.keep-b-btn').addEventListener('click', async () => {
      const ok = await showConfirm({ title: 'Merge Duplicate', message: 'Keep B and delete A.', confirmText: 'Merge' });
      if (ok) doMerge(row, c.chunk_b.id, [c.chunk_a.id]);
    });
    row.querySelector('.skip-btn').addEventListener('click', () => row.remove());

    list.appendChild(row);
  });
}

async function doMerge(rowEl, keepId, deleteIds) {
  const btns = rowEl.querySelectorAll('button');
  btns.forEach(b => { b.disabled = true; });

  try {
    await api('POST', '/api/dedup/merge', { keep_id: keepId, delete_ids: deleteIds });
    showToast('Duplicate chunks merged.', 'success');
    rowEl.style.opacity = '0.45';
    // Bug #5: remove deleted chunks from search results
    STATE.lastResults = STATE.lastResults.filter(r => !deleteIds.includes(String(r.chunk.id)));
    renderResults(STATE.lastResults);
    _markDataStale();
    // Bug #12: update dedup summary count
    const summaryEl = qs('dedup-list')?.querySelector('.dedup-summary strong');
    if (summaryEl) {
      const remaining = qs('dedup-list').querySelectorAll('.dedup-row').length - 1;
      summaryEl.textContent = Math.max(0, remaining);
    }
    loadStats();
  } catch (err) {
    showToast('Merge failed: ' + err.message, 'error');
    btns.forEach(b => { b.disabled = false; });
  }
}

// ---------------------------------------------------------------------------
// Decay tab
// ---------------------------------------------------------------------------

function resetDecayPanel() {
  hide(qs('decay-result'));
  hide(qs('decay-msg'));
  qs('decay-expire-btn').disabled = true;

  // Sync defaults from config
  const cfg = STATE.serverConfig?.decay;
  if (cfg) {
    if (cfg.half_life_days) qs('decay-max-age').value = cfg.half_life_days;
  }
}

async function runDecayScan() {
  const maxAge = parseFloat(qs('decay-max-age').value) || 90;
  const srcFilter = qs('decay-source-filter').value.trim();
  const params = new URLSearchParams({ max_age_days: maxAge });
  if (srcFilter) params.set('source_filter', srcFilter);

  const scanBtn = qs('decay-scan-btn');
  btnLoading(scanBtn, true);
  try {
    const data = await api('GET', `/api/decay/scan?${params}`);
    qs('decay-r-total').textContent   = data.total_chunks;
    qs('decay-r-expired').textContent = data.expired_chunks;
    qs('decay-r-deleted').textContent = '—';
    show(qs('decay-result'));
    qs('decay-expire-btn').disabled = data.expired_chunks === 0;
    if (data.expired_chunks === 0) {
      setMsg(qs('decay-msg'), 'No chunks to expire.', false);
    }
  } catch (err) {
    setMsg(qs('decay-msg'), 'Scan failed: ' + err.message, true);
  } finally {
    btnLoading(scanBtn, false);
  }
}

async function runDecayExpire() {
  const ok = await showConfirm({ title: 'Expire Chunks', message: 'Permanently delete expired chunks. This action cannot be undone.', confirmText: 'Expire' });
  if (!ok) return;
  const maxAge = parseFloat(qs('decay-max-age').value) || 90;
  const srcFilter = qs('decay-source-filter').value.trim() || null;

  const expireBtn = qs('decay-expire-btn');
  btnLoading(expireBtn, true);
  try {
    const data = await api('POST', '/api/decay/expire', {
      max_age_days: maxAge,
      source_filter: srcFilter,
      dry_run: false,
    });
    qs('decay-r-total').textContent   = data.total_chunks;
    qs('decay-r-expired').textContent = data.expired_chunks;
    qs('decay-r-deleted').textContent = data.deleted_chunks;
    show(qs('decay-result'));
    showToast(`${data.deleted_chunks} chunks expired and deleted.`, 'success');
    // Bug #6: clear search results since we don't know which chunks were deleted
    if (data.deleted_chunks > 0) {
      STATE.lastResults = [];
      renderResults([]);
      _markDataStale();
    }
    loadStats();
  } catch (err) {
    showToast('Expire failed: ' + err.message, 'error');
    expireBtn.disabled = false;
  } finally {
    btnLoading(expireBtn, false);
  }
}

qs('decay-scan-btn').addEventListener('click', runDecayScan);
qs('decay-expire-btn').addEventListener('click', runDecayExpire);

// ---------------------------------------------------------------------------
// Tags tab
// ---------------------------------------------------------------------------

async function loadTags() {
  const emptyEl = qs('tags-empty');
  const listEl  = qs('tags-list');
  emptyEl.innerHTML = '<div class="spinner-panel"></div>';
  show(emptyEl);
  hide(listEl);
  hide(qs('tags-stats'));

  try {
    const data = await api('GET', '/api/tags');
    listEl.innerHTML = '';

    if (data.tags.length === 0) {
      emptyEl.innerHTML = emptyState('🏷', 'No tags yet', 'Run Auto-Tag to generate tags');
      return;
    }

    STATE.lastTagsData = data.tags;

    // Compute and display stats
    _renderTagStats(data.tags);

    // Render with current filter/sort
    _renderTagViews();

    hide(emptyEl);
    // Show whichever view is active
    if (STATE.tagsView === 'cloud') { show(qs('tags-cloud')); hide(listEl); }
    else { show(listEl); hide(qs('tags-cloud')); }
  } catch (err) {
    emptyEl.innerHTML = emptyState('🏷', 'Failed to load tags: ' + err.message);
  }
}

function _renderTagStats(tags) {
  const statsEl = qs('tags-stats');
  if (!tags.length) { hide(statsEl); return; }
  const total = tags.length;
  const totalChunks = tags.reduce((s, t) => s + t.count, 0);
  const avgPerChunk = totalChunks > 0 ? (totalChunks / Math.max(total, 1)).toFixed(1) : '0';
  const top3 = tags.slice(0, 3).map(t => t.tag);
  statsEl.innerHTML =
    `<span class="tags-stats-label">${total}</span> tags` +
    `<span class="tags-stats-sep">|</span>` +
    `<span class="tags-stats-label">${avgPerChunk}</span> avg uses` +
    `<span class="tags-stats-sep">|</span>` +
    `Top: ${top3.map(t => `<span style="color:${_tagColor(t)}">${escapeHtml(t)}</span>`).join(', ')}`;
  show(statsEl);
}

function sortTags(tags) {
  const sorted = [...tags];
  switch (STATE.tagsSortBy) {
    case 'count-desc': sorted.sort((a, b) => b.count - a.count); break;
    case 'count-asc':  sorted.sort((a, b) => a.count - b.count); break;
    case 'az':         sorted.sort((a, b) => a.tag.localeCompare(b.tag)); break;
    case 'za':         sorted.sort((a, b) => b.tag.localeCompare(a.tag)); break;
  }
  return sorted;
}

function _getFilteredTags() {
  const q = (qs('tags-search').value || '').trim().toLowerCase();
  let tags = STATE.lastTagsData;
  if (q) tags = tags.filter(t => t.tag.toLowerCase().includes(q));
  return sortTags(tags);
}

function _renderTagViews() {
  const tags = _getFilteredTags();
  const maxCount = tags.reduce((m, t) => Math.max(m, t.count), 1);
  const listEl = qs('tags-list');

  // Render list view
  listEl.innerHTML = '';
  tags.forEach(({ tag, count }) => {
    const row = document.createElement('div');
    row.className = 'tag-row';
    const pct = Math.round((count / maxCount) * 100);
    const color = _tagColor(tag);
    row.innerHTML = `
      <span class="tag-name" style="color:${color}">${escapeHtml(tag)}</span>
      <div class="tag-bar-wrap">
        <div class="tag-bar" style="width:${pct}%;background:${color}"></div>
      </div>
      <span class="tag-count">${count}</span>`;
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => _searchByTag(tag));
    listEl.appendChild(row);
  });

  // Render cloud view
  _renderTagCloud(tags, maxCount);
}

// Tag cloud helpers
// STATE.lastTagsData, STATE.tagsView, STATE.tagsSortBy now in STATE

// Deterministic color from tag string (hue rotation, pastel)
function _tagColor(tag) {
  const colors = localStorage.getItem('m2m-tag-colors');
  const map = colors ? JSON.parse(colors) : {};
  if (map[tag]) return map[tag];
  let hash = 0;
  for (let i = 0; i < tag.length; i++) hash = tag.charCodeAt(i) + ((hash << 5) - hash);
  const hue = ((hash % 360) + 360) % 360;
  return `hsl(${hue}, 60%, 65%)`;
}

function _renderTagCloud(tags, maxCount) {
  const cloud = qs('tags-cloud');
  const minSize = 0.75, maxSize = 2.6;
  // Stable sort by count descending (largest first) — no random shuffle
  const stable = [...tags].sort((a, b) => b.count - a.count);
  cloud.innerHTML = stable.map(({ tag, count }) => {
    const ratio = maxCount > 1 ? (count - 1) / (maxCount - 1) : 0;
    const size = minSize + ratio * (maxSize - minSize);
    const color = _tagColor(tag);
    // Deterministic rotation & offset from tag hash
    let h = 0;
    for (let i = 0; i < tag.length; i++) h = tag.charCodeAt(i) + ((h << 5) - h);
    const rot = ((h % 25) - 12).toFixed(1);
    const yOff = ((h >> 4) % 15) - 7;
    const pad = 2 + Math.abs((h >> 8) % 7);
    return `<span class="tag-cloud-item" style="font-size:${size.toFixed(2)}rem;color:${color};transform:rotate(${rot}deg) translateY(${yOff}px);padding:${pad}px ${pad + 4}px"
      title="${escapeAttr(tag)}: ${count} chunks" data-tag="${escapeAttr(tag)}">${escapeHtml(tag)}</span>`;
  }).join('');
  cloud.querySelectorAll('.tag-cloud-item').forEach(el => {
    el.addEventListener('click', () => _searchByTag(el.dataset.tag));
  });
}

function _searchByTag(tag) {
  // Navigate to Search tab with tag filter pre-filled
  document.querySelector('[data-tab="search"]').click();
  qs('search-input').value = tag;
  // Open filters row if hidden
  const filters = document.querySelector('.search-filters');
  if (filters.hidden) qs('filter-toggle').click();
  qs('tag-filter').value = tag;
  doSearch();
}

// View toggle
qs('tags-cloud-btn').addEventListener('click', () => {
  STATE.tagsView = 'cloud';
  qs('tags-cloud-btn').classList.add('btn-active');
  qs('tags-list-btn').classList.remove('btn-active');
  show(qs('tags-cloud')); hide(qs('tags-list'));
});
qs('tags-list-btn').addEventListener('click', () => {
  STATE.tagsView = 'list';
  qs('tags-list-btn').classList.add('btn-active');
  qs('tags-cloud-btn').classList.remove('btn-active');
  show(qs('tags-list')); hide(qs('tags-cloud'));
});

// Tag search/filter
qs('tags-search').addEventListener('input', () => {
  if (STATE.lastTagsData.length) _renderTagViews();
});

// Tag sort controls
document.querySelectorAll('.tags-sort-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    STATE.tagsSortBy = btn.dataset.sort;
    document.querySelectorAll('.tags-sort-btn').forEach(b => b.classList.remove('btn-active'));
    btn.classList.add('btn-active');
    if (STATE.lastTagsData.length) _renderTagViews();
  });
});

async function runAutoTag() {
  const source  = qs('autotag-source').value.trim() || null;
  const maxTags = parseInt(qs('autotag-max').value) || 5;
  const overwrite = qs('autotag-overwrite').checked;
  const dryRun    = qs('autotag-dry-run').checked;

  const autotagBtn = qs('autotag-btn');
  btnLoading(autotagBtn, true);
  hide(qs('autotag-result'));
  try {
    const data = await api('POST', '/api/tags/auto', {
      source_filter: source,
      max_tags: maxTags,
      overwrite,
      dry_run: dryRun,
    });
    qs('at-total').textContent   = data.total_chunks;
    qs('at-tagged').textContent  = data.tagged_chunks;
    qs('at-skipped').textContent = data.skipped_chunks;
    show(qs('autotag-result'));
    const label = dryRun ? '(dry run) ' : '';
    showToast(`${label}${data.tagged_chunks} chunks tagged`, 'success');
    if (!dryRun) { loadTags(); loadStats(); _markDataStale(); }
  } catch (err) {
    showToast('Auto-Tag failed: ' + err.message, 'error');
  } finally {
    btnLoading(autotagBtn, false);
  }
}

// ---------------------------------------------------------------------------
// Index stream (SSE progress)
// ---------------------------------------------------------------------------

async function runIndexStream() {
  const path     = qs('index-path').value.trim();
  if (!path) { setMsg(qs('index-msg'), 'Please enter a path to index.', true); return; }
  const recursive = qs('index-recursive').checked;
  const force     = qs('index-force').checked;

  const progressEl = qs('index-progress');
  const barEl      = qs('index-progress-bar');
  const labelEl    = qs('index-progress-label');
  const fileEl     = qs('index-progress-file');
  const resultEl   = qs('index-result');

  show(progressEl); hide(resultEl); hide(qs('index-msg'));
  barEl.style.width = '0%';
  labelEl.textContent = 'Starting…';
  fileEl.textContent = '';

  btnLoading(qs('index-stream-btn'), true);
  btnLoading(qs('index-btn'), true);

  const params = new URLSearchParams({ path, recursive, force });
  const es = new EventSource(`/api/index/stream?${params}`);

  es.onmessage = (e) => {
    const event = JSON.parse(e.data);
    if (event.type === 'progress') {
      const pct = event.files_total > 0
        ? Math.round((event.files_done / event.files_total) * 100) : 0;
      barEl.style.width = pct + '%';
      labelEl.textContent = `${event.files_done} / ${event.files_total} files`;
      fileEl.textContent  = basename(event.file);
    } else if (event.type === 'complete') {
      es.close();
      barEl.style.width = '100%';
      labelEl.textContent = `Done — ${event.total_files} files`;
      fileEl.textContent  = '';

      qs('r-files').textContent   = event.total_files;
      qs('r-chunks').textContent  = event.total_chunks;
      qs('r-indexed').textContent = event.indexed_chunks;
      qs('r-skipped').textContent = event.skipped_chunks;
      qs('r-deleted').textContent = event.deleted_chunks;
      qs('r-duration').textContent = event.duration_ms.toFixed(0) + ' ms';
      show(resultEl);
      showToast(`Stream indexing complete — ${event.total_files} files`, 'success');
      _markDataStale();
      loadStats();
      loadNamespaceDropdowns();
      loadSourceFilter();
      btnLoading(qs('index-stream-btn'), false);
      btnLoading(qs('index-btn'), false);
    }
  };

  es.onerror = () => {
    es.close();
    showToast('Streaming failed. Try the Index button instead.', 'error');
    hide(progressEl);
    btnLoading(qs('index-stream-btn'), false);
    btnLoading(qs('index-btn'), false);
  };
}

qs('index-stream-btn').addEventListener('click', runIndexStream);

qs('refresh-tags-btn').addEventListener('click', loadTags);
qs('autotag-btn').addEventListener('click', runAutoTag);

// ---------------------------------------------------------------------------
// Export / Import tab
// ---------------------------------------------------------------------------

function resetExportPanel() {
  hide(qs('exp-preview'));
  hide(qs('exp-msg'));
  hide(qs('imp-result'));
  hide(qs('imp-msg'));
  qs('imp-btn').disabled = !qs('imp-file').files?.length;
}

function _exportParams() {
  const params = new URLSearchParams();
  const src   = qs('exp-source').value.trim();
  const tag   = qs('exp-tag').value.trim();
  const since = qs('exp-since').value.trim();
  const ns    = qs('exp-namespace').value;
  if (src)   params.set('source', src);
  if (tag)   params.set('tag', tag);
  if (since) params.set('since', since);
  if (ns)    params.set('namespace', ns);
  return params;
}

async function runExportPreview() {
  hide(qs('exp-preview'));
  try {
    const data = await api('GET', `/api/export/stats?${_exportParams()}`);
    qs('exp-count').textContent = data.total_chunks;
    show(qs('exp-preview'));
  } catch (err) {
    setMsg(qs('exp-msg'), 'Preview failed: ' + err.message, true);
  }
}

function runExportDownload() {
  const url = `/api/export?${_exportParams()}`;
  const a = document.createElement('a');
  a.href = url;
  a.download = 'memtomem_export.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

async function runImport() {
  const file = qs('imp-file').files[0];
  if (!file) return;

  hide(qs('imp-result'));
  qs('imp-btn').disabled = true;

  const form = new FormData();
  form.append('file', file);

  try {
    const res = await fetch('/api/export/import', { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    qs('imp-total').textContent    = data.total_chunks;
    qs('imp-imported').textContent = data.imported_chunks;
    qs('imp-skipped').textContent  = data.skipped_chunks;
    qs('imp-failed').textContent   = data.failed_chunks;
    show(qs('imp-result'));
    showToast(`${data.imported_chunks} chunks imported.`, 'success');
    _markDataStale();
    loadSourceFilter();
    loadStats();
  } catch (err) {
    showToast('Import failed: ' + err.message, 'error');
  } finally {
    qs('imp-btn').disabled = false;
  }
}

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
                chunk_overlap_tokens: 'Chunk Overlap', structured_chunk_mode: 'Structured Chunk Mode' },
  namespace: { default_namespace: 'Default NS', enable_auto_ns: 'Auto NS' },
};

// Sections that are fully read-only (require restart)
const _READONLY_SECTIONS = new Set(['embedding', 'storage']);

// Individual read-only fields within editable sections
const _READONLY_FIELDS = {
  indexing: new Set(['memory_dirs', 'supported_extensions']),
};

// STATE.serverConfig now in STATE

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
    const saved = localStorage.getItem('m2m-default-top-k');
    if (!saved) {
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
    STATE.serverConfig = await api('GET', '/api/config');
    contentEl.innerHTML = '';

    Object.entries(STATE.serverConfig).forEach(([section, values]) => {
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
        table.appendChild(tr);
      });

      card.appendChild(table);
      card.addEventListener('mouseenter', () => _showConfigGuide(section));
      card.addEventListener('focusin', () => _showConfigGuide(section));
      contentEl.appendChild(card);
    });

    // Show first section guide by default
    const firstSection = Object.keys(STATE.serverConfig)[0];
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
      { label: 'Memory Dirs', text: 'Directories that can be indexed. Only files under these paths are allowed. Manage via API or env var.' },
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
        'Chunk token settings: edit here + Save (immediate, no restart)',
        'After changing chunk settings, re-index to apply to existing data',
        'Memory Dirs: POST /api/memory-dirs/add with {"path": "/your/dir"}',
        'Or set env: MEMTOMEM_INDEXING__MEMORY_DIRS=\'["/path1","/path2"]\'',
      ],
      warn: 'Memory Dirs and Extensions are read-only in UI. Use API or env vars. Chunk setting changes require re-index to take effect on existing data.',
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
};

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
}

async function _saveSection(section) {
  const card = document.querySelector(`.config-card[data-section="${section}"]`);
  const inputs = card.querySelectorAll('input[data-section], select[data-section]');
  const patch = {};

  inputs.forEach(inp => {
    const key = inp.dataset.key;
    let val;
    if (inp.type === 'checkbox') val = inp.checked;
    else if (inp.type === 'number') val = parseFloat(inp.value);
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
      showToast(`Some fields rejected: ${resp.rejected.join(', ')}`, 'error');
    }
    if (resp.applied?.length) {
      showToast(`${resp.applied.length} settings updated`, 'success');
      resp.applied.forEach(c => {
        const [sec, key] = c.field.split('.');
        const inp = document.getElementById(`cfg-${sec}-${key}`);
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
    showToast('Config save failed: ' + err.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

// Fields that require reindex or FTS rebuild after change
const _REINDEX_FIELDS = new Set([
  'indexing.max_chunk_tokens', 'indexing.min_chunk_tokens', 'indexing.chunk_overlap_tokens',
  'indexing.structured_chunk_mode',
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
        const res = await api('POST', '/api/fts-rebuild');
        showToast(res.message || `FTS rebuilt: ${res.rebuilt_rows} chunks`, 'success');
        btn.textContent = 'Done';
        btn.disabled = true;
      } catch (err) {
        showToast('FTS rebuild failed: ' + err.message, 'error');
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
        const res = await api('POST', '/api/reindex?force=true');
        if (res.errors && res.errors.length) {
          showToast(`Re-index completed with ${res.errors.length} error(s): ${res.errors[0]}`, 'error');
        } else {
          const total = (res.results || []).reduce((s, r) => s + (r.indexed_chunks || 0), 0);
          showToast(`Re-index complete — ${total} chunks indexed`, 'success');
        }
        btn.textContent = 'Done';
        btn.disabled = true;
        _markDataStale();
        loadStats();
      } catch (err) {
        showToast('Re-index failed: ' + err.message, 'error');
      } finally {
        btnLoading(btn, false);
      }
    });
  }
}

qs('exp-preview-btn').addEventListener('click', runExportPreview);
qs('exp-download-btn').addEventListener('click', runExportDownload);
qs('imp-file-trigger')?.addEventListener('click', () => qs('imp-file')?.click());
qs('imp-file').addEventListener('change', () => {
  const files = qs('imp-file').files;
  qs('imp-btn').disabled = !files?.length;
  const nameEl = qs('imp-file-name');
  if (nameEl) nameEl.textContent = files?.length ? files[0].name : 'No file chosen';
});
qs('imp-btn').addEventListener('click', runImport);

// ---------------------------------------------------------------------------
// Find Similar
// ---------------------------------------------------------------------------

qs('d-similar-btn').addEventListener('click', findSimilar);
qs('similar-close-btn').addEventListener('click', () => hide(qs('similar-panel')));

async function findSimilar() {
  if (!STATE.selectedChunkId) return;
  const panel = qs('similar-panel');
  const list  = qs('similar-list');
  show(panel);
  list.innerHTML = '<div class="empty-state" style="height:60px"><p>Loading…</p></div>';

  try {
    const data = await api('GET', `/api/chunks/${STATE.selectedChunkId}/similar?top_k=5`);
    if (!data.results.length) {
      list.innerHTML = '<div class="empty-state" style="height:60px"><p>No similar chunks found</p></div>';
      return;
    }
    list.innerHTML = '';
    data.results.forEach(r => {
      const card = document.createElement('div');
      card.className = 'similar-card';
      card.innerHTML = `
        <div class="similar-card-meta">
          <span class="score-badge">${r.score.toFixed(3)}</span>
          <span class="file-path" style="font-size:0.72rem">${escapeHtml(truncate(r.chunk.source_file, 55))}</span>
        </div>
        <div class="similar-card-content">${escapeHtml(truncate(r.chunk.content, 180))}</div>
      `;
      card.addEventListener('click', () => {
        showDetail(r);
        hide(qs('similar-panel'));
        document.querySelectorAll('.result-item').forEach(el => el.classList.remove('selected'));
      });
      list.appendChild(card);
    });
  } catch (err) {
    list.innerHTML = `<div class="empty-state" style="height:60px"><p>Error: ${escapeHtml(err.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------------------
// XSS helpers + highlighting
// ---------------------------------------------------------------------------

/**
 * Highlight query tokens in text. Returns HTML string with <mark> wrapping matches.
 * Safely escapes all content to prevent XSS.
 */
function highlightText(text, query) {
  const escaped = escapeHtml(text);
  if (!query) return escaped;

  // Split query into non-empty tokens (word characters, 2+ chars)
  const tokens = query.split(/\s+/).filter(t => t.length >= 2);
  if (!tokens.length) return escaped;

  // Build alternation regex from escaped token literals
  const pattern = tokens
    .map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
  const re = new RegExp(`(${pattern})`, 'gi');
  return escaped.replace(re, '<mark>$1</mark>');
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function escapeAttr(str) { return String(str).replace(/"/g, '&quot;'); }

// ---------------------------------------------------------------------------
// Search History (A)
// ---------------------------------------------------------------------------

const HISTORY_KEY = 'memtomem_search_history';
const HISTORY_MAX = 10;

function _loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch { return []; }
}
function saveToHistory(query) {
  if (!query) return;
  let h = _loadHistory().filter(q => q !== query);
  h.unshift(query);
  localStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(0, HISTORY_MAX)));
}
function _removeFromHistory(query) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(_loadHistory().filter(q => q !== query)));
}

function renderHistoryDropdown() {
  const dropdown = qs('search-history-dropdown');
  const filter   = qs('search-input').value.trim().toLowerCase();
  let history    = _loadHistory();
  if (filter) history = history.filter(q => q.toLowerCase().includes(filter));
  if (!history.length) { hide(dropdown); return; }

  dropdown.innerHTML = '';
  history.forEach(q => {
    const item = document.createElement('div');
    item.className = 'history-item';
    item.innerHTML = `<span class="history-text">${escapeHtml(q)}</span><button class="history-remove" title="Remove">✕</button>`;
    item.querySelector('.history-text').addEventListener('mousedown', e => {
      e.preventDefault(); // keep focus on input
      qs('search-input').value = q;
      hide(dropdown);
      doSearch();
    });
    item.querySelector('.history-remove').addEventListener('mousedown', e => {
      e.preventDefault();
      _removeFromHistory(q);
      renderHistoryDropdown();
    });
    dropdown.appendChild(item);
  });

  const clearAll = document.createElement('div');
  clearAll.className = 'history-clear-all';
  clearAll.textContent = 'Clear history';
  clearAll.addEventListener('mousedown', e => {
    e.preventDefault();
    localStorage.removeItem(HISTORY_KEY);
    hide(dropdown);
  });
  dropdown.appendChild(clearAll);
  show(dropdown);
}

function renderRecentChips() {
  const container = qs('recent-chips');
  if (!container) return;
  const history = _loadHistory();
  if (!history.length) { hide(container); return; }
  container.innerHTML = '<span class="recent-chips-label">Recent:</span>' +
    history.slice(0, 6).map(q =>
      `<button class="recent-chip" title="${escapeAttr(q)}">${escapeHtml(truncate(q, 24))}</button>`
    ).join('');
  container.querySelectorAll('.recent-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      qs('search-input').value = btn.title;
      doSearch();
    });
  });
  show(container);
}

// Render chips on initial load
// renderRecentChips() is now called from the unified DOMContentLoaded handler in initTheme()

// ---------------------------------------------------------------------------
// Keyboard Shortcuts (B)
// ---------------------------------------------------------------------------

function _isTextField(el) {
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable;
}

document.addEventListener('keydown', e => {
  // Esc: close topmost overlay first (always handled)
  if (e.key === 'Escape') {
    const confirmModal = qs('confirm-modal');
    if (confirmModal && !confirmModal.hidden) return; // handled by showConfirm's own listener
    const srcPreview = qs('source-preview-modal');
    if (srcPreview && !srcPreview.hidden) { hide(srcPreview); return; }
    const expandModal = qs('expand-modal');
    if (expandModal && !expandModal.hidden) { hide(expandModal); return; }
    const settingsModal = qs('settings-modal');
    if (settingsModal && !settingsModal.hidden) { hide(settingsModal); return; }
    const modal = qs('shortcuts-modal');
    if (modal && !modal.hidden) { hide(modal); return; }
    const dropdown = qs('search-history-dropdown');
    if (dropdown && !dropdown.hidden) { hide(dropdown); return; }
    const similar = qs('similar-panel');
    if (similar && !similar.hidden) { hide(similar); return; }
    const sourceChunks = qs('source-chunks-panel');
    if (sourceChunks && !sourceChunks.hidden) { hide(sourceChunks); return; }
    if (qs('detail-view') && !qs('detail-view').hidden) { clearDetail(); return; }
    return;
  }

  // Other shortcuts: skip when user is typing
  if (_isTextField(e.target)) return;

  if (e.key === '/') {
    e.preventDefault();
    const input = qs('search-input');
    input.focus();
    input.select();
    return;
  }

  if (e.key === '?') {
    e.preventDefault();
    const modal = qs('shortcuts-modal');
    modal.hidden ? show(modal) : hide(modal);
    return;
  }

  if (e.key === 'h') {
    e.preventDefault();
    toggleHelp();
    return;
  }

  if (e.key === 'j' || e.key === 'k') {
    e.preventDefault();
    const items = [...document.querySelectorAll('.result-item')];
    if (!items.length) return;
    const cur = document.querySelector('.result-item.selected');
    const idx = cur ? items.indexOf(cur) : -1;
    const next = e.key === 'j'
      ? (idx < items.length - 1 ? items[idx + 1] : items[0])
      : (idx > 0 ? items[idx - 1] : items[items.length - 1]);
    next.click();
    next.scrollIntoView({ block: 'nearest' });
    return;
  }

  // H. Pin shortcut
  if (e.key === 'p' && STATE.selectedChunkId) {
    e.preventDefault();
    qs('d-pin-btn').click();
    return;
  }

  // H. Copy shortcut
  if (e.key === 'c' && STATE.selectedChunkId) {
    e.preventDefault();
    copyToClipboard(qs('d-editor').value);
    return;
  }
});

// ---------------------------------------------------------------------------
// Chunk-type filter (C2) — client-side re-render
// ---------------------------------------------------------------------------

qs('chunk-type-filter').addEventListener('change', () => renderResults(STATE.lastResults));

// URL query sync (C2)
function _syncSearchToURL() {
  const params = new URLSearchParams();
  const q = qs('search-input').value.trim();
  if (q) params.set('q', q);
  const ct = qs('chunk-type-filter').value;
  if (ct) params.set('type', ct);
  // source-filter is now a multi-select; not synced to URL
  history.replaceState(null, '', params.toString() ? '?' + params : window.location.pathname);
}

(function _loadSearchFromURL() {
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q');
  const ct = params.get('type');
  if (q) qs('search-input').value = q;
  if (ct && qs('chunk-type-filter')) qs('chunk-type-filter').value = ct;
  // source multi-filter not synced from URL
  if (q) {
    activateTab('search');
    doSearch();
  }
})();

// ---------------------------------------------------------------------------
// Load More (C3)
// ---------------------------------------------------------------------------

qs('load-more-btn').addEventListener('click', async () => {
  const q = qs('search-input').value.trim();
  if (!q) return;
  STATE.currentTopK = Math.min(STATE.currentTopK + 10, 100);
  const tf = qs('tag-filter').value.trim();
  const params = new URLSearchParams({ q, top_k: STATE.currentTopK });
  if (tf) params.set('tag_filter', tf);
  const btn = qs('load-more-btn');
  btnLoading(btn, true);
  try {
    const data = await api('GET', `/api/search?${params}`);
    renderResults(data.results);
  } catch (err) {
    showToast('Error: ' + err.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
});

// ---------------------------------------------------------------------------
// Pin / Favorite (D2)
// ---------------------------------------------------------------------------

function _getPinStore() {
  try { return JSON.parse(localStorage.getItem('m2m-pins') || '{}'); } catch { return {}; }
}
function _savePinStore(store) {
  localStorage.setItem('m2m-pins', JSON.stringify(store));
}
function isPinned(id) {
  return id !== null && String(id) in _getPinStore();
}
function pinChunk(id, preview) {
  const store = _getPinStore();
  store[String(id)] = preview;
  _savePinStore(store);
}
function unpinChunk(id) {
  const store = _getPinStore();
  delete store[String(id)];
  _savePinStore(store);
}
function updatePinBtn(chunkId) {
  const btn = qs('d-pin-btn');
  if (!btn) return;
  const pinned = isPinned(chunkId);
  btn.textContent = pinned ? '★ Pinned' : '☆ Pin';
  btn.classList.toggle('btn-pin-active', pinned);
}
function renderPinnedSection() {
  const list = qs('home-pinned-list');
  if (!list) return;
  const store = _getPinStore();
  const items = Object.entries(store);
  if (!items.length) {
    list.innerHTML = '<div class="empty-state" style="height:50px"><span>No pinned chunks yet — click ☆ Pin in the detail panel</span></div>';
    return;
  }
  list.innerHTML = items.map(([id, p]) => `
    <div class="home-source-item">
      <span class="home-source-name">${escapeHtml(p.source || 'unknown')}</span>
      <span class="home-pinned-snippet">${escapeHtml(truncate(p.snippet || '', 50))}</span>
      <button class="unpin-btn btn-ghost btn-xs" data-id="${escapeAttr(id)}" title="Unpin">✕</button>
    </div>`).join('');
  list.querySelectorAll('.unpin-btn').forEach(b => {
    b.addEventListener('click', e => {
      e.stopPropagation();
      unpinChunk(b.dataset.id);
      renderPinnedSection();
      if (STATE.selectedChunkId && String(STATE.selectedChunkId) === b.dataset.id) updatePinBtn(STATE.selectedChunkId);
    });
  });
}

qs('d-pin-btn').addEventListener('click', () => {
  if (!STATE.selectedChunkId) return;
  const id = String(STATE.selectedChunkId);
  if (isPinned(id)) {
    unpinChunk(id);
    showToast('Unpinned', 'info');
  } else {
    pinChunk(id, {
      source: qs('d-file').textContent || '',
      snippet: qs('d-editor').value.slice(0, 100),
    });
    showToast('Pinned!', 'info');
  }
  updatePinBtn(id);
  STATE.homeStale = true;
});

// ---------------------------------------------------------------------------
// Settings (E1)
// ---------------------------------------------------------------------------

function _loadSettings() {
  return {
    defaultTab: localStorage.getItem('m2m-default-tab') || 'search',
    defaultTopK: localStorage.getItem('m2m-default-top-k') || '10',
  };
}

(function _applySettings() {
  const s = _loadSettings();
  qs('top-k').value = s.defaultTopK;
  // Apply default tab only if no hash deep link is present
  if (!location.hash.slice(1)) {
    const currentActive = document.querySelector('.tab-btn.active');
    const currentTab = currentActive ? currentActive.dataset.tab : null;
    if (currentTab !== s.defaultTab) {
      activateTab(s.defaultTab);
    }
  }
})();

qs('settings-btn').addEventListener('click', () => {
  const s = _loadSettings();
  qs('settings-default-tab').value = s.defaultTab;
  qs('settings-default-topk').value = s.defaultTopK;
  show(qs('settings-modal'));
});
qs('settings-close-btn').addEventListener('click', () => hide(qs('settings-modal')));
qs('settings-modal').addEventListener('click', e => {
  if (e.target === qs('settings-modal')) hide(qs('settings-modal'));
});
qs('settings-save-btn').addEventListener('click', () => {
  localStorage.setItem('m2m-default-tab', qs('settings-default-tab').value);
  localStorage.setItem('m2m-default-top-k', qs('settings-default-topk').value);
  showToast('Settings saved', 'success');
  hide(qs('settings-modal'));
});
qs('settings-reset-btn').addEventListener('click', () => {
  localStorage.removeItem('m2m-default-tab');
  localStorage.removeItem('m2m-default-top-k');
  qs('settings-default-tab').value = 'search';
  qs('settings-default-topk').value = '10';
  showToast('Settings reset to defaults', 'info');
});

qs('shortcuts-close-btn').addEventListener('click', () => hide(qs('shortcuts-modal')));
qs('shortcuts-modal').addEventListener('click', e => {
  if (e.target === qs('shortcuts-modal')) hide(qs('shortcuts-modal'));
});

// ---------------------------------------------------------------------------
// Timeline Tab
// ---------------------------------------------------------------------------

let tlViewMode = 'chunks';
let currentTlChunks = null;

function resetTimelinePanel() {
  hide(qs('tl-list'));
  hide(qs('tl-heatmap'));
  hide(qs('tl-stats'));
  const tlEmpty = qs('tl-empty');
  tlEmpty.innerHTML = emptyState('🕐', 'Click Load to view the timeline');
  show(tlEmpty);
}

qs('tl-load-btn').addEventListener('click', loadTimeline);

qs('tl-view-chunks').addEventListener('click', () => {
  if (tlViewMode === 'chunks') return;
  tlViewMode = 'chunks';
  qs('tl-view-chunks').classList.add('tl-view-active');
  qs('tl-view-files').classList.remove('tl-view-active');
  if (currentTlChunks) renderTimeline(currentTlChunks);
});
qs('tl-view-files').addEventListener('click', () => {
  if (tlViewMode === 'files') return;
  tlViewMode = 'files';
  qs('tl-view-files').classList.add('tl-view-active');
  qs('tl-view-chunks').classList.remove('tl-view-active');
  if (currentTlChunks) renderTimeline(currentTlChunks);
});

qs('tl-days').addEventListener('change', () => {
  const custom = qs('tl-date-custom');
  custom.hidden = qs('tl-days').value !== 'custom';
});

async function loadTimeline() {
  const daysVal = qs('tl-days').value;
  const source = qs('tl-source').value.trim();
  const limit = qs('tl-limit').value;
  const ns = qs('tl-namespace').value;

  let days;
  if (daysVal === 'custom') {
    const fromVal = qs('tl-date-from').value;
    const toVal = qs('tl-date-to').value;
    const from = fromVal ? new Date(fromVal) : new Date(Date.now() - 30 * 86400000);
    const to = toVal ? new Date(toVal + 'T23:59:59') : new Date();
    days = Math.max(1, Math.ceil((to - from) / 86400000));
  } else {
    days = daysVal;
  }

  const params = new URLSearchParams({ days, limit });
  if (source) params.set('source', source);
  if (ns) params.set('namespace', ns);

  hide(qs('tl-empty'));
  const list = qs('tl-list');
  panelLoading(list);
  show(list);

  try {
    const data = await api('GET', `/api/timeline?${params}`);
    let chunks = data.chunks;
    // Custom range: filter to exact from–to window
    if (daysVal === 'custom') {
      const fromVal = qs('tl-date-from').value;
      const toVal = qs('tl-date-to').value;
      if (fromVal) chunks = chunks.filter(c => c.created_at.slice(0, 10) >= fromVal);
      if (toVal) chunks = chunks.filter(c => c.created_at.slice(0, 10) <= toVal);
    }
    currentTlChunks = chunks;
    renderTimeline(chunks);
  } catch (err) {
    setMsg(qs('tl-msg'), 'Error: ' + err.message, true);
    resetTimelinePanel();
  }
}

function renderTimeline(chunks) {
  const list = qs('tl-list');
  const tlStats = qs('tl-stats');
  if (!chunks.length) {
    hide(list);
    hide(tlStats);
    const tlEmpty = qs('tl-empty');
    tlEmpty.innerHTML = emptyState('🕐', 'No memories recorded in this period');
    show(tlEmpty);
    return;
  }

  // Group by calendar date (created_at)
  const groups = new Map();
  for (const c of chunks) {
    const date = c.created_at.slice(0, 10); // YYYY-MM-DD
    if (!groups.has(date)) groups.set(date, []);
    groups.get(date).push(c);
  }

  // (A) Activity Summary Bar
  const totalChunks = chunks.length;
  const uniqueFiles = new Set(chunks.map(c => c.source_file)).size;
  let mostActiveDate = '', mostActiveCount = 0;
  for (const [date, items] of groups) {
    if (items.length > mostActiveCount) { mostActiveCount = items.length; mostActiveDate = date; }
  }
  const fmtDate = mostActiveDate ? new Date(mostActiveDate + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '';
  tlStats.textContent = `${totalChunks} chunk${totalChunks !== 1 ? 's' : ''} \u00b7 ${uniqueFiles} file${uniqueFiles !== 1 ? 's' : ''} \u00b7 Most active: ${fmtDate} (${mostActiveCount} chunks)`;
  show(tlStats);

  // Render heatmap bar chart (scrollable track)
  const heatmap = qs('tl-heatmap');
  const maxCount = Math.max(...[...groups.values()].map(v => v.length));
  const cols = [...groups].map(([date, items]) => {
    const pct = Math.max(Math.round((items.length / maxCount) * 100), 4);
    const short = date.slice(5); // MM-DD
    const fmtTip = new Date(date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    const tipText = `${fmtTip} — ${items.length} chunk${items.length !== 1 ? 's' : ''}`;
    return `<div class="tl-heatmap-col" data-tooltip="${escapeAttr(tipText)}" data-date="${date}">
      <span class="tl-heatmap-count">${items.length}</span>
      <div class="tl-heatmap-bar" style="height:${pct}%"></div>
      <span class="tl-heatmap-label">${short}</span>
    </div>`;
  });
  heatmap.innerHTML = `<div class="tl-heatmap-track">${cols.join('')}</div>`;
  heatmap.querySelectorAll('.tl-heatmap-col').forEach(col => {
    col.addEventListener('click', () => {
      heatmap.querySelectorAll('.tl-heatmap-col').forEach(c => c.classList.remove('active'));
      col.classList.add('active');
      const target = list.querySelector(`[data-tl-date="${col.dataset.date}"]`);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        // Flash the date heading
        const heading = target.querySelector('.timeline-date-heading');
        if (heading) {
          heading.classList.remove('tl-date-flash');
          void heading.offsetWidth; // reflow to restart animation
          heading.classList.add('tl-date-flash');
        }
      }
    });
  });
  show(heatmap);
  // Scroll to latest (rightmost)
  requestAnimationFrame(() => { heatmap.scrollLeft = heatmap.scrollWidth; });

  list.innerHTML = '';
  if (tlViewMode === 'files') {
    renderFileView(list, groups);
  } else {
    renderChunkView(list, groups);
  }
  show(list);
}

function renderChunkView(list, groups) {
  for (const [date, items] of groups) {
    const group = document.createElement('div');
    group.className = 'timeline-date-group';
    group.setAttribute('data-tl-date', date);
    const heading = document.createElement('div');
    heading.className = 'timeline-date-heading';
    const uniqueSources = new Set(items.map(c => c.source_file)).size;
    heading.innerHTML = `
      <span>${date}</span>
      <span class="tl-date-stats">
        <span class="tl-date-stat">${items.length} chunk${items.length !== 1 ? 's' : ''}</span>
        <span class="tl-date-stat">${uniqueSources} file${uniqueSources !== 1 ? 's' : ''}</span>
      </span>
    `;
    group.appendChild(heading);

    for (const c of items) {
      const item = document.createElement('div');
      item.className = 'timeline-item';
      const time = c.created_at.slice(11, 16); // HH:MM
      const tagsHtml = c.tags.map(t => `<span class="timeline-tag">${escapeHtml(t)}</span>`).join('');
      const dot = `<span class="tl-type-dot" style="background:${fileTypeColor(c.source_file)}"></span>`;
      item.innerHTML = `
        <div class="timeline-item-header">
          <span class="timeline-item-source">${dot}${escapeHtml(truncate(c.source_file, 60))}</span>
          <span class="timeline-item-time">${time}</span>
        </div>
        <div class="timeline-item-snippet">${escapeHtml(c.content)}</div>
        <div class="tl-expand-tags">${tagsHtml}</div>
        <div class="tl-expand-actions">
          <button class="tl-btn-open">Open</button>
          <button class="tl-btn-copy">Copy</button>
        </div>
      `;
      // (C) Inline expansion: first click expand/collapse, "Open" button navigates
      item.addEventListener('click', (e) => {
        // If click is on an action button, handle separately
        if (e.target.closest('.tl-expand-actions')) return;
        item.classList.toggle('tl-item-expanded');
        item.setAttribute('aria-expanded', item.classList.contains('tl-item-expanded'));
      });
      item.querySelector('.tl-btn-open').addEventListener('click', (e) => {
        e.stopPropagation();
        showDetailFromChunk(c);
      });
      item.querySelector('.tl-btn-copy').addEventListener('click', (e) => {
        e.stopPropagation();
        copyToClipboard(c.content);
      });
      group.appendChild(item);
    }
    list.appendChild(group);
  }
}

function renderFileView(list, groups) {
  for (const [date, items] of groups) {
    const group = document.createElement('div');
    group.className = 'timeline-date-group';
    group.setAttribute('data-tl-date', date);

    const uniqueSources = new Set(items.map(c => c.source_file)).size;
    const heading = document.createElement('div');
    heading.className = 'timeline-date-heading';
    heading.innerHTML = `
      <span>${date}</span>
      <span class="tl-date-stats">
        <span class="tl-date-stat">${items.length} chunk${items.length !== 1 ? 's' : ''}</span>
        <span class="tl-date-stat">${uniqueSources} file${uniqueSources !== 1 ? 's' : ''}</span>
      </span>
    `;
    group.appendChild(heading);

    // Sub-group by source_file
    const fileGroups = new Map();
    for (const c of items) {
      if (!fileGroups.has(c.source_file)) fileGroups.set(c.source_file, []);
      fileGroups.get(c.source_file).push(c);
    }

    for (const [filePath, fileChunks] of fileGroups) {
      const sorted = [...fileChunks].sort((a, b) => a.created_at.localeCompare(b.created_at));
      const lastTime = sorted[sorted.length - 1].created_at.slice(11, 16);
      const fname = basename(filePath);
      const fdir = filePath.slice(0, filePath.length - fname.length - 1) || '/';

      const fileItem = document.createElement('div');
      fileItem.className = 'timeline-file-item';

      const header = document.createElement('div');
      header.className = 'timeline-file-header';
      const dot = `<span class="tl-type-dot" style="background:${fileTypeColor(filePath)}"></span>`;
      header.innerHTML = `
        <span class="tl-file-chevron">▶</span>
        ${dot}<span class="timeline-file-name" title="${escapeHtml(filePath)}">${escapeHtml(fname)}</span>
        <span class="timeline-file-dir">${escapeHtml(truncate(fdir, 50))}</span>
        <span class="tl-file-count">${fileChunks.length}</span>
        <span class="tl-file-time">${lastTime}</span>
      `;
      fileItem.appendChild(header);

      const preview = document.createElement('div');
      preview.className = 'tl-file-preview';
      preview.textContent = truncate(sorted[0].content, 130);
      fileItem.appendChild(preview);

      // Expanded chunk list (hidden by default)
      const chunkList = document.createElement('div');
      chunkList.className = 'tl-file-chunk-list';
      chunkList.hidden = true;
      for (const c of sorted) {
        const ci = document.createElement('div');
        ci.className = 'tl-file-chunk-item';
        const time = c.created_at.slice(11, 16);
        const tagsHtml = c.tags.map(t => `<span class="timeline-tag">${escapeHtml(t)}</span>`).join('');
        ci.innerHTML = `
          <div class="tl-fci-header">
            <span class="tl-fci-type">${escapeHtml(c.chunk_type)}</span>
            <span class="tl-fci-time">${time}</span>
          </div>
          <div class="tl-fci-snippet">${escapeHtml(truncate(c.content, 150))}</div>
          ${tagsHtml ? `<div class="timeline-item-tags">${tagsHtml}</div>` : ''}
        `;
        ci.addEventListener('click', e => { e.stopPropagation(); showDetailFromChunk(c); });
        chunkList.appendChild(ci);
      }
      fileItem.appendChild(chunkList);

      fileItem.addEventListener('click', () => {
        const expanded = !chunkList.hidden;
        chunkList.hidden = expanded;
        header.querySelector('.tl-file-chevron').textContent = expanded ? '▶' : '▼';
        fileItem.classList.toggle('tl-file-expanded', !expanded);
        fileItem.setAttribute('aria-expanded', !expanded);
      });

      group.appendChild(fileItem);
    }
    list.appendChild(group);
  }
}

function showDetailFromChunk(c) {
  // Switch to search tab and populate the detail panel
  activateTab('search');
  // Reuse score/rank from lastResults if available
  const existing = STATE.lastResults.find(r => String(r.chunk.id) === String(c.id));
  const result = existing || { chunk: c, score: 0, rank: 0, source: 'browse' };
  showDetail(result);
}

// ---------------------------------------------------------------------------
// Source Multi-Filter (F3)
// ---------------------------------------------------------------------------

async function loadSourceFilter() {
  const sel = qs('source-filter');
  if (!sel) return;
  try {
    const data = await api('GET', '/api/sources');
    if (!data.sources.length) {
      sel.innerHTML = '<option value="" disabled>No sources indexed</option>';
      return;
    }
    sel.innerHTML = data.sources.map(s =>
      `<option value="${escapeAttr(s.path)}">${escapeHtml(basename(s.path))}</option>`
    ).join('');
  } catch (_) {
    sel.innerHTML = '<option value="" disabled>Error loading sources</option>';
  }
}

qs('source-filter').addEventListener('change', () => renderResults(STATE.lastResults));

// ---------------------------------------------------------------------------
// Score Threshold (F2)
// ---------------------------------------------------------------------------

qs('score-threshold').addEventListener('input', () => {
  STATE.scoreMin = parseFloat(qs('score-threshold').value);
  qs('score-val').textContent = STATE.scoreMin.toFixed(1);
  renderResults(STATE.lastResults);
});

// ---------------------------------------------------------------------------
// Date Range Filter (Kibana-style)
// ---------------------------------------------------------------------------

function _getDateRange() {
  const preset = qs('date-range-preset').value;
  if (!preset) return null;
  const now = Date.now();
  const dayMs = 86400000;
  if (preset === 'today') {
    const start = new Date(); start.setHours(0,0,0,0);
    return { from: start.getTime(), to: now };
  }
  if (preset === '7d') return { from: now - 7 * dayMs, to: now };
  if (preset === '30d') return { from: now - 30 * dayMs, to: now };
  if (preset === '90d') return { from: now - 90 * dayMs, to: now };
  if (preset === 'custom') {
    const fromVal = qs('date-from').value;
    const toVal = qs('date-to').value;
    const from = fromVal ? new Date(fromVal).getTime() : 0;
    const to = toVal ? new Date(toVal + 'T23:59:59').getTime() : now;
    return { from, to };
  }
  return null;
}

qs('date-range-preset').addEventListener('change', () => {
  const custom = qs('date-range-custom');
  custom.hidden = qs('date-range-preset').value !== 'custom';
  if (STATE.lastResults.length) renderResults(STATE.lastResults);
});
qs('date-from').addEventListener('change', () => { if (STATE.lastResults.length) renderResults(STATE.lastResults); });
qs('date-to').addEventListener('change', () => { if (STATE.lastResults.length) renderResults(STATE.lastResults); });

// ---------------------------------------------------------------------------
// Filter Row Toggle
// ---------------------------------------------------------------------------

qs('filter-toggle').addEventListener('click', () => {
  const filters = document.querySelector('.search-filters');
  filters.hidden = !filters.hidden;
  qs('filter-toggle').classList.toggle('btn-active', !filters.hidden);
});

// ---------------------------------------------------------------------------
// Advanced Filters Toggle
// ---------------------------------------------------------------------------

qs('adv-toggle').addEventListener('click', () => {
  const panel = qs('search-advanced');
  panel.hidden = !panel.hidden;
  qs('adv-toggle').classList.toggle('btn-active', !panel.hidden);
  if (!panel.hidden) loadSourceFilter();
});

// ---------------------------------------------------------------------------
// View Toggle (I1)
// ---------------------------------------------------------------------------

qs('view-toggle').addEventListener('click', () => {
  STATE.viewMode = STATE.viewMode === 'card' ? 'list' : 'card';
  qs('view-toggle').textContent = STATE.viewMode === 'list' ? '⊟' : '☰';
  qs('view-toggle').title = STATE.viewMode === 'list' ? 'Switch to card view' : 'Switch to list view';
  renderResults(STATE.lastResults);
});

// ---------------------------------------------------------------------------
// Expand Detail (I2)
// ---------------------------------------------------------------------------

qs('d-expand-btn').addEventListener('click', () => {
  qs('expand-modal-title').textContent = qs('d-file').textContent || 'Content';
  const pre = qs('expand-content');
  pre.textContent = '';
  const fileExt = (qs('d-file').textContent || '').split('.').pop();
  const lang = getLanguage('.' + fileExt);
  const code = document.createElement('code');
  if (lang) code.className = `language-${lang}`;
  code.textContent = qs('d-editor').value;
  pre.appendChild(code);
  if (lang && window.Prism) Prism.highlightElement(code);
  show(qs('expand-modal'));
});

qs('expand-close-btn').addEventListener('click', () => hide(qs('expand-modal')));
qs('expand-modal').addEventListener('click', e => {
  if (e.target === qs('expand-modal')) hide(qs('expand-modal'));
});

// ---------------------------------------------------------------------------
// Source Preview Modal — full document view with chunk highlight
// ---------------------------------------------------------------------------

async function openSourcePreview(sourcePath, highlightStart, highlightEnd) {
  const modal = qs('source-preview-modal');
  const body = qs('source-preview-body');
  const title = qs('source-preview-title');
  const info = qs('source-preview-info');

  title.textContent = basename(sourcePath);
  title.title = sourcePath;
  info.textContent = 'Loading…';
  body.innerHTML = '<div class="loading-panel"><div class="spinner-panel"></div></div>';
  show(modal);

  try {
    const data = await api('GET', `/api/sources/content?path=${encodeURIComponent(sourcePath)}`);
    const lines = data.content.split('\n');
    info.textContent = `${lines.length} lines · ${formatBytes(data.size)}`;

    const table = document.createElement('table');
    lines.forEach((line, i) => {
      const lineNum = i + 1;
      const tr = document.createElement('tr');
      if (highlightStart && highlightEnd && lineNum >= highlightStart && lineNum <= highlightEnd) {
        tr.className = 'highlight-chunk';
      }
      const noTd = document.createElement('td');
      noTd.className = 'line-no';
      noTd.textContent = lineNum;
      const contentTd = document.createElement('td');
      contentTd.className = 'line-content';
      contentTd.textContent = line || '\u00A0';
      tr.appendChild(noTd);
      tr.appendChild(contentTd);
      table.appendChild(tr);
    });

    body.innerHTML = '';
    body.appendChild(table);

    // Scroll to highlighted chunk
    if (highlightStart) {
      const target = table.querySelector('tr.highlight-chunk');
      if (target) {
        requestAnimationFrame(() => {
          target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        });
      }
    }
  } catch (err) {
    body.innerHTML = `<div style="padding:24px;color:var(--danger)">${escapeHtml(err.message)}</div>`;
    info.textContent = '';
  }
}

qs('source-preview-close').addEventListener('click', () => hide(qs('source-preview-modal')));
qs('source-preview-modal').addEventListener('click', e => {
  if (e.target === qs('source-preview-modal')) hide(qs('source-preview-modal'));
});

// Make d-file clickable to open source preview
qs('d-file').style.cursor = 'pointer';
qs('d-file').title = 'Click to view full source file';
qs('d-file').addEventListener('click', () => {
  const path = qs('d-file').textContent;
  if (!path) return;
  const r = STATE.lastResults.find(x => String(x.chunk.id) === String(STATE.selectedChunkId));
  const start = r ? r.chunk.start_line : null;
  const end = r ? r.chunk.end_line : null;
  openSourcePreview(path, start, end);
});

// ---------------------------------------------------------------------------
// Sort (J1)
// ---------------------------------------------------------------------------

qs('sort-select').addEventListener('change', () => {
  STATE.currentSortMode = qs('sort-select').value;
  renderResults(STATE.lastResults);
});

// ---------------------------------------------------------------------------
// Word Count (K2)
// ---------------------------------------------------------------------------

function _updateWordCount() {
  const text = qs('d-editor').value;
  const chars = text.length;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  const tokens = Math.round(chars / 4);
  const el = qs('d-word-count');
  if (el) el.textContent = `${chars} chars · ${words} words · ~${tokens} tokens`;
}

qs('d-editor').addEventListener('input', _updateWordCount);

// ---------------------------------------------------------------------------
// Source Nav — prev/next in source (J2)
// ---------------------------------------------------------------------------

// Cache of all chunks for the current source file (for full navigation)
let _sourceChunksCache = { file: null, chunks: [] };

async function _loadSourceChunks(sourceFile) {
  if (_sourceChunksCache.file === sourceFile && _sourceChunksCache.chunks.length) {
    return _sourceChunksCache.chunks;
  }
  try {
    const data = await api('GET', `/api/chunks?source=${encodeURIComponent(sourceFile)}&limit=500`);
    _sourceChunksCache = { file: sourceFile, chunks: data.chunks || [] };
    return _sourceChunksCache.chunks;
  } catch {
    return [];
  }
}

function _getSourceIdx(chunks) {
  return chunks.findIndex(c => String(c.id) === String(STATE.selectedChunkId));
}

async function _updateSourceNav() {
  const prevBtn = qs('d-prev-btn');
  const nextBtn = qs('d-next-btn');
  const posEl   = qs('d-source-pos');
  if (!prevBtn) return;
  const sf = qs('d-file')?.textContent;
  if (!sf || !STATE.selectedChunkId) {
    prevBtn.disabled = true; nextBtn.disabled = true; posEl.textContent = ''; return;
  }
  const chunks = await _loadSourceChunks(sf);
  const idx = _getSourceIdx(chunks);
  prevBtn.disabled = idx <= 0;
  nextBtn.disabled = idx < 0 || idx >= chunks.length - 1;
  posEl.textContent = idx >= 0 ? `${idx + 1}/${chunks.length}` : '';
}

qs('d-prev-btn').addEventListener('click', async () => {
  const sf = qs('d-file')?.textContent;
  if (!sf) return;
  const chunks = await _loadSourceChunks(sf);
  const idx = _getSourceIdx(chunks);
  if (idx <= 0) return;
  const c = chunks[idx - 1];
  const existing = STATE.lastResults.find(r => String(r.chunk.id) === String(c.id));
  showDetail(existing || { chunk: c, score: 0, rank: 0, source: 'browse' });
});

qs('d-next-btn').addEventListener('click', async () => {
  const sf = qs('d-file')?.textContent;
  if (!sf) return;
  const chunks = await _loadSourceChunks(sf);
  const idx = _getSourceIdx(chunks);
  if (idx < 0 || idx >= chunks.length - 1) return;
  const c = chunks[idx + 1];
  const existing = STATE.lastResults.find(r => String(r.chunk.id) === String(c.id));
  showDetail(existing || { chunk: c, score: 0, rank: 0, source: 'browse' });
});

// ---------------------------------------------------------------------------
// Saved Searches (J3)
// ---------------------------------------------------------------------------

const _SAVED_KEY = 'm2m-saved-queries';

function _getSavedQueries() {
  try { return JSON.parse(localStorage.getItem(_SAVED_KEY) || '[]'); } catch { return []; }
}
function _setSavedQueries(list) { localStorage.setItem(_SAVED_KEY, JSON.stringify(list)); }

function _renderSavedSelect() {
  const sel = qs('saved-queries-select');
  if (!sel) return;
  const list = _getSavedQueries();
  sel.innerHTML = '<option value="">— Load saved —</option>' +
    list.map((q, i) => `<option value="${i}">${escapeHtml(q.name)}</option>`).join('');
}

qs('save-query-btn').addEventListener('click', () => {
  const q = qs('search-input').value.trim();
  if (!q) { showToast('Enter a query first', 'error'); return; }
  const name = prompt('Save search as:', q);
  if (!name) return;
  const list = _getSavedQueries();
  list.push({ name, query: q, typeFilter: qs('chunk-type-filter').value, tagFilter: qs('tag-filter').value.trim() });
  _setSavedQueries(list);
  _renderSavedSelect();
  _renderSavedBar();
  showToast(`Saved: "${name}"`, 'success');
});

qs('saved-queries-select').addEventListener('change', () => {
  const idx = parseInt(qs('saved-queries-select').value);
  if (isNaN(idx)) return;
  const q = _getSavedQueries()[idx];
  if (!q) return;
  qs('search-input').value = q.query;
  if (qs('chunk-type-filter')) qs('chunk-type-filter').value = q.typeFilter || '';
  if (qs('tag-filter')) qs('tag-filter').value = q.tagFilter || '';
  qs('saved-queries-select').value = '';
  doSearch();
});

qs('delete-query-btn').addEventListener('click', () => {
  const idx = parseInt(qs('saved-queries-select').value);
  if (isNaN(idx)) { showToast('Select a saved search to delete', 'error'); return; }
  const list = _getSavedQueries();
  const name = list[idx]?.name;
  list.splice(idx, 1);
  _setSavedQueries(list);
  _renderSavedSelect();
  _renderSavedBar();
  showToast(`Deleted: "${name}"`, 'info');
});

_renderSavedSelect();

function _renderSavedBar() {
  const bar = qs('saved-searches-bar');
  if (!bar) return;
  const list = _getSavedQueries();
  if (!list.length) { hide(bar); return; }
  bar.innerHTML = '<span class="saved-bar-label">Saved:</span>' +
    list.map((q, i) =>
      `<span class="saved-chip" data-idx="${i}" title="${escapeAttr(q.query)}">` +
      `<span class="saved-chip-name">${escapeHtml(q.name)}</span>` +
      `<button class="saved-chip-remove" data-idx="${i}" title="Remove">✕</button></span>`
    ).join('');
  bar.querySelectorAll('.saved-chip-name').forEach(el => {
    el.addEventListener('click', () => {
      const idx = parseInt(el.parentElement.dataset.idx);
      const q = list[idx];
      if (!q) return;
      qs('search-input').value = q.query;
      if (qs('chunk-type-filter')) qs('chunk-type-filter').value = q.typeFilter || '';
      if (qs('tag-filter')) qs('tag-filter').value = q.tagFilter || '';
      doSearch();
    });
  });
  bar.querySelectorAll('.saved-chip-remove').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.idx);
      const name = list[idx]?.name;
      list.splice(idx, 1);
      _setSavedQueries(list);
      _renderSavedBar();
      _renderSavedSelect();
      showToast(`Removed: "${name}"`, 'info');
    });
  });
  show(bar);
}
_renderSavedBar();

// ---------------------------------------------------------------------------
// Source Chunks Browser (K3)
// ---------------------------------------------------------------------------

qs('d-source-btn').addEventListener('click', async () => {
  const panel = qs('source-chunks-panel');
  if (!panel.hidden) { hide(panel); return; }
  hide(qs('similar-panel'));

  const sourceFile = qs('d-file').textContent;
  if (!sourceFile) return;

  const list = qs('source-chunks-list');
  list.innerHTML = '<div class="loading-panel"><div class="spinner-panel"></div></div>';
  // Hide related panel to avoid stacking
  hide(qs('related-panel'));
  show(panel);
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const data = await api('GET', `/api/chunks?source=${encodeURIComponent(sourceFile)}&limit=100`);
    const chunks = data.chunks || [];
    if (!chunks.length) {
      list.innerHTML = '<div class="empty-state" style="height:40px"><span>No chunks found</span></div>';
      return;
    }
    list.innerHTML = chunks.map(c => `
      <div class="similar-item${String(c.id) === String(STATE.selectedChunkId) ? ' source-chunk-current' : ''}" data-id="${escapeAttr(String(c.id))}">
        <div class="similar-item-header">
          <span class="badge badge-gray" style="font-size:0.65rem">${escapeHtml(c.chunk_type.replace('_', ' '))}</span>
          <span style="font-size:0.65rem;color:var(--muted)">L${c.start_line}–${c.end_line}</span>
        </div>
        <div class="similar-item-snippet">${escapeHtml(truncate(c.content, 90))}</div>
      </div>`).join('');
    list.querySelectorAll('.similar-item').forEach(el => {
      el.style.cursor = 'pointer';
      el.addEventListener('click', () => {
        const c = chunks.find(ch => String(ch.id) === el.dataset.id);
        if (c) showDetailFromChunk(c);
      });
    });
  } catch (err) {
    list.innerHTML = `<div class="status-msg err">${escapeHtml(err.message)}</div>`;
  }
});

qs('source-chunks-close-btn').addEventListener('click', () => hide(qs('source-chunks-panel')));

// ---------------------------------------------------------------------------
// M1 + M2: Export (bulk selected / all results)
// ---------------------------------------------------------------------------

function downloadResults(items, format) {
  let content, mime, ext;
  if (format === 'csv') {
    const header = 'id,source,type,score,content\n';
    const rows = items.map(r => {
      const c = r.chunk || r;
      const score = r.score != null ? r.score.toFixed(4) : '';
      return [
        c.id, c.source_file, c.chunk_type, score,
        '"' + (c.content || '').replace(/"/g, '""').replace(/\n/g, '\\n') + '"',
      ].join(',');
    }).join('\n');
    content = header + rows; mime = 'text/csv'; ext = 'csv';
  } else if (format === 'markdown') {
    content = items.map(r => {
      const c = r.chunk || r;
      const title = (c.heading_hierarchy || []).join(' › ') || basename(c.source_file || 'chunk');
      return `## ${title}\n\n*Source: ${c.source_file} | Type: ${c.chunk_type}*\n\n${c.content}\n`;
    }).join('\n---\n\n');
    mime = 'text/markdown'; ext = 'md';
  } else {
    content = JSON.stringify(items, null, 2); mime = 'application/json'; ext = 'json';
  }
  const blob = new Blob([content], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `memtomem-export-${Date.now()}.${ext}`;
  a.click();
  URL.revokeObjectURL(a.href);
  showToast(`Exported ${items.length} chunk${items.length !== 1 ? 's' : ''} as ${ext.toUpperCase()}`, 'success');
}

qs('bulk-export-btn').addEventListener('click', () => {
  const ids = [...STATE.selectedIds];
  if (!ids.length) return;
  const format = qs('bulk-export-fmt').value;
  const selected = STATE.lastResults.filter(r => ids.includes(String(r.chunk.id)));
  downloadResults(selected, format);
});

qs('export-all-btn').addEventListener('click', () => {
  if (!STATE.lastResults.length) { showToast('No results to export', 'error'); return; }
  downloadResults(STATE.lastResults, qs('export-format').value);
});

// ---------------------------------------------------------------------------
// M3: Edit history (localStorage per chunk)
// ---------------------------------------------------------------------------

const _HIST_PREFIX = 'm2m-hist-';
const _HIST_MAX = 5;

function _getHistory(chunkId) {
  try { return JSON.parse(localStorage.getItem(_HIST_PREFIX + chunkId) || '[]'); }
  catch (_) { return []; }
}

function _pushHistory(chunkId, content) {
  const hist = _getHistory(chunkId);
  hist.unshift({ content, ts: new Date().toISOString() });
  hist.splice(_HIST_MAX);
  localStorage.setItem(_HIST_PREFIX + chunkId, JSON.stringify(hist));
}

function _updateHistoryBtn(chunkId) {
  const btn = qs('d-history-btn');
  _getHistory(chunkId).length ? show(btn) : hide(btn);
}

qs('d-history-btn').addEventListener('click', () => {
  const panel = qs('history-panel');
  if (!panel.hidden) { hide(panel); return; }
  hide(qs('similar-panel'));
  hide(qs('source-chunks-panel'));
  const hist = _getHistory(STATE.selectedChunkId);
  const list = qs('history-list');
  if (!hist.length) {
    list.innerHTML = '<div class="empty-state" style="height:40px"><span>No history</span></div>';
  } else {
    list.innerHTML = hist.map((h, i) => `
      <div class="similar-item" data-idx="${i}" style="cursor:pointer">
        <div class="similar-item-header">
          <span class="badge badge-gray" style="font-size:0.65rem">${relativeTime(h.ts)}</span>
          <span style="font-size:0.65rem;color:var(--muted)">${escapeHtml(new Date(h.ts).toLocaleString())}</span>
        </div>
        <div class="similar-item-snippet">${escapeHtml(truncate(h.content, 80))}</div>
      </div>`).join('');
    list.querySelectorAll('.similar-item').forEach(el => {
      el.addEventListener('click', () => {
        const h = hist[parseInt(el.dataset.idx)];
        const ops = diffLines(h.content, qs('d-editor').value);
        qs('d-diff').innerHTML = renderDiff(ops);
        show(qs('d-diff')); hide(qs('d-editor'));
        const diffBtn = qs('d-diff-btn');
        show(diffBtn); diffBtn.textContent = 'Edit'; diffBtn.dataset.mode = 'diff';
        showToast('Showing diff: history → current', 'info');
      });
    });
  }
  show(panel);
});

qs('history-close-btn').addEventListener('click', () => hide(qs('history-panel')));

// ---------------------------------------------------------------------------
// N1: Group by source toggle
// ---------------------------------------------------------------------------

// STATE.groupMode now in STATE

qs('group-toggle').addEventListener('click', () => {
  STATE.groupMode = !STATE.groupMode;
  qs('group-toggle').classList.toggle('btn-active', STATE.groupMode);
  if (STATE.lastResults.length) renderResults(STATE.lastResults);
});

// ---------------------------------------------------------------------------
// N2: Drag-to-index on search tab
// ---------------------------------------------------------------------------

{
  const _tab = qs('tab-search');
  let _dragCnt = 0;

  _tab.addEventListener('dragenter', e => {
    if (![...e.dataTransfer.items].some(i => i.kind === 'file')) return;
    e.preventDefault();
    _dragCnt++;
    show(qs('search-drop-overlay'));
  });

  _tab.addEventListener('dragleave', () => {
    _dragCnt = Math.max(0, _dragCnt - 1);
    if (_dragCnt === 0) hide(qs('search-drop-overlay'));
  });

  _tab.addEventListener('dragover', e => { e.preventDefault(); });

  _tab.addEventListener('drop', async e => {
    e.preventDefault();
    _dragCnt = 0;
    hide(qs('search-drop-overlay'));
    const files = [...e.dataTransfer.files].filter(f =>
      /\.(md|txt|py|js|ts|tsx|json|yaml|yml)$/i.test(f.name));
    if (!files.length) { showToast('Only .md/.txt/.py/.js/.ts/.json/.yaml files accepted', 'error'); return; }
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    showToast(`Indexing ${files.length} file${files.length > 1 ? 's' : ''}…`, 'info');
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail);
      }
      const data = await res.json();
      const chunks = (data.results || []).reduce((s, r) => s + (r.indexed_chunks || 0), 0);
      showToast(`Indexed ${files.length} file${files.length > 1 ? 's' : ''} → ${chunks} chunks`, 'success');
      _markDataStale();
      loadSourceFilter();
      loadStats();
    } catch (err) {
      showToast('Upload failed: ' + err.message, 'error');
    }
  });
}

// ---------------------------------------------------------------------------
// Namespace filter dropdowns + Namespaces tab
// ---------------------------------------------------------------------------

async function loadNamespaceDropdowns() {
  try {
    const data = await api('GET', '/api/namespaces');
    const namespaces = data.namespaces || [];
    ['ns-filter', 'tl-namespace', 'exp-namespace'].forEach(id => {
      const sel = document.getElementById(id);
      if (!sel) return;
      const current = sel.value;
      // Keep first option (All)
      while (sel.options.length > 1) sel.remove(1);
      namespaces.forEach(ns => {
        const opt = document.createElement('option');
        opt.value = ns.namespace;
        opt.textContent = `${ns.namespace} (${ns.chunk_count})`;
        sel.appendChild(opt);
      });
      if (current) {
        sel.value = current;
        if (sel.value !== current) sel.value = '';
      }
    });
  } catch (_) { /* non-critical */ }
}

// Load on startup and when switching to related tabs
loadNamespaceDropdowns();

// Namespaces tab
qs('ns-refresh-btn').addEventListener('click', loadNamespacesTab);

async function loadNamespacesTab() {
  const list = qs('ns-list');
  list.innerHTML = '<div class="loading-panel"><div class="spinner-panel"></div></div>';

  try {
    const data = await api('GET', '/api/namespaces');
    const namespaces = data.namespaces || [];
    if (!namespaces.length) {
      list.innerHTML = emptyState('📁', 'No namespaces yet', 'Index files with a namespace to get started');
      return;
    }
    list.innerHTML = '';
    const defaultNs = STATE.serverConfig?.namespace?.default_namespace || 'default';
    namespaces.forEach(ns => {
      const card = document.createElement('div');
      card.className = 'ns-card';

      const dot = document.createElement('span');
      dot.className = 'ns-color-dot';
      dot.style.backgroundColor = ns.color || 'var(--muted)';

      const name = document.createElement('span');
      name.className = 'ns-name';
      name.textContent = ns.namespace;
      if (ns.namespace === defaultNs) {
        const badge = document.createElement('span');
        badge.className = 'badge badge-blue';
        badge.style.marginLeft = '6px';
        badge.style.fontSize = '0.7rem';
        badge.textContent = 'Default';
        name.appendChild(badge);
      }

      const desc = document.createElement('span');
      desc.className = 'ns-desc';
      desc.textContent = ns.description || '';

      const count = document.createElement('span');
      count.className = 'ns-count';
      count.textContent = `${ns.chunk_count} chunks`;

      const actions = document.createElement('span');
      actions.className = 'ns-actions';

      const editBtn = document.createElement('button');
      editBtn.className = 'btn-ghost btn-xs';
      editBtn.textContent = 'Edit';
      editBtn.addEventListener('click', () => editNamespaceMeta(ns, card));

      const renameBtn = document.createElement('button');
      renameBtn.className = 'btn-ghost btn-xs';
      renameBtn.textContent = 'Rename';
      renameBtn.addEventListener('click', () => renameNamespace(ns.namespace));

      const delBtn = document.createElement('button');
      delBtn.className = 'btn-danger btn-xs';
      delBtn.textContent = 'Delete';
      delBtn.addEventListener('click', () => deleteNamespace(ns.namespace));

      const sourcesBtn = document.createElement('button');
      sourcesBtn.className = 'btn-ghost btn-xs';
      sourcesBtn.textContent = 'Sources';
      sourcesBtn.title = `View sources in namespace '${ns.namespace}'`;
      sourcesBtn.addEventListener('click', () => navigateToSourcesByNs(ns.namespace));

      actions.appendChild(sourcesBtn);
      actions.appendChild(editBtn);
      actions.appendChild(renameBtn);
      actions.appendChild(delBtn);

      card.appendChild(dot);
      card.appendChild(name);
      card.appendChild(desc);
      card.appendChild(count);
      card.appendChild(actions);
      list.appendChild(card);
    });
  } catch (err) {
    list.innerHTML = `<div class="status-msg err">${escapeHtml(err.message)}</div>`;
  }
}

function editNamespaceMeta(ns, card) {
  // Close any existing edit form
  const existing = card.querySelector('.ns-edit-form');
  if (existing) { existing.remove(); return; }

  const form = document.createElement('div');
  form.className = 'ns-edit-form';
  form.innerHTML = `
    <div class="ns-edit-row">
      <label>Description</label>
      <input type="text" class="ns-edit-desc" value="${escapeAttr(ns.description || '')}" placeholder="Add a description..." />
    </div>
    <div class="ns-edit-row">
      <label>Color</label>
      <input type="color" class="ns-edit-color" value="${ns.color || '#808080'}" />
      <button class="btn-ghost btn-xs ns-edit-reset-color">Reset</button>
    </div>
    <div class="ns-edit-actions">
      <button class="btn-ghost btn-xs ns-edit-cancel">Cancel</button>
      <button class="btn-primary btn-xs ns-edit-save">Save</button>
    </div>
  `;

  form.querySelector('.ns-edit-cancel').addEventListener('click', () => form.remove());
  form.querySelector('.ns-edit-reset-color').addEventListener('click', () => {
    form.querySelector('.ns-edit-color').value = '#808080';
  });
  form.querySelector('.ns-edit-save').addEventListener('click', async () => {
    const desc = form.querySelector('.ns-edit-desc').value.trim();
    const colorVal = form.querySelector('.ns-edit-color').value;
    const body = { description: desc };
    // Only send color if not default gray
    body.color = colorVal === '#808080' ? '' : colorVal;
    try {
      await api('PATCH', `/api/namespaces/${encodeURIComponent(ns.namespace)}`, body);
      showToast('Namespace updated', 'success');
      loadNamespacesTab();
      loadNamespaceDropdowns();
    } catch (err) {
      showToast('Failed: ' + err.message, 'error');
    }
  });

  card.appendChild(form);
  form.querySelector('.ns-edit-desc').focus();
}

async function renameNamespace(oldName) {
  const newName = prompt('New namespace name:', oldName);
  if (!newName || newName === oldName) return;
  try {
    await api('POST', `/api/namespaces/${encodeURIComponent(oldName)}/rename`, { new_name: newName });
    showToast(`Renamed '${oldName}' → '${newName}'`, 'success');
    STATE.lastResults.forEach(r => { if (r.chunk.namespace === oldName) r.chunk.namespace = newName; });
    if (STATE.lastResults.length) renderResults(STATE.lastResults);
    loadNamespacesTab();
    loadNamespaceDropdowns();
  } catch (err) {
    showToast('Rename failed: ' + err.message, 'error');
  }
}

async function deleteNamespace(name) {
  if (!confirm(`Delete all chunks in namespace '${name}'? This cannot be undone.`)) return;
  try {
    const data = await api('DELETE', `/api/namespaces/${encodeURIComponent(name)}`);
    showToast(`Deleted ${data.deleted} chunks from '${name}'`, 'success');
    STATE.lastResults = STATE.lastResults.filter(r => r.chunk.namespace !== name);
    renderResults(STATE.lastResults);
    _markDataStale();
    loadNamespacesTab();
    loadNamespaceDropdowns();
    loadStats();
  } catch (err) {
    showToast('Delete failed: ' + err.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Phase 1a: Saved Searches — Star button + history dropdown integration
// ═══════════════════════════════════════════════════════════════════════════

qs('save-star-btn').addEventListener('click', () => {
  const q = qs('search-input').value.trim();
  if (!q) { showToast('Enter a query first', 'error'); return; }
  const list = _getSavedQueries();
  const exists = list.some(s => s.query === q);
  if (exists) {
    // Unsave
    const idx = list.findIndex(s => s.query === q);
    list.splice(idx, 1);
    _setSavedQueries(list);
    qs('save-star-btn').textContent = '☆';
    qs('save-star-btn').classList.remove('starred');
    showToast('Search removed from saved', 'info');
  } else {
    const name = q.length > 40 ? q.slice(0, 40) + '…' : q;
    const nsFilter = qs('ns-filter').value;
    const tf = qs('tag-filter').value.trim();
    const topK = parseInt(qs('top-k').value, 10);
    list.push({ name, query: q, namespace: nsFilter, tags: tf, topK, ts: new Date().toISOString() });
    _setSavedQueries(list);
    qs('save-star-btn').textContent = '★';
    qs('save-star-btn').classList.add('starred');
    showToast(`Saved: "${name}"`, 'success');
  }
  _renderSavedSelect();
  _renderSavedBar();
});

// Update star button state when search input changes
qs('search-input').addEventListener('input', () => {
  const q = qs('search-input').value.trim();
  const list = _getSavedQueries();
  const isSaved = list.some(s => s.query === q);
  qs('save-star-btn').textContent = isSaved ? '★' : '☆';
  qs('save-star-btn').classList.toggle('starred', isSaved);
});

// ═══════════════════════════════════════════════════════════════════════════
// Phase 1b: Related Chunks — auto-load on detail view
// ═══════════════════════════════════════════════════════════════════════════

let _relatedResults = [];

const _loadRelated = debounce(async function _loadRelatedChunks(chunkId, content) {
  const panel = qs('related-panel');
  const list = qs('related-list');
  const preview = qs('related-preview');
  const chevron = qs('related-chevron');
  const countEl = qs('related-count');
  _relatedResults = [];
  hide(list); hide(preview);
  chevron.classList.remove('open');
  countEl.textContent = '';
  show(panel);
  try {
    const query = content.slice(0, 300);
    const params = new URLSearchParams({ q: query, top_k: 6 });
    const data = await api('GET', `/api/search?${params}`);
    _relatedResults = (data.results || []).filter(r => String(r.chunk.id) !== String(chunkId)).slice(0, 5);
    countEl.textContent = _relatedResults.length || '';
    if (!_relatedResults.length) {
      countEl.textContent = '0';
    }
    _renderRelatedList();
  } catch {
    countEl.textContent = '!';
  }
}, 500);

function _renderRelatedList() {
  const list = qs('related-list');
  if (!_relatedResults.length) {
    list.innerHTML = '<div class="empty-state" style="height:40px"><span>No related chunks</span></div>';
    return;
  }
  list.innerHTML = _relatedResults.map(r => `
    <div class="similar-item" data-id="${escapeAttr(String(r.chunk.id))}">
      <div class="similar-item-header">
        <span class="result-type-dot" style="background:${fileTypeColor(r.chunk.source_file || '')}"></span>
        <span style="font-size:0.78rem;font-family:var(--mono)">${escapeHtml(basename(r.chunk.source_file || ''))}</span>
        <span class="score-badge">${r.score.toFixed(3)}</span>
      </div>
      <div class="similar-item-snippet">${escapeHtml(truncate(r.chunk.content, 90))}</div>
    </div>
  `).join('');
  list.querySelectorAll('.similar-item').forEach(el => {
    el.addEventListener('click', () => {
      const match = _relatedResults.find(r => String(r.chunk.id) === el.dataset.id);
      if (match) _showRelatedPreview(match, el);
    });
  });
}

function _showRelatedPreview(r, itemEl) {
  const preview = qs('related-preview');
  const src = r.chunk.source_file || '';
  const isMarkdown = src.endsWith('.md');
  const rendered = isMarkdown && typeof marked !== 'undefined'
    ? DOMPurify.sanitize(marked.parse(r.chunk.content))
    : `<pre style="white-space:pre-wrap;font-size:0.82rem">${escapeHtml(r.chunk.content)}</pre>`;

  preview.innerHTML = `
    <div class="related-preview-header">
      <span style="font-family:var(--mono)">${escapeHtml(basename(src))}</span>
      <span class="score-badge">${r.score.toFixed(3)}</span>
      <span style="color:var(--muted);font-size:0.72rem">L${r.chunk.start_line}–${r.chunk.end_line}</span>
    </div>
    <div class="related-preview-content${isMarkdown ? ' md-preview' : ''}">${rendered}</div>
    <div class="related-preview-actions">
      <button class="btn-primary btn-xs" id="related-go-btn">Go to this chunk</button>
      <button class="btn-ghost btn-xs" id="related-preview-close">Close</button>
    </div>`;
  show(preview);
  preview.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  // Highlight active item
  qs('related-list').querySelectorAll('.similar-item').forEach(el => el.classList.remove('active'));
  if (itemEl) itemEl.classList.add('active');

  qs('related-go-btn').addEventListener('click', () => {
    document.querySelectorAll('.result-item').forEach(i => i.classList.remove('selected'));
    showDetail(r);
  });
  qs('related-preview-close').addEventListener('click', () => {
    hide(preview);
    if (itemEl) itemEl.classList.remove('active');
  });
}

// Toggle related list open/close
qs('related-toggle').addEventListener('click', () => {
  const list = qs('related-list');
  const chevron = qs('related-chevron');
  if (list.hidden) {
    show(list);
    chevron.classList.add('open');
  } else {
    hide(list);
    hide(qs('related-preview'));
    chevron.classList.remove('open');
  }
});

// Hook into showDetail to auto-load related chunks
const _origShowDetail = showDetail;
showDetail = function(r) {
  _origShowDetail(r);
  _loadRelated(r.chunk.id, r.chunk.content);
};

// Compare removed — use dedup tab for chunk comparison

// ═══════════════════════════════════════════════════════════════════════════
// Phase 2b: Dashboard Charts — D3 Donut + GitHub-style Heatmap
// ═══════════════════════════════════════════════════════════════════════════

// Override _renderNsChart to use D3 donut
const _origRenderNsChart = _renderNsChart;
_renderNsChart = function(namespaces) {
  const chart = qs('home-ns-chart');
  if (!namespaces.length || typeof d3 === 'undefined') {
    _origRenderNsChart(namespaces);
    return;
  }

  const sorted = [...namespaces].sort((a, b) => b.chunk_count - a.chunk_count).slice(0, 8);
  const total = sorted.reduce((s, ns) => s + ns.chunk_count, 0);
  const palette = ['#6c8fff', '#4caf7d', '#e0a800', '#a29bfe', '#e17055', '#00cec9', '#fd79a8', '#636e72'];

  const size = 130, radius = 55;
  chart.innerHTML = '';
  chart.classList.remove('home-bar-chart');
  chart.classList.add('home-ns-d3');

  const svg = d3.select(chart).append('svg')
    .attr('viewBox', `0 0 ${size} ${size}`)
    .attr('preserveAspectRatio', 'xMidYMid meet')
    .style('max-width', `${size}px`)
    .style('margin', '0 auto')
    .style('display', 'block');

  const g = svg.append('g').attr('transform', `translate(${size / 2},${size / 2})`);

  const pie = d3.pie().value(d => d.chunk_count).sort(null);
  const arc = d3.arc().innerRadius(radius * 0.58).outerRadius(radius);

  g.selectAll('path').data(pie(sorted)).join('path')
    .attr('d', arc)
    .attr('fill', (d, i) => d.data.color || palette[i % palette.length])
    .attr('stroke', 'var(--surface)')
    .attr('stroke-width', 2)
    .style('cursor', 'pointer')
    .on('mouseover', function(event, d) {
      d3.select(this).attr('opacity', 0.8);
      centerText.text(`${d.data.chunk_count}`);
    })
    .on('mouseout', function() {
      d3.select(this).attr('opacity', 1);
      centerText.text(total);
    })
    .on('click', (event, d) => {
      navigateToSourcesByNs(d.data.namespace);
    });

  const centerText = g.append('text')
    .attr('class', 'd3-donut-center')
    .text(total);

  // Legend below the chart as a wrapping row
  const legend = document.createElement('div');
  legend.className = 'ns-legend-row';
  legend.innerHTML = sorted.map((ns, i) => {
    const c = ns.color || palette[i % palette.length];
    return `<span class="ns-legend-item"><span class="ns-legend-dot" style="background:${c}"></span>${escapeHtml(truncate(ns.namespace, 18))} ${ns.chunk_count}</span>`;
  }).join('');
  chart.appendChild(legend);
};

// Override _renderActivityMap to use GitHub-style contribution grid
const _origRenderActivity = _renderActivityMap;
_renderActivityMap = function(sources) {
  const map = qs('home-activity-map');
  if (typeof d3 === 'undefined') { _origRenderActivity(sources); return; }

  const now = new Date();
  // Fill available card width: ~14px cell + 3px gap → aim for card width
  // 52 weeks ≈ 900px, good fill for standard card
  const days = 364; // 52 weeks
  const counts = [];

  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    const key = d.toISOString().slice(0, 10);
    let cnt = 0;
    sources.forEach(s => {
      if (s.last_indexed_at && s.last_indexed_at.slice(0, 10) === key) cnt++;
    });
    counts.push({ date: key, count: cnt, weekday: d.getDay() });
  }

  const maxCount = Math.max(1, ...counts.map(c => c.count));
  const weekdays = ['', 'M', '', 'W', '', 'F', ''];
  const firstDow = counts[0]?.weekday || 0;
  const numWeeks = Math.ceil((counts.length + firstDow) / 7);

  // Grid: rows=7, cols=numWeeks. Cells fill available width via minmax(0,1fr).
  let html = `<div class="home-heatmap-grid" style="grid-template-columns: 16px repeat(${numWeeks}, minmax(0,1fr))">`;

  for (let dow = 0; dow < 7; dow++) {
    html += `<div class="heatmap-weekday">${weekdays[dow]}</div>`;
    for (let w = 0; w < numWeeks; w++) {
      const idx = w * 7 + dow - firstDow;
      if (idx < 0 || idx >= counts.length) {
        html += '<div class="heatmap-cell heatmap-empty"></div>';
      } else {
        const c = counts[idx];
        const intensity = c.count === 0 ? 0 : 0.25 + (c.count / maxCount) * 0.75;
        const bg = c.count === 0 ? 'var(--border)' : `rgba(var(--accent-rgb), ${intensity})`;
        html += `<div class="heatmap-cell" style="background:${bg}" data-tooltip="${c.date}: ${c.count} files"></div>`;
      }
    }
  }
  html += '</div>';
  map.innerHTML = html;
};

// ═══════════════════════════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════════════════════════
// Phase 4a: Command Palette (Cmd+K)
// ═══════════════════════════════════════════════════════════════════════════

function _buildCommands() {
  const tabs = [
    { icon: '🏠', label: 'Go to Home', action: () => activateTab('home'), hint: '1' },
    { icon: '🔍', label: 'Go to Search', action: () => activateTab('search'), hint: '2' },
    { icon: '📁', label: 'Go to Sources', action: () => activateTab('sources'), hint: '3' },
    { icon: '📥', label: 'Go to Index', action: () => activateTab('index'), hint: '4' },
    { icon: '🏷', label: 'Go to Tags', action: () => activateTab('tags'), hint: '5' },
    { icon: '📅', label: 'Go to Timeline', action: () => activateTab('timeline'), hint: '6' },
    { icon: '⚙', label: 'Go to Settings', action: () => activateTab('settings'), hint: '7' },
  ];

  const settings = [
    { icon: '🔧', label: 'Open Config', action: () => { activateTab('settings'); switchSettingsSection('config'); } },
    { icon: '📋', label: 'Open Dedup', action: () => { activateTab('settings'); switchSettingsSection('dedup'); } },
    { icon: '📦', label: 'Open Export/Import', action: () => { activateTab('settings'); switchSettingsSection('export'); } },
  ];

  const actions = [
    { icon: '🔍', label: 'Focus Search', action: () => { activateTab('search'); qs('search-input').focus(); }, hint: '/' },
    { icon: '🌗', label: 'Toggle Theme', action: () => qs('theme-toggle').click() },
    { icon: '⌨', label: 'Keyboard Shortcuts', action: () => show(qs('shortcuts-modal')), hint: '?' },
  ];

  // Dynamic: recent sources
  const sources = (STATE.allSources || []).slice(0, 5).map(s => ({
    icon: '📄', label: `Open ${basename(s.path)}`, action: () => _navigateToSource(s.path), hint: 'source',
  }));

  return [
    { group: 'Navigation', items: tabs },
    { group: 'Settings', items: settings },
    { group: 'Actions', items: actions },
    ...(sources.length ? [{ group: 'Recent Sources', items: sources }] : []),
  ];
}

function _openCmdPalette() {
  STATE.cmdPaletteOpen = true;
  const modal = qs('cmd-palette');
  const input = qs('cmd-input');
  const list = qs('cmd-list');
  show(modal);
  input.value = '';
  input.focus();
  _renderCmdList('');
}

function _closeCmdPalette() {
  STATE.cmdPaletteOpen = false;
  hide(qs('cmd-palette'));
}

function _renderCmdList(filter) {
  const list = qs('cmd-list');
  const commands = _buildCommands();
  const lower = filter.toLowerCase();
  let html = '';
  let firstId = null;
  let idx = 0;

  commands.forEach(group => {
    const filtered = group.items.filter(cmd => cmd.label.toLowerCase().includes(lower));
    if (!filtered.length) return;
    html += `<div class="cmd-group-label">${escapeHtml(group.group)}</div>`;
    filtered.forEach(cmd => {
      const id = `cmd-${idx}`;
      if (!firstId) firstId = id;
      html += `<div class="cmd-item" id="${id}" data-idx="${idx}">
        <span class="cmd-item-icon">${cmd.icon}</span>
        <span class="cmd-item-label">${escapeHtml(cmd.label)}</span>
        ${cmd.hint ? `<span class="cmd-item-hint">${escapeHtml(cmd.hint)}</span>` : ''}
      </div>`;
      idx++;
    });
  });

  list.innerHTML = html || '<div class="cmd-item" style="color:var(--muted)">No matching commands</div>';

  // Store flat command list for execution
  list._commands = [];
  commands.forEach(group => {
    group.items.filter(cmd => cmd.label.toLowerCase().includes(lower)).forEach(cmd => {
      list._commands.push(cmd);
    });
  });

  // Highlight first
  if (firstId) {
    list.querySelector(`#${firstId}`)?.classList.add('cmd-active');
  }

  // Click handlers
  list.querySelectorAll('.cmd-item[data-idx]').forEach(el => {
    el.addEventListener('click', () => {
      const cmd = list._commands[parseInt(el.dataset.idx)];
      _closeCmdPalette();
      if (cmd) cmd.action();
    });
  });
}

qs('cmd-input').addEventListener('input', () => _renderCmdList(qs('cmd-input').value));

qs('cmd-input').addEventListener('keydown', e => {
  const list = qs('cmd-list');
  const items = [...list.querySelectorAll('.cmd-item[data-idx]')];
  const active = list.querySelector('.cmd-active');
  const idx = active ? items.indexOf(active) : -1;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (active) active.classList.remove('cmd-active');
    const next = items[(idx + 1) % items.length];
    if (next) { next.classList.add('cmd-active'); next.scrollIntoView({ block: 'nearest' }); }
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (active) active.classList.remove('cmd-active');
    const prev = items[(idx - 1 + items.length) % items.length];
    if (prev) { prev.classList.add('cmd-active'); prev.scrollIntoView({ block: 'nearest' }); }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (active) {
      const cmd = list._commands[parseInt(active.dataset.idx)];
      _closeCmdPalette();
      if (cmd) cmd.action();
    }
  } else if (e.key === 'Escape') {
    _closeCmdPalette();
  }
});

qs('cmd-palette').addEventListener('click', e => {
  if (e.target === qs('cmd-palette')) _closeCmdPalette();
});

// ═══════════════════════════════════════════════════════════════════════════
// Phase 4b: Enhanced Keyboard Navigation
// ═══════════════════════════════════════════════════════════════════════════

// Extend existing keydown handler — we add a new listener that fires before
// We need to intercept certain keys
document.addEventListener('keydown', e => {
  // Cmd+K / Ctrl+K: Command Palette
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault();
    if (STATE.cmdPaletteOpen) _closeCmdPalette();
    else _openCmdPalette();
    return;
  }

  // Skip if in text field (except Esc / Cmd+K)
  if (_isTextField(e.target)) return;

  // Tab number shortcuts: 1-7
  const tabMap = { '1': 'home', '2': 'search', '3': 'sources', '4': 'index', '5': 'tags', '6': 'timeline', '7': 'settings' };
  if (tabMap[e.key] && !e.metaKey && !e.ctrlKey && !e.altKey) {
    e.preventDefault();
    activateTab(tabMap[e.key]);
    return;
  }

  // Enter: open selected result detail
  if (e.key === 'Enter') {
    const selected = document.querySelector('.result-item.selected');
    if (selected) { e.preventDefault(); selected.click(); }
    return;
  }

  // Backspace: go back from detail to results
  if (e.key === 'Backspace') {
    if (window.innerWidth <= 768) {
      const layout = document.querySelector('.results-layout');
      if (layout && layout.classList.contains('mobile-detail')) {
        e.preventDefault();
        layout.classList.remove('mobile-detail');
        return;
      }
    }
    if (qs('detail-view') && !qs('detail-view').hidden) {
      e.preventDefault();
      clearDetail();
      return;
    }
  }

  // g then s: Go to Sources (vim-style 2-key)
  if (e.key === 'g' && !STATE.pendingGKey) {
    STATE.pendingGKey = true;
    setTimeout(() => { STATE.pendingGKey = false; }, 1000);
    return;
  }
  if (STATE.pendingGKey) {
    STATE.pendingGKey = false;
    if (e.key === 's') { e.preventDefault(); activateTab('sources'); return; }
    if (e.key === 'h') { e.preventDefault(); activateTab('home'); return; }
    if (e.key === 't') { e.preventDefault(); activateTab('tags'); return; }
    if (e.key === 'i') { e.preventDefault(); activateTab('index'); return; }
  }
}, true);

// ═══════════════════════════════════════════════════════════════════════════
// Phase 4c: Mobile Touch Gestures — swipe navigation in search results
// ═══════════════════════════════════════════════════════════════════════════

(function initTouchGestures() {
  const detailPanel = qs('detail-panel');
  if (!detailPanel) return;

  detailPanel.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    STATE.touchStartX = e.touches[0].clientX;
    STATE.touchStartY = e.touches[0].clientY;
  }, { passive: true });

  detailPanel.addEventListener('touchend', e => {
    if (e.changedTouches.length !== 1) return;
    const dx = e.changedTouches[0].clientX - STATE.touchStartX;
    const dy = e.changedTouches[0].clientY - STATE.touchStartY;
    if (Math.abs(dy) > Math.abs(dx)) return; // vertical scroll, not swipe
    if (Math.abs(dx) < 50) return; // too short

    const items = [...document.querySelectorAll('.result-item')];
    if (!items.length) return;
    const cur = document.querySelector('.result-item.selected');
    const idx = cur ? items.indexOf(cur) : -1;

    if (dx > 0 && idx > 0) {
      // Swipe right → previous
      items[idx - 1].click();
      items[idx - 1].scrollIntoView({ block: 'nearest' });
    } else if (dx < 0 && idx < items.length - 1) {
      // Swipe left → next
      items[idx + 1].click();
      items[idx + 1].scrollIntoView({ block: 'nearest' });
    }
  }, { passive: true });
})();

// ═══════════════════════════════════════════════════════════════════════════
// Hook: Integrate treemap into dashboard
// ═══════════════════════════════════════════════════════════════════════════

// Treemap is now rendered inline in loadDashboard() — no monkey-patch needed.

// ---------------------------------------------------------------------------
// Tab Help System (A + C)
// ---------------------------------------------------------------------------

const _HELP_TABS = ['search', 'sources', 'tags', 'timeline'];
const _HELP_STORAGE_KEY = 'm2m-help-dismissed';

function _getHelpDismissed() {
  try { return JSON.parse(localStorage.getItem(_HELP_STORAGE_KEY) || '{}'); } catch { return {}; }
}

function _initTabHelp() {
  // Restore global visibility
  const vis = localStorage.getItem(_HELP_VISIBLE_KEY);
  STATE.helpVisible = vis !== 'false';  // default true on first visit
  if (!STATE.helpVisible) document.body.classList.add('help-hidden');

  const dismissed = _getHelpDismissed();
  _HELP_TABS.forEach(tab => {
    const bar = qs('help-' + tab);
    if (!bar) return;
    if (!dismissed[tab]) show(bar);
  });

  // Dismiss buttons
  document.querySelectorAll('.tab-help-bar-dismiss').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.getAttribute('data-help-tab');
      const bar = qs('help-' + tab);
      if (bar) hide(bar);
      const d = _getHelpDismissed();
      d[tab] = true;
      localStorage.setItem(_HELP_STORAGE_KEY, JSON.stringify(d));
    });
  });

  // Header toggle button
  const toggleBtn = qs('help-toggle');
  if (toggleBtn) {
    toggleBtn.setAttribute('aria-pressed', String(STATE.helpVisible));
    toggleBtn.addEventListener('click', toggleHelp);
  }
}

function toggleHelp() {
  STATE.helpVisible = !STATE.helpVisible;
  document.body.classList.toggle('help-hidden', !STATE.helpVisible);
  localStorage.setItem(_HELP_VISIBLE_KEY, String(STATE.helpVisible));
  const toggleBtn = qs('help-toggle');
  if (toggleBtn) toggleBtn.setAttribute('aria-pressed', String(STATE.helpVisible));
  // When re-showing, restore non-dismissed bars
  if (STATE.helpVisible) {
    const dismissed = _getHelpDismissed();
    _HELP_TABS.forEach(tab => {
      const bar = qs('help-' + tab);
      if (bar && !dismissed[tab]) show(bar);
    });
  }
}

// ── Harness: Sessions ──

async function loadHarnessSessions() {
  const list = qs('sessions-list');
  list.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    const data = await api('GET', '/api/sessions?limit=50');
    if (!data.sessions.length) {
      list.innerHTML = '<div class="empty-state">No sessions recorded yet</div>';
      return;
    }
    list.innerHTML = '<table class="harness-table"><thead><tr>' +
      '<th>ID</th><th>Agent</th><th>Namespace</th><th>Started</th><th>Ended</th><th>Summary</th><th></th>' +
      '</tr></thead><tbody>' +
      data.sessions.map(s => {
        const ended = s.ended_at ? relativeTime(s.ended_at) : '<span class="badge badge-active">active</span>';
        const summary = s.summary ? truncate(s.summary, 60) : '—';
        return `<tr>
          <td class="mono">${s.id.slice(0, 8)}</td>
          <td>${s.agent_id}</td>
          <td>${s.namespace}</td>
          <td>${relativeTime(s.started_at)}</td>
          <td>${ended}</td>
          <td>${summary}</td>
          <td><button class="btn-ghost btn-xs" onclick="showSessionEvents('${s.id}')">Events</button></td>
        </tr>`;
      }).join('') +
      '</tbody></table>';
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

async function showSessionEvents(sessionId) {
  const panel = qs('session-events-panel');
  const list = qs('session-events-list');
  qs('session-events-title').textContent = `Events: ${sessionId.slice(0, 8)}...`;
  show(panel);
  list.innerHTML = '<div class="spinner-panel"></div>';
  try {
    const data = await api('GET', `/api/sessions/${sessionId}/events`);
    if (!data.events.length) {
      list.innerHTML = '<div class="empty-state">No events</div>';
      return;
    }
    list.innerHTML = data.events.map(e =>
      `<div class="harness-event">
        <span class="badge badge-${e.event_type}">${e.event_type}</span>
        <span class="harness-event-content">${truncate(e.content, 120)}</span>
        <span class="muted-sm">${relativeTime(e.created_at)}</span>
      </div>`
    ).join('');
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

qs('session-events-close')?.addEventListener('click', () => hide(qs('session-events-panel')));
qs('sessions-refresh-btn')?.addEventListener('click', loadHarnessSessions);

// ── Harness: Working Memory (Scratch) ──

async function loadHarnessScratch() {
  const list = qs('scratch-list');
  list.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    const data = await api('GET', '/api/scratch');
    if (!data.entries.length) {
      list.innerHTML = '<div class="empty-state">No working memory entries</div>';
      return;
    }
    list.innerHTML = '<table class="harness-table"><thead><tr>' +
      '<th>Key</th><th>Value</th><th>Session</th><th>TTL</th><th>Promoted</th><th></th>' +
      '</tr></thead><tbody>' +
      data.entries.map(e => {
        const ttl = e.expires_at ? relativeTime(e.expires_at) : '—';
        const promoted = e.promoted ? '<span class="badge badge-promoted">yes</span>' : '—';
        const sess = e.session_id ? e.session_id.slice(0, 8) : '—';
        return `<tr>
          <td class="mono">${e.key}</td>
          <td>${truncate(e.value, 80)}</td>
          <td class="mono">${sess}</td>
          <td>${ttl}</td>
          <td>${promoted}</td>
          <td>
            <button class="btn-ghost btn-xs btn-danger-text" onclick="deleteScratchEntry('${e.key}')">Delete</button>
            ${!e.promoted ? `<button class="btn-ghost btn-xs" onclick="promoteScratchEntry('${e.key}')">Promote</button>` : ''}
          </td>
        </tr>`;
      }).join('') +
      '</tbody></table>';
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

async function addScratchEntry() {
  const key = qs('scratch-key').value.trim();
  const value = qs('scratch-value').value.trim();
  const ttl = parseInt(qs('scratch-ttl').value) || null;
  if (!key || !value) return;
  try {
    await api('POST', '/api/scratch', { key, value, ttl_minutes: ttl });
    qs('scratch-key').value = '';
    qs('scratch-value').value = '';
    qs('scratch-ttl').value = '';
    loadHarnessScratch();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function deleteScratchEntry(key) {
  try {
    await api('DELETE', `/api/scratch/${encodeURIComponent(key)}`);
    loadHarnessScratch();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function promoteScratchEntry(key) {
  try {
    await api('POST', `/api/scratch/${encodeURIComponent(key)}/promote`, {});
    toast('Promoted to long-term memory', 'success');
    loadHarnessScratch();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

qs('scratch-add-btn')?.addEventListener('click', addScratchEntry);
qs('scratch-refresh-btn')?.addEventListener('click', loadHarnessScratch);

// ── Harness: Procedures ──

async function loadHarnessProcedures() {
  const list = qs('procedures-list');
  list.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    const data = await api('GET', '/api/procedures');
    if (!data.procedures.length) {
      list.innerHTML = '<div class="empty-state">No procedures saved yet. Use <code>mem_procedure_save</code> to create one.</div>';
      return;
    }
    list.innerHTML = data.procedures.map(p => {
      const tags = (p.tags || []).map(t => `<span class="tag-pill">${t}</span>`).join(' ');
      return `<div class="harness-procedure card">
        <div class="harness-procedure-header">
          <span class="mono">${p.id.slice(0, 8)}</span>
          <span class="muted-sm">${p.namespace}</span>
          ${tags}
        </div>
        <pre class="harness-procedure-content">${p.content}</pre>
      </div>`;
    }).join('');
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

qs('procedures-refresh-btn')?.addEventListener('click', loadHarnessProcedures);

// ── Harness: Health Report ──

async function loadHarnessHealth() {
  const report = qs('health-report');
  report.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    const d = await api('GET', '/api/eval');
    report.innerHTML = `
      <div class="health-grid">
        <div class="health-card card">
          <div class="health-card-title">Access Coverage</div>
          <div class="health-gauge">
            <div class="health-gauge-bar" style="width:${d.access_coverage.pct}%"></div>
          </div>
          <div class="health-card-detail">${d.access_coverage.accessed} / ${d.access_coverage.total} chunks (${d.access_coverage.pct}%)</div>
        </div>
        <div class="health-card card">
          <div class="health-card-title">Tag Coverage</div>
          <div class="health-gauge">
            <div class="health-gauge-bar" style="width:${d.tag_coverage.pct}%"></div>
          </div>
          <div class="health-card-detail">${d.tag_coverage.tagged} / ${d.tag_coverage.total} chunks (${d.tag_coverage.pct}%)</div>
        </div>
        <div class="health-card card">
          <div class="health-card-title">Dead Memories</div>
          <div class="health-gauge">
            <div class="health-gauge-bar health-gauge-warn" style="width:${d.dead_memories_pct}%"></div>
          </div>
          <div class="health-card-detail">${d.dead_memories_pct}% never accessed</div>
        </div>
        <div class="health-card card">
          <div class="health-card-title">Sessions</div>
          <div class="stat-value">${d.sessions.total}</div>
          <div class="health-card-detail">${d.sessions.active} active</div>
        </div>
        <div class="health-card card">
          <div class="health-card-title">Working Memory</div>
          <div class="stat-value">${d.working_memory.total}</div>
          <div class="health-card-detail">${d.working_memory.promoted} promoted</div>
        </div>
        <div class="health-card card">
          <div class="health-card-title">Cross-References</div>
          <div class="stat-value">${d.cross_references}</div>
        </div>
      </div>
      ${d.top_accessed.length ? `
      <div class="health-section">
        <h3>Top Accessed</h3>
        <table class="harness-table"><thead><tr><th>ID</th><th>Content</th><th>Count</th></tr></thead>
        <tbody>${d.top_accessed.map(r => `<tr><td class="mono">${r.id.slice(0,8)}</td><td>${truncate(r.content, 80)}</td><td>${r.access_count}</td></tr>`).join('')}</tbody></table>
      </div>` : ''}
      ${d.namespace_distribution.length ? `
      <div class="health-section">
        <h3>Namespace Distribution</h3>
        <table class="harness-table"><thead><tr><th>Namespace</th><th>Chunks</th></tr></thead>
        <tbody>${d.namespace_distribution.map(r => `<tr><td>${r.namespace}</td><td>${r.count}</td></tr>`).join('')}</tbody></table>
      </div>` : ''}
    `;
  } catch (e) {
    report.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

qs('health-refresh-btn')?.addEventListener('click', loadHarnessHealth);

// =====================================================================
// HEALTH WATCHDOG PANEL
// =====================================================================

function _wdDot(status) {
  const cls = status === 'ok' ? 'health-ok' : status === 'warning' ? 'health-slow' : 'health-down';
  return `<span class="health-dot ${cls}"></span>`;
}

function _wdLabel(status) {
  return status === 'ok' ? 'OK' : status === 'warning' ? 'Warning' : 'Critical';
}

async function loadWatchdogStatus() {
  const report = qs('watchdog-report');
  const bar = qs('watchdog-status-bar');
  bar.style.display = 'none';
  report.innerHTML = '<div class="empty-state"><div class="spinner-panel"></div></div>';
  try {
    const d = await api('GET', '/api/watchdog/status');
    if (!d.enabled) {
      report.innerHTML = '<div class="empty-state">Health watchdog is disabled.<br><code>MEMTOMEM_HEALTH_WATCHDOG__ENABLED=true</code></div>';
      _watchdogEnabled = false;
      return;
    }
    _watchdogEnabled = true;
    const checks = d.checks || {};
    const names = Object.keys(checks).sort();
    if (!names.length) {
      report.innerHTML = '<div class="empty-state">Watchdog is running but no checks recorded yet.</div>';
      return;
    }
    const criticals = names.filter(n => checks[n].status === 'critical').length;
    const warnings = names.filter(n => checks[n].status === 'warning').length;
    let summary;
    if (criticals > 0) summary = `<span class="health-dot health-down"></span> ${criticals} critical, ${warnings} warning`;
    else if (warnings > 0) summary = `<span class="health-dot health-slow"></span> ${warnings} warning`;
    else summary = `<span class="health-dot health-ok"></span> All checks OK`;

    report.innerHTML = `
      <div class="health-section" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;font-size:0.9rem">${summary} &mdash; ${names.length} checks</div>
      </div>
      <div class="health-grid">
        ${names.map(n => {
          const c = checks[n];
          const val = c.value || {};
          const detail = Object.entries(val).map(([k,v]) => `<span class="mono">${k}</span>: ${v}`).join(' &middot; ');
          return `<div class="health-card card">
            <div class="health-card-title" style="display:flex;align-items:center;gap:6px">${_wdDot(c.status)} ${n}</div>
            <div style="font-size:0.85rem;font-weight:600;margin:4px 0">${_wdLabel(c.status)}</div>
            <div class="health-card-detail">${detail || '—'}</div>
            <div class="health-card-detail" style="opacity:0.5">${c.tier}</div>
          </div>`;
        }).join('')}
      </div>
    `;
  } catch (e) {
    report.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

let _watchdogEnabled = false;

async function runWatchdogNow() {
  if (!_watchdogEnabled) {
    showToast('Health watchdog is disabled. Set MEMTOMEM_HEALTH_WATCHDOG__ENABLED=true to enable.', 'error');
    return;
  }
  const bar = qs('watchdog-status-bar');
  const btn = qs('watchdog-run-btn');
  btn.disabled = true;
  btn.textContent = 'Running...';
  bar.style.display = 'none';
  try {
    await api('POST', '/api/watchdog/run');
    bar.className = 'status-msg ok';
    bar.textContent = 'Health checks completed.';
    bar.style.display = 'block';
    await loadWatchdogStatus();
  } catch (e) {
    bar.className = 'status-msg err';
    bar.textContent = 'Run failed: ' + e.message;
    bar.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run Now';
  }
}

qs('watchdog-refresh-btn')?.addEventListener('click', loadWatchdogStatus);
qs('watchdog-run-btn')?.addEventListener('click', runWatchdogNow);

// =====================================================================
// STM PROXY DASHBOARD
// =====================================================================

let _stmPeriod = '';
let _stmHistOffset = 0;
let _stmPollFailures = 0;

async function checkStmAvailability() {
  try {
    const r = await fetch('/api/proxy/status');
    const d = await r.json();
    if (d.installed && d.enabled) {
      const btn = qs('stm-tab-btn');
      if (btn) btn.hidden = false;
    }
  } catch { /* STM not available */ }
}

function _stmLoading(show) {
  const el = qs('stm-metrics-summary');
  if (el && show) el.innerHTML = '<div class="stm-empty-hint">Loading...</div>';
}

async function loadStmDashboard() {
  _stmLoading(true);
  try {
    const r = await fetch('/api/proxy/status');
    const d = await r.json();
    if (!d.enabled) {
      qs('stm-status-bar').innerHTML = '<div class="stm-badge stm-off">STM Disabled</div>';
      _stmLoading(false);
      return;
    }
    _renderStmStatus(d);
    _renderStmServers(d.servers);
    _stmPollFailures = 0;
    await Promise.all([_loadStmMetrics(), _loadStmCache(), _loadStmSurfacing(), _loadStmHistory()]);
  } catch (e) {
    qs('stm-status-bar').innerHTML = `<div class="stm-badge stm-off">Error: ${e.message}</div>`;
    _stmLoading(false);
  }
}

function _renderStmStatus(d) {
  const bar = qs('stm-status-bar');
  const badges = [
    '<span class="stm-badge stm-on">STM Active</span>',
    `<span class="stm-badge">${d.servers.length} servers</span>`,
    d.surfacing_enabled ? '<span class="stm-badge stm-on">Surfacing ON</span>' : '<span class="stm-badge stm-off">Surfacing OFF</span>',
  ];
  if (d.cache_enabled) badges.push('<span class="stm-badge">Cache ON</span>');
  if (d.auto_index_enabled) badges.push('<span class="stm-badge">Auto-Index ON</span>');
  if (d.langfuse_enabled) badges.push('<span class="stm-badge">Langfuse ON</span>');
  bar.innerHTML = badges.join(' ');
}

function _renderStmServers(servers) {
  const el = qs('stm-servers');
  if (!servers.length) { el.innerHTML = '<div class="empty-state">No upstream servers configured</div>'; return; }
  el.innerHTML = servers.map(s => `
    <div class="stm-server-card">
      <div class="stm-server-name">${escapeHtml(s.name)}</div>
      <div class="stm-server-meta">
        <span class="stm-badge">${s.prefix}__</span>
        <span>${s.transport}</span>
        <span>${s.compression}</span>
        <span>${s.max_result_chars} chars</span>
      </div>
      ${s.auto_index ? '<div class="stm-badge stm-sm">auto-index</div>' : ''}
      ${s.tool_overrides_count ? `<div class="stm-badge stm-sm">${s.tool_overrides_count} overrides</div>` : ''}
    </div>
  `).join('');
}

function _fmtChars(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}

function _stmAbsTime(ts) {
  return new Date(ts * 1000).toLocaleString();
}

function _stmRelTime(ts) {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

async function _loadStmMetrics() {
  try {
    const since = _stmPeriod ? (Date.now() / 1000 - Number(_stmPeriod)) : undefined;
    const url = since ? `/api/proxy/metrics?since=${since}` : '/api/proxy/metrics';
    const r = await fetch(url);
    const d = await r.json();
    _stmPollFailures = 0;

    const saved = d.total_original_chars - d.total_compressed_chars;

    if (d.total_calls === 0) {
      qs('stm-metrics-summary').innerHTML = `
        <div class="stm-empty-hint">Metrics will appear here after tool calls through the proxy.</div>
      `;
    } else {
      qs('stm-metrics-summary').innerHTML = `
        <div class="stm-metric-card"><div class="stm-metric-value">${d.total_calls}</div><div class="stm-metric-label">Total Calls</div></div>
        <div class="stm-metric-card"><div class="stm-metric-value">${_fmtChars(d.total_original_chars)}</div><div class="stm-metric-label">Original</div></div>
        <div class="stm-metric-card"><div class="stm-metric-value">${_fmtChars(d.total_compressed_chars)}</div><div class="stm-metric-label">Compressed</div></div>
        <div class="stm-metric-card stm-highlight"><div class="stm-metric-value">${d.total_savings_pct}%</div><div class="stm-metric-label">Savings</div><div class="stm-metric-sub">${_fmtChars(saved)} saved</div></div>
      `;
    }

    // By server
    const sBody = qs('stm-by-server')?.querySelector('tbody');
    if (sBody) {
      sBody.innerHTML = Object.entries(d.by_server).map(([name, s]) => `
        <tr class="stm-clickable" onclick="_stmFilterHistory('${escapeAttr(name)}','')">
          <td>${escapeHtml(name)}</td><td>${s.calls}</td><td>${_fmtChars(s.original_chars)}</td><td>${_fmtChars(s.compressed_chars)}</td><td>${s.savings_pct}%</td>
        </tr>
      `).join('') || '<tr><td colspan="5" class="stm-empty">No data</td></tr>';
    }

    // By tool
    const tBody = qs('stm-by-tool')?.querySelector('tbody');
    if (tBody) {
      tBody.innerHTML = Object.entries(d.by_tool).map(([name, t]) => `
        <tr class="stm-clickable" onclick="_stmFilterHistory('${escapeAttr(name.split('/')[0])}','${escapeAttr(name.split('/').slice(1).join('/'))}')">
          <td>${escapeHtml(name)}</td><td>${t.calls}</td><td>${_fmtChars(t.original_chars)}</td><td>${_fmtChars(t.compressed_chars)}</td><td>${t.savings_pct}%</td>
        </tr>
      `).join('') || '<tr><td colspan="5" class="stm-empty">No data</td></tr>';
    }
  } catch (e) {
    _stmPollFailures++;
    if (_stmPollFailures >= 3) {
      _stopStmPolling();
      const bar = qs('stm-status-bar');
      if (bar) bar.innerHTML += ' <span class="stm-badge stm-off">Polling stopped (connection lost)</span>';
    }
  }
}

async function _loadStmCache() {
  try {
    const r = await fetch('/api/proxy/cache');
    const d = await r.json();
    let detail = '';
    if (d.by_server && Object.keys(d.by_server).length > 0) {
      const rows = Object.entries(d.by_server).map(([s, c]) => `<tr><td>${escapeHtml(s)}</td><td>${c}</td></tr>`).join('');
      detail = `<div class="stm-card-detail" hidden><table class="stm-table"><thead><tr><th>Server</th><th>Entries</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    qs('stm-cache-info').innerHTML = `
      <div class="stm-card-expandable" onclick="this.nextElementSibling&&(this.nextElementSibling.hidden=!this.nextElementSibling.hidden)">
        <strong>${d.total_entries}</strong> entries${d.expired_entries ? ` <span style="color:var(--muted)">(${d.expired_entries} expired)</span>` : ''}
        ${d.total_entries > 0 ? ' <span style="color:var(--muted);font-size:0.75rem">click for details</span>' : ''}
      </div>
      ${detail}
    `;
  } catch { qs('stm-cache-info').innerHTML = '<div>Unavailable</div>'; }
}

async function _loadStmSurfacing() {
  try {
    const r = await fetch('/api/proxy/surfacing');
    const d = await r.json();
    if (d.total_surfacings === 0) {
      qs('stm-surfacing-info').innerHTML = '<div class="stm-empty-hint" style="padding:8px 0">No surfacings yet — memories are surfaced during proxied tool calls when relevant LTM results are found.</div>';
      return;
    }
    const total = d.total_feedback || 1;
    const ratingBars = Object.entries(d.by_rating).map(([k, v]) => {
      const pct = Math.round(v / total * 100);
      const cls = k === 'helpful' ? 'rating-helpful' : k === 'not_relevant' ? 'rating-not-relevant' : 'rating-other';
      return `<div class="stm-rating-row"><span class="stm-rating-label">${k}</span><div class="stm-rating-bar"><div class="stm-rating-fill ${cls}" style="width:${pct}%"></div></div><span class="stm-rating-count">${v} (${pct}%)</span></div>`;
    }).join('');

    // Recent events detail
    let eventsHtml = '';
    if (d.recent_events && d.recent_events.length > 0) {
      const rows = d.recent_events.map(e => `<tr><td title="${_stmAbsTime(e.timestamp)}">${_stmRelTime(e.timestamp)}</td><td>${escapeHtml(e.tool)}</td><td>${escapeHtml(e.query.substring(0, 60))}${e.query.length > 60 ? '...' : ''}</td><td>${e.score_count}</td></tr>`).join('');
      eventsHtml = `<div class="stm-card-detail" hidden>
        <div style="font-size:0.8rem;font-weight:600;margin-bottom:4px">Recent Events</div>
        <table class="stm-table"><thead><tr><th>Time</th><th>Tool</th><th>Query</th><th>Results</th></tr></thead><tbody>${rows}</tbody></table>
      </div>`;
    }

    const pendingBadge = d.pending_feedback > 0 ? ` <span class="stm-badge stm-sm">${d.pending_feedback} pending</span>` : '';

    qs('stm-surfacing-info').innerHTML = `
      <div class="stm-card-expandable" onclick="var d=this.parentElement.querySelector('.stm-card-detail');if(d)d.hidden=!d.hidden">
        <strong>${d.total_surfacings}</strong> surfacings, <strong>${d.total_feedback}</strong> feedback${pendingBadge}
        ${d.recent_events?.length ? ' <span style="color:var(--muted);font-size:0.75rem">click for details</span>' : ''}
      </div>
      ${ratingBars}
      <div style="margin-top:6px">Helpfulness: <strong>${d.helpfulness_pct}%</strong> <span style="color:var(--muted);font-size:0.75rem">(n=${d.total_feedback})</span></div>
      ${eventsHtml}
    `;
  } catch { qs('stm-surfacing-info').innerHTML = '<div>Unavailable</div>'; }
}

async function _loadStmHistory() {
  try {
    const server = qs('stm-hist-server')?.value || '';
    const tool = qs('stm-hist-tool')?.value || '';
    let url = `/api/proxy/history?limit=50&offset=${_stmHistOffset}`;
    if (server) url += `&server=${encodeURIComponent(server)}`;
    if (tool) url += `&tool=${encodeURIComponent(tool)}`;

    const r = await fetch(url);
    const d = await r.json();

    const tbody = qs('stm-history')?.querySelector('tbody');
    if (tbody) {
      tbody.innerHTML = d.entries.map(e => `
        <tr>
          <td title="${_stmAbsTime(e.timestamp)}">${_stmRelTime(e.timestamp)}</td>
          <td>${escapeHtml(e.server)}</td>
          <td>${escapeHtml(e.tool)}</td>
          <td>${_fmtChars(e.original_chars)}</td>
          <td>${_fmtChars(e.cleaned_chars)}</td>
          <td>${_fmtChars(e.compressed_chars)}</td>
          <td>${e.savings_pct}%</td>
        </tr>
      `).join('') || '<tr><td colspan="7" class="stm-empty">No history</td></tr>';
    }

    // Pagination
    const pag = qs('stm-hist-pagination');
    if (pag) {
      const hasMore = _stmHistOffset + 50 < d.total;
      const hasPrev = _stmHistOffset > 0;
      const start = d.total > 0 ? _stmHistOffset + 1 : 0;
      const end = Math.min(_stmHistOffset + 50, d.total);
      pag.innerHTML = `
        <span>${d.total > 0 ? `Showing ${start}-${end} of ${d.total}` : '0 total'}</span>
        ${hasPrev ? '<button class="btn-outline btn-sm" onclick="_stmHistPrev()">Prev</button>' : ''}
        ${hasMore ? '<button class="btn-outline btn-sm" onclick="_stmHistNext()">Next</button>' : ''}
      `;
    }
  } catch { /* history load failed */ }
}

function _stmFilterHistory(server, tool) {
  const sEl = qs('stm-hist-server');
  const tEl = qs('stm-hist-tool');
  if (sEl) sEl.value = server;
  if (tEl) tEl.value = tool;
  _stmHistOffset = 0;
  _loadStmHistory();
  _updateStmFilterBadge();
  qs('stm-history')?.scrollIntoView({ behavior: 'smooth' });
}

function _stmHistNext() { _stmHistOffset += 50; _loadStmHistory(); }
function _stmHistPrev() { _stmHistOffset = Math.max(0, _stmHistOffset - 50); _loadStmHistory(); }

function _startStmPolling() {
  _stopStmPolling();
  _stmPollFailures = 0;
  _stmPollId = setInterval(() => _loadStmMetrics(), 10000);
}

function _stopStmPolling() {
  if (_stmPollId) { clearInterval(_stmPollId); _stmPollId = null; }
}

// Event listeners
qs('stm-refresh-btn')?.addEventListener('click', async function() {
  this.textContent = 'Loading...';
  this.disabled = true;
  try {
    await Promise.all([_loadStmMetrics(), _loadStmCache(), _loadStmSurfacing(), _loadStmHistory()]);
  } finally {
    this.textContent = 'Refresh';
    this.disabled = false;
  }
});

qs('stm-cache-clear-btn')?.addEventListener('click', async () => {
  if (!confirm('Clear all proxy cache entries?')) return;
  try {
    const r = await fetch('/api/proxy/cache/clear', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' });
    const d = await r.json();
    _loadStmCache();
    qs('stm-cache-info').innerHTML += `<div style="color:var(--green);font-size:0.8rem;margin-top:4px">Cleared ${d.cleared} entries</div>`;
  } catch { alert('Failed to clear cache'); }
});

qs('stm-hist-filter-btn')?.addEventListener('click', () => { _stmHistOffset = 0; _loadStmHistory(); _updateStmFilterBadge(); });

function _updateStmFilterBadge() {
  const server = qs('stm-hist-server')?.value || '';
  const tool = qs('stm-hist-tool')?.value || '';
  const clearBtn = qs('stm-hist-clear-btn');
  if (clearBtn) clearBtn.hidden = !(server || tool);
}
qs('stm-hist-clear-btn')?.addEventListener('click', () => {
  const sEl = qs('stm-hist-server');
  const tEl = qs('stm-hist-tool');
  if (sEl) sEl.value = '';
  if (tEl) tEl.value = '';
  _stmHistOffset = 0;
  _loadStmHistory();
  _updateStmFilterBadge();
});

// Period filter buttons
document.querySelectorAll('.stm-period-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.stm-period-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _stmPeriod = btn.dataset.period;
    _loadStmMetrics();
  });
});

// Check STM availability on page load
checkStmAvailability();

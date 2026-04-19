/* memtomem Web UI — Vanilla JS SPA */
'use strict';

const API = '';  // same origin

// ── Early declarations (referenced before their section) ──
const _HELP_VISIBLE_KEY = 'm2m-help-visible';

// ── Unified global state ──
const STATE = {
  lastSettingsSection: null,
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
  serverDefaults: null,
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
  document.addEventListener('DOMContentLoaded', async () => {
    const isDark = el.getAttribute('data-theme') !== 'light';
    qs('theme-toggle').textContent = isDark ? '🌙' : '☀️';
    if (typeof I18N !== 'undefined') await I18N.init();
    renderRecentChips();
    _initTabHelp();
    // Re-apply i18n when language changes (dynamic JS strings)
    window.addEventListener('langchange', () => {
      if (typeof I18N !== 'undefined') I18N.applyDOM();
    });
    // Activate tab from URL hash now that i18n has loaded — tabs that
    // render JS-built UI (like the Sources tab's Memory Dirs panel) call
    // ``t()`` at build time, so they must run after the locale cache is
    // populated to avoid raw-key flashes.
    const hash = location.hash.slice(1);
    const validTabs = ['home', 'search', 'sources', 'index', 'tags', 'timeline', 'settings'];
    if (hash && validTabs.includes(hash)) {
      activateTab(hash);
    }
  });
})();

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

async function api(method, path, body, opts = {}) {
  if (typeof opts !== 'object' || Array.isArray(opts)) opts = {};
  const fetchOpts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) fetchOpts.body = JSON.stringify(body);
  if (opts.signal) fetchOpts.signal = opts.signal;
  else fetchOpts.signal = AbortSignal.timeout(opts.timeout ?? 30_000);
  const res = await fetch(API + path, fetchOpts);
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
  showToast(t('toast.copied'), 'info');
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
function showConfirm({ title, message = '', confirmText = t('common.confirm') }) {
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
  if (tabName === 'sources') {
    STATE.sourcesBrowserStale = false;
    if (typeof renderMemoryDirsPanel === 'function') renderMemoryDirsPanel();
    loadSources();
  }
  if (tabName === 'index') loadStats();
  if (tabName === 'tags') { STATE.tagsTabStale = false; loadTags(); }
  if (tabName === 'timeline') loadTimeline();
  if (tabName === 'settings') {
    let start = STATE.lastSettingsSection;
    if (!start) {
      try { start = localStorage.getItem(LAST_SECTION_KEY); } catch {}
    }
    switchSettingsSection(start || 'config');
  }
  if (['search', 'timeline'].includes(tabName)) loadNamespaceDropdowns();
}

// Settings Hub section switching

const NAV_COLLAPSE_KEY = 'memtomem_nav_collapsed';
const LAST_SECTION_KEY = 'memtomem_last_settings';
const DEFAULT_NAV_COLLAPSED = { general: false, integrations: false, runtime: true, data: true };
// Deep-link redirects for renamed/removed sections.
const LEGACY_SECTION_MAP = { 'harness-watchdog': 'harness-health' };

function loadNavCollapseState() {
  try {
    const raw = localStorage.getItem(NAV_COLLAPSE_KEY);
    return { ...DEFAULT_NAV_COLLAPSED, ...(raw ? JSON.parse(raw) : {}) };
  } catch {
    return { ...DEFAULT_NAV_COLLAPSED };
  }
}

function saveNavCollapseState() {
  try { localStorage.setItem(NAV_COLLAPSE_KEY, JSON.stringify(STATE.settingsNavCollapsed)); } catch {}
}

function applyNavCollapseState() {
  const state = STATE.settingsNavCollapsed || DEFAULT_NAV_COLLAPSED;
  document.querySelectorAll('.settings-nav-group[data-group]').forEach(groupBtn => {
    if (groupBtn.classList.contains('settings-nav-group--danger')) return;
    const groupId = groupBtn.dataset.group;
    const collapsed = !!state[groupId];
    groupBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    const caret = groupBtn.querySelector('.nav-group-caret');
    if (caret) caret.textContent = collapsed ? '▸' : '▾';
  });
  document.querySelectorAll('.settings-nav-btn[data-group]').forEach(btn => {
    const groupId = btn.dataset.group;
    if (groupId === 'danger') return;
    btn.classList.toggle('collapsed-member', !!state[groupId]);
  });
}

function toggleNavGroup(groupId) {
  if (!STATE.settingsNavCollapsed) STATE.settingsNavCollapsed = loadNavCollapseState();
  STATE.settingsNavCollapsed[groupId] = !STATE.settingsNavCollapsed[groupId];
  saveNavCollapseState();
  applyNavCollapseState();
}

function ensureActiveGroupExpanded(section) {
  const btn = document.querySelector(`.settings-nav-btn[data-section="${section}"]`);
  if (!btn) return;
  const groupId = btn.dataset.group;
  if (!groupId || groupId === 'danger') return;
  if (!STATE.settingsNavCollapsed) STATE.settingsNavCollapsed = loadNavCollapseState();
  if (STATE.settingsNavCollapsed[groupId]) {
    STATE.settingsNavCollapsed[groupId] = false;
    saveNavCollapseState();
    applyNavCollapseState();
  }
}

function switchSettingsSection(sectionName) {
  sectionName = LEGACY_SECTION_MAP[sectionName] || sectionName;
  STATE.lastSettingsSection = sectionName;
  try { localStorage.setItem(LAST_SECTION_KEY, sectionName); } catch {}
  document.querySelectorAll('.settings-nav-btn').forEach(b => {
    b.classList.remove('active');
    b.setAttribute('aria-selected', 'false');
  });
  document.querySelectorAll('.settings-section').forEach(s => s.classList.remove('active'));
  const btn = document.querySelector(`.settings-nav-btn[data-section="${sectionName}"]`);
  const section = document.getElementById(`settings-${sectionName}`);
  if (btn) {
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
  }
  if (section) section.classList.add('active');
  ensureActiveGroupExpanded(sectionName);
  // Section-specific loads (reuse existing functions)
  if (sectionName === 'config') loadConfig();
  if (sectionName === 'namespaces') loadNamespacesTab();
  if (sectionName === 'dedup') resetDedupPanel();
  if (sectionName === 'decay') resetDecayPanel();
  if (sectionName === 'export') { resetExportPanel(); loadNamespaceDropdowns(); }
  if (sectionName === 'harness-sessions') loadHarnessSessions();
  if (sectionName === 'harness-scratch') loadHarnessScratch();
  if (sectionName === 'harness-procedures') loadHarnessProcedures();
  if (sectionName === 'harness-health') { loadHarnessHealth(); loadWatchdogStatus(); }
  if (sectionName === 'hooks-sync') loadHooksSync();
  if (sectionName === 'ctx-overview') loadCtxOverview();
  if (sectionName === 'ctx-skills') loadCtxList('skills');
  if (sectionName === 'ctx-commands') loadCtxList('commands');
  if (sectionName === 'ctx-agents') loadCtxList('agents');
}

// Settings nav buttons
document.querySelectorAll('.settings-nav-btn').forEach(btn => {
  btn.addEventListener('click', () => switchSettingsSection(btn.dataset.section));
});

// Settings nav group buttons (expand/collapse)
document.querySelectorAll('.settings-nav-group[data-group]').forEach(grp => {
  if (grp.classList.contains('settings-nav-group--danger')) return;
  grp.addEventListener('click', () => toggleNavGroup(grp.dataset.group));
});

// Initialize collapse state from localStorage
STATE.settingsNavCollapsed = loadNavCollapseState();
applyNavCollapseState();

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
// Note: initial hash-based activateTab dispatch moved into the i18n init
// handler above so ``t()``-backed JS widgets (Sources tab's Memory Dirs
// panel) render with translated strings instead of raw keys.

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
  } catch (e) { console.warn('[stats]', e); }
}

loadStats();
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
      const warned = await showConfirm({
        title: t('confirm.emb_reset_title'),
        message: t('confirm.emb_reset_msg', {
          provider: data.configured.provider,
          model: data.configured.model,
          dimension: data.configured.dimension,
        }),
        confirmText: t('confirm.emb_reset_btn'),
      });
      if (!warned) return;
      try {
        const res = await api('POST', '/api/embedding-reset', undefined, { timeout: 120_000 });
        hide(banner);
        sessionStorage.removeItem('m2m-emb-banner-dismissed');
        await fetchServerConfig();
        showToast(res.message, 'success');
      } catch (err) {
        showToast(t('toast.reset_failed', { error: err.message }), 'error');
      }
    }, { once: true });
  } catch (e) { console.warn('[emb-check]', e); }
}

// ---------------------------------------------------------------------------
// Home Dashboard (D3)
// ---------------------------------------------------------------------------

async function loadDashboard() {
  try {
    const [stats, sourcesData, nsData, configData, embStatus, timelineData] = await Promise.all([
      api('GET', '/api/stats'),
      api('GET', '/api/sources'),
      api('GET', '/api/namespaces'),
      api('GET', '/api/config'),
      api('GET', '/api/embedding-status').catch(() => null),
      api('GET', '/api/timeline?days=365&limit=1000').catch(() => ({ chunks: [] })),
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

    // B. Activity Heatmap (GitHub contribution graph)
    _renderActivityMap(timelineData.chunks || []);

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

// B. Activity Heatmap — GitHub contribution graph (1 year)
function _renderActivityMap(chunks) {
  const map = qs('home-activity-map');
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

  // Count chunks per date
  const countByDate = {};
  chunks.forEach(c => {
    const key = (c.created_at || '').slice(0, 10);
    if (key) countByDate[key] = (countByDate[key] || 0) + 1;
  });

  // Start from Sunday before 364 days ago
  const startDate = new Date(today);
  startDate.setDate(startDate.getDate() - 364 - startDate.getDay());
  // Compute working range (but only show data for the last 364 days)
  const dataStart = new Date(today);
  dataStart.setDate(dataStart.getDate() - 364);

  const totalDays = Math.round((today - startDate) / 86400000) + 1;
  const cells = [];
  let maxCount = 0;

  for (let i = 0; i < totalDays; i++) {
    const d = new Date(startDate);
    d.setDate(d.getDate() + i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const count = countByDate[key] || 0;
    const isFuture = d > today;
    const isBeforeRange = d < dataStart;
    if (!isFuture && !isBeforeRange && count > maxCount) maxCount = count;
    cells.push({ date: key, count, isFuture, isBeforeRange, month: d.getMonth() });
  }

  // Quartile-based levels (like GitHub)
  const getLevel = (count) => {
    if (count === 0 || maxCount === 0) return 0;
    const q = count / maxCount;
    if (q <= 0.25) return 1;
    if (q <= 0.50) return 2;
    if (q <= 0.75) return 3;
    return 4;
  };

  // Month labels — detect when a new month starts in the first row (Sunday)
  const numWeeks = Math.ceil(totalDays / 7);
  const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const monthLabels = [];
  let prevMonth = -1;
  for (let w = 0; w < numWeeks; w++) {
    const idx = w * 7;
    if (idx < cells.length) {
      const m = cells[idx].month;
      if (m !== prevMonth) {
        monthLabels.push({ col: w + 1, label: monthNames[m] });
        prevMonth = m;
      }
    }
  }

  let html = '';

  // Month labels row
  html += `<div class="activity-months" style="grid-template-columns:repeat(${numWeeks},1fr);gap:2px">`;
  monthLabels.forEach(m => {
    html += `<span style="grid-column:${m.col}">${m.label}</span>`;
  });
  html += '</div>';

  // Grid of cells (7 rows × N cols, auto-flow column = weeks)
  html += `<div class="activity-grid" style="grid-template-columns:repeat(${numWeeks},1fr)">`;
  cells.forEach(cell => {
    if (cell.isFuture || cell.isBeforeRange) {
      html += '<div class="activity-cell activity-empty"></div>';
    } else {
      const level = getLevel(cell.count);
      html += `<div class="activity-cell" data-level="${level}" title="${cell.date}: ${cell.count}"></div>`;
    }
  });
  html += '</div>';

  map.innerHTML = html;
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

let _searchAbortCtrl = null;

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

  // Cancel any in-flight search
  if (_searchAbortCtrl) _searchAbortCtrl.abort();
  _searchAbortCtrl = new AbortController();

  const btn = qs('search-btn');
  btnLoading(btn, true);
  try {
    const data = await api('GET', `/api/search?${params}`, undefined,
                           { signal: _searchAbortCtrl.signal });
    renderResults(data.results, data.retrieval_stats);
  } catch (err) {
    if (err.name === 'AbortError') return;
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
        showToast(t('toast.tag_remove_failed', { error: err.message }), 'error');
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
    showToast(t('toast.chunk_saved'), 'success');
    STATE.selectedOriginal = newContent;
    _syncResultContent(STATE.selectedChunkId, newContent);
    _updateHistoryBtn(STATE.selectedChunkId);
    _markDataStale();
    loadStats();
  } catch (err) {
    showToast(t('toast.save_failed', { error: err.message }), 'error');
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
    title: t('confirm.chunk_delete_title'),
    message: t('confirm.chunk_delete_msg', { lines, source: src }),
    confirmText: t('common.delete'),
  });
  if (!ok) return;
  try {
    await api('DELETE', `/api/chunks/${STATE.selectedChunkId}`);
    showToast(t('toast.chunk_deleted'), 'success');
    clearDetail();
    _markDataStale();
    doSearch();
    loadStats();
  } catch (err) {
    showToast(t('toast.delete_failed', { error: err.message }), 'error');
  }
});

qs('d-reset-btn').addEventListener('click', () => {
  qs('d-editor').value = STATE.selectedOriginal;
  hide(qs('d-diff'));
  hide(qs('d-diff-btn'));
  qs('d-diff-btn').dataset.mode = 'source';
  _setDetailMode('edit');
  _updateWordCount();
  showToast(t('toast.content_restored'), 'info');
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
    title: t('confirm.bulk_delete_title', { count: ids.length }),
    message: t('confirm.bulk_delete_msg', { count: ids.length }),
    confirmText: t('common.delete'),
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
    ? t('toast.bulk_delete_partial', { deleted, failed })
    : t('toast.bulk_delete_ok', { count: deleted });
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
        showToast(t('toast.tag_remove_failed', { error: err.message }), 'error');
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
    showToast(t('toast.tags_saved'), 'success');
  } catch (err) {
    showToast(t('toast.tag_save_failed', { error: err.message }), 'error');
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
          title: t('confirm.source_delete_title'),
          message: t('confirm.source_delete_msg', { path: s.path }),
          confirmText: t('common.delete'),
        });
        if (!ok) return;
        try {
          await api('DELETE', `/api/sources?path=${encodeURIComponent(s.path)}`);
          showToast(t('toast.source_deleted'), 'success');
          STATE.allSources = STATE.allSources.filter(x => x.path !== s.path);
          renderSourceTree(_getFilteredSorted());
          hideBrowser();
          STATE.lastResults = STATE.lastResults.filter(r => r.chunk.source_file !== s.path);
          renderResults(STATE.lastResults);
          _markDataStale();
          loadSourceFilter();
          loadStats();
        } catch (err) {
          showToast(t('toast.delete_failed', { error: err.message }), 'error');
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
            title: t('confirm.chunk_delete_title'),
            message: t('confirm.chunk_delete_simple_msg', { start: c.start_line, end: c.end_line }),
            confirmText: t('common.delete'),
          });
          if (!ok) return;
          try {
            await api('DELETE', `/api/chunks/${c.id}`);
            card.remove();
            showToast(t('toast.chunk_deleted'), 'success');
            // Update count badge
            const countEl = content.querySelector('.badge-blue');
            const remaining = content.querySelectorAll('.chunk-card').length;
            if (countEl) countEl.textContent = `${remaining} chunks`;
            STATE.lastResults = STATE.lastResults.filter(r => String(r.chunk.id) !== String(c.id));
            renderResults(STATE.lastResults);
            _markDataStale();
            loadStats();
          } catch (err) {
            showToast(t('toast.delete_failed', { error: err.message }), 'error');
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
      showToast(t('toast.chunk_updated'), 'success');
      _syncResultContent(chunk.id, newContent);
      _markDataStale();
      // Refresh the browser to show updated content
      browseSource(sourcePath, card.closest('#chunks-browser-content')?.querySelectorAll('.chunk-card').length > 100 ? 500 : 100);
    } catch (err) {
      showToast(t('toast.update_failed', { error: err.message }), 'error');
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
    showToast(t('toast.indexed_count', { count: data.indexed_chunks }), 'success');
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
    showToast(t('toast.index_failed', { error: err.message }), 'error');
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
    showToast(t('toast.saved_n_indexed', { count: n }), 'success');
    qs('add-content').value = '';
    _markDataStale();
    loadStats();
  } catch (err) {
    showToast(t('toast.save_failed', { error: err.message }), 'error');
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
      showToast(t('toast.upload_complete', { count: data.total_indexed }), 'success');
      selectedFiles = [];
      renderFileList();
      _markDataStale();
      loadSourceFilter();
      loadStats();
    } catch (err) {
      showToast(t('toast.upload_failed', { error: err.message }), 'error');
    } finally {
      btnLoading(btn, false);
    }
  });
})();

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
    showToast(t('toast.tagged_count', { label, count: data.tagged_chunks }), 'success');
    if (!dryRun) { loadTags(); loadStats(); _markDataStale(); }
  } catch (err) {
    showToast(t('toast.autotag_failed', { error: err.message }), 'error');
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
  let _sseFailCount = 0;
  const _SSE_MAX_FAILS = 3;

  es.onmessage = (e) => {
    let event;
    try { event = JSON.parse(e.data); }
    catch {
      _sseFailCount++;
      console.warn(`[index-stream] malformed SSE (${_sseFailCount}/${_SSE_MAX_FAILS}):`, e.data);
      if (_sseFailCount >= _SSE_MAX_FAILS) {
        es.close();
        showToast(t('toast.stream_fallback'), 'error');
        hide(progressEl);
        btnLoading(qs('index-stream-btn'), false);
        btnLoading(qs('index-btn'), false);
      }
      return;
    }
    _sseFailCount = 0;
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
      showToast(t('toast.stream_complete', { count: event.total_files }), 'success');
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
    showToast(t('toast.stream_fallback'), 'error');
    hide(progressEl);
    btnLoading(qs('index-stream-btn'), false);
    btnLoading(qs('index-btn'), false);
  };
}

qs('index-stream-btn').addEventListener('click', runIndexStream);

qs('refresh-tags-btn').addEventListener('click', loadTags);
qs('autotag-btn').addEventListener('click', runAutoTag);


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
    showToast(t('toast.error', { error: err.message }), 'error');
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
    showToast(t('toast.unpinned'), 'info');
  } else {
    pinChunk(id, {
      source: qs('d-file').textContent || '',
      snippet: qs('d-editor').value.slice(0, 100),
    });
    showToast(t('toast.pinned'), 'info');
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
  };
}

// Deferred until DOMContentLoaded so sibling scripts (settings-config.js, etc.)
// have parsed — activateTab('settings') calls loadConfig() defined there.
document.addEventListener('DOMContentLoaded', () => {
  const s = _loadSettings();
  // top-k default is synced from server config via _syncSearchDefaults()
  // Apply default tab only if no hash deep link is present
  if (!location.hash.slice(1)) {
    const currentActive = document.querySelector('.tab-btn.active');
    const currentTab = currentActive ? currentActive.dataset.tab : null;
    if (currentTab !== s.defaultTab) {
      activateTab(s.defaultTab);
    }
  }
});

qs('settings-btn').addEventListener('click', () => {
  const s = _loadSettings();
  qs('settings-default-tab').value = s.defaultTab;
  // Show current server config top-k (or UI value as fallback)
  const curTopK = STATE.serverConfig?.search?.default_top_k || STATE.currentTopK || 10;
  qs('settings-default-topk').value = String(curTopK);
  show(qs('settings-modal'));
});
qs('settings-close-btn').addEventListener('click', () => hide(qs('settings-modal')));
qs('settings-modal').addEventListener('click', e => {
  if (e.target === qs('settings-modal')) hide(qs('settings-modal'));
});
qs('settings-save-btn').addEventListener('click', async () => {
  localStorage.setItem('m2m-default-tab', qs('settings-default-tab').value);
  const newTopK = parseInt(qs('settings-default-topk').value, 10);
  // Sync top-k to server config so all paths see the same value
  try {
    await api('PATCH', '/api/config?persist=true', { search: { default_top_k: newTopK } });
    if (STATE.serverConfig?.search) STATE.serverConfig.search.default_top_k = newTopK;
    qs('top-k').value = String(newTopK);
    STATE.currentTopK = newTopK;
  } catch (e) {
    console.warn('Failed to persist top-k to server config:', e);
  }
  showToast(t('toast.settings_saved'), 'success');
  hide(qs('settings-modal'));
});
qs('settings-reset-btn').addEventListener('click', () => {
  localStorage.removeItem('m2m-default-tab');
  qs('settings-default-tab').value = 'search';
  // Reset top-k to server default
  const serverTopK = STATE.serverConfig?.search?.default_top_k || 10;
  qs('settings-default-topk').value = String(serverTopK);
  showToast(t('toast.settings_reset'), 'info');
});

qs('shortcuts-close-btn').addEventListener('click', () => hide(qs('shortcuts-modal')));
qs('shortcuts-modal').addEventListener('click', e => {
  if (e.target === qs('shortcuts-modal')) hide(qs('shortcuts-modal'));
});

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
  } catch (e) {
    console.warn('[source-filter]', e);
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
  if (!q) { showToast(t('toast.enter_query'), 'error'); return; }
  const name = prompt('Save search as:', q);
  if (!name) return;
  const list = _getSavedQueries();
  list.push({ name, query: q, typeFilter: qs('chunk-type-filter').value, tagFilter: qs('tag-filter').value.trim() });
  _setSavedQueries(list);
  _renderSavedSelect();
  _renderSavedBar();
  showToast(t('toast.query_saved', { name }), 'success');
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
  if (isNaN(idx)) { showToast(t('toast.select_saved'), 'error'); return; }
  const list = _getSavedQueries();
  const name = list[idx]?.name;
  list.splice(idx, 1);
  _setSavedQueries(list);
  _renderSavedSelect();
  _renderSavedBar();
  showToast(t('toast.query_deleted', { name }), 'info');
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
      showToast(t('toast.query_removed', { name }), 'info');
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
  showToast(t('toast.exported_count', { count: items.length, ext: ext.toUpperCase() }), 'success');
}

qs('bulk-export-btn').addEventListener('click', () => {
  const ids = [...STATE.selectedIds];
  if (!ids.length) return;
  const format = qs('bulk-export-fmt').value;
  const selected = STATE.lastResults.filter(r => ids.includes(String(r.chunk.id)));
  downloadResults(selected, format);
});

qs('export-all-btn').addEventListener('click', () => {
  if (!STATE.lastResults.length) { showToast(t('toast.no_results_export'), 'error'); return; }
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
        showToast(t('toast.diff_shown'), 'info');
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
    if (!files.length) { showToast(t('toast.file_filter'), 'error'); return; }
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    showToast(t('toast.indexing_files', { count: files.length }), 'info');
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail);
      }
      const data = await res.json();
      const chunks = (data.results || []).reduce((s, r) => s + (r.indexed_chunks || 0), 0);
      showToast(t('toast.indexed_files_chunks', { files: files.length, chunks }), 'success');
      _markDataStale();
      loadSourceFilter();
      loadStats();
    } catch (err) {
      showToast(t('toast.upload_failed', { error: err.message }), 'error');
    }
  });
}




// =====================================================================
// GLOBAL EVENT DELEGATION (CSP-safe: no inline onclick)
// =====================================================================

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;

  if (action === 'session-events') {
    showSessionEvents(el.dataset.id);
  } else if (action === 'scratch-delete') {
    deleteScratchEntry(el.dataset.key);
  } else if (action === 'scratch-promote') {
    promoteScratchEntry(el.dataset.key);
  } else if (action === 'toggle-next') {
    const sib = el.nextElementSibling;
    if (sib) sib.hidden = !sib.hidden;
  }
});

/**
 * Namespace filter dropdowns + Namespaces CRUD tab.
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

// ---------------------------------------------------------------------------
// Namespace grouping helpers
// ---------------------------------------------------------------------------

/**
 * Group namespaces by their colon-delimited prefix.
 *
 * Splits on the **first** colon only — e.g. `claude-memory:slug-a` yields
 * prefix `claude-memory`.  If the namespace format migrates to 3-depth
 * (`claude:<project>:memory`), the first-colon split still produces a
 * reasonable top-level group (`claude`).  Revisit if deeper grouping is
 * needed.
 *
 * Single-member prefix groups are demoted to ungrouped to avoid a
 * collapsed group of one.
 */
function _groupNamespaces(namespaces) {
  const prefixMap = new Map();
  const ungrouped = [];

  namespaces.forEach(ns => {
    const colonIdx = ns.namespace.indexOf(':');
    if (colonIdx > 0) {
      const prefix = ns.namespace.slice(0, colonIdx);
      if (!prefixMap.has(prefix)) prefixMap.set(prefix, []);
      prefixMap.get(prefix).push(ns);
    } else {
      ungrouped.push(ns);
    }
  });

  const groups = [];
  for (const [prefix, members] of prefixMap) {
    if (members.length === 1) {
      ungrouped.push(members[0]);
    } else {
      const totalChunks = members.reduce((s, m) => s + m.chunk_count, 0);
      members.sort((a, b) => b.chunk_count - a.chunk_count);
      groups.push({ prefix, members, totalChunks });
    }
  }

  // Primary: totalChunks desc, secondary: prefix alphabetical
  groups.sort((a, b) => b.totalChunks - a.totalChunks || a.prefix.localeCompare(b.prefix));
  ungrouped.sort((a, b) => b.chunk_count - a.chunk_count);
  return { groups, ungrouped };
}

// ---------------------------------------------------------------------------
// Namespace filter dropdowns + Namespaces tab
// ---------------------------------------------------------------------------

async function loadNamespaceDropdowns() {
  try {
    const data = await api('GET', '/api/namespaces');
    const namespaces = data.namespaces || [];
    const { groups, ungrouped } = _groupNamespaces(namespaces);
    ['ns-filter', 'tl-namespace', 'exp-namespace'].forEach(id => {
      const sel = document.getElementById(id);
      if (!sel) return;
      const current = sel.value;
      // Keep first option (All Namespaces), remove rest + optgroups
      while (sel.children.length > 1) sel.removeChild(sel.lastChild);
      groups.forEach(g => {
        const optgroup = document.createElement('optgroup');
        optgroup.label = `${g.prefix} (${g.totalChunks} chunks)`;
        g.members.forEach(ns => {
          const opt = document.createElement('option');
          opt.value = ns.namespace;
          const suffix = ns.namespace.slice(g.prefix.length + 1);
          opt.textContent = `${suffix} (${ns.chunk_count})`;
          optgroup.appendChild(opt);
        });
        sel.appendChild(optgroup);
      });
      ungrouped.forEach(ns => {
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
  } catch (e) { console.warn('[ns-dropdown]', e); }
}

// Load on startup and when switching to related tabs
loadNamespaceDropdowns();

// Namespaces tab
qs('ns-refresh-btn').addEventListener('click', loadNamespacesTab);

function _buildNsCard(ns, defaultNs) {
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
  return card;
}

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
    const { groups, ungrouped } = _groupNamespaces(namespaces);

    // Render collapsible groups
    groups.forEach(g => {
      const group = document.createElement('div');
      group.className = 'ns-group';

      const header = document.createElement('div');
      header.className = 'ns-group-header';
      header.setAttribute('role', 'button');
      header.setAttribute('aria-expanded', 'true');
      header.innerHTML = `<span class="ns-group-chevron">&#9660;</span>`
        + `<span class="ns-group-name">${escapeHtml(g.prefix)}</span>`
        + `<span class="badge badge-blue">${g.members.length}</span>`
        + `<span class="ns-group-chunks">${t('settings.ns.group_chunks', { count: g.totalChunks })}</span>`;
      header.addEventListener('click', () => {
        group.classList.toggle('collapsed');
        header.setAttribute('aria-expanded', String(!group.classList.contains('collapsed')));
      });

      const items = document.createElement('div');
      items.className = 'ns-group-items';
      g.members.forEach(ns => items.appendChild(_buildNsCard(ns, defaultNs)));

      group.appendChild(header);
      group.appendChild(items);
      list.appendChild(group);
    });

    // Render ungrouped cards
    ungrouped.forEach(ns => list.appendChild(_buildNsCard(ns, defaultNs)));
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
      showToast(t('toast.ns_updated'), 'success');
      loadNamespacesTab();
      loadNamespaceDropdowns();
    } catch (err) {
      showToast(t('toast.error', { error: err.message }), 'error');
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
    showToast(t('toast.rename_failed', { error: err.message }), 'error');
  }
}

async function deleteNamespace(name) {
  const ok = await showConfirm({
    title: t('confirm.ns_delete_title'),
    message: t('confirm.ns_delete_msg', { name }),
    confirmText: t('common.delete'),
  });
  if (!ok) return;
  try {
    const data = await api('DELETE', `/api/namespaces/${encodeURIComponent(name)}`);
    showToast(t('toast.ns_deleted', { count: data.deleted, name }), 'success');
    STATE.lastResults = STATE.lastResults.filter(r => r.chunk.namespace !== name);
    renderResults(STATE.lastResults);
    _markDataStale();
    loadNamespacesTab();
    loadNamespaceDropdowns();
    loadStats();
  } catch (err) {
    showToast(t('toast.delete_failed', { error: err.message }), 'error');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Phase 1a: Saved Searches — Star button + history dropdown integration
// ═══════════════════════════════════════════════════════════════════════════

qs('save-star-btn').addEventListener('click', () => {
  const q = qs('search-input').value.trim();
  if (!q) { showToast(t('toast.enter_query'), 'error'); return; }
  const list = _getSavedQueries();
  const exists = list.some(s => s.query === q);
  if (exists) {
    // Unsave
    const idx = list.findIndex(s => s.query === q);
    list.splice(idx, 1);
    _setSavedQueries(list);
    qs('save-star-btn').textContent = '☆';
    qs('save-star-btn').classList.remove('starred');
    showToast(t('toast.search_removed'), 'info');
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


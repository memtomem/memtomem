/**
 * Timeline tab — activity heatmap, chunk timeline, source timeline.
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

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



/**
 * Tab Help System + Harness panels (Sessions, Scratch, Procedures, Health).
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

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
          <td><button class="btn-ghost btn-xs" data-action="session-events" data-id="${s.id}">Events</button></td>
        </tr>`;
      }).join('') +
      '</tbody></table>';
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

let _sessionEventsCache = [];

async function showSessionEvents(sessionId) {
  const panel = qs('session-events-panel');
  const list = qs('session-events-list');
  qs('session-events-title').textContent = `Events: ${sessionId.slice(0, 8)}...`;
  show(panel);
  list.innerHTML = '<div class="spinner-panel"></div>';
  try {
    const data = await api('GET', `/api/sessions/${sessionId}/events`);
    _sessionEventsCache = data.events;
    if (!data.events.length) {
      list.innerHTML = '<div class="empty-state">No events</div>';
      return;
    }
    const types = [...new Set(data.events.map(e => e.event_type))];
    const filterHtml = types.length > 1
      ? `<div class="harness-event-filter">
          <button class="active" data-filter="all">all (${data.events.length})</button>
          ${types.map(t => `<button data-filter="${t}">${t} (${data.events.filter(e => e.event_type === t).length})</button>`).join('')}
        </div>`
      : '';
    list.innerHTML = filterHtml + _renderSessionEvents(data.events);
    list.querySelectorAll('.harness-event-filter button').forEach(btn => {
      btn.addEventListener('click', () => {
        list.querySelectorAll('.harness-event-filter button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const f = btn.dataset.filter;
        const filtered = f === 'all' ? _sessionEventsCache : _sessionEventsCache.filter(e => e.event_type === f);
        const eventsContainer = list.querySelector('.harness-events-body');
        if (eventsContainer) eventsContainer.innerHTML = _renderSessionEventRows(filtered);
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

function _renderSessionEvents(events) {
  return `<div class="harness-events-body">${_renderSessionEventRows(events)}</div>`;
}

function _renderSessionEventRows(events) {
  return events.map(e => {
    const hasMeta = e.metadata && Object.keys(e.metadata).length > 0;
    const metaHtml = hasMeta
      ? `<div class="harness-event-meta" hidden>${JSON.stringify(e.metadata, null, 2)}</div>`
      : '';
    const metaBtn = hasMeta
      ? `<button class="btn-ghost btn-xs" onclick="this.nextElementSibling.hidden=!this.nextElementSibling.hidden" title="Toggle metadata">{ }</button>`
      : '';
    return `<div class="harness-event">
      <span class="badge badge-${e.event_type}">${e.event_type}</span>
      <span class="harness-event-content">
        ${truncate(e.content, 120)}
        ${metaBtn}${metaHtml}
      </span>
      <span class="muted-sm">${relativeTime(e.created_at)}</span>
    </div>`;
  }).join('');
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
            <button class="btn-ghost btn-xs btn-danger-text" data-action="scratch-delete" data-key="${e.key}">Delete</button>
            ${!e.promoted ? `<button class="btn-ghost btn-xs" data-action="scratch-promote" data-key="${e.key}">Promote</button>` : ''}
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


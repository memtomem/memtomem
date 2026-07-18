/**
 * Tab Help System + Harness panels (Sessions, Search Runs, Scratch,
 * Procedures, Health).
 *
 * Depends on globals from app.js. Loaded AFTER app.js.
 */

// ---------------------------------------------------------------------------
// Tab Help System (A + C)
// ---------------------------------------------------------------------------

const _HELP_TABS = ['search', 'sources', 'index', 'tags', 'timeline', 'ctx-overview'];
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
  renderPageState(list, { kind: 'loading', message: t('common.loading') });
  try {
    const data = await api('GET', '/api/sessions?limit=50');
    if (!data.sessions.length) {
      renderPageState(list, { kind: 'empty', message: t('settings.sessions.empty') });
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
    renderPageState(list, { kind: 'error', message: t('settings.sessions.load_failed'), detail: e.message, retry: loadHarnessSessions });
  }
}

let _sessionEventsCache = [];

async function showSessionEvents(sessionId) {
  const panel = qs('session-events-panel');
  const list = qs('session-events-list');
  qs('session-events-title').textContent = `Events: ${sessionId.slice(0, 8)}...`;
  show(panel);
  list.innerHTML = `<div class="spinner-panel"></div>${srLoading()}`;
  try {
    const data = await api('GET', `/api/sessions/${sessionId}/events`);
    _sessionEventsCache = data.events;
    if (!data.events.length) {
      renderPageState(list, { kind: 'empty', message: t('settings.sessions.events_empty') });
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
    renderPageState(list, { kind: 'error', message: t('settings.sessions.events_load_failed'), detail: e.message, retry: () => showSessionEvents(sessionId) });
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

// ── Harness: Search Runs (Quality Lab #1801) ──
//
// Every server-derived value goes through escapeHtml (query text is user
// input; snapshot metadata is file-derived) and IDs placed in URLs through
// encodeURIComponent — do not copy the unescaped sessions interpolation.

async function loadHarnessSearchRuns() {
  const list = qs('search-runs-list');
  renderPageState(list, { kind: 'loading', message: t('common.loading') });
  try {
    const data = await api('GET', '/api/search/runs?limit=50');
    if (!data.runs.length) {
      renderPageState(list, { kind: 'empty', message: t('settings.search_runs.empty') });
      return;
    }
    list.innerHTML = '<table class="harness-table"><thead><tr>' +
      '<th>Time</th><th>Query</th><th>Origin</th><th>Results</th><th>Feedback</th><th></th>' +
      '</tr></thead><tbody>' +
      data.runs.map(r => `<tr>
          <td>${relativeTime(r.created_at)}</td>
          <td>${escapeHtml(truncate(r.query_text, 60))}</td>
          <td class="mono">${escapeHtml(r.origin || '—')}</td>
          <td>${Number(r.result_count) || 0}</td>
          <td>${Number(r.feedback_count) || 0}</td>
          <td><button class="btn-ghost btn-xs" data-action="search-run-inspect" data-id="${escapeAttr(r.run_id)}">${t('settings.search_runs.inspect')}</button></td>
        </tr>`).join('') +
      '</tbody></table>';
  } catch (e) {
    renderPageState(list, { kind: 'error', message: t('settings.search_runs.load_failed'), detail: e.message, retry: loadHarnessSearchRuns });
  }
}

async function showSearchRunDetail(runId) {
  const panel = qs('search-run-detail-panel');
  const list = qs('search-run-detail');
  qs('search-run-detail-title').textContent =
    `${t('settings.search_runs.detail_title')}: ${runId.slice(0, 8)}...`;
  show(panel);
  list.innerHTML = `<div class="spinner-panel"></div>${srLoading()}`;
  try {
    const d = await api('GET', `/api/search/runs/${encodeURIComponent(runId)}`);
    const o = d.observation || {};
    const meta = [
      `origin=${escapeHtml(o.origin || '—')}`,
      `top_k=${Number(o.top_k) || '—'}`,
      o.latency_ms != null ? `latency=${Number(o.latency_ms)}ms` : '',
      `cache_hit=${o.cache_hit ? 'yes' : 'no'}`,
      o.profile_id ? `profile=${escapeHtml(String(o.profile_id).slice(0, 8))}` : '',
    ].filter(Boolean).join(' · ');
    const judgeBtn = (rw, judgment) =>
      `<button class="btn-ghost btn-xs${rw.judgment === judgment ? ' active' : ''}"
        data-action="search-run-judge" data-id="${escapeAttr(runId)}"
        data-chunk="${escapeAttr(rw.chunk_id)}" data-judgment="${judgment}">
        ${t(`settings.search_runs.judgment_${judgment}`)}</button>`;
    list.innerHTML = `
      <div class="muted-sm">“${escapeHtml(truncate(d.query_text, 120))}” — ${relativeTime(d.created_at)}</div>
      <div class="muted-sm mono">${meta}</div>
      <div><button class="btn-ghost btn-xs" data-action="search-run-promote" data-id="${escapeAttr(runId)}">${t('settings.search_runs.promote')}</button></div>
      <table class="harness-table"><thead><tr>
        <th>#</th><th>Source</th><th>Score</th><th>Judgment</th><th></th>
      </tr></thead><tbody>` +
      d.results.map(rw => `<tr>
          <td>${Number(rw.rank) || '—'}</td>
          <td>${escapeHtml(truncate(rw.source_name || rw.chunk_id, 48))}</td>
          <td>${rw.score != null ? Number(rw.score).toFixed(3) : '—'}</td>
          <td>${rw.judgment ? `<span class="badge">${escapeHtml(rw.judgment)}</span>` : '—'}</td>
          <td>${judgeBtn(rw, 'relevant')} ${judgeBtn(rw, 'not_relevant')}</td>
        </tr>`).join('') +
      '</tbody></table>';
  } catch (e) {
    renderPageState(list, { kind: 'error', message: t('settings.search_runs.load_failed'), detail: e.message, retry: () => showSearchRunDetail(runId) });
  }
}

async function submitSearchRunJudgment(runId, chunkId, judgment) {
  const post = (replace) => api(
    'POST',
    `/api/search/runs/${encodeURIComponent(runId)}/feedback`,
    { chunk_id: chunkId, judgment, replace },
  );
  try {
    await post(false);
  } catch (e) {
    // 409 = a different judgment exists; replacement is a deliberate act.
    if (e && e.status === 409) {
      if (!confirm(t('settings.search_runs.replace_confirm'))) return;
      try {
        await post(true);
      } catch (e2) {
        showToast(t('settings.search_runs.save_failed', { error: e2.message }), 'error');
        return;
      }
    } else {
      showToast(t('settings.search_runs.save_failed', { error: e.message }), 'error');
      return;
    }
  }
  showToast(t('settings.search_runs.saved'), 'success');
  showSearchRunDetail(runId);
  loadHarnessSearchRuns();
}

qs('search-runs-refresh-btn')?.addEventListener('click', loadHarnessSearchRuns);
qs('search-run-detail-close')?.addEventListener('click', () => hide(qs('search-run-detail-panel')));

// ── Harness: Quality Lab (#1802 PR-5) ──
//
// Dev-only advisory panel: list evaluation cases and replay them into a
// deterministic report. Replay runs cases serially server-side, so the POST
// uses a long timeout and the button is disabled while in flight. A job/poll
// design is deliberately not used — case sets are small by construction
// (promotion is a manual act) and this surface is dev-only.

async function loadHarnessQuality() {
  const list = qs('quality-cases-list');
  renderPageState(list, { kind: 'loading', message: t('common.loading') });
  try {
    const data = await api('GET', '/api/quality/cases');
    if (!data.cases.length) {
      renderPageState(list, { kind: 'empty', message: t('settings.quality.empty') });
      return;
    }
    list.innerHTML = '<table class="harness-table"><thead><tr>' +
      '<th>Case</th><th>Name</th><th>Status</th><th>Labels</th><th>Query</th><th>Created</th>' +
      '</tr></thead><tbody>' +
      data.cases.map(c => `<tr>
          <td class="mono">${escapeHtml(c.case_id.slice(0, 8))}</td>
          <td title="${escapeAttr(c.name || '')}">${escapeHtml(truncate(c.name || '—', 24))}</td>
          <td><span class="badge">${escapeHtml(c.status)}</span></td>
          <td>${Number(c.label_count) || 0}</td>
          <td>${escapeHtml(truncate(c.query_text, 60))}</td>
          <td>${relativeTime(c.created_at)}</td>
        </tr>`).join('') +
      '</tbody></table>';
  } catch (e) {
    renderPageState(list, { kind: 'error', message: t('settings.quality.load_failed'), detail: e.message, retry: loadHarnessQuality });
  }
}

async function runQualityReplay() {
  const panel = qs('quality-report-panel');
  const report = qs('quality-report');
  const btn = qs('quality-replay-btn');
  show(panel);
  report.innerHTML = `<div class="spinner-panel"></div>${srLoading()}`;
  if (btn) btn.disabled = true;
  try {
    const data = await api('POST', '/api/quality/replay', {}, { timeout: 120_000 });
    renderQualityReport(data);
  } catch (e) {
    renderPageState(report, { kind: 'error', message: t('settings.quality.replay_failed'), detail: e.message, retry: runQualityReplay });
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderQualityReport(rep) {
  const report = qs('quality-report');
  const counts = rep.counts || {};
  const agg = rep.aggregate || {};
  const header = [
    `replayed=${Number(counts.replayed) || 0}`,
    `archived_skipped=${Number(counts.archived_skipped) || 0}`,
    `degraded=${Number(counts.degraded) || 0}`,
    `excluded=${Number(counts.excluded_from_aggregate) || 0}`,
    rep.as_of_unix != null ? `as_of=${Number(rep.as_of_unix)}` : '',
  ].filter(Boolean).join(' · ');
  const warn = rep.deterministic === false
    ? `<div class="muted-sm">⚠ ${escapeHtml(t('settings.quality.nondeterministic', { stages: (rep.nondeterministic_stages || []).join(', ') }))}</div>`
    : '';
  const fmt = (v) => (v == null ? 'n/a' : Number(v).toFixed(3));
  const rows = (rep.cases || []).map(c => {
    const m = c.metrics || {};
    const label = c.name || (c.case_id || '').slice(0, 8);
    const flags = (c.flags || []).map(f => `<span class="badge">${escapeHtml(f)}</span>`).join(' ');
    return `<tr>
        <td title="${escapeAttr(c.case_id || '')}">${escapeHtml(truncate(label, 24))}</td>
        <td>${m.hit_rate == null ? 'n/a' : Number(m.hit_rate).toFixed(0)}</td>
        <td>${fmt(m.reciprocal_rank)}</td>
        <td>${fmt(m.recall_labeled)}</td>
        <td>${fmt(m.ndcg)}</td>
        <td>${m.precision == null ? 'n/a' : Number(m.precision).toFixed(3)}</td>
        <td>${flags}</td>
      </tr>`;
  }).join('');
  report.innerHTML = `
    <div class="muted-sm mono">${escapeHtml(header)}</div>
    ${warn}
    <table class="harness-table"><thead><tr>
      <th>Case</th><th>hit</th><th>rr</th><th>recall</th><th>ndcg</th><th>p</th><th>Flags</th>
    </tr></thead><tbody>${rows}</tbody></table>
    <div class="muted-sm">${escapeHtml(t('settings.quality.aggregate'))}: ` +
    `hit_rate=${fmt(agg.mean_hit_rate)} · mrr=${fmt(agg.mrr)} · ` +
    `recall=${fmt(agg.mean_recall_labeled)} · ndcg=${fmt(agg.mean_ndcg)} ` +
    `(${Number(agg.evaluated_cases) || 0})</div>`;
}

async function promoteSearchRun(runId) {
  try {
    const resp = await api('POST', '/api/quality/cases', { run_id: runId });
    showToast(t('settings.search_runs.promote_success', { case: (resp.case_id || '').slice(0, 8) }), 'success');
  } catch (e) {
    showToast(t('settings.search_runs.promote_failed', { error: e.message }), 'error');
  }
}

qs('quality-refresh-btn')?.addEventListener('click', loadHarnessQuality);
qs('quality-replay-btn')?.addEventListener('click', runQualityReplay);
qs('quality-report-close')?.addEventListener('click', () => hide(qs('quality-report-panel')));

// ── Harness: Working Memory (Scratch) ──

async function loadHarnessScratch() {
  const list = qs('scratch-list');
  renderPageState(list, { kind: 'loading', message: t('common.loading') });
  try {
    const data = await api('GET', '/api/scratch');
    if (!data.entries.length) {
      renderPageState(list, { kind: 'empty', message: t('settings.scratch.empty') });
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
    renderPageState(list, { kind: 'error', message: t('settings.scratch.load_failed'), detail: e.message, retry: loadHarnessScratch });
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
    const resp = await apiWithRedactionRetry(
      'POST',
      `/api/scratch/${encodeURIComponent(key)}/promote`,
      {},
    );
    if (resp === null) return;
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
  renderPageState(list, { kind: 'loading', message: t('common.loading') });
  try {
    const data = await api('GET', '/api/procedures');
    if (!data.procedures.length) {
      renderPageState(list, { kind: 'empty', message: t('settings.procedures.empty') });
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
    renderPageState(list, { kind: 'error', message: t('settings.procedures.load_failed'), detail: e.message, retry: loadHarnessProcedures });
  }
}

qs('procedures-refresh-btn')?.addEventListener('click', loadHarnessProcedures);

// ── Harness: Health Report ──

async function loadHarnessHealth() {
  const report = qs('health-report');
  renderPageState(report, { kind: 'loading', message: t('common.loading') });
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
    renderPageState(report, { kind: 'error', message: t('settings.health.load_failed'), detail: e.message, retry: loadHarnessHealth });
  }
}

qs('health-refresh-btn')?.addEventListener('click', loadHarnessHealth);


// ADR-0006 PR-B (Axis E.1 audit surface): the GUI view of ``privacy.snapshot()``
// — process-lifetime redaction counters (blocked / bypassed / pass /
// project_shared) totalled and broken down per write surface. Mirrors the MCP
// ``mem_add_redaction_stats`` tool. Surface names are internal constants but
// escaped defensively; outcome labels come from ``t()`` (trusted locale).
async function loadRedactionStats() {
  const report = qs('redaction-stats-report');
  if (!report) return;
  renderPageState(report, { kind: 'loading', message: t('common.loading') });
  try {
    const d = await api('GET', '/api/privacy/stats');
    const outcomes = (d && d.outcomes) || {};
    const byTool = (d && d.by_tool) || {};
    // Fixed outcome set (privacy._VALID_OUTCOMES); ordered so the two "blocked"
    // variants sit together and the security-relevant "bypassed" reads before
    // the benign "pass".
    const OUTCOMES = ['blocked', 'blocked_project_shared', 'bypassed', 'pass'];
    const label = (k) => t(`settings.redaction.outcome.${k}`);
    const cards = OUTCOMES.map(k => `
      <div class="health-card card">
        <div class="health-card-title">${label(k)}</div>
        <div class="stat-value">${Number(outcomes[k]) || 0}</div>
      </div>`).join('');
    const surfaces = Object.keys(byTool).sort();
    const table = surfaces.length ? `
      <div class="health-section">
        <h3>${t('settings.redaction.by_tool_heading')}</h3>
        <table class="harness-table">
          <thead><tr><th>${t('settings.redaction.surface_col')}</th>${
            OUTCOMES.map(k => `<th>${label(k)}</th>`).join('')}</tr></thead>
          <tbody>${surfaces.map(s => `<tr><td class="mono">${escapeHtml(s)}</td>${
            OUTCOMES.map(k => `<td>${Number(byTool[s] && byTool[s][k]) || 0}</td>`).join('')}</tr>`).join('')}</tbody>
        </table>
      </div>` : `<div class="page-state page-state--empty"><span class="page-state-message">${t('settings.redaction.empty')}</span></div>`;
    report.innerHTML = `<div class="health-grid">${cards}</div>${table}`;
  } catch (e) {
    const msg = (e && e.message) || String(e);
    renderPageState(report, { kind: 'error', message: t('settings.redaction.load_failed'), detail: msg, retry: loadRedactionStats });
  }
}

qs('redaction-stats-refresh-btn')?.addEventListener('click', loadRedactionStats);

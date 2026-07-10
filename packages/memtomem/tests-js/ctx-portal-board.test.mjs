/* Context Portal board (ADR-0021 PR4) — render, search/sort, active-switch,
 * inline rename (PATCH), and unregister (DELETE).
 *
 * Drives the production ``context-portal.js`` inside the index.html DOM via
 * the jsdom harness. ``loadCtxProjects`` reads the realm-scoped
 * ``_ctxProjectsCache`` that ``_ctxFetchProjects`` populates from the stubbed
 * ``/api/context/projects`` response, so the test exercises the real fetch →
 * render path rather than poking module state.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCOPES = [
  {
    scope_id: '', project_scope_id: '', label: 'Server CWD', root: '/srv',
    tier: 'project', sources: ['server-cwd'], missing: false, stale: false,
    experimental: false, counts: { skills: 2, commands: 1, agents: 0, 'mcp-servers': 0 },
  },
  {
    scope_id: 'p-alpha', project_scope_id: 'p-alpha', label: 'Alpha', root: '/work/alpha',
    tier: 'project', sources: ['known-projects'], missing: false, stale: false,
    experimental: false, counts: { skills: 5, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
  {
    scope_id: 'p-beta', project_scope_id: 'p-beta', label: 'Beta', root: '/work/beta',
    tier: 'project', sources: ['known-projects'], missing: false, stale: true,
    experimental: false, counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
  {
    scope_id: 'p-gone', project_scope_id: 'p-gone', label: 'Ghost', root: '/work/ghost',
    tier: 'project', sources: ['known-projects'], missing: true, stale: false,
    experimental: false, counts: null,
  },
];

function stubProjects(window, calls) {
  const upstream = window.fetch;
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (calls && (init || url.includes('known-projects'))) calls.push({ url, init: init || {} });
    if (url.startsWith('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: SCOPES, target_scope: 'project_shared' }) };
    }
    if (url.includes('/api/context/known-projects/')) {
      return { ok: true, status: 200, json: async () => ({ scope_id: 'x', label: 'x' }) };
    }
    return upstream(input, init);
  };
}

async function boot(calls) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
  stubProjects(dom.window, calls);
  // Deterministic CSRF + confirm so mutation assertions don't depend on the
  // token-bootstrap fetch or a modal.
  dom.window.ensureCsrfToken = async () => 'tok-123';
  dom.window.showConfirm = async () => true;
  // NB: we deliberately do NOT mark #settings-ctx-projects active here. i18n's
  // post-boot locale load fires a 'langchange'; with the section active that
  // would trigger a second loadCtxProjects (cache empty) that races the
  // explicit call below and bails it at the seq guard. Tests drive
  // loadCtxProjects directly, which renders regardless of the active class.
  return dom;
}

function rowsText(window) {
  return Array.from(window.document.querySelectorAll('.ctx-portal-row .ctx-portal-label'))
    .map(el => el.textContent.trim());
}

// The "Initialized only" toggle is default-ON, hiding stale (uninitialized)
// rows; turn it off when a test needs the full scope set (incl. stale rows).
function showUninitialized(window) {
  const cb = window.document.querySelector('#ctx-portal-hide-uninit');
  if (cb && cb.checked) {
    cb.checked = false;
    cb.dispatchEvent(new window.Event('change', { bubbles: true }));
  }
}

describe('Context Portal board (PR4)', () => {
  it('renders one row per scope with health badges and counts', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    showUninitialized(window); // reveal the stale Beta row hidden by default

    const rows = window.document.querySelectorAll('.ctx-portal-row');
    expect(rows.length).toBe(SCOPES.length);

    // Stale row carries the stale badge; missing row is dimmed + no counts.
    const beta = window.document.querySelector('.ctx-portal-row[data-scope-id="p-beta"]');
    expect(beta.querySelector('.ctx-scope-badge--stale')).not.toBeNull();
    const gone = window.document.querySelector('.ctx-portal-row[data-scope-id="p-gone"]');
    expect(gone.classList.contains('ctx-portal-row--missing')).toBe(true);
    expect(gone.querySelector('.ctx-scope-badge--missing')).not.toBeNull();
    expect(gone.querySelector('.ctx-portal-counts')).toBeNull(); // counts:null → no chips

    // Healthy managed row shows its four count chips.
    const alpha = window.document.querySelector('.ctx-portal-row[data-scope-id="p-alpha"]');
    expect(alpha.querySelectorAll('.ctx-portal-count').length).toBe(4);
  });

  it('Server CWD is pinned first and cannot be renamed/removed', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    // P0-5 (ADR-0026 #1353): the cwd row shows its real folder basename + a
    // "(current folder)" marker instead of the synthetic "Server CWD" label.
    expect(rowsText(window)[0]).toBe('srv (current folder)');
    const cwd = window.document.querySelector('.ctx-portal-row[data-scope-id=""]');
    expect(cwd.querySelector('.ctx-portal-rename')).toBeNull();
    expect(cwd.querySelector('.ctx-portal-remove')).toBeNull();
  });

  it('search filters client-side by label and root', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const search = window.document.getElementById('ctx-portal-search');
    search.value = 'beta';
    search.dispatchEvent(new window.Event('input', { bubbles: true }));
    expect(rowsText(window)).toEqual(['Beta']);

    // No match → empty-state, zero rows.
    search.value = 'zzzz-nope';
    search.dispatchEvent(new window.Event('input', { bubbles: true }));
    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(0);
  });

  it('runtime-filter empty state names the filter, not blank quotes (#1349)', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    // The stubbed runtimes fetch falls through to a rejecting upstream, so no
    // scope is registered for any runtime. With the search box empty, filtering
    // to Kimi empties the board purely via the filter chip predicate.
    const kimiBtn = window.document.querySelector('.ctx-portal-filter-group button[data-filter="kimi"]');
    expect(kimiBtn).not.toBeNull();
    kimiBtn.dispatchEvent(new window.Event('click', { bubbles: true }));

    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(0);
    const empty = window.document.querySelector('#ctx-projects-rows .empty-state');
    expect(empty).not.toBeNull();
    // Names the real cause (the filter), and never the pre-#1349 literal empty
    // quotes from interpolating a blank search query into portal_no_match.
    expect(empty.textContent).toContain('Kimi');
    expect(empty.textContent).not.toContain('""');
  });

  it('sort by Most items orders the non-cwd rows by total count desc', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const sort = window.document.getElementById('ctx-portal-sort');
    sort.value = 'items';
    sort.dispatchEvent(new window.Event('change', { bubbles: true }));
    // CWD pinned first; then Alpha(5) > Beta(0) = Ghost(0).
    const labels = rowsText(window);
    expect(labels[0]).toBe('srv (current folder)');  // P0-5: basename + marker
    expect(labels[1]).toBe('Alpha');
  });

  it('sort by Most items groups rows with failed count probes last (#1692 PR 5)', async () => {
    const { window } = await boot();
    // Local roster: partial totals (failed kinds ride as 0) must not rank
    // against reliable ones — Zulu's partial 18 would otherwise beat Alpha's
    // reliable 5. Within the unavailable tail, name order keeps it stable.
    const base = {
      tier: 'project', sources: ['known-projects'], missing: false, stale: false,
      experimental: false,
    };
    const scopes = [
      SCOPES[0],  // Server CWD (pinned first regardless)
      { ...base, scope_id: 'p-alpha', project_scope_id: 'p-alpha', label: 'Alpha',
        root: '/work/alpha', counts: { skills: 5, commands: 0, agents: 0, 'mcp-servers': 0 },
        counts_unavailable: [] },
      { ...base, scope_id: 'p-omega', project_scope_id: 'p-omega', label: 'Omega',
        root: '/work/omega', counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
        counts_unavailable: [] },
      { ...base, scope_id: 'p-zulu', project_scope_id: 'p-zulu', label: 'Zulu',
        root: '/work/zulu', counts: { skills: 9, commands: 9, agents: 0, 'mcp-servers': 0 },
        counts_unavailable: ['agents'] },
      { ...base, scope_id: 'p-mike', project_scope_id: 'p-mike', label: 'Mike',
        root: '/work/mike', counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
        counts_unavailable: ['skills'] },
    ];
    const upstream = window.fetch;
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url.startsWith('/api/context/projects')) {
        return { ok: true, status: 200, json: async () => ({ scopes, target_scope: 'project_shared' }) };
      }
      return upstream(input, init);
    };
    await window.loadCtxProjects();
    const sort = window.document.getElementById('ctx-portal-sort');
    sort.value = 'items';
    sort.dispatchEvent(new window.Event('change', { bubbles: true }));
    // CWD, then reliable rows by total (Alpha 5 > Omega 0), then the
    // unavailable group in name order (Mike < Zulu) despite Zulu's higher
    // partial total.
    const labels = rowsText(window);
    expect(labels[0]).toBe('srv (current folder)');
    expect(labels.slice(1)).toEqual(['Alpha', 'Omega', 'Mike', 'Zulu']);
  });

  it('switching active project persists the id and marks the row active', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const useBtn = window.document.querySelector('.ctx-portal-row[data-scope-id="p-alpha"] .ctx-portal-use');
    expect(useBtn).not.toBeNull();
    useBtn.dispatchEvent(new window.Event('click', { bubbles: true }));
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('p-alpha');
    const alpha = window.document.querySelector('.ctx-portal-row[data-scope-id="p-alpha"]');
    expect(alpha.classList.contains('ctx-portal-row--active')).toBe(true);
    expect(alpha.querySelector('.ctx-portal-active-badge')).not.toBeNull();
  });

  it('inline rename PATCHes with the CSRF token threaded', async () => {
    const calls = [];
    const { window } = await boot(calls);
    await window.loadCtxProjects();

    window.document.querySelector('.ctx-portal-row[data-scope-id="p-alpha"] .ctx-portal-rename')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    const input = window.document.querySelector('.ctx-portal-label-input');
    expect(input).not.toBeNull();
    input.value = 'Alpha Prod';
    window.document.querySelector('.ctx-portal-label-save')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));

    const patch = calls.find(c => (c.init.method || '').toUpperCase() === 'PATCH');
    expect(patch).toBeTruthy();
    expect(patch.url).toContain('/api/context/known-projects/p-alpha');
    expect(patch.init.headers['X-Memtomem-CSRF']).toBe('tok-123');
    expect(JSON.parse(patch.init.body)).toEqual({ label: 'Alpha Prod' });
  });

  it('unregister DELETEs after confirmation with the CSRF token', async () => {
    const calls = [];
    const { window } = await boot(calls);
    await window.loadCtxProjects();
    showUninitialized(window); // p-beta is stale (uninitialized) — reveal it

    window.document.querySelector('.ctx-portal-row[data-scope-id="p-beta"] .ctx-portal-remove')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));

    const del = calls.find(c => (c.init.method || '').toUpperCase() === 'DELETE');
    expect(del).toBeTruthy();
    expect(del.url).toContain('/api/context/known-projects/p-beta');
    expect(del.init.headers['X-Memtomem-CSRF']).toBe('tok-123');
  });
});

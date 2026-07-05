/* Projects-portal fleet-drift badge (#1649) — the sole web consumer of
 * GET /api/context/status-all.
 *
 * The board paints from /api/context/projects (+ per-scope /runtimes) first,
 * then fires a NOT-awaited status-all fetch whose result marks drifted projects
 * with a ``ctx-scope-badge--drift`` chip. These tests drive the real
 * ``context-portal.js`` in the jsdom harness and assert the rendered DOM (the
 * ``_ctxPortalDriftMap`` state is a module ``let``, pinned through the badge it
 * produces, not poked directly).
 *
 * Mutation that bites: dropping the ``requestedScope === 'project_shared'`` gate
 * makes the tier-gate test fetch status-all on project_local; dropping the seq
 * guard makes the race test paint a badge from the superseded payload.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function scope(id, label, extra = {}) {
  return {
    scope_id: id, project_scope_id: id, label, root: id ? `/work/${label}` : '/srv',
    tier: 'project', sources: id ? ['known-projects'] : ['server-cwd'],
    missing: false, stale: false, experimental: false,
    counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 0 },
    ...extra,
  };
}

// All visible (not stale, not missing) so every row renders under the default
// "Initialized only" toggle.
const CWD = scope('', 'Server CWD');
const P_ALPHA = scope('p-alpha', 'Alpha');
const P_CLEAN = scope('p-clean', 'Clean');
const P_ERR = scope('p-err', 'Err');
const SCOPES = [CWD, P_ALPHA, P_CLEAN, P_ERR];

// status-all payload: p-alpha drifted; the rest ok/error (neither is drift, so
// neither gets a badge). Server CWD ('') clean.
function driftPayload() {
  return {
    target_scope: 'project_shared',
    projects: [
      { project_scope_id: '', status: 'ok' },
      { project_scope_id: 'p-alpha', status: 'drift' },
      { project_scope_id: 'p-clean', status: 'ok' },
      { project_scope_id: 'p-err', status: 'error', error: { error_kind: 'x', message: 'y', http_status: 500 } },
    ],
    summary: { projects_total: 4, executed: 4, drifted: 1, clean: 2, errors: 1, skipped: 0 },
  };
}

function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body };
}

/* Records every fetched URL and routes the portal's three endpoints. status-all
 * is settable: a fixed payload by default, or a deferred promise a test resolves
 * to control resolution order. */
function installFetch(window, opts = {}) {
  const upstream = window.fetch;
  const urls = [];
  const state = {
    statusAll: opts.statusAll || (async () => jsonOk(driftPayload())),
  };
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    urls.push(url);
    if (url.startsWith('/api/context/status-all')) return state.statusAll(url, init);
    if (url.startsWith('/api/context/projects')) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ scopes: SCOPES, target_scope: 'project_shared' }) });
    }
    if (url.startsWith('/api/context/runtimes')) return Promise.resolve(jsonOk({ runtimes: [] }));
    return upstream(input, init);
  };
  return { urls, state };
}

// Drain pending microtasks/timers so the not-awaited status-all fetch settles.
function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function boot() {
  // Section intentionally left inactive: i18n's post-boot locale load fires a
  // 'langchange' that, with the section active, would trigger a second
  // loadCtxProjects racing the explicit call. Tests drive it directly.
  return bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
}

function driftBadge(window, scopeId) {
  return window.document.querySelector(
    `.ctx-portal-row[data-scope-id="${scopeId}"] .ctx-scope-badge--drift`,
  );
}

describe('Projects portal fleet-drift badge (#1649)', () => {
  it('marks the drifted project after the deferred status-all fetch, with a runnable tooltip', async () => {
    const { window } = await boot();
    installFetch(window);
    await window.loadCtxProjects();
    // Not painted yet — status-all is deferred past the initial render.
    expect(driftBadge(window, 'p-alpha')).toBeNull();
    await flush();

    const badge = driftBadge(window, 'p-alpha');
    expect(badge).not.toBeNull();
    expect(badge.textContent.trim().length).toBeGreaterThan(0);
    expect(badge.getAttribute('title')).toContain('mm context status --all-projects');
  });

  it('leaves ok / error projects (and Server CWD) unbadged', async () => {
    const { window } = await boot();
    installFetch(window);
    await window.loadCtxProjects();
    await flush();

    expect(driftBadge(window, 'p-alpha')).not.toBeNull();
    expect(driftBadge(window, 'p-clean')).toBeNull();
    expect(driftBadge(window, 'p-err')).toBeNull();
    expect(driftBadge(window, '')).toBeNull();
  });

  it('does NOT fetch status-all on a non-project_shared tier', async () => {
    const { window } = await boot();
    const { urls } = installFetch(window);
    // Flip the tier to project_local via the real toggle wiring, then load.
    window.loadCtxOverview = async () => {};
    const bar = window.document.createElement('div');
    bar.innerHTML = '<div class="ctx-tier-filter" data-type="overview">'
      + '<button type="button" data-scope="project_local">local</button></div>';
    window.document.body.appendChild(bar);
    window._ctxWireTierControls();
    bar.querySelector('button').dispatchEvent(new window.Event('click', { bubbles: true }));

    await window.loadCtxProjects();
    await flush();
    expect(urls.some(u => u.startsWith('/api/context/status-all'))).toBe(false);
  });

  it('discards a superseded status-all payload (seq guard)', async () => {
    const { window } = await boot();
    // First load's status-all is parked; we resolve it AFTER a newer load.
    let releaseOld;
    const parked = new Promise((resolve) => { releaseOld = resolve; });
    const { state } = installFetch(window, { statusAll: () => parked });

    const older = window.loadCtxProjects(); // seq 1 — status-all parked
    await older;
    // Newer load resolves status-all immediately with the drift payload.
    state.statusAll = async () => jsonOk(driftPayload());
    await window.loadCtxProjects(); // seq 2
    await flush();
    expect(driftBadge(window, 'p-alpha')).not.toBeNull();

    // Now release the stale (seq 1) fetch. Even though it also reports drift,
    // the seq guard must drop it — and here it would repaint from the SAME
    // payload, so to prove the guard we assert the map wasn't rebuilt by it: an
    // all-clean stale payload must not clear the badge the newer load set.
    state.statusAll = async () => jsonOk(driftPayload());
    releaseOld(jsonOk({ target_scope: 'project_shared', projects: [], summary: {} }));
    await flush();
    expect(driftBadge(window, 'p-alpha')).not.toBeNull();
  });

  it('survives a status-all failure with the board intact and no badge', async () => {
    const { window } = await boot();
    installFetch(window, { statusAll: async () => ({ ok: false, status: 500, json: async () => ({}) }) });
    await window.loadCtxProjects();
    await flush();
    // Board rendered, no throw, no badge.
    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(SCOPES.length);
    expect(driftBadge(window, 'p-alpha')).toBeNull();
  });

  it('re-renders the badge with the active locale on langchange', async () => {
    const { window } = await boot();
    installFetch(window);
    await window.loadCtxProjects();
    await flush();
    const before = driftBadge(window, 'p-alpha');
    expect(before).not.toBeNull();

    window.dispatchEvent(new window.Event('langchange'));
    // langchange repaints the whole board from the drift map — badge persists.
    const after = driftBadge(window, 'p-alpha');
    expect(after).not.toBeNull();
    expect(after.textContent.trim().length).toBeGreaterThan(0);
  });

  it('clears a stale badge when a fresh load reports all clean', async () => {
    const { window } = await boot();
    const { state } = installFetch(window);
    await window.loadCtxProjects();
    await flush();
    expect(driftBadge(window, 'p-alpha')).not.toBeNull();

    // Reload: status-all now reports every project clean.
    state.statusAll = async () => jsonOk({
      target_scope: 'project_shared',
      projects: SCOPES.map(s => ({ project_scope_id: s.scope_id, status: 'ok' })),
      summary: { projects_total: 4, executed: 4, drifted: 0, clean: 4, errors: 0, skipped: 0 },
    });
    await window.loadCtxProjects();
    await flush();
    expect(driftBadge(window, 'p-alpha')).toBeNull();
  });
});

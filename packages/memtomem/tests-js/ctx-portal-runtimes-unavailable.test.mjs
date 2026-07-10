/* Projects-portal runtimes-unavailable chip + row light (#1692 PR 6).
 *
 * GET /api/context/runtimes now carries ``runtimes_status`` ('ok' |
 * 'unavailable') plus a ``warnings`` envelope. Before this, a failed probe
 * (route-level or fetch-level) collapsed to ``runtimes: []`` — rendered as
 * four grey "uninstalled" chips and dots, false-healthy. These tests drive
 * the real ``context-portal.js`` in the jsdom harness.
 *
 * Mutations that bite: dropping the unavailable branch from the heading
 * chips / row lights regresses to silent all-grey; dropping the
 * ``_ctxIsAbortError`` guard stamps a superseded load's abort as
 * unavailable; dropping the seq guard lets a stale load's late runtimes
 * result overwrite the winning load's availability verdict.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function scope(id, label, extra = {}) {
  return {
    scope_id: id, project_scope_id: id, label, root: id ? `/work/${label}` : '/srv',
    tier: 'project', sources: id ? ['known-projects'] : ['server-cwd'],
    missing: false, stale: false, experimental: false,
    counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 0 },
    counts_unavailable: [],
    ...extra,
  };
}

const CWD = scope('', 'Server CWD');
const P_OTHER = scope('p-other', 'Other');

function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body };
}

function projectsPayload(scopes) {
  return {
    target_scope: 'project_shared',
    scopes,
    registry_status: 'ok',
    warnings: [],
  };
}

function runtimesOk() {
  return jsonOk({
    project_root: '/srv',
    runtimes: [
      {
        name: 'claude', installed: true, memtomem_registered: true, mms_registered: false,
        registered_locations: ['cli'], config_paths: ['~/.claude.json'], error_kind: null,
      },
    ],
    runtimes_status: 'ok',
    warnings: [],
  });
}

function runtimesUnavailable() {
  return jsonOk({
    project_root: '/srv',
    runtimes: [],
    runtimes_status: 'unavailable',
    warnings: [{
      reason_code: 'status_unavailable', error_kind: 'internal',
      message: 'probe machinery exploded', retryable: true,
    }],
  });
}

/* Records every fetched URL and routes the portal's three endpoints. Both
 * projects and runtimes are settable so a test can repair a probe between
 * loads or hand out deferred/rejecting responses. */
function installFetch(window, opts = {}) {
  const upstream = window.fetch;
  const urls = [];
  const state = {
    projects: opts.projects || (async () => jsonOk(projectsPayload([CWD, P_OTHER]))),
    runtimes: opts.runtimes || (async () => runtimesOk()),
  };
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    urls.push(url);
    if (url.startsWith('/api/context/status-all')) {
      return Promise.resolve(jsonOk({ target_scope: 'project_shared', projects: [], summary: {} }));
    }
    if (url.startsWith('/api/context/projects')) return state.projects(url, init);
    if (url.startsWith('/api/context/runtimes')) return state.runtimes(url, init);
    return upstream(input, init);
  };
  return { urls, state };
}

// Drain pending microtasks/timers (Retry's un-awaited loadCtxProjects).
function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function boot() {
  // Section intentionally left inactive: i18n's post-boot locale load fires a
  // 'langchange' that, with the section active, would trigger a second
  // loadCtxProjects racing the explicit call. Tests drive it directly.
  return bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
}

function row(window, id) {
  return window.document.querySelector(`.ctx-portal-row[data-scope-id="${id}"]`);
}

function headingChips(window) {
  return window.document.getElementById('ctx-portal-heading-chips');
}

describe('Projects portal runtimes-unavailable state (#1692 PR 6)', () => {
  it('renders one accessible unavailable chip + Retry — never four grey chips — on runtimes_status unavailable', async () => {
    const { window } = await boot();
    installFetch(window, { runtimes: async () => runtimesUnavailable() });
    await window.loadCtxProjects();

    const heading = headingChips(window);
    const chip = heading.querySelector('.ctx-runtime-chip--unavailable');
    expect(chip).not.toBeNull();
    expect(chip.textContent.trim().length).toBeGreaterThan(0);
    // State must not ride on color alone (WCAG 1.4.1) — same convention as
    // the sibling status chips and the row dots.
    expect(chip.getAttribute('role')).toBe('img');
    expect((chip.getAttribute('aria-label') || '').length).toBeGreaterThan(0);
    // The whole point: unknown must not read as "nothing installed".
    expect(heading.querySelector('.ctx-runtime-chip--greyed')).toBeNull();
    expect(heading.querySelector('.ctx-runtime-chip--registered')).toBeNull();
    const retry = heading.querySelector('.ctx-portal-runtimes-retry');
    expect(retry).not.toBeNull();
    // The Retry aria-label names the active scope (here the server-cwd row,
    // shown by its folder basename) so it is distinguishable from the
    // registry / counts Retry controls on the same view.
    expect(retry.getAttribute('aria-label')).toContain('runtime status');
    expect(retry.getAttribute('aria-label')).toContain('srv');

    // Row lights collapse to a single accessible unknown-state light.
    const light = row(window, 'p-other').querySelector('.ctx-portal-row-light--unavailable');
    expect(light).not.toBeNull();
    expect(light.getAttribute('role')).toBe('img');
    expect((light.getAttribute('aria-label') || '').length).toBeGreaterThan(0);
    expect(row(window, 'p-other').querySelector('.ctx-portal-row-light--uninstalled')).toBeNull();
  });

  it('Retry re-fetches, and a repaired probe restores the normal chips', async () => {
    const { window } = await boot();
    const { urls, state } = installFetch(window, { runtimes: async () => runtimesUnavailable() });
    await window.loadCtxProjects();
    expect(headingChips(window).querySelector('.ctx-runtime-chip--unavailable')).not.toBeNull();

    const projectsFetches = () =>
      urls.filter((u) => u.startsWith('/api/context/projects') && !u.includes('include=')).length;
    const before = projectsFetches();
    state.runtimes = async () => runtimesOk();
    headingChips(window)
      .querySelector('.ctx-portal-runtimes-retry')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    await flush();

    expect(projectsFetches()).toBe(before + 1);
    const heading = headingChips(window);
    expect(heading.querySelector('.ctx-runtime-chip--unavailable')).toBeNull();
    expect(heading.querySelector('.ctx-runtime-chip--registered')).not.toBeNull();
    expect(row(window, 'p-other').querySelector('.ctx-portal-row-light--unavailable')).toBeNull();
  });

  it('tolerates an old-server payload (no runtimes_status) — legacy chip path', async () => {
    const { window } = await boot();
    installFetch(window, {
      runtimes: async () => jsonOk({ project_root: '/srv', runtimes: [] }),
    });
    await window.loadCtxProjects();

    const heading = headingChips(window);
    expect(heading.querySelector('.ctx-runtime-chip--unavailable')).toBeNull();
    expect(heading.querySelectorAll('.ctx-runtime-chip--greyed').length).toBe(4);
  });

  it('marks a non-OK runtimes response unavailable (the silent-[] regression bite)', async () => {
    const { window } = await boot();
    installFetch(window, {
      runtimes: async () => ({ ok: false, status: 500, json: async () => ({}) }),
    });
    await window.loadCtxProjects();

    expect(headingChips(window).querySelector('.ctx-runtime-chip--unavailable')).not.toBeNull();
    expect(row(window, 'p-other').querySelector('.ctx-portal-row-light--unavailable')).not.toBeNull();
  });

  it('does NOT mark an aborted runtimes fetch unavailable (superseded load owns the verdict)', async () => {
    const { window } = await boot();
    installFetch(window, {
      runtimes: async () => {
        const err = new Error('aborted');
        err.name = 'AbortError';
        throw err;
      },
    });
    await window.loadCtxProjects();

    const heading = headingChips(window);
    expect(heading.querySelector('.ctx-runtime-chip--unavailable')).toBeNull();
    expect(row(window, 'p-other').querySelector('.ctx-portal-row-light--unavailable')).toBeNull();
    // The abort path degrades to the legacy grey render, not a failure state.
    expect(heading.querySelectorAll('.ctx-runtime-chip--greyed').length).toBe(4);
  });

  it('a superseded load resolving late cannot overwrite the winning load\'s verdict', async () => {
    const { window } = await boot();
    const { state } = installFetch(window);

    // Load 1: runtimes hang, shaped unavailable once released.
    let releaseFirst;
    const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
    state.runtimes = () => firstGate.then(() => runtimesUnavailable());
    const first = window.loadCtxProjects();

    // Load 2 supersedes with healthy runtimes and completes first.
    state.runtimes = async () => runtimesOk();
    await window.loadCtxProjects();
    expect(headingChips(window).querySelector('.ctx-runtime-chip--registered')).not.toBeNull();

    // Release load 1's stale unavailable result — the seq guard must discard
    // it at both commit points (incremental worker + final rebuild).
    releaseFirst();
    await first;
    await flush();

    const heading = headingChips(window);
    expect(heading.querySelector('.ctx-runtime-chip--unavailable')).toBeNull();
    expect(heading.querySelector('.ctx-runtime-chip--registered')).not.toBeNull();
    expect(row(window, 'p-other').querySelector('.ctx-portal-row-light--unavailable')).toBeNull();
  });

  it('paints an early-landing unavailable verdict mid-load (incremental commit, not just the final rebuild)', async () => {
    const { window } = await boot();
    const { state } = installFetch(window);

    // p-other's probe fails fast; the CWD scope's probe hangs until released,
    // so the load stays in flight while p-other's verdict lands.
    let releaseCwd;
    const cwdGate = new Promise((resolve) => { releaseCwd = resolve; });
    state.runtimes = (url) => {
      if (url.includes('scope_id=p-other')) return Promise.resolve(runtimesUnavailable());
      return cwdGate.then(() => runtimesOk());
    };
    const load = window.loadCtxProjects();
    await flush();

    // Mid-load: the incremental worker commit must already show the light.
    const light = row(window, 'p-other').querySelector('.ctx-portal-row-light--unavailable');
    expect(light).not.toBeNull();

    releaseCwd();
    await load;
    expect(row(window, 'p-other').querySelector('.ctx-portal-row-light--unavailable')).not.toBeNull();
  });

  it('tolerates a malformed truthy non-array runtimes payload without throwing', async () => {
    const { window } = await boot();
    installFetch(window, {
      runtimes: async () => jsonOk({ project_root: '/srv', runtimes: { bogus: true }, runtimes_status: 'ok' }),
    });
    await window.loadCtxProjects();

    const heading = headingChips(window);
    expect(heading.querySelector('.ctx-runtime-chip--unavailable')).toBeNull();
    expect(heading.querySelectorAll('.ctx-runtime-chip--greyed').length).toBe(4);
  });
});

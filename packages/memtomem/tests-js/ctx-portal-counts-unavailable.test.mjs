/* Projects-portal "Status unavailable" count pill (#1692 PR 5).
 *
 * GET /api/context/projects rows now carry ``counts_unavailable`` — the list
 * of kind keys whose count probe raised. Failed kinds ride as 0 inside
 * ``counts`` for wire compatibility, so without the new field the Portal
 * zero-suppresses a failed probe into the healthy "Empty" pill — the exact
 * false-confidence bug being fixed. These tests drive the real
 * ``context-portal.js`` in the jsdom harness.
 *
 * Mutation that bites: dropping the unavailable branch from
 * ``_ctxPortalCountsHtml`` falls through to zero-suppression and renders
 * "Empty" for a failed probe; dropping the retry wiring leaves a dead button.
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
// The dangerous shape: probe failed, counts collapsed to all-zero — without
// the new field this row would render the healthy "Empty" pill.
const P_BROKEN = scope('p-broken', 'Broken', {
  counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  counts_unavailable: ['skills'],
});

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

/* Records every fetched URL and routes the portal's three endpoints. projects
 * is settable so a test can repair the probe between loads. */
function installFetch(window, opts = {}) {
  const upstream = window.fetch;
  const urls = [];
  const state = {
    projects: opts.projects || (async () => jsonOk(projectsPayload([CWD, P_BROKEN]))),
  };
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    urls.push(url);
    if (url.startsWith('/api/context/status-all')) {
      return Promise.resolve(jsonOk({ target_scope: 'project_shared', projects: [], summary: {} }));
    }
    if (url.startsWith('/api/context/projects')) return state.projects(url, init);
    if (url.startsWith('/api/context/runtimes')) return Promise.resolve(jsonOk({ runtimes: [] }));
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

describe('Projects portal counts-unavailable pill (#1692 PR 5)', () => {
  it('renders the unavailable pill — never the "Empty" pill or chips — for a failed probe', async () => {
    const { window } = await boot();
    installFetch(window);
    await window.loadCtxProjects();

    const broken = row(window, 'p-broken');
    const pill = broken.querySelector('.ctx-portal-count--unavailable');
    expect(pill).not.toBeNull();
    expect(pill.textContent.trim().length).toBeGreaterThan(0);
    // The failed kinds are disclosed in the tooltip.
    expect(pill.getAttribute('title')).toContain('skills');
    // The whole point: an all-zero payload with a failed probe must NOT read
    // as a healthy empty inventory — no "Empty" pill, no numeric chips.
    expect(broken.querySelector('.ctx-portal-count--empty')).toBeNull();
    expect(broken.querySelectorAll('.ctx-portal-count').length).toBe(1);
    // Retry affordance, labelled for screen readers with the project name.
    const retry = broken.querySelector('.ctx-portal-counts-retry');
    expect(retry).not.toBeNull();
    expect(retry.getAttribute('aria-label')).toContain('Broken');
    // The healthy sibling row keeps its normal chips.
    expect(row(window, '').querySelector('.ctx-portal-count--unavailable')).toBeNull();
  });

  it('Retry re-fetches, and a repaired probe restores the chips', async () => {
    const { window } = await boot();
    const { urls, state } = installFetch(window);
    await window.loadCtxProjects();
    expect(row(window, 'p-broken').querySelector('.ctx-portal-counts-retry')).not.toBeNull();

    const projectsFetches = () =>
      urls.filter((u) => u.startsWith('/api/context/projects')).length;
    const before = projectsFetches();
    const repaired = scope('p-broken', 'Broken', {
      counts: { skills: 3, commands: 0, agents: 0, 'mcp-servers': 0 },
    });
    state.projects = async () => jsonOk(projectsPayload([CWD, repaired]));
    row(window, 'p-broken')
      .querySelector('.ctx-portal-counts-retry')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    await flush();

    expect(projectsFetches()).toBe(before + 1);
    const broken = row(window, 'p-broken');
    expect(broken.querySelector('.ctx-portal-count--unavailable')).toBeNull();
    expect(broken.textContent).toContain('3');
  });

  it('tolerates rows without the new field (older server) — legacy chip path', async () => {
    const { window } = await boot();
    const legacy = scope('p-old', 'Oldie');
    delete legacy.counts_unavailable;
    installFetch(window, {
      projects: async () => jsonOk({ target_scope: 'project_shared', scopes: [CWD, legacy] }),
    });
    await window.loadCtxProjects();

    const oldie = row(window, 'p-old');
    expect(oldie.querySelector('.ctx-portal-count--unavailable')).toBeNull();
    expect(oldie.querySelectorAll('.ctx-portal-count').length).toBeGreaterThan(0);
  });

  it('keeps zero-suppression for a genuine all-zero inventory (counts_unavailable: [])', async () => {
    const { window } = await boot();
    const emptyScope = scope('p-empty', 'Vacant', {
      counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
    });
    installFetch(window, {
      projects: async () => jsonOk(projectsPayload([CWD, emptyScope])),
    });
    await window.loadCtxProjects();

    const vacant = row(window, 'p-empty');
    expect(vacant.querySelector('.ctx-portal-count--empty')).not.toBeNull();
    expect(vacant.querySelector('.ctx-portal-count--unavailable')).toBeNull();
  });
});

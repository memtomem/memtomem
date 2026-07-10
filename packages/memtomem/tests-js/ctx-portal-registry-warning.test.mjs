/* Projects-portal registry read-failure banner (#1692).
 *
 * GET /api/context/projects now carries ``registry_status`` + ``warnings``:
 * a corrupt/unreadable known_projects.json degrades the roster (registered
 * rows vanish) instead of failing the request, so the Portal must say so.
 * These tests drive the real ``context-portal.js`` in the jsdom harness and
 * assert the rendered banner (the ``_ctxPortalRegistryWarning`` state is a
 * module ``let``, pinned through the DOM it produces, not poked directly).
 *
 * Mutation that bites: dropping the banner from ``_ctxPortalRenderScaffold``
 * renders the degraded roster with no signal (the exact false-confidence bug
 * being fixed); dropping the reset in ``loadCtxProjects`` leaves a stale
 * banner after Retry repaired the registry.
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

const CWD = scope('', 'Server CWD');
const P_ALPHA = scope('p-alpha', 'Alpha');
const SCOPES = [CWD, P_ALPHA];

function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body };
}

function projectsPayload(extra = {}) {
  return {
    target_scope: 'project_shared',
    scopes: SCOPES,
    registry_status: 'ok',
    warnings: [],
    ...extra,
  };
}

// Whole-file degradation: registered rows are gone, only server-cwd survives.
function unavailablePayload() {
  return projectsPayload({
    scopes: [CWD],
    registry_status: 'unavailable',
    warnings: [{
      reason_code: 'registry_corrupt', error_kind: 'parse',
      message: 'known_projects file at ~/kp.json is not valid JSON', retryable: true,
      skipped_rows: null,
    }],
  });
}

// Row-level degradation: the document parsed, so the roster and status stay
// intact and the warning carries only the skip count.
function rowSkipPayload() {
  return projectsPayload({
    warnings: [{
      reason_code: 'registry_corrupt', error_kind: 'parse',
      message: 'known_projects file at ~/kp.json has 2 unparsable project row(s), skipped',
      retryable: true, skipped_rows: 2,
    }],
  });
}

/* Records every fetched URL and routes the portal's three endpoints. projects
 * is settable so a test can repair the registry between loads. */
function installFetch(window, opts = {}) {
  const upstream = window.fetch;
  const urls = [];
  const state = {
    projects: opts.projects || (async () => jsonOk(projectsPayload())),
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

function banner(window) {
  return window.document.querySelector('.ctx-portal-registry-banner');
}

describe('Projects portal registry read-failure banner (#1692)', () => {
  it('renders a role="alert" banner when the registry is unavailable — board still paints', async () => {
    const { window } = await boot();
    installFetch(window, { projects: async () => jsonOk(unavailablePayload()) });
    await window.loadCtxProjects();

    const el = banner(window);
    expect(el).not.toBeNull();
    expect(el.getAttribute('role')).toBe('alert');
    const msg = el.querySelector('.ctx-portal-registry-banner-msg');
    expect(msg.textContent.trim().length).toBeGreaterThan(0);
    // The (server-redacted) cause shows as the diagnostic reason line.
    expect(el.querySelector('.ctx-diagnostic-reason').textContent).toContain('known_projects');
    expect(el.querySelector('.ctx-scopes-retry')).not.toBeNull();
    // Non-blocking: the surviving server-cwd row still renders below the
    // banner — the banner must never replace the board the way the
    // load-error surface does.
    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(1);
  });

  it('renders the skip count when rows were dropped but the registry stayed ok', async () => {
    const { window } = await boot();
    installFetch(window, { projects: async () => jsonOk(rowSkipPayload()) });
    await window.loadCtxProjects();

    const el = banner(window);
    expect(el).not.toBeNull();
    // The {count} placeholder is substituted with the wire skipped_rows.
    expect(el.querySelector('.ctx-portal-registry-banner-msg').textContent).toContain('2');
    // Row-skip does not hide the roster: every scope row renders.
    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(SCOPES.length);
  });

  it('renders no banner on a clean payload', async () => {
    const { window } = await boot();
    installFetch(window);
    await window.loadCtxProjects();

    expect(banner(window)).toBeNull();
    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(SCOPES.length);
  });

  it('tolerates payloads without the new fields (older server)', async () => {
    const { window } = await boot();
    installFetch(window, {
      projects: async () => jsonOk({ target_scope: 'project_shared', scopes: SCOPES }),
    });
    await window.loadCtxProjects();

    expect(banner(window)).toBeNull();
    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(SCOPES.length);
  });

  it('Retry re-fetches, and a repaired registry clears the banner', async () => {
    const { window } = await boot();
    const { urls, state } = installFetch(window, {
      projects: async () => jsonOk(unavailablePayload()),
    });
    await window.loadCtxProjects();
    expect(banner(window)).not.toBeNull();

    const projectsFetches = () =>
      urls.filter((u) => u.startsWith('/api/context/projects')).length;
    const before = projectsFetches();
    state.projects = async () => jsonOk(projectsPayload());
    banner(window)
      .querySelector('.ctx-scopes-retry')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    await flush();

    expect(projectsFetches()).toBe(before + 1);
    expect(banner(window)).toBeNull();
    // The repaired registry's registered row is back on the board.
    expect(window.document.querySelectorAll('.ctx-portal-row').length).toBe(SCOPES.length);
  });
});

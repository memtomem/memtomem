import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

/* Regression for the runtime_coverage probe gate (PR #1201 review P2).
 *
 * ``runtime_coverage`` costs a ``probe_all_runtimes`` pass (per-client config
 * reads) for every registered scope. Its only consumer — the overview's
 * Project Scope Matrix — was removed in rank 2, so NO fetch should opt into it
 * anymore: the overview is now a counts-only aggregate dashboard, and the
 * per-type list tabs (``loadCtxList``) only ever needed counts for the scope
 * picker. This pins that neither path re-introduces the expensive probe. */
function installFetch(window, projectsUrls) {
  const upstream = window.fetch;
  const scope = {
    scope_id: '',
    label: 'Server CWD',
    root: '/srv/demo',
    tier: 'project',
    sources: ['server-cwd'],
    missing: false,
    experimental: false,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
    runtime_coverage: [],
  };
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.includes('/api/context/projects')) {
      projectsUrls.push(url);
      return { ok: true, status: 200, json: async () => ({ scopes: [scope] }) };
    }
    if (url.includes('/api/context/overview')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          target_scope: 'project_shared',
          project_root: '/srv/demo',
          detected_runtimes: [],
          skills: { total: 0 },
          commands: { total: 0 },
          agents: { total: 0 },
          mcp_servers: { total: 0 },
          settings: { total: 0, status: 'in_sync' },
        }),
      };
    }
    if (/\/api\/context\/(skills|commands|agents|mcp-servers)(\?|$)/.test(url)) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          skills: [],
          commands: [],
          agents: [],
          'mcp-servers': [],
          scanned_dirs: [],
        }),
      };
    }
    return upstream(input, init);
  };
}

describe('Context Gateway — runtime_coverage probe is not requested (matrix removed)', () => {
  it('neither the overview nor the list-tab projects fetch requests runtime_coverage', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    const projectsUrls = [];
    installFetch(window, projectsUrls);
    await window.I18N.init();

    await window.loadCtxOverview();
    const overviewUrls = projectsUrls.splice(0);
    expect(overviewUrls.length).toBeGreaterThan(0);
    // rank 2: the matrix was the only coverage consumer, so the overview now
    // fetches counts only — never the expensive probe.
    expect(overviewUrls.every((u) => !u.includes('runtime_coverage'))).toBe(true);
    expect(overviewUrls.every((u) => u.includes('include=counts'))).toBe(true);

    await window.loadCtxList('skills');
    const listUrls = projectsUrls.splice(0);
    expect(listUrls.length).toBeGreaterThan(0);
    // The list tab never paid the probe either; counts (scope picker) still rides.
    expect(listUrls.every((u) => !u.includes('runtime_coverage'))).toBe(true);
    expect(listUrls.every((u) => u.includes('include=counts'))).toBe(true);
  });
});

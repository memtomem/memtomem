import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

/* Regression for the runtime_coverage opt-in gate (PR #1201 review P2).
 *
 * ``runtime_coverage`` costs a ``probe_all_runtimes`` pass (per-client config
 * reads) for every registered scope and is consumed ONLY by the overview's
 * Project Scope Matrix. The shared ``_ctxFetchProjectsData`` loader is also
 * used by the per-type list tabs (``loadCtxList``) purely for the scope
 * picker / counts, so the expensive probe must ride the overview fetch only —
 * never the list-tab reloads, which would defeat the gate for multi-project
 * users on normal navigation. */
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

describe('Context Gateway — runtime_coverage is overview-only opt-in', () => {
  it('overview fetch requests runtime_coverage; list-tab fetch requests only counts', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    const projectsUrls = [];
    installFetch(window, projectsUrls);
    await window.I18N.init();

    await window.loadCtxOverview();
    const overviewUrls = projectsUrls.splice(0);
    expect(overviewUrls.length).toBeGreaterThan(0);
    expect(overviewUrls.some((u) => u.includes('runtime_coverage'))).toBe(true);

    await window.loadCtxList('skills');
    const listUrls = projectsUrls.splice(0);
    expect(listUrls.length).toBeGreaterThan(0);
    // The expensive probe must NOT ride the list-tab projects fetch...
    expect(listUrls.every((u) => !u.includes('runtime_coverage'))).toBe(true);
    // ...but cheap counts (needed by the scope picker) still does.
    expect(listUrls.every((u) => u.includes('include=counts'))).toBe(true);
  });
});

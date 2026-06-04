/* rank 11 — the active-project switcher + canonical-tier filter are hoisted out
 * of every gateway section's content into ONE persistent header bar
 * (``#ctx-control-bar``). These guards pin the new contract:
 *   (1) the controls render once, in the bar — never inside the per-section
 *       list/content — for every bar-eligible section;
 *   (2) the bar is hidden on the Projects portal (which owns its own roster)
 *       and restored when switching back to a bar-eligible section;
 *   (3) exactly one instance exists no matter how many sections are visited
 *       (no duplicate/stacked pickers);
 *   (4) a change on the single control fires the ACTIVE section's loader.
 *
 * The static modules ship un-built, so bootApp loads the production index.html
 * (now carrying ``#ctx-control-bar``) and injects context-gateway.js verbatim.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCOPES = [
  {
    scope_id: '', label: 'Server CWD', root: '/srv', tier: 'project',
    sources: ['server-cwd'], missing: false, stale: false, experimental: false,
    counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
  {
    scope_id: 'p1', label: 'Alpha', root: '/work/Alpha', tier: 'project',
    sources: ['known-projects'], missing: false, stale: false, experimental: false,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
];

const OVERVIEW = {
  skills: { total: 0, in_sync: 0 },
  commands: { total: 0, in_sync: 0 },
  agents: { total: 0, in_sync: 0 },
  settings: { total: 0, in_sync: 0, status: 'in_sync' },
  detected_runtimes: [],
  project_root: '/srv',
  target_scope: 'project_shared',
};

function jsonOk(body) {
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => body, text: async () => JSON.stringify(body) };
}

function installFetch(window) {
  const upstream = window.fetch;
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const p = url.split('?')[0];
    if (p.includes('/api/context/projects')) return jsonOk({ scopes: SCOPES, target_scope: 'project_shared' });
    if (p.endsWith('/api/context/overview')) return jsonOk(OVERVIEW);
    if (p.endsWith('/api/context/runtimes')) return jsonOk({ runtimes: [] });
    // Per-scope list items (lazy-loaded on the open group) — empty is fine.
    if (p.match(/\/api\/context\/(skills|commands|agents|mcp-servers)$/)) return jsonOk({ items: [] });
    return upstream(input, init);
  };
}

async function flush(window, ticks = 30) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

// Mimic switchSettingsSection's class bookkeeping for the gateway sections so
// _ctxActiveGatewayType()'s ``.settings-section.active`` lookup resolves the
// intended section without driving the full activateTab/tab-hop machinery.
function setActiveSection(window, sectionId) {
  window.document
    .querySelectorAll('#tab-context-gateway .settings-section')
    .forEach((s) => s.classList.remove('active'));
  const sec = window.document.getElementById(`settings-${sectionId}`);
  if (sec) sec.classList.add('active');
}

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  installFetch(window);
  await window.I18N.init();
  return window;
}

describe('rank 11 — hoisted gateway control bar', () => {
  it('renders the controls once in the header bar, never inside the section list', async () => {
    const window = await boot();
    setActiveSection(window, 'ctx-skills');
    await window.loadCtxList('skills');
    await flush(window);

    const bar = window.document.getElementById('ctx-control-bar');
    expect(bar).not.toBeNull();
    expect(bar.hidden).toBe(false);
    expect(bar.querySelectorAll('.ctx-project-switcher').length).toBe(1);
    expect(bar.querySelectorAll('.ctx-tier-filter').length).toBe(1);
    // The select carries the active-section type so the wire handler routes a
    // change to the right loader.
    expect(bar.querySelector('.ctx-project-switcher').dataset.type).toBe('skills');

    // The section's own list must no longer carry a copy of either control.
    const list = window.document.getElementById('ctx-skills-list');
    expect(list.querySelectorAll('.ctx-project-switcher').length).toBe(0);
    expect(list.querySelectorAll('.ctx-tier-filter').length).toBe(0);
  });

  it('keeps a single bar instance (no stacking) across section visits', async () => {
    const window = await boot();
    setActiveSection(window, 'ctx-skills');
    await window.loadCtxList('skills');
    await flush(window);
    setActiveSection(window, 'ctx-commands');
    await window.loadCtxList('commands');
    await flush(window);
    setActiveSection(window, 'ctx-overview');
    await window.loadCtxOverview();
    await flush(window);

    // One bar, one switcher, one tier filter in the entire document.
    expect(window.document.querySelectorAll('.ctx-project-switcher').length).toBe(1);
    expect(window.document.querySelectorAll('.ctx-tier-filter').length).toBe(1);
    const bar = window.document.getElementById('ctx-control-bar');
    expect(bar.querySelector('.ctx-project-switcher').dataset.type).toBe('overview');
  });

  it('hides the bar on the Projects portal and restores it elsewhere', async () => {
    const window = await boot();
    // Prime the projects cache so the switcher (not just the tier filter)
    // renders, then exercise no-arg _ctxRenderControlBar's active-section
    // detection across switches.
    setActiveSection(window, 'ctx-skills');
    await window.loadCtxList('skills');
    await flush(window);
    const bar = window.document.getElementById('ctx-control-bar');
    expect(bar.hidden).toBe(false);
    expect(bar.querySelectorAll('.ctx-tier-filter').length).toBe(1);

    // Projects → bar hidden + emptied (portal owns its own roster).
    setActiveSection(window, 'ctx-projects');
    window._ctxRenderControlBar();
    expect(bar.hidden).toBe(true);
    expect(bar.innerHTML).toBe('');

    // Back to Commands → bar visible again, now driving the commands loader.
    setActiveSection(window, 'ctx-commands');
    window._ctxRenderControlBar();
    expect(bar.hidden).toBe(false);
    expect(bar.querySelector('.ctx-project-switcher').dataset.type).toBe('commands');
  });

  it('routes a project-switch on the shared bar to the active section loader', async () => {
    const window = await boot();
    setActiveSection(window, 'ctx-skills');
    await window.loadCtxList('skills');
    await flush(window);

    // Spy after the real render wired the controls; the wire handlers call the
    // global loaders by name, so a window reassignment intercepts them.
    const calls = { list: [], overview: 0, hooks: 0 };
    window.loadCtxList = (t) => { calls.list.push(t); };
    window.loadCtxOverview = () => { calls.overview += 1; };
    window.loadHooksSync = () => { calls.hooks += 1; };

    const select = window.document.querySelector('#ctx-control-bar .ctx-project-select');
    expect(select).not.toBeNull();
    select.value = 'p1';
    select.dispatchEvent(new window.Event('change'));

    expect(calls.list).toEqual(['skills']);
    expect(calls.overview).toBe(0);
    expect(calls.hooks).toBe(0);
  });

  it('routes a tier-filter change on the shared bar to the active section loader', async () => {
    const window = await boot();
    setActiveSection(window, 'ctx-agents');
    await window.loadCtxList('agents');
    await flush(window);

    const calls = { list: [], overview: 0, hooks: 0 };
    window.loadCtxList = (t) => { calls.list.push(t); };
    window.loadCtxOverview = () => { calls.overview += 1; };
    window.loadHooksSync = () => { calls.hooks += 1; };

    const userBtn = window.document.querySelector(
      '#ctx-control-bar .ctx-tier-filter button[data-scope="user"]',
    );
    expect(userBtn).not.toBeNull();
    userBtn.click();

    expect(calls.list).toEqual(['agents']);
    expect(calls.overview).toBe(0);
    expect(calls.hooks).toBe(0);
  });

  it('a stale overview render does NOT hijack the shared bar for the active section', async () => {
    // Regression for the review blocker: the bar is one shared, VISIBLE host, and
    // loaders are async. If a late loadCtxOverview (Sync All finally / Refresh /
    // slow fetch) resolves after the user navigated to Skills, it must repaint
    // the bar for Skills — NOT hijack it back to 'overview' and mis-route the
    // next tier/project change. ``_ctxRenderControlBar`` self-sources the type
    // from the active section, so this holds.
    const window = await boot();
    setActiveSection(window, 'ctx-skills');
    await window.loadCtxList('skills');
    await flush(window);
    const bar = window.document.getElementById('ctx-control-bar');
    expect(bar.querySelector('.ctx-tier-filter').dataset.type).toBe('skills');

    // A late overview render lands while Skills is still the active section.
    await window.loadCtxOverview();
    await flush(window);
    expect(bar.querySelector('.ctx-tier-filter').dataset.type).toBe('skills');
    expect(bar.querySelector('.ctx-project-switcher').dataset.type).toBe('skills');

    // And the next tier-filter change still routes to the Skills loader.
    const calls = { list: [], overview: 0, hooks: 0 };
    window.loadCtxList = (t) => { calls.list.push(t); };
    window.loadCtxOverview = () => { calls.overview += 1; };
    window.loadHooksSync = () => { calls.hooks += 1; };
    bar.querySelector('.ctx-tier-filter button[data-scope="user"]').click();
    expect(calls.list).toEqual(['skills']);
    expect(calls.overview).toBe(0);
  });

  it('re-renders the bar for hooks-sync on a locale flip (langchange)', async () => {
    // hooks-sync has no per-section re-issue branch in the langchange listener,
    // so the bar's inline-t() labels are re-translated only by the standalone
    // _ctxRenderControlBar() the listener fires when the Gateway tab is the
    // visible host. Pin that path: clear the bar, dispatch langchange, expect it
    // repainted for the active hooks-sync section.
    const window = await boot();
    // Prime the projects cache (any loader) so the switcher renders too.
    setActiveSection(window, 'ctx-skills');
    await window.loadCtxList('skills');
    await flush(window);
    // Now sit on hooks-sync with the Gateway tab visible (the langchange gate).
    window.document.getElementById('tab-context-gateway').classList.add('active');
    setActiveSection(window, 'hooks-sync');
    window._ctxRenderControlBar();
    const bar = window.document.getElementById('ctx-control-bar');
    expect(bar.querySelector('.ctx-tier-filter').dataset.type).toBe('hooks-sync');

    bar.innerHTML = '';
    window.dispatchEvent(new window.Event('langchange'));
    await flush(window);
    expect(bar.hidden).toBe(false);
    expect(bar.querySelector('.ctx-tier-filter')).not.toBeNull();
    expect(bar.querySelector('.ctx-tier-filter').dataset.type).toBe('hooks-sync');
    expect(bar.querySelector('.ctx-project-switcher')).not.toBeNull();
  });

  it('keeps the shared bar controls disabled across repaints during a Sync All run', async () => {
    // Codex review: the bar is one shared host that repaints on section switch /
    // loader / langchange. A Sync All lock must survive those repaints — else the
    // user flips project/tier mid-run, whose handler clears the run's status
    // summary. The lock lives in a module flag re-applied after every repaint.
    const window = await boot();
    setActiveSection(window, 'ctx-skills');
    await window.loadCtxList('skills');
    await flush(window);
    const bar = window.document.getElementById('ctx-control-bar');
    const allDisabled = () =>
      [...bar.querySelectorAll('.ctx-tier-filter button')].every((b) => b.disabled)
      && bar.querySelector('.ctx-project-select').disabled === true;
    const noneDisabled = () =>
      [...bar.querySelectorAll('.ctx-tier-filter button')].every((b) => !b.disabled)
      && bar.querySelector('.ctx-project-select').disabled === false;

    // Lock (Sync All start).
    window._ctxSetSyncControlsDisabled(true);
    expect(allDisabled()).toBe(true);

    // A repaint from navigating to another section must NOT re-enable them.
    setActiveSection(window, 'ctx-commands');
    await window.loadCtxList('commands');
    await flush(window);
    expect(allDisabled()).toBe(true);

    // Unlock (Sync All finally + its overview reload) restores them.
    window._ctxSetSyncControlsDisabled(false);
    setActiveSection(window, 'ctx-overview');
    await window.loadCtxOverview();
    await flush(window);
    expect(noneDisabled()).toBe(true);
  });
});

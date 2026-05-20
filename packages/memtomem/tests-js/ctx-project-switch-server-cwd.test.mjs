/* Regression guard for #1071 — return to Server CWD via project dropdown.
 *
 * Server CWD is intentionally represented by an empty ``scope_id`` (the
 * legacy route-default URL shape omits ``scope_id`` for the server-CWD
 * scope). ``_ctxWireProjectControls`` previously treated the empty string
 * as "nothing selected" and returned early, so once a user had switched
 * to an added project, picking Server CWD silently no-op'd. This guard
 * pins the symmetric pair per ``feedback_pin_invert_symmetric_assertion.md``:
 *
 *   positive — switching from a real scope_id to '' actually persists
 *              the new active scope and triggers the section reload.
 *   negative — re-selecting the *current* scope still short-circuits
 *              (no spurious refetch).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCOPES = [
  {
    scope_id: '',
    label: 'Server CWD',
    root: '/srv',
    tier: 'project',
    sources: ['server-cwd'],
    missing: false,
    experimental: false,
    counts: { skills: 0, commands: 0, agents: 0 },
  },
  {
    scope_id: 'proj-abc',
    label: 'proj-abc',
    root: '/work/proj-abc',
    tier: 'project',
    sources: ['memtomem-config'],
    missing: false,
    experimental: false,
    counts: { skills: 0, commands: 0, agents: 0 },
  },
];

function stubProjectsFetch(window) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: SCOPES }) };
    }
    return upstream(input);
  };
}

async function bootWithCache() {
  // ``_ctxActiveScopeId`` lives in the script's module scope, so we can't
  // poke it from outside — drive it through the same code path the user
  // would, i.e. ``_ctxFetchProjects`` populates ``_ctxProjectsCache`` and
  // a subsequent dispatched ``change`` event flips the active scope via
  // ``_ctxWireProjectControls``. The fetch stub returns both Server CWD
  // (scope_id='') and an added project so both branches are reachable.
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
  });
  stubProjectsFetch(dom.window);
  await dom.window._ctxFetchProjects();
  return dom;
}

function mountSelect(window, type = 'overview') {
  // Mirror the rendered shape from ``_ctxProjectControls`` closely enough
  // that ``_ctxWireProjectControls`` picks the select up and the closest
  // ``.ctx-project-switcher`` ancestor exposes ``data-type``.
  const wrap = window.document.createElement('label');
  wrap.className = 'ctx-project-switcher';
  wrap.dataset.type = type;
  const select = window.document.createElement('select');
  select.className = 'ctx-project-select';
  for (const scope of SCOPES) {
    const opt = window.document.createElement('option');
    opt.value = scope.scope_id;
    opt.textContent = scope.label;
    select.appendChild(opt);
  }
  wrap.appendChild(select);
  window.document.body.appendChild(wrap);
  window._ctxWireProjectControls();
  return select;
}

function dispatchChange(select, value) {
  select.value = value;
  select.dispatchEvent(new select.ownerDocument.defaultView.Event('change', { bubbles: true }));
}

describe('_ctxWireProjectControls — Server CWD round-trip (#1071)', () => {
  it('persists empty scope_id and refetches when switching back to Server CWD', async () => {
    const dom = await bootWithCache();
    const { window } = dom;
    const select = mountSelect(window, 'overview');

    // Seed the active scope to the added project via the same code path
    // the user would exercise; bypassing this would mean poking module-
    // scoped ``let _ctxActiveScopeId`` directly.
    dispatchChange(select, 'proj-abc');
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');

    let overviewCalls = 0;
    window.loadCtxOverview = async () => { overviewCalls += 1; };

    dispatchChange(select, '');

    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('');
    expect(overviewCalls).toBe(1);
  });

  it('short-circuits when the user re-picks the already-active scope', async () => {
    const dom = await bootWithCache();
    const { window } = dom;
    const select = mountSelect(window, 'overview');

    dispatchChange(select, 'proj-abc');

    let overviewCalls = 0;
    window.loadCtxOverview = async () => { overviewCalls += 1; };

    dispatchChange(select, 'proj-abc');

    expect(overviewCalls).toBe(0);
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');
  });
});

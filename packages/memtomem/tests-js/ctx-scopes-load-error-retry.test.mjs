/* Regression guard for #1287 — an empty projects roster used to render the
 * "No project scopes" empty state for a condition the code itself documents
 * as impossible (a healthy payload always contains the server-cwd scope).
 * Reaching it means the load failed shallowly, and an empty-state rendering
 * there reads as data loss.
 *
 * The fix renders a load-error (``settings.ctx.scopes_load_failed``) with a
 * Retry button that re-runs ``loadCtxList`` — recovery is pinned by flipping
 * the stub to a healthy payload and clicking Retry.
 *
 * Mutation that bites: restoring the old ``emptyState(...no_project_scopes)``
 * line fails the error-copy assertion AND the retry-button presence; wiring
 * Retry to anything but a reload fails the recovery assertion.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const CWD_SCOPE = {
  scope_id: '', project_scope_id: '', label: 'Server CWD', root: '/srv',
  tier: 'project', sources: ['server-cwd'],
  missing: false, stale: false, experimental: false,
  counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
};

function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body };
}

function installProjects(window) {
  const upstream = window.fetch;
  const state = { respond: () => jsonOk({ scopes: [], target_scope: 'project_shared' }) };
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/context/projects')) return state.respond();
    if (url.startsWith('/api/context/runtimes')) return jsonOk({ runtimes: [] });
    return upstream(input, init);
  };
  return state;
}

async function until(fn, timeout = 2000) {
  const start = Date.now();
  for (;;) {
    if (fn()) return;
    if (Date.now() - start > timeout) throw new Error('condition not met in time');
    await new Promise((r) => setTimeout(r, 10));
  }
}

describe('empty scopes roster renders load error with retry (#1287)', () => {
  it('shows error copy + Retry on an empty roster, recovers on click', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    await window.I18N.init();
    // jsdom (this harness version) lacks the global ``CSS`` — the recovery
    // render's group wiring calls ``CSS.escape`` and would false-fail into
    // the catch path without it. Real browsers always have it.
    if (!window.CSS) {
      window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
    }
    const state = installProjects(window);

    await window.loadCtxList('skills');

    const listEl = window.document.getElementById('ctx-skills-list');
    const text = listEl.textContent || '';
    expect(text).toContain("Couldn't load project scopes");
    // The impossible-inventory copy is gone (it implied the data was simply
    // absent rather than the load having failed).
    expect(text).not.toContain('No project scopes');
    const retry = listEl.querySelector('.ctx-scopes-retry');
    expect(retry).toBeTruthy();
    expect((retry.textContent || '').trim()).toBe('Retry');

    // Recovery: a healthy roster appears once the server answers properly.
    state.respond = () => jsonOk({ scopes: [CWD_SCOPE], target_scope: 'project_shared' });
    retry.click();
    await until(() => listEl.querySelector('details[data-scope-id]'));
    expect(listEl.querySelector('.ctx-scopes-retry')).toBeFalsy();
  });

  // Codex review of #1295: the Projects portal has the same defensive
  // empty-roster branch and used to reference the removed
  // ``no_project_scopes`` key — without this pin the portal would echo the
  // raw key. The portal shares ``_ctxScopesLoadError`` with its own reload
  // (``loadCtxProjects``) as the retry.
  it('Projects portal renders the same load error and recovers on Retry', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'],
    });
    const { window } = dom;
    await window.I18N.init();
    if (!window.CSS) {
      window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
    }
    const state = installProjects(window);

    await window.loadCtxProjects();

    const listEl = window.document.getElementById('ctx-projects-list');
    const text = listEl.textContent || '';
    expect(text).toContain("Couldn't load project scopes");
    // Neither the legacy copy nor a raw-key echo may surface.
    expect(text).not.toContain('No project scopes');
    expect(text).not.toContain('settings.ctx.no_project_scopes');
    const retry = listEl.querySelector('.ctx-scopes-retry');
    expect(retry).toBeTruthy();

    state.respond = () => jsonOk({ scopes: [CWD_SCOPE], target_scope: 'project_shared' });
    retry.click();
    await until(() => listEl.querySelector('.ctx-portal-row'));
    expect(listEl.querySelector('.ctx-scopes-retry')).toBeFalsy();
  });
});

/* Regression guard for #1194 — ``_ctxFetchProjects`` used to commit the shared
 * ``_ctxProjectsCache`` (and normalize+persist ``_ctxActiveScopeId``) INSIDE the
 * helper, before any caller's sequence guard ran. A superseded, still-in-flight
 * projects fetch that resolved AFTER a newer one therefore clobbered the shared
 * cache and the persisted active-scope selection with stale data, even though
 * each caller correctly skipped its own (stale) render.
 *
 * The fix splits the helper into a pure ``_ctxFetchProjectsData`` (fetch only,
 * no globals) and ``_ctxCommitProjects`` (the commit half), and has every caller
 * commit ONLY after re-checking its sequence/scope guard.
 *
 * These globals are module-scoped ``let`` bindings, not ``window`` properties,
 * so — per the #1102 suite's precedent — the cache/active-scope state is pinned
 * through PUBLIC behavior:
 *   - ``localStorage`` (``memtomem_ctx_active_scope_id``) for the active scope
 *   - ``_ctxProjectControls(...)`` rendered HTML (defaults to ``_ctxProjectsCache``)
 *     for the cache contents.
 *
 * Mutation that bites: reverting any guarded caller to the legacy
 * ``_ctxFetchProjects()`` (commit-before-guard) makes the stale late fetch win —
 * the race test's active-scope assertion flips from ``p-keep`` to ``''``.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const ACTIVE_KEY = 'memtomem_ctx_active_scope_id';

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
const P_KEEP = scope('p-keep', 'Keeper');
// Newer (authoritative) payload still has the user's selected project.
const SCOPES_NEW = [CWD, P_KEEP];
// Older (superseded) payload — lacks p-keep, so committing it would demote the
// active scope to Server-CWD ('').
const SCOPES_OLD = [CWD];

function projectsResponse(scopes) {
  return { ok: true, status: 200, json: async () => ({ scopes, target_scope: 'project_shared' }) };
}

function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body };
}

/* Routes ``/api/context/projects`` to a settable responder and everything else
 * (runtimes etc.) to immediate empties, so the portal's post-commit render can
 * complete. */
function installProjects(window) {
  const upstream = window.fetch;
  const state = { respond: () => projectsResponse(SCOPES_NEW) };
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/context/projects')) return state.respond();
    if (url.startsWith('/api/context/runtimes')) return jsonOk({ runtimes: [] });
    return upstream(input, init);
  };
  return state;
}

/* Routes ``/api/context/projects`` to deferred promises so the test controls
 * the RESOLUTION ORDER of two overlapping fetches; runtimes resolve immediately
 * so a committed load can finish rendering. Returns the array of pending
 * ``fetch`` resolvers (index 0 = first call, 1 = second call). */
function installDeferredProjects(window) {
  const upstream = window.fetch;
  const resolvers = [];
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/context/projects')) {
      return new Promise((resolve) => { resolvers.push(resolve); });
    }
    if (url.startsWith('/api/context/runtimes')) {
      return Promise.resolve(jsonOk({ runtimes: [] }));
    }
    return upstream(input, init);
  };
  return resolvers;
}

function mountSelect(window) {
  const wrap = window.document.createElement('label');
  wrap.className = 'ctx-project-switcher';
  wrap.dataset.type = 'overview';
  const select = window.document.createElement('select');
  select.className = 'ctx-project-select';
  for (const s of SCOPES_NEW) {
    const opt = window.document.createElement('option');
    opt.value = s.scope_id;
    opt.textContent = s.label;
    select.appendChild(opt);
  }
  wrap.appendChild(select);
  window.document.body.appendChild(wrap);
  window._ctxWireProjectControls();
  return select;
}

function selectActive(window, value) {
  const select = mountSelect(window);
  select.value = value;
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
}

describe('_ctxFetchProjectsData / _ctxCommitProjects split (#1194)', () => {
  it('_ctxFetchProjectsData fetches WITHOUT mutating the cache; _ctxCommitProjects commits', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    const state = installProjects(window);

    // Seed the shared cache with the two-scope list via the legacy wrapper.
    state.respond = () => projectsResponse(SCOPES_NEW);
    await window._ctxFetchProjects();
    expect(window._ctxProjectControls('overview')).toContain('p-keep');

    // Fetch a DIFFERENT (smaller) payload as pure data — the cache must not move
    // until we explicitly commit.
    state.respond = () => projectsResponse(SCOPES_OLD);
    const result = await window._ctxFetchProjectsData();
    expect(result.data.scopes).toHaveLength(1);
    expect(result.warn).toBeNull();
    // Purity: cache still reflects the seeded two-scope list.
    expect(window._ctxProjectControls('overview')).toContain('p-keep');

    // Commit applies it.
    window._ctxCommitProjects(result);
    expect(window._ctxProjectControls('overview')).not.toContain('p-keep');
  });

  it('a superseded late projects fetch does NOT clobber the shared cache / active scope', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
    const { window } = dom;

    // Seed: load the two-scope list, then select p-keep as the active project so
    // a stale commit (which lacks p-keep) would visibly demote it to ''.
    const state = installProjects(window);
    window.loadCtxOverview = async () => {}; // select-change triggers a reload; stub it
    await window._ctxFetchProjects();
    selectActive(window, 'p-keep');
    expect(window.localStorage.getItem(ACTIVE_KEY)).toBe('p-keep');

    // Race: two overlapping loadCtxProjects calls. The OLDER (seq 1) resolves
    // AFTER the newer (seq 2). With the bug, the older payload (SCOPES_OLD)
    // would land in the shared cache last and demote the active scope.
    const resolvers = installDeferredProjects(window);
    const older = window.loadCtxProjects(); // seq 1 → fetch parked at resolvers[0]
    const newer = window.loadCtxProjects(); // seq 2 → fetch parked at resolvers[1]
    expect(resolvers).toHaveLength(2);

    // Newer resolves first and commits the authoritative two-scope list.
    resolvers[1](projectsResponse(SCOPES_NEW));
    await newer;
    expect(window.localStorage.getItem(ACTIVE_KEY)).toBe('p-keep');

    // Older resolves later with the stale one-scope list. Its caller guard
    // (seq 1 !== 2) must prevent the commit entirely.
    resolvers[0](projectsResponse(SCOPES_OLD));
    await older;

    // Active scope preserved (would be '' if the stale fetch had clobbered it),
    // and the shared cache still carries p-keep.
    expect(window.localStorage.getItem(ACTIVE_KEY)).toBe('p-keep');
    expect(window._ctxProjectControls('overview')).toContain('p-keep');
  });
});

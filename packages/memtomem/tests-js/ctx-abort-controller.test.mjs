/* Regression guard for #1286 — per-surface ``AbortController``s on the context
 * gateway. The ``seq`` guards (covered by ctx-projects-stale-fetch-race +
 * ctx-project-fetch-failure) already stop a superseded response from PAINTING;
 * this suite pins the two NEW behaviors the AbortControllers add:
 *
 *   1. Benign abort classification (#1286 / #1247 id 20): an ``AbortError`` —
 *      whether the whole fetch rejects or the body read throws mid-stream — must
 *      land in the silent, non-authoritative class (no toast, no active-scope
 *      demotion). It must NOT be misclassified by the parse/shape branches as a
 *      loud failure. The symmetric cell (a real ``SyntaxError`` stays loud)
 *      proves the classifier keys on abort-ness, not "the body read threw"
 *      (feedback_pin_invert_symmetric_assertion).
 *
 *   2. One in-flight request per surface: a switch storm (rapid re-entry) must
 *      abort every superseded in-flight fetch, leaving exactly one live request
 *      per surface (overview / projects / list / detail). The independence cell
 *      proves the controllers are per-surface — a detail mount must not abort an
 *      in-flight list fetch (a regression to one shared controller flips it red).
 *
 * The jsdom fetch mock here is signal-aware (rejects with an ``AbortError`` when
 * its signal fires), unlike the seq-race suites whose signal-agnostic mocks let
 * the seq guard stay the sole gate. Both are valid: in production ``fetch``
 * rejects on abort; the seq guard is defense in depth.
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

function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
}

// Mirror a real browser fetch abort: a DOMException named 'AbortError'
// (``.code === 20``). Falls back to a tagged Error where DOMException is absent.
function abortError() {
  if (typeof DOMException === 'function') {
    return new DOMException('The operation was aborted.', 'AbortError');
  }
  return Object.assign(new Error('aborted'), { name: 'AbortError', code: 20 });
}

const flush = () => new Promise((resolve) => { setTimeout(resolve, 0); });

/* ----------------------------------------------------------------------------
 * 1. Benign abort classification
 * ------------------------------------------------------------------------- */

function installProjectsFetch(window, responder) {
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/context/projects')) return responder(init);
    return jsonOk({});
  };
}

function mountSelect(window) {
  const wrap = window.document.createElement('label');
  wrap.className = 'ctx-project-switcher';
  wrap.dataset.type = 'overview';
  const select = window.document.createElement('select');
  select.className = 'ctx-project-select';
  for (const s of [CWD, P_KEEP]) {
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

// Seed the shared cache with [CWD, P_KEEP] and select p-keep as the active
// project, so a stale/loud commit would visibly demote it to ''. Returns the
// toast spy array.
async function seedActiveKeep(window) {
  const toasts = [];
  window.showToast = (msg, level) => { toasts.push({ msg, level }); };
  window.loadCtxOverview = async () => {}; // select-change triggers a reload; stub it
  installProjectsFetch(window, () => jsonOk({ scopes: [CWD, P_KEEP] }));
  await window._ctxFetchProjects();
  const select = mountSelect(window);
  select.value = 'p-keep';
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
  expect(window.localStorage.getItem(ACTIVE_KEY)).toBe('p-keep');
  return toasts;
}

describe('_ctxFetchProjectsData — aborted fetch is benign (#1286 / #1247 id 20)', () => {
  it('an AbortError during the body read is silent + non-authoritative (no demotion)', async () => {
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const toasts = await seedActiveKeep(window);

    // 200 OK, but the body read aborts mid-stream (a supersede during
    // res.json()). Pre-#1286 the parse try/catch tagged this ``kind: 'parse'``
    // → loud toast, and ``authoritative`` stayed true (sawResponse) → the
    // synthetic Server-CWD list normalized over the persisted selection.
    installProjectsFetch(window, () => ({
      ok: true, status: 200, json: async () => { throw abortError(); }, text: async () => '',
    }));
    const result = await window._ctxFetchProjectsData();
    expect(result.warn).toBeNull();
    expect(result.authoritative).toBe(false);
    expect(result.aborted).toBe(true);

    // Committing it is a no-op: cache, active scope, and toast memo untouched.
    window._ctxCommitProjects(result);
    expect(window.localStorage.getItem(ACTIVE_KEY)).toBe('p-keep');
    expect(window._ctxProjectControls('overview')).toContain('p-keep');
    expect(toasts).toHaveLength(0);
  });

  it('an already-aborted signal yields the benign class (fetch rejects, body never read)', async () => {
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const toasts = await seedActiveKeep(window);

    installProjectsFetch(window, (init) => {
      if (init?.signal?.aborted) return Promise.reject(abortError());
      return Promise.resolve(jsonOk({ scopes: [CWD] })); // would demote if committed
    });
    const ac = new AbortController();
    ac.abort();
    const result = await window._ctxFetchProjectsData({ signal: ac.signal });
    expect(result.warn).toBeNull();
    expect(result.authoritative).toBe(false);
    expect(result.aborted).toBe(true);

    window._ctxCommitProjects(result);
    expect(window.localStorage.getItem(ACTIVE_KEY)).toBe('p-keep');
    expect(toasts).toHaveLength(0);
  });

  it('SYMMETRIC: a real SyntaxError during the body read stays LOUD (parse failure)', async () => {
    // The inverse of cell 1 — proves the classifier keys on the error being an
    // abort, not merely on res.json() throwing. Reverting _ctxIsAbortError to a
    // blanket "treat any body-read throw as benign" would flip this red.
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    await seedActiveKeep(window);
    installProjectsFetch(window, () => ({
      ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); }, text: async () => '<html>',
    }));
    const result = await window._ctxFetchProjectsData();
    expect(result.warn).not.toBeNull();
    expect(result.warn.kind).toBe('parse');
    expect(result.aborted).toBeFalsy();
  });

  it('post-fetch short-circuit: a 200 resolving while already aborted is benign WITHOUT a body read', async () => {
    // The window the catch cannot cover: the body buffered fully before the
    // abort landed, so res.json() would SUCCEED — only the post-fetch
    // signal.aborted check keeps the stale roster from committing as
    // authoritative (the #1247 id 20 demotion hazard on the unguarded
    // legacy/portal commit paths). Removing that check lets json() run and the
    // [CWD]-only roster normalize over p-keep.
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const toasts = await seedActiveKeep(window);
    let jsonCalls = 0;
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url.startsWith('/api/context/projects')) {
        // Resolves a 200 regardless of the (already-aborted) signal.
        return { ok: true, status: 200, json: async () => { jsonCalls += 1; return { scopes: [CWD] }; }, text: async () => '' };
      }
      return jsonOk({});
    };
    const ac = new AbortController();
    ac.abort();
    const result = await window._ctxFetchProjectsData({ signal: ac.signal });
    expect(result.aborted).toBe(true);
    expect(result.warn).toBeNull();
    expect(result.authoritative).toBe(false);
    expect(jsonCalls).toBe(0); // short-circuited before the body read

    window._ctxCommitProjects(result);
    expect(window.localStorage.getItem(ACTIVE_KEY)).toBe('p-keep');
    expect(toasts).toHaveLength(0);
  });
});

/* ----------------------------------------------------------------------------
 * 2. One in-flight request per surface (switch storm)
 * ------------------------------------------------------------------------- */

// Records every fetch whose URL matches ``park`` and returns a never-settling
// promise that rejects with an AbortError when its signal fires — so a storm of
// loader re-entries leaves exactly one un-aborted (live) request behind.
function installStormFetch(window, park) {
  const calls = [];
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const signal = init?.signal;
    if (park(url)) {
      calls.push({ url, signal });
      return new Promise((resolve, reject) => {
        if (signal) {
          if (signal.aborted) { reject(abortError()); return; }
          signal.addEventListener('abort', () => reject(abortError()), { once: true });
        }
        // else: never settles (stays "in flight")
      });
    }
    return Promise.resolve(jsonOk({})); // unrelated init paths resolve immediately
  };
  return calls;
}

function liveProfile(calls, prefix) {
  const surface = calls.filter(c => c.url.split('?')[0].startsWith(prefix));
  return {
    total: surface.length,
    live: surface.filter(c => c.signal && !c.signal.aborted).length,
    // The live one must be the LAST request issued (FIFO supersede), not an
    // arbitrary survivor — guards against a mutation that aborts the wrong
    // controller (e.g. the fresh one instead of ``prev``).
    lastIsLive: surface.length > 0 && !!surface[surface.length - 1].signal && !surface[surface.length - 1].signal.aborted,
  };
}

describe('context gateway — switch storm leaves one in-flight request per surface (#1286)', () => {
  it('overview: 4 rapid loads → 3 aborted, 1 live projects fetch', async () => {
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const calls = installStormFetch(window, (u) => u.startsWith('/api/context/projects'));
    for (let i = 0; i < 4; i++) window.loadCtxOverview();
    await flush();
    expect(liveProfile(calls, '/api/context/projects')).toEqual({ total: 4, live: 1, lastIsLive: true });
  });

  it('list: 4 rapid loads → 3 aborted, 1 live projects fetch', async () => {
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const calls = installStormFetch(window, (u) => u.startsWith('/api/context/projects'));
    for (let i = 0; i < 4; i++) window.loadCtxList('skills');
    await flush();
    expect(liveProfile(calls, '/api/context/projects')).toEqual({ total: 4, live: 1, lastIsLive: true });
  });

  it('detail: 4 rapid mounts → 3 aborted, 1 live detail fetch', async () => {
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const calls = installStormFetch(window, (u) => u.split('?')[0] === '/api/context/skills/foo');
    for (let i = 0; i < 4; i++) window.loadCtxDetail('skills', 'foo');
    await flush();
    expect(liveProfile(calls, '/api/context/skills/foo')).toEqual({ total: 4, live: 1, lastIsLive: true });
  });

  it('projects portal: 4 rapid loads → 3 aborted, 1 live projects fetch', async () => {
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
    const calls = installStormFetch(window, (u) => u.startsWith('/api/context/projects'));
    for (let i = 0; i < 4; i++) window.loadCtxProjects();
    await flush();
    expect(liveProfile(calls, '/api/context/projects')).toEqual({ total: 4, live: 1, lastIsLive: true });
  });

  it('INDEPENDENCE: a detail mount does NOT abort an in-flight list fetch (per-surface controllers)', async () => {
    // A regression to a single shared controller would make loadCtxDetail's swap
    // abort the list's still-in-flight projects fetch → listCall.aborted = true.
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const calls = installStormFetch(
      window,
      (u) => u.startsWith('/api/context/projects') || u.split('?')[0] === '/api/context/skills/foo',
    );
    window.loadCtxList('skills');        // parks a projects fetch under _ctxListAbort.skills
    window.loadCtxDetail('skills', 'foo'); // parks a detail fetch under _ctxDetailAbort.skills
    await flush();
    const listCall = calls.find(c => c.url.startsWith('/api/context/projects'));
    const detailCall = calls.find(c => c.url.split('?')[0] === '/api/context/skills/foo');
    expect(listCall).toBeTruthy();
    expect(detailCall).toBeTruthy();
    expect(listCall.signal.aborted).toBe(false);
    expect(detailCall.signal.aborted).toBe(false);
  });

  it('REFRESH GATING (overview): a superseded refresh does NOT toast "complete"; a clean one does', async () => {
    // Codex review: loadCtxOverview() returns early on abort/supersede, but the
    // Refresh handler used to toast unconditionally — a concurrent scope/tier
    // switch that aborts the in-flight refresh then lied "Refresh complete"
    // while the winning request was still running. The handler now gates the
    // toast on the loader's completion signal.

    // Clean refresh → toast fires (proves the gate isn't stuck-false).
    {
      const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
      const toasts = [];
      window.showToast = (msg) => { toasts.push(msg); };
      window.fetch = async (input) => {
        const url = typeof input === 'string' ? input : input?.url || '';
        if (url.startsWith('/api/context/projects')) return jsonOk({ scopes: [CWD] });
        if (url.startsWith('/api/context/overview')) return jsonOk({ total: 0, by_type: {} });
        return jsonOk({});
      };
      window.document.getElementById('ctx-refresh-btn').click();
      await flush();
      await flush();
      expect(toasts.length).toBe(1);
    }

    // Superseded refresh → no "complete" toast.
    {
      const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
      const toasts = [];
      window.showToast = (msg) => { toasts.push(msg); };
      installStormFetch(window, (u) => u.startsWith('/api/context/projects'));
      window.document.getElementById('ctx-refresh-btn').click(); // invocation A (parked projects fetch)
      window.loadCtxOverview();                                  // B aborts A
      await flush();
      expect(toasts).toHaveLength(0);
    }
  });

  it('REFRESH GATING (portal): a superseded refresh does NOT toast "complete"; a clean one does', async () => {
    // Clean refresh → toast fires.
    {
      const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
      const toasts = [];
      window.showToast = (msg) => { toasts.push(msg); };
      window.fetch = async (input) => {
        const url = typeof input === 'string' ? input : input?.url || '';
        if (url.startsWith('/api/context/projects')) return jsonOk({ scopes: [CWD, P_KEEP] });
        if (url.startsWith('/api/context/runtimes')) return jsonOk({ runtimes: [] });
        return jsonOk({});
      };
      window.document.getElementById('ctx-projects-refresh-btn').click();
      await flush();
      await flush();
      expect(toasts.length).toBe(1);
    }

    // Superseded refresh → no toast.
    {
      const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
      const toasts = [];
      window.showToast = (msg) => { toasts.push(msg); };
      installStormFetch(window, (u) => u.startsWith('/api/context/projects'));
      window.document.getElementById('ctx-projects-refresh-btn').click(); // A (parked)
      window.loadCtxProjects();                                           // B aborts A
      await flush();
      expect(toasts).toHaveLength(0);
    }
  });

  it('SCOPE SWITCH: changing the active scope aborts an in-flight detail fetch', async () => {
    // Distinct from supersede-by-fresh-mount: changing the active project bumps
    // every detail seq AND aborts every in-flight detail fetch (the pane's scope
    // just became invalid). _ctxBumpActiveScopeDetailSeq is the seam the
    // project-select handler and _ctxNormalizeActiveScope both route through.
    // Deleting the abort line there survives every other test, so pin it here.
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const calls = installStormFetch(window, (u) => u.split('?')[0] === '/api/context/skills/foo');
    window.loadCtxDetail('skills', 'foo'); // parks a detail fetch under _ctxDetailAbort.skills
    await flush();
    const detailCall = calls.find(c => c.url.split('?')[0] === '/api/context/skills/foo');
    expect(detailCall).toBeTruthy();
    expect(detailCall.signal.aborted).toBe(false); // live before the switch
    window._ctxBumpActiveScopeDetailSeq();          // an active-scope change fires this
    expect(detailCall.signal.aborted).toBe(true);   // aborted by the scope switch
  });
});

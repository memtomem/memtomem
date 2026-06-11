/* Regression guard for #1080 / #1100 — distinguish the failure shapes of
 * ``GET /api/context/projects`` instead of silently collapsing them all
 * onto the Server-CWD-only fallback.
 *
 *   - 404                     → silent fallback (legacy/older-deploy contract)
 *   - network throw           → silent fallback, but NOT authoritative — the
 *                               persisted active scope survives (#1247 id 20)
 *   - 5xx / non-404 4xx       → toast + fallback (endpoint failing)
 *   - 200 + malformed JSON    → toast + fallback (response unreadable)
 *   - 200 + unexpected shape  → toast + fallback (parses but not {scopes:[…]})
 *   - 200 + real {scopes:[…]} → no toast, real scopes rendered (baseline)
 *
 * The cells together pin the symmetric pair per
 * ``feedback_pin_invert_symmetric_assertion.md``: positive-only or
 * negative-only would false-pass since the bug is "several failing cases
 * collapsing to one silent no-projects state". The mutation that proves
 * these tests bite is restoring the original blanket ``catch (_err)
 * { fallback }`` — the 503 / parse cases then stop toasting; deleting the
 * shape branch (#1100) flips the null / {} cells specifically.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const REAL_SCOPES = [
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

function jsonRes(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function malformedJsonRes() {
  // The fix splits ``res.json()`` into its own try/catch — to exercise that
  // branch we need ``ok: true`` but a ``json()`` that throws like real
  // browser fetch would on invalid JSON. ``text()`` is included for parity
  // with the other stubs even though context-gateway never reads it.
  return {
    ok: true,
    status: 200,
    json: async () => { throw new SyntaxError('Unexpected token < in JSON at position 0'); },
    text: async () => '<html>oops</html>',
  };
}

function installProjectsFetch(window, responder) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/context/projects')) return responder();
    return upstream(input);
  };
}

async function bootWithToastSpy() {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
  });
  const { window } = dom;
  // ``showToast`` is a function declaration in app.js so it lives on the
  // window-global lookup chain; overwriting ``window.showToast`` after
  // load swaps every later free-reference call site in context-gateway.js
  // (verified against the existing pattern in
  // ``search-drag-zone-toast-chunks.test.mjs``).
  const toasts = [];
  window.showToast = (msg, level) => { toasts.push({ msg, level }); };
  return { dom, window, toasts };
}

describe('_ctxFetchProjects — failure-shape signal split (#1080)', () => {
  let window;
  let toasts;

  beforeEach(async () => {
    ({ window, toasts } = await bootWithToastSpy());
  });

  it('baseline 200 with real scopes — no toast, scopes rendered as-is', async () => {
    installProjectsFetch(window, () => jsonRes({ scopes: REAL_SCOPES }));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(2);
    expect(data.scopes[1].scope_id).toBe('proj-abc');
    expect(toasts).toHaveLength(0);
  });

  it('404 — silent fallback to Server CWD (older-deploy contract)', async () => {
    installProjectsFetch(window, () => jsonRes({ detail: 'not found' }, 404));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    // Pin the silent half: 404 must NOT toast, or older deployments / test
    // stubs that legitimately omit the endpoint would spam users.
    expect(toasts).toHaveLength(0);
  });

  it('503 — toast + fallback (endpoint exists but failing)', async () => {
    installProjectsFetch(window, () => jsonRes({ detail: 'store unavailable' }, 503));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].level).toBe('error');
    expect(toasts[0].msg).toContain('store unavailable');
  });

  it('200 with malformed JSON — toast + fallback (response unreadable)', async () => {
    installProjectsFetch(window, () => malformedJsonRes());
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].level).toBe('error');
    // Parse-error detail string varies by engine; just pin that *something*
    // about the failure surfaced (not the empty/raw key fallback).
    expect(toasts[0].msg).not.toBe('settings.ctx.projects_fetch_failed');
    expect(toasts[0].msg.length).toBeGreaterThan(0);
  });

  it('200 with null body — toast + fallback (shape failure, #1100)', async () => {
    // Server returns the JSON literal ``null``. Pre-#1100 this slipped past
    // both the !res.ok and parse branches, then ``data.scopes`` TypeErrored —
    // the caller surfaced a generic "Failed to load overview" and the toast
    // line was never reached. Now it routes through the shape branch.
    installProjectsFetch(window, () => jsonRes(null));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].level).toBe('error');
    expect(toasts[0].msg).not.toBe('settings.ctx.projects_fetch_failed');
    expect(toasts[0].msg.length).toBeGreaterThan(0);
  });

  it('200 with {} body — toast + fallback (shape failure, #1100)', async () => {
    // Parses fine but has no ``scopes`` array. Pre-#1100 this silently set
    // ``_ctxProjectsCache = []`` with no toast — indistinguishable from "no
    // registered projects", the exact #1080 symptom via a different path.
    installProjectsFetch(window, () => jsonRes({}));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].level).toBe('error');
    expect(toasts[0].msg).not.toBe('settings.ctx.projects_fetch_failed');
    expect(toasts[0].msg.length).toBeGreaterThan(0);
  });
});

/* Regression guard for #1101 — ``_ctxFetchProjects`` runs from three
 * independent panel-load paths (overview, settings projects, hooks sync), so
 * a single persistent outage must not stack one toast per call. Symmetric
 * pin: one cell proves identical failures collapse to a single toast, the
 * other proves the memo clears on recovery so a *later* outage still notifies
 * (a dedup with no clear would over-suppress). Mutation that bites: dropping
 * the ``_ctxProjectsFetchWarnKey`` memo → the first cell sees 2 toasts.
 */
describe('_ctxFetchProjects — failure-toast de-dup across panel loads (#1101)', () => {
  it('two consecutive identical 503s surface a single toast', async () => {
    const { window, toasts } = await bootWithToastSpy();
    installProjectsFetch(window, () => jsonRes({ detail: 'store unavailable' }, 503));
    await window._ctxFetchProjects(); // e.g. overview panel
    await window._ctxFetchProjects(); // e.g. settings / hooks-sync panel
    expect(toasts).toHaveLength(1);
    expect(toasts[0].msg).toContain('store unavailable');
  });

  it('re-notifies after the outage clears and a distinct failure returns', async () => {
    const { window, toasts } = await bootWithToastSpy();
    let respond = () => jsonRes({ detail: 'store unavailable' }, 503);
    installProjectsFetch(window, () => respond());
    await window._ctxFetchProjects();          // toast #1 (503)
    respond = () => jsonRes({ scopes: REAL_SCOPES });
    await window._ctxFetchProjects();          // recovery — clears the memo
    respond = () => jsonRes({ detail: 'store unavailable' }, 503);
    await window._ctxFetchProjects();          // toast #2 (fresh outage)
    expect(toasts).toHaveLength(2);
  });
});

/* Regression guard for #1102 — normalization runs only on an *authoritative*
 * outcome, gated on the absence of a "loud" failure toast (``warn``):
 *
 *   - 5xx / parse (toast)  → NOT authoritative; skip normalize so a transient
 *     failure can't persist a Server-CWD demotion. localStorage + the in-memory
 *     active id survive and recovery restores the selection.
 *   - 404 (silent)         → endpoint absent / older-deploy; normalize as
 *     before so a now-stale ``proj-*`` id is cleared (other consumers such as
 *     ``_ctxRestoreDraft`` key off ``_ctxActiveScopeId``).
 *
 * Symmetric pin per ``feedback_pin_invert_symmetric_assertion.md`` — the two
 * cells pull in opposite directions, so gating on the wrong signal (skip-all
 * or normalize-all) flips exactly one of them red. Mutation that bites the
 * 503 cell: normalizing against the synthetic scope rewrites the persisted id
 * to '' (post-503 assertion red).
 */
describe('_ctxFetchProjects — active scope normalize gated on failure shape (#1102)', () => {
  function mountSelect(window) {
    const wrap = window.document.createElement('label');
    wrap.className = 'ctx-project-switcher';
    wrap.dataset.type = 'overview';
    const select = window.document.createElement('select');
    select.className = 'ctx-project-select';
    for (const scope of REAL_SCOPES) {
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

  it('keeps the persisted active scope across a 503 and restores it on recovery', async () => {
    const { window } = await bootWithToastSpy();
    let respond = () => jsonRes({ scopes: REAL_SCOPES });
    installProjectsFetch(window, () => respond());
    // Selecting a scope triggers a section reload; stub it out.
    window.loadCtxOverview = async () => {};

    // Seed the active scope to a real added project via the same code path a
    // user exercises — module-scoped ``_ctxActiveScopeId`` is not pokable from
    // outside (mirrors ctx-project-switch-server-cwd.test.mjs).
    await window._ctxFetchProjects();
    const select = mountSelect(window);
    dispatchChange(select, 'proj-abc');
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');

    // Transient 5xx — the synthetic fallback must NOT overwrite the selection.
    respond = () => jsonRes({ detail: 'store unavailable' }, 503);
    await window._ctxFetchProjects();
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');

    // Endpoint recovers — proj-abc is authoritative again and stays selected.
    respond = () => jsonRes({ scopes: REAL_SCOPES });
    const recovered = await window._ctxFetchProjects();
    expect(recovered.scopes).toHaveLength(2);
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');
  });

  it('keeps the persisted active scope across a network throw and restores it on recovery', async () => {
    // #1247 id 20: a fetch() REJECTION (server restart / sleep-wake / offline)
    // was bucketed with the silent 404 — warn stays null, so normalization ran
    // against the synthetic Server-CWD-only list and persisted '' over the
    // user's selection, with no toast and no recovery. An absent endpoint on
    // the same origin resolves as a 404, so a rejection is the transient
    // class of the 503 cell above — same expectations, one layer lower.
    const { window, toasts } = await bootWithToastSpy();
    let respond = () => jsonRes({ scopes: REAL_SCOPES });
    installProjectsFetch(window, () => respond());
    window.loadCtxOverview = async () => {};

    await window._ctxFetchProjects();
    const select = mountSelect(window);
    dispatchChange(select, 'proj-abc');
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');

    // Outage: the responder throws like real fetch rejects. Selection must
    // survive in localStorage; the documented contract keeps this cell
    // toast-silent (unlike the 503 cell).
    respond = () => { throw new TypeError('Failed to fetch'); };
    const fallback = await window._ctxFetchProjects();
    expect(fallback.scopes).toHaveLength(1);
    expect(fallback.scopes[0].sources).toEqual(['server-cwd']);
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');
    expect(toasts).toHaveLength(0);

    // Server reachable again — the preserved selection is live immediately.
    respond = () => jsonRes({ scopes: REAL_SCOPES });
    await window._ctxFetchProjects();
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');
  });

  it('clears a now-stale active scope on a silent 404 fallback (older-deploy contract)', async () => {
    const { window } = await bootWithToastSpy();
    let respond = () => jsonRes({ scopes: REAL_SCOPES });
    installProjectsFetch(window, () => respond());
    window.loadCtxOverview = async () => {};

    await window._ctxFetchProjects();
    const select = mountSelect(window);
    dispatchChange(select, 'proj-abc');
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('proj-abc');

    // 404 is the silent older-deploy bucket — the endpoint is genuinely absent,
    // not transiently failing, so the stale proj-abc selection must be cleared
    // to Server-CWD (matches pre-#1099 behavior; keeps _ctxRestoreDraft & other
    // active-id consumers from leaking a dangling scope). Opposite of the 503
    // cell above: gating normalization on the wrong signal flips one of the two.
    respond = () => jsonRes({ detail: 'not found' }, 404);
    await window._ctxFetchProjects();
    expect(window.localStorage.getItem('memtomem_ctx_active_scope_id')).toBe('');
  });

  it('keys conflict drafts under the effective Server-CWD scope while the preserved id is uncached', async () => {
    // Consequence of #1102 preserving the active id across a 503: requests
    // collapse to Server-CWD (_ctxScopeParam omits scope_id for an uncached id)
    // but the draft key must collapse too, or an outage draft cross-contaminates
    // the real project after recovery. Both now route through _ctxEffectiveScopeId.
    const { window } = await bootWithToastSpy();
    let respond = () => jsonRes({ scopes: REAL_SCOPES });
    installProjectsFetch(window, () => respond());
    window.loadCtxOverview = async () => {};

    await window._ctxFetchProjects();
    const select = mountSelect(window);
    dispatchChange(select, 'proj-abc');
    // proj-abc is authoritative → draft keyed under it, and the request sends it.
    expect(window._ctxStashKey('skills', 'foo')).toContain('proj-abc');

    // Transient 503 — selection preserved in memory, cache is synthetic. The
    // request silently falls back to Server-CWD, so the draft key must too.
    // Mutation that bites: keying _ctxStashKey off the raw _ctxActiveScopeId
    // leaves 'proj-abc' in the outage key and this flips red.
    respond = () => jsonRes({ detail: 'store unavailable' }, 503);
    await window._ctxFetchProjects();
    expect(window._ctxScopeParam()).toBe('');                       // request → Server-CWD
    expect(window._ctxStashKey('skills', 'foo')).not.toContain('proj-abc');
    expect(window._ctxStashKey('skills', 'foo')).toContain('__default__'); // draft → Server-CWD too
  });
});

/* Regression guard for the conflict-draft lifecycle consequence of #1102
 * (Codex round-3 Major). Because the active id is preserved across a transient
 * outage, the *effective* scope can flip back mid-editor-session when the
 * projects endpoint recovers. Draft stash/restore/clear must therefore key off
 * a value pinned once at editor mount (``detailEl.dataset.draftKey``), not a
 * live recomputation — otherwise a draft stashed under Server-CWD during the
 * outage is cleared under the recovered project's key and orphaned, only to
 * resurrect after the user already discarded/saved. Mutation that bites:
 * reverting any clear call site to recompute via the live scope flips the
 * final assertion red (the __default__ draft survives the Cancel).
 */
describe('conflict-draft key pinned at editor mount (#1102 lifecycle)', () => {
  function installCombinedFetch(window, getProjects) {
    const upstream = window.fetch;
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url.startsWith('/api/context/projects')) return getProjects();
      if (url.endsWith('/diff')) return jsonRes({ runtimes: [], canonical_content: '# demo\n' });
      if (url.match(/\/api\/context\/[^/]+\/[^/]+$/)) {
        return jsonRes({ name: 'demo', content: '# demo\n', mtime_ns: '1', files: [], fields: {} });
      }
      return upstream(input);
    };
  }

  function mountSelect(window) {
    const wrap = window.document.createElement('label');
    wrap.className = 'ctx-project-switcher';
    wrap.dataset.type = 'overview';
    const select = window.document.createElement('select');
    select.className = 'ctx-project-select';
    for (const scope of REAL_SCOPES) {
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

  it('clears the draft under the mount-pinned key even after the effective scope recovers', async () => {
    const { window } = await bootWithToastSpy();
    let projectsResp = () => jsonRes({ scopes: REAL_SCOPES });
    installCombinedFetch(window, () => projectsResp());
    window.loadCtxOverview = async () => {};

    // Enter the #1102 state: proj-abc selected, then a 503 collapses the
    // effective scope to Server-CWD while the selection is preserved.
    await window._ctxFetchProjects();
    dispatchChange(mountSelect(window), 'proj-abc');
    projectsResp = () => jsonRes({ detail: 'store unavailable' }, 503);
    await window._ctxFetchProjects();
    expect(window._ctxStashKey('skills', 'demo')).toContain('__default__');

    // Mount the editor during the outage — the draft key is pinned now.
    await window.loadCtxDetail('skills', 'demo');
    const detailEl = window.document.getElementById('ctx-skills-detail');
    const pinned = detailEl.dataset.draftKey;
    expect(pinned).toContain('__default__');
    window._ctxStashDraft(pinned, 'unsaved edits'); // simulate a 409 stash
    expect(window.sessionStorage.getItem(pinned)).toBe('unsaved edits');

    // Projects recovers mid-session — the *live* effective key is proj-abc now,
    // but the editor's pinned key must not move.
    projectsResp = () => jsonRes({ scopes: REAL_SCOPES });
    await window._ctxFetchProjects();
    expect(window._ctxStashKey('skills', 'demo')).toContain('proj-abc');
    expect(detailEl.dataset.draftKey).toBe(pinned);

    // Cancel cleanup must remove the pinned (__default__) draft, not the live
    // proj-abc key — otherwise the outage draft is orphaned and resurrects.
    detailEl.querySelector('.ctx-edit-cancel').click();
    expect(window.sessionStorage.getItem(pinned)).toBeNull();
  });
});

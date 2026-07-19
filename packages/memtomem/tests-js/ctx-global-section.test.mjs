/* ADR-0030 PR-F2 — user-tier "global library" section (context-gateway-global.js).
 *
 * Drives the real module in the jsdom harness against a stubbed
 * GET /api/context/status-global (PR-F1). Pins: the inventory counts, the
 * pull-direction drift rows + badge, the sidebar glance-dot on has_pull_drift,
 * the leaf Pull button opening the SHARED pull modal at the USER tier, the
 * supersession seq guard, and the load-failure fallback.
 *
 * Mutation that bites: dropping the seq guard makes the race test paint a stale
 * payload; defaulting the modal to project_shared makes the leaf-Pull test fail
 * (a user-tier drift row must open user-tier Pull).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body };
}

function driftPayload() {
  return {
    scope: 'user',
    store: { skills: 2, agents: 1, commands: 0 },
    runtime_coverage: [
      { name: 'claude', available: true, installed: true, memtomem_registered: true },
      { name: 'codex', available: false, installed: null, memtomem_registered: null },
    ],
    pull_drift: {
      has_pull_drift: true,
      total: 2,
      differs: 1,
      errors: 0,
      identical: 1,
      rows: [
        { kind: 'skills', name: 'reviewer', verdict: 'differs', runtimes: ['claude'], reason: null },
        { kind: 'agents', name: 'planner', verdict: 'identical', runtimes: [], reason: null },
      ],
    },
  };
}

function cleanPayload() {
  return {
    scope: 'user',
    store: { skills: 1, agents: 0, commands: 0 },
    runtime_coverage: [],
    pull_drift: {
      has_pull_drift: false, total: 1, differs: 0, errors: 0, identical: 1,
      rows: [{ kind: 'skills', name: 'reviewer', verdict: 'identical', runtimes: [], reason: null }],
    },
  };
}

function installFetch(window, opts = {}) {
  const upstream = window.fetch;
  const urls = [];
  const state = { statusGlobal: opts.statusGlobal || (async () => jsonOk(driftPayload())) };
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    urls.push(url);
    if (url.startsWith('/api/context/status-global')) return state.statusGlobal(url, init);
    // The pull modal (opened by the leaf button) previews via this route.
    if (url.includes('/pull-preview')) {
      return Promise.resolve(jsonOk({
        kind: 'skills', name: 'reviewer', target_scope: 'user', store_present: true,
        candidates: [], distinct_landing_count: 0, ambiguous: false, auto_source: null,
      }));
    }
    return upstream(input, init);
  };
  return { urls, state };
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function boot() {
  return bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
}

function navDot(window) {
  return window.document.querySelector(
    '.settings-nav-btn[data-section="ctx-global"] .ctx-global-nav-dot',
  );
}

function driftBadge(window, verdict) {
  return window.document.querySelector(`.ctx-global-badge--${verdict}`);
}

describe('Global library section (ADR-0030 PR-F2)', () => {
  it('renders inventory counts, a differs row + badge, and lights the nav dot', async () => {
    const { window } = await boot();
    installFetch(window);
    await window.loadCtxGlobal();
    await flush();

    // Inventory counts.
    const inv = window.document.querySelector('.ctx-global-inventory');
    expect(inv).not.toBeNull();
    expect(inv.textContent).toContain('2'); // skills count

    // The differs row carries a badge + a Pull button; the identical row does not.
    const rows = window.document.querySelectorAll('.ctx-global-row');
    expect(rows.length).toBe(2);
    expect(driftBadge(window, 'differs')).not.toBeNull();
    expect(driftBadge(window, 'identical')).not.toBeNull();
    const pullBtns = window.document.querySelectorAll('.ctx-global-pull-btn');
    expect(pullBtns.length).toBe(1);
    expect(pullBtns[0].dataset.name).toBe('reviewer');

    // Sidebar glance-dot on.
    expect(navDot(window).hidden).toBe(false);
  });

  it('shows the in-sync summary, no Pull button, and no nav dot for a clean Store', async () => {
    const { window } = await boot();
    installFetch(window, { statusGlobal: async () => jsonOk(cleanPayload()) });
    await window.loadCtxGlobal();
    await flush();

    expect(window.document.querySelectorAll('.ctx-global-pull-btn').length).toBe(0);
    expect(driftBadge(window, 'differs')).toBeNull();
    expect(window.document.querySelector('.ctx-global-drift-summary--drift')).toBeNull();
    expect(navDot(window).hidden).toBe(true);
  });

  it('leaf Pull button opens the shared pull modal defaulted to the USER tier', async () => {
    const { window } = await boot();
    installFetch(window);
    await window.loadCtxGlobal();
    await flush();

    // Record the modal-open contract (the module reads window.ctxOpenPullModal
    // dynamically, so replacing it after boot is honored).
    const calls = [];
    window.ctxOpenPullModal = (kind, name, tier) => calls.push([kind, name, tier]);

    window.document.querySelector('.ctx-global-pull-btn')
      .dispatchEvent(new window.Event('click', { bubbles: true }));

    expect(calls).toEqual([['skills', 'reviewer', 'user']]);
  });

  it('discards a superseded status-global payload (seq guard)', async () => {
    const { window } = await boot();
    let releaseOld;
    const parked = new Promise((resolve) => { releaseOld = resolve; });
    const { state } = installFetch(window, { statusGlobal: () => parked });

    const older = window.loadCtxGlobal(); // seq 1 — parked
    state.statusGlobal = async () => jsonOk(driftPayload());
    await window.loadCtxGlobal(); // seq 2 — resolves with drift
    await flush();
    expect(driftBadge(window, 'differs')).not.toBeNull();

    // Release the stale seq-1 fetch with an all-clean payload: the guard must
    // drop it, so the drift badge + nav dot the newer load set survive.
    releaseOld(jsonOk(cleanPayload()));
    await flush();
    expect(driftBadge(window, 'differs')).not.toBeNull();
    expect(navDot(window).hidden).toBe(false);
  });

  it('survives a status-global failure with an error message and no nav dot', async () => {
    const { window } = await boot();
    installFetch(window, { statusGlobal: async () => ({ ok: false, status: 500, json: async () => ({}) }) });
    await window.loadCtxGlobal();
    await flush();

    expect(window.document.querySelector('.ctx-global-error')).not.toBeNull();
    expect(window.document.querySelectorAll('.ctx-global-row').length).toBe(0);
    expect(navDot(window).hidden).toBe(true);
  });

  it('re-renders rows from cached data on langchange WITHOUT re-fetching', async () => {
    const { window } = await boot();
    const { urls } = installFetch(window);
    await window.loadCtxGlobal();
    await flush();
    const fetchesBefore = urls.filter(u => u.startsWith('/api/context/status-global')).length;

    window.dispatchEvent(new window.Event('langchange'));
    await flush();

    // Rows persist, repainted from cache — no second probe.
    expect(window.document.querySelectorAll('.ctx-global-row').length).toBe(2);
    const fetchesAfter = urls.filter(u => u.startsWith('/api/context/status-global')).length;
    expect(fetchesAfter).toBe(fetchesBefore);
  });

  it('eager nav probe lights the glance-dot WITHOUT rendering the section body', async () => {
    const { window } = await boot();
    installFetch(window);
    // The gateway-open probe (app.js) sets only the dot; the section body is
    // rendered lazily when the user opens Global.
    await window._probeGlobalNavStatus();
    await flush();
    expect(navDot(window).hidden).toBe(false);
    expect(window.document.querySelectorAll('.ctx-global-row').length).toBe(0);
  });

  it('a stale eager probe cannot re-light the dot after a newer clean load (nav seq guard)', async () => {
    const { window } = await boot();
    let releaseProbe;
    const parked = new Promise((r) => { releaseProbe = r; });
    let call = 0;
    installFetch(window, {
      statusGlobal: () => {
        call += 1;
        return call === 1 ? parked : jsonOk(cleanPayload());
      },
    });

    const probe = window._probeGlobalNavStatus(); // seq 1 — parked, would report drift
    await window.loadCtxGlobal();                  // seq 2 — resolves clean → dot hidden
    await flush();
    expect(navDot(window).hidden).toBe(true);

    // Release the stale probe with a DRIFT payload: the shared nav-seq guard must
    // drop its write, so the dot stays hidden (no stale re-light).
    releaseProbe(jsonOk(driftPayload()));
    await probe;
    await flush();
    expect(navDot(window).hidden).toBe(true);
  });

  it('eager nav probe leaves the dot hidden on a failure', async () => {
    const { window } = await boot();
    installFetch(window, { statusGlobal: async () => ({ ok: false, status: 500, json: async () => ({}) }) });
    await window._probeGlobalNavStatus();
    await flush();
    expect(navDot(window).hidden).toBe(true);
  });

  it('clears the nav dot when a fresh load reports the Store clean', async () => {
    const { window } = await boot();
    const { state } = installFetch(window);
    await window.loadCtxGlobal();
    await flush();
    expect(navDot(window).hidden).toBe(false);

    state.statusGlobal = async () => jsonOk(cleanPayload());
    await window.loadCtxGlobal();
    await flush();
    expect(navDot(window).hidden).toBe(true);
  });
});

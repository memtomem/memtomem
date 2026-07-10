/* Overview detected-runtimes unavailable badge (#1692 PR 6).
 *
 * `/api/context/overview` now carries `detected_runtimes_unavailable` — true
 * when the detection probe raised (in which case `detected_runtimes` is []).
 * An empty chip row would read as "no runtimes", false-healthy, so the header
 * renders an explicit danger badge instead. Absent (older cached payloads
 * replaying through langchange, or an old server) and `false` must keep the
 * legacy chip render — the strict `=== true` check is the compat boundary.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function stubOverviewFetch(window, { runtimes, unavailable } = {}) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/api/context/overview')) {
      const body = {
        target_scope: 'project_shared',
        project_root: '/srv',
        detected_runtimes: runtimes,
        skills:   { total: 1, in_sync: 1 },
        commands: { total: 0 },
        agents:   { total: 0 },
      };
      if (unavailable !== undefined) body.detected_runtimes_unavailable = unavailable;
      return { ok: true, status: 200, json: async () => body };
    }
    // Valid empty projects payload so no error toast races the render.
    if (url.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: [] }) };
    }
    return upstream(input);
  };
}

async function renderOverview(opts) {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
  });
  const { window } = dom;
  stubOverviewFetch(window, opts);
  await window.I18N.init();
  await window.loadCtxOverview();
  return window;
}

const HEALTHY = [
  { name: 'claude', available: true, installed: true, memtomem_registered: true },
  { name: 'gemini', available: false, installed: false, memtomem_registered: false },
];

describe('overview detected-runtimes unavailable badge', () => {
  it('renders an accessible danger badge — and no runtime chips — when the flag is true', async () => {
    const window = await renderOverview({ runtimes: [], unavailable: true });
    const badge = window.document.querySelector('.ctx-overview-runtimes-unavailable');
    expect(badge).toBeTruthy();
    expect(badge.classList.contains('badge-danger')).toBe(true);
    expect(badge.textContent.trim().length).toBeGreaterThan(0);
    // Not color-only: the badge names its state for screen readers.
    expect(badge.getAttribute('role')).toBe('img');
    expect((badge.getAttribute('aria-label') || '').length).toBeGreaterThan(0);
    // No healthy/grey runtime chips alongside the failure badge.
    const strip = window.document.querySelector('.ctx-overview-runtimes');
    expect(strip.querySelectorAll('[data-runtime]').length).toBe(0);
  });

  it('renders the normal chips when the flag is false', async () => {
    const window = await renderOverview({ runtimes: HEALTHY, unavailable: false });
    expect(window.document.querySelector('.ctx-overview-runtimes-unavailable')).toBeNull();
    const strip = window.document.querySelector('.ctx-overview-runtimes');
    expect(strip.querySelectorAll('[data-runtime]').length).toBe(2);
  });

  it('renders the normal chips when the key is absent (older cached payload / old server)', async () => {
    const window = await renderOverview({ runtimes: HEALTHY, unavailable: undefined });
    expect(window.document.querySelector('.ctx-overview-runtimes-unavailable')).toBeNull();
    const strip = window.document.querySelector('.ctx-overview-runtimes');
    expect(strip.querySelectorAll('[data-runtime]').length).toBe(2);
  });
});

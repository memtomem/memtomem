/* Overview wiki update-available badge (0629 backlog c/d — the web half).
 *
 * `/api/context/overview` carries a `wiki_installs: {total, behind}` block —
 * the lockfile↔wiki staleness axis none of the canonical→runtime tiles
 * cover. The header renders a warning badge ONLY when actionable
 * (behind > 0); zero, a null block (degraded backend classifier), or an
 * absent key (older cached payloads replaying through langchange) must all
 * render nothing — a permanent "0 updates" chip would train users to stop
 * reading the header.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function stubOverviewFetch(window, wikiInstalls) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/api/context/overview')) {
      const body = {
        skills:   { total: 1, in_sync: 1 },
        commands: { total: 0 },
        agents:   { total: 0 },
      };
      if (wikiInstalls !== undefined) body.wiki_installs = wikiInstalls;
      return { ok: true, status: 200, json: async () => body };
    }
    // Valid empty projects payload so no error toast races the render.
    if (url.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: [] }) };
    }
    return upstream(input);
  };
}

async function renderOverview(wikiInstalls) {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
  });
  const { window } = dom;
  stubOverviewFetch(window, wikiInstalls);
  await window.I18N.init();
  await window.loadCtxOverview();
  return window;
}

describe('overview wiki behind badge', () => {
  it('renders a warning badge with the behind count when updates exist', async () => {
    const window = await renderOverview({ total: 3, behind: 2 });
    const badge = window.document.querySelector('.ctx-overview-wiki-behind');
    expect(badge).toBeTruthy();
    expect(badge.classList.contains('badge-warning')).toBe(true);
    expect(badge.textContent).toContain('2');
    // The hover tip explains the remediation path (Wiki section / CLI verb).
    expect(badge.getAttribute('title')).toContain('mm context update');
  });

  it('renders nothing when no install is behind', async () => {
    const window = await renderOverview({ total: 3, behind: 0 });
    expect(window.document.querySelector('.ctx-overview-wiki-behind')).toBeNull();
  });

  it('renders nothing when the block is null (degraded classifier)', async () => {
    const window = await renderOverview(null);
    expect(window.document.querySelector('.ctx-overview-wiki-behind')).toBeNull();
  });

  it('renders nothing when the key is absent (older cached payload)', async () => {
    const window = await renderOverview(undefined);
    expect(window.document.querySelector('.ctx-overview-wiki-behind')).toBeNull();
  });
});

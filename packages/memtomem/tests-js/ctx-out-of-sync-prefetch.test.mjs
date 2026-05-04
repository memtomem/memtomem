/* Regression guard for the out-of-sync auto-open Diff behaviour.
 *
 * When the list-click handler sees a card with ``data-out-of-sync="true"``
 * it calls ``loadCtxDetail(type, name, { autoOpenDiff: true })`` so the
 * detail view lands on the Diff tab pre-fetched. The default path
 * (no opts / autoOpenDiff false) must keep the Canonical tab active
 * — a positive + negative pair per ``feedback_pin_invert_symmetric_assertion.md``.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function stubCtxDetailFetch(window) {
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    // Canonical detail (``GET /api/context/{type}/{name}``)
    if (url.match(/\/api\/context\/[^/]+\/[^/]+$/)) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          name: 'demo',
          content: '# demo skill\n',
          mtime_ns: '1',
          files: [],
          fields: {},
        }),
      };
    }
    // Diff endpoint — minimal payload so ``_ctxLoadDiff`` doesn't throw
    if (url.endsWith('/diff')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          runtimes: [],
          canonical_content: '# demo skill\n',
        }),
      };
    }
    // Locale or anything else — empty 200
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
    };
  };
}

describe('loadCtxDetail — autoOpenDiff', () => {
  it('activates the Diff tab when autoOpenDiff: true', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubCtxDetailFetch(window);

    await window.loadCtxDetail('skills', 'demo', { autoOpenDiff: true });

    const detailEl = window.document.getElementById('ctx-skills-detail');
    const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
    const canonTab = detailEl.querySelector('.ctx-detail-tab[data-pane="canonical"]');
    expect(diffTab).not.toBeNull();
    expect(canonTab).not.toBeNull();
    expect(diffTab.classList.contains('active')).toBe(true);
    expect(canonTab.classList.contains('active')).toBe(false);
  });

  it('keeps Canonical tab active by default (no opts)', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubCtxDetailFetch(window);

    await window.loadCtxDetail('skills', 'demo');

    const detailEl = window.document.getElementById('ctx-skills-detail');
    const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
    const canonTab = detailEl.querySelector('.ctx-detail-tab[data-pane="canonical"]');
    expect(canonTab.classList.contains('active')).toBe(true);
    expect(diffTab.classList.contains('active')).toBe(false);
  });

  it('keeps Canonical tab active when autoOpenDiff: false', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubCtxDetailFetch(window);

    await window.loadCtxDetail('skills', 'demo', { autoOpenDiff: false });

    const detailEl = window.document.getElementById('ctx-skills-detail');
    const diffTab = detailEl.querySelector('.ctx-detail-tab[data-pane="diff"]');
    const canonTab = detailEl.querySelector('.ctx-detail-tab[data-pane="canonical"]');
    expect(canonTab.classList.contains('active')).toBe(true);
    expect(diffTab.classList.contains('active')).toBe(false);
  });
});

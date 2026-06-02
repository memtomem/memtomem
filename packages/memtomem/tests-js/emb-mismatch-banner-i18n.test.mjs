/* Behavioral guard for the embedding-mismatch banner i18n (#976, PR #1184).
 *
 * ``checkEmbeddingMismatch()`` fires at module load and now builds its text
 * with ``t('banner.emb_mismatch*')``. The locale cache is only populated by
 * ``I18N.init()`` in the DOMContentLoaded path, so the module-load fetch can
 * win the race and ``t()`` would fall back to the raw key. The fix caches the
 * status payload and re-renders the banner on ``langchange`` (init dispatches
 * one once the cache is ready). See feedback_i18n_init_order_race.md.
 *
 * This test pins the *contract*, not just the wiring: it asserts the banner
 * ends up showing localized text after boot, and — critically — re-renders
 * into Korean on ``setLang('ko')``. That single toggle defeats the mutations
 * a structural grep can't catch: dropping ``_embMismatchData = data``, making
 * the render a no-op, or rendering without the ``banner.emb_mismatch*`` keys
 * all leave the banner stuck in English (or showing the raw key) and fail here.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const MISMATCH = {
  has_mismatch: true,
  dimension_mismatch: true,
  model_mismatch: true,
  stored: { dimension: 384, provider: 'onnx', model: 'bge-small' },
  configured: { dimension: 1024, provider: 'onnx', model: 'bge-m3' },
};

async function flush(window) {
  // JSDOM defers DOMContentLoaded (→ I18N.init → locale load → langchange)
  // and the module-load /api/embedding-status fetch across several microtasks.
  for (let i = 0; i < 16; i++) await new Promise((r) => window.setTimeout(r, 0));
}

describe('embedding-mismatch banner i18n (#976)', () => {
  it('renders localized text after the init-order race, and re-renders on language toggle', async () => {
    const dom = await bootApp({ apiResponses: { '/api/embedding-status': MISMATCH } });
    const { window } = dom;
    const { document } = window;

    await flush(window);

    const msg = document.getElementById('emb-banner-msg');
    const en = msg.textContent;

    // Settled state must be real English copy, never the raw locale keys —
    // proving the langchange re-render fixed up any lost race.
    expect(en).toContain('Embedding mismatch');
    expect(en).not.toContain('banner.emb_mismatch');
    // The DB/config facts survive interpolation.
    expect(en).toContain('384');
    expect(en).toContain('bge-m3');
    expect(en).toContain('Search may not work until resolved');

    // Toggle to Korean: the banner must re-render from cached data via t(),
    // which only works if _embMismatchData was cached AND the langchange
    // listener calls renderEmbMismatchBanner().
    await window.I18N.setLang('ko');
    await flush(window);

    const ko = msg.textContent;
    expect(ko).not.toBe(en);
    expect(ko).toContain('임베딩 불일치');
    expect(ko).toContain('차원');
    expect(ko).toContain('384'); // interpolated facts preserved across locales
    expect(ko).not.toContain('banner.emb_mismatch');
  });
});

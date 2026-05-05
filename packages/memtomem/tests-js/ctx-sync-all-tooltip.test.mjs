/* Regression guard for the Sync All disabled-state hover tooltip.
 *
 * When every surface is runtime-only, ``loadCtxOverview`` previously
 * only set ``data-runtime-only="true"`` (CSS dim) and routed click
 * to a guidance toast. The user had to click first to learn why the
 * button looked dimmed. This guard pins the new pre-click affordance:
 * the button gains a localized ``title`` + ``aria-disabled`` whenever
 * the gate fires, and clears them when the gate releases.
 *
 * A second guard pins the ``langchange`` refresh — JS-owned ``title``
 * strings (vs. ``data-i18n-title``) don't auto-translate, so the
 * locale toggle must rewrite the attribute as long as the gate is
 * still active.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function stubOverviewFetch(window, { allRuntimeOnly }) {
  // ``allRuntimeOnly`` flips the gate: when true every surface reports
  // ``missing_canonical === total`` so ``loadCtxOverview`` should set
  // the disabled affordances. When false at least one canonical exists
  // so the button stays enabled.
  const data = allRuntimeOnly
    ? {
      skills:   { total: 2, missing_canonical: 2 },
      commands: { total: 1, missing_canonical: 1 },
      agents:   { total: 1, missing_canonical: 1 },
    }
    : {
      skills:   { total: 2, in_sync: 2 },
      commands: { total: 1, in_sync: 1 },
      agents:   { total: 1, in_sync: 1 },
    };
  // Delegate locale fetches to bootApp's stub so I18N caches both
  // languages — without this ``setLang('ko')`` would resolve to ``{}``
  // and ``t()`` would silently fall back to English, masking the
  // langchange refresh assertion.
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/api/context/overview')) {
      return { ok: true, status: 200, json: async () => data };
    }
    return upstream(input);
  };
}

describe('Sync All — disabled-state tooltip', () => {
  it('sets title + aria-disabled when every surface is runtime-only', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubOverviewFetch(window, { allRuntimeOnly: true });
    // Drain the I18N.init langchange that app.js's DCL handler kicks
    // off before calling loadCtxOverview — otherwise that langchange
    // races with the loadCtxOverview await and the listener at
    // ``context-gateway.js:191`` ends up doubling for the in-function
    // title-set (mutation tests miss it).
    await window.I18N.init();

    await window.loadCtxOverview();

    const btn = window.document.getElementById('ctx-sync-all-btn');
    expect(btn.dataset.runtimeOnly).toBe('true');
    expect(btn.getAttribute('aria-disabled')).toBe('true');
    expect(btn.title).toBeTruthy();
    expect(btn.title).not.toBe('settings.ctx.sync_all_disabled_tooltip');
  });

  it('clears title + aria-disabled when at least one canonical exists', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    // Pre-stamp the button as if a previous gate had fired so the
    // negative branch's "remove" path is exercised. Without this the
    // assertions could pass simply because the attributes were never
    // touched.
    const btn = window.document.getElementById('ctx-sync-all-btn');
    btn.dataset.runtimeOnly = 'true';
    btn.setAttribute('aria-disabled', 'true');
    btn.title = 'stale tooltip';

    stubOverviewFetch(window, { allRuntimeOnly: false });
    await window.loadCtxOverview();

    expect(btn.dataset.runtimeOnly).toBeUndefined();
    expect(btn.getAttribute('aria-disabled')).toBeNull();
    // PR #813 added a default ``data-i18n-title`` (sync_all_tooltip), so
    // releasing the gate must restore that locale-aware default rather
    // than wipe the attribute. Pin to the resolved string so a future
    // ``removeAttribute('title')`` regression fails loudly. Also confirm
    // the stale tooltip set above was actually replaced.
    expect(btn.title).toBe(window.I18N.t('settings.ctx.sync_all_tooltip'));
    expect(btn.title).not.toBe('stale tooltip');
  });

  it('refreshes title on langchange while the gate is still active', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubOverviewFetch(window, { allRuntimeOnly: true });
    await window.loadCtxOverview();

    const btn = window.document.getElementById('ctx-sync-all-btn');
    const titleEn = btn.title;
    expect(titleEn).toBeTruthy();

    // setLang flips the locale, applies DOM, dispatches langchange —
    // the listener in context-gateway.js should rewrite the title.
    await window.I18N.setLang('ko');
    const titleKo = btn.title;
    expect(titleKo).toBeTruthy();
    expect(titleKo).not.toBe(titleEn);
  });
});

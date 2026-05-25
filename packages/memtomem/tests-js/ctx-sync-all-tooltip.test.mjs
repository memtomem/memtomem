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
    // ``loadCtxOverview`` also calls ``_ctxFetchProjects`` → GET
    // /api/context/projects. bootApp's default stub returns ``{}`` for it,
    // which since #1100 is a shape failure that fires its own error toast —
    // that would land *first* in ``#toast-container`` and shadow the
    // click-toast these tests read back. Return a minimal valid scopes
    // payload so the projects fetch stays silent and the subject toast is
    // the only one present.
    if (url.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: [] }) };
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

  it('toast on click uses project_local copy when the active tier is project_local', async () => {
    // #1075 / #962: the disabled-state hover title for project_local
    // says "no runtime fan-out" (canonical drafts only), but the
    // post-click toast previously hard-coded the generic
    // ``sync_all_disabled_tooltip`` ("canonicals missing / import
    // first"). For a project_local user with canonical drafts already
    // present, that guidance is misleading. Pin the tier-aware toast
    // copy so the hover/click pair stays consistent.
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubOverviewFetch(window, { allRuntimeOnly: true });
    await window.I18N.init();
    await window.loadCtxOverview();

    // Flip to project_local via the tier filter button — this is the
    // only externally-reachable path to mutate the module-local
    // ``_ctxTargetScope``. The click handler updates it synchronously
    // before the (awaited-here) loadCtxOverview re-render runs.
    const tierBtn = window.document.querySelector(
      '.ctx-tier-filter button[data-scope="project_local"]'
    );
    expect(tierBtn).not.toBeNull();
    tierBtn.click();
    // The click schedules a loadCtxOverview but doesn't await it; await
    // one explicitly so the re-render's project_local branch lands
    // (``dataset.runtimeOnly='true'`` + project_local hover title).
    await window.loadCtxOverview();

    const btn = window.document.getElementById('ctx-sync-all-btn');
    expect(btn.dataset.runtimeOnly).toBe('true');
    expect(btn.title).toBe(window.I18N.t('settings.ctx.project_local_no_fanout_tooltip'));

    // Click and assert the toast text matches the project_local copy,
    // not the generic "canonicals missing" message. Toast rendering is
    // synchronous in ``showToast`` (DOM append before the dismiss
    // timer), so we can read it back immediately.
    btn.click();
    const toastMsg = window.document.querySelector('#toast-container .toast-msg');
    expect(toastMsg).not.toBeNull();
    expect(toastMsg.textContent).toBe(
      window.I18N.t('settings.ctx.project_local_no_fanout_tooltip')
    );
    expect(toastMsg.textContent).not.toBe(
      window.I18N.t('settings.ctx.sync_all_disabled_tooltip')
    );
  });

  it('toast on click keeps the generic copy on non-project_local tiers', async () => {
    // Negative companion to the project_local test above: on
    // project_shared (default) with everything runtime-only, the toast
    // must still surface the generic ``sync_all_disabled_tooltip``
    // ("import first") copy. Without this pin, a future refactor that
    // accidentally branches both tiers to the project_local copy would
    // silently slip through.
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubOverviewFetch(window, { allRuntimeOnly: true });
    await window.I18N.init();
    await window.loadCtxOverview();

    const btn = window.document.getElementById('ctx-sync-all-btn');
    expect(btn.dataset.runtimeOnly).toBe('true');
    btn.click();
    const toastMsg = window.document.querySelector('#toast-container .toast-msg');
    expect(toastMsg).not.toBeNull();
    expect(toastMsg.textContent).toBe(
      window.I18N.t('settings.ctx.sync_all_disabled_tooltip')
    );
    expect(toastMsg.textContent).not.toBe(
      window.I18N.t('settings.ctx.project_local_no_fanout_tooltip')
    );
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

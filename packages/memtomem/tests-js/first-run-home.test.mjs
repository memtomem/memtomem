import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// S2.1 — a brand-new install lands on Home (orientation) instead of the empty
// Search screen. Returning installs keep their saved default tab, and a
// deep-link hash always wins. bootApp defaults to a returning install (it seeds
// m2m-app-initialized); first-run tests opt in with { firstRun: true } or by
// clearing localStorage before calling the landing helper directly.
describe('First-run landing on Home (S2.1)', () => {
  it('renders onboarding progress from the backend bootstrap stage', async () => {
    const dom = await bootApp();
    const { window } = dom;
    await window.I18N.init();

    window._applyBootstrapOrientation({
      stage: 'needs_source',
      total_sources: 0,
      total_chunks: 0,
    });
    const orientation = window.document.getElementById('home-orientation');
    expect(orientation.open).toBe(true);
    expect(orientation.dataset.stage).toBe('needs_source');
    expect(window.document.getElementById('home-orientation-status').textContent)
      .toContain('No indexed source');

    window._applyBootstrapOrientation({
      stage: 'ready',
      total_sources: 2,
      total_chunks: 8,
    });
    const steps = orientation.querySelectorAll('.home-orientation-step');
    expect(steps[0].classList.contains('is-complete')).toBe(true);
    expect(steps[1].classList.contains('is-complete')).toBe(true);
    expect(window.document.getElementById('home-orientation-status').textContent)
      .toContain('2 sources and 8 chunks');
  });

  it('_isFirstRun() is true only when no app-owned localStorage key exists', async () => {
    const dom = await bootApp();
    const { window } = dom;

    window.localStorage.clear();
    expect(window._isFirstRun()).toBe(true);

    // Any app-owned key (under the m2m-*/memtomem prefixes) marks the install as
    // already used — the prefix scan catches keys a hand-list would miss, e.g.
    // search history and saved queries written elsewhere in the app.
    for (const key of [
      'm2m-app-initialized', 'm2m-default-tab', 'm2m-theme', 'm2m-pins',
      'm2m-saved-queries', 'memtomem_search_history', 'memtomem.sources_width',
    ]) {
      window.localStorage.clear();
      window.localStorage.setItem(key, 'x');
      expect(window._isFirstRun(), `${key} should mark a returning install`).toBe(false);
    }

    // memtomem.index.mode is written by the app on a cold boot (setIndexMode
    // persists the default), so it is NOT a sign of prior use on its own.
    window.localStorage.clear();
    window.localStorage.setItem('memtomem.index.mode', 'compose');
    expect(window._isFirstRun()).toBe(true);
  });

  it('routes a genuine first run to the Home tab and stamps the sentinel', async () => {
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const active = document.querySelector('.tab-btn.active');
    expect(active.dataset.tab).toBe('home');
    // The first no-hash visit stamps the install so the next one is "returning".
    expect(window.localStorage.getItem('m2m-app-initialized')).toBe('1');
  });

  it('leaves a returning install on the saved default (Search)', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const active = document.querySelector('.tab-btn.active');
    expect(active.dataset.tab).toBe('search');
  });

  it('_applyLandingTab routes by first-run / default and yields to a deep link', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const calls = [];
    window.activateTab = (name) => { calls.push(name); };

    // First run (cleared storage) → Home.
    window.localStorage.clear();
    window._applyLandingTab();
    expect(calls).toEqual(['home']);

    // Returning install with an explicit saved default → that tab.
    calls.length = 0;
    window.localStorage.setItem('m2m-app-initialized', '1');
    window.localStorage.setItem('m2m-default-tab', 'sources');
    window._applyLandingTab();
    expect(calls).toEqual(['sources']);

    // A deep-link hash is owned by the earlier hash handler — no auto-routing.
    calls.length = 0;
    window.location.hash = '#timeline';
    window._applyLandingTab();
    expect(calls).toEqual([]);
  });

  it('does not force-route to Home when the sentinel cannot persist (private mode)', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { I18N } = window;
    await I18N.init();

    const calls = [];
    window.activateTab = (name) => { calls.push(name); };

    // Fresh storage (would be first-run) but setItem throws like a locked-down
    // private mode: getItem/scan still work, the stamp write fails. We must fall
    // back to the default tab (Search) rather than route to Home on every visit.
    // Boot already left Search active, so the fallback is a no-op — the point is
    // that Home is NOT requested. Without the stamped-guard this would be ['home'].
    window.localStorage.clear();
    const protoSet = window.Storage.prototype.setItem;
    window.Storage.prototype.setItem = () => { throw new Error('QuotaExceededError'); };
    try {
      window._applyLandingTab();
    } finally {
      window.Storage.prototype.setItem = protoSet;
    }
    expect(calls).not.toContain('home');
    expect(calls).toEqual([]);
  });

  it('boots without crashing when storage throws on getItem (locked-down mode)', async () => {
    // A storage that throws on every getItem (not just setItem) must not abort
    // app.js at module load — initTheme reads a key before the landing logic.
    const dom = await bootApp({ storageBlock: ['getItem'] });
    const { window } = dom;
    const { document } = window;

    // app.js finished loading (functions defined) despite the boot-time read.
    expect(typeof window.activateTab).toBe('function');
    expect(window._lsGet('m2m-theme')).toBe(null);

    // The deferred landing handler runs and falls back to a safe default tab.
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(document.querySelector('.tab-btn.active')?.dataset.tab).toBe('search');
  });
});

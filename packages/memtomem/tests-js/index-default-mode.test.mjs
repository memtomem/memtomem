import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { bootApp, STATIC_DIR } from './setup/jsdom-app.mjs';

// S1.6 — a new install defaults the Index tab to the most intuitive
// "New memory" (compose) mode instead of the technical folder scan, and the
// toggle lists it first. Returning users keep their saved choice.
describe('Index default mode (S1.6)', () => {
  it('defaults a fresh install to compose (New memory)', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document } = window;

    expect(window._readIndexMode()).toBe('compose');
    // Module-load init applies the mode: compose tab active + panel visible.
    expect(document.getElementById('index-mode-compose').classList.contains('btn-active')).toBe(true);
    expect(document.getElementById('index-mode-compose').getAttribute('aria-selected')).toBe('true');
    expect(document.getElementById('index-mode-compose').getAttribute('tabindex')).toBe('0');
    expect(document.getElementById('index-panel-compose').hidden).toBe(false);
    expect(document.getElementById('index-panel-folder').hidden).toBe(true);
    expect(document.getElementById('index-mode-folder').classList.contains('btn-active')).toBe(false);
  });

  it('lists New memory (compose) first in the toggle', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const order = [...dom.window.document
      .querySelectorAll('.index-mode-toggle [role="tab"]')].map(b => b.dataset.mode);
    expect(order).toEqual(['compose', 'folder', 'upload']);
  });

  it('preserves a returning user\'s saved mode', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document } = window;

    window.localStorage.setItem('memtomem.index.mode', 'folder');
    expect(window._readIndexMode()).toBe('folder');
    window.setIndexMode(window._readIndexMode());
    expect(document.getElementById('index-mode-folder').classList.contains('btn-active')).toBe(true);
    expect(document.getElementById('index-mode-compose').classList.contains('btn-active')).toBe(false);
    expect(document.getElementById('index-panel-folder').hidden).toBe(false);
  });

  it('static HTML shows the compose panel by default (pre-JS, no tab/panel mismatch)', () => {
    const html = readFileSync(path.join(STATIC_DIR, 'index.html'), 'utf-8');
    // The compose tab is statically selected, so the compose panel must be the
    // unhidden one and folder/upload hidden — otherwise screen readers see a
    // selected tab whose panel is hidden before app.js boots.
    expect(/<div id="index-panel-compose"[^>]*\bhidden\b/.test(html)).toBe(false);
    expect(/<div id="index-panel-folder"[^>]*\bhidden\b/.test(html)).toBe(true);
    expect(/<div id="index-panel-upload"[^>]*\bhidden\b/.test(html)).toBe(true);
  });

  it('switches to folder mode for the Home folder/reindex quick actions', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document } = window;
    expect(window.STATE.indexMode).toBe('compose');

    document.getElementById('home-index-btn').click();
    expect(window.STATE.indexMode).toBe('folder');
    expect(document.getElementById('index-panel-folder').hidden).toBe(false);

    window.setIndexMode('compose');
    document.getElementById('home-reindex-btn').click();
    expect(window.STATE.indexMode).toBe('folder');
    expect(document.getElementById('index-force').checked).toBe(true);
  });
});

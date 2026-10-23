/* The search detail panel's View/Edit and Diff/Edit toggles label the next
 * action, so their label depends on state. Each toggle used to keep one fixed
 * data-i18n key in index.html while app.js wrote English text per state, so a
 * language switch made applyDOM() put back the other state's label: in diff
 * mode the button returns to the editor, yet read "차이점" after switching to
 * Korean. The toggles now swap the data-i18n key with the state.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp();
  const { window } = dom;
  await window.I18N.init();
  await window.I18N.setLang('en');
  return window;
}

describe('search detail toggles keep their state label across a language switch', () => {
  it('View/Edit toggle', async () => {
    const window = await boot();
    const btn = window.document.getElementById('d-view-btn');

    window._setDetailMode('view');
    expect(btn.dataset.mode).toBe('view');
    expect(btn.textContent).toBe('Edit');
    await window.I18N.setLang('ko');
    expect(btn.textContent).toBe('편집');

    window._setDetailMode('edit');
    expect(btn.textContent).toBe('보기');
    await window.I18N.setLang('en');
    expect(btn.textContent).toBe('View');
  });

  it('Diff/Edit toggle', async () => {
    const window = await boot();
    const { document } = window;
    const btn = document.getElementById('d-diff-btn');
    window.eval("STATE.selectedOriginal = 'before';");
    document.getElementById('d-editor').value = 'after';

    btn.click();
    expect(btn.dataset.mode).toBe('diff');
    expect(btn.textContent).toBe('Edit');
    await window.I18N.setLang('ko');
    expect(btn.textContent).toBe('편집');

    btn.click();
    expect(btn.dataset.mode).toBe('source');
    expect(btn.textContent).toBe('차이점');
    await window.I18N.setLang('en');
    expect(btn.textContent).toBe('Diff');
  });

  it('shows the English label, not the raw key, before the locale loads', async () => {
    let release;
    const localeGate = new Promise((r) => { release = r; });
    const dom = await bootApp({ localeGate });
    const { window } = dom;
    const btn = window.document.getElementById('d-view-btn');

    window._setDetailMode('view');
    expect(btn.textContent).toBe('Edit');
    expect(btn.dataset.i18n).toBe('search.detail_edit');

    release();
    await window.I18N.init();
    await window.I18N.setLang('ko');
    expect(btn.textContent).toBe('편집');
  });

  it('clearDetail writes the empty-state text in the current language', async () => {
    const window = await boot();
    await window.I18N.setLang('ko');
    window.clearDetail();
    const p = window.document.getElementById('detail-empty').querySelector('p');
    expect(p.textContent).toBe(window.t('search.detail_empty'));
    expect(p.textContent).not.toBe('Select a result to view details');
  });
});

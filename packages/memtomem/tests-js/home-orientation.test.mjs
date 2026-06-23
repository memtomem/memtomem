import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// S2.3 — first-run Home orientation block. bootApp fires app.js's
// DOMContentLoaded handler, which already calls _initHomeOrientation() once, so
// tests rely on that single boot-time wiring (a manual re-call would double-bind
// the CTA listeners). The restore test re-calls it deliberately to simulate a
// fresh page load and only asserts the open/class outcome, not call counts.
describe('First-run Home orientation block (S2.3)', () => {
  it('lands expanded with a localized title and mirrors the open state onto the layout', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const details = document.getElementById('home-orientation');
    expect(details.open).toBe(true);
    expect(document.querySelector('.home-orientation-summary').textContent).toBe('Getting started');
    // The open state is mirrored onto .home-layout so CSS can hide the
    // heatmap's "sample" summary while a new user is still orienting.
    expect(details.closest('.home-layout').classList.contains('orientation-open')).toBe(true);
  });

  it('wires the three add → connect → search CTAs to their tabs', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    // Spy on activateTab rather than clicking through to the real tab loaders —
    // the gateway tab pulls in globals from scripts this harness doesn't boot,
    // and the wiring (CTA → correct tab name) is what S2.3 actually adds.
    const calls = [];
    window.activateTab = (name) => { calls.push(name); };

    document.getElementById('home-orientation-add').click();
    document.getElementById('home-orientation-connect').click();
    document.getElementById('home-orientation-search').click();
    expect(calls).toEqual(['index', 'context-gateway', 'search']);
  });

  it('persists the collapse choice and restores it on the next load', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const details = document.getElementById('home-orientation');
    const layout = details.closest('.home-layout');
    expect(details.open).toBe(true);

    // Collapse it. Dispatch the event the toggle handler listens for so the test
    // is deterministic regardless of jsdom's <details> activation support.
    details.open = false;
    details.dispatchEvent(new window.Event('toggle'));
    expect(window.localStorage.getItem('m2m-home-orientation-collapsed')).toBe('1');
    expect(layout.classList.contains('orientation-open')).toBe(false);

    // Simulate the next page load: reset to the HTML default, re-init, and the
    // persisted collapse should win.
    details.open = true;
    window._initHomeOrientation();
    expect(details.open).toBe(false);
    expect(layout.classList.contains('orientation-open')).toBe(false);
  });

  it('localizes the orientation copy and relocalizes on langchange', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const summary = document.querySelector('.home-orientation-summary');
    const addCta = document.getElementById('home-orientation-add');
    expect(summary.textContent).toBe('Getting started');
    expect(addCta.textContent).toBe('Add memories');

    await I18N.setLang('ko');
    expect(summary.textContent).toBe('시작하기');
    expect(addCta.textContent).toBe('기억 추가');
  });
});

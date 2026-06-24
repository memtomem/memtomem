import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// S3.1 — first-run wizard. A one-time, three-step guided intro (add → search →
// connect) shown over Home on a genuine first run. It rides the same landing
// signal as S2.1 (bootApp({ firstRun: true })), so a returning install never
// sees it. It reuses the orientation block's copy + CTA labels and collapses
// that block on close so a dismissing user is not shown the same steps twice.
describe('First-run wizard (S3.1)', () => {
  const wiz = (window) => window.document.getElementById('first-run-wizard');
  const stepEl = (window, n) =>
    window.document.querySelector(`.fr-wizard-step[data-step="${n}"]`);
  const byId = (window, id) => window.document.getElementById(id);
  const inertBodyChildren = (window) =>
    Array.from(window.document.body.children).filter((el) => el.hasAttribute('inert')).length;

  it('opens over Home on a genuine first run, starting on step 1', async () => {
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    await window.I18N.init();

    expect(wiz(window).hidden).toBe(false);
    expect(window.isAnyModalOpen()).toBe(true);
    // Landed on Home underneath the modal (the wizard never routes on its own).
    expect(window.document.querySelector('.tab-btn.active').dataset.tab).toBe('home');
    // Step 1 visible, the rest hidden; Back is hidden on the first step.
    expect(stepEl(window, 1).hidden).toBe(false);
    expect(stepEl(window, 2).hidden).toBe(true);
    expect(byId(window, 'fr-wizard-back').hidden).toBe(true);
    expect(byId(window, 'fr-wizard-counter-text').textContent).toBe('Step 1 of 3');
  });

  it('stays hidden for a returning install', async () => {
    const dom = await bootApp(); // returning — bootApp seeds m2m-app-initialized
    const { window } = dom;
    await window.I18N.init();
    expect(wiz(window).hidden).toBe(true);
    expect(window.isAnyModalOpen()).toBe(false);
  });

  it('pages forward and back, switching Next to Done on the last step', async () => {
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    await window.I18N.init();

    const next = byId(window, 'fr-wizard-next');
    const back = byId(window, 'fr-wizard-back');
    const counter = byId(window, 'fr-wizard-counter-text');
    expect(next.textContent).toBe('Next');

    next.click(); // → step 2
    expect(stepEl(window, 2).hidden).toBe(false);
    expect(stepEl(window, 1).hidden).toBe(true);
    expect(back.hidden).toBe(false);
    expect(counter.textContent).toBe('Step 2 of 3');

    next.click(); // → step 3
    expect(stepEl(window, 3).hidden).toBe(false);
    expect(next.textContent).toBe('Done');
    expect(counter.textContent).toBe('Step 3 of 3');

    back.click(); // → step 2
    expect(stepEl(window, 2).hidden).toBe(false);
    expect(next.textContent).toBe('Next');
  });

  it('Done on the last step closes the wizard and clears the modal stack', async () => {
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    await window.I18N.init();
    const next = byId(window, 'fr-wizard-next');
    next.click();
    next.click(); // → step 3 (Done)
    next.click(); // Done
    expect(wiz(window).hidden).toBe(true);
    expect(window.isAnyModalOpen()).toBe(false);
  });

  it('each step CTA jumps to its tab and closes the wizard', async () => {
    for (const [btnId, tab] of [
      ['fr-wizard-cta-add', 'index'],
      ['fr-wizard-cta-search', 'search'],
      ['fr-wizard-cta-connect', 'context-gateway'],
    ]) {
      const dom = await bootApp({ firstRun: true });
      const { window } = dom;
      await window.I18N.init();
      const calls = [];
      window.activateTab = (name) => { calls.push(name); };
      byId(window, btnId).click();
      expect(calls, `${btnId} → ${tab}`).toEqual([tab]);
      expect(wiz(window).hidden).toBe(true);
    }
  });

  it('Skip, the ✕ button, and Escape all dismiss the wizard', async () => {
    // Skip + close button.
    for (const btnId of ['fr-wizard-skip', 'fr-wizard-close']) {
      const dom = await bootApp({ firstRun: true });
      const { window } = dom;
      await window.I18N.init();
      expect(wiz(window).hidden).toBe(false);
      byId(window, btnId).click();
      expect(wiz(window).hidden).toBe(true);
    }
    // Escape routes through the keydown dispatcher's wizard branch.
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    await window.I18N.init();
    expect(wiz(window).hidden).toBe(false);
    window.document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    expect(wiz(window).hidden).toBe(true);
    // The closer ran through _closeFirstRunWizard (release), not closeModal's
    // fallback hide() — so the a11y stack fully unwound.
    expect(window.isAnyModalOpen()).toBe(false);
    expect(inertBodyChildren(window)).toBe(0);
  });

  // Regression guard for the show-before-init race (Codex review): the async
  // boot handler is suspended on ``await I18N.init()`` when the synchronous
  // landing handler opens the wizard, so _showFirstRunWizard must wire the modal
  // closer itself. If it does not, closeModal falls back to a bare hide() and
  // the background inert + focus-trap + _ACTIVE_MODALS entry leak. This asserts
  // the full open→release lifecycle instead of just the hidden flag.
  it('inerts the background on open and fully releases it on close', async () => {
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    await window.I18N.init();
    expect(wiz(window).hidden).toBe(false);
    expect(window.isAnyModalOpen()).toBe(true);
    // openModal inerted every body child except the wizard itself.
    const inertedSiblings = Array.from(window.document.body.children)
      .filter((el) => el.hasAttribute('inert') && el !== wiz(window)).length;
    expect(inertedSiblings).toBeGreaterThan(0);
    // Close via the shared closeModal path (what Esc uses) → full release.
    window.closeModal(wiz(window));
    expect(window.isAnyModalOpen()).toBe(false);
    expect(inertBodyChildren(window)).toBe(0);
  });

  it('collapses the Home orientation block on close (no double onboarding)', async () => {
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    await window.I18N.init();
    const orientation = byId(window, 'home-orientation');
    expect(orientation.open).toBe(true); // lands expanded on first run
    byId(window, 'fr-wizard-skip').click();
    expect(orientation.open).toBe(false);
  });

  it('relocalizes the JS-rendered counter and static labels on langchange', async () => {
    const dom = await bootApp({ firstRun: true });
    const { window } = dom;
    await window.I18N.init();
    const counter = byId(window, 'fr-wizard-counter-text');
    expect(counter.textContent).toBe('Step 1 of 3');
    expect(byId(window, 'fr-wizard-cta-add').textContent).toBe('Add memories');

    await window.I18N.setLang('ko');
    expect(counter.textContent).toBe('3단계 중 1단계');
    expect(byId(window, 'fr-wizard-cta-add').textContent).toBe('기억 추가');
  });
});

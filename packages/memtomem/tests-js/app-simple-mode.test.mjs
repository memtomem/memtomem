import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// S2.2 — app-level Simple/Advanced progressive disclosure. Simple is the
// default-when-unset (gateway D-F precedent): it demotes the Tags + Timeline
// tabs and the Settings → Data group behind an Advanced toggle in the global
// <header>. The toggle stays reachable on every entry path, and — the highest-
// risk part — navigation is *gated* (not just painted) through the single
// _visibleMainTabs / _visibleSettingsSections predicate so no deep-link,
// saved-default, popstate, or programmatic jump can strand the user on a tab
// whose button is hidden (the #1358 class).
const ADVANCED_TABS = ['tags', 'timeline'];
const ALWAYS_TABS = ['home', 'search', 'sources', 'context-gateway', 'index', 'settings'];
const ADVANCED_SECTIONS = ['dedup', 'decay', 'export'];

describe('App-level Simple/Advanced (S2.2)', () => {
  it('defaults to Simple when no flag is stored, hiding the advanced surface', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    expect(document.body.classList.contains('app-simple')).toBe(true);

    const visible = window._visibleMainTabs();
    for (const tab of ADVANCED_TABS) {
      expect(visible, `${tab} hidden in Simple`).not.toContain(tab);
    }
    for (const tab of ALWAYS_TABS) {
      expect(visible, `${tab} always visible`).toContain(tab);
    }

    const sections = window._visibleSettingsSections();
    for (const sec of ADVANCED_SECTIONS) {
      expect(sections, `${sec} hidden in Simple`).not.toContain(sec);
    }
    // The Settings General group stays reachable so the tab is never a dead end.
    expect(sections).toContain('config');
    expect(sections).toContain('namespaces');

    // Toggle reflects the current mode: Simple => Advanced not engaged.
    const toggle = document.getElementById('app-mode-toggle');
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.querySelector('.app-mode-label').textContent).toBe('Simple');
  });

  it('boots expanded when the persisted flag is Advanced (0)', async () => {
    const dom = await bootApp({ seedStorage: { 'm2m-app-simple': '0' } });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    expect(document.body.classList.contains('app-simple')).toBe(false);

    const visible = window._visibleMainTabs();
    for (const tab of [...ALWAYS_TABS, ...ADVANCED_TABS]) {
      expect(visible, `${tab} visible in Advanced`).toContain(tab);
    }
    expect(window._visibleSettingsSections()).toEqual(
      expect.arrayContaining(ADVANCED_SECTIONS),
    );

    const toggle = document.getElementById('app-mode-toggle');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(toggle.querySelector('.app-mode-label').textContent).toBe('Advanced');
  });

  it('the header toggle flips, persists, and re-labels both ways', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const toggle = document.getElementById('app-mode-toggle');
    const label = () => toggle.querySelector('.app-mode-label').textContent;

    // Simple -> Advanced
    toggle.click();
    expect(window.localStorage.getItem('m2m-app-simple')).toBe('0');
    expect(document.body.classList.contains('app-simple')).toBe(false);
    expect(window._visibleMainTabs()).toEqual(expect.arrayContaining(ADVANCED_TABS));
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(label()).toBe('Advanced');

    // Advanced -> Simple
    toggle.click();
    expect(window.localStorage.getItem('m2m-app-simple')).toBe('1');
    expect(document.body.classList.contains('app-simple')).toBe(true);
    expect(window._visibleMainTabs()).not.toContain('timeline');
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(label()).toBe('Simple');
  });

  it('activateTab redirects away from an advanced tab while in Simple', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    // Real activateTab so the guard runs (a spy would shadow the recursive
    // redirect call). The guard must bounce to a visible tab, never strand.
    window.activateTab('timeline');
    const active = document.querySelector('.tab-btn.active');
    expect(active.dataset.tab).not.toBe('timeline');
    expect(window._visibleMainTabs()).toContain(active.dataset.tab);
    expect(document.getElementById('tab-timeline')?.classList.contains('active')).toBe(false);
  });

  it('_applyLandingTab clamps a stale saved default-tab to a visible tab', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { I18N } = window;
    await I18N.init();

    const calls = [];
    window.activateTab = (name) => { calls.push(name); };

    // Returning user whose saved default is now an advanced (hidden) tab must
    // not be force-routed there on every boot — clamp to the first visible tab.
    window.localStorage.setItem('m2m-app-initialized', '1');
    window.localStorage.setItem('m2m-default-tab', 'timeline');
    window._applyLandingTab();
    expect(calls).toHaveLength(1);
    expect(calls[0]).not.toBe('timeline');
    expect(window._visibleMainTabs()).toContain(calls[0]);
  });

  it('popstate ignores a Back/Forward entry pointing at a now-hidden tab', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { I18N } = window;
    await I18N.init();

    const calls = [];
    window.activateTab = (name) => { calls.push(name); };

    // History entry recorded while Timeline was visible, replayed after the user
    // dropped back to Simple — must be swallowed, not re-activated.
    window.dispatchEvent(new window.PopStateEvent('popstate', { state: { tab: 'timeline' } }));
    expect(calls).not.toContain('timeline');
    expect(calls).toHaveLength(0);

    // A visible tab still navigates on Back/Forward.
    window.dispatchEvent(new window.PopStateEvent('popstate', { state: { tab: 'sources' } }));
    expect(calls).toEqual(['sources']);
  });

  it('the Cmd+K palette omits advanced commands in Simple, lists them in Advanced', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-namespaces.js'] });
    const { window } = dom;
    const { I18N } = window;
    await I18N.init();

    const tabIds = (groups) => groups.flatMap(g => g.items).map(i => i.tab).filter(Boolean);
    const sectionIds = (groups) => groups.flatMap(g => g.items).map(i => i.section).filter(Boolean);

    // Simple (default): no Tags/Timeline tab commands, no Dedup/Export section
    // commands — the palette must match the visible surface, not redirect away.
    let groups = window._buildCommands();
    expect(tabIds(groups)).not.toContain('tags');
    expect(tabIds(groups)).not.toContain('timeline');
    expect(sectionIds(groups)).not.toContain('dedup');
    expect(sectionIds(groups)).not.toContain('export');
    // Always-on destinations stay listed.
    expect(tabIds(groups)).toEqual(expect.arrayContaining(['home', 'search', 'index']));
    expect(sectionIds(groups)).toContain('config');

    // Advanced: they reappear.
    window._setAppSimple(false);
    groups = window._buildCommands();
    expect(tabIds(groups)).toEqual(expect.arrayContaining(['tags', 'timeline']));
    expect(sectionIds(groups)).toEqual(expect.arrayContaining(['dedup', 'export']));
  });

  it('redirects a hidden Settings section to Config, never bounces to the Gateway tab', async () => {
    const dom = await bootApp();
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();
    // The fallback section (config) is the only loader that fires; stub it so
    // switchSettingsSection runs end-to-end without settings-config.js.
    window.loadConfig = () => {};
    const tabCalls = [];
    window.activateTab = (name) => { tabCalls.push(name); };

    // dedup is hidden in Simple. The Gateway sidebar (ctx-overview, …) shares the
    // .settings-nav-btn class and sorts first, so a naive visible[0] fallback
    // would hop to the Gateway tab. It must land on a Settings-tab section.
    window.switchSettingsSection('dedup');

    expect(document.querySelector('.settings-nav-btn.active')?.dataset.section).toBe('config');
    expect(tabCalls).not.toContain('context-gateway');
  });

  it('labels the toggle with the current mode even before i18n loads', async () => {
    // Persisted Advanced, but I18N.init() has NOT run yet (t() still returns the
    // raw key). The literal fallback must already read "Advanced" so the toggle
    // never momentarily reports the wrong mode (Codex review, S2.2).
    const dom = await bootApp({ seedStorage: { 'm2m-app-simple': '0' } });
    const { window } = dom;
    const label = window.document.querySelector('#app-mode-toggle .app-mode-label');
    expect(label.textContent).toBe('Advanced');
  });
});

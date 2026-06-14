/* ADR-0008 PR-E — read-only wiki browser controller (wiki.js).
 *
 * Drives the real loadWiki / loadWikiDetail against stubbed /api/wiki payloads
 * and asserts: assets render grouped by type, an absent wiki (404) shows the
 * onboarding empty-state rather than an error, a non-renderable vendor
 * (commands/codex) is disabled in the detail selector, and a locale switch
 * repaints the cached list in place (langchange).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const WIKI_LIST = {
  wiki_head: 'a'.repeat(40),
  wiki_root: '/home/u/.memtomem-wiki',
  is_dirty: false,
  items: [
    {
      type: 'skills',
      name: 'alpha',
      vendors: [
        { vendor: 'claude', renderable: true },
        { vendor: 'gemini', renderable: true },
        { vendor: 'codex', renderable: true },
        { vendor: 'kimi', renderable: true },
      ],
    },
    {
      type: 'commands',
      name: 'gamma',
      vendors: [
        { vendor: 'claude', renderable: true },
        { vendor: 'gemini', renderable: true },
        { vendor: 'codex', renderable: false },
      ],
    },
  ],
};

const GAMMA_DIFF = {
  override_path: '/w/commands/gamma/overrides/claude.md',
  exists: false,
  in_sync: false,
  diff_lines: [],
  dropped: [],
};
const GAMMA_LINT = { asset_type: 'commands', name: 'gamma', ok: true, findings: [] };

const API = {
  '/api/wiki': WIKI_LIST,
  '/api/wiki/commands/gamma/diff': GAMMA_DIFF,
  '/api/wiki/commands/gamma/lint': GAMMA_LINT,
};

async function boot(apiResponses = API) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'wiki.js'], apiResponses });
  await dom.window.I18N.setLang('en');
  return dom;
}

describe('wiki.js read-only browser', () => {
  it('renders assets grouped by type with clickable items', async () => {
    const { window } = await boot();
    await window.loadWiki();
    const list = window.document.getElementById('wiki-list');
    const groups = list.querySelectorAll('.wiki-group');
    expect(groups.length).toBe(2); // skills + commands (order preserved)
    const items = list.querySelectorAll('.wiki-item');
    const names = Array.from(items).map((b) => b.dataset.name);
    expect(names).toContain('alpha');
    expect(names).toContain('gamma');
  });

  it('shows the onboarding empty-state (not an error) when the wiki is absent', async () => {
    const { window } = await boot();
    // Override fetch: /api/wiki → 404 wiki_absent, everything else empty 200.
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url;
      if (url && url.includes('/api/wiki')) {
        return {
          ok: false,
          status: 404,
          json: async () => ({ detail: { error_kind: 'missing', reason_code: 'wiki_absent' } }),
          text: async () => '',
        };
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
    };
    await window.loadWiki();
    const list = window.document.getElementById('wiki-list');
    expect(list.querySelector('.empty-state')).not.toBeNull();
    // The message is the onboarding copy, not a raw error / key echo.
    expect(list.textContent).toContain(window.t('settings.ctx.wiki_empty'));
    expect(list.querySelector('.wiki-item')).toBeNull();
  });

  it('disables a non-renderable vendor in the detail selector', async () => {
    const { window } = await boot();
    await window.loadWiki();
    await window.loadWikiDetail('commands', 'gamma');
    const select = window.document.getElementById('wiki-vendor-select');
    expect(select).not.toBeNull();
    const codex = select.querySelector('option[value="codex"]');
    const claude = select.querySelector('option[value="claude"]');
    expect(codex.disabled).toBe(true);
    expect(claude.disabled).toBe(false);
  });

  it('repaints the cached list in place on a locale switch (langchange)', async () => {
    const { window } = await boot();
    await window.loadWiki();
    const list = window.document.getElementById('wiki-list');
    expect(list.textContent).toContain('Skills'); // en group title

    await window.I18N.setLang('ko');
    window.dispatchEvent(new window.Event('langchange'));
    expect(list.textContent).toContain('스킬'); // ko group title, no refetch
    expect(list.textContent).not.toContain('Skills');

    await window.I18N.setLang('en');
  });
});

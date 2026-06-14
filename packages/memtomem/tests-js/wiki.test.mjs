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

// skills/alpha override-seed fixtures (E-2). claude is the first renderable
// vendor, so loadWikiDetail('skills','alpha') auto-selects it and fetches these.
const ALPHA_DIFF_NONE = {
  override_path: '/w/skills/alpha/overrides/claude.md',
  exists: false,
  in_sync: false,
  diff_lines: [],
  dropped: [],
};
const ALPHA_DIFF_EXISTS = { ...ALPHA_DIFF_NONE, exists: true, in_sync: true };
const ALPHA_LINT_OK = { asset_type: 'skills', name: 'alpha', ok: true, findings: [] };

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

describe('wiki.js override-seed (E-2, dev tier)', () => {
  // A fetch stub that records POSTs and serves the skills/alpha diff/lint +
  // a seed response, falling back to the boot stub for /api/wiki, /api/session,
  // and /locales.
  function recordingFetch(window, { exists, seedResponse }) {
    const posts = [];
    const base = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const p = (url || '').split('?')[0];
      const method = (init.method || 'GET').toUpperCase();
      if (method === 'POST' && p === '/api/wiki/skills/alpha/override') {
        posts.push(JSON.parse(init.body));
        return { ok: true, status: 200, json: async () => seedResponse, text: async () => '' };
      }
      if (p === '/api/wiki/skills/alpha/diff') {
        const diff = exists ? ALPHA_DIFF_EXISTS : ALPHA_DIFF_NONE;
        return { ok: true, status: 200, json: async () => diff, text: async () => '' };
      }
      if (p === '/api/wiki/skills/alpha/lint') {
        return { ok: true, status: 200, json: async () => ALPHA_LINT_OK, text: async () => '' };
      }
      return base(input, init);
    };
    return posts;
  }

  it('shows the seed button in dev mode and hides it in prod', async () => {
    const apis = {
      '/api/wiki': WIKI_LIST,
      '/api/wiki/skills/alpha/diff': ALPHA_DIFF_NONE,
      '/api/wiki/skills/alpha/lint': ALPHA_LINT_OK,
    };

    const dev = await boot(apis);
    dev.window.document.body.classList.add('dev-mode');
    await dev.window.loadWiki();
    await dev.window.loadWikiDetail('skills', 'alpha');
    const devBtn = dev.window.document.getElementById('wiki-seed-btn');
    expect(devBtn).not.toBeNull();
    expect(devBtn.dataset.exists).toBe('0');
    expect(devBtn.textContent).toContain(dev.window.t('settings.ctx.wiki_seed'));

    const prod = await boot(apis); // no dev-mode class
    await prod.window.loadWiki();
    await prod.window.loadWikiDetail('skills', 'alpha');
    expect(prod.window.document.getElementById('wiki-seed-btn')).toBeNull();
  });

  it('labels the button "Re-seed" when an override already exists', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    recordingFetch(window, { exists: true, seedResponse: {} });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    const btn = window.document.getElementById('wiki-seed-btn');
    expect(btn.dataset.exists).toBe('1');
    expect(btn.textContent).toContain(window.t('settings.ctx.wiki_reseed'));
  });

  it('a fresh seed POSTs force=false and repaints the dirty badge', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    const posts = recordingFetch(window, {
      exists: false,
      seedResponse: { seeded: true, vendor: 'claude', forced: false, dropped: [], wiki_dirty: true },
    });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    // _onWikiSeedClick is a top-level fn → awaitable directly (no flush races).
    await window._onWikiSeedClick(false);
    expect(posts).toEqual([{ vendor: 'claude', force: false }]);
    // wiki_dirty:true → HEAD badge repainted without re-listing.
    const head = window.document.getElementById('wiki-head');
    expect(head.textContent).toContain(window.t('settings.ctx.wiki_dirty'));
  });

  it('a re-seed confirms first, then POSTs force=true', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    window.showConfirm = async () => true; // accept the overwrite
    const posts = recordingFetch(window, {
      exists: true,
      seedResponse: { seeded: true, vendor: 'claude', forced: true, dropped: [], wiki_dirty: true },
    });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    await window._onWikiSeedClick(true);
    expect(posts).toEqual([{ vendor: 'claude', force: true }]);
  });

  it('a declined re-seed confirm POSTs nothing', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    window.showConfirm = async () => false; // cancel the overwrite
    const posts = recordingFetch(window, { exists: true, seedResponse: {} });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    await window._onWikiSeedClick(true);
    expect(posts).toEqual([]);
  });
});

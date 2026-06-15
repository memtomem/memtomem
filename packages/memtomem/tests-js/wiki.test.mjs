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

describe('wiki.js install/update (E-3, dev tier)', () => {
  // Boot in dev mode with a stubbed projects roster + active scope. The picker
  // reads context-gateway.js globals via typeof-guards, so setting them on the
  // window (rather than loading that script) is enough to drive the action.
  async function bootDev(active = '', cache = [{ scope_id: '', label: 'Server CWD' }]) {
    const dom = await boot({
      '/api/wiki': WIKI_LIST,
      '/api/wiki/skills/alpha/diff': ALPHA_DIFF_NONE,
      '/api/wiki/skills/alpha/lint': ALPHA_LINT_OK,
    });
    dom.window.document.body.classList.add('dev-mode');
    dom.window._ctxActiveScopeId = active;
    dom.window._ctxProjectsCache = cache;
    return dom;
  }

  // Records POSTs to the install/update routes; the 409 mode refuses a non-force
  // update with stale_install (the dirty path) and accepts the force retry.
  function recordingCtxFetch(window, { updateStatus = 200 } = {}) {
    const posts = [];
    const base = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const p = (url || '').split('?')[0];
      const method = (init.method || 'GET').toUpperCase();
      if (method === 'POST' && p === '/api/context/skills/alpha/install') {
        posts.push({ url, body: init.body ? JSON.parse(init.body) : null });
        return { ok: true, status: 200, json: async () => ({ installed: true }), text: async () => '' };
      }
      if (method === 'POST' && p === '/api/context/skills/alpha/update') {
        const body = init.body ? JSON.parse(init.body) : {};
        posts.push({ url, body });
        if (updateStatus === 409 && !body.force) {
          return {
            ok: false, status: 409,
            json: async () => ({ detail: { reason_code: 'stale_install' } }), text: async () => '',
          };
        }
        return { ok: true, status: 200, json: async () => ({ updated: true, was_no_op: false }), text: async () => '' };
      }
      return base(input, init);
    };
    return posts;
  }

  it('shows install/update buttons in dev and hides them in prod', async () => {
    const dev = await bootDev();
    await dev.window.loadWiki();
    await dev.window.loadWikiDetail('skills', 'alpha');
    expect(dev.window.document.getElementById('wiki-install-btn')).not.toBeNull();
    expect(dev.window.document.getElementById('wiki-update-btn')).not.toBeNull();

    const prod = await boot({
      '/api/wiki': WIKI_LIST,
      '/api/wiki/skills/alpha/diff': ALPHA_DIFF_NONE,
      '/api/wiki/skills/alpha/lint': ALPHA_LINT_OK,
    });
    await prod.window.loadWiki();
    await prod.window.loadWikiDetail('skills', 'alpha');
    expect(prod.window.document.getElementById('wiki-install-btn')).toBeNull();
  });

  it('project <select> lists the roster and defaults to the active scope', async () => {
    const dom = await bootDev('p-1', [
      { scope_id: '', label: 'Server CWD' },
      { scope_id: 'p-1', label: 'Proj One' },
    ]);
    await dom.window.loadWiki();
    await dom.window.loadWikiDetail('skills', 'alpha');
    const sel = dom.window.document.getElementById('wiki-install-project');
    expect(sel).not.toBeNull();
    expect(Array.from(sel.options).map((o) => o.value)).toEqual(['', 'p-1']);
    expect(sel.value).toBe('p-1');
  });

  it('install POSTs to the install route with the selected scope_id', async () => {
    const dom = await bootDev('p-1', [
      { scope_id: '', label: 'Server CWD' },
      { scope_id: 'p-1', label: 'Proj One' },
    ]);
    const posts = recordingCtxFetch(dom.window);
    await dom.window.loadWiki();
    await dom.window.loadWikiDetail('skills', 'alpha');
    await dom.window._onWikiInstallOrUpdate('install');
    expect(posts.length).toBe(1);
    expect(posts[0].url).toBe('/api/context/skills/alpha/install?scope_id=p-1');
    expect(posts[0].body).toBeNull(); // install carries no body
  });

  it('update POSTs force:false to the update route', async () => {
    const dom = await bootDev();
    const posts = recordingCtxFetch(dom.window);
    await dom.window.loadWiki();
    await dom.window.loadWikiDetail('skills', 'alpha');
    await dom.window._onWikiInstallOrUpdate('update');
    expect(posts.length).toBe(1);
    expect(posts[0].url.split('?')[0]).toBe('/api/context/skills/alpha/update');
    expect(posts[0].body).toEqual({ force: false });
  });

  it('Server-CWD selection omits scope_id from the URL', async () => {
    const dom = await bootDev('', [{ scope_id: '', label: 'Server CWD' }]);
    const posts = recordingCtxFetch(dom.window);
    await dom.window.loadWiki();
    await dom.window.loadWikiDetail('skills', 'alpha');
    await dom.window._onWikiInstallOrUpdate('install');
    expect(posts[0].url).toBe('/api/context/skills/alpha/install');
  });

  it('a dirty update confirms then re-POSTs with force:true', async () => {
    const dom = await bootDev();
    dom.window.showConfirm = async () => true; // accept the overwrite
    const posts = recordingCtxFetch(dom.window, { updateStatus: 409 });
    await dom.window.loadWiki();
    await dom.window.loadWikiDetail('skills', 'alpha');
    await dom.window._onWikiInstallOrUpdate('update');
    expect(posts.map((p) => p.body.force)).toEqual([false, true]);
  });

  it('a declined dirty-update confirm sends no force POST', async () => {
    const dom = await bootDev();
    dom.window.showConfirm = async () => false; // cancel the overwrite
    const posts = recordingCtxFetch(dom.window, { updateStatus: 409 });
    await dom.window.loadWiki();
    await dom.window.loadWikiDetail('skills', 'alpha');
    await dom.window._onWikiInstallOrUpdate('update');
    expect(posts.map((p) => p.body.force)).toEqual([false]); // initial only, no retry
  });
});

describe('wiki.js override editor (ADR-0027 Editor-A, dev tier)', () => {
  const OVERRIDE_EXISTS = { vendor: 'claude', content: '# orig\n', mtime_ns: '111', exists: true };

  // Serves diff/lint + the override GET and records PUTs. The override GET is
  // dev-tier and only fetched by _loadWikiVendorView when in dev mode.
  function recordingEditFetch(window, { override = OVERRIDE_EXISTS } = {}) {
    const puts = [];
    const base = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const p = (url || '').split('?')[0];
      const method = (init.method || 'GET').toUpperCase();
      if (p === '/api/wiki/skills/alpha/diff') {
        return { ok: true, status: 200, json: async () => ALPHA_DIFF_EXISTS, text: async () => '' };
      }
      if (p === '/api/wiki/skills/alpha/lint') {
        return { ok: true, status: 200, json: async () => ALPHA_LINT_OK, text: async () => '' };
      }
      if (method === 'GET' && p === '/api/wiki/skills/alpha/override') {
        return { ok: true, status: 200, json: async () => override, text: async () => '' };
      }
      if (method === 'PUT' && p === '/api/wiki/skills/alpha/override') {
        puts.push({ headers: init.headers || {}, body: JSON.parse(init.body) });
        return {
          ok: true, status: 200,
          json: async () => ({ vendor: 'claude', mtime_ns: '222', wiki_dirty: true, privacy_warning: 0 }),
          text: async () => '',
        };
      }
      return base(input, init);
    };
    return puts;
  }

  it('renders the read pane + Edit toggle in dev when the override exists', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    recordingEditFetch(window);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    const pre = window.document.querySelector('.wiki-override-pre');
    expect(pre).not.toBeNull();
    expect(pre.textContent).toContain('# orig');
    expect(window.document.getElementById('wiki-override-edit-btn')).not.toBeNull();
  });

  it('hides the editor in prod (no dev-mode class, no override fetch)', async () => {
    const { window } = await boot({
      '/api/wiki': WIKI_LIST,
      '/api/wiki/skills/alpha/diff': ALPHA_DIFF_EXISTS,
      '/api/wiki/skills/alpha/lint': ALPHA_LINT_OK,
    });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    expect(window.document.querySelector('.wiki-override-editor')).toBeNull();
  });

  it('does not render the editor for a not-yet-seeded override', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    recordingEditFetch(window, {
      override: { vendor: 'claude', content: '', mtime_ns: '0', exists: false },
    });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    expect(window.document.querySelector('.wiki-override-editor')).toBeNull();
  });

  it('Save PUTs {vendor, content, mtime_ns} with the CSRF header + repaints dirty', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    window.ensureCsrfToken = async () => 'tok-123';
    const puts = recordingEditFetch(window);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-override-edit-btn').dispatchEvent(new window.Event('click'));
    const ta = window.document.getElementById('wiki-override-content');
    expect(ta.value).toBe('# orig\n');     // seeded from the read content
    expect(ta.dataset.mtimeNs).toBe('111'); // token from the GET
    ta.value = '# edited\n';
    await window._onWikiOverrideSave();
    expect(puts.length).toBe(1);
    expect(puts[0].body).toEqual({ vendor: 'claude', content: '# edited\n', mtime_ns: '111', force: false });
    expect(puts[0].headers['X-Memtomem-CSRF']).toBe('tok-123');
    const head = window.document.getElementById('wiki-head');
    expect(head.textContent).toContain(window.t('settings.ctx.wiki_dirty'));
  });

  it('a 409 shows the conflict banner with the on-disk bytes', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    let sawPut = false;
    const base = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const p = (url || '').split('?')[0];
      const method = (init.method || 'GET').toUpperCase();
      if (p === '/api/wiki/skills/alpha/diff') return { ok: true, status: 200, json: async () => ALPHA_DIFF_EXISTS, text: async () => '' };
      if (p === '/api/wiki/skills/alpha/lint') return { ok: true, status: 200, json: async () => ALPHA_LINT_OK, text: async () => '' };
      if (method === 'GET' && p === '/api/wiki/skills/alpha/override') {
        // After the conflict, the handler re-fetches the fresh on-disk bytes.
        return { ok: true, status: 200, json: async () => ({
          vendor: 'claude', content: sawPut ? '# theirs\n' : '# orig\n',
          mtime_ns: sawPut ? '999' : '111', exists: true,
        }), text: async () => '' };
      }
      if (method === 'PUT' && p === '/api/wiki/skills/alpha/override') {
        sawPut = true;
        return { ok: false, status: 409, json: async () => ({
          reason_code: 'stale_mtime', mtime_ns: '999', error_kind: 'conflict',
        }), text: async () => '' };
      }
      return base(input, init);
    };
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-override-edit-btn').dispatchEvent(new window.Event('click'));
    window.document.getElementById('wiki-override-content').value = '# mine\n';
    await window._onWikiOverrideSave();
    const banner = window.document.getElementById('wiki-conflict-banner');
    expect(banner.hidden).toBe(false);
    expect(banner.textContent).toContain('# theirs');
    expect(window.document.getElementById('wiki-conflict-force-btn')).not.toBeNull();
    expect(window.document.getElementById('wiki-conflict-reload-btn')).not.toBeNull();
  });

  it('Force save re-PUTs the draft with force:true and the fresh token', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    const puts = recordingEditFetch(window);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    // Call the writer directly (awaitable) — the force button wires to this.
    await window._saveWikiOverride('skills', 'alpha', 'claude', '# mine\n', '999', true);
    expect(puts.length).toBe(1);
    expect(puts[0].body).toEqual({ vendor: 'claude', content: '# mine\n', mtime_ns: '999', force: true });
  });

  it('preserves the in-progress draft across a langchange repaint', async () => {
    // Regression (Codex review): langchange rebuilds the whole detail via
    // _renderWikiDetail, wiping the textarea BEFORE _renderWikiVendorView runs —
    // the draft must be stashed before that wipe, not reverted to the saved bytes.
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    recordingEditFetch(window);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-override-edit-btn').dispatchEvent(new window.Event('click'));
    window.document.getElementById('wiki-override-content').value = '# unsaved\n';
    await window.I18N.setLang('ko');
    window.dispatchEvent(new window.Event('langchange'));
    const ta = window.document.getElementById('wiki-override-content');
    expect(ta).not.toBeNull();
    expect(ta.value).toBe('# unsaved\n'); // draft survived, not reverted to '# orig'
    await window.I18N.setLang('en');
  });
});

describe('wiki.js canonical editor (ADR-0027 Editor-B, dev tier)', () => {
  const CANON = { content: '# canon\n', mtime_ns: '111' };
  const OVERRIDE_NONE = { vendor: 'claude', content: '', mtime_ns: '0', exists: false };

  // Serves the canonical GET (artifact-level) + diff/lint + a not-seeded override
  // (so the per-vendor override editor stays out of the way) and records canonical
  // PUTs. The post-save reload re-fetches canonical + the vendor view, so all must
  // resolve. `putResponse` lets a test return a 409 / 400 instead of success.
  function recordingCanonFetch(window, { canon = CANON, putResponse } = {}) {
    const puts = [];
    const base = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const p = (url || '').split('?')[0];
      const method = (init.method || 'GET').toUpperCase();
      if (p === '/api/wiki/skills/alpha/diff') {
        return { ok: true, status: 200, json: async () => ALPHA_DIFF_EXISTS, text: async () => '' };
      }
      if (p === '/api/wiki/skills/alpha/lint') {
        return { ok: true, status: 200, json: async () => ALPHA_LINT_OK, text: async () => '' };
      }
      if (method === 'GET' && p === '/api/wiki/skills/alpha/override') {
        return { ok: true, status: 200, json: async () => OVERRIDE_NONE, text: async () => '' };
      }
      if (method === 'GET' && p === '/api/wiki/skills/alpha/canonical') {
        return { ok: true, status: 200, json: async () => canon, text: async () => '' };
      }
      if (method === 'PUT' && p === '/api/wiki/skills/alpha/canonical') {
        puts.push({ headers: init.headers || {}, body: JSON.parse(init.body) });
        if (putResponse) return putResponse;
        return {
          ok: true, status: 200,
          json: async () => ({ mtime_ns: '222', wiki_dirty: true, privacy_warning: 0 }),
          text: async () => '',
        };
      }
      return base(input, init);
    };
    return puts;
  }

  it('renders the canonical read pane + Edit toggle in dev', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    recordingCanonFetch(window);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    const editor = window.document.querySelector('.wiki-canonical-editor');
    expect(editor).not.toBeNull();
    expect(editor.textContent).toContain('# canon');
    expect(window.document.getElementById('wiki-canonical-edit-btn')).not.toBeNull();
  });

  it('hides the canonical editor in prod (no canonical fetch)', async () => {
    const { window } = await boot({
      '/api/wiki': WIKI_LIST,
      '/api/wiki/skills/alpha/diff': ALPHA_DIFF_EXISTS,
      '/api/wiki/skills/alpha/lint': ALPHA_LINT_OK,
    });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    expect(window.document.querySelector('.wiki-canonical-editor')).toBeNull();
  });

  it('Save PUTs {content, mtime_ns, force} with the CSRF header + repaints dirty', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    window.ensureCsrfToken = async () => 'tok-xyz';
    const puts = recordingCanonFetch(window);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-canonical-edit-btn').dispatchEvent(new window.Event('click'));
    const ta = window.document.getElementById('wiki-canonical-content');
    expect(ta.value).toBe('# canon\n');     // seeded from the canonical GET
    expect(ta.dataset.mtimeNs).toBe('111'); // token from the GET
    ta.value = '# edited canon\n';
    await window._onWikiCanonicalSave();
    expect(puts.length).toBe(1);
    expect(puts[0].body).toEqual({ content: '# edited canon\n', mtime_ns: '111', force: false });
    expect(puts[0].headers['X-Memtomem-CSRF']).toBe('tok-xyz');
    const head = window.document.getElementById('wiki-head');
    expect(head.textContent).toContain(window.t('settings.ctx.wiki_dirty'));
  });

  it('a 409 shows the conflict banner with the on-disk bytes', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    let sawPut = false;
    const base = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const p = (url || '').split('?')[0];
      const method = (init.method || 'GET').toUpperCase();
      if (p === '/api/wiki/skills/alpha/diff') return { ok: true, status: 200, json: async () => ALPHA_DIFF_EXISTS, text: async () => '' };
      if (p === '/api/wiki/skills/alpha/lint') return { ok: true, status: 200, json: async () => ALPHA_LINT_OK, text: async () => '' };
      if (method === 'GET' && p === '/api/wiki/skills/alpha/override') return { ok: true, status: 200, json: async () => OVERRIDE_NONE, text: async () => '' };
      if (method === 'GET' && p === '/api/wiki/skills/alpha/canonical') {
        // After the conflict, the handler re-fetches the fresh on-disk bytes.
        return { ok: true, status: 200, json: async () => ({
          content: sawPut ? '# theirs\n' : '# canon\n', mtime_ns: sawPut ? '999' : '111',
        }), text: async () => '' };
      }
      if (method === 'PUT' && p === '/api/wiki/skills/alpha/canonical') {
        sawPut = true;
        return { ok: false, status: 409, json: async () => ({
          reason_code: 'stale_mtime', mtime_ns: '999', error_kind: 'conflict',
        }), text: async () => '' };
      }
      return base(input, init);
    };
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-canonical-edit-btn').dispatchEvent(new window.Event('click'));
    window.document.getElementById('wiki-canonical-content').value = '# mine\n';
    await window._onWikiCanonicalSave();
    const banner = window.document.getElementById('wiki-canonical-conflict-banner');
    expect(banner.hidden).toBe(false);
    expect(banner.textContent).toContain('# theirs');
    expect(window.document.getElementById('wiki-canonical-conflict-force-btn')).not.toBeNull();
    expect(window.document.getElementById('wiki-canonical-conflict-reload-btn')).not.toBeNull();
  });

  it('a 400 parse failure toasts and writes nothing (editor stays open)', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    const toasts = [];
    window.showToast = (msg, kind) => { toasts.push({ msg, kind }); };
    recordingCanonFetch(window, {
      putResponse: {
        ok: false, status: 400,
        json: async () => ({ detail: { message: 'missing YAML frontmatter: agents/x/agent.md', reason_code: 'canonical_unparseable' } }),
        text: async () => '',
      },
    });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-canonical-edit-btn').dispatchEvent(new window.Event('click'));
    window.document.getElementById('wiki-canonical-content').value = 'bad\n';
    await window._onWikiCanonicalSave();
    // The parse-failed toast fired and the textarea is still present (not closed).
    expect(toasts.some((x) => x.kind === 'error')).toBe(true);
    expect(window.document.getElementById('wiki-canonical-content')).not.toBeNull();
    // The dirty badge was NOT repainted (nothing was saved).
    const head = window.document.getElementById('wiki-head');
    expect(head.textContent).not.toContain(window.t('settings.ctx.wiki_dirty'));
  });

  it('preserves the in-progress canonical draft across a langchange repaint', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    window.document.body.classList.add('dev-mode');
    recordingCanonFetch(window);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-canonical-edit-btn').dispatchEvent(new window.Event('click'));
    window.document.getElementById('wiki-canonical-content').value = '# unsaved canon\n';
    await window.I18N.setLang('ko');
    window.dispatchEvent(new window.Event('langchange'));
    const ta = window.document.getElementById('wiki-canonical-content');
    expect(ta).not.toBeNull();
    expect(ta.value).toBe('# unsaved canon\n'); // draft survived the detail rebuild
    await window.I18N.setLang('en');
  });
});

describe('wiki.js commit affordance (ADR-0027 §3, dev tier)', () => {
  const CANON = { content: '# canon\n', mtime_ns: '111' };
  const OVERRIDE_NONE = { vendor: 'claude', content: '', mtime_ns: '0', exists: false };

  // Serves diff/lint + the canonical GET/PUT (Save) and records the commit POST.
  function recordingCommitFetch(window, { commitResponse } = {}) {
    const posts = [];
    const base = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const p = (url || '').split('?')[0];
      const method = (init.method || 'GET').toUpperCase();
      if (p === '/api/wiki/skills/alpha/diff') return { ok: true, status: 200, json: async () => ALPHA_DIFF_EXISTS, text: async () => '' };
      if (p === '/api/wiki/skills/alpha/lint') return { ok: true, status: 200, json: async () => ALPHA_LINT_OK, text: async () => '' };
      if (method === 'GET' && p === '/api/wiki/skills/alpha/override') return { ok: true, status: 200, json: async () => OVERRIDE_NONE, text: async () => '' };
      if (method === 'GET' && p === '/api/wiki/skills/alpha/canonical') return { ok: true, status: 200, json: async () => CANON, text: async () => '' };
      if (method === 'PUT' && p === '/api/wiki/skills/alpha/canonical') {
        return { ok: true, status: 200, json: async () => ({ mtime_ns: '222', wiki_dirty: true, privacy_warning: 0 }), text: async () => '' };
      }
      if (method === 'POST' && p === '/api/wiki/skills/alpha/commit') {
        posts.push({ headers: init.headers || {}, body: JSON.parse(init.body) });
        if (commitResponse) return commitResponse;
        return { ok: true, status: 200, json: async () => ({ committed: true, wiki_head: 'b'.repeat(40), wiki_dirty: false, privacy_warning: 0 }), text: async () => '' };
      }
      return base(input, init);
    };
    return posts;
  }

  // Open skills/alpha in dev, edit + Save the canonical so a pending commit
  // target (and the HEAD-row Commit button) appear.
  async function bootSavedCanonical(window, opts) {
    window.document.body.classList.add('dev-mode');
    window.ensureCsrfToken = async () => 'tok-xyz';
    const posts = recordingCommitFetch(window, opts);
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    window.document.getElementById('wiki-canonical-edit-btn').dispatchEvent(new window.Event('click'));
    window.document.getElementById('wiki-canonical-content').value = '# edited canon\n';
    await window._onWikiCanonicalSave();
    return posts;
  }

  it('Commit button appears after Save, POSTs the resolved target + CSRF, then clears', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    const posts = await bootSavedCanonical(window);
    const btn = window.document.getElementById('wiki-commit-btn');
    expect(btn).not.toBeNull(); // a pending target → Commit affordance shows
    btn.dispatchEvent(new window.Event('click'));
    expect(window.document.getElementById('wiki-commit-modal').hidden).toBe(false);
    await window._doWikiCommit(false);
    expect(posts.length).toBe(1);
    expect(posts[0].headers['X-Memtomem-CSRF']).toBe('tok-xyz');
    expect(posts[0].body.expected_head).toBe('a'.repeat(40));
    expect(posts[0].body.force).toBe(false);
    expect(posts[0].body.targets).toEqual([{ kind: 'canonical', mtime_ns: '222' }]);
    const head = window.document.getElementById('wiki-head');
    expect(head.textContent).toContain('bbbbbbbbbbbb'); // HEAD advanced
    expect(head.textContent).not.toContain(window.t('settings.ctx.wiki_dirty')); // badge cleared
    expect(window.document.getElementById('wiki-commit-btn')).toBeNull(); // pending cleared
    expect(window.document.getElementById('wiki-commit-modal').hidden).toBe(true); // modal closed
  });

  it('a no-op commit (committed:false) clears pending without erroring', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    const posts = await bootSavedCanonical(window, {
      commitResponse: {
        ok: true, status: 200,
        json: async () => ({ committed: false, reason_code: 'nothing_to_commit', wiki_head: 'a'.repeat(40), wiki_dirty: false }),
        text: async () => '',
      },
    });
    await window._openWikiCommitModal();
    await window._doWikiCommit(false);
    expect(posts.length).toBe(1);
    expect(window.document.getElementById('wiki-commit-btn')).toBeNull(); // pending cleared
  });

  it('a 409 stale_head refreshes HEAD, closes the modal, and CLEARS pending', async () => {
    const { window } = await boot({ '/api/wiki': WIKI_LIST });
    const posts = await bootSavedCanonical(window, {
      commitResponse: {
        ok: false, status: 409,
        json: async () => ({ reason_code: 'stale_head', wiki_head: 'c'.repeat(40), error_kind: 'conflict' }),
        text: async () => '',
      },
    });
    await window._openWikiCommitModal();
    await window._doWikiCommit(false);
    expect(posts.length).toBe(1);
    expect(window.document.getElementById('wiki-head').textContent).toContain('cccccccccccc');
    // Pending is cleared so the tokens (captured against the OLD head) can't be
    // one-click re-committed against the NEW head — a fresh Save is required
    // (Codex M1). The Commit button disappears.
    expect(window.document.getElementById('wiki-commit-btn')).toBeNull();
    expect(window.document.getElementById('wiki-commit-modal').hidden).toBe(true);
  });

  it('never shows the Commit button in prod (no dev-mode class)', async () => {
    const { window } = await boot({
      '/api/wiki': WIKI_LIST,
      '/api/wiki/skills/alpha/diff': ALPHA_DIFF_EXISTS,
      '/api/wiki/skills/alpha/lint': ALPHA_LINT_OK,
    });
    await window.loadWiki();
    await window.loadWikiDetail('skills', 'alpha');
    expect(window.document.getElementById('wiki-commit-btn')).toBeNull();
  });
});

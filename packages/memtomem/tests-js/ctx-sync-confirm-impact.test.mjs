/* U4 (#1229): Sync / Sync All confirms carry a create-vs-overwrite impact
 * summary and a conditional overwrite warning instead of click-and-pray copy.
 *
 * The impact preview is a best-effort pre-confirm fetch of the type list
 * (per-type Sync) or the four lists + overview (Sync All) under a pinned
 * (project, tier). These tests capture the showConfirm options via a window
 * override — `showConfirm` is a global function declaration (app.js), so the
 * bare call inside context-gateway.js resolves through window.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 30) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

const LIST_MIXED = {
  skills: [
    {
      name: 'a',
      runtimes: [
        { runtime: 'claude_skills', status: 'missing target' },
        { runtime: 'codex_skills', status: 'out of sync' },
        { runtime: 'gemini_skills', status: 'in sync' },
      ],
    },
    {
      name: 'b',
      runtimes: [
        { runtime: 'claude_skills', status: 'out of sync' },
        // Not sync writes — must not count:
        { runtime: 'codex_skills', status: 'missing canonical' },
        { runtime: 'kimi_skills', status: 'parse error' },
      ],
    },
  ],
};

const LIST_CLEAN = {
  skills: [
    { name: 'a', runtimes: [{ runtime: 'claude_skills', status: 'in sync' }] },
  ],
};

async function bootTypeSync({ listBody, listOk = true }) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  let captured = null;
  window.showConfirm = async (opts) => {
    captured = opts;
    return false; // never run the sync — we only inspect the dialog
  };
  window.ensureCsrfToken = async () => 'test-token';
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith('/api/context/skills')) {
      if (!listOk) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => listBody };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  // The handler reads the section's canonicalCount gate first.
  const section = window.document.getElementById('settings-ctx-skills');
  section.dataset.canonicalCount = '2';
  const btn = section.querySelector('.ctx-sync-btn');
  btn.click();
  await flush(window);
  return { window, captured: () => captured };
}

describe('per-type Sync confirm impact (U4)', () => {
  it('names create/overwrite counts and target runtimes; warns on overwrites', async () => {
    const { captured } = await bootTypeSync({ listBody: LIST_MIXED });
    const opts = captured();
    expect(opts).toBeTruthy();
    // 1 missing target -> create, 2 out of sync -> overwrite.
    expect(opts.message).toContain('create 1');
    expect(opts.message).toContain('overwrite 2');
    // Runtime display names, sorted; gemini maps to Antigravity and only
    // write-target runtimes appear (kimi only had parse error -> absent).
    expect(opts.message).toContain('claude, codex');
    expect(opts.message).not.toContain('Antigravity');
    expect(opts.warningText).toContain('2');
  });

  it('all-in-sync payload: no warning, says already in sync (negative pin)', async () => {
    const { captured } = await bootTypeSync({ listBody: LIST_CLEAN });
    const opts = captured();
    expect(opts).toBeTruthy();
    expect(opts.message).toContain('already in sync');
    expect(opts.warningText || '').toBe('');
  });

  it('falls back to count-only copy when the impact fetch fails — confirm still opens', async () => {
    const { captured } = await bootTypeSync({ listBody: null, listOk: false });
    const opts = captured();
    expect(opts).toBeTruthy();
    // Base copy survives; no impact sentence, no warning.
    expect(opts.message).not.toContain('overwrite');
    expect(opts.warningText || '').toBe('');
  });
});

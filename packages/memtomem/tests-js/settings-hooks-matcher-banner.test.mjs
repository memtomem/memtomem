/* Malformed-matcher validation banner in the hooks panel (#1983, #1986 review).
 *
 * GET /api/settings-sync carries ``matcher_warnings`` (canonical rules the
 * sync drops, server-worded) and ``target_hooks.malformed`` (target rules the
 * diff skips). Before this banner, a canonical whose only rule was malformed
 * rendered as a plain "no hooks" with Sync Now disabled — no path to the
 * warning a POST would have shown. These tests pin:
 *
 *  - canonical warnings render verbatim, target rows render with the
 *    owned/user consequence text,
 *  - the banner clears on a clean re-fetch and on a failed scoped refetch,
 *  - payloads without the fields keep the banner hidden,
 *  - server-echoed values are HTML-escaped.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const BASE_SYNC = {
  status: 'no_hooks',
  target_scope: 'project_shared',
  target_path: '/proj/.claude/settings.json',
  hooks: { pending: [], conflicts: [], synced: [] },
};

async function bootHooksPanel(syncPayload) {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'settings-hooks-watchdog.js'],
    apiResponses: { '/api/settings-sync': syncPayload },
  });
  const { window } = dom;
  await window.loadHooksSync();
  return window;
}

describe('hooks malformed-matcher banner (#1983)', () => {
  it('renders canonical warnings and target malformed rows', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      matcher_warnings: [
        "Hook rule under 'PostToolUse' has a non-string matcher (list) and was dropped.",
      ],
      target_hooks: {
        configured: [],
        target_only: [],
        malformed: [
          { event: 'PreToolUse', matcher_type: 'list', rule_index: 0, owned: true },
          { event: 'Stop', matcher_type: 'dict', rule_index: 1, owned: false },
        ],
      },
    });
    const el = window.document.getElementById('hooks-matcher-banner');
    expect(el).toBeTruthy();
    // Container styling (and the [hidden] display override) is class-scoped.
    expect(el.classList.contains('hooks-matcher-banner')).toBe(true);
    expect(el.hidden).toBe(false);
    const rows = el.querySelectorAll('.hooks-matcher-banner-row');
    expect(rows.length).toBe(3);
    expect(rows[0].textContent).toContain('non-string matcher (list)');
    expect(rows[1].textContent).toContain('PreToolUse');
    expect(rows[1].textContent).toContain('next sync removes');
    expect(rows[2].textContent).toContain('Stop');
    expect(rows[2].textContent).toContain('never fire');
  });

  it('clears after a clean re-fetch', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      matcher_warnings: ['dropped'],
    });
    const el = window.document.getElementById('hooks-matcher-banner');
    expect(el.hidden).toBe(false);

    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url;
      if (url && url.split('?')[0] === '/api/settings-sync') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...BASE_SYNC, matcher_warnings: [] }),
          text: async () => '',
        };
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
    };
    await window.loadHooksSync();
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });

  it('clears when a scoped refetch fails (no stale warning)', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      matcher_warnings: ['dropped'],
    });
    const el = window.document.getElementById('hooks-matcher-banner');
    expect(el.hidden).toBe(false);

    window.fetch = async () => ({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'boom' }),
      text: async () => '{}',
    });
    await window.loadHooksSync();
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });

  it('re-renders in the new locale on langchange (EN→KO)', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      target_hooks: {
        configured: [],
        target_only: [],
        malformed: [{ event: 'PreToolUse', matcher_type: 'list', rule_index: 0, owned: true }],
      },
    });
    const el = window.document.getElementById('hooks-matcher-banner');
    expect(el.hidden).toBe(false);
    expect(el.textContent).toContain('next sync removes');

    await window.I18N.setLang('ko');
    expect(el.hidden).toBe(false);
    expect(el.textContent).toContain('memtomem 관리 규칙이 제거됩니다');
  });

  it('langchange does not resurrect a cleared banner from the stale cache', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      matcher_warnings: ['dropped'],
    });
    const el = window.document.getElementById('hooks-matcher-banner');
    expect(el.hidden).toBe(false);

    window.fetch = async () => ({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'boom' }),
      text: async () => '{}',
    });
    await window.loadHooksSync();
    expect(el.hidden).toBe(true);

    // _hooksLastSyncData still holds the pre-failure payload; the langchange
    // re-render must not repaint from it while the banner is cleared.
    window.dispatchEvent(new window.CustomEvent('langchange', { detail: { lang: 'ko' } }));
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });

  it('treats a payload without the fields as clean', async () => {
    const window = await bootHooksPanel({ ...BASE_SYNC });
    const el = window.document.getElementById('hooks-matcher-banner');
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });

  it('escapes HTML in server-echoed values', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      matcher_warnings: ['<script>boom</script>'],
      target_hooks: {
        configured: [],
        target_only: [],
        malformed: [
          { event: '<img src=x onerror=alert(1)>', matcher_type: 'list', rule_index: 0, owned: false },
        ],
      },
    });
    const el = window.document.getElementById('hooks-matcher-banner');
    expect(el.hidden).toBe(false);
    expect(el.querySelector('script')).toBeNull();
    expect(el.querySelector('img')).toBeNull();
    expect(el.innerHTML).toContain('&lt;script&gt;');
  });
});

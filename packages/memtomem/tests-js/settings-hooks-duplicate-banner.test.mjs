/* Duplicate-tier warning banner in the hooks panel (#1247 id 32).
 *
 * GET /api/settings-sync has carried ``duplicate_tier_warnings`` since the
 * settings-doctor shipped (ADR-0010 §4), but no web surface consumed it —
 * the ADR-0010 §3 "banner when duplicate-tier memtomem-managed hooks are
 * detected" was missing. These tests pin the banner render:
 *
 *  - rows render from the GET payload only (the POST response lacks
 *    ``target_scope``, the ``--to=`` hint source; every POST success path
 *    re-runs loadHooksSync, so the GET repaint covers apply — design-gate
 *    fold, codex-20260611-234932),
 *  - the migrate hint reflects the payload's ``target_scope`` (non-default
 *    scopes get the right ``--to=``),
 *  - a clean re-fetch clears the banner,
 *  - tier/path values are HTML-escaped.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const BASE_SYNC = {
  status: 'in_sync',
  target_scope: 'project_shared',
  target_path: '/proj/.claude/settings.json',
  hooks: { pending: [], conflicts: [], synced: [] },
};

function dup(tier, path, entryCount) {
  return {
    tier,
    path,
    entries: Array.from({ length: entryCount }, (_, i) => ({
      event: 'Stop',
      matcher: '',
      command_preview: `echo hook-${i}`,
    })),
  };
}

async function bootHooksPanel(syncPayload) {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'settings-hooks-watchdog.js'],
    apiResponses: { '/api/settings-sync': syncPayload },
  });
  const { window } = dom;
  await window.loadHooksSync();
  return window;
}

describe('hooks duplicate-tier banner (#1247 id 32)', () => {
  it('renders one row per duplicate tier with count, path, and migrate hint', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      duplicate_tier_warnings: [
        dup('user', '/home/u/.claude/settings.json', 2),
        dup('project_local', '/proj/.claude/settings.local.json', 1),
      ],
    });
    const el = window.document.getElementById('hooks-duplicate-banner');
    expect(el).toBeTruthy();
    // The container styling (and the [hidden] display override) is
    // class-scoped — an id-only element would render unstyled (Codex
    // impl-gate round-2 catch).
    expect(el.classList.contains('hooks-duplicate-banner')).toBe(true);
    expect(el.hidden).toBe(false);
    const rows = el.querySelectorAll('.hooks-duplicate-banner-row');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('2');
    expect(rows[0].textContent).toContain('user');
    expect(rows[0].textContent).toContain('/home/u/.claude/settings.json');
    expect(rows[0].textContent).toContain(
      'mm context settings-migrate --from=user --to=project_shared',
    );
    expect(rows[1].textContent).toContain(
      'mm context settings-migrate --from=project_local --to=project_shared',
    );
  });

  it('builds the --to= hint from the payload target_scope (non-default scope)', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      target_scope: 'user',
      duplicate_tier_warnings: [dup('project_shared', '/proj/.claude/settings.json', 1)],
    });
    const el = window.document.getElementById('hooks-duplicate-banner');
    expect(el.textContent).toContain(
      'mm context settings-migrate --from=project_shared --to=user',
    );
  });

  it('stays hidden with no duplicates and clears after a clean re-fetch', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      duplicate_tier_warnings: [dup('user', '/home/u/.claude/settings.json', 1)],
    });
    const el = window.document.getElementById('hooks-duplicate-banner');
    expect(el.hidden).toBe(false);

    // Post-apply repaint path: every POST success handler re-runs
    // loadHooksSync(); a now-clean GET must clear the banner rather than
    // leave a stale warning behind.
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url;
      if (url && url.split('?')[0] === '/api/settings-sync') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...BASE_SYNC, duplicate_tier_warnings: [] }),
          text: async () => '',
        };
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
    };
    await window.loadHooksSync();
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });

  it('clears the banner when a scoped refetch fails (no stale --to hint)', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      duplicate_tier_warnings: [dup('user', '/home/u/.claude/settings.json', 1)],
    });
    const el = window.document.getElementById('hooks-duplicate-banner');
    expect(el.hidden).toBe(false);

    // Tier/project switch whose reload fails: the banner must be cleared at
    // reload start, not left showing the previous scope's migrate hint
    // (Codex impl-gate catch).
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

  it('langchange does not resurrect a cleared banner from the stale cache', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      duplicate_tier_warnings: [dup('user', '/home/u/.claude/settings.json', 1)],
    });
    const el = window.document.getElementById('hooks-duplicate-banner');
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

  it('treats a payload without the field as no duplicates', async () => {
    const window = await bootHooksPanel({ ...BASE_SYNC });
    const el = window.document.getElementById('hooks-duplicate-banner');
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });

  it('escapes HTML in tier and path values', async () => {
    const window = await bootHooksPanel({
      ...BASE_SYNC,
      duplicate_tier_warnings: [
        dup('<img src=x onerror=alert(1)>', '/tmp/<script>boom</script>.json', 1),
      ],
    });
    const el = window.document.getElementById('hooks-duplicate-banner');
    expect(el.hidden).toBe(false);
    expect(el.querySelector('img')).toBeNull();
    expect(el.querySelector('script')).toBeNull();
    expect(el.innerHTML).toContain('&lt;script&gt;');
  });
});

/* Three-way response discrimination in `_hooksPostRuleAction` (#1229).
 *
 * Stale-write aborts from /api/context/settings/rules/{delete,promote} and
 * /resolve arrive as HTTP 409 with a status-keyed envelope
 * ({status: 'aborted', reason, ...}) — the helper must pass them through to
 * the callers' `result.status` handling instead of throwing, while the OTHER
 * 409 on the very same endpoints (the sync-eligibility write-guard, whose
 * body is {detail: {reason_code, ...}} with no `status` key) must keep
 * throwing so its localized detail mapping renders. HTTP 200 ok envelopes
 * pass through unchanged.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const ENTRY = { event: 'Stop', matcher: '', rule_index: 0, rule_hash: 'abc' };

function fetchResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

async function bootWithRuleActionResponse(status, body) {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'settings-hooks-watchdog.js'],
  });
  const { window } = dom;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url;
    if (url && url.includes('/api/session')) {
      return fetchResponse(200, { csrf: 'test-token' });
    }
    if (url && url.includes('/api/context/settings/rules/')) {
      return fetchResponse(status, body);
    }
    return fetchResponse(200, {});
  };
  return window;
}

describe('_hooksPostRuleAction stale-write 409 discrimination', () => {
  it('passes a status-keyed 409 abort envelope through to the caller', async () => {
    const window = await bootWithRuleActionResponse(409, {
      status: 'aborted',
      reason: 'Target rule changed. Refresh and retry.',
      target_mtime_ns: '1',
      canonical_mtime_ns: '2',
    });
    const result = await window._hooksPostRuleAction('delete', ENTRY, false);
    expect(result.status).toBe('aborted');
    expect(result.reason).toContain('Refresh and retry');
    // The two-key freshness names are load-bearing for Promote All's token
    // refresh — they must survive the pass-through untouched.
    expect(result.target_mtime_ns).toBe('1');
    expect(result.canonical_mtime_ns).toBe('2');
  });

  it('still throws on the sync-eligibility write-guard 409 (detail body, no status key)', async () => {
    const window = await bootWithRuleActionResponse(409, {
      detail: { reason_code: 'sync_paused', message: 'Sync is paused.' },
    });
    await expect(window._hooksPostRuleAction('delete', ENTRY, false)).rejects.toThrow();
  });

  it('throws on non-409 failures', async () => {
    const window = await bootWithRuleActionResponse(503, {
      detail: 'Delete timed out — another sync may be in progress',
    });
    await expect(window._hooksPostRuleAction('delete', ENTRY, false)).rejects.toThrow(
      /timed out/,
    );
  });

  it('returns the body on HTTP 200 ok envelopes', async () => {
    const window = await bootWithRuleActionResponse(200, {
      status: 'ok',
      reason: 'Rule deleted from target',
      target_mtime_ns: '3',
      canonical_mtime_ns: '4',
    });
    const result = await window._hooksPostRuleAction('delete', ENTRY, false);
    expect(result.status).toBe('ok');
  });
});

/* Localized sync / Sync-All privacy-block toast (#1409).
 *
 * The per-type sync privacy 422 keeps a deliberately path-free ENGLISH string
 * ``detail`` (#1385/#1387, issue-pinned) and now rides a top-level
 * ``reason_code: "privacy_blocked"`` sibling. Unlike the import surface — whose
 * ONLY 422 is the privacy block, so it keys on ``status`` (#1398 item 1) — the
 * sync route has other 422 causes (parse_error, strict_drop), so the privacy
 * block is disambiguated by ``reason_code``, not the status code.
 *
 * These pin:
 *   - ``_ctxSyncErrToast`` (per-row / per-section Sync button error path):
 *     ``reason_code === 'privacy_blocked'`` → the localized SYNC hint, every
 *     other error → the shared detail renderer.
 *   - ``_ctxErrorMessageFromResponse`` (Sync-All per-phase summary): the same
 *     ``reason_code`` → the same localized SYNC hint, so the aggregate toast
 *     localizes the block exactly like the per-row button does.
 *   - the SYNC hint is its own key, distinct from the IMPORT hint (the import
 *     wording — "Import to user library" — is wrong for the fan-out direction).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  await window.I18N.init();
  return window;
}

// A minimal ``Response`` double for ``_ctxErrorMessageFromResponse``: a
// content-type header lookup + an async JSON body.
const jsonResp = (body) => ({
  headers: { get: () => 'application/json' },
  json: async () => body,
  text: async () => '',
});

describe('localized sync privacy-block toast (#1409)', () => {
  it('_ctxSyncErrToast maps reason_code privacy_blocked → the localized SYNC hint', async () => {
    const window = await boot();
    const { I18N } = window;
    const out = window._ctxSyncErrToast({
      detail: 'Privacy scan blocked this sync: a secret was detected …',
      reason_code: 'privacy_blocked',
    });
    expect(out).toBe(I18N.t('settings.ctx.privacy_blocked_shared_sync_hint'));
    // NOT the raw English server detail (the whole point — locale-unaware).
    expect(out).not.toContain('Privacy scan blocked this sync');
  });

  it('_ctxSyncErrToast uses the SYNC hint, distinct from the IMPORT hint', async () => {
    const window = await boot();
    const { I18N } = window;
    const syncHint = I18N.t('settings.ctx.privacy_blocked_shared_sync_hint');
    const importHint = I18N.t('settings.ctx.privacy_blocked_shared_hint');
    // The two hints exist and differ — the import wording ("Import to user
    // library") would be wrong for the fan-out direction.
    expect(syncHint).not.toBe(importHint);
    expect(window._ctxSyncErrToast({ reason_code: 'privacy_blocked' })).toBe(syncHint);
  });

  it('_ctxSyncErrToast falls back to the detail renderer for non-privacy errors', async () => {
    const window = await boot();
    // A string detail is rendered verbatim (parse_error / generic 422).
    expect(window._ctxSyncErrToast({ detail: 'boom', reason_code: 'parse_error' }))
      .toBe('boom');
    // A strict_drop object detail still renders its message via _ctxErrDetail,
    // and is NOT hijacked by the privacy mapping.
    const out = window._ctxSyncErrToast({
      detail: { error_kind: 'validation', message: 'partial fan-out', reason_code: 'strict_drop' },
      reason_code: 'strict_drop',
    });
    expect(out).toContain('partial fan-out');
    expect(out).not.toBe(window.I18N.t('settings.ctx.privacy_blocked_shared_sync_hint'));
  });

  it('_ctxSyncErrToast tolerates an empty / missing error body', async () => {
    const window = await boot();
    const { I18N } = window;
    expect(window._ctxSyncErrToast({})).toBe(I18N.t('toast.request_failed'));
    expect(window._ctxSyncErrToast(null)).toBe(I18N.t('toast.request_failed'));
  });

  it('_ctxErrorMessageFromResponse localizes the Sync-All privacy phase', async () => {
    const window = await boot();
    const { I18N } = window;
    const reason = await window._ctxErrorMessageFromResponse(
      jsonResp({
        detail: 'Privacy scan blocked this sync: a secret was detected …',
        reason_code: 'privacy_blocked',
      }),
      'fallback',
    );
    expect(reason).toBe(I18N.t('settings.ctx.privacy_blocked_shared_sync_hint'));
  });

  it('_ctxErrorMessageFromResponse leaves other phase errors untouched', async () => {
    const window = await boot();
    // A non-privacy structured error still renders its server detail string.
    const reason = await window._ctxErrorMessageFromResponse(
      jsonResp({ detail: 'parse failed at line 3' }),
      'fallback',
    );
    expect(reason).toBe('parse failed at line 3');
  });
});

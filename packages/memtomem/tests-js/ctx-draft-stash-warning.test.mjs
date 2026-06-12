/* Regression guard for #1291 — ``_ctxStashDraft`` swallowed sessionStorage
 * failures (quota exceeded, private browsing) silently. The stash exists to
 * survive navigation, so a silent drop means a user's conflict-edit buffer
 * can vanish with no warning.
 *
 * The fix warns ONCE per page session ('warning' toast,
 * ``settings.ctx.draft_stash_failed``) on the first failure; later failures
 * stay quiet — a busted sessionStorage fails on every stash and a toast per
 * keystroke would be worse than the silence it replaces.
 *
 * Mutation that bites: reverting the catch to the bare comment removes the
 * toast (first assertion); dropping the ``_ctxStashWarnedOnce`` latch makes
 * the second stash add a second toast (exactly-once assertion).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('draft stash failure warning', () => {
  it('toasts once on the first failure, stays quiet after', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    await window.I18N.init();

    const proto = Object.getPrototypeOf(window.sessionStorage);
    const original = proto.setItem;
    proto.setItem = () => {
      throw new window.DOMException('quota exceeded', 'QuotaExceededError');
    };
    try {
      window._ctxStashDraft('m2m-ctx-conflict-buffer:test', 'draft body');
      let toasts = window.document.querySelectorAll('#toast-container .toast-warning');
      expect(toasts.length).toBe(1);
      const text = toasts[0].textContent || '';
      // Localized copy, not the raw key (cold-boot fallback echoes EN literal).
      expect(text).toContain('keep this tab open');
      expect(text).not.toContain('settings.ctx.draft_stash_failed');

      // Second failure in the same page session: no second toast.
      window._ctxStashDraft('m2m-ctx-conflict-buffer:test', 'draft body 2');
      toasts = window.document.querySelectorAll('#toast-container .toast-warning');
      expect(toasts.length).toBe(1);
    } finally {
      proto.setItem = original;
    }
  });

  it('stays silent when sessionStorage works', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    await window.I18N.init();

    window._ctxStashDraft('m2m-ctx-conflict-buffer:test', 'draft body');
    expect(window.sessionStorage.getItem('m2m-ctx-conflict-buffer:test')).toBe('draft body');
    expect(window.document.querySelectorAll('#toast-container .toast').length).toBe(0);
  });
});

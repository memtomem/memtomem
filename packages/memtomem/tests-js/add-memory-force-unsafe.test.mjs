/* Regression guard for the Add Memory privacy-warning → server submit
 * flow. After ``ca483c5`` wired the trust-boundary redaction guard
 * with ``force_unsafe: bool = False`` defaulting to block, the SPA
 * still posted ``/api/add`` without the bypass flag even when the user
 * confirmed the privacy warning — turning the confirm dialog into a
 * 403-trap. This file pins the three branches:
 *
 *   1. clean content → POST without ``force_unsafe``
 *   2. flagged content + user confirms → POST with ``force_unsafe: true``
 *   3. flagged content + user cancels → no POST
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('Add Memory — force_unsafe wired to privacy confirm', () => {
  let window;
  let document;
  let captured;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;

    // Capture every call to /api/add so each test can assert the body
    // shape. Keep the no-op default for unrelated fetches (the click
    // path also fires off ``loadStats`` etc.).
    captured = [];
    const original = window.fetch;
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      if (url === '/api/add') {
        captured.push({
          url,
          method: init?.method,
          body: init?.body ? JSON.parse(init.body) : null,
        });
        return {
          ok: true,
          status: 200,
          json: async () => ({ file: '/tmp/x.md', indexed_chunks: 1 }),
          text: async () => '{}',
        };
      }
      return original(input, init);
    };
  });

  async function clickAdd() {
    const btn = document.getElementById('add-btn');
    btn.dispatchEvent(new window.Event('click'));
    // Flush microtasks twice — the handler awaits ``showConfirm`` and
    // then ``api(...)``; both resolve as resolved promises in this
    // test, but JSDOM's task queue still needs a tick per ``await``.
    for (let i = 0; i < 5; i++) {
      await new Promise(r => setTimeout(r, 0));
    }
  }

  it('clean content posts without force_unsafe', async () => {
    document.getElementById('add-content').value = 'just a normal note';
    window.STATE.privacyPatterns = [/sk-[A-Za-z0-9]{20,}/];

    await clickAdd();

    expect(captured).toHaveLength(1);
    expect(captured[0].method).toBe('POST');
    expect(captured[0].body.content).toBe('just a normal note');
    expect(captured[0].body.force_unsafe).toBeUndefined();
  });

  it('flagged content + confirm posts force_unsafe: true', async () => {
    document.getElementById('add-content').value =
      'token sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ rest';
    window.STATE.privacyPatterns = [/sk-[A-Za-z0-9]{20,}/];
    window.showConfirm = async () => true;

    await clickAdd();

    expect(captured).toHaveLength(1);
    expect(captured[0].body.force_unsafe).toBe(true);
  });

  it('flagged content + cancel does not post', async () => {
    document.getElementById('add-content').value =
      'token sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ rest';
    window.STATE.privacyPatterns = [/sk-[A-Za-z0-9]{20,}/];
    window.showConfirm = async () => false;

    await clickAdd();

    expect(captured).toHaveLength(0);
  });
});

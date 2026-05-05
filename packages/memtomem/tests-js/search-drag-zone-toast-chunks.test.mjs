/* Regression guard for the search-tab drag-zone success toast.
 *
 * The toast handler at the bottom of the search drag-zone reads
 * ``(data.results || []).reduce(...)`` to compute its chunk count, but
 * ``/api/upload`` returns ``{files, total_indexed}`` and has never
 * exposed a ``results`` field — see ``UploadResponse`` in
 * ``web/routes/system.py``. The fallback to ``[]`` silently gave
 * "Indexed N files → 0 chunks" since v0.1.0 even when files indexed
 * cleanly. This pins the fix: aggregate over ``data.files`` so the
 * displayed chunk count matches the actual write.
 *
 * The mutation that proves this test bites: revert to
 * ``(data.results || []).reduce(...)`` and the chunk-count assertion
 * fails because the toast reads "0 chunks".
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('Search drag-zone — success toast aggregates real chunk count', () => {
  let window;
  let document;
  let toasts;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;

    // ``showToast`` writes DOM through helpers we don't need to exercise
    // here — the test cares about (msg, level) pairs, so swap in a spy.
    toasts = [];
    window.showToast = (msg, level) => {
      toasts.push({ msg, level });
    };

    // Stub ``/api/upload`` with a clean two-file response (no ``error``
    // field on either row). The drag-zone helper takes the early
    // ``!blockedRows.length`` return, so the retry path doesn't fire
    // and the success-toast branch is the only thing under test.
    const original = window.fetch;
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      if (url === '/api/upload') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            files: [
              { filename: 'a.md', indexed_chunks: 3, path: '/x/a.md' },
              { filename: 'b.md', indexed_chunks: 4, path: '/x/b.md' },
            ],
            total_indexed: 7,
          }),
          text: async () => '{}',
        };
      }
      return original(input, init);
    };
  });

  it('drop two clean .md files → toast reports the summed chunk count', async () => {
    // The drag-zone helper builds FormData and posts to ``/api/upload``;
    // the stub above ignores the body, so the file objects only need to
    // pass the extension regex filter and survive ``FormData.append``.
    const fileA = new window.File(['a body'], 'a.md', { type: 'text/markdown' });
    const fileB = new window.File(['b body'], 'b.md', { type: 'text/markdown' });

    const tab = document.getElementById('tab-search');
    const drop = new window.Event('drop', { bubbles: true, cancelable: true });
    // ``Event`` doesn't carry ``dataTransfer`` in JSDOM; assign it
    // manually so the handler's ``e.dataTransfer.files`` path runs.
    Object.defineProperty(drop, 'dataTransfer', {
      value: { files: [fileA, fileB], items: [{ kind: 'file' }, { kind: 'file' }] },
    });
    tab.dispatchEvent(drop);

    // Drop handler is async (awaits fetch + toast). Flush microtasks
    // generously — same pattern as add-memory-force-unsafe.test.mjs.
    for (let i = 0; i < 10; i++) {
      await new Promise(r => setTimeout(r, 0));
    }

    const success = toasts.find(toast => toast.level === 'success');
    expect(
      success,
      `expected a success toast; saw: ${JSON.stringify(toasts)}`,
    ).toBeDefined();
    // Locale en: "Indexed {files} files → {chunks} chunks".
    // The fix is the ``7 chunks`` half — the bug always rendered ``0
    // chunks`` regardless of what the server reported.
    expect(success.msg).toContain('2 files');
    expect(success.msg).toContain('7 chunks');
    expect(success.msg).not.toContain('0 chunks');
  });
});

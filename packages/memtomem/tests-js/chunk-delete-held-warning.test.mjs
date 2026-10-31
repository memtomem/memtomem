/* #2923: a chunk delete whose re-index was held answers 200 with a ``warning``.
 *
 * The line is gone from disk, but the file's project tier stopped being
 * registered while the delete ran, so the server held the source instead of
 * re-indexing it as another scope. The detail pane's delete must show that
 * warning in place of the plain "deleted" toast, and still show the plain
 * toast when there is none.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const CHUNK_ID = 'a1b2c3d4-0000-4000-8000-000000000002';

async function bootDelete(body) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
  const { window } = dom;
  const toasts = [];
  window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });
  window.showConfirm = async () => true;
  window.ensureCsrfToken = async () => 'test-token';
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.includes(`/api/chunks/${CHUNK_ID}`) && opts && opts.method === 'DELETE') {
      return { ok: true, status: 200, json: async () => body };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  window.STATE.selectedChunkId = CHUNK_ID;
  return { window, toasts };
}

async function clickDelete(window) {
  window.document.getElementById('d-delete-btn').click();
  for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));
}

describe('chunk delete — held re-index warning (#2923)', () => {
  it('shows the server warning instead of the plain success toast', async () => {
    const warning = 'The delete is on disk, but the file\'s project tier stopped being registered';
    const { window, toasts } = await bootDelete({ deleted: 1, warning });

    await clickDelete(window);

    expect(toasts).toContainEqual({ msg: warning, sev: 'warning' });
    expect(toasts.some((t) => t.sev === 'success')).toBe(false);
  });

  it('keeps the plain success toast when there is no warning', async () => {
    const { window, toasts } = await bootDelete({ deleted: 1, warning: null });

    await clickDelete(window);

    expect(toasts.some((t) => t.sev === 'success')).toBe(true);
    expect(toasts.some((t) => t.sev === 'warning')).toBe(false);
  });
});

describe('chunk save — held re-index warning (#2923)', () => {
  it('the detail save shows the warning and not the saved toast', async () => {
    const warning = 'The edit is on disk, but the file\'s project tier stopped being registered';
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const toasts = [];
    window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });
    window.ensureCsrfToken = async () => 'test-token';
    const upstream = window.fetch;
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url.includes(`/api/chunks/${CHUNK_ID}`) && opts && opts.method === 'PATCH') {
        return { ok: true, status: 200, json: async () => ({ id: CHUNK_ID, warning }) };
      }
      return upstream(input, opts);
    };
    await window.I18N.init();
    window.STATE.selectedChunkId = CHUNK_ID;
    window.STATE.selectedOriginal = 'before';
    window.document.getElementById('d-editor').value = 'after';

    window.document.getElementById('d-save-btn').click();
    for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));

    expect(toasts).toContainEqual({ msg: warning, sev: 'warning' });
    expect(toasts.some((t) => t.sev === 'success')).toBe(false);
  });
});

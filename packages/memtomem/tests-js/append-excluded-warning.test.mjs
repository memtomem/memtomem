/* #3165: an append whose target became excluded mid-write answers 200 with a
 * ``warning``.
 *
 * The entry is on disk, but an exclude pattern installed between the server's
 * check and its re-index made the re-index skip the file, so nothing was
 * indexed. The Add form and the scratch Promote button must show that warning
 * in place of their success toast, and keep the success toast when there is
 * none.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const WARNING = 'The new entry is on disk, but indexing started skipping this file';

async function boot(scripts, url, body) {
  const dom = await bootApp({ scripts });
  const { window } = dom;
  const toasts = [];
  window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });
  window.ensureCsrfToken = async () => 'test-token';
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const target = typeof input === 'string' ? input : input?.url || '';
    if (target === url && opts && opts.method === 'POST') {
      return { ok: true, status: 200, json: async () => body, text: async () => '{}' };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  return { window, toasts };
}

async function settle(window) {
  for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));
}

async function add(body) {
  const { window, toasts } = await boot(['i18n.js', 'app.js'], '/api/add', body);
  window.document.getElementById('add-content').value = 'a normal note';
  window.document.getElementById('add-btn').dispatchEvent(new window.Event('click'));
  await settle(window);
  return toasts;
}

async function promote(body) {
  const { window, toasts } = await boot(
    ['i18n.js', 'app.js', 'settings-harness.js'],
    '/api/scratch/k1/promote',
    body,
  );
  await window.promoteScratchEntry('k1');
  await settle(window);
  return toasts;
}

describe('Add memory — excluded mid-append warning (#3165)', () => {
  it('shows the server warning instead of the saved toast', async () => {
    const toasts = await add({ file: '/tmp/x.md', indexed_chunks: 0, warning: WARNING });

    expect(toasts).toContainEqual({ msg: WARNING, sev: 'warning' });
    expect(toasts.some((t) => t.sev === 'success')).toBe(false);
  });

  it('keeps the saved toast when there is no warning', async () => {
    const toasts = await add({ file: '/tmp/x.md', indexed_chunks: 1, warning: null });

    expect(toasts.some((t) => t.sev === 'success')).toBe(true);
    expect(toasts.some((t) => t.sev === 'warning')).toBe(false);
  });
});

describe('Scratch promote — excluded mid-append warning (#3165)', () => {
  it('shows the server warning instead of the promoted toast', async () => {
    const toasts = await promote({ key: 'k1', promoted: true, indexed_chunks: 0, warning: WARNING });

    expect(toasts).toContainEqual({ msg: WARNING, sev: 'warning' });
    expect(toasts.some((t) => t.sev === 'success')).toBe(false);
  });

  it('keeps the promoted toast when there is no warning', async () => {
    const toasts = await promote({ key: 'k1', promoted: true, indexed_chunks: 1, warning: null });

    expect(toasts.some((t) => t.sev === 'success')).toBe(true);
    expect(toasts.some((t) => t.sev === 'warning')).toBe(false);
  });
});

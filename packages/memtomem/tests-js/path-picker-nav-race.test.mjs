/* Path-picker navigation hardening (#1247 id 28).
 *
 * Two related gaps in ``navigate()``:
 *   1. No request sequencing — under rapid clicks the last RESPONSE painted,
 *      not the last CLICK, so a slow earlier directory listing could
 *      overwrite the one the user actually chose.
 *   2. No in-modal failure state — a failed (initial) load toasted and left
 *      a blank modal with no retry; close/reopen was the only way out.
 *
 * Plus the close() interaction: a response landing after close must not
 * paint into the hidden modal or pull focus back to it.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const ROOTS_BODY = {
  path: null,
  parent: null,
  is_root: true,
  entries: [
    { name: 'alpha', path: '/roots/alpha' },
    { name: 'beta', path: '/roots/beta' },
  ],
};

function dirBody(path, childName) {
  return {
    path,
    parent: '/roots',
    is_root: false,
    entries: [{ name: childName, path: `${path}/${childName}` }],
  };
}

function jsonResponse(body) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

async function flush(window, ticks = 20) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'path-picker.js'] });
  await dom.window.I18N.init();
  return dom.window;
}

// Route /api/fs/list by its ``path`` query param. ``routes`` maps the param
// value ('' for the roots view) to a function returning a Promise (deferred
// resolution) or a plain response.
function installFsListFetch(window, routes) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/fs/list')) {
      const params = new URL(url, 'http://localhost').searchParams;
      const key = params.get('path') || '';
      const route = routes[key];
      if (route) return route();
      return jsonResponse(ROOTS_BODY);
    }
    return upstream(input);
  };
}

describe('path-picker navigate() sequencing', () => {
  it('paints the last CLICKED directory when responses resolve out of order', async () => {
    const window = await boot();
    const alphaGate = deferred();
    const betaGate = deferred();
    installFsListFetch(window, {
      '': () => jsonResponse(ROOTS_BODY),
      '/roots/alpha': () => alphaGate.promise,
      '/roots/beta': () => betaGate.promise,
    });

    window.PathPicker.open();
    await flush(window);
    const items = window.document.querySelectorAll('#path-picker-list li');
    expect(items.length).toBe(2);

    // Click alpha (response held), then beta (response held). Beta is the
    // last user intent.
    items[0].click();
    items[1].click();
    await flush(window);

    // Resolve in REVERSE order: beta first (newer click), alpha last.
    betaGate.resolve(jsonResponse(dirBody('/roots/beta', 'inside-beta')));
    await flush(window);
    alphaGate.resolve(jsonResponse(dirBody('/roots/alpha', 'inside-alpha')));
    await flush(window);

    // The stale alpha response must NOT have overwritten beta's listing.
    const names = Array.from(window.document.querySelectorAll('#path-picker-list li'))
      .map((li) => li.textContent);
    expect(names.join(',')).toContain('inside-beta');
    expect(names.join(',')).not.toContain('inside-alpha');
    // Breadcrumb tail is the directory the user actually chose, and Select
    // would commit it (currentPath followed the painted view).
    expect(window.document.getElementById('path-picker-breadcrumb').textContent)
      .toContain('beta');
  });

  it('suppresses the toast of a stale FAILED request superseded by a newer success', async () => {
    // Codex review on #1247 id 28: the seq guard must gate the toast too,
    // not just the paint — ``_fetchList`` used to toast before ``navigate``
    // checked the ticket, so a slow failing request error-toasted right
    // over the newer listing the user was already on.
    const window = await boot();
    const toasts = [];
    window.showToast = (msg, level) => { toasts.push({ msg, level }); };
    const alphaGate = deferred();
    installFsListFetch(window, {
      '': () => jsonResponse(ROOTS_BODY),
      '/roots/alpha': () => alphaGate.promise,
      '/roots/beta': () => jsonResponse(dirBody('/roots/beta', 'inside-beta')),
    });

    window.PathPicker.open();
    await flush(window);
    const items = window.document.querySelectorAll('#path-picker-list li');
    items[0].click(); // alpha — will FAIL, slowly
    items[1].click(); // beta — succeeds immediately
    await flush(window);
    expect(window.document.getElementById('path-picker-breadcrumb').textContent)
      .toContain('beta');

    // The stale alpha request now fails. No toast, no error state — the
    // user is on beta and never asked for this outcome.
    alphaGate.resolve({
      ok: false,
      status: 503,
      json: async () => ({ detail: 'boom' }),
      text: async () => '{"detail":"boom"}',
    });
    await flush(window);
    expect(toasts).toHaveLength(0);
    expect(window.document.getElementById('path-picker-error').hidden).toBe(true);
    const names = Array.from(window.document.querySelectorAll('#path-picker-list li'))
      .map((li) => li.textContent);
    expect(names.join(',')).toContain('inside-beta');
  });

  it('ignores a response that lands after close (no hidden-modal paint or focus steal)', async () => {
    const window = await boot();
    const alphaGate = deferred();
    installFsListFetch(window, {
      '': () => jsonResponse(ROOTS_BODY),
      '/roots/alpha': () => alphaGate.promise,
    });

    window.PathPicker.open();
    await flush(window);
    window.document.querySelectorAll('#path-picker-list li')[0].click();
    window.PathPicker.close();
    alphaGate.resolve(jsonResponse(dirBody('/roots/alpha', 'inside-alpha')));
    await flush(window);

    expect(window.document.getElementById('path-picker-modal').hidden).toBe(true);
    expect(window.document.querySelectorAll('#path-picker-list li').length).toBe(0);
    // Focus was not pulled back into the hidden dialog by the stale paint.
    expect(window.document.getElementById('path-picker-modal')
      .contains(window.document.activeElement)).toBe(false);
  });
});

describe('path-picker in-modal load-failure state', () => {
  it('shows the error + Retry on a failed initial load, and Retry recovers in place', async () => {
    const window = await boot();
    let failing = true;
    installFsListFetch(window, {
      '': () => {
        if (failing) throw new TypeError('Failed to fetch');
        return jsonResponse(ROOTS_BODY);
      },
    });

    window.PathPicker.open();
    await flush(window);

    // Pre-fix: toast-only, blank modal, no in-modal affordance. Now: the
    // error block is visible, Select stays disabled, and the empty-state
    // (a different message) stays hidden.
    const errorEl = window.document.getElementById('path-picker-error');
    expect(errorEl.hidden).toBe(false);
    expect(window.document.getElementById('path-picker-select-btn').disabled).toBe(true);
    expect(window.document.getElementById('path-picker-empty').hidden).toBe(true);
    expect(window.document.querySelectorAll('#path-picker-list li').length).toBe(0);

    // Retry re-runs the SAME failed navigation without close/reopen.
    failing = false;
    window.document.getElementById('path-picker-retry-btn').click();
    await flush(window);

    expect(errorEl.hidden).toBe(true);
    expect(window.document.querySelectorAll('#path-picker-list li').length).toBe(2);
  });

  it('keeps the current listing on a 422 outside-scope refusal (toast-only contract)', async () => {
    const window = await boot();
    installFsListFetch(window, {
      '': () => jsonResponse(ROOTS_BODY),
      '/roots/alpha': () => ({
        ok: false,
        status: 422,
        json: async () => ({ detail: 'outside_picker_scope' }),
        text: async () => '{"detail":"outside_picker_scope"}',
      }),
    });

    window.PathPicker.open();
    await flush(window);
    window.document.querySelectorAll('#path-picker-list li')[0].click();
    await flush(window);

    // Scope refusal ≠ load failure: the roots listing the user was on is
    // still valid, so no error block and no wiped list.
    expect(window.document.getElementById('path-picker-error').hidden).toBe(true);
    expect(window.document.querySelectorAll('#path-picker-list li').length).toBe(2);
  });
});

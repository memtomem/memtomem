/* Regression guard for #1080 — distinguish the three failure shapes of
 * ``GET /api/context/projects`` instead of silently collapsing them all
 * onto the Server-CWD-only fallback.
 *
 *   - 404 / network throw     → silent fallback (legacy/older-deploy contract)
 *   - 5xx / non-404 4xx       → toast + fallback (endpoint failing)
 *   - 200 + malformed JSON    → toast + fallback (response unreadable)
 *   - 200 + real {scopes:[…]} → no toast, real scopes rendered (baseline)
 *
 * The four cells together pin the symmetric pair per
 * ``feedback_pin_invert_symmetric_assertion.md``: positive-only or
 * negative-only would false-pass since the bug is "three cases collapsing
 * to one". The mutation that proves these tests bite is restoring the
 * original blanket ``catch (_err) { fallback }`` — the 503 / parse cases
 * then stop toasting.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const REAL_SCOPES = [
  {
    scope_id: '',
    label: 'Server CWD',
    root: '/srv',
    tier: 'project',
    sources: ['server-cwd'],
    missing: false,
    experimental: false,
    counts: { skills: 0, commands: 0, agents: 0 },
  },
  {
    scope_id: 'proj-abc',
    label: 'proj-abc',
    root: '/work/proj-abc',
    tier: 'project',
    sources: ['memtomem-config'],
    missing: false,
    experimental: false,
    counts: { skills: 0, commands: 0, agents: 0 },
  },
];

function jsonRes(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function malformedJsonRes() {
  // The fix splits ``res.json()`` into its own try/catch — to exercise that
  // branch we need ``ok: true`` but a ``json()`` that throws like real
  // browser fetch would on invalid JSON. ``text()`` is included for parity
  // with the other stubs even though context-gateway never reads it.
  return {
    ok: true,
    status: 200,
    json: async () => { throw new SyntaxError('Unexpected token < in JSON at position 0'); },
    text: async () => '<html>oops</html>',
  };
}

function installProjectsFetch(window, responder) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/context/projects')) return responder();
    return upstream(input);
  };
}

async function bootWithToastSpy() {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
  });
  const { window } = dom;
  // ``showToast`` is a function declaration in app.js so it lives on the
  // window-global lookup chain; overwriting ``window.showToast`` after
  // load swaps every later free-reference call site in context-gateway.js
  // (verified against the existing pattern in
  // ``search-drag-zone-toast-chunks.test.mjs``).
  const toasts = [];
  window.showToast = (msg, level) => { toasts.push({ msg, level }); };
  return { dom, window, toasts };
}

describe('_ctxFetchProjects — failure-shape signal split (#1080)', () => {
  let window;
  let toasts;

  beforeEach(async () => {
    ({ window, toasts } = await bootWithToastSpy());
  });

  it('baseline 200 with real scopes — no toast, scopes rendered as-is', async () => {
    installProjectsFetch(window, () => jsonRes({ scopes: REAL_SCOPES }));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(2);
    expect(data.scopes[1].scope_id).toBe('proj-abc');
    expect(toasts).toHaveLength(0);
  });

  it('404 — silent fallback to Server CWD (older-deploy contract)', async () => {
    installProjectsFetch(window, () => jsonRes({ detail: 'not found' }, 404));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    // Pin the silent half: 404 must NOT toast, or older deployments / test
    // stubs that legitimately omit the endpoint would spam users.
    expect(toasts).toHaveLength(0);
  });

  it('503 — toast + fallback (endpoint exists but failing)', async () => {
    installProjectsFetch(window, () => jsonRes({ detail: 'store unavailable' }, 503));
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].level).toBe('error');
    expect(toasts[0].msg).toContain('store unavailable');
  });

  it('200 with malformed JSON — toast + fallback (response unreadable)', async () => {
    installProjectsFetch(window, () => malformedJsonRes());
    const data = await window._ctxFetchProjects();
    expect(data.scopes).toHaveLength(1);
    expect(data.scopes[0].sources).toEqual(['server-cwd']);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].level).toBe('error');
    // Parse-error detail string varies by engine; just pin that *something*
    // about the failure surfaced (not the empty/raw key fallback).
    expect(toasts[0].msg).not.toBe('settings.ctx.projects_fetch_failed');
    expect(toasts[0].msg.length).toBeGreaterThan(0);
  });
});

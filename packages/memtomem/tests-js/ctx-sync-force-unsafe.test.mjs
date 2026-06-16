/* Reviewed Gate A bypass on the web fan-out (sync) surface.
 *
 * The sync-side mirror of ctx-import-force-unsafe.test.mjs. Gate A's
 * secret-shape heuristic matches more than real secrets — a pydantic
 * ``api_key: str`` annotation trips ``(api_key|secret_key|...)\s*[:=]`` with no
 * real secret. The import surface exposes a reviewed bypass valve; the fan-out
 * (sync) surface now does too.
 *
 * These pin ``_ctxMaybeForceUnsafeSync``:
 *   - offers the force confirm ONLY when a skip carries
 *     ``reason_code === 'privacy_blocked'`` (the force-able user tier), then
 *     re-syncs ONCE with BOTH ``force_unsafe_sync`` and ``allow_host_writes``.
 *     Unlike import, no second host-write disclosure is needed: sync host
 *     targets are name-based (force-independent), so the pre-force host-write
 *     confirm already disclosed the exact paths a forced write lands on.
 *   - inert for the hard ``privacy_blocked_project_shared`` code and for
 *     unrelated skips.
 *   - reads the artifact name from the sync skip's ``runtime`` key.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  await window.I18N.init();
  return window;
}

// Sync skips serialize the artifact name under the ``runtime`` key.
const BLOCKED = {
  generated: [],
  skipped: [
    { runtime: 'llm-project-architect', reason: 'privacy blocked', reason_code: 'privacy_blocked' },
  ],
};

// A ``resync(extra)`` spy: records each call's ``extra`` and returns the queued
// responses in order.
function resyncSpy(responses) {
  const calls = [];
  const queue = [...responses];
  const fn = async (extra) => {
    calls.push(extra);
    const body = queue.shift();
    return typeof body === 'function' ? body() : body;
  };
  fn.calls = calls;
  return fn;
}

const okResp = (json) => ({ ok: true, status: 200, json: async () => json });

describe('_ctxMaybeForceUnsafeSync (reviewed Gate A bypass on fan-out)', () => {
  it('re-syncs ONCE with both flags after the red override is approved', async () => {
    const window = await boot();
    const confirms = [];
    window.showConfirm = async (opts) => { confirms.push(opts); return true; };
    window.showToast = () => {};

    const resync = resyncSpy([
      okResp({ generated: [{ runtime: 'claude' }], skipped: [] }),
    ]);
    const out = await window._ctxMaybeForceUnsafeSync(BLOCKED, resync);

    // Host writes were already disclosed pre-force, so the single forced
    // re-sync carries BOTH flags — no second host-write round-trip.
    expect(resync.calls).toEqual([{ force_unsafe_sync: true, allow_host_writes: true }]);
    // One red dialog, naming the blocked artifact.
    expect(confirms.length).toBe(1);
    expect(confirms[0].danger).toBe(true);
    expect(confirms[0].warningText).toContain('llm-project-architect');
    expect(out).toEqual({ generated: [{ runtime: 'claude' }], skipped: [] });
  });

  it('returns null and re-syncs nothing when the override is declined', async () => {
    const window = await boot();
    window.showConfirm = async () => false;
    const resync = resyncSpy([okResp({})]);
    const out = await window._ctxMaybeForceUnsafeSync(BLOCKED, resync);
    expect(out).toBeNull();
    expect(resync.calls.length).toBe(0);
  });

  it('is inert when no skip is privacy_blocked (unrelated skips ignored)', async () => {
    const window = await boot();
    let confirmed = false;
    window.showConfirm = async () => { confirmed = true; return true; };
    const data = {
      generated: [],
      skipped: [{ runtime: 'x', reason: 'target conflict', reason_code: 'target_conflict' }],
    };
    const out = await window._ctxMaybeForceUnsafeSync(data, async () => {
      throw new Error('must not re-sync');
    });
    expect(out).toBeNull();
    expect(confirmed).toBe(false);
  });

  it('does NOT offer force for the hard project_shared block', async () => {
    const window = await boot();
    let confirmed = false;
    window.showConfirm = async () => { confirmed = true; return true; };
    const data = {
      generated: [],
      skipped: [{ runtime: 'x', reason: '...', reason_code: 'privacy_blocked_project_shared' }],
    };
    const out = await window._ctxMaybeForceUnsafeSync(data, async () => {
      throw new Error('project_shared has no bypass');
    });
    expect(out).toBeNull();
    expect(confirmed).toBe(false);
  });

  it('surfaces an error toast when the forced re-sync itself fails', async () => {
    const window = await boot();
    window.showConfirm = async () => true;
    const toasts = [];
    window.showToast = (msg, sev) => toasts.push({ msg, sev });
    const resync = resyncSpy([
      { ok: false, status: 422, json: async () => ({ detail: 'still blocked' }) },
    ]);
    const out = await window._ctxMaybeForceUnsafeSync(BLOCKED, resync);
    expect(out).toBeNull();
    expect(toasts.some((x) => x.sev === 'error')).toBe(true);
  });

  it('tolerates a skip that carries name instead of runtime', async () => {
    const window = await boot();
    const confirms = [];
    window.showConfirm = async (opts) => { confirms.push(opts); return true; };
    window.showToast = () => {};
    const data = {
      generated: [],
      skipped: [{ name: 'shaped-by-name', reason_code: 'privacy_blocked' }],
    };
    const resync = resyncSpy([okResp({ generated: [{ runtime: 'claude' }], skipped: [] })]);
    const out = await window._ctxMaybeForceUnsafeSync(data, resync);
    expect(confirms[0].warningText).toContain('shaped-by-name');
    expect(out.generated.length).toBe(1);
  });
});

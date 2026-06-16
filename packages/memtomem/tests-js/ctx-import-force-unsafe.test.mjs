/* Reviewed Gate A bypass on the web import surface.
 *
 * Gate A's secret-shape heuristic matches more than real secrets — a pydantic
 * ``api_key: str`` type annotation or a ``secret_key=settings.x`` kwarg trips
 * ``(api_key|secret_key|...)\s*[:=]`` with no actual secret present. The CLI's
 * ``--force-unsafe-import`` and the upload/memory/chunk web write surfaces
 * already expose a reviewed bypass valve; the context import surface did not.
 *
 * These pin the two JS helpers added to close that gap:
 *   - ``_ctxMaybeForceUnsafeImport`` — offers the force confirm ONLY when a
 *     skip carries ``reason_code === 'privacy_blocked'`` (the force-able user
 *     tier), then retries with ``force_unsafe_import`` ALONE so the server's
 *     host-write gate can disclose the forced ``~/.memtomem/`` destinations,
 *     and only resends with ``allow_host_writes`` after that disclosure is
 *     confirmed. (Consent separation: approving the privacy override is not
 *     consent to write outside the project root.) Inert for the hard
 *     ``privacy_blocked_project_shared`` code and unrelated skips.
 *   - ``_ctxImportErrToast`` — surfaces the localized "switch to the User tier"
 *     hint ALONE on the project_shared 422 (every import 422 is that one privacy
 *     block, and its server detail is fixed English — #1398 item 1), leaving
 *     every other status' detail untouched.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  await window.I18N.init();
  return window;
}

const BLOCKED = {
  imported: [],
  skipped: [
    { name: 'llm-project-architect', reason: 'privacy blocked', reason_code: 'privacy_blocked' },
  ],
};

const HOST_ENVELOPE = {
  status: 'needs_confirmation',
  confirm: 'allow_host_writes',
  reason: 'Import skill targets the user tier — host paths outside any project root.',
  host_targets: ['/home/u/.memtomem/skills/llm-project-architect'],
};

// A ``reimport(extra)`` spy: records each call's ``extra`` and returns the
// queued responses in order.
function reimportSpy(responses) {
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

describe('_ctxImportErrToast (project_shared privacy hint)', () => {
  it('shows the localized hint ALONE on a 422, dropping the English detail', async () => {
    const window = await boot();
    const hint = window.t('settings.ctx.privacy_blocked_shared_hint');
    expect(hint).not.toBe('settings.ctx.privacy_blocked_shared_hint'); // localized, not key echo

    // The English server detail must NOT leak into the Korean-UI toast (#1398
    // item 1): the 422 is always the privacy block, so the hint stands alone.
    const detail = "Gate A: SKILL.md contains 2 privacy pattern hit(s); import to scope='project_shared' rejected.";
    const at422 = window._ctxImportErrToast(422, detail);
    expect(at422).toBe(hint);
    expect(at422).not.toContain(detail);

    const at500 = window._ctxImportErrToast(500, 'boom');
    expect(at500).toBe('boom');
    expect(at500).not.toContain(hint);
  });
});

describe('_ctxMaybeForceUnsafeImport (reviewed Gate A bypass)', () => {
  it('retries with force ALONE, then discloses host writes before resending both flags', async () => {
    const window = await boot();
    // Queue: force confirm = approve, host-write confirm = approve.
    const answers = [true, true];
    const confirms = [];
    window.showConfirm = async (opts) => { confirms.push(opts); return answers.shift(); };
    window.showToast = () => {};

    const reimport = reimportSpy([
      okResp(HOST_ENVELOPE), // first force retry → host-write disclosure needed
      okResp({ imported: [{ name: 'llm-project-architect' }], skipped: [] }), // confirmed
    ]);
    const out = await window._ctxMaybeForceUnsafeImport(BLOCKED, reimport);

    // First retry carries force but NOT allow_host_writes — the whole point.
    expect(reimport.calls[0]).toEqual({ force_unsafe_import: true });
    // Only after the host-write disclosure is confirmed do both flags ride.
    expect(reimport.calls[1]).toEqual({ force_unsafe_import: true, allow_host_writes: true });
    expect(reimport.calls.length).toBe(2);

    // Two dialogs: the red privacy override, then the host-write disclosure.
    expect(confirms[0].danger).toBe(true);
    expect(confirms[0].warningText).toContain('llm-project-architect');
    expect(confirms[1].warningText).toContain('/home/u/.memtomem/skills/llm-project-architect');

    expect(out).toEqual({ imported: [{ name: 'llm-project-architect' }], skipped: [] });
  });

  it('returns the result directly when the force retry needs no host write', async () => {
    const window = await boot();
    window.showConfirm = async () => true;
    window.showToast = () => {};
    const reimport = reimportSpy([
      okResp({ imported: [{ name: 'llm-project-architect' }], skipped: [] }),
    ]);
    const out = await window._ctxMaybeForceUnsafeImport(BLOCKED, reimport);
    expect(reimport.calls).toEqual([{ force_unsafe_import: true }]);
    expect(out.imported.length).toBe(1);
  });

  it('writes nothing when the host-write disclosure is declined in the force path', async () => {
    const window = await boot();
    const answers = [true, false]; // approve privacy override, decline host write
    window.showConfirm = async () => answers.shift();
    window.showToast = () => {};
    const reimport = reimportSpy([okResp(HOST_ENVELOPE)]);
    const out = await window._ctxMaybeForceUnsafeImport(BLOCKED, reimport);
    expect(out).toBeNull();
    expect(reimport.calls).toEqual([{ force_unsafe_import: true }]); // no second, flagged write
  });

  it('returns null and re-imports nothing when the privacy override is declined', async () => {
    const window = await boot();
    window.showConfirm = async () => false;
    const reimport = reimportSpy([okResp({})]);
    const out = await window._ctxMaybeForceUnsafeImport(BLOCKED, reimport);
    expect(out).toBeNull();
    expect(reimport.calls.length).toBe(0);
  });

  it('is inert when no skip is privacy_blocked (unrelated skips ignored)', async () => {
    const window = await boot();
    let confirmed = false;
    window.showConfirm = async () => { confirmed = true; return true; };
    const data = {
      imported: [],
      skipped: [{ name: 'x', reason: 'canonical exists', reason_code: 'canonical_exists' }],
    };
    const out = await window._ctxMaybeForceUnsafeImport(data, async () => {
      throw new Error('must not re-import');
    });
    expect(out).toBeNull();
    expect(confirmed).toBe(false);
  });

  it('does NOT offer force for the hard project_shared block', async () => {
    const window = await boot();
    let confirmed = false;
    window.showConfirm = async () => { confirmed = true; return true; };
    const data = {
      imported: [],
      skipped: [{ name: 'x', reason: '...', reason_code: 'privacy_blocked_project_shared' }],
    };
    const out = await window._ctxMaybeForceUnsafeImport(data, async () => {
      throw new Error('project_shared has no bypass');
    });
    expect(out).toBeNull();
    expect(confirmed).toBe(false);
  });

  it('surfaces an error toast when the forced re-import itself fails', async () => {
    const window = await boot();
    window.showConfirm = async () => true;
    const toasts = [];
    window.showToast = (msg, sev) => toasts.push({ msg, sev });
    const reimport = reimportSpy([
      { ok: false, status: 422, json: async () => ({ detail: 'still blocked' }) },
    ]);
    const out = await window._ctxMaybeForceUnsafeImport(BLOCKED, reimport);
    expect(out).toBeNull();
    expect(toasts.some((x) => x.sev === 'error')).toBe(true);
  });
});

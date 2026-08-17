/* #2081: deleting an indexed source that holds project_shared chunks rides
 * the same disclose-then-confirm round-trip the context gateway uses.
 *
 * Server contract: an unconfirmed DELETE answers HTTP 200
 * ``{status: "needs_confirmation", confirm: "confirm_project_shared", ...}``
 * and deletes nothing; the confirmed re-send carries
 * ``confirm_project_shared=true`` in the query string (DELETE bodies are
 * client-hostile, so every flag on this verb rides the URL). These pin the
 * JS half:
 *
 *   - a user-scope source still deletes on one request, no second modal;
 *   - the envelope opens a second confirm and the re-send carries the flag
 *     the SERVER named, not a hardcoded client guess;
 *   - declining the disclosure sends nothing further and reports no success
 *     — a declined delete is a choice, not a failure.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SOURCE = '/tmp/memories/shared-note.md';

const SHARED_ENVELOPE = {
  status: 'needs_confirmation',
  confirm: 'confirm_project_shared',
  reason:
    'This source holds project_shared chunks that are shared with the '
    + 'repository. Deleting it removes them for everyone using this project. '
    + 'Source deletes are all-or-nothing.',
  scopes: ['project_shared', 'user'],
};

async function bootDelete({ deleteResponses, confirmAnswers }) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
  const { window } = dom;
  const confirms = [];
  const answers = [...confirmAnswers];
  window.showConfirm = async (opts) => {
    confirms.push(opts);
    return answers.length ? answers.shift() : false;
  };
  const toasts = [];
  window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });
  window.ensureCsrfToken = async () => 'test-token';
  const deleteCalls = [];
  const pending = [...deleteResponses];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.split('?')[0].endsWith('/api/sources') && opts && opts.method === 'DELETE') {
      deleteCalls.push(url);
      const body = pending.length ? pending.shift() : { deleted: 3 };
      return { ok: true, status: 200, json: async () => body };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  return { window, confirms, toasts, deleteCalls };
}

describe('source delete — project_shared confirm round-trip (#2081)', () => {
  it('deletes a user-scope source on a single request', async () => {
    const { window, confirms, toasts, deleteCalls } = await bootDelete({
      deleteResponses: [{ deleted: 3 }],
      confirmAnswers: [true],
    });

    await window._deleteSourceFile(SOURCE);

    expect(deleteCalls).toHaveLength(1);
    expect(deleteCalls[0]).not.toContain('confirm_project_shared');
    // Only the ordinary delete confirm — no shared-scope disclosure.
    expect(confirms).toHaveLength(1);
    expect(toasts.some((t) => t.sev === 'success')).toBe(true);
  });

  it('discloses then re-sends with the flag the envelope names', async () => {
    const { window, confirms, toasts, deleteCalls } = await bootDelete({
      deleteResponses: [SHARED_ENVELOPE, { deleted: 3 }],
      confirmAnswers: [true, true],
    });

    await window._deleteSourceFile(SOURCE);

    expect(deleteCalls).toHaveLength(2);
    expect(deleteCalls[0]).not.toContain('confirm_project_shared');
    expect(deleteCalls[1]).toContain('confirm_project_shared=true');
    // The path must survive the second leg intact.
    expect(deleteCalls[1]).toContain(encodeURIComponent(SOURCE));
    expect(confirms).toHaveLength(2);
    expect(toasts.some((t) => t.sev === 'success')).toBe(true);
  });

  it('sends nothing further when the disclosure is declined', async () => {
    const { window, confirms, toasts, deleteCalls } = await bootDelete({
      deleteResponses: [SHARED_ENVELOPE],
      confirmAnswers: [true, false],
    });

    await window._deleteSourceFile(SOURCE);

    expect(deleteCalls).toHaveLength(1);
    expect(confirms).toHaveLength(2);
    // Neither success nor error: the user chose to stop.
    expect(toasts).toHaveLength(0);
  });
});

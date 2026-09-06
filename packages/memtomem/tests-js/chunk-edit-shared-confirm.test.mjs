/* #2317: saving a chunk that lives in the project_shared tier rides the same
 * disclose-then-confirm round-trip the source delete and context gateway use.
 *
 * Server contract: an unconfirmed PATCH answers HTTP 200
 * ``{status: "needs_confirmation", confirm: "confirm_project_shared", reason}``
 * and writes nothing; the confirmed re-send carries
 * ``confirm_project_shared: true`` in the JSON body. Gate B is checked before
 * the redaction scan, so a confirmed re-send can still trip Gate A.
 *
 * These pin the JS half:
 *
 *   - a user-scope chunk still saves on one request, no modal;
 *   - the envelope opens a confirm and the re-send carries the flag the
 *     SERVER named, not a hardcoded client guess;
 *   - the re-send carries the SAME body — the editor can change under an
 *     open dialog, and saving the newer bytes would write content the user
 *     was never warned about;
 *   - declining sends nothing further and reports no success;
 *   - a confirmed re-send that then trips the redaction guard still gets its
 *     own dialog, rather than surfacing a raw 403.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const CHUNK_ID = 'a1b2c3d4-0000-4000-8000-000000000001';
const BODY = { new_content: 'the reviewed text' };

const SHARED_ENVELOPE = {
  status: 'needs_confirmation',
  confirm: 'confirm_project_shared',
  reason:
    'This chunk lives in the project_shared tier, on a path the repository '
    + 'tracks. Saving replaces its body for everyone who pulls the project.',
};

const SAVED = { id: CHUNK_ID, content: 'the reviewed text' };

const REDACTION_403 = {
  ok: false,
  status: 403,
  json: async () => ({
    detail: { detail: 'redaction_blocked', hits: 2, surface: 'web_api_chunk_edit' },
  }),
};

async function bootEdit({ responses, confirmAnswers }) {
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
  const patches = [];
  const pending = [...responses];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.includes('/api/chunks/') && opts && opts.method === 'PATCH') {
      patches.push(JSON.parse(opts.body));
      const next = pending.length ? pending.shift() : SAVED;
      // A raw response object (the 403 shapes) is returned as-is; a plain
      // payload is wrapped as a 200.
      if (next && typeof next.json === 'function') return next;
      return { ok: true, status: 200, json: async () => next };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  return { window, confirms, toasts, patches };
}

describe('chunk edit — project_shared confirm round-trip (#2317)', () => {
  it('saves a user-scope chunk on a single request', async () => {
    const { window, confirms, patches } = await bootEdit({
      responses: [SAVED],
      confirmAnswers: [],
    });

    const resp = await window.saveChunkBody(CHUNK_ID, BODY);

    expect(patches).toHaveLength(1);
    expect(patches[0]).not.toHaveProperty('confirm_project_shared');
    expect(confirms).toHaveLength(0);
    expect(resp).toEqual(SAVED);
  });

  it('discloses then re-sends with the flag the envelope names', async () => {
    const { window, confirms, patches } = await bootEdit({
      responses: [SHARED_ENVELOPE, SAVED],
      confirmAnswers: [true],
    });

    const resp = await window.saveChunkBody(CHUNK_ID, BODY);

    expect(patches).toHaveLength(2);
    expect(patches[0].confirm_project_shared).toBeUndefined();
    expect(patches[1].confirm_project_shared).toBe(true);
    expect(confirms).toHaveLength(1);
    expect(resp).toEqual(SAVED);
  });

  it('re-sends the body it disclosed, not whatever the editor holds now', async () => {
    const { window, patches } = await bootEdit({
      responses: [SHARED_ENVELOPE, SAVED],
      confirmAnswers: [true],
    });
    const editable = { new_content: 'the reviewed text' };

    // The caller's object is mutated while the dialog is open — the shape a
    // live textarea produces if the retry re-reads it.
    window.showConfirm = async () => {
      editable.new_content = 'something the user never saw warned';
      return true;
    };
    await window.saveChunkBody(CHUNK_ID, editable);

    expect(patches).toHaveLength(2);
    expect(patches[1].new_content).toBe('the reviewed text');
  });

  it('sends nothing further when the disclosure is declined', async () => {
    const { window, confirms, toasts, patches } = await bootEdit({
      responses: [SHARED_ENVELOPE],
      confirmAnswers: [false],
    });

    const resp = await window.saveChunkBody(CHUNK_ID, BODY);

    expect(patches).toHaveLength(1);
    expect(confirms).toHaveLength(1);
    // Null is the cancel contract the redaction retry already uses, so the
    // call sites keep one check.
    expect(resp).toBeNull();
    expect(toasts).toHaveLength(0);
  });

  it('does not offer a force_unsafe bypass the shared tier can never accept', async () => {
    /* The first draft of this test asserted a third request carrying
     * force_unsafe that came back SAVED. The real backend cannot answer
     * that: enforce_write_guard hard-refuses force_unsafe on
     * project_shared unconditionally (ADR-0011 §5 Gate A). So the fiction
     * was in the fixture, and it hid a real defect — the helper prompted
     * for a bypass that was going to be refused, and the extra request
     * carried confirm_project_shared again, recording a SECOND Gate B
     * consent line for one human consent.
     */
    const { window, confirms, patches } = await bootEdit({
      responses: [SHARED_ENVELOPE, REDACTION_403],
      confirmAnswers: [true, true],
    });

    await expect(window.saveChunkBody(CHUNK_ID, BODY)).rejects.toThrow();

    // Gate B, then the confirmed leg. No third request: the bypass is not
    // on offer here, so nothing re-sends the consent.
    expect(patches).toHaveLength(2);
    expect(patches[1].confirm_project_shared).toBe(true);
    expect(patches.every((p) => p.force_unsafe === undefined)).toBe(true);
    // One dialog — the shared-tier disclosure. The redaction dialog must
    // not appear; a second confirmAnswer was staged precisely so that an
    // extra prompt would be answerable and therefore visible here.
    expect(confirms).toHaveLength(1);
  });

  it('reports the shared-tier refusal in its own words, not the bypass wording', async () => {
    const { window } = await bootEdit({
      responses: [SHARED_ENVELOPE, REDACTION_403],
      confirmAnswers: [true],
    });

    const err = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    // A RedactionBlockedError would make the caller's toast say "retry with
    // force_unsafe", which is false on this tier.
    expect(err.name).toBe('ProjectTierBlockedError');
    expect(err.message).toContain('2');
    expect(err.message).not.toMatch(/redaction_blocked/);
  });
});

/* #2317 / #2332: saving a chunk is a conversation, not a request.
 *
 * Server contract. ``PATCH /api/chunks/{id}`` carries two ADR-0011 §5 gates.
 * Gate B answers an unconfirmed ``project_shared`` write at HTTP 200 with
 * ``{status: "needs_confirmation", confirm: "confirm_project_shared", reason}``
 * and writes nothing; the confirmed re-send carries
 * ``confirm_project_shared: true``. Gate A runs after it and refuses a
 * redaction hit with a 403 that NAMES THE TIER it judged under — so a
 * confirmed re-send can still trip the scanner, and the client can tell
 * "the bypass is available here" from "it is not".
 *
 * Neither answer is stable for the length of the interaction: an incremental
 * re-index re-derives scope from the path and chunk ids survive it, while
 * every dialog holds a window open for as long as the user takes to answer.
 * ``saveChunkBody`` therefore loops, and the loop holds one invariant:
 *
 *     each attempt carries exactly the flag the server's LAST answer asked
 *     for, applied to the body disclosed before the first request.
 *
 * These pin the JS half:
 *
 *   - a user-scope chunk still saves on one request, no modal;
 *   - the envelope opens a confirm and the re-send carries the field the
 *     SERVER named — the client matches that name against the one
 *     confirmation it can answer and refuses anything else (an allow-list
 *     check, not dynamic following);
 *   - every attempt carries the SAME body — the editor can change under an
 *     open dialog, and saving the newer bytes would write content the user
 *     was never warned about;
 *   - declining at any dialog sends nothing further and reports no success;
 *   - a refusal on the shared tier is converted to a non-overridable
 *     shared-tier refusal, with NO bypass dialog: Gate A refuses
 *     force_unsafe there unconditionally.
 *
 * And the three cross-request races, which exist because a re-index can
 * re-scope a chunk while a dialog is open:
 *
 *   - private → shared between the redaction retry and its response: the
 *     retry gets the envelope and writes nothing, so no success toast may
 *     be shown (#2328);
 *   - shared → private between the envelope and the confirmed request: the
 *     bypass IS available now, so it must be offered IN THAT INTERACTION
 *     rather than left to a second Save (#2332);
 *   - either of those again on the next hop — answered up to a bounded
 *     number of attempts, then stopped with an honest refusal.
 *
 * Which tier is bypassable is an allow-list, never ``!== 'project_shared'``:
 * a refusal reporting no scope, or one naming a tier this build does not
 * know, is *unknown*, and unknown is offered nothing.
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

// The route reports the tier it decided the refusal under, so the client
// branches on what the server just saw rather than on the earlier envelope.
// ``scope: undefined`` models a surface that reports none at all — not this
// route, whose field is pinned server-side for every tier in
// ``test_web_routes.py``, but the shape the client must not guess about.
const redaction403 = (scope) => ({
  ok: false,
  status: 403,
  json: async () => ({
    detail: {
      detail: 'redaction_blocked',
      hits: 2,
      surface: 'web_api_chunk_edit',
      ...(scope === undefined ? {} : { scope }),
    },
  }),
});
const REDACTION_403 = redaction403('project_shared');

async function bootEdit({ responses, confirmAnswers, deferConfirms = false }) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
  const { window } = dom;
  const confirms = [];
  const answers = [...confirmAnswers];
  // ``deferConfirms`` parks each dialog on a promise the test settles through
  // ``answerConfirm`` instead of answering it inline. That is what holds one
  // save open long enough for a second to overlap it (#2340); every other test
  // here leaves it off and gets the immediate stub unchanged.
  const openDialogs = [];
  window.showConfirm = async (opts) => {
    confirms.push(opts);
    if (deferConfirms) return new Promise((r) => openDialogs.push(r));
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
  const answerConfirm = (value) => {
    const resolve = openDialogs.shift();
    if (!resolve) throw new Error('no dialog is open to answer');
    resolve(value);
  };
  return { window, confirms, toasts, patches, answerConfirm };
}

/** Let every already-scheduled microtask/timer drain. */
async function flush(window, ticks = 20) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
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

  it('re-sends with the field the envelope names, after checking it is one we answer', async () => {
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

  it('claims no success when a re-scope turns the bypass retry into an envelope', async () => {
    /* private → shared, in the window the redaction dialog holds open.
     * The force_unsafe retry then answers 200 needs_confirmation and writes
     * NOTHING. Announcing the bypass at that point would be the only toast
     * the user ever sees, saying the entry was written when it was not —
     * and if they decline the shared-tier confirm, they lose the edit
     * believing it saved.
     */
    const { window, toasts, patches } = await bootEdit({
      // The first refusal names ``user`` because that is what this route
      // always sends; #2332 made the tier decide whether the bypass is
      // offered at every hop, including the first, so an unscoped refusal
      // here would be declined outright — which the last two cases pin.
      responses: [redaction403('user'), SHARED_ENVELOPE],
      // 1st: bypass the scanner. 2nd: decline the shared-tier disclosure.
      confirmAnswers: [true, false],
    });

    const resp = await window.saveChunkBody(CHUNK_ID, BODY);

    expect(patches).toHaveLength(2);
    expect(patches[1].force_unsafe).toBe(true);
    expect(resp).toBeNull();
    // The load-bearing assertion: nothing told the user this worked.
    expect(toasts).toHaveLength(0);
  });

  it('apiWithRedactionRetry still refuses to call an envelope a bypassed write', async () => {
    /* The property above belongs to the generic wrapper too, and since
     * #2332 ``saveChunkBody`` no longer reaches it — so it needs a pin that
     * drives it directly, or the guard would be provable only through a
     * caller that stopped existing.
     */
    const { window, toasts, patches } = await bootEdit({
      // Scoped, because the URL below is this route's and this route always
      // reports one. The wrapper never reads the field — that is exactly
      // what is being pinned — so an unscoped fixture would only add a wire
      // shape the named route cannot produce.
      responses: [redaction403('user'), SHARED_ENVELOPE],
      confirmAnswers: [true],
    });

    const resp = await window.apiWithRedactionRetry(
      'PATCH', `/api/chunks/${CHUNK_ID}`, BODY,
    );

    expect(patches).toHaveLength(2);
    expect(patches[1].force_unsafe).toBe(true);
    // The envelope is handed back for the caller to answer, unannounced.
    expect(resp).toEqual(SHARED_ENVELOPE);
    expect(toasts).toHaveLength(0);
  });

  it('offers the bypass a shared → private re-scope has made available (#2332)', async () => {
    /* shared → private, in the window the shared-tier modal holds open.
     * The confirmed request is refused by the scanner, but on the tier the
     * chunk now lives in force_unsafe IS permitted. Before #2332 the error
     * propagated to a call site that only renders a failed-save toast, so
     * the offer was never made in that interaction — a second Save reached
     * it, which is why this was a missing offer and not a lost capability.
     */
    const { window, confirms, toasts, patches } = await bootEdit({
      responses: [SHARED_ENVELOPE, redaction403('user'), SAVED],
      // 1st: the shared-tier disclosure. 2nd: the redaction bypass.
      confirmAnswers: [true, true],
    });

    // Caught into a value, not awaited bare: a regression here is a throw,
    // and an escaping rejection is formatted across the JSDOM boundary,
    // where a source-map read aborts the whole run and leaves this case
    // reported as pending rather than failed.
    const resp = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(resp).toEqual(SAVED);
    expect(patches).toHaveLength(3);
    expect(confirms).toHaveLength(2);
    // Each attempt carries exactly the flag the LAST answer asked for.
    expect(patches[0]).toEqual(BODY);
    expect(patches[1]).toEqual({ ...BODY, confirm_project_shared: true });
    // The third must not re-send the consent: one human answer, one Gate B
    // consent line. Re-sending it would record a second if the chunk had
    // flipped back to shared before this attempt landed.
    expect(patches[2]).toEqual({ ...BODY, force_unsafe: true });
    // The write that landed used the bypass, so it is announced.
    expect(toasts).toEqual([
      { msg: window.I18N.t('toast.redaction_bypassed', { hits: 2 }), sev: 'info' },
    ]);
  });

  it('offers it on project_local too — the bypassable tiers are a list, not "not shared"', async () => {
    /* A ``!== "project_shared"`` test passes the case above on its own.
     * Only a second named tier forces the check to be a real membership
     * test, and only that keeps the unknown cases below out of it.
     */
    const { window, confirms, patches } = await bootEdit({
      responses: [SHARED_ENVELOPE, redaction403('project_local'), SAVED],
      confirmAnswers: [true, true],
    });

    // Caught into a value, not awaited bare: a regression here is a throw,
    // and an escaping rejection is formatted across the JSDOM boundary,
    // where a source-map read aborts the whole run and leaves this case
    // reported as pending rather than failed.
    const resp = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(resp).toEqual(SAVED);
    expect(patches).toHaveLength(3);
    expect(patches[2].force_unsafe).toBe(true);
    expect(confirms).toHaveLength(2);
  });

  it('sends nothing further when the bypass it offered is declined', async () => {
    const { window, confirms, toasts, patches } = await bootEdit({
      responses: [SHARED_ENVELOPE, redaction403('user')],
      confirmAnswers: [true, false],
    });

    const resp = await window.saveChunkBody(CHUNK_ID, BODY);

    expect(resp).toBeNull();
    expect(patches).toHaveLength(2);
    expect(confirms).toHaveLength(2);
    expect(toasts).toHaveLength(0);
  });

  it('drops the bypass when the next answer is an envelope, and says so', async () => {
    /* private → shared → (still private enough to write). The bypass was
     * granted, then the envelope came back and the confirmed attempt cannot
     * carry force_unsafe — Gate A refuses that combination outright. So the
     * write that landed did NOT bypass anything, and announcing a bypass
     * would describe a request that was never sent.
     */
    const { window, toasts, patches } = await bootEdit({
      responses: [redaction403('user'), SHARED_ENVELOPE, SAVED],
      confirmAnswers: [true, true],
    });

    // Caught into a value, not awaited bare: a regression here is a throw,
    // and an escaping rejection is formatted across the JSDOM boundary,
    // where a source-map read aborts the whole run and leaves this case
    // reported as pending rather than failed.
    const resp = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(resp).toEqual(SAVED);
    expect(patches).toHaveLength(3);
    expect(patches[1]).toEqual({ ...BODY, force_unsafe: true });
    expect(patches[2]).toEqual({ ...BODY, confirm_project_shared: true });
    expect(toasts).toHaveLength(0);
  });

  /* Every hop can re-scope, so the exchange is bounded at four attempts —
   * the longest flow that ends in a write, plus one more flip. Both arms end
   * on the fourth answer, one on each prompt site, because the rule is
   * "stop before the question", not "stop after the answer": a dialog whose
   * answer can only be discarded asks for an authorisation this helper
   * cannot act on. Hence three dialogs for four requests, asserted on both
   * arms — a cap that merely bounded the requests would leave the fourth
   * dialog free to appear.
   */
  const CAP_ARMS = [
    ['a redaction refusal', [
      SHARED_ENVELOPE, redaction403('user'), SHARED_ENVELOPE, redaction403('user'), SAVED,
    ]],
    ['an envelope', [
      redaction403('user'), SHARED_ENVELOPE, redaction403('user'), SHARED_ENVELOPE, SAVED,
    ]],
  ];

  it.each(CAP_ARMS)(
    'stops instead of looping when the tier keeps changing, last answer being %s',
    async (_label, responses) => {
      const { window, confirms, toasts, patches } = await bootEdit({
        // One more answer than can be asked for, so an extra prompt would be
        // answerable and therefore visible in the count below.
        responses,
        confirmAnswers: [true, true, true, true, true],
      });

      const err = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

      expect(patches).toHaveLength(4);
      expect(confirms).toHaveLength(3);
      expect(err.message).toBe(window.I18N.t('toast.chunk_save_tier_kept_changing'));
      // A missing locale entry makes ``t()`` echo the key; the message the
      // caller toasts has to be prose.
      expect(err.message).not.toBe('toast.chunk_save_tier_kept_changing');
      expect(toasts).toHaveLength(0);
    },
  );

  it('applies the same rule on the first hop, before any tier is known', async () => {
    /* The rule is uniform across hops, not stricter after the first. Before
     * #2332 the opening request went through ``apiWithRedactionRetry``,
     * which offers the bypass on any refusal, so an unscoped one was offered
     * it here and refused it two hops later. This route always reports a
     * scope (pinned server-side for all three tiers), so the case is
     * unreachable in production — it is pinned because the comment above
     * claims it, not because a user can reach it.
     */
    const { window, confirms, patches } = await bootEdit({
      responses: [redaction403(undefined)],
      confirmAnswers: [true],
    });

    const err = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(err.name).toBe('RedactionBlockedError');
    expect(patches).toHaveLength(1);
    expect(confirms).toHaveLength(0);
  });

  /* The cap bounds the exchange; it does not shorten it. Every terminal
   * outcome is decided from the fourth response *before* the guard is
   * consulted, so a save that lands on the fourth request still lands and a
   * refusal on it is still reported in its own words. Without these the
   * guard could be hoisted ahead of the classification and quietly become a
   * cap of three — measured: that mutation leaves the two arms above green.
   */
  const UPTO_THREE_PROMPTS = [SHARED_ENVELOPE, redaction403('user'), SHARED_ENVELOPE];
  const UNKNOWN_CONFIRM = {
    status: 'needs_confirmation',
    confirm: 'confirm_something_this_build_cannot_answer',
    reason: 'a consent a later server knows about and this client does not',
  };

  const FOURTH_RESPONSE_OUTCOMES = [
    ['a write, which lands', SAVED, (out) => {
      expect(out).toEqual(SAVED);
    }],
    ['a shared-tier refusal, reported as one', REDACTION_403, (out) => {
      expect(out.name).toBe('ProjectTierBlockedError');
    }],
    ['a refusal on a tier we do not know, rethrown raw', redaction403('project_archived'),
      (out) => {
        expect(out.name).toBe('RedactionBlockedError');
        expect(out.scope).toBe('project_archived');
      }],
    ['an envelope we cannot answer, failing loudly', UNKNOWN_CONFIRM, (out) => {
      expect(out.name).toBe('Error');
      expect(out.message).toBe(`unsupported confirmation: ${UNKNOWN_CONFIRM.confirm}`);
    }],
  ];

  it.each(FOURTH_RESPONSE_OUTCOMES)(
    'lets the fourth response decide the outcome — %s',
    async (_label, fourth, check) => {
      const { window, confirms, toasts, patches } = await bootEdit({
        responses: [...UPTO_THREE_PROMPTS, fourth],
        confirmAnswers: [true, true, true, true],
      });

      const out = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

      // Three prompts got us to the fourth request, so the cap was in reach
      // — and did not take it.
      expect(patches).toHaveLength(4);
      expect(confirms).toHaveLength(3);
      expect(out && out.message).not.toBe(
        window.I18N.t('toast.chunk_save_tier_kept_changing'),
      );
      check(out);
      // Nothing here used a bypass on the write that landed.
      expect(toasts).toHaveLength(0);
    },
  );

  it('does not offer a bypass on a tier it does not recognise', async () => {
    /* The companion to the unreported case below: a scope this build has
     * no rule for is unknown, exactly as an absent one is. Treating it as
     * "not shared, therefore private" would offer a bypass on a tier that
     * may refuse it.
     */
    const { window, confirms } = await bootEdit({
      responses: [SHARED_ENVELOPE, redaction403('project_archived')],
      confirmAnswers: [true, true],
    });

    const err = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(err.name).toBe('RedactionBlockedError');
    expect(err.scope).toBe('project_archived');
    // Only the shared-tier disclosure; a second answer was staged so a
    // stray bypass prompt would be answerable and therefore visible.
    expect(confirms).toHaveLength(1);
  });

  it('does not assert a tier the server declined to report', async () => {
    /* A surface that sends no scope leaves the tier unknown. Unknown is
     * neither "shared" — asserting that would put the false claim back —
     * nor "private": offering a bypass there would be the mirror error.
     */
    const { window, confirms } = await bootEdit({
      responses: [SHARED_ENVELOPE, redaction403(undefined)],
      confirmAnswers: [true, true],
    });

    const err = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(err.name).toBe('RedactionBlockedError');
    expect(err.scope).toBeUndefined();
    expect(confirms).toHaveLength(1);
  });
});

describe('chunk edit — one save conversation per chunk (#2340)', () => {
  /* Nothing stops a second save for the same chunk starting while the first is
   * still going: the inline editor's Cancel only removes the edit area and
   * never consults the save in flight, `_startChunkEdit`'s guard is per card,
   * and the detail pane's ``btnLoading`` disables its own button and nothing
   * else. The reachable window is the stretch BEFORE a dialog opens — a PATCH
   * in flight, nothing inert yet — since once a dialog is up the background is
   * inert and the editor's own buttons cannot be clicked. Two conversations
   * each answer the tier THEIR request was judged on, and their writes race
   * with nothing said.
   *
   * These drive ``saveChunkBody`` directly with a deferred dialog, which is
   * the same overlap without needing the editor DOM. */

  const OTHER_CHUNK_ID = 'a1b2c3d4-0000-4000-8000-000000000002';

  it('refuses a second save for the same chunk while the first is answering', async () => {
    const { window, confirms, patches, answerConfirm } = await bootEdit({
      responses: [SHARED_ENVELOPE],
      confirmAnswers: [],
      deferConfirms: true,
    });

    const first = window.saveChunkBody(CHUNK_ID, BODY);
    await flush(window);
    // The first save is parked on the disclosure dialog, mid-conversation.
    expect(patches).toHaveLength(1);
    expect(confirms).toHaveLength(1);

    const second = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(second.name).toBe('Error');
    expect(second.message).toBe(window.I18N.t('toast.chunk_save_in_flight'));
    // Not the raw key: the refusal has to be sayable to the user, since both
    // call sites render it through toast.save_failed / toast.update_failed.
    expect(second.message).not.toBe('toast.chunk_save_in_flight');
    // Nothing was sent for it, and no second dialog was raised.
    expect(patches).toHaveLength(1);
    expect(confirms).toHaveLength(1);

    // The first conversation is untouched and still completes.
    answerConfirm(true);
    expect(await first).toEqual(SAVED);
    expect(patches).toHaveLength(2);
    expect(patches[1]).toHaveProperty('confirm_project_shared', true);
  });

  it('keeps refusing while the first save is still going', async () => {
    /* The guard and the ``add`` sit OUTSIDE the try/finally on purpose. Move
     * them inside and the refused call's own ``finally`` deletes the key the
     * RUNNING save owns — after which the next call sails through. A test that
     * only refuses once cannot see that: the first refusal still throws. This
     * one refuses twice, so the release-by-the-wrong-caller shows up. */
    const { window, patches, answerConfirm } = await bootEdit({
      responses: [SHARED_ENVELOPE],
      confirmAnswers: [],
      deferConfirms: true,
    });

    const first = window.saveChunkBody(CHUNK_ID, BODY);
    await flush(window);

    const second = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);
    const third = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(second.message).toBe(window.I18N.t('toast.chunk_save_in_flight'));
    expect(third.message).toBe(window.I18N.t('toast.chunk_save_in_flight'));
    expect(patches).toHaveLength(1);

    answerConfirm(true);
    expect(await first).toEqual(SAVED);
  });

  it('does not block a save for a different chunk', async () => {
    const { window, confirms, patches, answerConfirm } = await bootEdit({
      responses: [SHARED_ENVELOPE, SHARED_ENVELOPE],
      confirmAnswers: [],
      deferConfirms: true,
    });

    const first = window.saveChunkBody(CHUNK_ID, BODY);
    const other = window.saveChunkBody(OTHER_CHUNK_ID, BODY);
    await flush(window);

    // Both reached the server and both raised their own disclosure: the guard
    // is per chunk, not a global "one save at a time".
    expect(patches).toHaveLength(2);
    expect(confirms).toHaveLength(2);

    answerConfirm(true);
    answerConfirm(true);
    expect(await first).toEqual(SAVED);
    expect(await other).toEqual(SAVED);
  });

  /* Every exit of the conversation has to release the chunk, not just the one
   * that returns a write — otherwise the first decline or refusal of a session
   * locks that chunk out of editing for the life of the page. A bare
   * ``return`` in place of ``return await`` fails the overlap test above
   * instead: it would release the chunk before the conversation settles. */
  const RELEASE_CASES = [
    ['a write that lands', { responses: [SAVED], confirmAnswers: [] }],
    ['a declined disclosure', { responses: [SHARED_ENVELOPE], confirmAnswers: [false] }],
    ['a shared-tier refusal', { responses: [REDACTION_403], confirmAnswers: [] }],
    ['a tier that kept changing', {
      responses: [SHARED_ENVELOPE, redaction403('user'), SHARED_ENVELOPE, SHARED_ENVELOPE],
      confirmAnswers: [true, true, true, true],
    }],
  ];

  it.each(RELEASE_CASES)('releases the chunk after %s', async (_label, args) => {
    const { window, patches } = await bootEdit(args);

    await window.saveChunkBody(CHUNK_ID, BODY).catch(() => {});
    const sentByTheFirst = patches.length;

    const out = await window.saveChunkBody(CHUNK_ID, BODY).catch((e) => e);

    expect(out && out.message).not.toBe(window.I18N.t('toast.chunk_save_in_flight'));
    expect(patches).toHaveLength(sentByTheFirst + 1);
  });
});

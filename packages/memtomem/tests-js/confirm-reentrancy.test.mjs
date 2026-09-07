/* #2340 — one confirm dialog at a time.
 *
 * ``#confirm-modal`` is a singleton: one title, one message, one pair of
 * buttons. A second ``showConfirm`` while a dialog is on screen therefore
 * cannot be *shown* — it can only overwrite the first one's text and take its
 * buttons over. Assigning ``onclick`` did exactly that, so an OK/Cancel click
 * settled only the LATER promise and the earlier caller's ``await`` was left
 * pending forever.
 *
 * The stranded promise is not the worst of it. That caller's ``releaseA11y``
 * never ran either, so its entry stayed in ``_ACTIVE_MODALS``; once the visible
 * dialog was answered and ``cleanup()`` hid the modal, the stack still held a
 * phantom entry for an element that is now ``hidden``, and
 * ``_recomputeBackgroundInert()`` kept ``inert`` on every OTHER ``<body>``
 * child. The page froze with nothing on screen to explain why.
 *
 * These pin both halves of the fix:
 *
 *   - overlapping calls are serialized, each dialog carrying its own text and
 *     settling its own caller;
 *   - the background is left clean once every dialog has been answered;
 *   - the buttons are wired per invocation rather than by assignment, so one
 *     dialog's resolution cannot be replaced by another's even if the queue
 *     were bypassed;
 *   - a dialog that rejects does not wedge the queue behind it, and the
 *     synchronous open is back by the time the previous caller resumes.
 *
 * A stranded promise must never become a test timeout, so nothing here awaits
 * a promise that may not settle: both are tagged into ``settled`` via
 * ``.then`` and the ARRAY is asserted on after a flush.
 *
 * Two jsdom limits bound what these can claim. jsdom stores ``inert`` as an
 * attribute and implements none of its behaviour, and it does not stop a
 * ``.click()`` on a ``hidden`` element. So these pin that the BOOKKEEPING is
 * right — an empty modal stack, no stray ``inert``, the right caller settled —
 * which is the fix's actual contract. They cannot demonstrate that a real
 * browser's page is dead, only that we would have told it to be.
 *
 * Run from packages/memtomem/tests-js (a repo-root run collects stale worktree
 * copies of the static modules).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 40) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

describe('showConfirm — one dialog at a time (#2340)', () => {
  let window;
  let qs;

  beforeEach(async () => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js'] }));
    await window.I18N.init();
    qs = (id) => window.document.getElementById(id);
  });

  /** Tag each settlement so a promise that never settles is an absent row,
   *  not a hung test. */
  function track(settled, tag, promise) {
    promise.then(
      (v) => settled.push([tag, v]),
      (e) => settled.push([tag, 'REJECTED: ' + e.message]),
    );
    return promise;
  }

  it('holds the second dialog back and settles the first caller on its own click', async () => {
    const settled = [];
    track(settled, 'first', window.showConfirm({ title: 'First', confirmText: 'Yes' }));
    track(settled, 'second', window.showConfirm({ title: 'Second', confirmText: 'Also yes' }));
    await flush(window);

    // Only the first is on screen: the second must not have overwritten it.
    expect(qs('confirm-title').textContent).toBe('First');
    expect(settled).toHaveLength(0);

    qs('confirm-ok-btn').click();
    await flush(window);

    // The click answered the dialog that was showing — the FIRST caller.
    expect(settled).toEqual([['first', true]]);
    // And only now does the queued one open.
    expect(qs('confirm-title').textContent).toBe('Second');
    expect(qs('confirm-modal').hidden).toBe(false);

    qs('confirm-cancel-btn').click();
    await flush(window);

    expect(settled).toEqual([['first', true], ['second', false]]);
  });

  it('leaves no modal on the stack and nothing inert once both are answered', async () => {
    /* The freeze pin. Pre-fix the first caller's release never ran, so a
     * phantom ``_ACTIVE_MODALS`` entry survived for an element that is now
     * hidden — and every other <body> child stayed inert with no dialog on
     * screen to explain it. */
    const settled = [];
    track(settled, 'first', window.showConfirm({ title: 'First' }));
    track(settled, 'second', window.showConfirm({ title: 'Second' }));
    await flush(window);

    qs('confirm-ok-btn').click();
    await flush(window);
    qs('confirm-ok-btn').click();
    await flush(window);

    expect(settled).toEqual([['first', true], ['second', true]]);
    expect(window.isAnyModalOpen()).toBe(false);
    expect(qs('confirm-modal').hidden).toBe(true);
    const inert = Array.from(window.document.body.children)
      .filter((el) => el.hasAttribute('inert'))
      .map((el) => el.id || el.tagName.toLowerCase());
    expect(inert).toEqual([]);
  });

  it('gives each queued dialog its own text rather than the last caller wins', async () => {
    const settled = [];
    track(settled, 'first', window.showConfirm({
      title: 'Delete source',
      message: 'first message',
      warningText: 'first warning',
      confirmText: 'Delete',
      cancelText: 'Keep',
    }));
    track(settled, 'second', window.showConfirm({
      title: 'Sync scope',
      message: 'second message',
      confirmText: 'Sync',
      danger: false,
    }));
    await flush(window);

    expect(qs('confirm-title').textContent).toBe('Delete source');
    expect(qs('confirm-message').textContent).toBe('first message');
    expect(qs('confirm-warning').textContent).toBe('first warning');
    expect(qs('confirm-warning').hidden).toBe(false);
    expect(qs('confirm-ok-btn').textContent).toBe('Delete');
    expect(qs('confirm-ok-btn').className).toBe('btn-danger');
    expect(qs('confirm-cancel-btn').textContent).toBe('Keep');

    qs('confirm-ok-btn').click();
    await flush(window);

    expect(qs('confirm-title').textContent).toBe('Sync scope');
    expect(qs('confirm-message').textContent).toBe('second message');
    expect(qs('confirm-ok-btn').textContent).toBe('Sync');
    expect(qs('confirm-ok-btn').className).toBe('btn-primary');
    // The first dialog's custom labels did not survive into the second, and
    // its warning line was reset rather than carried over.
    expect(qs('confirm-cancel-btn').textContent).toBe(window.t('modal.cancel_btn'));
    expect(qs('confirm-warning').hidden).toBe(true);

    qs('confirm-ok-btn').click();
    await flush(window);
    expect(settled).toEqual([['first', true], ['second', true]]);
  });

  it('wires the buttons per invocation instead of assigning onclick', async () => {
    /* Pins the mechanism the issue names, independently of the queue: an
     * assignment is a single slot on a single reused element, so a second
     * dialog would silently replace this one's handlers. */
    const settled = [];
    track(settled, 'only', window.showConfirm({ title: 'Probe' }));
    await flush(window);

    expect(qs('confirm-ok-btn').onclick).toBe(null);
    expect(qs('confirm-cancel-btn').onclick).toBe(null);

    qs('confirm-ok-btn').click();
    await flush(window);
    expect(settled).toEqual([['only', true]]);

    // The listeners came off in cleanup(), so a stray click on the hidden
    // dialog settles nothing further.
    qs('confirm-ok-btn').click();
    qs('confirm-cancel-btn').click();
    await flush(window);
    expect(settled).toEqual([['only', true]]);
  });

  it('does not wedge the queue when a dialog rejects', async () => {
    /* ``showConfirm`` counts a dialog as pending before it runs, so a throw
     * that escapes synchronously would leave the counter stuck above zero and
     * every later dialog queued behind a promise that never settles. The
     * options are destructured inside the executor so any throw becomes a
     * rejection the chain settles on. */
    const settled = [];
    const title = qs('confirm-title');
    title.remove();

    track(settled, 'broken', window.showConfirm({ title: 'Never shown' }));
    await flush(window);
    expect(settled).toHaveLength(1);
    expect(settled[0][0]).toBe('broken');
    expect(String(settled[0][1])).toMatch(/^REJECTED: /);

    // Put the element back and confirm the next dialog still opens.
    qs('confirm-modal').querySelector('.modal-header').appendChild(title);
    track(settled, 'after', window.showConfirm({ title: 'Opens anyway' }));
    await flush(window);

    expect(qs('confirm-title').textContent).toBe('Opens anyway');
    expect(qs('confirm-modal').hidden).toBe(false);
    qs('confirm-ok-btn').click();
    await flush(window);
    expect(settled[1]).toEqual(['after', true]);
  });

  it('does not let a queued dialog inherit the previous extra-option row', async () => {
    /* The sharpest case of field bleed, because the row is a consent: the
     * hooks-promote confirm renders "delete the original" here. A second call
     * used to run its own setup — `extraRow.hidden = true` — over the row the
     * user was reading, before they could tick it. It could only ever DROP an
     * opt-in, never manufacture one (cleanup computes `extraOption && ok`), but
     * dropping one silently is still the wrong answer to a question we asked. */
    const settled = [];
    track(settled, 'withExtra', window.showConfirm({
      title: 'Promote hooks',
      confirmText: 'Promote',
      extraOption: { id: 'delete_original', label: 'Delete the original', defaultChecked: false },
    }));
    track(settled, 'plain', window.showConfirm({ title: 'Plain', confirmText: 'Go' }));
    await flush(window);

    // The row belongs to the dialog on screen, not to the one queued behind it.
    expect(qs('confirm-extra-row').hidden).toBe(false);
    expect(qs('confirm-extra-label').textContent).toBe('Delete the original');

    qs('confirm-extra-checkbox').checked = true;
    qs('confirm-ok-btn').click();
    await flush(window);

    // The opt-in the user actually ticked came back.
    expect(settled).toEqual([['withExtra', { ok: true, extras: { delete_original: true } }]]);
    // And the plain dialog behind it did not inherit the row.
    expect(qs('confirm-extra-row').hidden).toBe(true);
    expect(qs('confirm-extra-checkbox').checked).toBe(false);

    qs('confirm-ok-btn').click();
    await flush(window);
    expect(settled[1]).toEqual(['plain', true]);
  });

  it('empties the modal stack even when two dialogs share the element', async () => {
    /* The listener swap on its own, with the queue deliberately bypassed by
     * calling the inner helper directly. Per-invocation listeners mean ONE
     * click runs BOTH cleanups, so both releases run and the a11y stack
     * empties — the page cannot be left inert with nothing on screen. Assigning
     * ``onclick`` left the first caller stranded and its entry in
     * ``_ACTIVE_MODALS`` forever. This is why the swap is not redundant with
     * the queue: it is what makes the freeze unreachable. */
    const settled = [];
    track(settled, 'a', window._runConfirmDialog({ title: 'A' }));
    track(settled, 'b', window._runConfirmDialog({ title: 'B' }));
    await flush(window);

    qs('confirm-ok-btn').click();
    await flush(window);

    expect(settled.map(([tag]) => tag).sort()).toEqual(['a', 'b']);
    expect(window.isAnyModalOpen()).toBe(false);
    expect(Array.from(window.document.body.children).filter((el) => el.hasAttribute('inert')))
      .toEqual([]);
  });

  it('hands focus back to the trigger, not to a button inside the hidden dialog', async () => {
    /* ``openModalA11y`` captures ``document.activeElement`` at OPEN time, so a
     * queued dialog opening after its predecessor closed depends on that
     * predecessor's ``releaseA11y()`` having already restored the trigger —
     * otherwise the queued dialog would capture ``#confirm-ok-btn``, which is
     * inside a hidden subtree by then, and focus would fall to <body>.
     * Measured: cleanup() releases before the queue advances, so it does not.
     * Pinned because the ordering is invisible at both call sites. */
    const trigger = qs('settings-btn');
    trigger.focus();

    const settled = [];
    track(settled, 'first', window.showConfirm({ title: 'First' }));
    track(settled, 'second', window.showConfirm({ title: 'Second' }));
    await flush(window);

    qs('confirm-ok-btn').click();
    await flush(window);
    // The queued dialog is up and did NOT capture the previous OK button.
    expect(qs('confirm-title').textContent).toBe('Second');

    qs('confirm-ok-btn').click();
    await flush(window);

    expect(settled).toHaveLength(2);
    expect(window.document.activeElement).toBe(trigger);
  });

    it('opens synchronously again immediately after the previous dialog is answered', async () => {
    /* The fast path has to be back by the time the awaiting caller resumes,
     * or every back-to-back confirm opens one microtask late and the
     * read-the-DOM-on-the-next-line contract silently dies. Nothing in the app
     * would notice; this does. */
    const p1 = window.showConfirm({ title: 'First', cancelText: 'Keep' });
    expect(qs('confirm-title').textContent).toBe('First');
    qs('confirm-cancel-btn').click();
    expect(await p1).toBe(false);

    const p2 = window.showConfirm({ title: 'Second' });
    // Read with NO await in between — this is the whole contract.
    expect(qs('confirm-title').textContent).toBe('Second');
    expect(qs('confirm-modal').hidden).toBe(false);
    expect(qs('confirm-cancel-btn').textContent).toBe(window.t('modal.cancel_btn'));
    qs('confirm-ok-btn').click();
    expect(await p2).toBe(true);
  });
});

describe('modal paint order follows the stack, not the document (#2340)', () => {
  /* Every .modal-overlay shares one z-index, so DOM order decides what paints
   * on top — and #confirm-modal sits EARLIER in index.html than every Context
   * Gateway overlay. The pull and move/copy flows work around that by hiding
   * their own modal around a disclosure and showing it again afterwards, which
   * assumed the confirm was gone by then. Serializing confirms broke that
   * assumption: a queued confirm opens after the restore. Without stack-derived
   * z-index it lands under a modal the user can see, and Esc — which the
   * dispatcher hands to the confirm whenever it is not hidden — answers a
   * dialog that is not visible. */

  let window;
  let qs;

  beforeEach(async () => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js'] }));
    await window.I18N.init();
    qs = (id) => window.document.getElementById(id);
  });

  const z = (id) => Number(qs(id).style.zIndex);

  it('puts a confirm above an overlay that is later in the document', async () => {
    // #ctx-pull-modal is declared after #confirm-modal in index.html, so equal
    // z-index would paint it on top.
    const outer = qs('ctx-pull-modal');
    const release = window.openModal(outer);

    const p = window.showConfirm({ title: 'Disclosure' });
    expect(z('confirm-modal')).toBeGreaterThan(z('ctx-pull-modal'));
    expect(window.isTopModal(qs('confirm-modal'))).toBe(true);

    qs('confirm-ok-btn').click();
    expect(await p).toBe(true);
    // Closed modals carry no stacking of their own.
    expect(qs('confirm-modal').style.zIndex).toBe('');
    expect(window.isTopModal(outer)).toBe(true);
    release();
    expect(outer.style.zIndex).toBe('');
  });

  it('keeps a QUEUED confirm above a modal restored while it waited', async () => {
    /* The sequence the fix is for: an outer flow hides its modal for its own
     * disclosure, a second confirm queues behind that one, the outer flow is
     * declined and shows its modal again — and only then does the queued
     * confirm open. */
    const outer = qs('ctx-pull-modal');
    const release = window.openModal(outer);
    window.hide(outer);  // the flow hides its own modal for the disclosure

    const settled = [];
    const first = window.showConfirm({ title: 'Outer disclosure' });
    first.then((v) => settled.push(['first', v]));
    const second = window.showConfirm({ title: 'Queued behind it' });
    second.then((v) => settled.push(['second', v]));
    await flush(window);

    expect(qs('confirm-title').textContent).toBe('Outer disclosure');
    qs('confirm-cancel-btn').click();   // declined
    await flush(window);

    window.show(outer);                 // the flow restores its modal
    await flush(window);

    // The queued confirm is now open on top of that restored modal, not under
    // it — so Esc reaches the dialog the user can actually see.
    expect(qs('confirm-title').textContent).toBe('Queued behind it');
    expect(qs('confirm-modal').hidden).toBe(false);
    expect(z('confirm-modal')).toBeGreaterThan(z('ctx-pull-modal'));

    qs('confirm-ok-btn').click();
    await flush(window);
    expect(settled).toEqual([['first', false], ['second', true]]);
    release();
  });

  it('renumbers the remaining modals when one in the middle closes', async () => {
    const a = qs('ctx-pull-modal');
    const b = qs('ctx-conflict-modal');
    const releaseA = window.openModal(a);
    const releaseB = window.openModal(b);
    expect(z('ctx-conflict-modal')).toBeGreaterThan(z('ctx-pull-modal'));

    releaseA();
    // B is now the only modal, and must sit at the base rather than keep the
    // depth it had while A was under it.
    expect(a.style.zIndex).toBe('');
    expect(z('ctx-conflict-modal')).toBe(200);
    releaseB();
    expect(b.style.zIndex).toBe('');
  });
});

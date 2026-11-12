/* #3077: saving one source-browser card leaves the other cards' drafts alone.
 *
 * A card save used to finish with ``browseSource(sourcePath, …)``, which
 * rebuilds every card from the server: an editor open on another card, and
 * its unsaved draft, went with it. And because the refresh named the saved
 * card's source, a save that completed after the browser had moved to another
 * source replaced that view, and any draft open in it.
 *
 * The refresh now waits until no editor is open in the browser (either view),
 * runs wherever an editor closes, and only ever refreshes the source the
 * browser still shows. A listing answers only the latest browse.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const A = 'a1b2c3d4-0000-4000-8000-00000000000a';
const B = 'a1b2c3d4-0000-4000-8000-00000000000b';
const C = 'a1b2c3d4-0000-4000-8000-00000000000c';
const D = 'a1b2c3d4-0000-4000-8000-00000000000d';
const NOTE = '/m/note.md';
const OTHER = '/m/other.md';
const hashOf = (id) => id.slice(-1).repeat(64);

function chunk(id, path, start) {
  return {
    id, content: `${id.slice(-1)} indexed`, source_file: path, chunk_type: 'markdown_section',
    start_line: start, end_line: start + 2, heading_hierarchy: [], tags: [],
    namespace: 'default',
  };
}

const NOTE_CHUNKS = () => [chunk(A, NOTE, 1), chunk(B, NOTE, 4)];
const OTHER_CHUNKS = () => [chunk(C, OTHER, 1), chunk(D, OTHER, 4)];

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const fail = () => ({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) });
const settle = async (window) => {
  for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));
};
function gate() {
  let release;
  const p = new Promise((r) => { release = r; });
  return { p, release };
}

// ``listing(path, n)`` answers the n-th listing of ``path`` (0-based): an array
// of chunks, a promise of one, or ``fail()``. ``source(id)`` and ``patch(id)``
// default to an immediate success.
async function boot({ listing = null, source = null, patch = null } = {}) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
  const { window } = dom;
  window.showToast = () => {};
  window.ensureCsrfToken = async () => 'test-token';
  window.showConfirm = async () => true;
  const listings = [];
  const patches = [];
  const deletes = [];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const method = (opts && opts.method) || 'GET';
    const src = url.match(/\/api\/chunks\/([^/?]+)\/source$/);
    if (src && method === 'GET') {
      const custom = source && (await source(src[1]));
      return custom || ok({ text: `${src[1]} source`, base_hash: hashOf(src[1]) });
    }
    const list = url.match(/\/api\/chunks\?source=([^&]+)&limit=(\d+)/);
    if (list && method === 'GET') {
      const path = decodeURIComponent(list[1]);
      const n = listings.filter((l) => l.path === path).length;
      listings.push({ path, limit: Number(list[2]) });
      const answer = await (listing
        ? listing(path, n)
        : (path === NOTE ? NOTE_CHUNKS() : OTHER_CHUNKS()));
      if (!Array.isArray(answer)) return answer;
      return ok({ chunks: answer, total: answer.length });
    }
    const one = url.match(/\/api\/chunks\/([^/?]+)$/);
    if (one && method === 'PATCH') {
      patches.push({ id: one[1], body: JSON.parse(opts.body) });
      return (patch && (await patch(one[1]))) || ok({ id: one[1] });
    }
    if (one && method === 'DELETE') {
      deletes.push(one[1]);
      return ok({ deleted: 1 });
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  return { window, listings, patches, deletes };
}

const doc = (window) => window.document;
const card = (window, id) =>
  doc(window).querySelector(`#chunks-browser .chunk-card[data-chunk-id="${id}"]`);
const block = (window, id) =>
  doc(window).querySelector(`#chunks-browser .doc-chunk-block[data-chunk-id="${id}"]`);
const editorIn = (el) => el.querySelector(':scope > .chunk-card-edit-area');
const textarea = (el) => editorIn(el).querySelector('textarea');
const saveBtn = (el) => editorIn(el).querySelector('.chunk-card-edit-actions .btn-primary');
const cancelBtn = (el) => editorIn(el).querySelector('.chunk-card-edit-actions .btn-ghost');
const notice = (window) => doc(window).querySelector('#chunks-browser .chunks-refresh-held');
const shownPath = (window) => doc(window).querySelector('#chunks-browser .file-path')?.textContent;

async function openCard(window, id, draft) {
  card(window, id).querySelector('.card-edit-btn').click();
  await settle(window);
  if (draft !== undefined) textarea(card(window, id)).value = draft;
  return textarea(card(window, id));
}

async function browse(window, path, limit) {
  await window.browseSource(path, limit);
  await settle(window);
}

describe('a card save keeps other drafts (#3077)', () => {
  it('saving card A leaves the open editor and draft in card B', async () => {
    const { window, listings, patches } = await boot();
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    const editorB = await openCard(window, B, 'B draft');

    saveBtn(card(window, A)).click();
    await settle(window);

    expect(patches.map((p) => p.id)).toEqual([A]);
    expect(listings).toHaveLength(1);  // no refresh while B is open
    expect(editorB.isConnected).toBe(true);
    expect(textarea(card(window, B))).toBe(editorB);
    expect(editorB.value).toBe('B draft');
    // A's session is closed, and the browser says why it has not refreshed.
    expect(editorIn(card(window, A))).toBe(null);
    expect(notice(window).textContent).toBe(window.t('chunks.refresh_held'));
  });

  it("disables the saved chunk's controls and Copy All, in both views", async () => {
    const { window } = await boot();
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    await openCard(window, B, 'B draft');
    saveBtn(card(window, A)).click();
    await settle(window);

    for (const sel of ['.card-copy-btn', '.card-edit-btn', '.card-delete-btn']) {
      expect(card(window, A).querySelector(sel).disabled).toBe(true);
      expect(card(window, B).querySelector(sel).disabled).toBe(false);
    }
    expect(block(window, A).querySelector('.doc-chunk-edit-btn').disabled).toBe(true);
    expect(block(window, B).querySelector('.doc-chunk-edit-btn').disabled).toBe(false);
    // Copy All joins A's pre-save text.
    expect(doc(window).querySelector('#chunks-browser .document-copy-btn').disabled).toBe(true);
  });

  it('the notice sits above both views, as a status, and follows the language', async () => {
    const { window } = await boot();
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    await openCard(window, B, 'B draft');
    saveBtn(card(window, A)).click();
    await settle(window);

    const n = notice(window);
    expect(n.getAttribute('role')).toBe('status');
    expect(n.closest('.document-view')).toBe(null);
    expect(n.previousElementSibling.classList.contains('chunks-browser-header')).toBe(true);
    const english = n.textContent;
    expect(english).toMatch(/^Saved\./);
    await window.I18N.setLang('ko');
    // Not the English fallback ``t`` gives a key missing from ko.json.
    expect(n.textContent).not.toBe(english);
    expect(n.textContent).toBe(window.t('chunks.refresh_held'));
    await window.I18N.setLang('en');
    expect(n.textContent).toBe(english);
  });

  it('the notice enters the page empty and is filled after, so it is announced', async () => {
    const { window } = await boot();
    await browse(window, NOTE);
    window._showRefreshHeldNotice();
    expect(notice(window).textContent).toBe('');
    await settle(window);
    expect(notice(window).textContent).toBe(window.t('chunks.refresh_held'));
  });

  it('closing one of several open editors keeps a single notice', async () => {
    const { window, listings } = await boot();
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    await openCard(window, B, 'B draft');
    block(window, B).querySelector('.doc-chunk-edit-btn').click();  // a second editor on B
    await settle(window);
    saveBtn(card(window, A)).click();
    await settle(window);

    cancelBtn(card(window, B)).click();  // held again: the Document editor is open
    await settle(window);
    expect(listings).toHaveLength(1);
    expect(doc(window).querySelectorAll('#chunks-browser .chunks-refresh-held')).toHaveLength(1);
  });

  it('a save with no other editor open refreshes at once, with no note', async () => {
    const { window, listings } = await boot();
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    saveBtn(card(window, A)).click();
    await settle(window);

    expect(listings).toEqual([{ path: NOTE, limit: 100 }, { path: NOTE, limit: 100 }]);
    expect(notice(window)).toBe(null);
    expect(card(window, A).querySelector('.card-edit-btn').disabled).toBe(false);
    expect(doc(window).querySelector('#chunks-browser .document-copy-btn').disabled).toBe(false);
  });
});

describe('the held refresh runs when the last editor closes (#3077)', () => {
  async function savedAWithBOpen(opts = {}) {
    const env = await boot(opts);
    await browse(env.window, NOTE);
    await openCard(env.window, A, 'A edited');
    await openCard(env.window, B, 'B draft');
    saveBtn(card(env.window, A)).click();
    await settle(env.window);
    expect(env.listings).toHaveLength(1);
    return env;
  }

  it('on Cancel', async () => {
    const { window, listings } = await savedAWithBOpen();
    cancelBtn(card(window, B)).click();
    await settle(window);
    expect(listings).toEqual([{ path: NOTE, limit: 100 }, { path: NOTE, limit: 100 }]);
    expect(doc(window).querySelector('.chunk-card-saved')).toBe(null);
    expect(notice(window)).toBe(null);
  });

  it('on a Save with unchanged text', async () => {
    const { window, listings, patches } = await savedAWithBOpen();
    textarea(card(window, B)).value = `${B} source`;
    saveBtn(card(window, B)).click();
    await settle(window);
    expect(patches).toHaveLength(1);
    expect(listings).toHaveLength(2);
  });

  it("on B's own save, which sends B's id and base", async () => {
    const { window, listings, patches } = await savedAWithBOpen();
    saveBtn(card(window, B)).click();
    await settle(window);
    expect(patches[1]).toEqual({ id: B, body: { new_content: 'B draft', base_hash: hashOf(B) } });
    expect(listings).toHaveLength(2);
  });

  it("on Delete of the card holding the editor", async () => {
    const { window, listings, deletes } = await savedAWithBOpen();
    card(window, B).querySelector('.card-delete-btn').click();
    await settle(window);
    expect(deletes).toEqual([B]);
    expect(listings).toHaveLength(2);
  });

  it("on a source-load failure of the last editor", async () => {
    const slowB = gate();
    const { window, listings } = await boot({
      source: (id) => (id === B ? slowB.p.then(() => fail()) : null),
    });
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    card(window, B).querySelector('.card-edit-btn').click();  // loading, never editable
    await settle(window);
    saveBtn(card(window, A)).click();
    await settle(window);
    expect(listings).toHaveLength(1);

    slowB.release();
    await settle(window);
    expect(editorIn(card(window, B))).toBe(null);
    expect(listings).toHaveLength(2);
  });

  it('an editor in the Document view holds the refresh too', async () => {
    const { window, listings } = await boot();
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    block(window, B).querySelector('.doc-chunk-edit-btn').click();
    await settle(window);
    const docEditor = textarea(block(window, B));
    docEditor.value = 'B doc draft';

    saveBtn(card(window, A)).click();
    await settle(window);
    expect(listings).toHaveLength(1);
    expect(docEditor.isConnected).toBe(true);
    expect(docEditor.value).toBe('B doc draft');

    cancelBtn(block(window, B)).click();
    await settle(window);
    expect(listings).toHaveLength(2);
  });

  it('overlapping saves refresh once, after the second completes', async () => {
    const gates = { [A]: gate(), [B]: gate() };
    const { window, listings } = await boot({
      patch: async (id) => { await gates[id].p; return null; },
    });
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    await openCard(window, B, 'B edited');
    saveBtn(card(window, A)).click();
    saveBtn(card(window, B)).click();
    await settle(window);

    gates[A].release();
    await settle(window);
    expect(listings).toHaveLength(1);
    gates[B].release();
    await settle(window);
    expect(listings).toHaveLength(2);
  });

  it('follows the Load All limit', async () => {
    const { window, listings } = await boot();
    await browse(window, NOTE, 500);
    await openCard(window, A, 'A edited');
    saveBtn(card(window, A)).click();
    await settle(window);
    expect(listings).toEqual([{ path: NOTE, limit: 500 }, { path: NOTE, limit: 500 }]);
  });
});

describe('a save never refreshes a view it did not come from (#3077)', () => {
  it('a save completing after the browser moved does not replace that view', async () => {
    const slow = gate();
    const { window, listings } = await boot({
      patch: async () => { await slow.p; return null; },
    });
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    saveBtn(card(window, A)).click();
    await settle(window);

    await browse(window, OTHER);
    const editorC = await openCard(window, C, 'C draft');
    slow.release();
    await settle(window);

    expect(shownPath(window)).toBe(OTHER);
    expect(editorC.isConnected).toBe(true);
    expect(editorC.value).toBe('C draft');
    expect(listings.map((l) => l.path)).toEqual([NOTE, OTHER]);
  });

  it("a NOTE save completing leaves OTHER's held refresh in place", async () => {
    const slowA = gate();
    const { window, listings } = await boot({
      patch: async (id) => { if (id === A) await slowA.p; return null; },
    });
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    saveBtn(card(window, A)).click();
    await settle(window);

    await browse(window, OTHER);
    await openCard(window, C, 'C edited');
    await openCard(window, D, 'D draft');
    saveBtn(card(window, C)).click();  // held: D is open
    await settle(window);
    slowA.release();
    await settle(window);
    expect(window.STATE.browserRefreshPending).toBe(OTHER);

    cancelBtn(card(window, D)).click();
    await settle(window);
    expect(listings.map((l) => l.path)).toEqual([NOTE, OTHER, OTHER]);
  });

  it('Save, Cancel, reopen: the first save completing keeps the new session', async () => {
    const slow = gate();
    const { window, listings } = await boot({
      patch: async () => { await slow.p; return null; },
    });
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    saveBtn(card(window, A)).click();
    await settle(window);
    cancelBtn(card(window, A)).click();
    const second = await openCard(window, A, 'A second draft');

    slow.release();
    await settle(window);
    expect(second.isConnected).toBe(true);
    expect(second.value).toBe('A second draft');
    expect(listings).toHaveLength(1);
  });

  for (const outcome of ['resolves', 'rejects']) {
    it(`a refresh listing that ${outcome} after navigating does not replace the view`, async () => {
      const slow = gate();
      const { window } = await boot({
        listing: (path, n) => {
          if (path === OTHER) return OTHER_CHUNKS();
          if (n === 0) return NOTE_CHUNKS();
          return slow.p.then(() => (outcome === 'resolves' ? NOTE_CHUNKS() : fail()));
        },
      });
      await browse(window, NOTE);
      await openCard(window, A, 'A edited');
      saveBtn(card(window, A)).click();  // refresh starts, its listing held
      await settle(window);

      await browse(window, OTHER);
      const editorC = await openCard(window, C, 'C draft');
      slow.release();
      await settle(window);

      expect(shownPath(window)).toBe(OTHER);
      expect(editorC.isConnected).toBe(true);
      expect(doc(window).querySelector('#chunks-browser .empty-state')).toBe(null);
    });
  }

  it('hideBrowser drops a held refresh and a listing still in flight', async () => {
    const slow = gate();
    const { window } = await boot({
      listing: (path, n) => (n === 0 ? NOTE_CHUNKS() : slow.p.then(() => NOTE_CHUNKS())),
    });
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    await openCard(window, B, 'B draft');
    saveBtn(card(window, A)).click();
    await settle(window);
    expect(window.STATE.browserRefreshPending).toBe(NOTE);

    window.hideBrowser();
    expect(window.STATE.browserRefreshPending).toBe(null);
    window.browseSource(NOTE);  // a listing in flight when the browser is hidden
    await settle(window);
    window.hideBrowser();
    slow.release();
    await settle(window);
    expect(doc(window).querySelector('#chunks-browser .chunk-card')).toBe(null);
  });

  it('a same-source browse that fails clears the held refresh', async () => {
    const { window } = await boot({
      listing: (path, n) => (n === 0 ? NOTE_CHUNKS() : fail()),
    });
    await browse(window, NOTE);
    await openCard(window, A, 'A edited');
    await openCard(window, B, 'B draft');
    saveBtn(card(window, A)).click();
    await settle(window);
    expect(window.STATE.browserRefreshPending).toBe(NOTE);

    await browse(window, NOTE, 500);  // Load All, answered with an error
    expect(doc(window).querySelector('#chunks-browser .empty-state')).not.toBe(null);
    expect(window.STATE.browserRefreshPending).toBe(null);
  });
});

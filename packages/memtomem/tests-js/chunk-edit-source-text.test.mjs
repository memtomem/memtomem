/* #3071: the chunk editors edit the span's source text, not indexed content.
 *
 * Indexed content is a projection of the span (resolved wikilinks, merged
 * sections with rebuilt heading labels, a frontmatter preamble without the
 * heading it was merged into); saving an edit of it rewrote the span from text
 * that did not hold all of its lines. The detail editor therefore loads
 * ``GET /api/chunks/{id}/source`` on Edit and saves with its ``base_hash``.
 *
 * These pin the session rules:
 *   - Edit loads the source text into the editor and Save sends its hash;
 *   - a response for a chunk no longer selected never fills the editor;
 *   - View → Edit keeps a draft instead of fetching over it;
 *   - a stale base (409 ``edit_base_mismatch``) keeps the draft and blocks
 *     another save of it;
 *   - a successful save closes the panel rather than keep a selection the
 *     re-index may have retired.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const A = 'a1b2c3d4-0000-4000-8000-00000000000a';
const B = 'a1b2c3d4-0000-4000-8000-00000000000b';
const HASH_A = 'a'.repeat(64);
const SOURCE_A = '---\ntags: [x]\n---\n\n# Cache policy\n\nRedis LRU eviction.';

function result(id, content) {
  return {
    chunk: {
      id, content, source_file: '/m/note.md', chunk_type: 'markdown_section',
      start_line: 1, end_line: 7, heading_hierarchy: ['# Cache policy'], tags: [],
      namespace: 'default',
    },
    score: 0.5, rank: 1, source: 'bm25',
  };
}

async function boot({ sources = {}, patch = null } = {}) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
  const { window } = dom;
  const toasts = [];
  window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });
  window.ensureCsrfToken = async () => 'test-token';
  const gets = [];
  const patches = [];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const m = url.match(/\/api\/chunks\/([^/?]+)\/source$/);
    if (m && (!opts || !opts.method || opts.method === 'GET')) {
      gets.push(m[1]);
      return sources[m[1]]();
    }
    if (url.includes('/api/chunks/') && opts && opts.method === 'PATCH') {
      patches.push(JSON.parse(opts.body));
      return patch ? patch() : { ok: true, status: 200, json: async () => ({ id: A }) };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  return { window, toasts, gets, patches };
}

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const settle = async (window) => {
  for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));
};

describe('detail editor edits source text (#3071)', () => {
  it('loads the source on Edit and saves with its hash', async () => {
    const { window, gets, patches } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A, start_line: 1, end_line: 7 }) },
    });
    window.showDetail(result(A, 'indexed projection'));
    const editor = window.document.getElementById('d-editor');
    expect(editor.value).toBe('indexed projection');

    window._setDetailMode('edit');
    await settle(window);
    expect(gets).toEqual([A]);
    expect(editor.value).toBe(SOURCE_A);
    expect(editor.readOnly).toBe(false);

    editor.value = SOURCE_A.replace('LRU', 'LFU');
    window.document.getElementById('d-save-btn').click();
    await settle(window);
    expect(patches).toEqual([{ new_content: SOURCE_A.replace('LRU', 'LFU'), base_hash: HASH_A }]);
    // Closed rather than left on a chunk the re-index may have retired.
    expect(window.STATE.selectedChunkId).toBe(null);
  });

  it('a late source response for a deselected chunk does not fill the editor', async () => {
    let releaseA;
    const gateA = new Promise((r) => { releaseA = r; });
    const { window } = await boot({
      sources: {
        [A]: async () => { await gateA; return ok({ text: SOURCE_A, base_hash: HASH_A }); },
      },
    });
    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    window._setDetailMode('view');
    window.showDetail(result(B, 'B indexed'));
    releaseA();
    await settle(window);

    expect(window.document.getElementById('d-editor').value).toBe('B indexed');
    expect(window.STATE.sourceEdit).toBe(null);
  });

  it('View then Edit keeps the draft and does not fetch again', async () => {
    const { window, gets } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }) },
    });
    window.showDetail(result(A, 'indexed'));
    window._setDetailMode('edit');
    await settle(window);
    const editor = window.document.getElementById('d-editor');
    editor.value = 'my draft';

    window._setDetailMode('view');
    window._setDetailMode('edit');
    await settle(window);

    expect(gets).toEqual([A]);
    expect(editor.value).toBe('my draft');
  });

  it('a stale base keeps the draft and blocks saving it again', async () => {
    const mismatch = {
      ok: false,
      status: 409,
      json: async () => ({
        detail: { detail: 'edit_base_mismatch', text: 'changed on disk', base_hash: 'b'.repeat(64) },
      }),
    };
    const { window, toasts, patches } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }) },
      patch: () => mismatch,
    });
    window.showDetail(result(A, 'indexed'));
    window._setDetailMode('edit');
    await settle(window);
    const editor = window.document.getElementById('d-editor');
    editor.value = 'my draft';
    const save = window.document.getElementById('d-save-btn');

    save.click();
    await settle(window);
    expect(editor.value).toBe('my draft');
    expect(toasts).toContainEqual({ msg: window.t('toast.chunk_source_changed'), sev: 'error' });
    expect(save.disabled).toBe(true);

    save.click();
    await settle(window);
    expect(patches).toHaveLength(1);
  });

  it('a save that completes after another chunk was opened leaves that draft alone', async () => {
    let releasePatch;
    const patchGate = new Promise((r) => { releasePatch = r; });
    const { window, gets } = await boot({
      sources: {
        [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }),
        [B]: () => ok({ text: 'B source', base_hash: 'b'.repeat(64) }),
      },
      patch: async () => { await patchGate; return ok({ id: A }); },
    });
    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    await settle(window);
    window.document.getElementById('d-save-btn').click();
    await settle(window);

    window.showDetail(result(B, 'B indexed'));
    window._setDetailMode('edit');
    await settle(window);
    const editor = window.document.getElementById('d-editor');
    editor.value = 'B draft';
    releasePatch();
    await settle(window);

    expect(gets).toEqual([A, B]);
    expect(window.STATE.selectedChunkId).toBe(B);
    expect(editor.value).toBe('B draft');
  });

  it('reopening the same chunk while its save runs keeps the new session', async () => {
    let releasePatch;
    const patchGate = new Promise((r) => { releasePatch = r; });
    const { window, gets } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }) },
      patch: async () => { await patchGate; return ok({ id: A }); },
    });
    window.renderResults([result(A, 'A indexed')], {});
    window._setDetailMode('edit');
    await settle(window);
    window.document.getElementById('d-save-btn').click();
    await settle(window);

    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    await settle(window);
    const editor = window.document.getElementById('d-editor');
    editor.value = 'second draft';
    releasePatch();
    await settle(window);

    expect(gets).toEqual([A, A]);
    expect(window.STATE.selectedChunkId).toBe(A);
    expect(editor.value).toBe('second draft');
    // The saved row is gone: its id may not survive the re-index.
    expect(window.document.querySelector(`.result-item[data-id="${A}"]`)).toBe(null);
    expect(window.STATE.lastResults).toEqual([]);
  });

  it('the editor is read-only while its save is in flight', async () => {
    let releasePatch;
    const patchGate = new Promise((r) => { releasePatch = r; });
    const { window } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }) },
      patch: async () => { await patchGate; return { ok: false, status: 500, json: async () => ({ detail: 'boom' }) }; },
    });
    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    await settle(window);
    const editor = window.document.getElementById('d-editor');
    editor.value = 'draft';
    window.document.getElementById('d-save-btn').click();
    await settle(window);
    expect(editor.readOnly).toBe(true);

    releasePatch();
    await settle(window);
    // A failed save hands the draft back, editable.
    expect(editor.readOnly).toBe(false);
    expect(editor.value).toBe('draft');
  });

  it('a save finishing while another chunk loads leaves that chunk unable to save', async () => {
    let releasePatch;
    const patchGate = new Promise((r) => { releasePatch = r; });
    const { window } = await boot({
      sources: {
        [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }),
        [B]: () => new Promise(() => {}),  // never loads
      },
      patch: async () => { await patchGate; return { ok: false, status: 500, json: async () => ({ detail: 'boom' }) }; },
    });
    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    await settle(window);
    window.document.getElementById('d-save-btn').click();
    await settle(window);
    window.showDetail(result(B, 'B indexed'));
    window._setDetailMode('edit');
    await settle(window);

    releasePatch();
    await settle(window);
    expect(window.document.getElementById('d-save-btn').disabled).toBe(true);
    expect(window.document.getElementById('d-editor').readOnly).toBe(true);
  });

  it('one detail save at a time: another chunk saves only after the first finishes', async () => {
    let releaseA;
    const gateA = new Promise((r) => { releaseA = r; });
    let n = 0;
    const { window, patches } = await boot({
      sources: {
        [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }),
        [B]: () => ok({ text: 'B source', base_hash: 'b'.repeat(64) }),
      },
      patch: async () => {
        n += 1;
        if (n === 1) await gateA;
        return { ok: false, status: 500, json: async () => ({ detail: 'boom' }) };
      },
    });
    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    await settle(window);
    const save = window.document.getElementById('d-save-btn');
    save.click();
    await settle(window);

    window.showDetail(result(B, 'B indexed'));
    window._setDetailMode('edit');
    await settle(window);
    const editor = window.document.getElementById('d-editor');
    expect(save.disabled).toBe(true);
    // The handler refuses too, not just the disabled button: a disabled
    // button never dispatches, so force one through.
    save.disabled = false;
    save.click();
    await settle(window);
    expect(patches).toHaveLength(1);  // B's save did not start
    editor.value = 'B draft';

    releaseA();
    await settle(window);
    // A's cleanup hands B its controls back without touching B's draft.
    expect(save.disabled).toBe(false);
    expect(editor.readOnly).toBe(false);
    expect(editor.value).toBe('B draft');
    save.click();
    await settle(window);
    expect(patches).toHaveLength(2);
    expect(patches[1]).toEqual({ new_content: 'B draft', base_hash: 'b'.repeat(64) });
  });
});

describe('source-browser card editor (#3071)', () => {
  async function saveCard(window, chunkId, text) {
    const card = window.document.createElement('div');
    window.document.body.appendChild(card);
    window._startChunkEdit(card, { id: chunkId, content: 'indexed' }, '/m/note.md');
    await settle(window);
    card.querySelector('.chunk-card-edit-area textarea').value = text;
    card.querySelector('.chunk-card-edit-actions .btn-primary').click();
    await settle(window);
  }

  it('saves the source text with its hash and drops the stale search result', async () => {
    const { window, patches } = await boot({
      sources: {
        [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }),
        [B]: () => ok({ text: 'B source', base_hash: 'b'.repeat(64) }),
      },
    });
    window.renderResults([result(A, 'A indexed'), result(B, 'B indexed')], {});
    window.showDetail(result(B, 'B indexed'));
    window._setDetailMode('edit');
    await settle(window);
    window.document.getElementById('d-editor').value = 'B draft';

    await saveCard(window, A, 'A edited');

    expect(patches).toEqual([{ new_content: 'A edited', base_hash: HASH_A }]);
    // Search no longer offers A's pre-edit text.
    expect(window.STATE.lastResults.map((r) => r.chunk.id)).toEqual([B]);
    expect(window.document.querySelector(`.result-item[data-id="${A}"]`)).toBe(null);
    // B, open with a draft in the detail panel, is left alone.
    expect(window.STATE.selectedChunkId).toBe(B);
    expect(window.document.getElementById('d-editor').value).toBe('B draft');
  });

  it('closes the detail panel showing the saved chunk unless an edit is open there', async () => {
    const { window } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }) },
    });
    window.renderResults([result(A, 'A indexed')], {});
    expect(window.STATE.selectedChunkId).toBe(A);  // auto-selected, view mode

    await saveCard(window, A, 'A edited');

    expect(window.STATE.selectedChunkId).toBe(null);
  });
});

describe('a dropped result leaves the bulk selection (#3071)', () => {
  it('bulk delete never reaches a saved chunk that is no longer listed', async () => {
    const { window } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }) },
    });
    const deletes = [];
    const upstream = window.fetch;
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (opts && opts.method === 'DELETE') { deletes.push(url); return ok({ deleted: 1 }); }
      return upstream(input, opts);
    };
    window.showConfirm = async () => true;
    window.renderResults([result(A, 'A indexed'), result(B, 'B indexed')], {});
    const doc = window.document;
    doc.querySelector(`.result-checkbox[data-id="${A}"]`).click();
    expect(window.STATE.selectedIds.has(A)).toBe(true);

    // Save A unchanged from the detail panel: its row is dropped.
    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    await settle(window);
    doc.getElementById('d-save-btn').click();
    await settle(window);
    expect(doc.querySelector(`.result-item[data-id="${A}"]`)).toBe(null);

    expect([...window.STATE.selectedIds]).toEqual([]);
    const allCb = doc.getElementById('bulk-select-all');
    allCb.checked = false;
    allCb.dispatchEvent(new window.Event('change'));
    doc.getElementById('bulk-delete-btn').click();
    await settle(window);
    expect(deletes).toEqual([]);
  });
});

describe('consumers of search-result state after a save (#3071)', () => {
  // A deferred save, with DELETE calls and confirm dialogs recorded.
  async function bootSweep({ confirmGate = null } = {}) {
    let releasePatch;
    const patchGate = new Promise((r) => { releasePatch = r; });
    const env = await boot({
      sources: {
        [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }),
        [B]: () => ok({ text: 'B source', base_hash: 'b'.repeat(64) }),
      },
      patch: async () => { await patchGate; return ok({ id: A }); },
    });
    const { window } = env;
    const deletes = [];
    const confirms = [];
    const upstream = window.fetch;
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (opts && opts.method === 'DELETE') { deletes.push(url); return ok({ deleted: 1 }); }
      return upstream(input, opts);
    };
    window.showConfirm = async (o) => { confirms.push(o); return confirmGate ? confirmGate : true; };
    return { ...env, deletes, confirms, releasePatch };
  }

  async function startSaveOfA(window) {
    window.showDetail(result(A, 'A indexed'));
    window._setDetailMode('edit');
    await settle(window);
    window.document.getElementById('d-save-btn').click();
    await settle(window);
  }

  it('a bulk delete confirmed after a save dropped its chunk does not delete it', async () => {
    let answer;
    const confirmGate = new Promise((r) => { answer = r; });
    const { window, deletes, releasePatch } = await bootSweep({ confirmGate });
    window.renderResults([result(A, 'A indexed'), result(B, 'B indexed')], {});
    await startSaveOfA(window);
    const doc = window.document;
    doc.querySelector(`.result-checkbox[data-id="${A}"]`).click();
    doc.querySelector(`.result-checkbox[data-id="${B}"]`).click();
    doc.getElementById('bulk-delete-btn').click();  // dialog open, ids captured
    await settle(window);

    releasePatch();  // the save drops A
    await settle(window);
    answer(true);
    await settle(window);

    expect(deletes).toEqual([`/api/chunks/${B}`]);
  });

  it('a detail delete confirmed after the save closed the panel sends nothing', async () => {
    let answer;
    const confirmGate = new Promise((r) => { answer = r; });
    const { window, deletes, releasePatch } = await bootSweep({ confirmGate });
    window.renderResults([result(A, 'A indexed')], {});
    await startSaveOfA(window);
    window.document.getElementById('d-delete-btn').click();
    await settle(window);

    releasePatch();  // the save closes the panel
    await settle(window);
    answer(true);
    await settle(window);

    expect(window.STATE.selectedChunkId).toBe(null);
    expect(deletes).toEqual([]);
  });

  it('totals and the empty state follow the dropped row', async () => {
    const { window, releasePatch } = await bootSweep();
    window.renderResults([result(A, 'A indexed'), result(B, 'B indexed')], {});
    const doc = window.document;
    const total = () => doc.querySelector('.results-summary-total').textContent;
    const before = total();
    await startSaveOfA(window);
    releasePatch();
    await settle(window);
    expect(total()).not.toBe(before);
    expect(total()).toBe(window.t('search.results_total', { count: 1 }));

    // Saving the last row leaves the empty state, not an empty list.
    window.showDetail(result(B, 'B indexed'));
    window._setDetailMode('edit');
    await settle(window);
    doc.getElementById('d-save-btn').click();
    await settle(window);
    expect(window.STATE.lastResults).toEqual([]);
    expect(doc.getElementById('results-empty').hidden).toBe(false);
  });

  it('delete keeps its labels for a chunk whose row a card save dropped', async () => {
    const { window } = await boot({
      sources: { [A]: () => ok({ text: SOURCE_A, base_hash: HASH_A }) },
    });
    const confirms = [];
    window.showConfirm = async (o) => { confirms.push(o); return false; };
    window.renderResults([result(A, 'A indexed')], {});
    window._setDetailMode('edit');  // a source edit (draft) open on A
    await settle(window);
    window.document.getElementById('d-editor').value = 'A draft';
    const card = window.document.createElement('div');
    window.document.body.appendChild(card);
    window._startChunkEdit(card, { id: A, content: 'indexed' }, '/m/note.md');
    await settle(window);
    card.querySelector('.chunk-card-edit-area textarea').value = 'card edit';
    card.querySelector('.chunk-card-edit-actions .btn-primary').click();
    await settle(window);
    expect(window.STATE.lastResults).toEqual([]);  // the row is gone
    expect(window.STATE.selectedChunkId).toBe(A);  // the draft's panel is not

    window.document.getElementById('d-delete-btn').click();
    await settle(window);
    expect(confirms).toHaveLength(1);
    expect(confirms[0].message).toContain('note.md');
    expect(confirms[0].message).toContain('lines 1–7');
  });

  it('prev/next re-reads a source after a save instead of its pre-save list', async () => {
    const { window, releasePatch } = await bootSweep();
    window.renderResults([result(A, 'A indexed')], {});
    window.eval("_sourceChunksCache = { file: '/m/note.md', chunks: [{ id: 'old' }] };");
    await startSaveOfA(window);
    releasePatch();
    await settle(window);
    expect(window.eval('_sourceChunksCache.file')).toBe(null);
  });

  it('the same chunk reopened during its save gets its History button', async () => {
    const { window, releasePatch } = await bootSweep();
    window.renderResults([result(A, 'A indexed')], {});
    await startSaveOfA(window);
    window.showDetail(result(A, 'A indexed'));  // reopened: a new session
    window._setDetailMode('edit');
    await settle(window);
    const historyBtn = window.document.getElementById('d-history-btn');
    expect(historyBtn.hidden).toBe(true);

    releasePatch();
    await settle(window);
    expect(window.STATE.selectedChunkId).toBe(A);
    expect(historyBtn.hidden).toBe(false);
  });
});


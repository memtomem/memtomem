/* #3093: the related-chunks preview shows a note's leading YAML frontmatter as
 * metadata, as the detail view does (#3030).
 *
 * The preview asks ``GET /api/chunks/{id}/frontmatter`` and judges the block
 * with the detail view's ``_leadingFrontmatter``. A preview is rebuilt for
 * every row click, so an answer is written only into the preview that asked.
 *
 * Indexed contents are the ones ``tests/test_chunk_frontmatter_route.py``
 * re-indexes and compares (``fixtures/frontmatter-indexed.json``).
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const FIXTURE = JSON.parse(fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures/frontmatter-indexed.json'),
  'utf8',
));

const A = 'a1b2c3d4-0000-4000-8000-00000000000a';
const B = 'a1b2c3d4-0000-4000-8000-00000000000b';

const INDEXED = '---\ntags: [alpha]\nowner: zqxvteam\n---\n\nRedis LRU eviction.';
const BLOCK = '---\ntags: [alpha]\nowner: zqxvteam\n---';
// ``# Topic\n---\nStatus: draft\n---\nBody.\n`` stores this, with the same shape.
const TOPIC_INDEXED = '---\nStatus: draft\n---\nBody.';

function result(id, content, { source = '/m/note.md', start = 1 } = {}) {
  return {
    chunk: {
      id, content, source_file: source, chunk_type: 'markdown_section',
      start_line: start, end_line: start + 6, heading_hierarchy: [], tags: [],
      namespace: 'default',
    },
    score: 0.5, rank: 1, source: 'bm25',
  };
}

const ok = (body) => ({ ok: true, status: 200, json: async () => body });

async function boot({ fm = {}, withMarked = true } = {}) {
  const dom = await bootApp({
    scripts: [
      'vendor/purify.min.js',
      ...(withMarked ? ['vendor/marked.umd.js'] : []),
      'i18n.js', 'app.js', 'settings-namespaces.js',
    ],
  });
  const { window } = dom;
  window.showToast = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  const fmGets = [];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const m = url.match(/\/api\/chunks\/([^/?]+)\/frontmatter$/);
    if (m) {
      fmGets.push(m[1]);
      const next = fm[m[1]];
      return typeof next === 'function' ? next() : ok({ leading: !!next });
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  return { window, fmGets };
}

const settle = async (window) => {
  for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));
};
const panel = (window) => window.document.getElementById('related-preview');
const body = (window) => panel(window).querySelector('.related-preview-content');
const box = (window) => body(window).querySelector('pre.md-frontmatter');
const headingTexts = (window) =>
  [...body(window).querySelectorAll('h1,h2,h3,h4,h5,h6')].map((h) => h.textContent);

async function preview(window, r) {
  window._showRelatedPreview(r, null);
  await settle(window);
}

function gated(response) {
  let release;
  const gate = new Promise((r) => { release = r; });
  return { next: async () => { await gate; return response; }, release };
}

describe('related-chunks preview frontmatter (#3093)', () => {
  it('boxes each block the server certifies, with no rule from it', async () => {
    const leading = FIXTURE.filter((e) => e.leading);
    const fm = Object.fromEntries(leading.map((e, i) => [`id-${i}`, true]));
    const { window, fmGets } = await boot({ fm });
    for (const [i, entry] of leading.entries()) {
      await preview(window, result(`id-${i}`, entry.content));
      expect(box(window)?.textContent, entry.name).toBe(entry.block);
      expect(body(window).querySelector('hr'), entry.name).toBe(null);
      expect(body(window).querySelector('img'), entry.name).toBe(null);
    }
    expect(fmGets).toEqual(leading.map((e, i) => `id-${i}`));
  });

  it('renders the body under the block, and no heading from the block', async () => {
    const { window } = await boot({ fm: { [A]: true } });
    await preview(window, result(A, INDEXED));

    expect(box(window)?.textContent).toBe(BLOCK);
    expect(headingTexts(window)).toEqual([]);
    expect(body(window).firstElementChild.tagName).toBe('PRE');
    expect(body(window).querySelector('p').textContent).toBe('Redis LRU eviction.');
  });

  it('renders as before when the server does not certify it', async () => {
    const { window, fmGets } = await boot({ fm: { [A]: false } });
    await preview(window, result(A, TOPIC_INDEXED));

    expect(fmGets).toEqual([A]);
    expect(box(window)).toBe(null);
    expect(body(window).querySelector('hr')).not.toBe(null);
    expect(headingTexts(window)).toEqual(['Status: draft']);
  });

  it('renders as before when the request fails', async () => {
    const failed = { ok: false, status: 500, statusText: 'x', json: async () => ({ detail: 'x' }) };
    const { window, fmGets } = await boot({ fm: { [A]: () => failed } });
    await preview(window, result(A, TOPIC_INDEXED));

    expect(fmGets).toEqual([A]);
    expect(box(window)).toBe(null);
    expect(headingTexts(window)).toEqual(['Status: draft']);
  });

  it('treats a .markdown source as Markdown', async () => {
    const { window, fmGets } = await boot({ fm: { [A]: true } });
    await preview(window, result(A, INDEXED, { source: '/m/note.markdown' }));

    expect(fmGets).toEqual([A]);
    expect(body(window).classList.contains('md-preview')).toBe(true);
    expect(box(window)?.textContent).toBe(BLOCK);
  });

  it('asks only for a Markdown chunk at line 1 whose content opens with ---', async () => {
    const { window, fmGets } = await boot({ fm: { [A]: true } });
    await preview(window, result(A, INDEXED, { start: 5 }));
    await preview(window, result(A, INDEXED, { source: '/m/note.txt' }));
    await preview(window, result(A, 'Redis LRU eviction.'));

    expect(fmGets).toEqual([]);
  });

  it('keeps the plain text and asks nothing without the Markdown renderer', async () => {
    const { window, fmGets } = await boot({ fm: { [A]: true }, withMarked: false });
    await preview(window, result(A, INDEXED));

    expect(fmGets).toEqual([]);
    expect(body(window).querySelector('pre').textContent).toBe(INDEXED);
  });

  it('shows markup inside the block as text, and sanitizes the body under it', async () => {
    const { window } = await boot({ fm: { [A]: true } });
    await preview(window, result(
      A,
      '---\nname: <img src=x onerror="window.pwned=1">\n---\n\n<b onclick="window.pwned=1">Body.</b>',
    ));

    expect(body(window).querySelector('img')).toBe(null);
    expect(box(window)?.textContent).toContain('<img src=x');
    expect(body(window).querySelector('b')?.textContent).toBe('Body.');
    expect(body(window).querySelector('b').getAttribute('onclick')).toBe(null);
    expect(window.pwned).toBe(undefined);
  });

  it('leaves the preview alone when the certified content holds no block', async () => {
    const late = gated(ok({ leading: true }));
    const { window, fmGets } = await boot({ fm: { [A]: late.next } });
    await preview(window, result(A, '---\ntags: [alpha]'));
    const before = body(window).firstElementChild;
    late.release();
    await settle(window);

    expect(fmGets).toEqual([A]);
    expect(body(window).firstElementChild).toBe(before);
  });

  it('never writes an answer for a replaced preview into the one shown', async () => {
    const late = gated(ok({ leading: true }));
    const { window, fmGets } = await boot({ fm: { [A]: late.next, [B]: false } });
    await preview(window, result(A, INDEXED));
    await preview(window, result(B, TOPIC_INDEXED));
    late.release();
    await settle(window);

    expect(fmGets).toEqual([A, B]);
    expect(box(window)).toBe(null);
    expect(headingTexts(window)).toEqual(['Status: draft']);
  });

  it('an answer arriving after Close does not reopen the preview', async () => {
    const late = gated(ok({ leading: true }));
    const { window, fmGets } = await boot({ fm: { [A]: late.next } });
    await preview(window, result(A, INDEXED));
    window.document.getElementById('related-preview-close').click();
    late.release();
    await settle(window);

    expect(fmGets).toEqual([A]);
    expect(panel(window).hidden).toBe(true);
  });
});

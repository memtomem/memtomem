/* #3030: the detail view shows a note's leading YAML frontmatter as metadata.
 *
 * ``marked`` reads the block as Markdown: the opening ``---`` becomes a rule and
 * ``key: value`` above the closing ``---`` a setext heading. Whether indexed
 * content opens with its file's frontmatter is the server's answer
 * (``GET /api/chunks/{id}/frontmatter``), since a file opening ``# Topic`` /
 * ``---`` / text / ``---`` stores the same shape. Source text loaded for an
 * edit is the file's head, so it is judged by the producer's regex directly.
 *
 * Indexed contents below are what ``IndexEngine.index_file`` stores for the
 * named files (see ``tests/test_chunk_frontmatter_route.py``).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const A = 'a1b2c3d4-0000-4000-8000-00000000000a';
const B = 'a1b2c3d4-0000-4000-8000-00000000000b';
const HASH = 'a'.repeat(64);

// ``---\ntags: [alpha]\nowner: zqxvteam\n---\n\n# Cache policy\n\nRedis LRU eviction.\n``
const INDEXED = '---\ntags: [alpha]\nowner: zqxvteam\n---\n\nRedis LRU eviction.';
const SOURCE = '---\ntags: [alpha]\nowner: zqxvteam\n---\n\n# Cache policy\n\nRedis LRU eviction.';
// ``# Topic\n---\nStatus: draft\n---\nBody.\n`` stores this, with the same shape.
const TOPIC_INDEXED = '---\nStatus: draft\n---\nBody.';
const TOPIC_SOURCE = '# Topic\n---\nStatus: draft\n---\nBody.';

function result(id, content, { source = '/m/note.md', start = 1 } = {}) {
  return {
    chunk: {
      id, content, source_file: source, chunk_type: 'markdown_section',
      start_line: start, end_line: start + 6, heading_hierarchy: ['# Cache policy'], tags: [],
      namespace: 'default',
    },
    score: 0.5, rank: 1, source: 'bm25',
  };
}

const ok = (body) => ({ ok: true, status: 200, json: async () => body });

async function boot({ fm = {}, sources = {} } = {}) {
  const dom = await bootApp({
    scripts: ['vendor/purify.min.js', 'vendor/marked.umd.js', 'i18n.js', 'app.js'],
  });
  const { window } = dom;
  window.showToast = () => {};
  const fmGets = [];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    let m = url.match(/\/api\/chunks\/([^/?]+)\/frontmatter$/);
    if (m) {
      fmGets.push(m[1]);
      const next = fm[m[1]];
      return typeof next === 'function' ? next() : ok({ leading: !!next });
    }
    m = url.match(/\/api\/chunks\/([^/?]+)\/source$/);
    if (m) return sources[m[1]]();
    return upstream(input, opts);
  };
  await window.I18N.init();
  return { window, fmGets };
}

const settle = async (window) => {
  for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));
};
const view = (window) => window.document.getElementById('d-code-view');
const box = (window) => view(window).querySelector('pre.md-frontmatter');
const headingTexts = (window) =>
  [...view(window).querySelectorAll('h1,h2,h3,h4,h5,h6')].map((h) => h.textContent);

function gated() {
  let release;
  const gate = new Promise((r) => { release = r; });
  return { gate, release };
}

describe('detail view frontmatter (#3030)', () => {
  it('boxes the block the server certifies, and renders no heading from it', async () => {
    const { window, fmGets } = await boot({ fm: { [A]: true } });
    window.showDetail(result(A, INDEXED));
    await settle(window);

    expect(fmGets).toEqual([A]);
    expect(box(window).textContent).toBe('---\ntags: [alpha]\nowner: zqxvteam\n---');
    expect(headingTexts(window)).toEqual([]);
    expect(view(window).firstElementChild.tagName).toBe('PRE');
    expect(view(window).querySelector('hr')).toBe(null);
    expect(view(window).querySelector('p').textContent).toBe('Redis LRU eviction.');
    // The editor, Copy and save keep the raw content.
    expect(window.document.getElementById('d-editor').value).toBe(INDEXED);
  });

  it('renders as before when the server does not certify it', async () => {
    const { window } = await boot({ fm: { [A]: false } });
    window.showDetail(result(A, TOPIC_INDEXED));
    await settle(window);

    expect(box(window)).toBe(null);
    expect(view(window).querySelector('hr')).not.toBe(null);
    expect(headingTexts(window)).toEqual(['Status: draft']);
  });

  it('asks only for a Markdown chunk at line 1 whose content opens with ---', async () => {
    const { window, fmGets } = await boot({ fm: { [A]: true, [B]: true } });
    window.showDetail(result(A, INDEXED, { start: 5 }));
    window.showDetail(result(A, INDEXED, { source: '/m/note.txt' }));
    window.showDetail(result(A, 'Redis LRU eviction.'));
    await settle(window);
    expect(fmGets).toEqual([]);

    window.showDetail(result(B, INDEXED, { source: '/m/note.markdown' }));
    await settle(window);
    expect(fmGets).toEqual([B]);
    expect(box(window)).not.toBe(null);
  });

  it('boxes a block that ends the content, and astral characters whole', async () => {
    const { window } = await boot({ fm: { [A]: true, [B]: true } });
    window.showDetail(result(A, '---\ntags: [alpha]\n---'));
    await settle(window);
    expect(box(window).textContent).toBe('---\ntags: [alpha]\n---');
    expect(view(window).querySelector('hr')).toBe(null);

    const astral = '---\ntags: [alpha]\nname: 😀😀😀😀😀😀\n---\n\nBody text.';
    window.showDetail(result(B, astral));
    await settle(window);
    expect(box(window).textContent).toBe('---\ntags: [alpha]\nname: 😀😀😀😀😀😀\n---');
    expect(view(window).querySelector('p').textContent).toBe('Body text.');
  });

  it('shows markup inside the block as text', async () => {
    const { window } = await boot({ fm: { [A]: true } });
    window.showDetail(result(A, '---\nname: <img src=x onerror="window.pwned=1">\n---\n\nBody.'));
    await settle(window);

    expect(view(window).querySelector('img')).toBe(null);
    expect(box(window).textContent).toContain('<img src=x');
    expect(window.pwned).toBe(undefined);
  });

  it('never applies a response for an earlier selection, even of the same chunk', async () => {
    const first = gated();
    let calls = 0;
    const { window } = await boot({
      fm: {
        [A]: async () => {
          calls += 1;
          if (calls === 1) { await first.gate; return ok({ leading: true }); }
          return ok({ leading: false });
        },
        [B]: false,
      },
    });
    window.showDetail(result(A, TOPIC_INDEXED));
    window.showDetail(result(B, TOPIC_INDEXED));
    window.showDetail(result(A, TOPIC_INDEXED));
    await settle(window);
    first.release();
    await settle(window);

    expect(window.STATE.detailFrontmatter.leading).toBe(false);
    expect(box(window)).toBe(null);
  });

  it('a response arriving after the panel closed changes nothing', async () => {
    const late = gated();
    const { window } = await boot({
      fm: { [A]: async () => { await late.gate; return ok({ leading: true }); } },
    });
    window.showDetail(result(A, INDEXED));
    window.clearDetail();
    late.release();
    await settle(window);

    expect(window.STATE.detailFrontmatter).toBe(null);
    expect(box(window)).toBe(null);
  });

  for (const order of ['frontmatter-first', 'source-first']) {
    it(`Edit → View before the source loads re-renders it when it lands (${order})`, async () => {
      const src = gated();
      const fmg = gated();
      const { window } = await boot({
        fm: { [A]: async () => { await fmg.gate; return ok({ leading: true }); } },
        sources: {
          [A]: async () => { await src.gate; return ok({ text: SOURCE, base_hash: HASH }); },
        },
      });
      window.showDetail(result(A, INDEXED));
      window._setDetailMode('edit');
      window._setDetailMode('view');
      if (order === 'frontmatter-first') { fmg.release(); await settle(window); src.release(); } else { src.release(); await settle(window); fmg.release(); }
      await settle(window);

      expect(box(window).textContent).toBe('---\ntags: [alpha]\nowner: zqxvteam\n---');
      expect(headingTexts(window)).toEqual(['Cache policy']);
    });
  }

  it('judges source text by what it holds, even when a draft equals the indexed text', async () => {
    const { window } = await boot({
      fm: { [A]: false },
      sources: { [A]: () => ok({ text: TOPIC_SOURCE, base_hash: HASH }) },
    });
    window.showDetail(result(A, TOPIC_INDEXED));
    window._setDetailMode('edit');
    await settle(window);
    // The draft drops ``# Topic``: saved, the file would open with this block.
    window.document.getElementById('d-editor').value = TOPIC_INDEXED;
    window._setDetailMode('view');

    expect(box(window).textContent).toBe('---\nStatus: draft\n---');
  });

  it('source text whose closing line is not exactly --- gets no box', async () => {
    for (const closing of ['----', '--- x']) {
      const text = `---\ntags: x\n${closing}\nBody\n---\n`;
      const { window } = await boot({
        fm: { [A]: false },
        sources: { [A]: () => ok({ text, base_hash: HASH }) },
      });
      window.showDetail(result(A, '---\ntags: x'));
      window._setDetailMode('edit');
      await settle(window);
      window._setDetailMode('view');

      expect(box(window)).toBe(null);
    }
  });
});

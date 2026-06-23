import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// S1.3 — the high-frequency search/sources micro-copy is keyed through t().
// Parity (test_i18n.py) proves the keys exist in both locales; these assertions
// prove the t() call sites are wired correctly (a typo'd key would render the
// raw "search.results_total" string to users instead of localized text).
describe('Search micro-copy is localized (S1.3)', () => {
  function mkResult(id) {
    return {
      chunk: {
        id, content: 'content', source_file: `/repo/${id}.md`,
        chunk_type: 'paragraph', start_line: 1, end_line: 3,
        heading_hierarchy: [], tags: [], namespace: 'default',
        created_at: '2026-05-13T00:00:00Z', updated_at: '2026-05-13T00:00:00Z',
        target_scope: 'user',
      },
      score: 0.03, rank: 1, source: 'fused',
    };
  }

  it('renders results total, bulk count, and empty-tags hint via t() (en)', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    window.renderResults([mkResult('a'), mkResult('b'), mkResult('c')],
      { bm25_candidates: 3, dense_candidates: 3, fused_total: 3, final_total: 3 });
    expect(document.querySelector('.results-summary-total').textContent).toBe('3 total');

    window.STATE.selectedIds = new window.Set(['a', 'b']);
    window.updateBulkToolbar(3);
    expect(document.getElementById('bulk-count').textContent).toBe('2 selected');

    window.renderTagChips([]);
    expect(document.querySelector('.tag-empty-hint').textContent).toBe('No tags — type below to add');
    // The exact localized assertions above already prove the t() keys resolved
    // (a typo'd key would render the raw "search.*" string instead).
  });

  it('renders results microcopy in the active locale on the next render (ko)', async () => {
    // S1.3 localizes imperative microcopy at render time (interpolation +
    // locale). A live language toggle does not repaint already-rendered
    // imperative nodes — that surface-wide repaint is deferred (see the
    // langchange NOTE in app.js) — so this asserts the next-render guarantee.
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();
    await I18N.setLang('ko');

    window.renderResults([mkResult('a'), mkResult('b')],
      { bm25_candidates: 2, dense_candidates: 2, fused_total: 2, final_total: 2 });
    expect(document.querySelector('.results-summary-total').textContent).toBe('총 2개');
  });

  it('localizes the source-item chunk/avg-token meta', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const item = window._renderMemorySourceItem(
      { path: '/repo/a.md', chunk_count: 3, avg_tokens: 120 }, 10);
    const row2 = item.querySelector('.source-item-row2').textContent;
    expect(row2).toContain('3 chunks');
    expect(row2).toContain('avg 120 tok');
  });

  it('localizes the chunk browser toggle, header count and card actions', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();
    window.HTMLElement.prototype.scrollIntoView = () => {};
    window.fetch = async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        total: 5,
        chunks: [{
          id: 'c1', chunk_type: 'paragraph', start_line: 1, end_line: 3,
          heading_hierarchy: [], target_scope: 'user', content: 'hello',
        }],
      }),
      text: async () => '{}',
    });

    await window.browseSource('/repo/a.md');
    const browser = document.getElementById('chunks-browser');
    expect([...browser.querySelectorAll('.view-mode-btn')].map(b => b.textContent))
      .toEqual(['Chunks', 'Document']);
    expect(browser.querySelector('.chunks-browser-info').textContent).toBe('1 of 5 shown');
    expect(browser.querySelector('.card-copy-btn').textContent).toBe('Copy');
    expect(browser.querySelector('.card-delete-btn').getAttribute('title')).toBe('Delete chunk');
  });

  it('localizes the editor word-count line', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    document.getElementById('d-editor').value = 'hello world'; // 11 chars, 2 words, ~3 tokens
    window._updateWordCount();
    expect(document.getElementById('d-word-count').textContent)
      .toBe('11 chars · 2 words · ~3 tokens');
  });
});

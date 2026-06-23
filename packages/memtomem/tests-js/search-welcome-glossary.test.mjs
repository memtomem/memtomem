import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const UNITS_EN = 'Sources are the files you add. Chunks are the searchable pieces. Memories are everything together.';
const UNITS_KO = '소스는 추가한 파일, 청크는 검색 단위 조각, 기억은 전체 모음입니다.';

describe('First-run welcome card + units glossary (S1.5 / S1.7)', () => {
  it('shows a localized welcome card by default and wires the CTAs', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    try { window.localStorage.removeItem('m2m-welcome-dismissed'); } catch {}
    window._initSearchWelcome();

    const welcome = document.getElementById('search-welcome');
    expect(welcome.hidden).toBe(false);
    expect(document.querySelector('.search-welcome-title').textContent).toBe('Welcome to memtomem');
    // The units line reuses the shared glossary.units key (also used by S1.7).
    expect(document.querySelector('.search-welcome-units').textContent).toBe(UNITS_EN);

    // "Try a search" → focus the query box (assert before the tab switch hides it).
    document.getElementById('search-welcome-search').click();
    expect(document.activeElement).toBe(document.getElementById('search-input'));

    // "Add a memory" → the Index tab.
    document.getElementById('search-welcome-add').click();
    expect(document.querySelector('.tab-btn[data-tab="index"]').classList.contains('active')).toBe(true);
  });

  it('stays dismissed across re-init via localStorage', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    window._initSearchWelcome();
    document.getElementById('search-welcome-dismiss').click();
    expect(document.getElementById('search-welcome').hidden).toBe(true);
    expect(window.localStorage.getItem('m2m-welcome-dismissed')).toBe('1');

    // Re-init (next page load) keeps it hidden.
    window._initSearchWelcome();
    expect(document.getElementById('search-welcome').hidden).toBe(true);
  });

  it('survives the empty-results render and tracks results visibility', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();
    try { window.localStorage.removeItem('m2m-welcome-dismissed'); } catch {}
    window._initSearchWelcome();
    expect(document.getElementById('search-welcome').hidden).toBe(false);

    // A zero-result render rewrites #results-empty.innerHTML; the sibling card
    // must survive and stay visible (regression for the clobber bug).
    window.renderResults([]);
    const welcome = document.getElementById('search-welcome');
    expect(welcome).not.toBeNull();
    expect(welcome.hidden).toBe(false);

    // A render with results hides the card (panel no longer in its empty state).
    window.renderResults([{
      chunk: {
        id: 'a', content: 'c', source_file: '/r/a.md', chunk_type: 'paragraph',
        start_line: 1, end_line: 2, heading_hierarchy: [], tags: [], namespace: 'default',
        created_at: '2026-05-13T00:00:00Z', updated_at: '2026-05-13T00:00:00Z', target_scope: 'user',
      },
      score: 0.03, rank: 1, source: 'fused',
    }], { bm25_candidates: 1, dense_candidates: 1, fused_total: 1, final_total: 1 });
    expect(document.getElementById('search-welcome').hidden).toBe(true);
  });

  it('hides the welcome card when a search fails (sibling error path)', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();
    try { window.localStorage.removeItem('m2m-welcome-dismissed'); } catch {}
    window._initSearchWelcome();
    expect(document.getElementById('search-welcome').hidden).toBe(false);

    const original = window.fetch;
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input?.url;
      if (url && url.startsWith('/api/search')) {
        return { ok: false, status: 500, json: async () => ({ detail: 'boom' }), text: async () => '{}' };
      }
      return original(input, init);
    };
    document.getElementById('search-input').value = 'cache';
    await window.doSearch();

    expect(document.getElementById('results-empty').hidden).toBe(true);
    expect(document.getElementById('results-list').hidden).toBe(false);
    expect(document.getElementById('search-welcome').hidden).toBe(true);
  });

  it('localizes the header units glossary help-tip and relocalizes on langchange (S1.7)', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    const tip = document.querySelector('.header-info-bar [data-help-i18n="glossary.units"]');
    expect(tip).not.toBeNull();
    expect(tip.getAttribute('data-help')).toBe(UNITS_EN);
    expect(tip.getAttribute('aria-label')).toBe(UNITS_EN);

    await I18N.setLang('ko');
    expect(tip.getAttribute('data-help')).toBe(UNITS_KO);
  });
});

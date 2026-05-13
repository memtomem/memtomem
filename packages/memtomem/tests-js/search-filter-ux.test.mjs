import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('Search filters - add/remove UX', () => {
  let window;
  let document;
  let searchUrls;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;
    searchUrls = [];

    const original = window.fetch;
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      if (url && url.startsWith('/api/search')) {
        searchUrls.push(url);
        return {
          ok: true,
          status: 200,
          json: async () => ({
            results: [],
            retrieval_stats: {
              bm25_candidates: 0,
              dense_candidates: 0,
              fused_total: 0,
              final_total: 0,
            },
          }),
          text: async () => '{}',
        };
      }
      return original(input, init);
    };
  });

  function addSourceOption(path, selected = true) {
    const opt = document.createElement('option');
    opt.value = path;
    opt.textContent = path.split('/').pop();
    opt.selected = selected;
    document.getElementById('source-filter').appendChild(opt);
  }

  async function flush() {
    for (let i = 0; i < 8; i++) {
      await new Promise(r => setTimeout(r, 0));
    }
  }

  it('keeps active filter chips visible when filters produce zero results', () => {
    document.getElementById('tag-filter').value = 'redis';
    document.getElementById('date-range-preset').value = '7d';
    addSourceOption('/repo/docs/cache.md');

    window.renderResults([]);

    const active = document.getElementById('active-filters');
    expect(active.hidden).toBe(false);
    expect(active.textContent).toContain('tag: redis');
    expect(active.textContent).toContain('source: cache.md');
    expect(active.textContent).toContain('date: Last 7 days');
    expect(active.textContent).toContain('Clear all');
    expect(document.getElementById('filter-count-badge').textContent).toBe('3');

    document.getElementById('clear-search-filters').click();
    expect(active.hidden).toBe(true);
    expect(document.getElementById('filter-count-badge').hidden).toBe(true);
    expect(document.getElementById('tag-filter').value).toBe('');
    expect(document.getElementById('date-range-preset').value).toBe('');
    expect([...document.getElementById('source-filter').selectedOptions]).toHaveLength(0);
    expect(document.getElementById('results-empty').textContent).toContain('Enter a query to search');
    expect(document.getElementById('results-empty').textContent).not.toContain('tag: redis');
  });

  it('runs source-only search from the Search tab', async () => {
    addSourceOption('/repo/docs/cache.md');

    await window.doSearch();
    await flush();

    expect(searchUrls).toHaveLength(1);
    const params = new URL(`http://localhost${searchUrls[0]}`).searchParams;
    expect(params.get('source_filter')).toBe('/repo/docs/cache.md');
    expect(params.get('q')).toBeNull();
    expect(params.get('tag_filter')).toBeNull();
  });

  it('preserves server-side filters when loading more results', async () => {
    document.getElementById('search-input').value = 'cache';
    document.getElementById('context-window').value = '2';
    const ns = document.createElement('option');
    ns.value = 'work';
    ns.textContent = 'work';
    document.getElementById('ns-filter').appendChild(ns);
    document.getElementById('ns-filter').value = 'work';
    addSourceOption('/repo/docs/cache.md');

    document.getElementById('load-more-btn').dispatchEvent(new window.Event('click'));
    await flush();

    expect(searchUrls).toHaveLength(1);
    const params = new URL(`http://localhost${searchUrls[0]}`).searchParams;
    expect(params.get('q')).toBe('cache');
    expect(params.get('top_k')).toBe('20');
    expect(params.get('namespace')).toBe('work');
    expect(params.get('context_window')).toBe('2');
    expect(params.get('source_filter')).toBe('/repo/docs/cache.md');
  });
});

describe('Search namespace filter list', () => {
  it('keeps full namespace names inside stable human groups', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-namespaces.js'] });
    const { window } = dom;
    const { document } = window;
    const original = window.fetch;
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      if (url === '/api/namespaces') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            namespaces: [
              { namespace: 'claude-memory:alpha', chunk_count: 3 },
              { namespace: 'claude-memory:beta', chunk_count: 7 },
              { namespace: 'codex:planner', chunk_count: 2 },
              { namespace: 'work', chunk_count: 5 },
              { namespace: 'default', chunk_count: 11 },
            ],
          }),
          text: async () => '{}',
        };
      }
      return original(input, init);
    };

    await window.loadNamespaceDropdowns();

    const sel = document.getElementById('ns-filter');
    expect(sel.children[1].tagName).toBe('OPTION');
    expect(sel.children[1].textContent).toBe('default (11)');
    const groups = [...sel.querySelectorAll('optgroup')];
    expect(groups.map(g => g.label)).toEqual(['User (5)', 'Claude (10)', 'OpenAI (2)']);
    expect([...groups[1].querySelectorAll('option')].map(o => o.textContent)).toEqual([
      'claude-memory:beta (7)',
      'claude-memory:alpha (3)',
    ]);
  });
});

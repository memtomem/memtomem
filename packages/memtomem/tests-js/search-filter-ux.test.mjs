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

  it('shows reranked results with rank percentile instead of raw negative score', () => {
    const mkResult = (id, score, rank) => ({
      chunk: {
        id,
        content: `content ${id}`,
        source_file: `/repo/notebooks/${id}.ipynb`,
        chunk_type: 'paragraph',
        start_line: 1,
        end_line: 3,
        heading_hierarchy: [],
        tags: [],
        namespace: 'default',
        created_at: '2026-05-13T00:00:00Z',
        updated_at: '2026-05-13T00:00:00Z',
        target_scope: 'user',
      },
      score,
      rank,
      source: 'reranked',
    });

    window.renderResults([
      mkResult('PA_S01_Intro', -0.42, 1),
      mkResult('PA_S02_Control', -3.7, 2),
    ], {
      bm25_candidates: 2,
      dense_candidates: 2,
      fused_total: 2,
      final_total: 2,
    });

    const scoreBadges = [...document.querySelectorAll('.result-item .score-badge')];
    const badges = scoreBadges.map(el => el.textContent);
    expect(badges).toEqual(['Relevance 100%', 'Relevance 1%']);
    expect(badges.join(' ')).not.toContain('-');
    expect(document.getElementById('d-score').textContent).toBe('rank #1 · 100%');

    // S1.2: the always-visible score badge's hover title is plain-language by
    // default — it must NOT leak the raw "Raw reranker score …" tooltip.
    expect(scoreBadges[0].getAttribute('title')).toBe('How closely this result matches your query.');
    expect(scoreBadges.map(b => b.getAttribute('title')).join(' ')).not.toMatch(/Raw|percentile|score -/);

    // S1.2: raw score + retrieval source are debug-only (hidden by default via
    // .result-debug-meta), and the score-bar tooltip is plain-language. The raw
    // retrieval math moves to data-tooltip-debug, surfaced only in debug mode.
    expect(document.getElementById('d-score').classList.contains('result-debug-meta')).toBe(true);
    expect(document.getElementById('d-source').classList.contains('result-debug-meta')).toBe(true);
    const detailRow = document.getElementById('d-score-detail');
    expect(detailRow.dataset.tooltip).toBe('How closely this result matches your query.');
    expect(detailRow.dataset.tooltip).not.toMatch(/RRF|raw score|percentile/);
    expect(detailRow.dataset.tooltipDebug).toContain('raw score -0.420000');
  });

  it('keeps retrieval internals hidden until Advanced details is expanded', () => {
    const mkResult = (id, score, rank, source) => ({
      chunk: {
        id, content: `content ${id}`, source_file: `/repo/${id}.md`,
        chunk_type: 'paragraph', start_line: 1, end_line: 3,
        heading_hierarchy: [], tags: [], namespace: 'default',
        created_at: '2026-05-13T00:00:00Z', updated_at: '2026-05-13T00:00:00Z',
        target_scope: 'user',
      },
      score, rank, source,
    });

    expect(document.body.classList.contains('show-retrieval-debug')).toBe(false);

    window.renderResults(
      [mkResult('a', 0.03, 1, 'fused'), mkResult('b', 0.02, 2, 'bm25')],
      { bm25_candidates: 2, dense_candidates: 2, fused_total: 2, final_total: 2 },
    );

    // Default render: debug class absent, raw per-result source badge carries
    // the hide hook.
    expect(document.body.classList.contains('show-retrieval-debug')).toBe(false);
    expect(document.querySelector('.result-item .badge-retrieval').classList
      .contains('result-debug-meta')).toBe(true);

    // Expanding "Advanced details" flips the app-wide reveal.
    const details = document.querySelector('.results-debug-details');
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    details.open = true;
    details.dispatchEvent(new window.Event('toggle'));
    expect(window.STATE.showRetrievalDebug).toBe(true);
    expect(document.body.classList.contains('show-retrieval-debug')).toBe(true);

    // Collapsing it hides them again.
    details.open = false;
    details.dispatchEvent(new window.Event('toggle'));
    expect(window.STATE.showRetrievalDebug).toBe(false);
    expect(document.body.classList.contains('show-retrieval-debug')).toBe(false);
  });

  it('keeps the score-badge title friendly across the live Advanced-details toggle', () => {
    // Drive the real user path (render default → expand Advanced details), not
    // a pre-seeded STATE, so a stale-on-toggle reintroduction would be caught.
    const FRIENDLY = 'How closely this result matches your query.';
    const stats = { bm25_candidates: 1, dense_candidates: 1, fused_total: 1, final_total: 1 };
    const mk = (id) => ({
      chunk: {
        id, content: 'content', source_file: `/repo/${id}.md`,
        chunk_type: 'paragraph', start_line: 1, end_line: 3,
        heading_hierarchy: [], tags: [], namespace: 'default',
        created_at: '2026-05-13T00:00:00Z', updated_at: '2026-05-13T00:00:00Z',
        target_scope: 'user',
      },
      score: 0.0302, rank: 1, source: 'fused',
    });

    // Default render (debug off): badge title is plain-language.
    window.renderResults([mk('a')], stats);
    let badge = document.querySelector('.result-item .score-badge');
    expect(badge.getAttribute('title')).toBe(FRIENDLY);

    // Live-toggle Advanced details ON — the already-rendered badge title must
    // NOT go stale/raw; the raw score instead becomes reachable via the
    // now-revealed detail-panel #d-score.
    const details = document.querySelector('.results-debug-details');
    details.open = true;
    details.dispatchEvent(new window.Event('toggle'));
    expect(window.STATE.showRetrievalDebug).toBe(true);
    expect(badge.getAttribute('title')).toBe(FRIENDLY);
    expect(badge.getAttribute('title')).not.toMatch(/Raw|score 0\./);
    expect(document.getElementById('d-score').classList.contains('result-debug-meta')).toBe(true);
    expect(document.getElementById('d-score').title).toMatch(/Raw fused score/);

    // A fresh render while debug is genuinely on (real state) still produces a
    // plain-language badge title.
    window.renderResults([mk('b')], stats);
    badge = document.querySelector('.result-item .score-badge');
    expect(badge.getAttribute('title')).toBe(FRIENDLY);
  });

  it('shows a friendly namespace label while keeping the full id reachable (S1.4)', () => {
    const longNs = 'claude:-Users-pdstudio-Work-agent-harness-memtomem';
    window.renderResults([{
      chunk: {
        id: 'ns1', content: 'content', source_file: '/repo/notes.md',
        chunk_type: 'paragraph', start_line: 1, end_line: 3,
        heading_hierarchy: [], tags: [], namespace: longNs,
        created_at: '2026-05-13T00:00:00Z', updated_at: '2026-05-13T00:00:00Z',
        target_scope: 'user',
      },
      score: 0.03, rank: 1, source: 'fused',
    }], { bm25_candidates: 1, dense_candidates: 1, fused_total: 1, final_total: 1 });

    // List badge: truncated label, full id in title (not the raw 50-char id).
    const listBadge = document.querySelector('.result-item .badge-ns');
    expect(listBadge.textContent).toBe('claude: .../harness/memtomem');
    expect(listBadge.getAttribute('title')).toBe(longNs);

    // Detail panel: same friendly label, full id in title + aria-label.
    const nsEl = document.getElementById('d-namespace');
    expect(nsEl.hidden).toBe(false);
    expect(nsEl.textContent).toBe('claude: .../harness/memtomem');
    expect(nsEl.getAttribute('title')).toBe(longNs);
    expect(nsEl.getAttribute('aria-label')).toBe(`namespace ${longNs}`);
  });
});

describe('Search config hint stays jargon-free', () => {
  const SERVER_CONFIG = {
    search: {
      default_top_k: 10,
      enable_bm25: true,
      enable_dense: true,
      rrf_k: 60,
      rrf_weights: [1, 1],
      tokenizer: 'unicode61',
    },
    rerank: {
      enabled: true,
      provider: 'fastembed',
      model: 'jinaai/jina-reranker-v2-base-multilingual',
      oversample: 2,
      min_pool: 20,
      max_pool: 200,
    },
  };

  it('summarizes config in plain language, not raw retrieval acronyms', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-config.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init(); // load the real locale so t() returns translated strings

    window.STATE.serverConfig = SERVER_CONFIG;
    window._syncConfigToUI();

    const hint = document.getElementById('search-config-info');
    expect(hint.hidden).toBe(false);
    // Localized plain-language summary — no raw i18n keys leaked.
    expect(hint.textContent).toContain('Up to 10 results');
    expect(hint.textContent).toContain('Hybrid search');
    expect(hint.textContent).not.toContain('search.status_');
    // Non-default reranker tuning still surfaces as a clickable badge.
    expect(hint.textContent).toContain('Pool 20-200 ×2');
    // First-time users must not meet retrieval-engine jargon in the always-on
    // summary: no acronyms, and no raw provider/model id leak.
    for (const jargon of ['BM25', 'Dense', 'RRF', 'Top-K', 'jina-reranker-v2-base-multilingual']) {
      expect(hint.textContent).not.toContain(jargon);
    }
  });

  it('re-renders the hint on langchange (i18n init-order + toggle) in the active locale', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-config.js'] });
    const { window } = dom;
    const { document, I18N } = window;
    await I18N.init();

    window.STATE.serverConfig = SERVER_CONFIG;
    // The hint is built imperatively with ``t()``; switching the locale fires
    // ``langchange``, which must re-render it in the new language (not freeze
    // the previous render or raw keys).
    await I18N.setLang('ko');

    const hint = document.getElementById('search-config-info');
    expect(hint.hidden).toBe(false);
    expect(hint.textContent).toContain('결과 최대 10개');
    expect(hint.textContent).toContain('하이브리드 검색');
    expect(hint.textContent).not.toContain('search.status_');
    expect(hint.textContent).not.toContain('Up to 10 results');
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

describe('Home namespace summary chart', () => {
  it('makes long namespaces identifiable and actionable', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    const { window } = dom;
    const { document } = window;

    const longNs = 'claude:-Users-pdstudio-Work-agent-harness-memtomem';
    window._renderNsChart([
      { namespace: longNs, chunk_count: 42 },
      { namespace: 'work', chunk_count: 12 },
      { namespace: 'default', chunk_count: 8 },
      { namespace: 'codex:-Users-pdstudio-Work-other-project', chunk_count: 7 },
      { namespace: 'notes', chunk_count: 6 },
      { namespace: 'archive', chunk_count: 5 },
      { namespace: 'hidden', chunk_count: 4 },
    ]);

    const chart = document.getElementById('home-ns-chart');
    const firstAction = chart.querySelector('[data-home-ns]');
    expect(firstAction.textContent).toContain('claude: .../harness/memtomem');
    expect(firstAction.getAttribute('aria-label')).toContain(longNs);
    expect(firstAction.getAttribute('aria-label')).toContain('42 chunks');
    expect(chart.querySelector('.home-ns-detail span').textContent).toBe(longNs);
    expect(chart.querySelector('.home-ns-more').textContent).toContain('+ 1 more');

    firstAction.click();

    expect(window.STATE.sourcesNsFilter).toBe(longNs);
    expect(document.querySelector('.tab-btn[data-tab="sources"]').classList.contains('active')).toBe(true);
  });
});

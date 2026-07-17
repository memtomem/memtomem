// Quality Lab Search Runs harness panel (#1801): server-derived values are
// rendered escaped (query text is user input — the unescaped sessions-table
// interpolation must NOT be copied here), and replacing an existing
// different judgment is a deliberate confirm() + replace:true retry.
import { describe, expect, it, vi } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const RUN_ID = '11111111-1111-4111-8111-111111111111';
const XSS = '<img src=x onerror="window.__pwned = true">';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-harness.js'] });
  await dom.window.I18N.init();
  return dom;
}

describe('search runs panel', () => {
  it('renders HTML-bearing query text as text, not markup', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      runs: [{
        run_id: RUN_ID,
        query_text: XSS,
        created_at: '2026-07-17T00:00:00+00:00',
        result_count: 1,
        origin: 'web',
        feedback_count: 0,
      }],
      total: 1,
    }));

    await window.loadHarnessSearchRuns();

    const list = window.document.getElementById('search-runs-list');
    expect(list.querySelector('img')).toBeNull();
    expect(window.__pwned).toBeUndefined();
    expect(list.textContent).toContain('<img src=x');
    expect(list.querySelector('[data-action="search-run-inspect"]').dataset.id).toBe(RUN_ID);
  });

  it('renders detail snapshot fields escaped and shows current judgments', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      run_id: RUN_ID,
      query_text: 'plain query',
      created_at: '2026-07-17T00:00:00+00:00',
      observation: { origin: 'web', top_k: 5, cache_hit: false, latency_ms: 12 },
      results: [
        { chunk_id: 'c1', rank: 1, score: 0.9, source_name: `${XSS}.md`, judgment: 'relevant' },
        { chunk_id: 'c2', rank: 2, score: 0.5, source_name: 'note.md', judgment: null },
      ],
    }));

    await window.showSearchRunDetail(RUN_ID);

    const detail = window.document.getElementById('search-run-detail');
    expect(detail.querySelector('img')).toBeNull();
    expect(detail.textContent).toContain('<img src=x');
    expect(detail.querySelector('.badge').textContent).toBe('relevant');
    const judgeButtons = detail.querySelectorAll('[data-action="search-run-judge"]');
    expect(judgeButtons).toHaveLength(4);
    expect(judgeButtons[0].dataset.chunk).toBe('c1');
    expect(judgeButtons[0].dataset.judgment).toBe('relevant');
  });

  it('retries with replace:true after a confirmed 409 conflict', async () => {
    const { window } = await boot();
    const posts = [];
    window.api = vi.fn(async (method, path, body) => {
      if (method === 'POST') {
        posts.push(body);
        if (!body.replace) {
          const err = new Error('conflict');
          err.status = 409;
          throw err;
        }
        return { ...body, run_id: RUN_ID, created: false, replaced: true };
      }
      return {
        run_id: RUN_ID, query_text: 'q', created_at: '2026-07-17T00:00:00+00:00',
        observation: {}, results: [],
      };
    });
    window.confirm = vi.fn(() => true);
    window.showToast = vi.fn();

    await window.submitSearchRunJudgment(RUN_ID, 'c1', 'not_relevant');

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(posts).toEqual([
      { chunk_id: 'c1', judgment: 'not_relevant', replace: false },
      { chunk_id: 'c1', judgment: 'not_relevant', replace: true },
    ]);
    expect(window.showToast).toHaveBeenCalledWith(expect.any(String), 'success');
  });

  it('declining the confirm leaves the judgment untouched', async () => {
    const { window } = await boot();
    const posts = [];
    window.api = vi.fn(async (method, path, body) => {
      posts.push(body);
      const err = new Error('conflict');
      err.status = 409;
      throw err;
    });
    window.confirm = vi.fn(() => false);
    window.showToast = vi.fn();

    await window.submitSearchRunJudgment(RUN_ID, 'c1', 'relevant');

    expect(posts).toHaveLength(1);
    expect(window.showToast).not.toHaveBeenCalled();
  });
});

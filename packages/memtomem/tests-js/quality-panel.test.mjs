// Quality Lab harness panel (#1802 PR-5): server-derived values (case names,
// query text, flags) are rendered escaped; replay report renders null-metric
// cells as "n/a" and surfaces the nondeterminism warning; the promote button
// dispatches a POST and toasts on success/failure; the replay button is
// disabled while a replay is in flight.
import { describe, expect, it, vi } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const RUN_ID = '11111111-1111-4111-8111-111111111111';
const CASE_ID = 'cccccccc-1111-4111-8111-111111111111';
const XSS = '<img src=x onerror="window.__pwned = true">';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-harness.js'] });
  await dom.window.I18N.init();
  return dom;
}

describe('quality lab panel', () => {
  it('renders HTML-bearing case name/query as text, not markup', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      cases: [{
        case_id: CASE_ID,
        name: XSS,
        query_text: XSS,
        top_k: 5,
        status: 'active',
        label_count: 2,
        created_at: '2026-07-18T00:00:00+00:00',
      }],
      total: 1,
    }));

    await window.loadHarnessQuality();

    const list = window.document.getElementById('quality-cases-list');
    expect(list.querySelector('img')).toBeNull();
    expect(window.__pwned).toBeUndefined();
    expect(list.textContent).toContain('<img src=x');
  });

  it('renders replay report with n/a metrics, flags, and nondeterminism warning', async () => {
    const { window } = await boot();
    const report = {
      kind: 'replay_report',
      as_of_unix: 1784500000,
      deterministic: false,
      nondeterministic_stages: ['query_expansion'],
      counts: { replayed: 1, archived_skipped: 0, degraded: 0, excluded_from_aggregate: 0 },
      aggregate: {
        mean_hit_rate: 1.0, mrr: 1.0, mean_recall_labeled: 1.0,
        mean_ndcg: 1.0, evaluated_cases: 1,
      },
      cases: [{
        case_id: CASE_ID,
        name: XSS,
        metrics: {
          hit_rate: 100, reciprocal_rank: 1.0, recall_labeled: 1.0,
          ndcg: 1.0, precision: null,
        },
        flags: ['stale_corpus'],
      }],
    };
    window.api = vi.fn(async () => report);

    await window.runQualityReplay();

    const panel = window.document.getElementById('quality-report');
    expect(panel.querySelector('img')).toBeNull();
    expect(panel.textContent).toContain('<img src=x');       // escaped name
    expect(panel.textContent).toContain('n/a');              // null precision
    expect(panel.textContent).toContain('stale_corpus');     // flag badge
    expect(panel.textContent.toLowerCase()).toContain('nondeterministic');
  });

  it('disables the replay button while in flight and re-enables after', async () => {
    const { window } = await boot();
    let inFlightDisabled = null;
    const btn = window.document.getElementById('quality-replay-btn');
    window.api = vi.fn(async () => {
      inFlightDisabled = btn.disabled;
      return {
        kind: 'replay_report', deterministic: true, nondeterministic_stages: [],
        counts: {}, aggregate: {}, cases: [],
      };
    });

    await window.runQualityReplay();

    expect(inFlightDisabled).toBe(true);
    expect(btn.disabled).toBe(false);
  });

  it('promote dispatches a POST with the run_id and toasts on success', async () => {
    const { window } = await boot();
    const posts = [];
    window.api = vi.fn(async (method, path, body) => {
      posts.push({ method, path, body });
      return { ok: true, case_id: CASE_ID, name: `run-${RUN_ID}`, label_count: 2 };
    });
    window.showToast = vi.fn();

    await window.promoteSearchRun(RUN_ID);

    expect(posts).toEqual([{ method: 'POST', path: '/api/quality/cases', body: { run_id: RUN_ID } }]);
    expect(window.showToast).toHaveBeenCalledWith(expect.any(String), 'success');
  });

  it('promote surfaces an error toast when the server rejects (409)', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => {
      const err = new Error('name collision');
      err.status = 409;
      throw err;
    });
    window.showToast = vi.fn();

    await window.promoteSearchRun(RUN_ID);

    expect(window.showToast).toHaveBeenCalledWith(expect.any(String), 'error');
  });

  it('dispatches the promote button action from the search-run detail', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      run_id: RUN_ID, query_text: 'q', created_at: '2026-07-18T00:00:00+00:00',
      observation: {}, results: [],
    }));
    await window.showSearchRunDetail(RUN_ID);

    const btn = window.document.querySelector('[data-action="search-run-promote"]');
    expect(btn).not.toBeNull();
    expect(btn.dataset.id).toBe(RUN_ID);
  });
});

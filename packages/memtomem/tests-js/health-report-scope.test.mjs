import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// The health report is project-scoped, but the sessions / working-memory rows
// carry no project identity — the API answers ``available: false`` with null
// counts rather than a 0 that would read as "this install has none" (#2281).
const UNAVAILABLE = {
  total_chunks: 2,
  access_coverage: { accessed: 1, total: 2, pct: 50 },
  tag_coverage: { tagged: 1, total: 2, pct: 50 },
  dead_memories_pct: 50,
  top_accessed: [],
  namespace_distribution: [],
  sessions: { total: null, active: null, recent_7d: null, available: false, reason: 'no_project_identity' },
  working_memory: { total: null, promoted: null, available: false, reason: 'no_project_identity' },
  cross_references: 0,
};

async function boot(evalPayload) {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'settings-harness.js'],
    apiResponses: { '/api/eval': evalPayload },
  });
  await dom.window.I18N.init();
  await dom.window.loadHarnessHealth();
  return dom;
}

const reportOf = dom => dom.window.document.getElementById('health-report');

describe('health report — blocks with no project-scoped answer', () => {
  it('labels sessions and working memory instead of printing a count', async () => {
    const report = reportOf(await boot(UNAVAILABLE));
    const cards = [...report.querySelectorAll('.health-card')];
    const byTitle = (name) =>
      cards.find(c => c.querySelector('.health-card-title')?.textContent === name);

    for (const name of ['Sessions', 'Working Memory']) {
      const card = byTitle(name);
      expect(card, `${name} card is still rendered`).toBeTruthy();
      expect(card.querySelector('.stat-value--na')).toBeTruthy();
      expect(card.textContent).not.toMatch(/\b0\b/);
      expect(card.textContent).not.toContain('null');
      expect(card.textContent).toContain('Not project-scoped');
      expect(card.getAttribute('title')).toBeTruthy();
    }
  });

  it('renders real counts when the blocks are available', async () => {
    const report = reportOf(await boot({
      ...UNAVAILABLE,
      sessions: { total: 12, active: 3, recent_7d: 5 },
      working_memory: { total: 7, promoted: 2 },
    }));

    expect(report.textContent).toContain('12');
    expect(report.textContent).toContain('3 active');
    expect(report.textContent).toContain('2 promoted');
    expect(report.querySelector('.stat-value--na')).toBeNull();
    expect(report.textContent).not.toContain('Not project-scoped');
  });

  it('re-localizes the label and tooltip on a language switch', async () => {
    const dom = await boot(UNAVAILABLE);
    const { window } = dom;
    const ko = JSON.parse(
      readFileSync(new URL('../src/memtomem/web/static/locales/ko.json', import.meta.url), 'utf-8'),
    );

    await window.I18N.setLang('ko');

    const card = [...reportOf(dom).querySelectorAll('.health-card')]
      .find(c => c.querySelector('.health-card-title')?.textContent === 'Sessions');
    expect(card.textContent).toContain(ko['settings.health.not_project_scoped']);
    expect(card.getAttribute('title')).toBe(ko['settings.health.not_project_scoped_hint']);
  });

  it('degrades a malformed 200 to the error state instead of a stuck spinner', async () => {
    // The render reads ``d.access_coverage.pct``; a body missing the block
    // throws mid-template. That must land on the error state (which offers
    // Retry), not leave the panel on "Loading…".
    const dom = await boot({ total_chunks: 1 });
    const report = reportOf(dom);

    expect(report.querySelector('.page-state--loading')).toBeNull();
    expect(report.querySelector('[role="alert"]')).toBeTruthy();
    expect(report.querySelector('.page-state-retry')).toBeTruthy();
  });

  it('keeps markup in API values inert', async () => {
    const report = reportOf(await boot({
      ...UNAVAILABLE,
      top_accessed: [{ id: '<img src=x onerror="window.__pwned = true">', content: '<script>1</script>', access_count: 1 }],
      namespace_distribution: [{ namespace: '<b>ns</b>', count: 2 }],
    }));

    expect(report.querySelector('img')).toBeNull();
    expect(report.querySelector('b')).toBeNull();
    expect(report.textContent).toContain('<b>ns</b>');
  });

  describe('overlapping refreshes', () => {
    // A slower earlier fetch must not repaint over a newer one. Each test
    // drives fetch by hand so the two requests are genuinely in flight at once.
    function deferredEval(window) {
      let settle;
      const gate = new Promise((resolve, reject) => { settle = { resolve, reject }; });
      const realFetch = window.fetch;
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input?.url;
        if (url && url.split('?')[0] === '/api/eval') {
          return gate;
        }
        return realFetch(input, init);
      };
      return { settle, restore: () => { window.fetch = realFetch; } };
    }

    const jsonOk = body => ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });

    it('ignores a stale success that lands after a newer one', async () => {
      const dom = await boot(UNAVAILABLE);
      const { window } = dom;
      const stale = deferredEval(window);
      const first = window.loadHarnessHealth();  // in flight, gated

      stale.restore();
      await window.loadHarnessHealth();  // newer request wins
      expect(reportOf(dom).textContent).toContain('Not project-scoped');

      stale.settle.resolve(jsonOk({ ...UNAVAILABLE, sessions: { total: 99, active: 7, recent_7d: 7 } }));
      await first;

      expect(reportOf(dom).textContent).not.toContain('99');
      expect(reportOf(dom).textContent).toContain('Not project-scoped');
    });

    it('ignores a stale failure that lands after a newer success', async () => {
      const dom = await boot(UNAVAILABLE);
      const { window } = dom;
      const stale = deferredEval(window);
      const first = window.loadHarnessHealth();

      stale.restore();
      await window.loadHarnessHealth();

      stale.settle.reject(new Error('stale boom'));
      await first;

      const report = reportOf(dom);
      expect(report.querySelector('[role="alert"]')).toBeNull();
      expect(report.textContent).toContain('Not project-scoped');
    });
  });
});

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
});

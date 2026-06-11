/* Dedup scan results i18n (#1025) — regression guards for the two review
 * findings on the conversion:
 *
 *  1. Translated strings must never enter ``innerHTML`` raw. Labels/titles
 *     go through ``data-i18n*`` attributes (applyDOM writes textContent /
 *     title only); the summary count is a real <strong> node with the
 *     translation appended as text nodes around it.
 *  2. An already-rendered scan must re-translate on EN/KO toggle — both
 *     the candidate rows and the JS-owned empty-state / summary / line
 *     strings, without resurrecting skipped rows.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const CANDIDATES = [
  {
    exact: true,
    score: 1.0,
    chunk_a: { id: '1', source_file: 'notes/a.md', start_line: 1, end_line: 4, content: 'alpha' },
    chunk_b: {
      id: '2',
      source_file: '<img src=x onerror=alert(1)>.md',
      start_line: 9,
      end_line: 12,
      content: '<script>boom</script>',
    },
  },
  {
    exact: false,
    score: 0.931,
    chunk_a: { id: '3', source_file: 'notes/c.md', start_line: 2, end_line: 5, content: 'gamma' },
    chunk_b: { id: '4', source_file: 'notes/d.md', start_line: 7, end_line: 9, content: 'delta' },
  },
];

async function bootDedup() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-maintenance.js'] });
  const { window } = dom;
  await window.I18N.init();
  await window.I18N.setLang('en');
  return window;
}

describe('dedup results — escaping', () => {
  it('keeps chunk-derived markup inert and renders labels via data-i18n*', async () => {
    const window = await bootDedup();
    const { document } = window;

    window.renderDedupCandidates(CANDIDATES, 0.92);
    const list = document.getElementById('dedup-list');

    // Attacker-shaped chunk fields never become elements.
    expect(list.querySelector('img')).toBeNull();
    expect(list.querySelector('script')).toBeNull();
    const filePaths = [...list.querySelectorAll('.file-path')].map(el => el.textContent);
    expect(filePaths).toContain('<img src=x onerror=alert(1)>.md');

    // Labels/titles are filled from the locale (not raw keys, not empty).
    const keepA = list.querySelector('.keep-a-btn');
    expect(keepA.textContent).toBe(window.t('settings.dedup.keep_a'));
    expect(keepA.textContent).not.toBe('settings.dedup.keep_a');
    expect(keepA.title).toBe(window.t('settings.dedup.keep_a_title'));
    expect(list.querySelector('.badge-danger').textContent)
      .toBe(window.t('settings.dedup.badge_exact'));

    // Summary: real <strong> count node (doMerge rewrites it in place),
    // translation text around it, no leftover placeholder.
    const summary = list.querySelector('.dedup-summary');
    expect(summary.querySelector('strong').textContent).toBe('2');
    expect(summary.textContent).not.toContain('{count}');

    // Parameterized line ranges resolved.
    const lines = [...list.querySelectorAll('.lines-info')].map(el => el.textContent);
    expect(lines[1]).toContain('9');
    expect(lines[1]).toContain('12');
    expect(lines.join(' ')).not.toContain('{start}');
  });
});

describe('dedup results — langchange re-translation', () => {
  it('re-translates rendered rows, summary, and line ranges on EN→KO', async () => {
    const window = await bootDedup();
    const { document, I18N } = window;

    window.renderDedupCandidates(CANDIDATES, 0.92);
    const list = document.getElementById('dedup-list');

    // Skipping a row must survive the language toggle (no full re-render
    // from cached candidates). Capture references afterwards — the
    // skipped row is detached and intentionally out of applyDOM's reach.
    list.querySelectorAll('.dedup-row')[0].querySelector('.skip-btn').click();
    expect(list.querySelectorAll('.dedup-row')).toHaveLength(1);

    const keepA = list.querySelector('.keep-a-btn');
    const summary = list.querySelector('.dedup-summary');
    const linesB = list.querySelectorAll('.lines-info')[1]; // remaining row, chunk B (7–9)
    const enKeepA = keepA.textContent;
    const enSummary = summary.textContent;
    const enLines = linesB.textContent;

    await I18N.setLang('ko');

    expect(keepA.textContent).not.toBe(enKeepA);
    expect(keepA.textContent).toBe(window.t('settings.dedup.keep_a'));
    expect(summary.textContent).not.toBe(enSummary);
    expect(summary.querySelector('strong').textContent).toBe('2');
    expect(linesB.textContent).not.toBe(enLines);
    expect(linesB.textContent).toContain('7');
    expect(list.querySelectorAll('.dedup-row')).toHaveLength(1);
  });

  it('re-derives the pluralized summary key after a merge lowers the count', async () => {
    const window = await bootDedup();
    const { document, I18N } = window;

    window.renderDedupCandidates(CANDIDATES, 0.92);
    const list = document.getElementById('dedup-list');
    const summary = list.querySelector('.dedup-summary');
    expect(summary.textContent).toBe(window.t('settings.dedup.summary_other', { count: '2' }));

    // Merge the first pair directly (the confirm dialog wraps this in the
    // UI); the fetch stub answers the POST with an empty 200.
    await window.doMerge(list.querySelectorAll('.dedup-row')[0], '1', ['2']);
    expect(summary.querySelector('strong').textContent).toBe('1');
    expect(summary.textContent).toBe(window.t('settings.dedup.summary_one', { count: '1' }));

    // KO one/other strings are identical, so round-trip back to EN to
    // observe the key choice on a langchange re-render.
    await I18N.setLang('ko');
    await I18N.setLang('en');
    expect(summary.textContent).toBe(window.t('settings.dedup.summary_one', { count: '1' }));
  });

  it('re-translates the JS-owned empty states on toggle', async () => {
    const window = await bootDedup();
    const { document, I18N } = window;

    // no-results state carries the threshold parameter.
    window.renderDedupCandidates([], 0.95);
    const empty = document.getElementById('dedup-empty');
    const enText = empty.textContent;
    expect(enText).toContain('0.95');

    await I18N.setLang('ko');
    expect(empty.textContent).not.toBe(enText);
    expect(empty.textContent).toContain('0.95');

    await I18N.setLang('en');
    expect(empty.textContent).toBe(enText);
  });
});

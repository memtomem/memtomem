/* ADR-0006 PR-B (Axis E.1 audit surface) — the Settings → Redaction panel, a
 * GUI view of `privacy.snapshot()` (GET /api/privacy/stats). Pins that
 * `loadRedactionStats` renders the outcome totals + the per-surface table, and
 * falls back to the empty state when no surface has recorded a write yet.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot(stats) {
  const dom = await bootApp({
    scripts: ['i18n.js', 'app.js', 'settings-harness.js'],
    apiResponses: { '/api/privacy/stats': stats },
  });
  await dom.window.I18N.init();
  return dom;
}

const FILLED = {
  outcomes: { blocked: 3, pass: 10, bypassed: 2, blocked_project_shared: 1 },
  by_tool: {
    index: { blocked: 3, pass: 5, bypassed: 2, blocked_project_shared: 1 },
    mem_add: { blocked: 0, pass: 5, bypassed: 0, blocked_project_shared: 0 },
  },
};

describe('Settings → Redaction stats panel', () => {
  it('renders outcome totals + a per-surface table', async () => {
    const { window } = await boot(FILLED);
    const document = window.document;

    await window.loadRedactionStats();
    await new Promise((r) => setTimeout(r, 0));

    const report = document.getElementById('redaction-stats-report');
    const txt = report.textContent;
    // Outcome cards use localized labels.
    expect(txt).toContain(window.t('settings.redaction.outcome.blocked'));
    expect(txt).toContain(window.t('settings.redaction.outcome.bypassed'));
    // Per-surface breakdown table with each firing surface.
    expect(report.querySelector('.harness-table')).toBeTruthy();
    expect(txt).toContain('index');
    expect(txt).toContain('mem_add');
    // A total is rendered (pass=10).
    expect(txt).toContain('10');
    // No empty-state when data exists.
    expect(txt).not.toContain(window.t('settings.redaction.empty'));
  });

  it('shows the empty state when no surface has fired', async () => {
    const { window } = await boot({
      outcomes: { blocked: 0, pass: 0, bypassed: 0, blocked_project_shared: 0 },
      by_tool: {},
    });
    const document = window.document;

    await window.loadRedactionStats();
    await new Promise((r) => setTimeout(r, 0));

    const report = document.getElementById('redaction-stats-report');
    expect(report.textContent).toContain(window.t('settings.redaction.empty'));
    // Outcome cards still render (all zero), so no table but the grid is present.
    expect(report.querySelector('.harness-table')).toBeNull();
  });

  it('escapes surface names (no HTML injection)', async () => {
    const { window } = await boot({
      outcomes: { blocked: 1, pass: 0, bypassed: 0, blocked_project_shared: 0 },
      by_tool: { '<img src=x onerror=alert(1)>': { blocked: 1, pass: 0, bypassed: 0, blocked_project_shared: 0 } },
    });
    const document = window.document;

    await window.loadRedactionStats();
    await new Promise((r) => setTimeout(r, 0));

    const report = document.getElementById('redaction-stats-report');
    // The injected string is rendered as text, not a live <img> element.
    expect(report.querySelector('img')).toBeNull();
    expect(report.textContent).toContain('<img src=x onerror=alert(1)>');
  });
});

/* Regression guard for #1290 — the overview "Canonical updated" row's hover
 * ``title`` used to carry the raw UTC ISO string (``2026-06-12T03:00:00Z``).
 * For non-UTC users the tooltip showed a different date/time than their wall
 * clock (the #677 hazard class, this time via ``title=`` instead of a slice).
 *
 * The fix keeps the #1076 intent — absolute timestamp on hover for the
 * "did this actually reach my runtimes?" diagnose case — but renders it via
 * ``new Date(iso).toLocaleString()`` (the detail-pane "Modified" precedent),
 * while ``data-iso`` keeps the machine-readable raw form.
 *
 * Mutation that bites: reverting ``title="${localAbs}"`` back to
 * ``title="${iso}"`` flips the positive assertion (title === toLocaleString)
 * AND the negative one (title !== raw ISO) — ``toLocaleString()`` never
 * round-trips to the ISO shape in any locale/TZ, so both hold on UTC CI too.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const RAW_ISO = '2026-06-12T03:00:00Z';

function stubOverviewFetch(window) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/api/context/overview')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          skills:   { total: 1, in_sync: 1 },
          commands: { total: 0 },
          agents:   { total: 0 },
          last_synced_at: RAW_ISO,
        }),
      };
    }
    // Keep the projects fetch silent (valid empty payload) so no error toast
    // races the overview render — same rationale as the sync-all tooltip suite.
    if (url.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: [] }) };
    }
    return upstream(input);
  };
}

describe('overview last-sync tooltip', () => {
  it('renders the hover title in local time, keeps data-iso raw', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    stubOverviewFetch(window);
    await window.I18N.init();

    await window.loadCtxOverview();

    const row = window.document.querySelector('.ctx-overview-last-sync');
    expect(row).toBeTruthy();
    // Positive pin: the exact toLocaleString rendering for this environment.
    expect(row.getAttribute('title')).toBe(new Date(RAW_ISO).toLocaleString());
    // Negative pin: the raw UTC ISO no longer leaks into the tooltip.
    expect(row.getAttribute('title')).not.toBe(RAW_ISO);
    // Machine-readable form survives on the value span.
    const value = row.querySelector('.ctx-overview-last-sync-value');
    expect(value.getAttribute('data-iso')).toBe(RAW_ISO);
  });
});

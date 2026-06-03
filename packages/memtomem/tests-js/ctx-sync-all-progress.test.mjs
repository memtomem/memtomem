/* Per-phase progress + result-summary rendering for Sync All (ADR-0021 §C).
 *
 * The Sync All fan-out is a sequence of independent ``POST /sync`` calls, so
 * instead of a streaming progress bar it drives a declarative status region
 * (``#ctx-sync-status``): each phase moves pending → syncing → done | failed,
 * and on completion artifact phases show a generated/dropped/skipped summary.
 * These guards pin (1) the happy-path summary text per phase and (2) that a
 * mid-run failure marks the remaining phases ``not_run`` rather than leaving a
 * frozen spinner.
 *
 * The handler runs behind ``showConfirm`` + ``ensureCsrfToken``; both are
 * global function declarations (app.js), so the tests override them on
 * ``window`` to drive the flow without the modal / a real token. The phase
 * fetches are stubbed per-URL on top of bootApp's locale-aware stub.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const HEALTHY_OVERVIEW = {
  skills: { total: 2, in_sync: 2 },
  commands: { total: 1, in_sync: 1 },
  agents: { total: 1, in_sync: 1 },
  settings: { total: 1, in_sync: 1, status: 'in_sync' },
};

// Install a fetch stub that serves the overview + a minimal projects payload
// and routes each ``/sync`` endpoint to a caller-supplied body. ``syncBodies``
// maps the artifact type → { ok?, status?, body } (settings uses ``results``).
function stubSyncFetch(window, syncBodies) {
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith('/api/context/overview')) {
      return { ok: true, status: 200, json: async () => HEALTHY_OVERVIEW };
    }
    if (path.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: [] }) };
    }
    const m = path.match(/\/api\/context\/([^/]+)\/sync$/);
    if (m) {
      const spec = syncBodies[m[1]] || { body: {} };
      return {
        ok: spec.ok !== false,
        status: spec.status || 200,
        // ``_ctxErrorMessageFromResponse`` reads ``headers.get('content-type')``
        // on the failure path; default to JSON so it parses ``detail``.
        headers: { get: () => 'application/json' },
        json: async () => spec.body || {},
        text: async () => JSON.stringify(spec.body || {}),
      };
    }
    return upstream(input, opts);
  };
}

async function flush(window, ticks = 30) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

async function bootSyncAll(syncBodies) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  // Bypass the confirm modal and the CSRF round-trip — both are global
  // function declarations so a window override redirects the bare call.
  window.showConfirm = async () => true;
  window.ensureCsrfToken = async () => 'test-token';
  stubSyncFetch(window, syncBodies);
  await window.I18N.init();
  // Render the overview first so the Sync All button is enabled (healthy
  // canonicals → not runtime-only) and the tier controls exist.
  await window.loadCtxOverview();
  return window;
}

describe('Sync All — per-phase progress + result summary', () => {
  it('renders a done summary per phase with generated/dropped/skipped counts', async () => {
    const window = await bootSyncAll({
      skills: { body: { generated: [{ runtime: 'claude' }, { runtime: 'codex' }], skipped: [] } },
      commands: {
        body: {
          generated: [{ runtime: 'claude' }],
          dropped: [{ runtime: 'codex', name: 'x' }],
          skipped: [{ runtime: 'kimi', reason: 'r' }],
        },
      },
      agents: { body: { generated: [], skipped: [{ runtime: 'codex', reason: 'r' }] } },
      'mcp-servers': { body: { generated: [{ runtime: 'claude' }], skipped: [] } },
      settings: { body: { results: [{ name: 'claude', status: 'ok' }], duplicate_tier_warnings: [] } },
    });
    const { I18N } = window;

    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);

    const region = window.document.getElementById('ctx-sync-status');
    expect(region.hidden).toBe(false);

    const rows = region.querySelectorAll('.ctx-sync-phase');
    expect(rows.length).toBe(5);

    // Every phase landed → all rows are in the done state, no frozen spinner.
    expect(region.querySelectorAll('.ctx-sync-phase--done').length).toBe(5);
    expect(region.querySelector('.ctx-sync-spinner')).toBeNull();

    const rowText = (key) =>
      Array.from(rows)
        .map((li) => li.textContent)
        .find((tx) => tx.includes(I18N.t(`settings.ctx.${key}_phase_title`))) || '';

    // skills: 2 generated, no dropped/skipped fragments.
    const skills = rowText('skills');
    expect(skills).toContain(I18N.t('settings.ctx.sync_count_generated', { count: 2 }));
    expect(skills).not.toContain(I18N.t('settings.ctx.sync_count_dropped', { count: 0 }));

    // commands: 1 generated · 1 dropped · 1 skipped.
    const commands = rowText('commands');
    expect(commands).toContain(I18N.t('settings.ctx.sync_count_generated', { count: 1 }));
    expect(commands).toContain(I18N.t('settings.ctx.sync_count_dropped', { count: 1 }));
    expect(commands).toContain(I18N.t('settings.ctx.sync_count_skipped', { count: 1 }));

    // agents: 0 generated · 1 skipped (no dropped key in response).
    const agents = rowText('agents');
    expect(agents).toContain(I18N.t('settings.ctx.sync_count_generated', { count: 0 }));
    expect(agents).toContain(I18N.t('settings.ctx.sync_count_skipped', { count: 1 }));

    // success toast still fires (existing contract, unchanged by the region).
    const toast = window.document.querySelector('#toast-container .toast-success .toast-msg');
    expect(toast).not.toBeNull();
    expect(toast.textContent).toBe(I18N.t('settings.ctx.sync_success'));
  });

  it('marks phases after a mid-run failure as not_run, not a frozen spinner', async () => {
    const window = await bootSyncAll({
      skills: { body: { generated: [{ runtime: 'claude' }], skipped: [] } },
      commands: { body: { generated: [{ runtime: 'claude' }], skipped: [] } },
      // agents fails → loop breaks; mcp-servers + settings never fire.
      agents: { ok: false, status: 422, body: { detail: 'Agents target is read-only' } },
      'mcp-servers': { body: { generated: [] } },
      settings: { body: { results: [{ name: 'claude', status: 'ok' }] } },
    });
    const { I18N } = window;

    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);

    const region = window.document.getElementById('ctx-sync-status');
    expect(region.hidden).toBe(false);

    // skills + commands done; agents failed; mcp-servers + settings not_run.
    expect(region.querySelectorAll('.ctx-sync-phase--done').length).toBe(2);
    expect(region.querySelectorAll('.ctx-sync-phase--failed').length).toBe(1);
    expect(region.querySelectorAll('.ctx-sync-phase--not_run').length).toBe(2);
    // No phase left mid-flight (a frozen spinner is the bug this guards).
    expect(region.querySelectorAll('.ctx-sync-phase--syncing').length).toBe(0);
    expect(region.querySelector('.ctx-sync-spinner')).toBeNull();

    const failedRow = region.querySelector('.ctx-sync-phase--failed');
    expect(failedRow.textContent).toContain(I18N.t('settings.ctx.agents_phase_title'));
    expect(failedRow.textContent).toContain(I18N.t('settings.ctx.sync_state_failed'));

    // Partial-failure toast names what landed + the failed phase (#1074).
    const toast = window.document.querySelector('#toast-container .toast-error .toast-msg');
    expect(toast).not.toBeNull();
    expect(toast.textContent).toContain(I18N.t('settings.ctx.agents_phase_title'));
  });

  it('renders the settings phase as attention (not done) on needs_confirmation', async () => {
    // All artifacts ok, but settings needs host-write confirmation. The region
    // must NOT show settings as plain "done" — that would contradict the
    // "complete except Settings" info toast. It reads as a distinct attention
    // state instead (review minor).
    const window = await bootSyncAll({
      skills: { body: { generated: [{ runtime: 'claude' }], skipped: [] } },
      commands: { body: { generated: [{ runtime: 'claude' }], skipped: [] } },
      agents: { body: { generated: [{ runtime: 'claude' }], skipped: [] } },
      'mcp-servers': { body: { generated: [{ runtime: 'claude' }], skipped: [] } },
      settings: { body: { results: [{ name: 'claude', status: 'needs_confirmation' }] } },
    });
    const { I18N } = window;

    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);

    const region = window.document.getElementById('ctx-sync-status');
    // 4 artifacts done, settings in the distinct attention state — not 5 done.
    expect(region.querySelectorAll('.ctx-sync-phase--done').length).toBe(4);
    const attention = region.querySelector('.ctx-sync-phase--attention');
    expect(attention).not.toBeNull();
    expect(attention.textContent).toContain(I18N.t('settings.ctx.settings_phase_title'));
    expect(attention.textContent).toContain(I18N.t('settings.ctx.sync_state_needs_confirmation'));
    // The needs-confirmation info toast (with Open Settings action) still fires.
    const toast = window.document.querySelector('#toast-container .toast-info .toast-msg');
    expect(toast).not.toBeNull();
  });

  it('clears the summary region when the tier changes after a run', async () => {
    // The summary belongs to the (project, tier) it ran on; switching the tier
    // must hide + empty the region so a stale summary can't be misread
    // (context-gateway.js _renderCtxSyncStatus(null) on tier change).
    const window = await bootSyncAll({
      skills: { body: { generated: [{ runtime: 'claude' }] } },
      commands: { body: { generated: [{ runtime: 'claude' }] } },
      agents: { body: { generated: [{ runtime: 'claude' }] } },
      'mcp-servers': { body: { generated: [{ runtime: 'claude' }] } },
      settings: { body: { results: [{ name: 'claude', status: 'ok' }] } },
    });

    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);

    const region = window.document.getElementById('ctx-sync-status');
    // Precondition: the region is populated after the run.
    expect(region.hidden).toBe(false);
    expect(region.querySelectorAll('.ctx-sync-phase').length).toBe(5);

    // Flip the tier — the only externally reachable mutation path. The click
    // handler calls _renderCtxSyncStatus(null) synchronously before the async
    // overview re-render.
    const tierBtn = window.document.querySelector(
      '.ctx-tier-filter button[data-scope="user"]',
    );
    expect(tierBtn).not.toBeNull();
    tierBtn.click();

    expect(region.hidden).toBe(true);
    expect(region.innerHTML).toBe('');
  });

  it('re-translates the result summary on langchange (no mixed-language rows)', async () => {
    // Regression for the P2 review finding: the phase state must store RAW
    // counts, not a pre-localized string. Run in EN, switch to KO — the
    // summary must follow the locale, not stay frozen as English next to a
    // re-translated Korean label.
    const window = await bootSyncAll({
      skills: { body: { generated: [{ runtime: 'claude' }, { runtime: 'codex' }], skipped: [] } },
      commands: { body: { generated: [{ runtime: 'claude' }] } },
      agents: { body: { generated: [{ runtime: 'claude' }] } },
      'mcp-servers': { body: { generated: [{ runtime: 'claude' }] } },
      settings: { body: { results: [{ name: 'claude', status: 'ok' }] } },
    });
    const { I18N } = window;

    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);

    const region = window.document.getElementById('ctx-sync-status');
    // skills is first in _CTX_SYNC_PHASES — find by index so the lookup is
    // locale-independent (the label text changes after setLang).
    const skillsRow = () => region.querySelectorAll('.ctx-sync-phase')[0];

    // Precondition: the summary is the EN "2 generated".
    const enSummary = I18N.t('settings.ctx.sync_count_generated', { count: 2 });
    expect(skillsRow().textContent).toContain(enSummary);

    // The langchange re-render of the status region is gated behind an active
    // Gateway/Settings host tab + active overview section — mark them active so
    // the listener reaches _renderCtxSyncStatus.
    window.document.getElementById('tab-context-gateway')?.classList.add('active');
    window.document.getElementById('tab-settings')?.classList.add('active');
    window.document.getElementById('settings-ctx-overview')?.classList.add('active');

    await I18N.setLang('ko');

    const koSummary = I18N.t('settings.ctx.sync_count_generated', { count: 2 });
    // Sanity: the two locales actually differ, else the assertion is vacuous.
    expect(koSummary).not.toBe(enSummary);
    // The summary followed the locale; the stale English text is gone.
    expect(skillsRow().textContent).toContain(koSummary);
    expect(skillsRow().textContent).not.toContain(enSummary);
    // And the phase label itself is the Korean one (no mixed-language row).
    expect(skillsRow().textContent).toContain(I18N.t('settings.ctx.skills_phase_title'));
  });
});

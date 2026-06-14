/* ADR-0026 P1a (#1353) — Context Gateway Simple mode.
 *
 * Simple mode is a progressive-disclosure layer over the Overview: a
 * localStorage flag (default OFF = Advanced, per ADR-0026 D-F's staged rollout)
 * that toggles a ``.ctx-simple`` class on the gateway tab and swaps the
 * four-axis tile grid for a one-line verdict + a read-only per-type row list
 * (3-state display remap). These guards pin:
 *   (1) default is Advanced — the grid renders, no ``.ctx-simple`` class, no
 *       Simple body (today's UI verbatim);
 *   (2) the toggle flips the class + aria-pressed + persists the flag, and the
 *       Overview re-renders into the verdict + per-type rows (hooks excluded);
 *   (3) the 3-state remap maps each type's raw counts to the right Simple state
 *       (display-only — no wire status string mutated);
 *   (4) a row's Manage button leaves Simple mode and deep-links into Advanced;
 *   (5) the empty state surfaces the read-only hint + Open-Advanced CTA;
 *   (6) Simple re-renders in place on a langchange (locale flip).
 *
 * jsdom does not apply external CSS, so visibility (the grid being display:none
 * under .ctx-simple) is covered by the Playwright spec
 * (tests/web/test_context_gateway_simple_mode.py); here we assert the DOM +
 * class + state logic that drives it.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCOPES = [
  {
    scope_id: '', label: 'Server CWD', root: '/srv', tier: 'project',
    sources: ['server-cwd'], missing: false, stale: false, experimental: false,
    counts: { skills: 2, commands: 1, agents: 1, 'mcp-servers': 1 },
  },
];

// One artifact type per Simple state: skills→needs_sync, commands→in_tools,
// agents→not_saved (runtime-only), mcp_servers→attention (parse error). The
// settings/hooks slot stays out of the Simple rows (Advanced-only in P1a).
const OVERVIEW = {
  skills: { total: 2, in_sync: 1, missing_target: 1 },
  commands: { total: 1, in_sync: 1 },
  agents: { total: 1, in_sync: 0, missing_canonical: 1 },
  mcp_servers: { total: 1, in_sync: 0, parse_error: 1 },
  settings: { total: 1, in_sync: 1, status: 'in_sync' },
  detected_runtimes: [],
  project_root: '/srv',
  target_scope: 'project_shared',
};

const EMPTY_OVERVIEW = {
  skills: { total: 0, in_sync: 0 },
  commands: { total: 0, in_sync: 0 },
  agents: { total: 0, in_sync: 0 },
  mcp_servers: { total: 0, in_sync: 0 },
  settings: { total: 0, in_sync: 0, status: 'in_sync' },
  detected_runtimes: [],
  project_root: '/srv',
  target_scope: 'project_shared',
};

// Mixed multi-runtime rows: per-status counts can sum above ``total`` because
// they are per-(runtime, name) pairs. These pin the precedence against the
// Advanced ladder (missing_target → missing_canonical → out_of_sync).
const MIXED_OVERVIEW = {
  // out_of_sync AND missing_canonical → import side wins (matches Advanced).
  skills: { total: 2, in_sync: 0, out_of_sync: 1, missing_canonical: 1 },
  // missing_target AND missing_canonical → push side wins (missing_target first).
  commands: { total: 2, in_sync: 0, missing_target: 1, missing_canonical: 1 },
  agents: { total: 1, in_sync: 1 },
  mcp_servers: { total: 1, in_sync: 1 },
  settings: { total: 1, in_sync: 1, status: 'in_sync' },
  detected_runtimes: [],
  project_root: '/srv',
  target_scope: 'project_shared',
};

function jsonOk(body) {
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => body, text: async () => JSON.stringify(body) };
}

function installFetch(window, overview = OVERVIEW) {
  const upstream = window.fetch;
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const p = url.split('?')[0];
    if (p.includes('/api/context/projects')) return jsonOk({ scopes: SCOPES, target_scope: 'project_shared' });
    if (p.endsWith('/api/context/overview')) return jsonOk(overview);
    if (p.endsWith('/api/context/runtimes')) return jsonOk({ runtimes: [] });
    if (p.match(/\/api\/context\/(skills|commands|agents|mcp-servers)$/)) return jsonOk({ items: [] });
    return upstream(input, init);
  };
}

async function flush(window, ticks = 30) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

function setActiveSection(window, sectionId) {
  window.document
    .querySelectorAll('#tab-context-gateway .settings-section')
    .forEach((s) => s.classList.remove('active'));
  const sec = window.document.getElementById(`settings-${sectionId}`);
  if (sec) sec.classList.add('active');
}

async function boot(overview = OVERVIEW) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  installFetch(window, overview);
  await window.I18N.init();
  // The Gateway tab is the visible host (langchange gate) and the Overview is
  // the active section for every Simple-mode assertion below.
  window.document.getElementById('tab-context-gateway').classList.add('active');
  setActiveSection(window, 'ctx-overview');
  return window;
}

describe('ADR-0026 P1a — Context Gateway Simple mode', () => {
  it('defaults to Advanced: tile grid renders, no .ctx-simple class, no Simple body', async () => {
    const window = await boot();
    await window.loadCtxOverview();
    await flush(window);

    const tab = window.document.getElementById('tab-context-gateway');
    expect(tab.classList.contains('ctx-simple')).toBe(false);
    expect(window.document.querySelector('.ctx-overview-grid')).not.toBeNull();
    expect(window.document.querySelector('.ctx-overview-simple')).toBeNull();
    // The toggle is present and reports the un-pressed (Advanced) state.
    const toggle = window.document.getElementById('ctx-mode-toggle');
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
  });

  it('toggle flips the class + aria-pressed + persists, and renders verdict + per-type rows', async () => {
    const window = await boot();
    await window.loadCtxOverview();
    await flush(window);

    const tab = window.document.getElementById('tab-context-gateway');
    const toggle = window.document.getElementById('ctx-mode-toggle');
    toggle.click();
    await flush(window);

    expect(tab.classList.contains('ctx-simple')).toBe(true);
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(window.localStorage.getItem('memtomem_ctx_simple_mode')).toBe('1');

    const simple = window.document.querySelector('.ctx-overview-simple');
    expect(simple).not.toBeNull();
    // One row per artifact type (skills/commands/agents/mcp) — hooks excluded.
    const rows = simple.querySelectorAll('.ctx-simple-row');
    expect(rows.length).toBe(4);
    expect(simple.querySelector('[data-section="hooks-sync"]')).toBeNull();
    // Every row carries non-empty status text (never color-only — D-G).
    rows.forEach((r) => {
      expect(r.querySelector('.ctx-simple-status-text').textContent.trim().length).toBeGreaterThan(0);
    });

    // Flip back: class off, flag persisted as '0'.
    toggle.click();
    await flush(window);
    expect(tab.classList.contains('ctx-simple')).toBe(false);
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(window.localStorage.getItem('memtomem_ctx_simple_mode')).toBe('0');
  });

  it('3-state remap maps raw counts to the right Simple state per type', async () => {
    const window = await boot();
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const stateOf = (section) =>
      window.document.querySelector(`.ctx-simple-row[data-section="${section}"]`)?.dataset.state;
    expect(stateOf('ctx-skills')).toBe('needs_sync');       // missing_target > 0
    expect(stateOf('ctx-commands')).toBe('in_tools');       // all in sync
    expect(stateOf('ctx-mcp-servers')).toBe('attention');   // parse_error > 0
    expect(stateOf('ctx-agents')).toBe('not_saved');        // missing_canonical only

    // Verdict escalates to "attention" when any type needs attention.
    const verdict = window.document.querySelector('.ctx-simple-verdict');
    expect(verdict.textContent).toBe(window.t('settings.ctx.simple_verdict_attention'));
  });

  it('mixed multi-runtime rows match the Advanced precedence ladder', async () => {
    const window = await boot(MIXED_OVERVIEW);
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const stateOf = (section) =>
      window.document.querySelector(`.ctx-simple-row[data-section="${section}"]`)?.dataset.state;
    // out_of_sync + missing_canonical → import side wins (missing_canonical
    // outranks out_of_sync, exactly as the Advanced badge ladder).
    expect(stateOf('ctx-skills')).toBe('not_saved');
    // missing_target + missing_canonical → push side wins (missing_target first).
    expect(stateOf('ctx-commands')).toBe('needs_sync');
  });

  it('a row Manage button leaves Simple mode and deep-links into Advanced', async () => {
    const window = await boot();
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const calls = [];
    window.switchSettingsSection = (s) => { calls.push(s); };

    const tab = window.document.getElementById('tab-context-gateway');
    const manage = window.document.querySelector(
      '.ctx-simple-row[data-section="ctx-skills"] .ctx-simple-manage',
    );
    expect(manage).not.toBeNull();
    manage.click();

    expect(calls).toEqual(['ctx-skills']);
    expect(tab.classList.contains('ctx-simple')).toBe(false);
    expect(window.localStorage.getItem('memtomem_ctx_simple_mode')).toBe('0');
  });

  it('empty state surfaces the read-only hint + Open-Advanced CTA', async () => {
    const window = await boot(EMPTY_OVERVIEW);
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const simple = window.document.querySelector('.ctx-overview-simple');
    expect(simple.querySelector('.ctx-simple-empty-hint')).not.toBeNull();
    const cta = simple.querySelector('.ctx-simple-advanced-cta');
    expect(cta).not.toBeNull();
    expect(simple.querySelector('.ctx-simple-verdict').textContent)
      .toBe(window.t('settings.ctx.simple_verdict_empty'));

    // The CTA leaves Simple mode (no section → stays on the Overview).
    cta.click();
    await flush(window);
    expect(window.document.getElementById('tab-context-gateway').classList.contains('ctx-simple')).toBe(false);
  });

  it('re-localizes the Simple body in place on a locale flip (langchange)', async () => {
    const window = await boot();
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const verdict = () => window.document.querySelector('.ctx-simple-verdict').textContent.trim();
    const skillsStatus = () =>
      window.document
        .querySelector('.ctx-simple-row[data-section="ctx-skills"] .ctx-simple-status-text')
        .textContent.trim();
    // EN baseline — verdict escalates to attention (mcp parse_error in OVERVIEW).
    expect(verdict()).toBe('Some items need your attention — open Advanced to review.');
    expect(skillsStatus()).toBe('Needs sync');

    // setLang dispatches langchange; the listener re-renders the Overview from
    // cache. Simple is sticky, so the verdict + 3-state copy re-translate in
    // place (inline t()), not revert to the grid or linger in English.
    await window.I18N.setLang('ko');
    await flush(window);
    const simple = window.document.querySelector('.ctx-overview-simple');
    expect(simple).not.toBeNull();
    expect(simple.querySelectorAll('.ctx-simple-row').length).toBe(4);
    expect(verdict()).toBe(window.t('settings.ctx.simple_verdict_attention'));
    expect(verdict()).not.toContain('attention');
    expect(skillsStatus()).toBe(window.t('settings.ctx.status_simple_needs_sync'));
  });
});

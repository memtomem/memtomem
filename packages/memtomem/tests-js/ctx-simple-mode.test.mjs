/* ADR-0026 P1a/P1b (#1353) — Context Gateway Simple mode.
 *
 * Simple mode is a progressive-disclosure layer over the Overview: a
 * localStorage flag (default ON = Simple since the D-F flip 2026-06-18, a
 * reversible experiment; Advanced via the toggle) that toggles a ``.ctx-simple``
 * class on the gateway tab and swaps the four-axis tile grid for a one-line
 * verdict + a per-type row list (3-state display remap). P1a guards:
 *   (1) default is Simple — the ``.ctx-simple`` class is on and the verdict +
 *       per-type rows render (Advanced is one toggle click away);
 *   (2) the toggle flips the class + aria-pressed + persists the flag, and the
 *       Overview re-renders into the verdict + per-type rows (hooks excluded);
 *   (3) the 3-state remap maps each type's raw counts to the right Simple state
 *       (display-only — no wire status string mutated);
 *   (4) an attention row's Manage button leaves Simple mode and deep-links into
 *       Advanced;
 *   (5) the empty state surfaces the read-only hint + Open-Advanced CTA;
 *   (6) Simple re-renders in place on a langchange (locale flip).
 * P1b guards:
 *   (7) one control per row — Sync (needs_sync) / Import (not_saved) / a check
 *       (in_tools) / Manage (attention, empty, and not_saved for mcp-servers,
 *       which has no /import route);
 *   (8) the inline buttons run the SAME _ctxRunSync / _ctxRunImport flow as the
 *       Advanced toolbar, with an Overview refresh on success;
 *   (9) an all-empty active tier names items held in another tier (D-D).
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

// P1b: mcp-servers runtime-only (not_saved) — but mcp-servers has no /import
// route, so its row must fall back to Manage, not an inline Import.
const MCP_RUNTIME_ONLY = {
  ...OVERVIEW,
  mcp_servers: { total: 1, in_sync: 0, missing_canonical: 1 },
};

// P1b D-D: the active tier (project_shared) is empty; the User tier holds 3.
const USER_TIER_OVERVIEW = {
  skills: { total: 3, in_sync: 3 },
  commands: { total: 0 },
  agents: { total: 0 },
  mcp_servers: { total: 0 },
  settings: { total: 0, in_sync: 0, status: 'in_sync' },
  detected_runtimes: [],
  project_root: '/srv',
  target_scope: 'user',
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
  it('defaults to Simple: .ctx-simple class on, verdict + per-type rows render, toggle pressed', async () => {
    const window = await boot();
    await window.loadCtxOverview();
    await flush(window);

    const tab = window.document.getElementById('tab-context-gateway');
    expect(tab.classList.contains('ctx-simple')).toBe(true);
    // Simple body renders by default; the Advanced grid stays in the DOM
    // (CSS-hidden — visibility is the Playwright spec's job, not jsdom's).
    expect(window.document.querySelector('.ctx-overview-simple')).not.toBeNull();
    // The toggle is present and reports the pressed (Simple) state.
    const toggle = window.document.getElementById('ctx-mode-toggle');
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
  });

  it('toggle flips the class + aria-pressed + persists, and re-renders the Overview', async () => {
    const window = await boot();
    await window.loadCtxOverview();
    await flush(window);

    const tab = window.document.getElementById('tab-context-gateway');
    const toggle = window.document.getElementById('ctx-mode-toggle');

    // Default is Simple; the first click flips to Advanced (class off, flag '0',
    // Simple body gone).
    toggle.click();
    await flush(window);
    expect(tab.classList.contains('ctx-simple')).toBe(false);
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(window.localStorage.getItem('memtomem_ctx_simple_mode')).toBe('0');
    expect(window.document.querySelector('.ctx-overview-simple')).toBeNull();

    // Flip back to Simple: class on, flag '1', verdict + per-type rows render.
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

  it('an attention row Manage button leaves Simple mode and deep-links into Advanced', async () => {
    // P1b: skills (needs_sync) now carries an inline Sync button, so the Manage
    // deep-link is asserted on the attention row (mcp parse_error in OVERVIEW),
    // which has no safe one-click fix and keeps Manage.
    const window = await boot();
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const calls = [];
    window.switchSettingsSection = (s) => { calls.push(s); };

    const tab = window.document.getElementById('tab-context-gateway');
    const manage = window.document.querySelector(
      '.ctx-simple-row[data-section="ctx-mcp-servers"] .ctx-simple-manage',
    );
    expect(manage).not.toBeNull();
    manage.click();

    expect(calls).toEqual(['ctx-mcp-servers']);
    expect(tab.classList.contains('ctx-simple')).toBe(false);
    expect(window.localStorage.getItem('memtomem_ctx_simple_mode')).toBe('0');
  });

  it('P1b: one control per row — Sync (needs_sync) / Import (not_saved) / check (in_tools) / Manage (attention)', async () => {
    const window = await boot();
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const rowCtl = (section, sel) =>
      window.document.querySelector(`.ctx-simple-row[data-section="${section}"] ${sel}`);

    // needs_sync → inline Sync (primary), carrying the type + click-time snapshot.
    const sync = rowCtl('ctx-skills', '[data-ctx-action="sync"]');
    expect(sync).not.toBeNull();
    expect(sync.dataset.type).toBe('skills');
    expect(sync.dataset.canonicalCount).toBeDefined();
    expect(sync.dataset.noFanout).toBe('false');
    expect(sync.classList.contains('btn-primary')).toBe(true);
    expect((sync.getAttribute('aria-label') || '').length).toBeGreaterThan(0);

    // not_saved (importable) → inline Import (ghost).
    const imp = rowCtl('ctx-agents', '[data-ctx-action="import"]');
    expect(imp).not.toBeNull();
    expect(imp.dataset.type).toBe('agents');
    expect(imp.classList.contains('btn-ghost')).toBe(true);

    // in_tools → decorative check, no action button.
    expect(rowCtl('ctx-commands', '.ctx-simple-check')).not.toBeNull();
    expect(rowCtl('ctx-commands', '[data-ctx-action]')).toBeNull();

    // attention → no safe one-click fix, keeps the read-only Manage deep-link.
    expect(rowCtl('ctx-mcp-servers', '.ctx-simple-manage')).not.toBeNull();
    expect(rowCtl('ctx-mcp-servers', '[data-ctx-action]')).toBeNull();
  });

  it('P1b: inline buttons run the shared _ctxRunSync/_ctxRunImport flow with an Overview refresh', async () => {
    const window = await boot();
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const syncCalls = [];
    const importCalls = [];
    // Global function bindings, so reassigning the window property swaps what the
    // delegated click handler resolves at call time (same idiom as the
    // switchSettingsSection override above) — isolates the wiring from the full
    // confirm flow.
    window._ctxRunSync = (type, opts) => { syncCalls.push({ type, opts }); };
    window._ctxRunImport = (type, opts) => { importCalls.push({ type, opts }); };

    window.document
      .querySelector('.ctx-simple-row[data-section="ctx-skills"] [data-ctx-action="sync"]')
      .click();
    window.document
      .querySelector('.ctx-simple-row[data-section="ctx-agents"] [data-ctx-action="import"]')
      .click();

    expect(syncCalls).toHaveLength(1);
    expect(syncCalls[0].type).toBe('skills');
    expect(syncCalls[0].opts.btn).not.toBeUndefined();
    expect(typeof syncCalls[0].opts.onComplete).toBe('function');
    expect(importCalls).toHaveLength(1);
    expect(importCalls[0].type).toBe('agents');
    expect(typeof importCalls[0].opts.onComplete).toBe('function');
  });

  it('P1b: mcp-servers not_saved falls back to Manage (no inline Import — no /import route)', async () => {
    const window = await boot(MCP_RUNTIME_ONLY);
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const row = window.document.querySelector('.ctx-simple-row[data-section="ctx-mcp-servers"]');
    expect(row.dataset.state).toBe('not_saved');
    expect(row.querySelector('[data-ctx-action]')).toBeNull();
    expect(row.querySelector('.ctx-simple-manage')).not.toBeNull();
  });

  it('P1b D-D: an empty active tier names items found in another tier', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    // Active tier (project_shared, no target_scope param) + project_local are
    // empty; the User tier holds items — so the cross-tier read names it.
    const upstream = window.fetch;
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      const [path, query] = url.split('?');
      const params = new URLSearchParams(query || '');
      if (path.includes('/api/context/projects')) return jsonOk({ scopes: SCOPES, target_scope: 'project_shared' });
      if (path.endsWith('/api/context/overview')) {
        return jsonOk(params.get('target_scope') === 'user' ? USER_TIER_OVERVIEW : EMPTY_OVERVIEW);
      }
      if (path.endsWith('/api/context/runtimes')) return jsonOk({ runtimes: [] });
      if (path.match(/\/api\/context\/(skills|commands|agents|mcp-servers)$/)) return jsonOk({ items: [] });
      return upstream(input, init);
    };
    await window.I18N.init();
    window.document.getElementById('tab-context-gateway').classList.add('active');
    setActiveSection(window, 'ctx-overview');
    window._ctxSetSimpleMode(true);
    await window.loadCtxOverview();
    await flush(window);

    const span = window.document.querySelector('.ctx-simple-empty-hint > span');
    expect(span).not.toBeNull();
    const expected = `${window.t('settings.ctx.simple_cross_tier_label')} `
      + window.t('settings.ctx.simple_cross_tier_entry', {
        count: 3,
        tier: window.t('settings.ctx.tier_option_user'),
      });
    expect(span.textContent).toBe(expected);
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

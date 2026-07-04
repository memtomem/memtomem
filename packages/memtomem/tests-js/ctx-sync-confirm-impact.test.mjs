/* U4 (#1229): Sync / Sync All confirms carry a create-vs-overwrite impact
 * summary and a conditional overwrite warning instead of click-and-pray copy.
 *
 * The impact preview is a best-effort pre-confirm fetch of the type list
 * (per-type Sync) or the four lists + overview (Sync All) under a pinned
 * (project, tier). These tests capture the showConfirm options via a window
 * override — `showConfirm` is a global function declaration (app.js), so the
 * bare call inside context-gateway.js resolves through window.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 30) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

const LIST_MIXED = {
  skills: [
    {
      name: 'a',
      runtimes: [
        { runtime: 'claude_skills', status: 'missing target' },
        { runtime: 'codex_skills', status: 'out of sync' },
        { runtime: 'gemini_skills', status: 'in sync' },
      ],
    },
    {
      name: 'b',
      runtimes: [
        { runtime: 'claude_skills', status: 'out of sync' },
        // Not sync writes — must not count:
        { runtime: 'codex_skills', status: 'missing canonical' },
        { runtime: 'kimi_skills', status: 'parse error' },
      ],
    },
  ],
};

const LIST_CLEAN = {
  skills: [
    { name: 'a', runtimes: [{ runtime: 'claude_skills', status: 'in sync' }] },
  ],
};

async function bootTypeSync({ listBody, listOk = true }) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  let captured = null;
  window.showConfirm = async (opts) => {
    captured = opts;
    return false; // never run the sync — we only inspect the dialog
  };
  window.ensureCsrfToken = async () => 'test-token';
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith('/api/context/skills')) {
      if (!listOk) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => listBody };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  // The handler reads the section's canonicalCount gate first.
  const section = window.document.getElementById('settings-ctx-skills');
  section.dataset.canonicalCount = '2';
  const btn = section.querySelector('.ctx-sync-btn');
  btn.click();
  await flush(window);
  return { window, captured: () => captured };
}

describe('per-type Sync confirm impact (U4)', () => {
  it('names create/overwrite counts and target runtimes; warns on overwrites', async () => {
    const { captured } = await bootTypeSync({ listBody: LIST_MIXED });
    const opts = captured();
    expect(opts).toBeTruthy();
    // 1 missing target -> create, 2 out of sync -> overwrite.
    expect(opts.message).toContain('create 1');
    expect(opts.message).toContain('overwrite 2');
    // Runtime display names, sorted; gemini maps to Antigravity and only
    // write-target runtimes appear (kimi only had parse error -> absent).
    expect(opts.message).toContain('Claude Code, Codex');
    expect(opts.message).not.toContain('Antigravity');
    expect(opts.warningText).toContain('2');
  });

  it('all-in-sync payload: no warning, says already in sync (negative pin)', async () => {
    const { captured } = await bootTypeSync({ listBody: LIST_CLEAN });
    const opts = captured();
    expect(opts).toBeTruthy();
    expect(opts.message).toContain('already in sync');
    expect(opts.warningText || '').toBe('');
  });

  it('falls back to count-only copy when the impact fetch fails — confirm still opens', async () => {
    const { captured } = await bootTypeSync({ listBody: null, listOk: false });
    const opts = captured();
    expect(opts).toBeTruthy();
    // Base copy survives; no impact sentence, no warning.
    expect(opts.message).not.toContain('overwrite');
    expect(opts.warningText || '').toBe('');
  });
});

/* B-5 (#1288): the two Sync All confirms (dashboard ``#ctx-sync-all-btn`` and
 * the portal per-project card) share ``_ctxSyncAllPreview`` +
 * ``_ctxSyncAllConfirmCopy`` to render a per-type × per-runtime breakdown,
 * capped with "…and N more", degrading to the aggregate counts on a failed
 * list fetch and to the base copy when even the overview fails. */

// skills: 1 create → Claude Code, 1 overwrite → Codex; commands: 1 overwrite
// → Claude Code (branded labels, #1646 item 3); agents + mcp-servers clean.
// Totals: create 1, overwrite 2.
const SA_BODIES = {
  skills: [{
    name: 's1',
    runtimes: [
      { runtime: 'claude_skills', status: 'missing target' },
      { runtime: 'codex_skills', status: 'out of sync' },
    ],
  }],
  commands: [{ name: 'c1', runtimes: [{ runtime: 'claude_commands', status: 'out of sync' }] }],
  agents: [],
  'mcp-servers': [],
};

// All four types with BOTH a create and an overwrite → 8 segments, exercising
// the cap (_CTX_SYNC_BREAKDOWN_CAP = 4) + "…and 4 more".
const SA_BODIES_FULL = {
  skills: [{ name: 's', runtimes: [{ runtime: 'claude_skills', status: 'missing target' }, { runtime: 'codex_skills', status: 'out of sync' }] }],
  commands: [{ name: 'c', runtimes: [{ runtime: 'claude_commands', status: 'missing target' }, { runtime: 'codex_commands', status: 'out of sync' }] }],
  agents: [{ name: 'a', runtimes: [{ runtime: 'claude_agents', status: 'missing target' }, { runtime: 'codex_agents', status: 'out of sync' }] }],
  'mcp-servers': [{ name: 'm', runtimes: [{ runtime: 'project_mcp', status: 'missing target' }, { runtime: 'project_mcp', status: 'out of sync' }] }],
};

const SA_OVERVIEW_NO_SETTINGS = { settings: { missing_target: 0, out_of_sync: 0 } };

async function bootSyncAllEnv({ bodies = SA_BODIES, overview = SA_OVERVIEW_NO_SETTINGS, failTypes = [] } = {}) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  let captured = null;
  window.showConfirm = async (opts) => { captured = opts; return false; };
  window.ensureCsrfToken = async () => 'test-token';
  const upstream = window.fetch;
  window.fetch = async (input, fopts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    for (const type of ['skills', 'commands', 'agents', 'mcp-servers']) {
      if (path.endsWith(`/api/context/${type}`)) {
        if (failTypes.includes(type)) return { ok: false, status: 503, json: async () => ({}) };
        return { ok: true, status: 200, json: async () => ({ [type]: bodies[type] || [] }) };
      }
    }
    if (path.endsWith('/api/context/overview')) {
      if (overview === null) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => overview };
    }
    return upstream(input, fopts);
  };
  await window.I18N.init();
  return { window, captured: () => captured };
}

describe('Sync All confirm per-type × per-runtime breakdown (B-5 #1288)', () => {
  it('dashboard Sync All renders type×runtime breakdown lines + uncapped totals', async () => {
    const { window, captured } = await bootSyncAllEnv();
    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);
    const opts = captured();
    expect(opts).toBeTruthy();
    // Totals lead (uncapped) — create 1, overwrite 2 across the four lists.
    expect(opts.message).toContain('create 1');
    expect(opts.message).toContain('overwrite 2');
    // Per-type × per-runtime segments, capitalized type heads + mapped runtimes.
    expect(opts.message).toContain('Skills: 1 create → Claude Code');
    expect(opts.message).toContain('Skills: 1 overwrite → Codex');
    expect(opts.message).toContain('Commands: 1 overwrite → Claude Code');
    // Overwrite total (artifact 2 + settings 0) drives the warning.
    expect(opts.warningText).toContain('2');
  });

  it('caps the breakdown at 4 segments and appends "…and N more"', async () => {
    const { window, captured } = await bootSyncAllEnv({ bodies: SA_BODIES_FULL });
    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);
    const opts = captured();
    expect(opts).toBeTruthy();
    // 8 segments (skills/commands ×{create,overwrite} shown), agents + mcp omitted.
    expect(opts.message).toContain('…and 4 more');
    expect(opts.message).not.toContain('Subagents');
    expect(opts.message).not.toContain('MCP servers');
  });

  it('degrades to aggregate counts (no breakdown) when a list fetch fails but overview is up', async () => {
    const { window, captured } = await bootSyncAllEnv({
      failTypes: ['skills'],
      overview: {
        skills: { missing_target: 2, out_of_sync: 1 },
        commands: { missing_target: 0, out_of_sync: 0 },
        settings: { missing_target: 0, out_of_sync: 0 },
      },
    });
    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);
    const opts = captured();
    expect(opts).toBeTruthy();
    // Aggregate counts survive from the overview…
    expect(opts.message).toContain('create 2');
    expect(opts.message).toContain('overwrite 1');
    // …but NO per-type breakdown line (no runtime arrow).
    expect(opts.message).not.toContain('→');
    expect(opts.message).not.toContain('Skills:');
  });

  it('degrades to the base confirm when even the overview fails — dialog still opens', async () => {
    const { window, captured } = await bootSyncAllEnv({ failTypes: ['skills'], overview: null });
    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);
    const opts = captured();
    expect(opts).toBeTruthy();
    expect(opts.message).not.toContain('→');
    expect(opts.message).not.toContain('create');
    expect(opts.warningText || '').toBe('');
  });

  it('lists load but overview fails → base copy, NOT a settings-blind partial breakdown', async () => {
    // Overview is the floor: it is the only source of settings counts. If the
    // four lists load but the overview does not, showing the artifact breakdown
    // would silently omit settings writes and under-count the overwrite warning
    // (Codex impl-review Major). Fall through to the base confirm instead.
    const { window, captured } = await bootSyncAllEnv({ bodies: SA_BODIES, overview: null });
    window.document.getElementById('ctx-sync-all-btn').click();
    await flush(window);
    const opts = captured();
    expect(opts).toBeTruthy();
    expect(opts.message).not.toContain('→');
    expect(opts.message).not.toContain('Skills:');
    expect(opts.message).not.toContain('create');
    expect(opts.warningText || '').toBe('');
  });

  it('portal per-project card shares the breakdown helper (drives _ctxSyncProjectScope)', async () => {
    const { window, captured } = await bootSyncAllEnv();
    const btn = window.document.createElement('button');
    // Portal card sync targets a (possibly non-active) scope id directly.
    await window._ctxSyncProjectScope('scope-x', btn);
    await flush(window);
    const opts = captured();
    expect(opts).toBeTruthy();
    expect(opts.message).toContain('Skills: 1 create → Claude Code');
    expect(opts.message).toContain('Commands: 1 overwrite → Claude Code');
    expect(opts.warningText).toContain('2');
  });
});

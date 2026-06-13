/* #1314 — constrained mcp-servers variant of the Move/Copy modal.
 *
 * B-6 (#1289) shipped the full Move/Copy modal for skills/commands/agents. This
 * spec pins the mcp-servers branch the same modal now serves (engine A-12
 * #1282, web route already supports kind=mcp-servers):
 *
 *   - the detail-pane "Move / Copy" button renders for mcp-servers (canonical);
 *   - the modal opens in the CONSTRAINED shape — mode + tier fieldsets hidden,
 *     rename row hidden, only the destination-project picker shown, with the
 *     mcp note and the copy-only title;
 *   - the destination select EXCLUDES the source project (cross-project only)
 *     and lists only sync-eligible projects;
 *   - the dry-run body is pinned: copy / to_target_scope=project_shared /
 *     from_scope=project_shared / no as_name / an explicit to_project_scope_id;
 *   - Apply threads the project_shared confirm round-trip, then the
 *     destination-pinned "Sync now" hits /api/context/mcp-servers/sync;
 *   - with no eligible cross-project destination the modal shows the dedicated
 *     no-destination warning, keeps Apply disabled, and fires no dry-run.
 *
 * Run from packages/memtomem/tests-js (a repo-root run collects stale worktree
 * copies of context-gateway.js).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 40) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

// Mimic switchSettingsSection's class bookkeeping so _ctxActiveGatewayType()'s
// ``.settings-section.active`` lookup resolves the intended section — lets the
// control-bar project switcher render so a test can change the active SOURCE
// scope without the full tab machinery (mirrors ctx-control-bar-hoist.test.mjs).
function setActiveSection(window, sectionId) {
  window.document
    .querySelectorAll('#tab-context-gateway .settings-section')
    .forEach((s) => s.classList.remove('active'));
  const sec = window.document.getElementById(`settings-${sectionId}`);
  if (sec) sec.classList.add('active');
}

const NAME = 'demo-mcp';
const SKILL = 'demo-skill';   // used only by the shared-static-modal recovery test

const SKILL_DETAIL = {
  name: SKILL, content: 'name: demo\n', target_scope: 'project_shared',
  layout: 'flat', mtime_ns: '1700000000000000000', fields: {},
};
const SKILL_PLAN = {
  status: 'plan', transferred: false, kind: 'skills', name: SKILL, dst_name: SKILL,
  mode: 'copy', from_scope: 'project_shared', to_scope: 'project_local',
  src_project_scope_id: '', dst_project_scope_id: '',
  src_path: '/srv/.memtomem/skills/demo-skill.md',
  dst_path: '/srv/.memtomem/skills-local/demo-skill.md',
  needs_sync: false, sync_command: null, notes: [],
};

function scope(id, label, root, extra = {}) {
  return {
    scope_id: id, label, root, tier: 'project',
    sources: id === '' ? ['server-cwd'] : ['known-projects'],
    missing: false, stale: false, experimental: false, enabled: true,
    sync_eligible: true,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': id === '' ? 1 : 0 },
    ...extra,
  };
}

// Source = Server CWD ('' — active by default). Two named cross-project
// destinations + (optionally) a paused one that must be filtered out.
const SCOPES = [
  scope('', 'Server CWD', '/srv'),
  scope('proj-a', 'Project A', '/work/a'),
  scope('proj-b', 'Project B', '/work/b'),
];

const DETAIL = {
  name: NAME, content: '{"mcpServers": {}}\n', target_scope: 'project_shared',
  layout: 'flat', mtime_ns: '1700000000000000000',
  fields: { command: 'node', args_count: 1, env_count: 0 },
};

const PLAN = {
  ok: true,
  body: {
    status: 'plan', transferred: false, kind: 'mcp-servers', name: NAME, dst_name: NAME,
    mode: 'copy', from_scope: 'project_shared', to_scope: 'project_shared',
    src_project_scope_id: '', dst_project_scope_id: 'proj-a',
    src_path: '/srv/.memtomem/mcp_servers/demo-mcp.json',
    dst_path: '/work/a/.memtomem/mcp_servers/demo-mcp.json',
    needs_sync: true, sync_command: 'cd /work/a && mm context sync --include=mcp-servers',
    sync_hint: 'Run `mm context sync --include=mcp-servers` in Project A',
    notes: [],
  },
};

async function bootModal({ scopes = SCOPES, dryRun = PLAN, applyResponses = [], confirmAnswers = [] } = {}) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;

  const confirms = [];
  const answers = [...confirmAnswers];
  window.showConfirm = async (opts) => { confirms.push(opts); return answers.length ? answers.shift() : false; };
  const toasts = [];
  window.showToast = (msg, sev, options) => toasts.push({ msg, sev: sev || 'success', options: options || {} });
  window.ensureCsrfToken = async () => 'test-token';

  const transferCalls = [];
  const syncCalls = [];
  const applyQueue = [...applyResponses];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith(`/api/context/mcp-servers/${NAME}/transfer`)) {
      const isDry = url.includes('dry_run');
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      const csrf = opts && opts.headers ? opts.headers['X-Memtomem-CSRF'] : undefined;
      transferCalls.push({ url, isDry, body, csrf });
      const resp = isDry ? dryRun : (applyQueue.length ? applyQueue.shift() : { ok: true, body: { ...PLAN.body, status: 'ok', transferred: true } });
      return { ok: resp.ok !== false, status: resp.status || 200, json: async () => resp.body };
    }
    if (path.endsWith('/api/context/mcp-servers/sync')) {
      syncCalls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null, csrf: opts?.headers?.['X-Memtomem-CSRF'] });
      return { ok: true, status: 200, json: async () => ({ generated: [], dropped: [], skipped: [] }) };
    }
    if (path.endsWith(`/api/context/mcp-servers/${NAME}`)) {
      return { ok: true, status: 200, json: async () => DETAIL };
    }
    // Skills routes — only exercised by the shared-static-modal recovery test
    // (open mcp → close → open a skills artifact in the same static modal).
    if (path.endsWith(`/api/context/skills/${SKILL}/transfer`)) {
      return { ok: true, status: 200, json: async () => SKILL_PLAN };
    }
    if (path.endsWith(`/api/context/skills/${SKILL}`)) {
      return { ok: true, status: 200, json: async () => SKILL_DETAIL };
    }
    if (path.endsWith('/api/context/skills')) {
      return { ok: true, status: 200, json: async () => ({ skills: [{ name: SKILL, runtimes: [] }] }) };
    }
    if (path.endsWith('/api/context/mcp-servers')) {
      return { ok: true, status: 200, json: async () => ({ 'mcp-servers': [{ name: NAME, runtimes: [], canonical_path: DETAIL.src_path }] }) };
    }
    if (path.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes, target_scope: 'project_shared' }) };
    }
    return upstream(input, opts);
  };

  await window.I18N.init();
  if (!window.CSS) {
    window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
  }
  await window.loadCtxList('mcp-servers');
  await flush(window);
  await window.loadCtxDetail('mcp-servers', NAME);
  await flush(window);
  return { window, confirms, toasts, transferCalls, syncCalls };
}

function modalOf(window) { return window.document.getElementById('ctx-move-copy-modal'); }
function $(window, id) { return window.document.getElementById(id); }

async function openModal(ctx) {
  const detail = $(ctx.window, 'ctx-mcp-servers-detail');
  const btn = detail.querySelector('.ctx-detail-move-copy-btn');
  expect(btn, 'move/copy button should render for canonical mcp-servers').toBeTruthy();
  btn.click();
  await flush(ctx.window);
}

describe('Move/Copy modal — mcp-servers constrained variant (#1314)', () => {
  it('opens a copy-only, project_shared-pinned modal hiding mode/tier/rename', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const w = ctx.window;
    expect(modalOf(w).hidden).toBe(false);
    // Constrained shape: mode + tier fieldsets and the rename row are hidden;
    // only the destination-project picker and the mcp note remain.
    expect($(w, 'ctx-mc-mode-field').hidden).toBe(true);
    expect($(w, 'ctx-mc-tier-field').hidden).toBe(true);
    expect($(w, 'ctx-mc-rename-row').hidden).toBe(true);
    expect($(w, 'ctx-mc-project-row').hidden).toBe(false);
    expect($(w, 'ctx-mc-mcp-note').hidden).toBe(false);
    // Copy-only title (not the generic "Move or copy").
    expect($(w, 'ctx-mc-title').textContent).toBe(w.I18N.t('settings.ctx.move_copy_mcp_title'));
  });

  it('excludes the source project from the destination picker (cross-project only)', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const sel = $(ctx.window, 'ctx-mc-project');
    const values = Array.from(sel.options).map((o) => o.value);
    // Source is Server CWD (''); destinations are the two named projects only.
    expect(values).not.toContain('');
    expect(values.sort()).toEqual(['proj-a', 'proj-b']);
  });

  it('pins the dry-run body: copy / project_shared / no as_name / explicit destination', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const dry = ctx.transferCalls.filter((c) => c.isDry);
    expect(dry.length).toBeGreaterThanOrEqual(1);
    expect(dry[0].csrf).toBe('test-token');
    const b = dry[0].body;
    expect(b.mode).toBe('copy');
    expect(b.to_target_scope).toBe('project_shared');
    expect(b.from_scope).toBe('project_shared');
    expect(b.to_project_scope_id).toBe('proj-a');   // first eligible destination
    expect(b.as_name).toBeUndefined();
    // A clean plan enables Apply.
    expect($(ctx.window, 'ctx-mc-apply-btn').disabled).toBe(false);
  });

  it('re-targets the destination on a project-select change', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const sel = $(ctx.window, 'ctx-mc-project');
    sel.value = 'proj-b';
    sel.dispatchEvent(new ctx.window.Event('change'));
    await flush(ctx.window);
    const lastDry = ctx.transferCalls.filter((c) => c.isDry).pop();
    expect(lastDry.body.to_project_scope_id).toBe('proj-b');
  });

  it('threads the project_shared confirm round-trip and offers a destination-pinned Sync now', async () => {
    const ctx = await bootModal({
      applyResponses: [
        { ok: true, body: { status: 'needs_confirmation', confirm: 'confirm_project_shared', reason: 'shared write', plan: PLAN.body } },
        { ok: true, body: { ...PLAN.body, status: 'ok', transferred: true, dst_project_scope_id: 'proj-a' } },
      ],
      confirmAnswers: [true],
    });
    await openModal(ctx);
    $(ctx.window, 'ctx-mc-apply-btn').click();
    await flush(ctx.window);
    const applies = ctx.transferCalls.filter((c) => !c.isDry);
    expect(applies.length).toBe(2);
    expect(applies[0].body.confirm_project_shared).toBe(false);
    expect(applies[1].body.confirm_project_shared).toBe(true);
    expect(ctx.confirms.length).toBe(1);            // shared confirm, not host-write
    expect(modalOf(ctx.window).hidden).toBe(true);
    // Success toast carries the destination-pinned "Sync now" follow-up.
    const successToast = ctx.toasts.find((x) => x.sev === 'success' && x.options && x.options.action);
    expect(successToast, 'success toast should carry a Sync-now action').toBeTruthy();
    successToast.options.action.onClick();
    await flush(ctx.window);
    expect(ctx.syncCalls.length).toBe(1);
    expect(ctx.syncCalls[0].url).toContain('/api/context/mcp-servers/sync');
    expect(ctx.syncCalls[0].url).toContain('scope_id=proj-a');   // destination, not source
    expect(ctx.syncCalls[0].csrf).toBe('test-token');
  });

  it('shows a no-destination warning and fires no dry-run when only the source exists', async () => {
    const ctx = await bootModal({ scopes: [scope('', 'Server CWD', '/srv')] });
    await openModal(ctx);
    const w = ctx.window;
    expect($(w, 'ctx-mc-project').options.length).toBe(0);
    const warn = $(w, 'ctx-mc-warning');
    expect(warn.hidden).toBe(false);
    expect(warn.textContent).toBe(w.I18N.t('settings.ctx.move_copy_mcp_no_dest'));
    expect($(w, 'ctx-mc-apply-btn').disabled).toBe(true);
    // No dry-run POST is issued — it would only 400 ("cross-project only").
    expect(ctx.transferCalls.length).toBe(0);
  });

  it('sends to_project_scope_id="" verbatim for a named-source → Server-CWD copy', async () => {
    // The inverse of the default fixture: when the SOURCE is a named project,
    // Server-CWD ('') becomes a valid cross-project destination. Its empty-string
    // scope_id must reach the body verbatim — collapsing it to null would 400 as
    // a same-project copy. The options.length guard (not value-truthiness)
    // distinguishes 'Server-CWD selected' ('') from 'no eligible destination'.
    const ctx = await bootModal();   // roster: Server CWD '', proj-a, proj-b
    // Switch the active SOURCE to proj-a via the control-bar project switcher.
    setActiveSection(ctx.window, 'ctx-mcp-servers');
    ctx.window._ctxRenderControlBar();
    const switcher = ctx.window.document.querySelector('.ctx-project-select');
    expect(switcher, 'control-bar project switcher should render').toBeTruthy();
    switcher.value = 'proj-a';
    switcher.dispatchEvent(new ctx.window.Event('change'));
    await flush(ctx.window);
    await ctx.window.loadCtxDetail('mcp-servers', NAME);
    await flush(ctx.window);
    await openModal(ctx);
    const sel = $(ctx.window, 'ctx-mc-project');
    const values = Array.from(sel.options).map((o) => o.value);
    expect(values).toContain('');            // Server-CWD is a valid destination
    expect(values).not.toContain('proj-a');  // the source is excluded
    expect(sel.value).toBe('');              // first option (Server-CWD) selected
    const lastDry = ctx.transferCalls.filter((c) => c.isDry).pop();
    expect(lastDry.body.to_project_scope_id).toBe('');   // verbatim, NOT null
  });

  it('restores the full skills shape after an mcp session (shared static modal)', async () => {
    // The modal markup is shared static DOM. After an mcp session hides the
    // mode/tier fieldsets + shows the note, opening a skills artifact must
    // RE-SHOW the full surface and hide the note (the B-6 regression spot).
    const ctx = await bootModal();
    await openModal(ctx);                         // mcp session — constrained
    expect($(ctx.window, 'ctx-mc-mode-field').hidden).toBe(true);
    expect($(ctx.window, 'ctx-mc-mcp-note').hidden).toBe(false);
    $(ctx.window, 'ctx-mc-cancel-btn').click();
    await flush(ctx.window);
    expect(modalOf(ctx.window).hidden).toBe(true);
    // Now open a skills artifact's Move/Copy modal in the same static DOM.
    await ctx.window.loadCtxDetail('skills', SKILL);
    await flush(ctx.window);
    ctx.window.document
      .getElementById('ctx-skills-detail')
      .querySelector('.ctx-detail-move-copy-btn')
      .click();
    await flush(ctx.window);
    expect(modalOf(ctx.window).hidden).toBe(false);
    expect($(ctx.window, 'ctx-mc-mode-field').hidden).toBe(false);   // re-shown
    expect($(ctx.window, 'ctx-mc-tier-field').hidden).toBe(false);   // re-shown
    expect($(ctx.window, 'ctx-mc-rename-row').hidden).toBe(false);   // copy default
    expect($(ctx.window, 'ctx-mc-mcp-note').hidden).toBe(true);      // note hidden
    expect($(ctx.window, 'ctx-mc-title').textContent)
      .toBe(ctx.window.I18N.t('settings.ctx.move_copy_title'));      // generic title back
  });

  it('filters paused (non-sync-eligible) projects out of the destination picker', async () => {
    const ctx = await bootModal({
      scopes: [
        scope('', 'Server CWD', '/srv'),
        scope('proj-a', 'Project A', '/work/a'),
        scope('proj-paused', 'Paused', '/work/p', { sync_eligible: false, sources: ['known-projects'] }),
      ],
    });
    await openModal(ctx);
    const values = Array.from($(ctx.window, 'ctx-mc-project').options).map((o) => o.value);
    expect(values).toEqual(['proj-a']);
  });
});

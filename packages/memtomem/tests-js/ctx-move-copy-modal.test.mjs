/* B-6 #1289 — per-artifact Move/Copy destination modal.
 *
 * Pins the JS state machine that drives POST /api/context/{kind}/{name}/transfer
 * (the A-5 #1276 endpoint):
 *
 *   - the detail-pane "Move / Copy" button opens the modal (transfer kinds only);
 *   - every destination change runs a dry-run preview (?dry_run=1) that gates
 *     Apply — a clean plan enables it, a 409 destination_exists collision shows
 *     an inline warning and keeps it disabled;
 *   - CSRF rides every transfer POST (CSRFGuardMiddleware);
 *   - Apply threads the tier-keyed gate round-trip: project_shared →
 *     confirm_project_shared (shared confirm), user → allow_host_writes
 *     (the shared host-write disclosure), re-POSTing with the flag set;
 *   - rename is copy-only (the move mode hides the field and sends no as_name);
 *   - cancel / Escape close the modal without transferring.
 *
 * Run from packages/memtomem/tests-js (a repo-root run collects stale worktree
 * copies of context-gateway.js).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 40) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

const NAME = 'demo-skill';

// Healthy two-project roster — an empty scopes array drops the list into the
// #1287 load-error path (Retry banner) and buries everything.
const SCOPES = [
  {
    scope_id: '', label: 'Server CWD', root: '/srv', tier: 'project',
    sources: ['server-cwd'], missing: false, stale: false, experimental: false,
    enabled: true, sync_eligible: true,
    counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
  {
    scope_id: 'proj-dest', label: 'Dest Project', root: '/work/dest', tier: 'project',
    sources: ['known-projects'], missing: false, stale: false, experimental: false,
    enabled: true, sync_eligible: true,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
];

const DETAIL = {
  content: 'name: demo\n', target_scope: 'project_shared', layout: 'flat',
  files: [], mtime_ns: '1700000000000000000', fields: {},
};

const PLAN = {
  ok: true,
  body: {
    status: 'plan', transferred: false, kind: 'skills', name: NAME, dst_name: NAME,
    mode: 'copy', from_scope: 'project_shared', to_scope: 'project_local',
    src_project_scope_id: '', dst_project_scope_id: '',
    src_path: '/srv/.memtomem/skills/demo-skill.md',
    dst_path: '/srv/.memtomem/skills-local/demo-skill.md',
    needs_sync: false, sync_command: null, notes: [],
  },
};
const OK_APPLIED = { ok: true, body: { ...PLAN.body, status: 'ok', transferred: true } };
const COLLISION_409 = {
  ok: false, status: 409,
  body: { detail: { error_kind: 'conflict', reason_code: 'destination_exists', message: 'destination already exists: /srv/x' } },
};

async function bootModal({ dryRun = PLAN, applyResponses = [OK_APPLIED], confirmAnswers = [], holdApply = false } = {}) {
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
  // Optional gate to hold an apply in flight (test mid-apply close/reopen).
  let releaseApply = () => {};
  const applyGate = holdApply ? new Promise((res) => { releaseApply = res; }) : null;
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith(`/api/context/skills/${NAME}/transfer`)) {
      const isDry = url.includes('dry_run');
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      const csrf = opts && opts.headers ? opts.headers['X-Memtomem-CSRF'] : undefined;
      transferCalls.push({ url, isDry, body, csrf });
      if (!isDry && applyGate) await applyGate;
      const resp = isDry ? dryRun : (applyQueue.length ? applyQueue.shift() : OK_APPLIED);
      return { ok: resp.ok !== false, status: resp.status || 200, json: async () => resp.body };
    }
    if (path.endsWith('/api/context/skills/sync')) {
      syncCalls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null, csrf: opts?.headers?.['X-Memtomem-CSRF'] });
      return { ok: true, status: 200, json: async () => ({ generated: [], dropped: [], skipped: [] }) };
    }
    if (path.endsWith(`/api/context/skills/${NAME}`)) {
      return { ok: true, status: 200, json: async () => DETAIL };
    }
    if (path.endsWith('/api/context/skills')) {
      return { ok: true, status: 200, json: async () => ({ skills: [{ name: NAME, runtimes: [] }] }) };
    }
    if (path.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: SCOPES, target_scope: 'project_shared' }) };
    }
    return upstream(input, opts);
  };

  await window.I18N.init();
  if (!window.CSS) {
    window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
  }
  await window.loadCtxList('skills');
  await flush(window);
  await window.loadCtxDetail('skills', NAME);
  await flush(window);
  return { window, confirms, toasts, transferCalls, syncCalls, releaseApply };
}

function modalOf(window) { return window.document.getElementById('ctx-move-copy-modal'); }
function clickRadio(window, name, value) {
  const el = modalOf(window).querySelector(`input[name="${name}"][value="${value}"]`);
  el.click();
  return el;
}
async function openModal(ctx) {
  const detail = ctx.window.document.getElementById('ctx-skills-detail');
  const btn = detail.querySelector('.ctx-detail-move-copy-btn');
  expect(btn, 'move/copy button should render for skills').toBeTruthy();
  btn.click();
  await flush(ctx.window);
}

describe('Move/Copy modal — open + dry-run preview (#1289)', () => {
  it('opens the modal and runs a CSRF-bearing dry-run that enables Apply', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const modal = modalOf(ctx.window);
    expect(modal.hidden).toBe(false);
    const dry = ctx.transferCalls.filter((c) => c.isDry);
    expect(dry.length).toBeGreaterThanOrEqual(1);
    // CSRF header threads every transfer POST, including the preview leg.
    expect(dry[0].csrf).toBe('test-token');
    // Defaults: copy, from the source tier, to a differing tier (no same-store no-op).
    expect(dry[0].body.mode).toBe('copy');
    expect(dry[0].body.from_scope).toBe('project_shared');
    expect(dry[0].body.to_target_scope).toBe('project_local');
    // A clean plan enables Apply.
    expect(ctx.window.document.getElementById('ctx-mc-apply-btn').disabled).toBe(false);
  });

  it('disables Apply and shows an inline warning on a destination_exists collision', async () => {
    const ctx = await bootModal({ dryRun: COLLISION_409 });
    await openModal(ctx);
    const warn = ctx.window.document.getElementById('ctx-mc-warning');
    expect(warn.hidden).toBe(false);
    expect(warn.textContent.length).toBeGreaterThan(0);
    expect(ctx.window.document.getElementById('ctx-mc-apply-btn').disabled).toBe(true);
    // No apply POST is reachable while Apply is disabled.
    expect(ctx.transferCalls.every((c) => c.isDry)).toBe(true);
  });

  it('hides the rename row in Move mode and sends no as_name', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const renameInput = ctx.window.document.getElementById('ctx-mc-rename');
    renameInput.value = 'renamed';
    clickRadio(ctx.window, 'ctx-mc-mode', 'move');
    await flush(ctx.window);
    expect(ctx.window.document.getElementById('ctx-mc-rename-row').hidden).toBe(true);
    const lastDry = ctx.transferCalls.filter((c) => c.isDry).pop();
    expect(lastDry.body.mode).toBe('move');
    expect(lastDry.body.as_name).toBeUndefined();
  });

  it('sends an explicit destination project on a cross-project copy', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const sel = ctx.window.document.getElementById('ctx-mc-project');
    sel.value = 'proj-dest';
    sel.dispatchEvent(new ctx.window.Event('change'));
    await flush(ctx.window);
    const lastDry = ctx.transferCalls.filter((c) => c.isDry).pop();
    expect(lastDry.body.to_project_scope_id).toBe('proj-dest');
  });
});

describe('Move/Copy modal — apply gate round-trips (#1289)', () => {
  it('threads the project_shared confirm round-trip', async () => {
    const ctx = await bootModal({
      applyResponses: [
        { ok: true, body: { status: 'needs_confirmation', confirm: 'confirm_project_shared', reason: 'shared write', plan: PLAN.body } },
        OK_APPLIED,
      ],
      confirmAnswers: [true],
    });
    await openModal(ctx);
    clickRadio(ctx.window, 'ctx-mc-tier', 'project_shared');
    await flush(ctx.window);
    ctx.window.document.getElementById('ctx-mc-apply-btn').click();
    await flush(ctx.window);
    const applies = ctx.transferCalls.filter((c) => !c.isDry);
    expect(applies.length).toBe(2);
    expect(applies[0].body.confirm_project_shared).toBe(false);
    expect(applies[1].body.confirm_project_shared).toBe(true);
    // The gate uses the shared confirm dialog (not the host-write disclosure).
    expect(ctx.confirms.length).toBe(1);
    expect(modalOf(ctx.window).hidden).toBe(true);
    expect(ctx.toasts.some((x) => x.sev === 'success')).toBe(true);
  });

  it('threads the user-tier host-write disclosure round-trip', async () => {
    const ctx = await bootModal({
      applyResponses: [
        { ok: true, body: { status: 'needs_confirmation', confirm: 'allow_host_writes', host_targets: ['/home/u/.memtomem/skills/demo-skill.md'], plan: { ...PLAN.body, to_scope: 'user' } } },
        { ok: true, body: { ...PLAN.body, status: 'ok', transferred: true, to_scope: 'user', dst_project_scope_id: null } },
      ],
      confirmAnswers: [true],
    });
    await openModal(ctx);
    clickRadio(ctx.window, 'ctx-mc-tier', 'user');
    await flush(ctx.window);
    // user tier is global — the destination project row is hidden.
    expect(ctx.window.document.getElementById('ctx-mc-project-row').hidden).toBe(true);
    ctx.window.document.getElementById('ctx-mc-apply-btn').click();
    await flush(ctx.window);
    const applies = ctx.transferCalls.filter((c) => !c.isDry);
    expect(applies.length).toBe(2);
    expect(applies[0].body.allow_host_writes).toBe(false);
    expect(applies[1].body.allow_host_writes).toBe(true);
    // The disclosure listed the host target path.
    const disclosure = ctx.confirms[ctx.confirms.length - 1];
    expect(disclosure.warningText).toContain('/home/u/.memtomem/skills/demo-skill.md');
  });

  it('declining the gate sends no apply and leaves the modal open', async () => {
    const ctx = await bootModal({
      applyResponses: [
        { ok: true, body: { status: 'needs_confirmation', confirm: 'confirm_project_shared', reason: 'shared write', plan: PLAN.body } },
        OK_APPLIED,
      ],
      confirmAnswers: [false],
    });
    await openModal(ctx);
    clickRadio(ctx.window, 'ctx-mc-tier', 'project_shared');
    await flush(ctx.window);
    ctx.window.document.getElementById('ctx-mc-apply-btn').click();
    await flush(ctx.window);
    const applies = ctx.transferCalls.filter((c) => !c.isDry);
    expect(applies.length).toBe(1);            // first POST only; decline stops the re-POST
    expect(modalOf(ctx.window).hidden).toBe(false);
    expect(ctx.toasts.some((x) => x.sev === 'success')).toBe(false);
  });

  it('re-disables Apply when the apply itself hits a collision (TOCTOU race)', async () => {
    // The preview is clean (Apply enabled), but the engine re-checks the
    // destination after the pair-lock acquire and can 409 destination_exists
    // even when the dry-run did not — that collision is terminal, so Apply must
    // go back to disabled instead of staying clickable on the same destination.
    const ctx = await bootModal({
      applyResponses: [{
        ok: false, status: 409,
        body: { detail: { error_kind: 'conflict', reason_code: 'destination_exists',
                          message: 'destination appeared during lock acquire' } },
      }],
    });
    await openModal(ctx);
    const applyBtn = ctx.window.document.getElementById('ctx-mc-apply-btn');
    expect(applyBtn.disabled).toBe(false);     // clean dry-run enabled it
    applyBtn.click();
    await flush(ctx.window);
    const warn = ctx.window.document.getElementById('ctx-mc-warning');
    expect(warn.hidden).toBe(false);           // collision warning surfaced
    expect(applyBtn.disabled).toBe(true);      // terminal — back to disabled
    expect(modalOf(ctx.window).hidden).toBe(false);
  });

  it('locks the destination controls while an apply is in flight', async () => {
    // Prevents the user from changing the destination mid-apply (which would
    // start a new dry-run a late apply failure could then clobber). The
    // synchronous prefix of the click handler disables the controls before the
    // first await, so they read disabled immediately after the click.
    const ctx = await bootModal({
      applyResponses: [{
        ok: false, status: 409,
        body: { detail: { error_kind: 'conflict', reason_code: 'destination_exists', message: 'race' } },
      }],
    });
    await openModal(ctx);
    const modal = modalOf(ctx.window);
    const tier = modal.querySelector('input[name="ctx-mc-tier"][value="project_local"]');
    const rename = ctx.window.document.getElementById('ctx-mc-rename');
    ctx.window.document.getElementById('ctx-mc-apply-btn').click();
    expect(tier.disabled, 'tier radio locked during apply').toBe(true);
    expect(rename.disabled, 'rename locked during apply').toBe(true);
    await flush(ctx.window);
    // Unlocked again after the apply settles so the user can change the
    // destination to clear the (terminal) collision.
    expect(tier.disabled).toBe(false);
    expect(rename.disabled).toBe(false);
  });

  it('a close mid-apply unlocks the shared controls; a stale settle is ignored', async () => {
    // The modal/controls are shared static DOM. Closing while an apply is in
    // flight must reset the controls (so a reopen is not frozen), and the held
    // apply settling later must not touch the superseded modal.
    const ctx = await bootModal({ holdApply: true });
    await openModal(ctx);
    const modal = modalOf(ctx.window);
    const tier = modal.querySelector('input[name="ctx-mc-tier"][value="project_local"]');
    ctx.window.document.getElementById('ctx-mc-apply-btn').click();
    await flush(ctx.window);
    expect(tier.disabled).toBe(true);          // locked while the apply is held
    ctx.window.document.getElementById('ctx-mc-cancel-btn').click();   // close mid-apply
    await flush(ctx.window);
    expect(modal.hidden).toBe(true);
    expect(tier.disabled).toBe(false);         // close reset the shared control
    ctx.releaseApply();                        // the stale apply settles after close
    await flush(ctx.window);
    expect(tier.disabled).toBe(false);         // it owns a superseded state — no re-lock
  });

  it('offers a destination-pinned "Sync now" follow-up when the apply needs sync', async () => {
    const ctx = await bootModal({
      applyResponses: [
        { ok: true, body: { ...PLAN.body, status: 'ok', transferred: true, to_scope: 'project_shared', dst_project_scope_id: 'proj-dest', needs_sync: true, sync_command: 'cd /work/dest && mm context sync' } },
      ],
    });
    await openModal(ctx);
    clickRadio(ctx.window, 'ctx-mc-tier', 'project_shared');
    const sel = ctx.window.document.getElementById('ctx-mc-project');
    sel.value = 'proj-dest';
    sel.dispatchEvent(new ctx.window.Event('change'));
    await flush(ctx.window);
    ctx.window.document.getElementById('ctx-mc-apply-btn').click();
    await flush(ctx.window);
    const successToast = ctx.toasts.find((x) => x.sev === 'success' && x.options && x.options.action);
    expect(successToast, 'success toast should carry a Sync-now action').toBeTruthy();
    successToast.options.action.onClick();
    await flush(ctx.window);
    // The follow-up sync pins the DESTINATION project (proj-dest), NOT the
    // active UI scope (server-cwd ''). target_scope is project_shared, which is
    // the route default and so is correctly omitted from the URL (a non-default
    // destination tier would emit it; project_shared never does).
    expect(ctx.syncCalls.length).toBe(1);
    expect(ctx.syncCalls[0].url).toContain('scope_id=proj-dest');
    expect(ctx.syncCalls[0].url).not.toContain('target_scope=project_local');
    expect(ctx.syncCalls[0].csrf).toBe('test-token');
  });
});

describe('Move/Copy modal — dismissal (#1289)', () => {
  it('closes on Cancel without transferring', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    ctx.window.document.getElementById('ctx-mc-cancel-btn').click();
    await flush(ctx.window);
    expect(modalOf(ctx.window).hidden).toBe(true);
    expect(ctx.transferCalls.every((c) => c.isDry)).toBe(true);
  });

  it('closes on Escape without transferring', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    ctx.window.document.dispatchEvent(new ctx.window.KeyboardEvent('keydown', { key: 'Escape' }));
    await flush(ctx.window);
    expect(modalOf(ctx.window).hidden).toBe(true);
    expect(ctx.transferCalls.every((c) => c.isDry)).toBe(true);
  });
});

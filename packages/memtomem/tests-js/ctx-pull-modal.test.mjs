/* ADR-0030 PR-D2 — source-selectable Pull picker (#ctx-pull-modal).
 *
 * Pins the JS state machine that drives GET …/pull-preview → POST …/pull:
 *
 *   - the detail-pane "Pull" button opens the modal (Pull-eligible kinds);
 *   - the preview paints a candidate radio table; an unambiguous source is
 *     auto-selected and enables Apply, while divergent copies (§5 ambiguous)
 *     leave Apply disabled until the user names a source;
 *   - the overwrite checkbox shows only when the Store already holds the item,
 *     and the force-unsafe checkbox only for a selected copy whose privacy gate
 *     is a bypassable warning; a hard project_shared block disables Apply;
 *   - CSRF rides the POST; Apply threads the tier-keyed gate round-trip
 *     (needs_confirmation → re-POST with the named flag) and toasts on success;
 *   - Cancel / Escape close without pulling.
 *
 * Run from packages/memtomem/tests-js.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 40) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

const NAME = 'demo-skill';
const DETAIL = {
  content: 'name: demo\n', target_scope: 'project_shared', layout: 'flat',
  files: [], mtime_ns: '1700000000000000000', fields: {},
};

function _c(runtime, content_status, gate_status, landing_group = 0) {
  return {
    runtime, content_status, gate_status, importable: true,
    landing_group, override_warning: false, reason: null,
  };
}

// Preview builder — candidates are two-axis rows (content_status / gate_status).
function preview(over = {}) {
  return {
    kind: 'skills', name: NAME, target_scope: 'project_shared', store_present: false,
    candidates: [{ runtime: 'claude', content_status: 'new', gate_status: 'ok', importable: true, landing_group: 0, override_warning: false, reason: null }],
    distinct_landing_count: 1, ambiguous: false, auto_source: 'claude',
    ...over,
  };
}

const APPLIED = { ok: true, body: { status: 'applied', kind: 'skills', name: NAME, target_scope: 'project_shared', reason: 'ok', reason_code: null, selected_runtime: 'claude', write_outcome: 'created', duplicate_runtimes: [], canonical_path: '.memtomem/skills/demo-skill', candidates: [], distinct_landing_count: 0, gate_status: null, gate_hits: null, force_bypassable: false } };
const NEEDS_CONFIRM_SHARED = { ok: true, body: { status: 'needs_confirmation', confirm: 'confirm_project_shared', reason: 'raw dev prose', host_targets: [] } };

async function bootModal({ previewBody = preview(), applyResponses = [APPLIED], confirmAnswers = [], activeScope = '', scopes = [], holdPreviewAfter = Infinity } = {}) {
  const seedStorage = activeScope ? { memtomem_ctx_active_scope_id: activeScope } : {};
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'], seedStorage });
  const { window } = dom;

  const confirms = [];
  const answers = [...confirmAnswers];
  window.showConfirm = async (opts) => { confirms.push(opts); return answers.length ? answers.shift() : false; };
  const toasts = [];
  window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });
  window.ensureCsrfToken = async () => 'test-token';

  const previewCalls = [];
  const pullCalls = [];
  const applyQueue = [...applyResponses];
  let releasePreview = () => {};
  const previewGate = new Promise((res) => { releasePreview = res; });
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith(`/api/context/skills/${NAME}/pull-preview`)) {
      previewCalls.push({ url });
      if (previewCalls.length > holdPreviewAfter) await previewGate; // gate late previews
      return { ok: true, status: 200, json: async () => previewBody };
    }
    if (path.endsWith(`/api/context/skills/${NAME}/pull`)) {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      pullCalls.push({ url, body, csrf: opts?.headers?.['X-Memtomem-CSRF'] });
      const resp = applyQueue.length ? applyQueue.shift() : APPLIED;
      return { ok: resp.ok !== false, status: resp.status || 200, json: async () => resp.body };
    }
    if (path.endsWith(`/api/context/skills/${NAME}`)) {
      return { ok: true, status: 200, json: async () => DETAIL };
    }
    if (path.endsWith('/api/context/skills')) {
      return { ok: true, status: 200, json: async () => ({ skills: [{ name: NAME, runtimes: [] }] }) };
    }
    if (path.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes, target_scope: 'project_shared' }) };
    }
    return upstream(input, opts);
  };

  await window.I18N.init();
  if (!window.CSS) window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
  await window.loadCtxList('skills');
  await flush(window);
  await window.loadCtxDetail('skills', NAME);
  await flush(window);
  return { window, confirms, toasts, previewCalls, pullCalls, releasePreview };
}

function modalOf(window) { return window.document.getElementById('ctx-pull-modal'); }

async function openModal(ctx) {
  const detail = ctx.window.document.getElementById('ctx-skills-detail');
  const btn = detail.querySelector('.ctx-detail-pull-btn');
  expect(btn, 'Pull button should render for skills').toBeTruthy();
  btn.click();
  await flush(ctx.window);
}

describe('Pull picker — open + preview (ADR-0030 PR-D2)', () => {
  it('opens the modal and auto-selects the unambiguous source, enabling Apply', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    const modal = modalOf(ctx.window);
    expect(modal.hidden).toBe(false);
    expect(ctx.previewCalls.length).toBe(1);
    expect(ctx.previewCalls[0].url).toContain('target_scope=project_shared');
    const radio = modal.querySelector('input[name="ctx-pull-source"][value="claude"]');
    expect(radio.checked).toBe(true);
    expect(ctx.window.document.getElementById('ctx-pull-apply-btn').disabled).toBe(false);
  });

  it('keeps Apply disabled for divergent copies until a source is picked (§5)', async () => {
    const ctx = await bootModal({
      previewBody: preview({
        ambiguous: true, auto_source: null, distinct_landing_count: 2,
        candidates: [
          { runtime: 'claude', content_status: 'differs', gate_status: 'ok', importable: true, landing_group: 0, override_warning: false, reason: null },
          { runtime: 'codex', content_status: 'new', gate_status: 'ok', importable: true, landing_group: 1, override_warning: false, reason: null },
        ],
      }),
    });
    await openModal(ctx);
    const modal = modalOf(ctx.window);
    const applyBtn = ctx.window.document.getElementById('ctx-pull-apply-btn');
    expect(applyBtn.disabled).toBe(true); // no auto-selection when ambiguous
    modal.querySelector('input[name="ctx-pull-source"][value="codex"]').click();
    await flush(ctx.window);
    expect(applyBtn.disabled).toBe(false); // a pick unblocks Apply
  });

  it('shows the overwrite checkbox only when the Store already holds the item', async () => {
    const ctx = await bootModal({ previewBody: preview({ store_present: true, candidates: [{ runtime: 'claude', content_status: 'differs', gate_status: 'ok', importable: true, landing_group: 0, override_warning: false, reason: null }] }) });
    await openModal(ctx);
    expect(ctx.window.document.getElementById('ctx-pull-overwrite-row').hidden).toBe(false);
  });

  it('shows the force valve only for a bypassable gate, and disables Apply on a hard block', async () => {
    const warnCtx = await bootModal({ previewBody: preview({ candidates: [{ runtime: 'claude', content_status: 'new', gate_status: 'requires_unsafe_confirmation', importable: true, landing_group: 0, override_warning: false, reason: null }] }) });
    await openModal(warnCtx);
    expect(warnCtx.window.document.getElementById('ctx-pull-force-row').hidden).toBe(false);
    expect(warnCtx.window.document.getElementById('ctx-pull-apply-btn').disabled).toBe(false);

    const hardCtx = await bootModal({ previewBody: preview({ candidates: [{ runtime: 'claude', content_status: 'new', gate_status: 'blocked', importable: true, landing_group: 0, override_warning: false, reason: null }] }) });
    await openModal(hardCtx);
    expect(hardCtx.window.document.getElementById('ctx-pull-force-row').hidden).toBe(true);
    expect(hardCtx.window.document.getElementById('ctx-pull-apply-btn').disabled).toBe(true); // no bypass
  });
});

describe('Pull picker — apply + consent round-trip', () => {
  it('POSTs with CSRF, confirms the shared-store write, re-POSTs with the flag, and toasts', async () => {
    const ctx = await bootModal({ applyResponses: [NEEDS_CONFIRM_SHARED, APPLIED], confirmAnswers: [true] });
    await openModal(ctx);
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);

    expect(ctx.pullCalls.length).toBe(2);
    expect(ctx.pullCalls[0].csrf).toBe('test-token');
    expect(ctx.pullCalls[0].body.source_runtime).toBe('claude');
    expect(ctx.pullCalls[0].body.confirm_project_shared).toBeUndefined(); // first POST has no consent
    expect(ctx.confirms.length).toBe(1);
    expect(ctx.pullCalls[1].body.confirm_project_shared).toBe(true); // re-POST carries the flag
    expect(ctx.toasts.length).toBe(1);
    expect(modalOf(ctx.window).hidden).toBe(true); // closed on success
  });

  it('does not pull when the shared-store confirmation is declined', async () => {
    const ctx = await bootModal({ applyResponses: [NEEDS_CONFIRM_SHARED], confirmAnswers: [false] });
    await openModal(ctx);
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);
    expect(ctx.pullCalls.length).toBe(1); // no re-POST
    expect(ctx.toasts.length).toBe(0);
    expect(modalOf(ctx.window).hidden).toBe(false); // picker restored
  });

  it('sends force_unsafe_import=true only when the force checkbox is checked', async () => {
    const ctx = await bootModal({
      previewBody: preview({ target_scope: 'user', candidates: [{ runtime: 'claude', content_status: 'new', gate_status: 'requires_unsafe_confirmation', importable: true, landing_group: 0, override_warning: false, reason: null }] }),
      applyResponses: [APPLIED],
    });
    await openModal(ctx);
    // Switch the destination to the user tier (bypassable), check the force valve.
    ctx.window.document.querySelector('#ctx-pull-modal input[name="ctx-pull-tier"][value="user"]').click();
    await flush(ctx.window);
    ctx.window.document.getElementById('ctx-pull-force').click();
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);
    const last = ctx.pullCalls[ctx.pullCalls.length - 1];
    expect(last.body.force_unsafe_import).toBe(true);
    expect(last.url).toContain('target_scope=user');
  });

  it('re-previews with the new tier when the destination changes', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    expect(ctx.previewCalls.length).toBe(1);
    ctx.window.document.querySelector('#ctx-pull-modal input[name="ctx-pull-tier"][value="user"]').click();
    await flush(ctx.window);
    expect(ctx.previewCalls.length).toBe(2);
    expect(ctx.previewCalls[1].url).toContain('target_scope=user');
  });
});

describe('Pull picker — scope pinning', () => {
  it('sends BOTH target_scope and the active project scope_id (non-CWD)', async () => {
    const ctx = await bootModal({
      activeScope: 'proj-dest',
      scopes: [{
        scope_id: 'proj-dest', label: 'Dest', root: '/work/dest', tier: 'project',
        sources: ['known-projects'], missing: false, stale: false, experimental: false,
        enabled: true, sync_eligible: true,
        counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 0 },
      }],
    });
    await openModal(ctx);
    expect(ctx.previewCalls.length).toBe(1);
    // Without the scope_id a Pull from a non-CWD project would target Server CWD
    // and could overwrite a same-named artifact there (Codex Blocker).
    expect(ctx.previewCalls[0].url).toContain('scope_id=proj-dest');
    expect(ctx.previewCalls[0].url).toContain('target_scope=project_shared');
    // Pin it end-to-end: the APPLY POST carries both dimensions too.
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);
    expect(ctx.pullCalls[0].url).toContain('scope_id=proj-dest');
    expect(ctx.pullCalls[0].url).toContain('target_scope=project_shared');
  });
});

describe('Pull picker — consent never leaks across tier / source', () => {
  it('clears a checked overwrite when the destination tier changes', async () => {
    const ctx = await bootModal({
      previewBody: preview({ store_present: true, candidates: [_c('claude', 'differs', 'ok')] }),
    });
    await openModal(ctx);
    const overwrite = ctx.window.document.getElementById('ctx-pull-overwrite');
    expect(ctx.window.document.getElementById('ctx-pull-overwrite-row').hidden).toBe(false);
    overwrite.click();
    expect(overwrite.checked).toBe(true);
    ctx.window.document.querySelector('#ctx-pull-modal input[name="ctx-pull-tier"][value="user"]').click();
    await flush(ctx.window);
    expect(overwrite.checked).toBe(false); // stale overwrite must not survive the tier switch
  });

  it('clears a checked force valve when switching to another warning source', async () => {
    const ctx = await bootModal({
      previewBody: preview({
        ambiguous: true, auto_source: null, distinct_landing_count: 2,
        candidates: [
          _c('claude', 'new', 'requires_unsafe_confirmation', 0),
          _c('codex', 'new', 'requires_unsafe_confirmation', 1),
        ],
      }),
    });
    await openModal(ctx);
    ctx.window.document.querySelector('#ctx-pull-modal input[name="ctx-pull-source"][value="claude"]').click();
    await flush(ctx.window);
    const force = ctx.window.document.getElementById('ctx-pull-force');
    expect(ctx.window.document.getElementById('ctx-pull-force-row').hidden).toBe(false);
    force.click();
    expect(force.checked).toBe(true);
    ctx.window.document.querySelector('#ctx-pull-modal input[name="ctx-pull-source"][value="codex"]').click();
    await flush(ctx.window);
    expect(force.checked).toBe(false); // force is per-selected-bytes — never carried over
  });
});

describe('Pull picker — apply-time refusal', () => {
  it('toasts and re-previews when the Store changed since preview (canonical_exists)', async () => {
    const ctx = await bootModal({
      applyResponses: [{ ok: true, body: { status: 'canonical_exists', reason: 'the Store already has it', reason_code: 'canonical_exists', kind: 'skills', name: NAME, target_scope: 'project_shared', selected_runtime: 'claude', write_outcome: null, duplicate_runtimes: [], canonical_path: null, candidates: [], distinct_landing_count: 0, gate_status: null, gate_hits: null, force_bypassable: false } }],
    });
    await openModal(ctx);
    expect(ctx.previewCalls.length).toBe(1);
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);
    expect(ctx.toasts.length).toBe(1); // the refusal reason is surfaced
    // ...followed by the BROWSER's remediation, naming this modal's control
    // rather than the CLI's --overwrite flag (#1869).
    expect(ctx.toasts[0].msg).toContain('the Store already has it');
    expect(ctx.toasts[0].msg).toContain(ctx.window.t('settings.ctx.pull_hint_canonical_exists'));
    expect(ctx.toasts[0].msg).not.toContain('--overwrite');
    expect(ctx.previewCalls.length).toBe(2); // controls re-computed against reality
    expect(modalOf(ctx.window).hidden).toBe(false); // picker stays open to adjust
  });

  it('localizes the remediation hint (ko)', async () => {
    const ctx = await bootModal({
      applyResponses: [{ ok: true, body: { status: 'source_conflict', reason: 'multiple distinct contents', reason_code: 'source_conflict', kind: 'skills', name: NAME, target_scope: 'project_shared', selected_runtime: null, write_outcome: null, duplicate_runtimes: [], canonical_path: null, candidates: [], distinct_landing_count: 2, gate_status: null, gate_hits: null, force_bypassable: false } }],
    });
    await ctx.window.I18N.setLang('ko');
    await openModal(ctx);
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);
    expect(ctx.toasts[0].msg).toContain('\uc704 \ubaa9\ub85d\uc5d0\uc11c'); // "위 목록에서" — the ko hint
    await ctx.window.I18N.setLang('en');
  });

  it('offers no force control for a hard project_shared block, and degrades to the bare reason for an unknown code', async () => {
    // ``privacy_blocked`` arrives on BOTH tiers; only ``force_bypassable``
    // distinguishes the one where the Force checkbox can actually help.
    const hard = await bootModal({
      applyResponses: [{ ok: true, body: { status: 'gate_blocked', reason: 'Gate A blocked the pull', reason_code: 'privacy_blocked', kind: 'skills', name: NAME, target_scope: 'project_shared', selected_runtime: 'claude', write_outcome: null, duplicate_runtimes: [], canonical_path: null, candidates: [], distinct_landing_count: 0, gate_status: 'blocked', gate_hits: 1, force_bypassable: false } }],
    });
    await openModal(hard);
    hard.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(hard.window);
    expect(hard.toasts[0].msg).toBe('Gate A blocked the pull');

    const future = await bootModal({
      applyResponses: [{ ok: true, body: { status: 'write_failed', reason: 'some future condition', reason_code: 'future_code', kind: 'skills', name: NAME, target_scope: 'project_shared', selected_runtime: 'claude', write_outcome: null, duplicate_runtimes: [], canonical_path: null, candidates: [], distinct_landing_count: 0, gate_status: null, gate_hits: null, force_bypassable: false } }],
    });
    await openModal(future);
    future.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(future.window);
    expect(future.toasts[0].msg).toBe('some future condition');
  });

  it('keeps Apply disabled while the post-refusal re-preview is still pending', async () => {
    const ctx = await bootModal({
      holdPreviewAfter: 1, // the on-open preview resolves; the re-preview is gated
      applyResponses: [{ ok: true, body: { status: 'plan_stale', reason: 'stale', reason_code: 'plan_stale', kind: 'skills', name: NAME, target_scope: 'project_shared', selected_runtime: 'claude', write_outcome: null, duplicate_runtimes: [], canonical_path: null, candidates: [], distinct_landing_count: 0, gate_status: null, gate_hits: null, force_bypassable: false } }],
    });
    await openModal(ctx);
    const applyBtn = ctx.window.document.getElementById('ctx-pull-apply-btn');
    expect(applyBtn.disabled).toBe(false);
    applyBtn.click();
    await flush(ctx.window); // refusal handled; re-preview started but GATED (pending)
    expect(applyBtn.disabled).toBe(true); // must not re-enable with the stale body
    ctx.releasePreview();
    await flush(ctx.window);
    expect(applyBtn.disabled).toBe(false); // the fresh preview re-enables it
  });
});

describe('Pull picker — success refresh targeting', () => {
  it('refreshes the current pane when the pulled tier+project is the one on screen', async () => {
    const ctx = await bootModal(); // default view: project_shared, Server CWD
    await openModal(ctx);
    const calls = [];
    ctx.window.loadCtxList = (k) => calls.push(['list', k]);
    ctx.window.loadCtxDetail = (k, n) => calls.push(['detail', k, n]);
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);
    expect(calls).toContainEqual(['list', 'skills']);
    expect(calls).toContainEqual(['detail', 'skills', NAME]);
  });

  it('skips the refresh when pulling into a tier that is not on screen', async () => {
    const ctx = await bootModal(); // global view stays project_shared
    await openModal(ctx);
    // Retarget the Pull to the user library; the visible pane is still
    // project_shared, so a refresh would reload the wrong (unchanged) tier.
    ctx.window.document.querySelector('#ctx-pull-modal input[name="ctx-pull-tier"][value="user"]').click();
    await flush(ctx.window);
    const calls = [];
    ctx.window.loadCtxList = (k) => calls.push(k);
    ctx.window.loadCtxDetail = (k) => calls.push(k);
    ctx.window.document.getElementById('ctx-pull-apply-btn').click();
    await flush(ctx.window);
    expect(calls.length).toBe(0); // wrong tier on screen → toast only, no reload
    expect(ctx.toasts.length).toBe(1);
  });
});

describe('Pull picker — dismissal', () => {
  it('closes on Cancel without pulling', async () => {
    const ctx = await bootModal();
    await openModal(ctx);
    ctx.window.document.getElementById('ctx-pull-cancel-btn').click();
    await flush(ctx.window);
    expect(modalOf(ctx.window).hidden).toBe(true);
    expect(ctx.pullCalls.length).toBe(0);
  });
});

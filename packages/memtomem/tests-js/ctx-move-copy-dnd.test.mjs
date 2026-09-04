/* #2297 — drag an artifact card onto a project group or a tier chip to start a
 * Move/Copy.
 *
 * The drop never transfers: it opens the existing Move/Copy modal (#1289) with
 * the dropped destination pre-filled, and everything after that — dry-run,
 * confirm gates, collision handling — is the flow ``ctx-move-copy-modal.test.mjs``
 * already pins. What this spec pins is the part that is new:
 *
 *   - which cards are drag sources (active group, transfer kind, real canonical,
 *     Advanced mode) and which are not;
 *   - which drop targets accept (eligibility must match the modal's own
 *     destination option list, so a drop can't pre-fill a destination the
 *     dropdown would have refused);
 *   - what the first dry-run body says, i.e. that the pre-fill actually landed;
 *   - that a stale/invalid pre-fill fails closed instead of silently previewing
 *     the source project;
 *   - the drag lifecycle: hover class, live-region announcements, cleanup.
 *
 * Run from packages/memtomem/tests-js.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 40) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

const NAME = 'demo-skill';
// Server CWD carries a COMPUTED scope_id in production (``compute_scope_id``),
// not ''. The transfer route matches a destination by scope_id equality, so the
// tests must drop the same shape the server emits.
const CWD_ID = 'cwd-9f1c2a';

const SCOPES = [
  {
    scope_id: CWD_ID, label: 'Server CWD', root: '/srv', tier: 'project',
    sources: ['server-cwd'], missing: false, stale: false, experimental: false,
    enabled: true, sync_eligible: true,
    counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 1 },
  },
  {
    scope_id: 'proj-dest', label: 'Dest Project', root: '/work/dest', tier: 'project',
    sources: ['known-projects'], missing: false, stale: false, experimental: false,
    enabled: true, sync_eligible: true,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
  {
    // Paused registration: the route 409s a paused destination.
    scope_id: 'proj-paused', label: 'Paused Project', root: '/work/paused', tier: 'project',
    sources: ['known-projects'], missing: false, stale: false, experimental: false,
    enabled: false, sync_eligible: false,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
  {
    // Root exists but has no ``.memtomem/`` store → the cross-project transfer
    // gate 409s ``no_memtomem_store`` every time.
    scope_id: 'proj-stale', label: 'Stale Project', root: '/work/stale', tier: 'project',
    sources: ['known-projects'], missing: false, stale: true, experimental: false,
    enabled: true, sync_eligible: true,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
  {
    scope_id: 'proj-missing', label: 'Missing Project', root: '/work/gone', tier: 'project',
    sources: ['known-projects'], missing: true, stale: false, experimental: false,
    enabled: true, sync_eligible: true,
    counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
];

const DETAIL = {
  content: 'name: demo\n', target_scope: 'project_shared', layout: 'flat',
  files: [], mtime_ns: '1700000000000000000', fields: {},
};

const PLAN = {
  status: 'plan', transferred: false, kind: 'skills', name: NAME, dst_name: NAME,
  mode: 'copy', from_scope: 'project_shared', to_scope: 'project_local',
  src_project_scope_id: CWD_ID, dst_project_scope_id: CWD_ID,
  src_path: '/srv/.memtomem/skills/demo-skill.md',
  dst_path: '/srv/.memtomem/skills-local/demo-skill.md',
  needs_sync: false, sync_command: null, notes: [],
};

// One canonical skill (drag-eligible) plus one runtime-only row (no canonical
// file, so nothing to transfer).
const SKILL_ITEMS = [
  { name: NAME, canonical_path: '/srv/.memtomem/skills/demo-skill.md', runtimes: [] },
  { name: 'runtime-only-skill', canonical_path: '', runtimes: [{ runtime: 'claude', status: 'missing canonical' }] },
];

async function boot({ type = 'skills', items = SKILL_ITEMS } = {}) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;

  window.showConfirm = async () => false;
  const toasts = [];
  window.showToast = (msg, sev) => toasts.push({ msg, sev });
  window.ensureCsrfToken = async () => 'test-token';

  const transferCalls = [];
  const listCalls = [];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith('/transfer')) {
      transferCalls.push({
        url,
        isDry: url.includes('dry_run'),
        body: opts && opts.body ? JSON.parse(opts.body) : null,
      });
      return { ok: true, status: 200, json: async () => PLAN };
    }
    if (path.endsWith(`/api/context/${type}/${NAME}`)) {
      return { ok: true, status: 200, json: async () => DETAIL };
    }
    if (path.endsWith(`/api/context/${type}`)) {
      listCalls.push(url);
      return { ok: true, status: 200, json: async () => ({ [type]: items, scanned_dirs: [] }) };
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
  // Advanced mode must be set BEFORE the first render: ``draggable`` is decided
  // when the card HTML is built, not at drag time.
  window._ctxSetSimpleMode(false);
  await window.loadCtxList(type);
  await flush(window);
  // Reveal the non-active project groups — the cross-project drop targets.
  const showAll = window.document.getElementById(`ctx-${type}-show-all`);
  if (showAll) { showAll.checked = true; showAll.dispatchEvent(new window.Event('change', { bubbles: true })); }
  await flush(window);
  return { window, transferCalls, listCalls, toasts, type };
}

// --- synthetic drag events (JSDOM has neither DragEvent nor DataTransfer) ----
function dtStub() {
  return { setData() {}, getData: () => '', effectAllowed: 'none', dropEffect: 'none' };
}
function fire(window, el, kind, dt) {
  const ev = new window.Event(kind, { bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'dataTransfer', { value: dt === undefined ? dtStub() : dt });
  el.dispatchEvent(ev);
  return ev;
}

const cardOf = (ctx, name = NAME) =>
  ctx.window.document.querySelector(`#ctx-${ctx.type}-list .ctx-card[data-name="${name}"]`);
const summaryOf = (ctx, scopeId) =>
  ctx.window.document.querySelector(
    `#ctx-${ctx.type}-list details[data-scope-id="${scopeId}"] > summary`);
const chipOf = (ctx, tier) =>
  ctx.window.document.querySelector(`#ctx-control-bar .ctx-tier-filter button[data-scope="${tier}"]`);
const modalOf = (ctx) => ctx.window.document.getElementById('ctx-move-copy-modal');
const announcerOf = (ctx) => ctx.window.document.getElementById('ctx-drag-announce');
const projValue = (ctx) => ctx.window.document.getElementById('ctx-mc-project').value;
const checkedTier = (ctx) =>
  modalOf(ctx).querySelector('input[name="ctx-mc-tier"]:checked').value;
const checkedMode = (ctx) =>
  modalOf(ctx).querySelector('input[name="ctx-mc-mode"]:checked').value;

async function dragTo(ctx, target, { card = cardOf(ctx), hover = true } = {}) {
  const dt = dtStub();
  fire(ctx.window, card, 'dragstart', dt);
  let over = null;
  if (hover) {
    fire(ctx.window, target, 'dragenter', dt);
    over = fire(ctx.window, target, 'dragover', dt);
  }
  fire(ctx.window, target, 'drop', dt);
  await flush(ctx.window);
  return { over, dt };
}

describe('#2297 drag source', () => {
  it('marks only transferable canonical cards in the active group draggable', async () => {
    const ctx = await boot();
    expect(cardOf(ctx).getAttribute('draggable')).toBe('true');
    // Runtime-only: no canonical file, so there is nothing to transfer (its
    // detail pane offers no Move/Copy either) and a dry-run would 404.
    expect(cardOf(ctx, 'runtime-only-skill').getAttribute('draggable')).toBe(null);
    // Another project's group renders readonly cards. Asserting they are absent
    // would only be re-testing that a collapsed group has not been fetched, so
    // render that branch directly and check the attribute itself.
    const div = ctx.window.document.createElement('div');
    div.innerHTML = ctx.window._ctxRenderItemsHtml(
      SKILL_ITEMS, 'skills', '/work/dest', [], { clickable: false });
    const readonly = div.querySelector(`.ctx-card[data-name="${NAME}"]`);
    expect(readonly.classList.contains('ctx-card--readonly')).toBe(true);
    expect(readonly.getAttribute('draggable')).toBe(null);
  });

  it('gates the draggable attribute on Simple mode and on the kind', async () => {
    // The renderer is the single place the attribute is decided, so drive it
    // directly: the gates are render-time, not drag-time.
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    await window.I18N.setLang('en');
    const render = (type) => {
      const div = window.document.createElement('div');
      div.innerHTML = window._ctxRenderItemsHtml(SKILL_ITEMS, type, '/srv', [], { clickable: true });
      return div;
    };

    window._ctxSetSimpleMode(false);
    expect(render('skills').querySelector(`.ctx-card[data-name="${NAME}"]`)
      .getAttribute('draggable')).toBe('true');
    // Not a transfer kind — there is no Move/Copy to accelerate.
    expect(render('hooks').querySelector(`.ctx-card[data-name="${NAME}"]`)
      .getAttribute('draggable')).toBe(null);
    // Simple mode hides the whole control bar; it must not sprout a drag gesture.
    window._ctxSetSimpleMode(true);
    expect(render('skills').querySelector(`.ctx-card[data-name="${NAME}"]`)
      .getAttribute('draggable')).toBe(null);
    window._ctxSetSimpleMode(false);
  });
});

describe('#2297 drop on a project group header', () => {
  it('opens Move/Copy pre-filled with the dropped project and previews it', async () => {
    const ctx = await boot();
    await dragTo(ctx, summaryOf(ctx, 'proj-dest'));

    expect(modalOf(ctx).hidden).toBe(false);
    expect(checkedMode(ctx)).toBe('copy');          // copy is the safe default
    expect(projValue(ctx)).toBe('proj-dest');
    expect(checkedTier(ctx)).toBe('project_shared'); // source tier carried over
    const dry = ctx.transferCalls.filter((c) => c.isDry);
    expect(dry.length).toBeGreaterThanOrEqual(1);
    // The FIRST dry-run already targets the drop — the pre-fill is not a
    // second request that races the default.
    expect(dry[0].body.to_project_scope_id).toBe('proj-dest');
    expect(dry[0].body.to_target_scope).toBe('project_shared');
    expect(dry[0].body.mode).toBe('copy');
    // Filtering to the dry-run leg above would hide a real transfer rather than
    // catch it, and "a drop never transfers" is the whole safety contract: every
    // call the drop provoked must be a preview.
    expect(ctx.transferCalls.every((c) => c.isDry)).toBe(true);
  });

  it('refuses paused, stale, and missing destinations, and refuses no-drop cursor', async () => {
    for (const scopeId of ['proj-paused', 'proj-stale', 'proj-missing']) {
      const ctx = await boot();
      const target = summaryOf(ctx, scopeId);
      expect(target, `${scopeId} group must render`).toBeTruthy();
      const { over } = await dragTo(ctx, target);
      expect(modalOf(ctx).hidden, `${scopeId} must not open the modal`).toBe(true);
      expect(ctx.transferCalls.length).toBe(0);
      // No preventDefault ⇒ the browser keeps the native no-drop cursor.
      expect(over.defaultPrevented).toBe(false);
      expect(target.classList.contains('ctx-drop-target--over')).toBe(false);
    }
  });

  it('keeps those same scopes out of the modal destination list', async () => {
    const ctx = await boot();
    await dragTo(ctx, summaryOf(ctx, 'proj-dest'));
    const values = Array.from(
      ctx.window.document.querySelectorAll('#ctx-mc-project option')).map((o) => o.value);
    // The drop targets and the dropdown gate on ONE predicate, so they agree.
    expect(values).toContain('proj-dest');
    expect(values).toContain(CWD_ID);          // source stays (same-project promote)
    expect(values).not.toContain('proj-stale');
    expect(values).not.toContain('proj-missing');
    expect(values).not.toContain('proj-paused');
  });

  it('refuses a drop on the source project group', async () => {
    const ctx = await boot();
    const { over } = await dragTo(ctx, summaryOf(ctx, CWD_ID));
    expect(modalOf(ctx).hidden).toBe(true);
    expect(ctx.transferCalls.length).toBe(0);
    expect(over.defaultPrevented).toBe(false);
  });

  it('never expands a collapsed group on hover, and issues no request for it', async () => {
    const ctx = await boot();
    const group = ctx.window.document.querySelector(
      '#ctx-skills-list details[data-scope-id="proj-dest"]');
    const items = group.querySelector('.ctx-scope-items');
    expect(group.open).toBe(false);
    // Requests, not just ``open``: expanding is what triggers the group's lazy
    // item fetch, so a hover that quietly fetched — or a future handler that
    // fetched without expanding — must be caught too.
    const before = ctx.listCalls.length;
    const dt = dtStub();
    fire(ctx.window, cardOf(ctx), 'dragstart', dt);
    fire(ctx.window, summaryOf(ctx, 'proj-dest'), 'dragenter', dt);
    fire(ctx.window, summaryOf(ctx, 'proj-dest'), 'dragover', dt);
    await flush(ctx.window);
    expect(group.open).toBe(false);
    expect(items.dataset.loaded).toBe('false');
    expect(ctx.listCalls.length).toBe(before);
  });

  it('sends a user-tier source to the destination project shared store', async () => {
    // A user-tier destination cannot carry a project (hard 400 at the route),
    // so a user-tier source dropped on a project must land in that project's
    // shared store instead of keeping its own tier.
    const ctx = await boot();
    chipOf(ctx, 'user').click();
    await flush(ctx.window);
    await dragTo(ctx, summaryOf(ctx, 'proj-dest'));

    expect(modalOf(ctx).hidden).toBe(false);
    expect(checkedTier(ctx)).toBe('project_shared');
    const dry = ctx.transferCalls.filter((c) => c.isDry);
    expect(dry[0].body.to_target_scope).toBe('project_shared');
    expect(dry[0].body.to_project_scope_id).toBe('proj-dest');
  });
});

describe('#2297 drop on a tier chip', () => {
  it('pre-fills the dropped store and leaves the project alone', async () => {
    // The ``user`` chip, deliberately: ``project_local`` is already the modal's
    // default for a project_shared source, so a test using it would pass even
    // if the chip pre-fill were ignored entirely.
    const ctx = await boot();
    await dragTo(ctx, chipOf(ctx, 'user'));

    expect(modalOf(ctx).hidden).toBe(false);
    expect(checkedTier(ctx)).toBe('user');
    const dry = ctx.transferCalls.filter((c) => c.isDry);
    expect(dry[0].body.to_target_scope).toBe('user');
    // The user tier is global — the body must not name a project.
    expect(dry[0].body.to_project_scope_id).toBe(null);
  });

  it('carries a project_local source onto the project_shared chip', async () => {
    const ctx = await boot();
    chipOf(ctx, 'project_local').click();
    await flush(ctx.window);
    await dragTo(ctx, chipOf(ctx, 'project_shared'));

    expect(checkedTier(ctx)).toBe('project_shared');
    const dry = ctx.transferCalls.filter((c) => c.isDry);
    expect(dry[0].body.to_target_scope).toBe('project_shared');
    expect(dry[0].body.from_scope).toBe('project_local');
  });

  it('refuses the pressed chip (same store) and a disabled chip', async () => {
    const ctx = await boot();
    const pressed = chipOf(ctx, 'project_shared');   // the live tier filter
    const { over } = await dragTo(ctx, pressed);
    expect(modalOf(ctx).hidden).toBe(true);
    expect(ctx.transferCalls.length).toBe(0);
    expect(over.defaultPrevented).toBe(false);

    // A Sync All run disables the chips; a disabled control is not a target.
    const other = chipOf(ctx, 'user');
    other.disabled = true;
    const { over: over2 } = await dragTo(ctx, other);
    expect(modalOf(ctx).hidden).toBe(true);
    expect(over2.defaultPrevented).toBe(false);
  });
});

describe('#2297 drag lifecycle and refusals', () => {
  it('refuses a drop with no drag in flight', async () => {
    const ctx = await boot();
    fire(ctx.window, summaryOf(ctx, 'proj-dest'), 'drop');
    await flush(ctx.window);
    expect(modalOf(ctx).hidden).toBe(true);
    expect(ctx.transferCalls.length).toBe(0);
  });

  it('refuses a drop after a tier flip rebuilt the list mid-drag', async () => {
    // The modal derives the SOURCE from the live tier, so a tier flip between
    // dragstart and drop would otherwise transfer from a store the user never
    // picked up. The flip repaints the list, and the repaint cancels the drag.
    const ctx = await boot();
    const dt = dtStub();
    fire(ctx.window, cardOf(ctx), 'dragstart', dt);
    chipOf(ctx, 'project_local').click();
    await flush(ctx.window);
    fire(ctx.window, summaryOf(ctx, 'proj-dest'), 'drop', dt);
    await flush(ctx.window);
    expect(modalOf(ctx).hidden).toBe(true);
    expect(ctx.transferCalls.length).toBe(0);
  });

  it('refuses a drop that lands while a list reload is still in flight', async () => {
    // A reload cancels the drag when it STARTS, not when its fetch settles.
    // In the gap the old card and the old group headers are still on screen and
    // still connected, so without an up-front cancel a drop would resolve
    // against a list that is already being replaced.
    const ctx = await boot();
    const dt = dtStub();
    fire(ctx.window, cardOf(ctx), 'dragstart', dt);
    expect(announcerOf(ctx).textContent.length).toBeGreaterThan(0);
    const target = summaryOf(ctx, 'proj-dest');
    ctx.window.loadCtxList('skills');          // deliberately not awaited
    // The reload cancels the drag up front, so the live region stops narrating
    // a gesture whose surface is already gone — it does not wait for the fetch.
    expect(announcerOf(ctx).textContent).toBe('');
    fire(ctx.window, target, 'drop', dt);
    await flush(ctx.window);
    expect(modalOf(ctx).hidden).toBe(true);
    expect(ctx.transferCalls.length).toBe(0);
  });

  it('refuses a drop from a card that left the DOM without a cleanup', async () => {
    // The backstop for a detach that did not run through a repaint: the drag
    // session is stale, and the drop must not resolve against it.
    const ctx = await boot();
    const card = cardOf(ctx);
    const dt = dtStub();
    fire(ctx.window, card, 'dragstart', dt);
    card.remove();
    const { over } = { over: fire(ctx.window, summaryOf(ctx, 'proj-dest'), 'dragover', dt) };
    fire(ctx.window, summaryOf(ctx, 'proj-dest'), 'drop', dt);
    await flush(ctx.window);
    expect(over.defaultPrevented).toBe(false);
    expect(modalOf(ctx).hidden).toBe(true);
    expect(ctx.transferCalls.length).toBe(0);
  });

  it('fails closed when a requested destination is no longer offered', async () => {
    const ctx = await boot();
    const opened = ctx.window._ctxOpenMoveCopyModal('skills', NAME, { toScopeId: 'vanished' });
    await flush(ctx.window);
    expect(opened).toBe(false);
    expect(modalOf(ctx).hidden).toBe(true);
    // Crucially: no dry-run against the DEFAULT (the source project) either —
    // a silent fallback would look like the drop worked.
    expect(ctx.transferCalls.length).toBe(0);
  });

  it('fails closed on an unknown tier and on a tier request for mcp-servers', async () => {
    const ctx = await boot();
    expect(ctx.window._ctxOpenMoveCopyModal('skills', NAME, { toTier: 'nope' })).toBe(false);
    expect(ctx.window._ctxOpenMoveCopyModal('mcp-servers', NAME, { toTier: 'user' })).toBe(false);
    await flush(ctx.window);
    expect(modalOf(ctx).hidden).toBe(true);
    expect(ctx.transferCalls.length).toBe(0);
  });

  it('marks the hovered target, then clears every trace on dragend', async () => {
    const ctx = await boot();
    const card = cardOf(ctx);
    const target = summaryOf(ctx, 'proj-dest');
    const dt = dtStub();
    fire(ctx.window, card, 'dragstart', dt);
    expect(card.classList.contains('ctx-card--dragging')).toBe(true);
    const over = fire(ctx.window, target, 'dragenter', dt);
    fire(ctx.window, target, 'dragover', dt);
    expect(over).toBeTruthy();
    expect(target.classList.contains('ctx-drop-target--over')).toBe(true);

    fire(ctx.window, card, 'dragend', dt);
    expect(card.classList.contains('ctx-card--dragging')).toBe(false);
    expect(target.classList.contains('ctx-drop-target--over')).toBe(false);
    expect(announcerOf(ctx).textContent).toBe('');
  });

  it('holds the hover state while the pointer crosses the header children', async () => {
    // The reason the counter exists: a ``<summary>`` has element children, and
    // moving from one to the next fires ``dragleave`` on the summary before the
    // matching ``dragenter``. Dispatching from the children (the events bubble)
    // reproduces that ordering; a naive toggle would strobe off mid-header.
    const ctx = await boot();
    const target = summaryOf(ctx, 'proj-dest');
    const kids = target.querySelectorAll('span');
    expect(kids.length).toBeGreaterThanOrEqual(2);
    const dt = dtStub();
    fire(ctx.window, cardOf(ctx), 'dragstart', dt);

    fire(ctx.window, kids[0], 'dragenter', dt);
    expect(target.classList.contains('ctx-drop-target--over')).toBe(true);
    // Crossing into the sibling: enter lands before the outgoing leave.
    fire(ctx.window, kids[1], 'dragenter', dt);
    fire(ctx.window, kids[0], 'dragleave', dt);
    expect(target.classList.contains('ctx-drop-target--over')).toBe(true);
    // Leaving the header for good balances the count.
    fire(ctx.window, kids[1], 'dragleave', dt);
    expect(target.classList.contains('ctx-drop-target--over')).toBe(false);
  });

  it('does not inherit a stale hover count from an interrupted drag', async () => {
    // ``dragend`` while still hovering leaves the counter unbalanced, and the
    // counter is closure-local so global cleanup cannot reset it. Keying it by
    // drag session is what stops the next drag from inheriting the count.
    const ctx = await boot();
    const card = cardOf(ctx);
    const target = summaryOf(ctx, 'proj-dest');
    const kid = target.querySelector('span');
    const dt = dtStub();
    fire(ctx.window, card, 'dragstart', dt);
    fire(ctx.window, kid, 'dragenter', dt);
    fire(ctx.window, kid, 'dragenter', dt);         // count now 2
    fire(ctx.window, card, 'dragend', dt);          // interrupted mid-hover

    fire(ctx.window, card, 'dragstart', dt);
    fire(ctx.window, kid, 'dragenter', dt);
    expect(target.classList.contains('ctx-drop-target--over')).toBe(true);
    fire(ctx.window, kid, 'dragleave', dt);
    expect(target.classList.contains('ctx-drop-target--over')).toBe(false);
  });

  it('announces the drag, each eligible target, and each refusal', async () => {
    const ctx = await boot();
    const { window } = ctx;
    const card = cardOf(ctx);
    const dt = dtStub();

    fire(window, card, 'dragstart', dt);
    expect(announcerOf(ctx).textContent)
      .toBe(window.I18N.t('settings.ctx.dnd_start', { name: NAME }));

    fire(window, summaryOf(ctx, 'proj-dest'), 'dragenter', dt);
    expect(announcerOf(ctx).textContent).toBe(
      window.I18N.t('settings.ctx.dnd_drop_project', { name: NAME, project: 'Dest Project' }));

    fire(window, chipOf(ctx, 'user'), 'dragenter', dt);
    expect(announcerOf(ctx).textContent).toBe(
      window.I18N.t('settings.ctx.dnd_drop_tier', {
        name: NAME, tier: window.I18N.t('settings.ctx.tier_option_user'),
      }));

    fire(window, summaryOf(ctx, 'proj-stale'), 'dragenter', dt);
    expect(announcerOf(ctx).textContent).toBe(
      window.I18N.t('settings.ctx.dnd_refused', { name: NAME, target: 'Stale Project' }));
  });
});

describe('#2297 mcp-servers', () => {
  it('opens the constrained modal on a project drop and refuses a tier chip', async () => {
    const ctx = await boot({
      type: 'mcp-servers',
      items: [{ name: NAME, canonical_path: '/srv/.mcp.json', runtimes: [] }],
    });
    await dragTo(ctx, summaryOf(ctx, 'proj-dest'));
    expect(modalOf(ctx).hidden).toBe(false);
    expect(projValue(ctx)).toBe('proj-dest');
    // Constrained variant: mode/tier/rename hidden, copy-only.
    expect(ctx.window.document.getElementById('ctx-mc-mode-field').hidden).toBe(true);
    expect(ctx.window.document.getElementById('ctx-mc-tier-field').hidden).toBe(true);
    const dry = ctx.transferCalls.filter((c) => c.isDry);
    expect(dry[0].body.mode).toBe('copy');
    expect(dry[0].body.to_project_scope_id).toBe('proj-dest');

    // The announcements name the only operation mcp supports — a copy to
    // another project — rather than the generic "or a store" opening, which
    // would point a screen-reader user at chips that always refuse.
    const ctxA = await boot({
      type: 'mcp-servers',
      items: [{ name: NAME, canonical_path: '/srv/.mcp.json', runtimes: [] }],
    });
    const dtA = dtStub();
    fire(ctxA.window, cardOf(ctxA), 'dragstart', dtA);
    expect(announcerOf(ctxA).textContent)
      .toBe(ctxA.window.I18N.t('settings.ctx.dnd_start_mcp', { name: NAME }));
    fire(ctxA.window, summaryOf(ctxA, 'proj-dest'), 'dragenter', dtA);
    expect(announcerOf(ctxA).textContent).toBe(
      ctxA.window.I18N.t('settings.ctx.dnd_drop_project_mcp', {
        name: NAME, project: 'Dest Project',
      }));

    // mcp-servers are single-tier: a tier chip is never a destination for them.
    const ctx2 = await boot({
      type: 'mcp-servers',
      items: [{ name: NAME, canonical_path: '/srv/.mcp.json', runtimes: [] }],
    });
    const { over } = await dragTo(ctx2, chipOf(ctx2, 'project_local'));
    expect(modalOf(ctx2).hidden).toBe(true);
    expect(ctx2.transferCalls.length).toBe(0);
    expect(over.defaultPrevented).toBe(false);
  });
});

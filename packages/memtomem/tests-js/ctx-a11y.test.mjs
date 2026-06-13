/* B-2 #1285 — Context Gateway a11y batch.
 *
 * Pins the screen-reader-facing behavior added by the a11y batch:
 *   - error toasts are assertive (role=alert), other toasts polite
 *     (role=status), and #toast-container is NOT itself a live region
 *     (per-toast roles own the urgency — a polite container wrapping an
 *     assertive toast double-announces);
 *   - the shared loading spinner carries an sr-only text alternative;
 *   - showConfirm's optional cancelText is driven on EVERY call so a custom
 *     label can't leak into the next default confirm (the cancel button is a
 *     single reused element);
 *   - detail-pane Edit/Delete buttons carry localized data-i18n-title tooltips;
 *   - the inline 409 conflict banner marks only its short heading role=alert,
 *     never the scrolling diff body;
 *   - portal/main-list load failures announce via a role=alert empty-state,
 *     and the portal thrown-error catch routes through the same helper.
 *
 * Run from packages/memtomem/tests-js (a repo-root run collects stale worktree
 * copies of the static modules).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 40) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

const NAME = 'demo-skill';
const DETAIL = {
  name: NAME, content: 'name: demo\n', target_scope: 'project_shared',
  layout: 'flat', files: [], mtime_ns: '1700000000000000000', fields: {},
};
const SCOPES = [
  {
    scope_id: '', label: 'Server CWD', root: '/srv', tier: 'project',
    sources: ['server-cwd'], missing: false, stale: false, experimental: false,
    enabled: true, sync_eligible: true,
    counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
];

describe('showToast — per-toast live semantics (#1285)', () => {
  let window;

  beforeEach(async () => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js'] }));
    await window.I18N.init();
  });

  it('error toasts are assertive (role=alert)', () => {
    window.showToast('boom', 'error');
    const toast = window.document.querySelector('#toast-container .toast-error');
    expect(toast).toBeTruthy();
    expect(toast.getAttribute('role')).toBe('alert');
  });

  it('non-error toasts are polite (role=status)', () => {
    window.showToast('done', 'success');
    const toast = window.document.querySelector('#toast-container .toast-success');
    expect(toast).toBeTruthy();
    expect(toast.getAttribute('role')).toBe('status');
  });

  it('#toast-container is NOT itself a live region (no double-announce)', () => {
    // Symmetric pin: the per-toast roles only avoid double-announce if the
    // container is inert. Reinstating aria-live on the container flips this.
    const container = window.document.getElementById('toast-container');
    expect(container.hasAttribute('aria-live')).toBe(false);
    expect(container.hasAttribute('aria-atomic')).toBe(false);
  });
});

describe('panelLoading — sr-only text alternative (#1285)', () => {
  it('renders an sr-only span carrying common.loading, with no nested live region', async () => {
    const { window } = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    await window.I18N.init();
    const div = window.document.createElement('div');
    window.panelLoading(div);
    const sr = div.querySelector('.sr-only');
    expect(sr).toBeTruthy();
    expect(sr.textContent).toBe(window.t('common.loading'));
    // No aria-live on the span: parents like #results-list are already live
    // regions, so a nested one would double-announce.
    expect(sr.hasAttribute('aria-live')).toBe(false);
    // The decorative spinner is still present.
    expect(div.querySelector('.spinner-panel')).toBeTruthy();
  });
});

describe('showConfirm — cancelText driven every call, no leak (#1285)', () => {
  let window;

  beforeEach(async () => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js'] }));
    await window.I18N.init();
  });

  it('applies a custom cancelText and drops the static data-i18n', async () => {
    const p = window.showConfirm({ title: 'Remove', cancelText: 'Keep' });
    const cancelBtn = window.document.getElementById('confirm-cancel-btn');
    expect(cancelBtn.textContent).toBe('Keep');
    expect(cancelBtn.hasAttribute('data-i18n')).toBe(false);
    cancelBtn.click(); // resolve the dialog
    await p;
  });

  it('a later default confirm shows the default label, not the leaked custom one', async () => {
    const cancelBtn = window.document.getElementById('confirm-cancel-btn');
    // First: custom cancel label.
    const p1 = window.showConfirm({ title: 'Remove', cancelText: 'Keep' });
    expect(cancelBtn.textContent).toBe('Keep');
    cancelBtn.click();
    await p1;
    // Then: a default confirm must NOT inherit "Keep".
    const p2 = window.showConfirm({ title: 'Sync', confirmText: 'Sync', danger: false });
    expect(cancelBtn.textContent).toBe(window.t('modal.cancel_btn'));
    expect(cancelBtn.textContent).not.toBe('Keep');
    expect(cancelBtn.hasAttribute('data-i18n')).toBe(false);
    cancelBtn.click();
    await p2;
  });
});

describe('detail-pane Edit/Delete tooltips (#1285)', () => {
  async function bootDetail() {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    const upstream = window.fetch;
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      const path = url.split('?')[0];
      if (path.endsWith('/diff')) return { ok: true, status: 200, json: async () => ({ runtimes: [], canonical_content: '# demo\n' }) };
      if (path.endsWith(`/api/context/skills/${NAME}`)) return { ok: true, status: 200, json: async () => DETAIL };
      if (path.endsWith('/api/context/skills')) return { ok: true, status: 200, json: async () => ({ skills: [{ name: NAME, runtimes: [] }] }) };
      if (path.includes('/api/context/projects')) return { ok: true, status: 200, json: async () => ({ scopes: SCOPES, target_scope: 'project_shared' }) };
      return upstream(input, opts);
    };
    await window.I18N.init();
    if (!window.CSS) window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
    await window.loadCtxList('skills');
    await flush(window);
    await window.loadCtxDetail('skills', NAME);
    await flush(window);
    return window;
  }

  it('Edit and Delete buttons carry localized data-i18n-title + title', async () => {
    const window = await bootDetail();
    const editBtn = window.document.querySelector('.ctx-detail-edit-btn');
    const delBtn = window.document.querySelector('.ctx-detail-delete-btn');
    expect(editBtn).toBeTruthy();
    expect(delBtn).toBeTruthy();
    expect(editBtn.getAttribute('data-i18n-title')).toBe('settings.ctx.edit_tooltip');
    expect(editBtn.getAttribute('title')).toBe(window.t('settings.ctx.edit_tooltip'));
    expect(delBtn.getAttribute('data-i18n-title')).toBe('settings.ctx.delete_tooltip');
    expect(delBtn.getAttribute('title')).toBe(window.t('settings.ctx.delete_tooltip'));
    // The title must be a real localized string, not the raw key fallback.
    expect(editBtn.getAttribute('title')).not.toBe('settings.ctx.edit_tooltip');
  });
});

describe('conflict banner — role=alert on heading only, not the diff (#1285)', () => {
  it('marks the short heading assertive and leaves the diff body non-live', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    await window.I18N.init();
    const detailEl = window.document.createElement('div');
    const banner = window.document.createElement('div');
    banner.className = 'ctx-conflict-banner';
    banner.hidden = true;
    detailEl.appendChild(banner);

    window._ctxRenderConflictBanner(detailEl, 'my local edits\n', 'whats on disk\n');

    expect(banner.hidden).toBe(false);
    const alertEl = banner.querySelector('[role="alert"]');
    expect(alertEl).toBeTruthy();
    // The alert is the heading, not the diff.
    expect(alertEl.classList.contains('text-muted')).toBe(true);
    expect(alertEl.classList.contains('diff-view')).toBe(false);
    // The scrolling diff body must NOT be a live region.
    const diff = banner.querySelector('.diff-view');
    expect(diff).toBeTruthy();
    expect(diff.hasAttribute('role')).toBe(false);
    // Exactly one alert region inside the banner.
    expect(banner.querySelectorAll('[role="alert"]').length).toBe(1);
  });
});

describe('_ctxScopesLoadError — announced retryable error (#1285)', () => {
  it('tags the rendered empty-state role=alert and offers a Retry button', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    await window.I18N.init();
    const listEl = window.document.createElement('div');
    let retried = 0;
    window._ctxScopesLoadError(listEl, 'load failed', 'boom detail', () => { retried += 1; });
    const card = listEl.querySelector('.empty-state');
    expect(card).toBeTruthy();
    expect(card.getAttribute('role')).toBe('alert');
    const retry = listEl.querySelector('.ctx-scopes-retry');
    expect(retry).toBeTruthy();
    retry.click();
    expect(retried).toBe(1);
  });
});

describe('portal thrown-error catch routes through the announced helper (#1285)', () => {
  it('renders a role=alert empty-state with Retry when loadCtxProjects throws', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'],
    });
    const { window } = dom;
    await window.I18N.init();
    // Force a thrown failure inside loadCtxProjects' try (network fallback is
    // swallowed by _ctxFetchProjectsData, so we throw at the commit seam — a
    // free reference that resolves through window for a function declaration).
    window._ctxFetchProjectsData = async () => { throw new Error('store unavailable'); };

    await window.loadCtxProjects();
    await flush(window);

    const listEl = window.document.getElementById('ctx-projects-list');
    const card = listEl.querySelector('.empty-state');
    expect(card).toBeTruthy();
    // Pre-fix this path rendered emptyState() directly with no role — the
    // mutation that bites is reverting the catch to the bare emptyState call.
    expect(card.getAttribute('role')).toBe('alert');
    expect(listEl.querySelector('.ctx-scopes-retry')).toBeTruthy();
  });
});

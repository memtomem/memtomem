/* Per-project sync enrollment (#1203 frontend) — portal Enroll + Pause/Resume
 * and the sync-eligibility helpers shared with the portal card Sync gate.
 *
 * Drives the production ``context-portal.js`` + ``context-gateway.js`` inside
 * the index.html DOM via the jsdom harness, mirroring ``ctx-portal-board``.
 * The portal card Sync-button gating (eligibility / write-block / server-CWD)
 * is covered by the Playwright suite
 * ``tests/web/test_context_portal_card_sync.py``; here we unit-test the shared
 * eligibility helpers and the portal mutation flows.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// One scope of each enrollment shape the backend can emit (#1203):
//   server-cwd  — implicitly sync-eligible, never managed
//   p-on        — enrolled + enabled  → eligible, Pause offered
//   p-off       — enrolled + paused   → ineligible, Resume offered + badge
//   p-scan      — scan-only auto-display (NOT enrolled) → Enroll offered, no
//                 rename/remove (no known_projects entry to PATCH/DELETE)
const SCOPES = [
  {
    scope_id: '', project_scope_id: '', label: 'Server CWD', root: '/srv',
    tier: 'project', sources: ['server-cwd'], missing: false, stale: false,
    experimental: false, enabled: true, sync_eligible: true, counts: null,
  },
  {
    scope_id: 'p-on', project_scope_id: 'p-on', label: 'Enabled', root: '/work/on',
    tier: 'project', sources: ['known-projects'], missing: false, stale: false,
    experimental: false, enabled: true, sync_eligible: true, counts: null,
  },
  {
    scope_id: 'p-off', project_scope_id: 'p-off', label: 'Paused', root: '/work/off',
    tier: 'project', sources: ['known-projects'], missing: false, stale: false,
    experimental: false, enabled: false, sync_eligible: false, counts: null,
  },
  {
    scope_id: 'p-scan', project_scope_id: 'p-scan', label: 'Scanned', root: '/work/scan',
    tier: 'project', sources: ['claude-projects'], missing: false, stale: false,
    experimental: false, enabled: true, sync_eligible: false, counts: null,
  },
  {
    // Paused AND stale — exercises the deliberate orthogonality (paused badge is
    // its own check, not folded into the missing/stale if/else-if chain).
    scope_id: 'p-paused-stale', project_scope_id: 'p-paused-stale', label: 'PausedStale',
    root: '/work/ps', tier: 'project', sources: ['known-projects'], missing: false,
    stale: true, experimental: false, enabled: false, sync_eligible: false, counts: null,
  },
  {
    // A known project PAUSED then reopened as the running dir: the backend
    // coalesces known-projects + server-cwd onto one scope with enabled:false
    // but sync_eligible:true (the running dir can't be paused). It must stay
    // non-managed AND non-enrollable (server-cwd guard in both predicates) AND
    // show NO paused badge — the badge would be unresumable + contradict
    // eligibility.
    scope_id: 'p-cwd-enrolled', project_scope_id: 'p-cwd-enrolled', label: 'CwdEnrolled',
    root: '/work/cwd-enrolled', tier: 'project', sources: ['known-projects', 'server-cwd'],
    missing: false, stale: false, experimental: false, enabled: false, sync_eligible: true,
    counts: null,
  },
];

function stubFetch(window, calls) {
  const upstream = window.fetch;
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (calls && (init || url.includes('known-projects'))) calls.push({ url, init: init || {} });
    if (url.startsWith('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: SCOPES, target_scope: 'project_shared' }) };
    }
    // Both POST /known-projects (enroll) and PATCH /known-projects/{id} (toggle).
    if (url.includes('/api/context/known-projects')) {
      return { ok: true, status: 200, json: async () => ({ scope_id: 'x', enabled: true }) };
    }
    return upstream(input, init);
  };
}

async function boot(calls) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js', 'context-portal.js'] });
  stubFetch(dom.window, calls);
  dom.window.ensureCsrfToken = async () => 'tok-123';
  dom.window.showConfirm = async () => true;
  return dom;
}

const row = (window, id) => window.document.querySelector(`.ctx-portal-row[data-scope-id="${id}"]`);

// The "Initialized only" toggle is default-ON; turn it off when a test asserts
// on a stale (uninitialized) row that would otherwise be hidden.
function showUninitialized(window) {
  const cb = window.document.querySelector('#ctx-portal-hide-uninit');
  if (cb && cb.checked) {
    cb.checked = false;
    cb.dispatchEvent(new window.Event('change', { bubbles: true }));
  }
}

describe('sync-eligibility helpers', () => {
  it('_ctxScopeIsEnrolled reads "known-projects" in sources', async () => {
    const { window } = await boot();
    expect(window._ctxScopeIsEnrolled({ sources: ['known-projects'] })).toBe(true);
    expect(window._ctxScopeIsEnrolled({ sources: ['claude-projects'] })).toBe(false);
    expect(window._ctxScopeIsEnrolled({ sources: ['server-cwd'] })).toBe(false);
    expect(window._ctxScopeIsEnrolled({})).toBe(false);
  });

  it('_ctxScopeSyncEligible trusts the backend field when present', async () => {
    const { window } = await boot();
    // Field wins even when it contradicts the derived value.
    expect(window._ctxScopeSyncEligible({ sync_eligible: true, sources: ['claude-projects'] })).toBe(true);
    expect(window._ctxScopeSyncEligible({ sync_eligible: false, sources: ['server-cwd'] })).toBe(false);
  });

  it('_ctxScopeSyncEligible re-derives when the field is absent', async () => {
    const { window } = await boot();
    expect(window._ctxScopeSyncEligible({ sources: ['server-cwd'] })).toBe(true);
    expect(window._ctxScopeSyncEligible({ sources: ['known-projects'], enabled: true })).toBe(true);
    expect(window._ctxScopeSyncEligible({ sources: ['known-projects'], enabled: false })).toBe(false);
    expect(window._ctxScopeSyncEligible({ sources: ['claude-projects'] })).toBe(false);
  });
});

describe('_ctxErrDetail — structured 409 (#1210) → localized string', () => {
  it('passes a plain-string detail through unchanged (back-compat with every non-gated route)', async () => {
    const { window } = await boot();
    expect(window._ctxErrDetail('Already exists', 'fb')).toBe('Already exists');
  });

  it('maps reason_code → the localized toast copy (not the raw key, not [object Object])', async () => {
    const { window } = await boot();
    // i18n loads the catalog asynchronously and ``boot`` doesn't await it;
    // ``setLang`` resolves once ``/locales/en.json`` is in, so ``t()`` returns
    // real copy rather than the raw-key fallback.
    await window.I18N.setLang('en');
    const paused = window._ctxErrDetail({ reason_code: 'sync_paused' }, 'fb');
    expect(paused).toBe(window.t('settings.ctx.error_sync_paused'));
    expect(paused).not.toBe('settings.ctx.error_sync_paused'); // key actually resolved
    expect(paused).not.toContain('[object Object]');

    const notEnrolled = window._ctxErrDetail({ reason_code: 'sync_not_enrolled' }, 'fb');
    expect(notEnrolled).toBe(window.t('settings.ctx.error_sync_not_enrolled'));
    expect(notEnrolled).not.toBe('settings.ctx.error_sync_not_enrolled');
  });

  it('falls back to the backend message for an unknown reason_code', async () => {
    const { window } = await boot();
    expect(window._ctxErrDetail(
      { reason_code: 'something_else', message: 'Backend said no' }, 'fb',
    )).toBe('Backend said no');
  });

  it('uses .message when there is no reason_code', async () => {
    const { window } = await boot();
    expect(window._ctxErrDetail({ message: 'plain message' }, 'fb')).toBe('plain message');
  });

  it('falls back to the caller fallback for empty / unusable detail', async () => {
    const { window } = await boot();
    expect(window._ctxErrDetail(undefined, 'fb')).toBe('fb');
    expect(window._ctxErrDetail(null, 'fb')).toBe('fb');
    expect(window._ctxErrDetail({}, 'fb')).toBe('fb');
    expect(window._ctxErrDetail({ reason_code: 'something_else' }, 'fb')).toBe('fb');
  });
});

describe('_ctxErrDetail — B-1 #1284 error_kind envelope → localized kind label', () => {
  it('composes the localized kind label with the server message for every kind', async () => {
    const { window } = await boot();
    await window.I18N.setLang('en');
    for (const kind of ['parse', 'permission', 'missing', 'internal', 'validation', 'conflict', 'busy']) {
      const label = window.t(`settings.ctx.error_kind_${kind}`);
      // every kind has an en+ko key, so t() resolves (does not echo the key)
      expect(label).not.toBe(`settings.ctx.error_kind_${kind}`);
      const out = window._ctxErrDetail({ error_kind: kind, message: 'detail text' }, 'fb');
      expect(out).toBe(`${label} — detail text`);
      expect(out).not.toContain('[object Object]');
    }
  });

  it('shows only the localized kind label when the envelope has no message', async () => {
    const { window } = await boot();
    await window.I18N.setLang('en');
    expect(window._ctxErrDetail({ error_kind: 'conflict' }, 'fb')).toBe(
      window.t('settings.ctx.error_kind_conflict'),
    );
  });

  it('lets a specific reason_code win over the generic error_kind (sync_paused on a conflict 409)', async () => {
    const { window } = await boot();
    await window.I18N.setLang('en');
    // The sync-ineligible 409s carry BOTH error_kind:"conflict" AND a reason_code;
    // the reason_code copy is more specific and must take precedence.
    const out = window._ctxErrDetail(
      { error_kind: 'conflict', reason_code: 'sync_paused', message: 'Project p-x is not enrolled for sync' },
      'fb',
    );
    expect(out).toBe(window.t('settings.ctx.error_sync_paused'));
    expect(out).not.toBe(window.t('settings.ctx.error_kind_conflict'));
  });

  it('echoes an unknown error_kind verbatim (forward-compat) and never leaks the i18n key or [object Object]', async () => {
    const { window } = await boot();
    await window.I18N.setLang('en');
    const out = window._ctxErrDetail({ error_kind: 'future_kind', message: 'msg' }, 'fb');
    expect(out).toBe('future_kind — msg');
    expect(out).not.toContain('[object Object]');
    expect(out).not.toContain('settings.ctx.error_kind_');
  });
});

describe('portal enrollment UI', () => {
  it('scan-only row offers Enroll and hides rename/remove/toggle', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const scan = row(window, 'p-scan');
    expect(scan.querySelector('.ctx-portal-enroll')).not.toBeNull();
    // No known_projects entry → PATCH/DELETE would 404, so these must be absent.
    expect(scan.querySelector('.ctx-portal-rename')).toBeNull();
    expect(scan.querySelector('.ctx-portal-remove')).toBeNull();
    expect(scan.querySelector('.ctx-portal-toggle-sync')).toBeNull();
  });

  it('enrolled row offers Pause + rename/remove, no Enroll, no paused badge', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const on = row(window, 'p-on');
    const toggle = on.querySelector('.ctx-portal-toggle-sync');
    expect(toggle).not.toBeNull();
    expect(toggle.textContent.trim()).toBe('Pause sync');
    expect(on.querySelector('.ctx-portal-rename')).not.toBeNull();
    expect(on.querySelector('.ctx-portal-remove')).not.toBeNull();
    expect(on.querySelector('.ctx-portal-enroll')).toBeNull();
    expect(on.querySelector('.ctx-scope-badge--paused')).toBeNull();
  });

  it('paused row shows the paused badge and a Resume toggle', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const off = row(window, 'p-off');
    expect(off.querySelector('.ctx-scope-badge--paused')).not.toBeNull();
    expect(off.querySelector('.ctx-portal-toggle-sync').textContent.trim()).toBe('Resume sync');
  });

  it('a paused AND stale row renders BOTH badges (orthogonal, not folded into the chain)', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    showUninitialized(window); // p-paused-stale is stale → hidden by default
    const ps = row(window, 'p-paused-stale');
    // Folding paused into the `if (missing) ... else if (stale) ...` chain would
    // drop the paused badge here (stale wins the else-if), so assert both.
    expect(ps.querySelector('.ctx-scope-badge--paused')).not.toBeNull();
    expect(ps.querySelector('.ctx-scope-badge--stale')).not.toBeNull();
  });

  it('a server-cwd + enrolled scope stays non-managed/non-enrollable with NO paused badge', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const r = row(window, 'p-cwd-enrolled');
    // Pins the `&& !_ctxScopeIsServerCwd` guard in _ctxPortalIsManaged,
    // _ctxPortalCanEnroll and _ctxPortalIsPaused: dropping it would expose
    // Pause/Rename/Remove/Enroll on the running directory's row, or render an
    // unresumable "sync paused" badge despite enabled:false + sync_eligible:true.
    expect(r.querySelector('.ctx-portal-enroll')).toBeNull();
    expect(r.querySelector('.ctx-portal-toggle-sync')).toBeNull();
    expect(r.querySelector('.ctx-portal-rename')).toBeNull();
    expect(r.querySelector('.ctx-portal-remove')).toBeNull();
    expect(r.querySelector('.ctx-scope-badge--paused')).toBeNull();
  });

  it('Server CWD is never enrollable or managed', async () => {
    const { window } = await boot();
    await window.loadCtxProjects();
    const cwd = row(window, '');
    expect(cwd.querySelector('.ctx-portal-enroll')).toBeNull();
    expect(cwd.querySelector('.ctx-portal-toggle-sync')).toBeNull();
    expect(cwd.querySelector('.ctx-portal-rename')).toBeNull();
  });

  it('Enroll POSTs the scope root with the CSRF token threaded', async () => {
    const calls = [];
    const { window } = await boot(calls);
    await window.loadCtxProjects();
    row(window, 'p-scan').querySelector('.ctx-portal-enroll')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));

    const post = calls.find(c => (c.init.method || '').toUpperCase() === 'POST');
    expect(post).toBeTruthy();
    expect(post.url).toBe('/api/context/known-projects');
    expect(post.init.headers['X-Memtomem-CSRF']).toBe('tok-123');
    expect(JSON.parse(post.init.body)).toEqual({ root: '/work/scan' });
  });

  it('Pause PATCHes enabled:false; Resume PATCHes enabled:true', async () => {
    let calls = [];
    let dom = await boot(calls);
    await dom.window.loadCtxProjects();
    row(dom.window, 'p-on').querySelector('.ctx-portal-toggle-sync')
      .dispatchEvent(new dom.window.Event('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    let patch = calls.find(c => (c.init.method || '').toUpperCase() === 'PATCH');
    expect(patch.url).toContain('/api/context/known-projects/p-on');
    expect(JSON.parse(patch.init.body)).toEqual({ enabled: false });

    // Resume from the paused row → enabled:true.
    calls = [];
    dom = await boot(calls);
    await dom.window.loadCtxProjects();
    row(dom.window, 'p-off').querySelector('.ctx-portal-toggle-sync')
      .dispatchEvent(new dom.window.Event('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    patch = calls.find(c => (c.init.method || '').toUpperCase() === 'PATCH');
    expect(patch.url).toContain('/api/context/known-projects/p-off');
    expect(JSON.parse(patch.init.body)).toEqual({ enabled: true });
  });
});

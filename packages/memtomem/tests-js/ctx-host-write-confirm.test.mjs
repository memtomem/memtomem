/* #1263 PR-2: user-tier writes ride a disclose-then-confirm round-trip.
 *
 * Server contract (shipped in #1302): an unconfirmed ``target_scope=user``
 * write answers HTTP 200 ``{status: "needs_confirmation",
 * confirm: "allow_host_writes", reason, host_targets}`` and writes nothing;
 * the confirmed re-send carries ``allow_host_writes=true`` (body field on
 * POST/PUT, query parameter on DELETE). These tests pin the JS half:
 *
 *   - the tier filter no longer blanket-blocks the user tier — CRUD /
 *     sync / import buttons stay live there, while Sync All keeps its
 *     block (its multi-phase run hits settings + mcp-servers, which have
 *     no user-tier surface), and project_local still blocks everything;
 *   - ``_ctxConfirmHostWrite`` opens the shared confirm modal with the
 *     disclosed paths and re-sends with the flag on approval;
 *   - declining sends nothing further (one request total, no success
 *     toast) — a declined disclosure is a choice, not an error;
 *   - the DELETE leg carries the flag in the query string.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function flush(window, ticks = 30) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => window.setTimeout(r, 0));
}

const SYNC_ENVELOPE = {
  status: 'needs_confirmation',
  confirm: 'allow_host_writes',
  reason:
    'Push skills targets the user tier — host paths outside any project '
    + 'root. Re-send the request with allow_host_writes=true after '
    + 'confirming with the user.',
  host_targets: ['/home/u/.claude/skills/a', '/home/u/.gemini/skills/a'],
};

const SYNC_OK = { generated: [{ runtime: 'claude_skills', path: 'x' }], dropped: [], skipped: [] };

// Healthy roster — the list render error-paths (and toasts) on an empty
// scopes array (#1287), which would bury the banner/buttons under a Retry
// state and pollute the toast assertions.
const SCOPES = [
  {
    scope_id: '', label: 'Server CWD', root: '/srv', tier: 'project',
    sources: ['server-cwd'], missing: false, stale: false, experimental: false,
    counts: { skills: 1, commands: 0, agents: 0, 'mcp-servers': 0 },
  },
];

async function bootUserTier({ syncResponses, confirmAnswers }) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  const confirms = [];
  const answers = [...confirmAnswers];
  window.showConfirm = async (opts) => {
    confirms.push(opts);
    return answers.length ? answers.shift() : false;
  };
  const toasts = [];
  window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });
  window.ensureCsrfToken = async () => 'test-token';
  const syncCalls = [];
  const pending = [...syncResponses];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const path = url.split('?')[0];
    if (path.endsWith('/api/context/skills/sync')) {
      syncCalls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
      const body = pending.length ? pending.shift() : SYNC_OK;
      return { ok: true, status: 200, json: async () => body };
    }
    if (path.endsWith('/api/context/skills')) {
      return { ok: true, status: 200, json: async () => ({ skills: [] }) };
    }
    if (path.includes('/api/context/projects')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ scopes: SCOPES, target_scope: 'project_shared' }),
      };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  // jsdom (this harness version) lacks the global ``CSS`` — the list
  // group wiring calls ``CSS.escape`` and would false-fail the render
  // into its catch path without it. Real browsers always have it.
  if (!window.CSS) {
    window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
  }
  // The tier filter lives in the control bar, which mounts on the first
  // list render — load one section before driving the real control so
  // ``_ctxTargetScope`` and the write-block sweep both update. The click
  // re-renders the control bar's own (overview) surface; re-load the
  // skills list afterwards so the section reflects the new tier the way
  // a user navigating the section would see it.
  await window.loadCtxList('skills');
  await flush(window);
  const userBtn = window.document.querySelector(
    '.ctx-tier-filter button[data-scope="user"]',
  );
  userBtn.click();
  await flush(window);
  await window.loadCtxList('skills');
  await flush(window);
  return { window, confirms, toasts, syncCalls };
}

describe('tier filter write-block matrix (#1263)', () => {
  it('keeps CRUD/sync/import live on the user tier, blocks Sync All', async () => {
    const { window } = await bootUserTier({ syncResponses: [], confirmAnswers: [] });
    const doc = window.document;
    const section = doc.getElementById('settings-ctx-skills');
    for (const sel of ['.ctx-create-btn', '.ctx-sync-btn', '.ctx-import-btn']) {
      const btn = section.querySelector(sel);
      expect(btn.dataset.writeBlocked, sel).toBeUndefined();
      expect(btn.getAttribute('aria-disabled'), sel).toBeNull();
    }
    const syncAll = doc.getElementById('ctx-sync-all-btn');
    expect(syncAll.dataset.writeBlocked).toBe('user');
  });

  it('keeps MCP Servers blocked on the user tier (project_shared-only routes)', async () => {
    // Same button classes as the open families, different route policy
    // (ADR-0011 §1) — the type scoping is what keeps these blocked.
    const { window } = await bootUserTier({ syncResponses: [], confirmAnswers: [] });
    const section = window.document.getElementById('settings-ctx-mcp-servers');
    // No import button here — _CTX_TOOLBAR_CAPS['mcp-servers'].import is false.
    for (const sel of ['.ctx-create-btn', '.ctx-sync-btn']) {
      const btn = section.querySelector(sel);
      expect(btn.dataset.writeBlocked, sel).toBe('user');
      expect(btn.getAttribute('aria-disabled'), sel).toBe('true');
    }
  });

  it('still blocks everything on project_local', async () => {
    const { window } = await bootUserTier({ syncResponses: [], confirmAnswers: [] });
    const doc = window.document;
    doc.querySelector('.ctx-tier-filter button[data-scope="project_local"]').click();
    await flush(window);
    const section = doc.getElementById('settings-ctx-skills');
    for (const sel of ['.ctx-create-btn', '.ctx-sync-btn', '.ctx-import-btn']) {
      expect(section.querySelector(sel).dataset.writeBlocked, sel).toBe('project_local');
    }
  });

  it('renders the user-tier banner with confirm-first copy, not read-only copy', async () => {
    const { window } = await bootUserTier({ syncResponses: [], confirmAnswers: [] });
    const banner = window.document.querySelector(
      '.ctx-write-blocked-banner[data-tier="user"]',
    );
    expect(banner).toBeTruthy();
    expect(banner.textContent).toContain('confirmation');
    expect(banner.textContent.toLowerCase()).not.toContain('read-only');
  });
});

describe('host-write confirm round-trip (sync)', () => {
  it('discloses host_targets and re-sends with allow_host_writes on approval', async () => {
    const { window, confirms, syncCalls, toasts } = await bootUserTier({
      syncResponses: [SYNC_ENVELOPE, SYNC_OK],
      // 1st dialog = the existing sync-impact confirm, 2nd = host-write.
      confirmAnswers: [true, true],
    });
    const section = window.document.getElementById('settings-ctx-skills');
    section.dataset.canonicalCount = '1';
    section.querySelector('.ctx-sync-btn').click();
    await flush(window);

    expect(syncCalls.length).toBe(2);
    expect(syncCalls[0].body).toBeNull(); // first leg keeps the no-body shape
    expect(syncCalls[1].body).toEqual({ allow_host_writes: true });

    const hostDialog = confirms[confirms.length - 1];
    // Localized copy resolved (not a raw key echo), and every disclosed
    // path is in the warning listing.
    expect(hostDialog.title).toBe('Write outside the project?');
    expect(hostDialog.message).toContain('2');
    for (const target of SYNC_ENVELOPE.host_targets) {
      expect(hostDialog.warningText).toContain(target);
    }
    expect(hostDialog.danger).toBe(false);
    expect(toasts.some((x) => x.sev === 'success' || x.sev === undefined)).toBe(true);
  });

  it('sends nothing further when the disclosure is declined', async () => {
    const { window, syncCalls, toasts } = await bootUserTier({
      syncResponses: [SYNC_ENVELOPE],
      confirmAnswers: [true, false], // approve impact, decline host write
    });
    const section = window.document.getElementById('settings-ctx-skills');
    section.dataset.canonicalCount = '1';
    // Boot-time background loads (projects roster fed by the harness's
    // generic empty-200 stub) may already have toasted — measure the
    // decline flow as a DELTA so this pins the click, not the harness.
    const errorsBefore = toasts.filter((x) => x.sev === 'error').length;
    section.querySelector('.ctx-sync-btn').click();
    await flush(window);

    expect(syncCalls.length).toBe(1);
    // No success toast — and no NEW error toast either: declining is a choice.
    expect(toasts.filter((x) => x.sev === 'error').length).toBe(errorsBefore);
    expect(toasts.filter((x) => x.sev === 'success').length).toBe(0);
  });

  it('caps the disclosed listing and appends the more-count line', async () => {
    const many = Array.from({ length: 11 }, (_, i) => `/home/u/.claude/skills/s${i}`);
    const { window, confirms } = await bootUserTier({
      syncResponses: [{ ...SYNC_ENVELOPE, host_targets: many }, SYNC_OK],
      confirmAnswers: [true, true],
    });
    const section = window.document.getElementById('settings-ctx-skills');
    section.dataset.canonicalCount = '1';
    section.querySelector('.ctx-sync-btn').click();
    await flush(window);
    const hostDialog = confirms[confirms.length - 1];
    expect(hostDialog.warningText).toContain('s0');
    expect(hostDialog.warningText).toContain('s7');
    expect(hostDialog.warningText).not.toContain('s8'); // capped at 8
    expect(hostDialog.warningText).toContain('3 more');
  });
});

describe('import destination label (#1263 fold)', () => {
  it('names the user tier, not Project (shared), when the import is tier-pinned to user', async () => {
    const { window, confirms } = await bootUserTier({
      syncResponses: [],
      confirmAnswers: [{ ok: false, extras: {} }], // open the dialog, then bail
    });
    const upstream = window.fetch;
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url.split('?')[0].endsWith('/api/context/skills/import')) {
        return { ok: true, status: 200, json: async () => ({ imported: [], skipped: [] }) };
      }
      return upstream(input, opts);
    };
    const section = window.document.getElementById('settings-ctx-skills');
    section.querySelector('.ctx-import-btn').click();
    await flush(window);
    const dialog = confirms[confirms.length - 1];
    expect(dialog.message).toContain('User');
    expect(dialog.message).not.toContain('Project (shared)');
  });
});

describe('host-write confirm round-trip (delete — query-param flag)', () => {
  it('re-sends DELETE with allow_host_writes=true in the query string', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    const answers = [{ ok: true, extras: {} }, true]; // cascade dialog, host dialog
    window.showConfirm = async () => answers.shift();
    window.showToast = () => {};
    window.ensureCsrfToken = async () => 'test-token';

    const DETAIL = {
      name: 'a',
      content: '# a\n',
      mtime_ns: '1',
      files: [],
      target_scope: 'user',
      layout: 'dir',
      fields: {},
    };
    const ENVELOPE = {
      status: 'needs_confirmation',
      confirm: 'allow_host_writes',
      reason: 'Delete skill targets the user tier — …',
      host_targets: ['/home/u/.memtomem/skills/a'],
    };
    const deleteCalls = [];
    const upstream = window.fetch;
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      const path = url.split('?')[0];
      const method = (opts && opts.method) || 'GET';
      if (method === 'DELETE' && path.endsWith('/api/context/skills/a')) {
        deleteCalls.push(url);
        const body = deleteCalls.length === 1 ? ENVELOPE : { deleted: ['x'], skipped: [] };
        return { ok: true, status: 200, json: async () => body };
      }
      if (path.endsWith('/api/context/skills/a')) {
        return { ok: true, status: 200, json: async () => DETAIL };
      }
      if (path.endsWith('/api/context/skills')) {
        return { ok: true, status: 200, json: async () => ({ skills: [] }) };
      }
      if (path.includes('/api/context/projects')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ scopes: SCOPES, target_scope: 'project_shared' }),
        };
      }
      return upstream(input, opts);
    };
    await window.I18N.init();
    if (!window.CSS) {
      window.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`) };
    }
    await window.loadCtxList('skills');
    await flush(window);
    window.document.querySelector('.ctx-tier-filter button[data-scope="user"]').click();
    await flush(window);

    await window.loadCtxDetail('skills', 'a');
    await flush(window);
    const delBtn = window.document.querySelector('.ctx-detail-delete-btn');
    expect(delBtn).toBeTruthy();
    expect(delBtn.dataset.writeBlocked).toBeUndefined(); // live on user tier
    delBtn.click();
    await flush(window);

    expect(deleteCalls.length).toBe(2);
    expect(deleteCalls[0]).not.toContain('allow_host_writes');
    expect(deleteCalls[1]).toContain('allow_host_writes=true');
    expect(deleteCalls[1]).toContain('cascade=false');
  });
});

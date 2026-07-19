/* Web "Import to user library" — the cross-tier escape hatch for a
 * PROJECT-runtime skill that the project tier can't accept.
 *
 * A project-runtime skill (under <project>/.claude/skills) that trips Gate A's
 * false-positive secret heuristic has no plain web import path: project_shared
 * is a hard 422 (git is forever), project_local is rejected, and a user-tier
 * import reads ~/.claude (the wrong source). The new ``import-to-user`` route
 * reads the PROJECT runtime but writes the force-bypassable USER canonical.
 *
 * These pin the frontend half:
 *   - the "Import to user library" button renders on a runtime-only SKILL
 *     detail outside the user tier, and only for skills (the route is
 *     skills-only — agents/commands would 404);
 *   - clicking it posts to ``/skills/{name}/import-to-user`` with NO tier
 *     param (the route pins scope=user / source=project itself), running the
 *     shared host-write + force flow;
 *   - ``_ctxRunRuntimeImportFlow`` drives envelope → force correctly.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCRIPTS = ['i18n.js', 'app.js', 'context-gateway.js'];

function runtimeOnlySkillDiff(name, content = '# s\n') {
  return {
    name,
    canonical_content: null,
    canonical_path: `.memtomem/skills/${name}`,
    runtimes: [{ runtime: 'claude_skills', status: 'missing canonical', runtime_content: content }],
  };
}

const jsonOk = (body) => ({ ok: true, status: 200, json: async () => body });

describe('Import-to-user-library button (rendering gate)', () => {
  it('renders on a runtime-only SKILL detail in the project_shared tier', async () => {
    const { window } = await bootApp({
      scripts: SCRIPTS,
      apiResponses: { '/api/context/skills/architect/diff': runtimeOnlySkillDiff('architect') },
    });
    await window.I18N.init();
    const detailEl = window.document.getElementById('ctx-skills-detail');
    await window._ctxLoadRuntimeOnlyDetail('skills', 'architect', detailEl);

    const btn = detailEl.querySelector('.ctx-runtime-import-to-user');
    expect(btn).not.toBeNull();
    expect(btn.textContent.trim()).toBe('Pull to user library');
    // The plain Import button is still there too.
    expect(detailEl.querySelector('.ctx-runtime-only-import')).not.toBeNull();
  });

  it('does NOT render for non-skill kinds (route is skills-only)', async () => {
    const { window } = await bootApp({
      scripts: SCRIPTS,
      apiResponses: {
        '/api/context/commands/deploy/diff': {
          name: 'deploy',
          canonical_content: null,
          canonical_path: '.memtomem/commands/deploy.md',
          runtimes: [
            { runtime: 'claude_commands', status: 'missing canonical', runtime_content: '# d' },
          ],
        },
      },
    });
    await window.I18N.init();
    const detailEl = window.document.getElementById('ctx-commands-detail');
    await window._ctxLoadRuntimeOnlyDetail('commands', 'deploy', detailEl);

    // commands have a plain Import button but NOT the user-library escape hatch.
    expect(detailEl.querySelector('.ctx-runtime-only-import')).not.toBeNull();
    expect(detailEl.querySelector('.ctx-runtime-import-to-user')).toBeNull();
  });
});

describe('Import-to-user-library button (wiring)', () => {
  it('posts to /skills/{name}/import-to-user with no tier param, runs host-write + success', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    await window.I18N.init();
    window.ensureCsrfToken = async () => 'test-token';
    const confirms = [];
    window.showConfirm = async (opts) => { confirms.push(opts); return true; };
    const toasts = [];
    window.showToast = (msg, sev) => toasts.push({ msg, sev: sev || 'success' });

    const importCalls = [];
    const HOST_ENVELOPE = {
      status: 'needs_confirmation',
      confirm: 'allow_host_writes',
      reason: 'Import skill to user library targets the user tier …',
      host_targets: ['/home/u/.memtomem/skills/architect'],
    };
    const responses = [HOST_ENVELOPE, { imported: [{ name: 'architect' }], skipped: [] }];
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      const path = url.split('?')[0];
      const method = (opts && opts.method) || 'GET';
      if (path.endsWith('/api/context/skills/architect/diff')) {
        return jsonOk(runtimeOnlySkillDiff('architect'));
      }
      if (method === 'POST' && path.endsWith('/api/context/skills/architect/import-to-user')) {
        importCalls.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
        return jsonOk(responses.shift());
      }
      return jsonOk({});
    };

    const detailEl = window.document.getElementById('ctx-skills-detail');
    await window._ctxLoadRuntimeOnlyDetail('skills', 'architect', detailEl);
    detailEl.querySelector('.ctx-runtime-import-to-user').click();
    // let the click handler's awaits settle
    for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));

    expect(importCalls.length).toBe(2);
    // No tier param — the route pins scope=user / source=project itself.
    expect(importCalls[0].url).not.toContain('target_scope');
    expect(importCalls[0].url).toContain('/api/context/skills/architect/import-to-user');
    // First call bare, confirmed re-send carries the host-write opt-in.
    expect(importCalls[0].body).toEqual({});
    expect(importCalls[1].body).toEqual({ allow_host_writes: true });
    // Host-write disclosure ran and a success toast fired.
    expect(confirms.length).toBe(1);
    expect(confirms[confirms.length - 1].warningText).toContain('/home/u/.memtomem/skills/architect');
    expect(toasts.some((x) => x.sev === 'success')).toBe(true);
  });
});

describe('Import-to-user-library button (active project scope)', () => {
  // Codex Major regression: the POST must carry the ACTIVE project's scope_id,
  // or resolve_scope_root falls back to server CWD and reads the wrong
  // project's runtime. project_shared is the default tier, so target_scope
  // stays omitted while scope_id rides.
  const SCOPES = [
    { scope_id: '', label: 'Server CWD', root: '/srv', tier: 'project', sources: ['server-cwd'], missing: false, experimental: false, counts: { skills: 0, commands: 0, agents: 0 } },
    { scope_id: 'proj-abc', label: 'proj-abc', root: '/work/proj-abc', tier: 'project', sources: ['memtomem-config'], missing: false, experimental: false, counts: { skills: 0, commands: 0, agents: 0 } },
  ];

  it('sends the active project scope_id and omits target_scope', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    await window.I18N.init();
    window.ensureCsrfToken = async () => 'test-token';
    window.showConfirm = async () => true;
    window.showToast = () => {};
    window.loadCtxList = async () => {};
    window.loadCtxOverview = async () => {};

    const importCalls = [];
    const HOST_ENVELOPE = {
      status: 'needs_confirmation', confirm: 'allow_host_writes',
      reason: '…', host_targets: ['/home/u/.memtomem/skills/architect'],
    };
    const responses = [HOST_ENVELOPE, { imported: [{ name: 'architect' }], skipped: [] }];
    window.fetch = async (input, opts) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      const path = url.split('?')[0];
      const method = (opts && opts.method) || 'GET';
      if (path.startsWith('/api/context/projects')) return jsonOk({ scopes: SCOPES });
      if (path.endsWith('/api/context/skills/architect/diff')) return jsonOk(runtimeOnlySkillDiff('architect'));
      if (method === 'POST' && path.endsWith('/api/context/skills/architect/import-to-user')) {
        importCalls.push(url);
        return jsonOk(responses.shift());
      }
      return jsonOk({});
    };

    // Flip the active scope to a non-CWD project through the real code path
    // (module-scoped ``_ctxActiveScopeId`` can't be poked from outside).
    await window._ctxFetchProjects();
    const wrap = window.document.createElement('label');
    wrap.className = 'ctx-project-switcher';
    wrap.dataset.type = 'skills';
    const select = window.document.createElement('select');
    select.className = 'ctx-project-select';
    for (const s of SCOPES) {
      const o = window.document.createElement('option');
      o.value = s.scope_id; o.textContent = s.label; select.appendChild(o);
    }
    wrap.appendChild(select);
    window.document.body.appendChild(wrap);
    window._ctxWireProjectControls();
    select.value = 'proj-abc';
    select.dispatchEvent(new window.Event('change', { bubbles: true }));

    const detailEl = window.document.getElementById('ctx-skills-detail');
    await window._ctxLoadRuntimeOnlyDetail('skills', 'architect', detailEl);
    detailEl.querySelector('.ctx-runtime-import-to-user').click();
    for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));

    expect(importCalls.length).toBeGreaterThanOrEqual(1);
    expect(importCalls[0]).toContain('scope_id=proj-abc');
    expect(importCalls[0]).not.toContain('target_scope');
  });
});

describe('_ctxRunRuntimeImportFlow (shared envelope→force flow)', () => {
  it('handles the host-write envelope then offers the force valve', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    await window.I18N.init();
    // Three confirms: host-write disclosure, force override, then the SECOND
    // host-write disclosure for the now-would-import forced target (#1379
    // consent separation re-discloses after forcing).
    const answers = [true, true, true];
    window.showConfirm = async () => answers.shift();
    window.showToast = () => {};

    const calls = [];
    const responses = [
      { status: 'needs_confirmation', confirm: 'allow_host_writes', host_targets: ['/h/x'] },
      { imported: [], skipped: [{ name: 'x', reason: 'p', reason_code: 'privacy_blocked' }] },
      { status: 'needs_confirmation', confirm: 'allow_host_writes', host_targets: ['/h/x'] },
      { imported: [{ name: 'x' }], skipped: [] },
    ];
    const importOnce = async (extra) => {
      calls.push(extra);
      return jsonOk(responses.shift());
    };
    const data = await window._ctxRunRuntimeImportFlow(importOnce);

    // bare → allow_host_writes → force-alone → force+allow_host_writes
    expect(calls[0]).toEqual({});
    expect(calls[1]).toEqual({ allow_host_writes: true });
    expect(calls[2]).toEqual({ force_unsafe_import: true });
    expect(calls[3]).toEqual({ force_unsafe_import: true, allow_host_writes: true });
    expect(data.imported).toEqual([{ name: 'x' }]);
  });

  it('returns the result directly when no host write and no privacy skip', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    await window.I18N.init();
    window.showToast = () => {};
    const importOnce = async () => jsonOk({ imported: [{ name: 'y' }], skipped: [] });
    const data = await window._ctxRunRuntimeImportFlow(importOnce);
    expect(data.imported).toEqual([{ name: 'y' }]);
  });
});

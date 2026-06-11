/* #1247 B9 — two JS contract fixes on the artifact detail panel.
 *
 * id 30: the diff pane diffs ``expected_content`` (what sync would write —
 * vendor override or rendered output) against the runtime file when the
 * response provides it, falling back to ``canonical_content`` for response
 * shapes that don't send it (skills, mcp-servers). The raw canonical diff
 * showed md-vs-toml noise for rendering runtimes.
 *
 * id 33: the delete handler branches on ``skipped.length`` instead of
 * ``if (data.deleted)`` — ``[]`` is truthy, so a fully-failed delete
 * ({deleted: [], skipped: [{path, reason}]}) used to show the success toast
 * and hide the detail panel.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function fetchResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

describe('diff pane expected-content baseline (_ctxLoadDiff)', () => {
  async function bootDiff(type, diffBody) {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    await window.I18N.init();
    const realFetch = window.fetch;
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url;
      if (url && url.includes(`/diff`)) return fetchResponse(200, diffBody);
      if (url && url.includes(`/rendered`)) return fetchResponse(200, {});
      return realFetch(input);
    };
    const detailEl = window.document.createElement('div');
    detailEl.innerHTML = `<div id="ctx-pane-${type}-diff"></div>`;
    window.document.body.appendChild(detailEl);
    await window._ctxLoadDiff(type, 'x', detailEl);
    return detailEl.querySelector(`#ctx-pane-${type}-diff`).innerHTML;
  }

  it('diffs expected_content (not canonical) when the row provides it', async () => {
    const html = await bootDiff('commands', {
      name: 'x',
      canonical_content: 'CANONICAL-LINE\n',
      canonical_path: '.memtomem/commands/x.md',
      runtimes: [{
        runtime: 'gemini_commands',
        status: 'out of sync',
        expected_content: 'EXPECTED-LINE\n',
        runtime_content: 'RUNTIME-LINE\n',
      }],
    });
    expect(html).toContain('EXPECTED-LINE');
    expect(html).toContain('RUNTIME-LINE');
    // The raw canonical text must NOT be the diff baseline anymore.
    expect(html).not.toContain('CANONICAL-LINE');
  });

  it('falls back to canonical_content for rows without expected_content (skills shape)', async () => {
    const html = await bootDiff('skills', {
      name: 'x',
      canonical_content: 'CANONICAL-LINE\n',
      runtimes: [{
        runtime: 'claude_skills',
        status: 'out of sync',
        runtime_content: 'RUNTIME-LINE\n',
      }],
    });
    expect(html).toContain('CANONICAL-LINE');
    expect(html).toContain('RUNTIME-LINE');
  });
});

describe('delete toast branches on skipped, not array truthiness', () => {
  async function bootDelete(deleteBody) {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
    const { window } = dom;
    await window.I18N.init();

    const toasts = [];
    window.showToast = (message, kind = 'success') => toasts.push({ message, kind });
    window.showConfirm = async () => ({ ok: true, extras: {} });
    window.ensureCsrfToken = async () => 'test-token';
    const listReloads = [];
    window.loadCtxList = (type) => listReloads.push(type);

    const realFetch = window.fetch;
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url;
      const pathname = (url || '').split('?')[0];
      if (init.method === 'DELETE') return fetchResponse(200, deleteBody);
      if (pathname === '/api/context/commands/x') {
        // Detail GET — minimal payload the detail renderer needs.
        return fetchResponse(200, {
          name: 'x',
          content: 'body\n',
          mtime_ns: '1',
          fields: {},
          target_scope: 'project_shared',
          layout: 'flat',
        });
      }
      return realFetch(input, init);
    };

    await window.loadCtxDetail('commands', 'x');
    const detailEl = window.document.getElementById('ctx-commands-detail');
    const btn = detailEl.querySelector('.ctx-detail-delete-btn');
    expect(btn).toBeTruthy();
    btn.click();
    // The click handler is async (confirm → fetch → toast); drain it.
    for (let i = 0; i < 5; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return { toasts, listReloads, detailEl };
  }

  it('shows a warning naming the failure when every leg was skipped', async () => {
    const { toasts, listReloads, detailEl } = await bootDelete({
      deleted: [],
      skipped: [{ path: '.claude/commands/x.md', reason: 'Permission denied' }],
    });
    expect(toasts).toHaveLength(1);
    expect(toasts[0].kind).toBe('warning');
    expect(toasts[0].message).toContain('.claude/commands/x.md');
    expect(toasts[0].message).toContain('Permission denied');
    // The artifact may still exist — detail stays open, list repaints.
    expect(detailEl.hidden).toBe(false);
    expect(listReloads).toContain('commands');
  });

  it('keeps the success toast for a clean delete', async () => {
    const { toasts, detailEl } = await bootDelete({
      deleted: ['.memtomem/commands/x/command.md'],
      skipped: [],
    });
    expect(toasts).toHaveLength(1);
    expect(toasts[0].kind).toBe('success');
    expect(detailEl.hidden).toBe(true);
  });
});

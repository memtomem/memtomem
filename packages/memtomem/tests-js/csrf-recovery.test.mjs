import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const response = (body, status = 200) => new Response(JSON.stringify(body), { status });
const mismatch = () => response({ code: 'csrf_token_mismatch' }, 403);
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

describe('server rotation recovery without mutation replay', () => {
  let window, calls, sessionCount, token, refuse, bootstrapFails;
  beforeEach(async () => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js'] }));
    calls = []; sessionCount = 0; token = 'A'; refuse = false; bootstrapFails = false;
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') {
        sessionCount++;
        return bootstrapFails ? response({}, 503) : response({ csrf: token });
      }
      if (!opts.method || opts.method === 'GET') return response({});
      calls.push({ url, ...opts });
      return refuse ? mismatch() : response({ ok: true });
    };
    await window.ensureCsrfToken();
  });
  afterEach(() => window.close());

  it('refreshes once, preserves JSON and requires an explicit retry', async () => {
    token = 'B'; refuse = true;
    await expect(window.api('POST', '/api/echo', { draft: 'keep me' }))
      .rejects.toMatchObject({ name: 'CsrfRecoveryError', tokenRefreshed: true });
    expect(calls).toHaveLength(1);
    expect(calls[0].headers['X-Memtomem-CSRF']).toBe('A');
    expect(sessionCount).toBe(2);
    refuse = false;
    await window.api('POST', '/api/echo', { draft: 'keep me' });
    expect(calls).toHaveLength(2);
    expect(calls[1].headers['X-Memtomem-CSRF']).toBe('B');
    expect(calls[1].headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(calls[1].body)).toEqual({ draft: 'keep me' });
  });

  it('never loops on a repeated refusal or a failed bootstrap', async () => {
    refuse = true; bootstrapFails = true;
    await expect(window.csrfFetch('/api/x', { method: 'POST' }))
      .rejects.toMatchObject({ name: 'CsrfRecoveryError', tokenRefreshed: false });
    expect(calls).toHaveLength(1);
    expect(sessionCount).toBe(2);
    bootstrapFails = false;
    await expect(window.csrfFetch('/api/x', { method: 'POST' }))
      .rejects.toMatchObject({ name: 'CsrfRecoveryError' });
    expect(calls).toHaveLength(2);
  });

  it.each([
    { detail: 'CSRF / origin / host check failed' },
    { detail: { detail: 'redaction_blocked', hits: [] } },
    { detail: { code: 'blocked_project_shared' } },
  ])('leaves unrelated 403 bodies available to callers: %j', async body => {
    window.fetch = async () => response(body, 403);
    const res = await window.csrfFetch('/api/x', { method: 'DELETE' });
    expect(await res.json()).toEqual(body);
    expect(sessionCount).toBe(1);
  });

  it('coalesces concurrent refreshes and ignores late refusals of the old token', async () => {
    const refresh = deferred(), late = deferred();
    let writes = 0, bootstraps = 0;
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') { bootstraps++; return refresh.promise; }
      if (!opts.method || opts.method === 'GET') return response({});
      writes++;
      return writes === 3 ? late.promise : mismatch();
    };
    const requests = [1, 2, 3].map(() => window.csrfFetch('/api/x', { method: 'POST' })
      .catch(err => err));
    await new Promise(r => setTimeout(r, 20));
    expect(bootstraps).toBe(1);
    refresh.resolve(response({ csrf: 'B' }));
    await Promise.all(requests.slice(0, 2));
    late.resolve(mismatch());
    expect((await requests[2]).name).toBe('CsrfRecoveryError');
    expect(bootstraps).toBe(1);
    expect(writes).toBe(3);
    expect(await window.ensureCsrfToken()).toBe('B');
  });

  it('preserves multipart bodies and cancellation signals', async () => {
    const form = new window.FormData(); form.append('draft', 'keep me');
    const controller = new window.AbortController();
    await window.csrfFetch('/api/upload', { method: 'POST', body: form, signal: controller.signal });
    expect(calls[0].body).toBe(form);
    expect(calls[0].headers['Content-Type']).toBeUndefined();
    expect(calls[0].signal).toBe(controller.signal);
    controller.abort(); refuse = true;
    await expect(window.csrfFetch('/api/upload', { method: 'POST', signal: controller.signal }))
      .rejects.toMatchObject({ name: 'AbortError' });
    expect(sessionCount).toBe(1);
  });
});

describe('shared recovery at direct caller boundaries', () => {
  let window;
  afterEach(() => window?.close());
  it('stops a hooks batch at the refused write and restores the button', async () => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-hooks-watchdog.js'] }));
    window.eval("_hooksRuleRegistry = [{ _bucket: 'target-only' }, { _bucket: 'target-only' }]");
    window._confirmHooksPromote = async () => ({ ok: true, deleteOriginal: true });
    const toasts = [];
    window.showToast = text => toasts.push(text);
    let writes = 0;
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') return response({ csrf: 'token' });
      if (!opts.method || opts.method === 'GET') return response({});
      writes++;
      return mismatch();
    };
    const btn = window.document.createElement('button');
    await window._handleHooksPromoteAll(btn);
    expect(writes).toBe(1);
    expect(btn.disabled).toBe(false);
    expect(toasts).toContain(window.t('error.csrf_refreshed'));
  });

  it('versioning buttons show recovery guidance instead of generic failure', async () => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] }));
    const toasts = [];
    window.showToast = text => toasts.push(text);
    window.fetch = async (url, opts = {}) => url === '/api/session'
      ? response({ csrf: 'token' }) : opts.method === 'POST' ? mismatch() : response({});
    const pane = window.document.createElement('div');
    pane.innerHTML = '<button class="ctx-version-enable-btn">Enable</button>';
    const btn = pane.querySelector('button');
    window._ctxWireEnableVersioning('agents', 'note', pane);
    btn.click();
    for (let i = 0; i < 50 && !toasts.length; i++) await new Promise(r => setTimeout(r, 10));
    expect(toasts).toContain(window.t('error.csrf_refreshed'));
    expect(btn.disabled).toBe(false);
  });

  it.each([
    ['SSE index', [], w => w.fetchIndexStream({ path: '/tmp/notes' })],
    ['multipart upload', [], w => w.uploadFilesWithRedactionRetry(new w.FormData())],
    ['Context transfer', ['context-gateway.js'], w => w._ctxMoveCopyPost(
      { srcType: 'skills', srcName: 'note', srcTier: 'user' }, {}, false)],
    ['Context pull', ['context-gateway.js'], w => w._ctxPullPost(
      { kind: 'skills', name: 'note', tier: 'user' }, {})],
    ['hooks rule action', ['settings-hooks-watchdog.js'], w => w._hooksPostRuleAction('promote', {}, false)],
    ['import file', ['settings-maintenance.js'], w => {
      const input = w.document.getElementById('imp-file');
      Object.defineProperty(input, 'files', { value: [new w.File(['{}'], 'import.json')] });
      return w.runImport();
    }],
    ['wiki save', ['wiki.js'], w => w._saveWikiCanonical('skills', 'note', 'draft', '1', false)],
    ['portal rename', ['context-gateway.js', 'context-portal.js'], w => w._ctxPortalSaveLabel({ scope_id: 'project' }, 'draft')],
  ])('%s recovers without replay', async (_name, scripts, invoke) => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js', ...scripts] }));
    const toasts = [];
    window.showToast = message => toasts.push(message);
    let bootstraps = 0, writes = 0;
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') return response({ csrf: ++bootstraps === 1 ? 'A' : 'B' });
      if (!opts.method || opts.method === 'GET') return response({});
      writes++;
      expect(opts.headers['X-Memtomem-CSRF']).toBe('A');
      return mismatch();
    };
    let error;
    try { await invoke(window); } catch (err) { error = err; }
    expect(writes).toBe(1);
    expect(bootstraps).toBe(2);
    if (error) expect(error.name).toBe('CsrfRecoveryError');
    else expect(toasts.some(text => text.includes(window.t('error.csrf_refreshed')))).toBe(true);
  });
});

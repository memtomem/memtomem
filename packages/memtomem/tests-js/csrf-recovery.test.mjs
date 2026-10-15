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

  it.each(['object', 'Headers', 'tuples'])('preserves %s headers and replaces stale caller tokens', async kind => {
    window.Headers = Headers;
    const pairs = [['X-Probe', 'keep'], ['x-memtomem-csrf', 'stale']];
    if (kind === 'tuples') pairs.push(['X-Probe', 'also']);
    const headers = kind === 'object' ? Object.fromEntries(pairs)
      : kind === 'Headers' ? new Headers(pairs) : pairs;
    const before = JSON.stringify(headers instanceof Headers ? [...headers] : headers);
    await window.csrfFetch('/api/x', { method: 'POST', headers });
    const sent = new Headers(calls[0].headers);
    expect(sent.get('x-probe')).toBe(kind === 'tuples' ? 'keep, also' : 'keep');
    expect(sent.get('x-memtomem-csrf')).toBe('A');
    expect(JSON.stringify(headers instanceof Headers ? [...headers] : headers)).toBe(before);
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
  it.each([
    ['first promote', 1, 0, 0],
    ['second promote', 2, 1, 0],
    ['first delete', 4, 3, 0],
    ['second delete', 5, 3, 1],
  ])('reports partial results and refreshes after refusal at %s', async (_name, refuseAt, saved, deleted) => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-hooks-watchdog.js'] }));
    window.eval("_hooksRuleRegistry = [0, 1, 2].map(i => ({ _bucket: 'target-only', event: String(i) }))");
    window._confirmHooksPromote = async () => ({ ok: true, deleteOriginal: true });
    // The batch test isolates rule matching while retaining the real write helper.
    window._deleteOriginalAfterPromote = entry => window._hooksPostRuleAction('delete', entry, false);
    const events = [], actions = [];
    window.showToast = (text, tone) => events.push({ text, tone });
    window.loadHooksSync = async () => {
      events.push('reload');
      window.eval('_hooksRuleRegistry = []');
    };
    let writes = 0;
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') return response({ csrf: 'token' });
      if (!opts.method || opts.method === 'GET') return response({});
      actions.push(url.includes('/delete') ? 'delete' : 'promote');
      return ++writes === refuseAt ? mismatch() : response({ status: 'ok' });
    };
    const btn = window.document.createElement('button');
    await window._handleHooksPromoteAll(btn);
    expect(writes).toBe(refuseAt);
    if (refuseAt <= 3) expect(actions).not.toContain('delete');
    expect(btn.disabled).toBe(false);
    expect(window.eval('_hooksRuleRegistry.length')).toBe(0);
    const expected = [];
    if (saved || deleted) expected.push({
      text: window.t('settings.hooks.promote_all_result', { saved, deleted, conflicts: 0, aborted: 0, failed: 1 })
        + (saved > deleted ? ' ' + window.t('settings.hooks.promote_all_remaining_originals', { count: saved - deleted }) : ''),
      tone: 'warning',
    });
    expected.push('reload', { text: window.t('error.csrf_refreshed'), tone: 'error' });
    expect(events).toEqual(expected);
  });

  it.each(['shared sync', 'hooks replace'])('%s displays recovery and restores UI after refusal', async kind => {
    const scripts = kind === 'shared sync' ? ['context-gateway.js'] : ['settings-hooks-watchdog.js'];
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js', ...scripts] }));
    await window.I18N.init();
    const toasts = [], writes = [];
    let reloads = 0;
    window.showConfirm = async () => true;
    window.showToast = text => toasts.push(text);
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') return response({ csrf: 'fresh' });
      if (opts.method === 'POST') { writes.push(url); return mismatch(); }
      if (url.includes('/api/settings-sync')) return response({
        status: 'conflicts', hooks: { pending: [], synced: [], conflicts: [
          { event: 'Stop', existing: { command: 'old' }, proposed: { command: 'new' } },
        ] },
      });
      return response({});
    };
    let btn;
    if (kind === 'shared sync') {
      window.eval("_ctxTargetScope = 'project_shared'");
      window._ctxSyncAllPreview = async () => ({});
      window._ctxSyncAllConfirmCopy = () => ({ message: 'Sync', warningText: '' });
      window.loadCtxOverview = () => { reloads++; };
      btn = window.document.getElementById('ctx-sync-all-btn');
      btn.disabled = false;
      btn.dataset.runtimeOnly = 'false';
    } else {
      await window.loadHooksSync();
      btn = window.document.querySelector('.hooks-resolve-btn');
    }
    expect(btn).toBeTruthy();
    btn.click();
    for (let i = 0; i < 100 && !toasts.length; i++) await new Promise(r => setTimeout(r, 10));
    expect(toasts.some(text => text.includes(window.t('error.csrf_refreshed')))).toBe(true);
    expect(writes).toHaveLength(1);
    expect(btn.disabled).toBe(false);
    if (kind === 'shared sync') {
      expect(writes[0]).toContain('/api/context/sync-all');
      expect(window.document.querySelectorAll('.ctx-sync-phase--failed')).toHaveLength(5);
      expect(window.document.querySelectorAll('.ctx-sync-phase--syncing')).toHaveLength(0);
      expect(reloads).toBe(1);
    } else {
      expect(writes[0]).toContain('/api/context/settings/resolve');
      expect(window.document.querySelector('.hooks-sync-conflict')).toBeTruthy();
    }
  });

  it.each(['csrf refusal', 'read failure'])('single promote reports saved state after deletion %s', async failure => {
    ({ window } = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-hooks-watchdog.js'] }));
    window.eval("_hooksRuleRegistry = [{event:'Stop', _bucket:'target-only'}]");
    window._confirmHooksPromote = async () => ({ok:true, deleteOriginal:true});
    const events = [], writes = [];
    window.showToast = (text, tone) => events.push({text, tone});
    window.loadHooksSync = async () => { events.push('reload'); };
    window._deleteOriginalAfterPromote = async entry => {
      if (failure === 'read failure') throw new Error('Read failed');
      return window._hooksPostRuleAction('delete', entry, false);
    };
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') return response({csrf:'fresh'});
      if (opts.method !== 'POST') return response({});
      writes.push(url);
      return url.includes('/delete') ? mismatch() : response({status:'ok'});
    };
    const btn = window.document.createElement('button');
    await window._handleHooksRuleAction('promote', '0', btn);
    expect(events).toEqual([
      {text:window.t('settings.hooks.promote_delete_partial'), tone:'warning'},
      'reload',
      {text:failure === 'csrf refusal' ? window.t('error.csrf_refreshed') : 'Read failed', tone:'error'},
    ]);
    expect(writes).toHaveLength(failure === 'csrf refusal' ? 2 : 1);
    expect(btn.disabled).toBe(false);
  });

  it('warns when import preview refreshes the token and still requires confirmation', async () => {
    ({ window } = await bootApp({scripts:['i18n.js','app.js','context-gateway.js']}));
    const toasts = [], writes = [], confirmations = [];
    window.showToast = (text, tone) => toasts.push({text, tone});
    window.showConfirm = async opts => { confirmations.push(opts); return false; };
    window.fetch = async (url, opts = {}) => {
      if (url === '/api/session') return response({csrf:'fresh'});
      if (opts.method !== 'POST') return response({});
      writes.push(url);
      return mismatch();
    };
    const btn = window.document.createElement('button');
    await window._ctxRunImport('skills', {btn, onComplete:() => { throw new Error('Unexpected import'); }});
    expect(toasts).toContainEqual({text:window.t('error.csrf_refreshed'), tone:'warning'});
    expect(confirmations).toHaveLength(1);
    expect(writes).toHaveLength(1);
    expect(writes[0]).toContain('dry_run=1');
    expect(btn.disabled).toBe(false);
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

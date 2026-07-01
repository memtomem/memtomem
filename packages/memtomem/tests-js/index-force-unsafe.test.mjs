/* ADR-0006 PR-B — Web UI `force_unsafe` override toggle + blocked-file
 * surfacing for bulk indexing.
 *
 * PR-A (#1499) put the secret-redaction gate at the engine chokepoint and
 * added `blocked_*` counts to every index response, but no frontend surface
 * forwarded `force_unsafe` or displayed the skipped-file counts. This file
 * pins PR-B:
 *
 *   Index tab (folder mode):
 *     - toggle OFF → the normal `GET /api/index/stream` SSE path, no
 *       `force_unsafe` on the URL (the GET is a token-exempt safe method, so it
 *       must never carry a redaction bypass).
 *     - toggle ON → the bypass rides the CSRF-protected `POST /api/index`
 *       (`force_unsafe:true` in the body), NOT the SSE stream. No `EventSource`
 *       is opened.
 *     - blocked files render (Blocked row + toast) from both the SSE `complete`
 *       event and the POST response; the `project_shared` block fires the
 *       cannot-bypass toast instead of the bypassable one (ADR-0011 §5).
 *
 *   Sources "+ Add path":
 *     - toggle ON → `POST /api/memory-dirs/add` body carries `force_unsafe:true`;
 *       an `indexed.blocked_files>0` response fires the blocked toast.
 *
 * jsdom ships no `EventSource`; `runIndexStream` guards the sync throw, so we
 * inject a capturing fake and drive `onmessage` by hand.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function installFakeEventSource(window) {
  const instances = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.onmessage = null;
      this.onerror = null;
      this.closed = false;
      instances.push(this);
    }
    close() { this.closed = true; }
    emit(obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); }
  }
  window.EventSource = FakeEventSource;
  return instances;
}

function spyToasts(window) {
  const toasts = [];
  window.showToast = (message, type = 'success') => { toasts.push({ message, type }); };
  return toasts;
}

async function flush(n = 8) {
  for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0));
}

function indexResult(overrides = {}) {
  return {
    type: 'complete',
    total_files: 2,
    total_chunks: 1,
    indexed_chunks: 1,
    skipped_chunks: 0,
    deleted_chunks: 0,
    duration_ms: 5,
    errors: [],
    resolved_namespaces: [],
    blocked_files: 0,
    blocked_paths: [],
    blocked_project_shared_files: 0,
    ...overrides,
  };
}

describe('Index tab — force_unsafe toggle + blocked surfacing', () => {
  let window;
  let document;
  let streams;
  let toasts;
  let capturedIndex;
  let postResult;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;
    await window.I18N.init(); // real English strings so t() != raw key

    streams = installFakeEventSource(window);
    toasts = spyToasts(window);

    // Capture the force_unsafe POST /api/index; other fetches fall through.
    capturedIndex = [];
    postResult = indexResult();
    window.__setPostResult = (r) => { postResult = r; };
    const origFetch = window.fetch;
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      if (url === '/api/index') {
        capturedIndex.push({ method: init?.method, body: init?.body ? JSON.parse(init.body) : null });
        return { ok: true, status: 200, json: async () => postResult, text: async () => '{}' };
      }
      return origFetch(input, init);
    };

    // Bypass the concurrency preflight (a global function → bare call resolves).
    window._indexingTryStartOrRefresh = async () => true;
    // Sibling-module helpers not loaded in this minimal boot.
    window.makeChunkProgressRenderer = () => ({ onChunk: () => false, onProgressBoundary: () => {} });
    window.loadNamespaceDropdowns = () => {};
    window.loadSourceFilter = () => {};
    window.loadStats = () => {};

    document.getElementById('index-path').value = '/tmp/memories';
  });

  function clickIndex() {
    document.getElementById('index-btn').dispatchEvent(new window.Event('click'));
    return flush();
  }

  it('toggle OFF → GET SSE stream, no force_unsafe on the URL, no POST', async () => {
    document.getElementById('index-force-unsafe').checked = false;
    await clickIndex();
    expect(streams).toHaveLength(1);
    expect(streams[0].url).toContain('/api/index/stream?');
    expect(streams[0].url).not.toContain('force_unsafe');
    expect(capturedIndex).toHaveLength(0);
  });

  it('toggle ON → CSRF-protected POST /api/index with force_unsafe:true, no EventSource', async () => {
    document.getElementById('index-force-unsafe').checked = true;
    await clickIndex();
    expect(streams).toHaveLength(0); // the token-exempt GET stream is not used
    expect(capturedIndex).toHaveLength(1);
    expect(capturedIndex[0].method).toBe('POST');
    expect(capturedIndex[0].body.force_unsafe).toBe(true);
    expect(capturedIndex[0].body.path).toBe('/tmp/memories');
  });

  it('SSE complete with blocked_files>0 renders the Blocked row + bypassable toast', async () => {
    document.getElementById('index-force-unsafe').checked = false;
    await clickIndex();
    streams[0].emit(indexResult({
      blocked_files: 1,
      blocked_paths: ['/tmp/memories/leak.md'],
      blocked_project_shared_files: 0,
    }));
    await flush(1);

    expect(document.getElementById('r-blocked-row').hidden).toBe(false);
    const cell = document.getElementById('r-blocked').textContent;
    expect(cell).toContain('1');
    expect(cell).toContain('leak.md');

    const msgs = toasts.filter((t) => t.type === 'error').map((t) => t.message);
    expect(msgs).toContain(window.t('toast.index_blocked', { count: 1 }));
    expect(msgs).not.toContain(window.t('toast.index_blocked_project_shared', { count: 1 }));
  });

  it('project_shared block fires the cannot-bypass toast, not the bypassable one', async () => {
    document.getElementById('index-force-unsafe').checked = false;
    await clickIndex();
    streams[0].emit(indexResult({
      blocked_files: 1,
      blocked_paths: ['/tmp/memories/shared.md'],
      blocked_project_shared_files: 1,
    }));
    await flush(1);

    const msgs = toasts.filter((t) => t.type === 'error').map((t) => t.message);
    expect(msgs).toContain(window.t('toast.index_blocked_project_shared', { count: 1 }));
    expect(msgs).not.toContain(window.t('toast.index_blocked', { count: 1 }));
  });

  it('force_unsafe POST response with blocked_files>0 also renders the Blocked row + toast', async () => {
    window.__setPostResult(indexResult({
      blocked_files: 2,
      blocked_paths: ['/tmp/memories/a.md', '/tmp/memories/b.md'],
      blocked_project_shared_files: 0,
    }));
    document.getElementById('index-force-unsafe').checked = true;
    await clickIndex();

    expect(capturedIndex).toHaveLength(1);
    expect(document.getElementById('r-blocked-row').hidden).toBe(false);
    const msgs = toasts.filter((t) => t.type === 'error').map((t) => t.message);
    expect(msgs).toContain(window.t('toast.index_blocked', { count: 2 }));
  });

  it('clean SSE run keeps the Blocked row hidden and fires no blocked toast', async () => {
    document.getElementById('index-force-unsafe').checked = false;
    await clickIndex();
    streams[0].emit(indexResult());
    await flush(1);

    expect(document.getElementById('r-blocked-row').hidden).toBe(true);
    const msgs = toasts.map((t) => t.message);
    expect(msgs).not.toContain(window.t('toast.index_blocked', { count: 1 }));
  });
});

describe('Sources "+ Add path" — force_unsafe body + blocked surfacing', () => {
  let window;
  let document;
  let captured;
  let toasts;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'sources-memory-dirs.js'] });
    window = dom.window;
    document = window.document;
    await window.I18N.init();

    captured = [];
    const original = window.fetch;
    let indexedPayload = { indexed_chunks: 1, total_files: 2, blocked_files: 0, blocked_project_shared_files: 0 };
    window.__setIndexed = (p) => { indexedPayload = p; };
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      if (url === '/api/memory-dirs/add') {
        captured.push({ method: init?.method, body: init?.body ? JSON.parse(init.body) : null });
        return {
          ok: true,
          status: 200,
          json: async () => ({ ok: true, memory_dirs: ['/tmp/memories'], indexed: indexedPayload }),
          text: async () => '{}',
        };
      }
      return original(input, init);
    };
    toasts = spyToasts(window);
    window.loadSources = () => {}; // don't render against the empty stub
  });

  function submitAdd() {
    document.getElementById('memory-add-input').value = '/tmp/memories';
    document.getElementById('memory-add-submit').dispatchEvent(new window.Event('click'));
    return flush(10);
  }

  it('toggle OFF → POST body has no force_unsafe', async () => {
    document.getElementById('memory-add-force-unsafe').checked = false;
    await submitAdd();
    expect(captured).toHaveLength(1);
    expect(captured[0].method).toBe('POST');
    expect(captured[0].body.path).toBe('/tmp/memories');
    expect(captured[0].body.force_unsafe).toBeUndefined();
  });

  it('toggle ON → POST body carries force_unsafe:true', async () => {
    document.getElementById('memory-add-force-unsafe').checked = true;
    await submitAdd();
    expect(captured).toHaveLength(1);
    expect(captured[0].body.force_unsafe).toBe(true);
  });

  it('indexed.blocked_files>0 fires the blocked toast', async () => {
    window.__setIndexed({ indexed_chunks: 1, total_files: 2, blocked_files: 2, blocked_project_shared_files: 0 });
    document.getElementById('memory-add-force-unsafe').checked = false;
    await submitAdd();
    const msgs = toasts.filter((t) => t.type === 'error').map((t) => t.message);
    expect(msgs).toContain(window.t('toast.index_blocked', { count: 2 }));
  });
});

/* Regression guards for Timeline -> Source chunk activation.
 *
 * Issue #680: a target chunk beyond the first fetch used to clear
 * ``STATE.pendingActivateChunkId`` on the initial miss, so a later Load All
 * / pagination fetch had nothing left to retry.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function chunk(id) {
  return {
    id,
    chunk_type: 'paragraph',
    start_line: 1,
    end_line: 2,
    heading_hierarchy: [],
    content: `content for ${id}`,
  };
}

async function nextFrame(window) {
  await new Promise(resolve => window.requestAnimationFrame(resolve));
}

describe('browseSource pending chunk activation retry', () => {
  let window;
  let document;
  let toasts;
  let requests;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;
    toasts = [];
    requests = [];

    window.CSS = window.CSS || {};
    window.CSS.escape = window.CSS.escape || (value => String(value).replace(/"/g, '\\"'));
    window.Element.prototype.scrollIntoView = function scrollIntoViewSpy() {
      this.dataset.scrolled = 'true';
    };
    window.showToast = (msg, level) => {
      toasts.push({ msg, level });
    };
  });

  function stubChunksByLimit(responsesByLimit) {
    const originalFetch = window.fetch;
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input?.url;
      if (url && url.startsWith('/api/chunks')) {
        const parsed = new URL(url, 'http://localhost');
        const limit = Number(parsed.searchParams.get('limit'));
        requests.push({ source: parsed.searchParams.get('source'), limit });
        const chunks = responsesByLimit[limit] || [];
        return {
          ok: true,
          status: 200,
          json: async () => ({ chunks, total: Math.max(501, chunks.length) }),
          text: async () => '{}',
        };
      }
      return originalFetch(input, init);
    };
  }

  it('keeps the pending chunk id after a same-source miss and clears it after retry success', async () => {
    const sourcePath = '/notes/large.md';
    const targetId = 'target-501';
    stubChunksByLimit({
      100: [chunk('first-visible')],
      500: [chunk('first-visible'), chunk(targetId)],
    });
    Object.assign(window.STATE, {
      pendingActivateChunkId: targetId,
      pendingActivateChunkSourcePath: sourcePath,
    });

    await window.browseSource(sourcePath, 100);
    await nextFrame(window);

    expect(window.STATE.pendingActivateChunkId).toBe(targetId);
    expect(window.STATE.pendingActivateChunkSourcePath).toBe(sourcePath);
    expect(toasts.some(toast => toast.level === 'info')).toBe(true);

    await window.browseSource(sourcePath, 500);
    await nextFrame(window);

    const card = document.querySelector(`.chunk-card[data-chunk-id="${targetId}"]`);
    expect(card).not.toBeNull();
    expect(card?.dataset.scrolled).toBe('true');
    expect(card?.classList.contains('tl-target-flash')).toBe(true);
    expect(window.STATE.pendingActivateChunkId).toBe('');
    expect(window.STATE.pendingActivateChunkSourcePath).toBe('');
    expect(requests.map(req => req.limit)).toEqual([100, 500]);
  });

  it('does not consume a pending chunk target while browsing another source', async () => {
    stubChunksByLimit({ 100: [chunk('target-501')] });
    Object.assign(window.STATE, {
      pendingActivateChunkId: 'target-501',
      pendingActivateChunkSourcePath: '/notes/large.md',
    });

    await window.browseSource('/notes/other.md', 100);
    await nextFrame(window);

    expect(window.STATE.pendingActivateChunkId).toBe('target-501');
    expect(window.STATE.pendingActivateChunkSourcePath).toBe('/notes/large.md');
    expect(toasts).toEqual([]);
  });

  it('manual source item click clears a stale pending chunk target', () => {
    Object.assign(window.STATE, {
      pendingActivateChunkId: 'target-501',
      pendingActivateChunkSourcePath: '/notes/large.md',
    });
    stubChunksByLimit({ 100: [chunk('other')] });

    const item = window._renderMemorySourceItem(
      { path: '/notes/other.md', chunk_count: 1, namespaces: [] },
      1,
    );
    document.body.appendChild(item);

    item.click();

    expect(window.STATE.pendingActivateChunkId).toBe('');
    expect(window.STATE.pendingActivateChunkSourcePath).toBe('');
  });
});

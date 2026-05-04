/* Regression guards for ``_renderMemorySourceTree`` in ``app.js``.
 * Both tests scope the recent #639 review:
 *   1. orphan rows (sources with ``memory_dir = null``) must surface
 *      under the User vendor as a ``.source-vendor-orphan`` block, and
 *      the user sub-tab badge must count them toward ``totalFiles``.
 *   2. when the User vendor has zero indexed dirs but does have
 *      orphans, the empty-state placeholder must be suppressed (the
 *      orphan block is real content) and the orphan block still
 *      renders.
 */

import { beforeEach, describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('_renderMemorySourceTree — orphan rendering', () => {
  let window;
  let document;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;
    // The Sources sub-tab strip is rendered by the Python template into
    // ``index.html`` only inside the ``#sources`` panel, but the live
    // production tree relies on ``[data-vendor-count]`` and
    // ``.sources-vendor-tab`` lookups. Inject them so the badge-update
    // pass in ``_renderMemorySourceTree`` has something to write to —
    // without these the function still returns successfully but the
    // "badge counts orphans" half of the test has nothing to assert.
    document.body.insertAdjacentHTML('beforeend', `
      <div id="sources-vendor-tabs">
        <button class="sources-vendor-tab" data-vendor="user">
          <span data-vendor-count="user"></span>
        </button>
        <button class="sources-vendor-tab" data-vendor="claude">
          <span data-vendor-count="claude"></span>
        </button>
        <button class="sources-vendor-tab" data-vendor="openai">
          <span data-vendor-count="openai"></span>
        </button>
      </div>
    `);
  });

  it('renders .source-vendor-orphan block with indexed + orphan mix', () => {
    const dir = '/home/user/.memtomem/memory';
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'user',
      memoryDirs: [dir],
      memoryStatusByPath: {
        [dir]: {
          provider: 'user',
          category: 'user',
          exists: true,
          chunk_count: 5,
          file_count: 1,
          source_file_count: 1,
        },
      },
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);

    const sources = [
      { memory_dir: dir, path: 'note.md', chunk_count: 5 },
      { memory_dir: null, path: 'upload.md', chunk_count: 2 },
    ];

    window._renderMemorySourceTree(sources, list);

    const orphanBlock = list.querySelector('.source-vendor-orphan');
    expect(orphanBlock).not.toBeNull();
    const orphanCount = orphanBlock.querySelector('.source-vendor-count');
    expect(orphanCount?.textContent).toBe('1');

    const userBadge = document.querySelector('[data-vendor-count="user"]');
    // 1 indexed file + 1 orphan = 2.
    expect(userBadge?.textContent).toBe('2');
    expect(userBadge?.hidden).toBe(false);
  });

  it('suppresses empty-state placeholder when only orphans exist under User', () => {
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'user',
      memoryDirs: [],
      memoryStatusByPath: {},
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);

    const sources = [
      { memory_dir: null, path: 'upload-a.md', chunk_count: 1 },
      { memory_dir: null, path: 'upload-b.md', chunk_count: 3 },
    ];

    window._renderMemorySourceTree(sources, list);

    expect(list.querySelector('.source-vendor-placeholder')).toBeNull();
    const orphanBlock = list.querySelector('.source-vendor-orphan');
    expect(orphanBlock).not.toBeNull();
    expect(orphanBlock.querySelector('.source-vendor-count')?.textContent).toBe('2');

    const userBadge = document.querySelector('[data-vendor-count="user"]');
    expect(userBadge?.textContent).toBe('2');
  });

  it('orphan rows do not appear under non-User vendors', () => {
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      memoryDirs: [],
      memoryStatusByPath: {},
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);

    const sources = [{ memory_dir: null, path: 'upload.md', chunk_count: 1 }];

    window._renderMemorySourceTree(sources, list);

    expect(list.querySelector('.source-vendor-orphan')).toBeNull();
    const claudeBadge = document.querySelector('[data-vendor-count="claude"]');
    // No indexed dirs and orphans don't count toward Claude → badge is 0
    // and the ``hidden`` flag suppresses the "0" rendering.
    expect(claudeBadge?.textContent).toBe('0');
    expect(claudeBadge?.hidden).toBe(true);
  });
});

/* #2538: the memory-dir remove dialog, driven through both remove buttons.
 *
 * Both handlers build the dialog with ``memoryDirRemoveConfirmFresh``, which
 * refetches ``GET /api/memory-dirs/status`` and passes the fresh row to
 * ``memoryDirRemoveConfirmOptions`` (#2536, #2561):
 *
 *   - the Sources tree (``app.js: _renderMemoryDirGroup``): ``.source-group-remove``
 *     calls the top-level ``mdRemove``;
 *   - the legacy Memory Dirs panel (``_buildMemoryDirsPanel``):
 *     ``.memory-dirs-remove-btn`` calls its closure ``handleRemove``.
 *
 * Each entry point starts with a stale cached row (7 chunks, 7 deletable) and
 * the click's refetch serves the case's fresh row, so a handler that built its
 * options from the cache, or inline from ``chunk_count`` as before #2536, shows
 * a different dialog. The ``showConfirm`` stub resolves only when the test says
 * so, keyed the way the real dialog keys ``extras`` (``extras[extraOption.id]``).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const DIR = '/home/user/.memtomem/memories';
const OTHER_DIR = '/home/user/notes';
const COVERED_KEY = 'confirm.memory_dir_chunks_still_covered';
const LABEL_KEY = 'confirm.memory_dir_delete_chunks_label';

function statusRow(overrides) {
  return {
    path: DIR,
    provider: 'user',
    category: 'user',
    tier: 'user',
    exists: true,
    file_count: 2,
    source_file_count: 2,
    chunk_count: 7,
    delete_chunk_count: 7,
    ...overrides,
  };
}

const CACHED_ROW = statusRow({});

async function waitFor(predicate, what) {
  for (let i = 0; i < 50; i += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

async function boot(freshRow) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'sources-memory-dirs.js'] });
  const { window } = dom;
  await window.I18N.init();

  const posts = [];
  const confirms = [];
  let clicked = false;
  window.api = async (method, path, body) => {
    if (method === 'GET' && path === '/api/memory-dirs/status') {
      const row = clicked ? freshRow : CACHED_ROW;
      return { dirs: [row, statusRow({ path: OTHER_DIR })] };
    }
    if (method === 'POST' && path === '/api/memory-dirs/remove') {
      posts.push(body);
      return { memory_dirs: [OTHER_DIR], deleted_chunks: body.delete_chunks ? 3 : 0 };
    }
    return {};
  };
  window.showConfirm = (opts) => new Promise((resolve) => {
    confirms.push({ opts, resolve });
  });
  window.showToast = () => {};
  window.loadSources = () => {};
  window.loadStats = () => {};
  window.hideBrowser = () => {};

  return {
    window,
    posts,
    confirms,
    markClicked() { clicked = true; },
  };
}

const ENTRY_POINTS = [
  {
    name: 'Sources tree (.source-group-remove → mdRemove)',
    async mount(env) {
      const { window } = env;
      window.STATE.memoryDirs = [DIR, OTHER_DIR];
      window.STATE.memoryStatusByPath = { [DIR]: CACHED_ROW };
      const group = window._renderMemoryDirGroup(DIR, [], CACHED_ROW, 7, { isDefault: false });
      window.document.body.appendChild(group);
      const btn = group.querySelector('.source-group-remove');
      expect(btn, 'remove button rendered for a user-tier root').not.toBeNull();
      expect(btn.disabled).toBe(false);
      return btn;
    },
  },
  {
    name: 'legacy panel (.memory-dirs-remove-btn → handleRemove)',
    async mount(env) {
      const { window } = env;
      const panel = window._buildMemoryDirsPanel([DIR, OTHER_DIR]);
      window.document.body.appendChild(panel);
      const rowFor = () => [...panel.querySelectorAll('.memory-dirs-item')]
        .find((li) => li.textContent.includes(DIR));
      // The panel's own status fetch fills its private cache; its row then
      // shows the cached 7 chunks.
      await waitFor(() => {
        const li = rowFor();
        const meta = li && li.querySelector('.memory-dirs-item-meta');
        return meta && /\b7\b/.test(meta.textContent);
      }, 'the panel row to show the cached chunk count');
      const btn = rowFor().querySelector('.memory-dirs-remove-btn');
      expect(btn.disabled).toBe(false);
      return btn;
    },
  },
];

async function clickRemove(env, entry) {
  const btn = await entry.mount(env);
  env.markClicked();
  btn.click();
  await waitFor(() => env.confirms.length > 0, 'showConfirm to be called');
  expect(env.confirms).toHaveLength(1);
  // Nothing is removed while the dialog is still open.
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(env.posts).toHaveLength(0);
  return env.confirms[0];
}

describe.each(ENTRY_POINTS)('memory-dir remove dialog — $name', (entry) => {
  it('a dir whose chunks another root still covers: no checkbox, the covered warning', async () => {
    const env = await boot(statusRow({ delete_chunk_count: 0 }));
    const { opts, resolve } = await clickRemove(env, entry);

    expect(opts.extraOption).toBeNull();
    const warning = env.window.t(COVERED_KEY);
    expect(warning).not.toBe('');
    expect(warning).not.toBe(COVERED_KEY);
    expect(opts.warningText).toBe(warning);

    resolve(true);
    await waitFor(() => env.posts.length > 0, 'the remove POST');
    expect(env.posts).toEqual([{ path: DIR, delete_chunks: false }]);
  });

  it('a dir with deletable chunks: the checkbox counts them, ticking it deletes', async () => {
    const env = await boot(statusRow({ delete_chunk_count: 3 }));
    const { opts, resolve } = await clickRemove(env, entry);

    expect(opts.extraOption).not.toBeNull();
    expect(opts.extraOption.id).toBe('deleteChunks');
    expect(opts.extraOption.label).toBe(env.window.t(LABEL_KEY, { count: 3 }));
    expect(opts.extraOption.label).toContain('3');
    expect(opts.extraOption.label).not.toContain('7');
    expect(opts.warningText).toBe('');

    resolve({ ok: true, extras: { [opts.extraOption.id]: true } });
    await waitFor(() => env.posts.length > 0, 'the remove POST');
    expect(env.posts).toEqual([{ path: DIR, delete_chunks: true }]);
  });

  it('a dir with deletable chunks, box left unticked: keeps the chunks', async () => {
    const env = await boot(statusRow({ delete_chunk_count: 3 }));
    const { opts, resolve } = await clickRemove(env, entry);

    expect(opts.extraOption.defaultChecked).toBe(false);

    resolve({ ok: true, extras: { [opts.extraOption.id]: false } });
    await waitFor(() => env.posts.length > 0, 'the remove POST');
    expect(env.posts).toEqual([{ path: DIR, delete_chunks: false }]);
  });
});

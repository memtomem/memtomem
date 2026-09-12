/* Config panel banner: load warnings, precedence, and the auto-hide timer.
 *
 * ``_renderReloadBanner`` now has three states (#2385 item 3):
 *
 *   err  — a re-read of config.json failed; the runtime kept its old config
 *   warn — the running config is missing a section the file was rejected for
 *   info — the file changed externally and was reloaded (auto-hides after 5s)
 *
 * The info branch arms a 5-second hide. Before this change that handle was
 * dropped on the floor, so a timer armed by an info render fired five seconds
 * later and hid whatever banner had replaced it in the meantime. The timer
 * test below drives that exact sequence with a controllable scheduler: with
 * the ``clearTimeout`` removed, the pending callback survives the second
 * render and hides a live warning.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCRIPTS = ['i18n.js', 'app.js', 'settings-config.js'];

const REJECTED_WARNING = {
  section: 'embedding',
  path: '/home/u/.memtomem/config.json',
  error: 'multilingual-e5-small requires dimension=384',
  fix: 'fix [embedding] in /home/u/.memtomem/config.json',
};

function configPayload(extra = {}) {
  return {
    embedding: { provider: 'none', model: '', dimension: 0 },
    search: { default_top_k: 10 },
    config_mtime_ns: 1000,
    config_reload_error: null,
    config_load_warnings: [],
    ...extra,
  };
}

// Node's own timer, so flushing still works while the window's scheduler is
// swapped for the controllable one below.
const flush = async (times = 12) => {
  for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
};

async function boot(payload) {
  const dom = await bootApp({
    scripts: SCRIPTS,
    apiResponses: { '/api/config': payload, '/api/config/defaults': payload },
  });
  await flush();
  return dom;
}

/** Swap the window scheduler for one the test can inspect and fire by hand. */
function installControllableTimers(window) {
  const pending = new Map();
  let nextId = 1;
  const realSetTimeout = window.setTimeout;
  const realClearTimeout = window.clearTimeout;
  window.setTimeout = (fn, ms) => {
    const id = nextId++;
    pending.set(id, fn);
    return id;
  };
  window.clearTimeout = (id) => {
    pending.delete(id);
  };
  return {
    pending,
    /** Ids currently scheduled — snapshot before/after to isolate a render. */
    ids() {
      return new Set(pending.keys());
    },
    /** Fire only the given ids that are still scheduled. */
    run(ids) {
      for (const id of ids) {
        const fn = pending.get(id);
        if (fn === undefined) continue;
        pending.delete(id);
        fn();
      }
    },
    restore() {
      window.setTimeout = realSetTimeout;
      window.clearTimeout = realClearTimeout;
    },
  };
}

describe('config panel banner — rejected sections (#2385)', () => {

  it('renders a warn banner naming the section and the reason', async () => {
    const dom = await boot(configPayload({ config_load_warnings: [REJECTED_WARNING] }));
    const { window } = dom;

    await window.loadConfig();
    await flush();

    const el = window.document.getElementById('config-reload-banner');
    expect(el.hidden).toBe(false);
    expect(el.className).toContain('warn');
    expect(el.textContent).toContain('embedding');
    expect(el.textContent).toContain('dimension=384');
    // Localized copy, not the raw key.
    expect(el.textContent).not.toContain('settings.config.load_rejected');
  });

  it('shows no banner when nothing was rejected', async () => {
    const dom = await boot(configPayload());
    const { window } = dom;

    await window.loadConfig();
    await flush();

    expect(window.document.getElementById('config-reload-banner').hidden).toBe(true);
  });

  it('gives a failed re-read precedence over a load warning', async () => {
    const dom = await boot(
      configPayload({
        config_reload_error: 'ConfigError: Invalid config section [embedding]',
        config_load_warnings: [REJECTED_WARNING],
      }),
    );
    const { window } = dom;

    await window.loadConfig();
    await flush();

    const el = window.document.getElementById('config-reload-banner');
    expect(el.className).toContain('err');
    expect(el.className).not.toContain('warn');
  });

  it("does not let an external-change timer hide a warning rendered after it", async () => {
    // First render: same mtime as boot, so no info banner yet.
    const dom = await boot(configPayload());
    const { window } = dom;
    await window.loadConfig();
    await flush();

    const el = window.document.getElementById('config-reload-banner');
    const timers = installControllableTimers(window);
    try {
      // Second render: mtime moved → info banner + a 5s auto-hide.
      window.STATE.serverConfig = configPayload({ config_mtime_ns: 2000 });
      window.fetch = async () => ({
        ok: true,
        status: 200,
        json: async () => configPayload({ config_mtime_ns: 2000 }),
        text: async () => '{}',
      });
      await window.loadConfig();
      await flush();
      expect(el.className).toContain('info');
      // Everything this render armed, the auto-hide among it. Other panel
      // timers land here too, which is why the assertion below is about the
      // banner's state and not about how many were cancelled.
      const armedByTheInfoRender = timers.ids();
      expect(armedByTheInfoRender.size).toBeGreaterThan(0);

      // Third render: the file is now rejected, so a warning replaces it.
      window.fetch = async () => ({
        ok: true,
        status: 200,
        json: async () =>
          configPayload({ config_mtime_ns: 3000, config_load_warnings: [REJECTED_WARNING] }),
        text: async () => '{}',
      });
      await window.loadConfig();
      await flush();
      expect(el.className).toContain('warn');

      // Fire whatever the info render armed and is still scheduled. With the
      // cancellation removed, its auto-hide is in there and hides the warning.
      timers.run(armedByTheInfoRender);

      expect(el.hidden).toBe(false);
      expect(el.className).toContain('warn');
    } finally {
      timers.restore();
    }
  });
});

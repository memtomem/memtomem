/* The reindex banner's "Rebuild FTS" button reports a tokenizer fallback.
 *
 * ``POST /api/fts-rebuild`` returns ``warning`` when the rebuild did not use
 * the configured tokenizer (a kiwipiepy rebuild without the extra runs as
 * unicode61), and its ``message`` then already carries that warning. The toast
 * for that response has to be a warning, not the success toast a real Kiwi
 * rebuild gets.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCRIPTS = ['i18n.js', 'app.js', 'settings-config.js'];

const flush = async (times = 12) => {
  for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
};

async function clickRebuild(rebuildResponse) {
  const dom = await bootApp({
    scripts: SCRIPTS,
    apiResponses: { '/api/fts-rebuild': rebuildResponse },
  });
  await flush();
  const { window } = dom;
  window._showReindexWarning([{ field: 'search.tokenizer', old_value: 'unicode61', new_value: 'kiwipiepy' }]);
  window.document.getElementById('cfg-fts-rebuild-btn').click();
  await flush();
  return window.document.querySelectorAll('#toast-container .toast');
}

async function saveTokenizer(ftsRebuild) {
  const config = {
    embedding: { provider: 'none', model: '', dimension: 0 },
    search: {
      default_top_k: 10,
      bm25_candidates: 10,
      dense_candidates: 10,
      rrf_k: 60,
      enable_bm25: true,
      enable_dense: true,
      tokenizer: 'unicode61',
      rrf_weights: [0.5, 0.5],
    },
    config_mtime_ns: 1000,
    config_reload_error: null,
    config_load_warnings: [],
  };
  const dom = await bootApp({ scripts: SCRIPTS, apiResponses: { '/api/config': config } });
  await flush();
  const { window } = dom;
  await window.loadConfig();
  await flush();

  const originalFetch = window.fetch;
  window.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input?.url;
    if (url?.startsWith('/api/config') && init.method === 'PATCH') {
      const body = {
        applied: [{ field: 'search.tokenizer', old_value: 'unicode61', new_value: 'kiwipiepy' }],
        rejected: [],
        fts_rebuild: ftsRebuild,
      };
      return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      };
    }
    return originalFetch(input, init);
  };

  const tokenizer = window.document.querySelector(
    'select[data-section="search"][data-key="tokenizer"]',
  );
  tokenizer.value = 'kiwipiepy';
  tokenizer.dispatchEvent(new window.Event('change', { bubbles: true }));
  window.document.querySelector('.config-save-btn[data-section="search"]').click();
  await flush(30);
  return window.document.querySelectorAll('#toast-container .toast');
}

describe('FTS rebuild toast', () => {
  it('is a warning when the rebuild fell back', async () => {
    const toasts = await clickRebuild({
      ok: true,
      rebuilt_rows: 2,
      tokenizer: 'unicode61',
      message: 'FTS index rebuilt with unicode61 (2 chunks). kiwipiepy is unavailable.',
      warning: 'kiwipiepy is unavailable.',
    });
    expect(toasts.length).toBe(1);
    expect(toasts[0].classList.contains('toast-warning')).toBe(true);
    expect(toasts[0].querySelector('.toast-msg').textContent).toBe(
      'FTS index rebuilt with unicode61 (2 chunks). kiwipiepy is unavailable.',
    );
  });

  it('is a success toast when the rebuild used the configured tokenizer', async () => {
    const toasts = await clickRebuild({
      ok: true,
      rebuilt_rows: 2,
      tokenizer: 'kiwipiepy',
      message: 'FTS index rebuilt with kiwipiepy (2 chunks).',
      warning: null,
    });
    expect(toasts.length).toBe(1);
    expect(toasts[0].classList.contains('toast-success')).toBe(true);
    expect(toasts[0].querySelector('.toast-msg').textContent).toBe(
      'FTS index rebuilt with kiwipiepy (2 chunks).',
    );
  });

  it('shows the server warning when saving a tokenizer change falls back', async () => {
    const warning = 'kiwipiepy is unavailable; the index was rebuilt with unicode61.';
    const toasts = await saveTokenizer({
      tokenizer: 'unicode61',
      rebuilt_rows: 2,
      warning,
    });
    const warningToast = Array.from(toasts).find((toast) =>
      toast.classList.contains('toast-warning')
      && toast.querySelector('.toast-msg').textContent === warning,
    );
    expect(warningToast).toBeDefined();
  });
});

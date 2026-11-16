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
});

import { describe, expect, it } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

// The Simple-mode toggle label and the active-store chip are written with
// ``t()`` from ``_ctxApplySimpleMode``, which first runs at script-eval time —
// BEFORE ``I18N.init()``'s locale fetch resolves — so their first paint is the
// raw-key fallback. Neither element carries ``data-i18n`` (the label is
// state-dependent), so ``applyDOM`` cannot repair them; the ``langchange``
// listener registered next to the load-time call must. Regression: PR #1704
// review — without the listener the default Context Gateway header showed
// ``settings.ctx.open_advanced`` / ``settings.ctx.active_store_chip`` verbatim.
describe('Simple-mode toggle/chip relocalization', () => {
  it('replaces the raw-key first paint once the locale cache loads', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway-core.js'] });
    // ``init`` dispatches ``langchange`` after populating the cache — the
    // listener re-renders both elements from real translations.
    await dom.window.I18N.init();

    const toggle = dom.window.document.getElementById('ctx-mode-toggle');
    const chip = dom.window.document.getElementById('ctx-simple-active-chip');
    // Simple is the default-when-unset, so the action label is "Open Advanced".
    expect(toggle.textContent).toBe(dom.window.t('settings.ctx.open_advanced'));
    expect(toggle.textContent).not.toContain('settings.ctx.');
    expect(chip.textContent).not.toContain('settings.ctx.');
    expect(chip.textContent).toContain(dom.window.t('settings.ctx.server_cwd'));
  });

  it('re-translates both elements on a locale flip', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway-core.js'] });
    await dom.window.I18N.init();
    await dom.window.I18N.setLang('ko');

    const toggle = dom.window.document.getElementById('ctx-mode-toggle');
    const chip = dom.window.document.getElementById('ctx-simple-active-chip');
    expect(toggle.textContent).toBe('고급 보기 열기');
    expect(chip.textContent).toContain('활성:');
  });
});

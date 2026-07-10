import { describe, expect, it, vi } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-harness.js'] });
  await dom.window.I18N.init();
  return dom;
}

describe('shared dev page state', () => {
  it('shows a localized summary and Retry without technical detail in prod', async () => {
    const { window } = await boot();
    const container = window.document.getElementById('sessions-list');
    const retry = vi.fn();
    window.STATE.uiMode = 'prod';
    window.renderPageState(container, {
      kind: 'error', message: window.t('settings.sessions.load_failed'), detail: 'secret path', retry,
    });

    expect(container.getAttribute('role')).toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeTruthy();
    expect(container.querySelector('.page-state-details')).toBeNull();
    container.querySelector('.page-state-retry').click();
    expect(retry).toHaveBeenCalledOnce();
  });

  it('reveals sanitized technical detail only in dev', async () => {
    const { window } = await boot();
    const container = window.document.getElementById('health-report');
    window.STATE.uiMode = 'dev';
    window.renderPageState(container, {
      kind: 'error', message: window.t('settings.health.load_failed'), detail: '<img src=x>', retry: () => {},
    });

    expect(container.querySelector('.page-state-details pre').textContent).toBe('<img src=x>');
    expect(container.querySelector('img')).toBeNull();
  });
});

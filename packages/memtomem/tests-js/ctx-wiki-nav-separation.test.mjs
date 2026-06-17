import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCRIPTS = ['i18n.js', 'app.js', 'context-gateway.js'];

describe('Context Gateway Wiki nav placement', () => {
  it('keeps the host-global Wiki entry separated at the end of the Gateway nav', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    const nav = window.document.querySelector('#tab-context-gateway .settings-nav');
    const buttons = [...nav.querySelectorAll('.settings-nav-btn[data-section]')];
    const sections = buttons.map((btn) => btn.dataset.section);

    expect(sections.at(-2)).toBe('hooks-sync');
    expect(sections.at(-1)).toBe('ctx-wiki');

    const wikiBtn = nav.querySelector('.settings-nav-btn[data-section="ctx-wiki"]');
    const divider = wikiBtn.previousElementSibling;
    expect(divider.classList.contains('settings-nav-divider')).toBe(true);
    expect(divider.dataset.group).toBe('integrations');
    expect(divider.getAttribute('aria-hidden')).toBe('true');
  });

  it('hides the Wiki separator when the Gateway nav group is collapsed', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    const group = window.document.querySelector(
      '#tab-context-gateway .settings-nav-group[data-group="integrations"]',
    );
    const divider = window.document.querySelector(
      '#tab-context-gateway .settings-nav-divider[data-group="integrations"]',
    );

    group.click();

    expect(group.getAttribute('aria-expanded')).toBe('false');
    expect(divider.classList.contains('collapsed-member')).toBe(true);
  });
});

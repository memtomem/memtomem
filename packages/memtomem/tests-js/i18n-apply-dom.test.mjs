/* Regression guard for ``feedback_data_i18n_placeholder_clobber.md`` /
 * PR #595. ``I18N.applyDOM`` must refresh ``data-i18n-placeholder``
 * attributes on every language change so JS-owned dynamic strings
 * don't reset to the static ``placeholder=""`` value baked into HTML.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('I18N.applyDOM — data-i18n-placeholder', () => {
  it('refreshes placeholder text on setLang', async () => {
    const dom = await bootApp({ scripts: ['i18n.js'] });
    const { window } = dom;
    const { document, I18N } = window;

    const input = document.createElement('input');
    input.setAttribute('data-i18n-placeholder', 'sources.filter_placeholder');
    input.placeholder = 'static-default';
    document.body.appendChild(input);

    await I18N.init();
    const englishPlaceholder = input.placeholder;
    expect(englishPlaceholder).not.toBe('sources.filter_placeholder');
    expect(englishPlaceholder).not.toBe('static-default');

    await I18N.setLang('ko');
    const koreanPlaceholder = input.placeholder;
    expect(koreanPlaceholder).not.toBe('sources.filter_placeholder');
    expect(koreanPlaceholder).not.toBe(englishPlaceholder);

    await I18N.setLang('en');
    expect(input.placeholder).toBe(englishPlaceholder);
  });

  it('refreshes data-i18n textContent on setLang', async () => {
    const dom = await bootApp({ scripts: ['i18n.js'] });
    const { window } = dom;
    const { document, I18N } = window;

    const span = document.createElement('span');
    span.setAttribute('data-i18n', 'nav.search');
    document.body.appendChild(span);

    await I18N.init();
    const en = span.textContent;
    await I18N.setLang('ko');
    const ko = span.textContent;
    expect(ko).not.toBe(en);
    expect(ko).not.toBe('nav.search');
  });
});

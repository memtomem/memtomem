import { describe, expect, it } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('Import provenance receipt', () => {
  it('renders the selected runtime and all duplicate candidates', async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway-core.js'] });
    await dom.window.I18N.init();
    const html = dom.window.renderImportResult({
      imported: [{
        name: 'shared',
        source_runtime: 'claude',
        duplicate_candidates: ['claude', 'gemini', 'codex'],
      }],
      skipped: [],
    });
    const host = dom.window.document.createElement('div');
    host.innerHTML = html;

    expect(host.querySelector('.ctx-import-source').textContent).toContain('claude');
    expect(host.querySelector('.ctx-import-duplicates').textContent).toContain('gemini');
    expect(host.querySelector('.ctx-import-duplicates').textContent).toContain('codex');
  });
});

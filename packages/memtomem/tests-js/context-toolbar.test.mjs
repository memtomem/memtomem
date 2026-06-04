/* rank 21: the four artifact-section toolbars (Skills / Commands / Agents /
   MCP Servers) render from one capability-mapped source (``_ctxToolbarHtml`` +
   ``_CTX_TOOLBAR_CAPS`` in context-gateway.js) instead of hand-copied static
   markup. These guards lock the invariant that MCP's missing Import is a
   *declared* capability flag, not silent copy-paste drift, and that the shared
   template keeps the universal buttons + rightmost-primary Sync in every
   section. */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SECTION_ID = {
  skills: 'settings-ctx-skills',
  commands: 'settings-ctx-commands',
  agents: 'settings-ctx-agents',
  'mcp-servers': 'settings-ctx-mcp-servers',
};

const SCRIPTS = ['i18n.js', 'app.js', 'context-gateway.js'];

function toolbar(doc, type) {
  return doc.querySelector(`#${SECTION_ID[type]} .ctx-toolbar[data-type="${type}"]`);
}

describe('Context Gateway artifact-section toolbars (rank 21)', () => {
  it('renders a shared toolbar into every artifact section at init', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    const doc = window.document;
    for (const type of Object.keys(SECTION_ID)) {
      const bar = toolbar(doc, type);
      expect(bar, `${type} toolbar container`).toBeTruthy();
      // Universal buttons present and tagged with the section's data-type.
      for (const cls of ['ctx-add-project-btn', 'ctx-create-btn', 'ctx-sync-btn']) {
        const btn = bar.querySelector(`.${cls}`);
        expect(btn, `${type} .${cls}`).toBeTruthy();
        expect(btn.dataset.type).toBe(type);
      }
      // Sync stays the rightmost, primary action across every section.
      const buttons = [...bar.querySelectorAll('button')];
      const last = buttons[buttons.length - 1];
      expect(last.classList.contains('ctx-sync-btn'), `${type} Sync rightmost`).toBe(true);
      expect(last.classList.contains('btn-primary'), `${type} Sync primary`).toBe(true);
    }
  });

  it('exposes Import for skills/commands/agents but not MCP servers', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    const doc = window.document;
    for (const type of ['skills', 'commands', 'agents']) {
      expect(
        toolbar(doc, type).querySelector('.ctx-import-btn'),
        `${type} should expose Import`,
      ).toBeTruthy();
    }
    expect(
      toolbar(doc, 'mcp-servers').querySelector('.ctx-import-btn'),
      'MCP servers must omit Import (single .mcp.json source, no /import route)',
    ).toBeNull();
  });

  it('bridges the hyphenated data-type to underscore i18n keys', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    // ``data-type="mcp-servers"`` but the localization keys are ``mcp_servers_*``.
    const html = window._ctxToolbarHtml('mcp-servers');
    expect(html).toContain('data-i18n-title="settings.ctx.mcp_servers_sync_tooltip"');
    expect(html).toContain('data-i18n-aria-label="settings.ctx.mcp_servers_add_project_aria"');
    expect(html).not.toContain('ctx-import-btn');
  });
});

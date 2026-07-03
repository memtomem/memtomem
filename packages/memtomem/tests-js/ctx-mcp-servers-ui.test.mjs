import { describe, it, expect } from 'vitest';
import { bootApp, readGatewayText } from './setup/jsdom-app.mjs';

function installOverviewFetch(window) {
  const upstream = window.fetch;
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/api/context/overview')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          target_scope: 'project_shared',
          project_root: '/srv/demo',
          detected_runtimes: [],
          skills: { total: 0 },
          commands: { total: 0 },
          agents: { total: 0 },
          mcp_servers: { total: 1, in_sync: 1 },
          settings: { total: 0, status: 'in_sync' },
        }),
      };
    }
    if (url.includes('/api/context/projects')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          scopes: [{
            scope_id: '',
            label: 'Server CWD',
            root: '/srv/demo',
            tier: 'project',
            sources: ['server-cwd'],
            missing: false,
            experimental: false,
            counts: { skills: 0, commands: 0, agents: 0, 'mcp-servers': 1 },
          }],
        }),
      };
    }
    return upstream(input, init);
  };
}

describe('Context Gateway MCP Servers UI', () => {
  it('ships a prod MCP Servers section and renders the overview tile', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    installOverviewFetch(window);
    await window.I18N.init();

    expect(window.document.getElementById('settings-ctx-mcp-servers')).not.toBeNull();
    expect(
      window.document.querySelector('[data-section="ctx-mcp-servers"][data-ui-tier="prod"]'),
    ).not.toBeNull();

    await window.loadCtxOverview();
    const tile = window.document.querySelector(
      '.ctx-overview-stat[data-section="ctx-mcp-servers"]',
    );
    expect(tile).not.toBeNull();
    expect(tile.textContent).toContain('MCP Servers');
    expect(tile.textContent).toContain('1/1');
  });

  it('includes mcp-servers in the Sync All phase list', () => {
    // #1517: gateway source is split into fragments — grep the concatenation.
    const text = readGatewayText();
    expect(text).toContain("const types = ['skills', 'commands', 'agents', 'mcp-servers']");
  });
});

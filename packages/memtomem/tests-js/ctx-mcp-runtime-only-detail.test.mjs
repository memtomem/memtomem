/* #1247 B8 (ids 31 + 36): MCP-servers runtime-only visibility + detail chips.
 *
 * id 31 — runtime-only ``.mcp.json`` servers now get list/diff rows, which
 * makes the runtime-only detail pane reachable for mcp-servers. That pane
 * renders a per-item "Import this …" button posting ``/{type}/{name}/import``
 * — a route that does NOT exist for mcp-servers. The button must be gated on
 * the same ``_CTX_TOOLBAR_CAPS`` capability map as the section toolbar
 * (#1223), or every runtime-only MCP detail click-through ends in a 404
 * toast. Skills keep the button — the gate must not over-reach.
 *
 * id 36 — ``read_mcp_server`` has always returned
 * ``{command, args_count, env_count}``; ``_ctxRenderDetailMetaHeader`` now
 * renders them as chips (numeric 0 included — it confirms the definition
 * parsed).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const SCRIPTS = ['i18n.js', 'app.js', 'context-gateway.js'];

function runtimeOnlyDiff(name, content) {
  return {
    name,
    canonical_content: null,
    canonical_path: `.memtomem/mcp-servers/${name}.json`,
    runtimes: [
      { runtime: 'project_mcp', status: 'missing canonical', runtime_content: content },
    ],
  };
}

describe('MCP runtime-only detail (#1247 id 31)', () => {
  it('renders the .mcp.json definition without an Import button', async () => {
    const { window } = await bootApp({
      scripts: SCRIPTS,
      apiResponses: {
        '/api/context/mcp-servers/adhoc/diff':
          runtimeOnlyDiff('adhoc', '{\n  "command": "node",\n  "args": ["server.js"]\n}\n'),
      },
    });
    await window.I18N.init();
    const detailEl = window.document.getElementById('ctx-mcp-servers-detail');
    expect(detailEl, 'detail container').toBeTruthy();

    await window._ctxLoadRuntimeOnlyDetail('mcp-servers', 'adhoc', detailEl);

    expect(detailEl.textContent).toContain('server.js');
    // No /import route exists for mcp-servers — the CTA must not render.
    expect(detailEl.querySelector('.ctx-runtime-only-import')).toBeNull();
  });

  it('keeps the Import button for skills (gate must not over-reach)', async () => {
    const { window } = await bootApp({
      scripts: SCRIPTS,
      apiResponses: {
        '/api/context/skills/notes/diff': {
          name: 'notes',
          canonical_content: null,
          canonical_path: '.memtomem/skills/notes',
          runtimes: [
            { runtime: 'claude_skills', status: 'missing canonical', runtime_content: '# notes' },
          ],
        },
      },
    });
    await window.I18N.init();
    const detailEl = window.document.getElementById('ctx-skills-detail');
    expect(detailEl, 'detail container').toBeTruthy();

    await window._ctxLoadRuntimeOnlyDetail('skills', 'notes', detailEl);

    expect(detailEl.querySelector('.ctx-runtime-only-import')).not.toBeNull();
  });
});

describe('MCP missing-canonical banner copy (#1247 id 31, Codex impl review)', () => {
  it('does not tell the user to click a nonexistent Import button', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    await window.I18N.init();

    const mcpHtml = window._ctxMissingCanonicalRemediationHtml('mcp-servers', 2, ['.mcp.json']);
    expect(mcpHtml).not.toContain('Click Pull above');
    expect(mcpHtml).toContain('Create');

    // Sibling families keep the Import-oriented copy — the branch must not
    // over-reach.
    const skillsHtml = window._ctxMissingCanonicalRemediationHtml('skills', 1, ['.claude/skills']);
    expect(skillsHtml).toContain('Click Pull above');
  });
});

describe('MCP detail meta chips (#1247 id 36)', () => {
  it('renders command / args / env chips, keeping numeric zero', async () => {
    const { window } = await bootApp({ scripts: SCRIPTS });
    await window.I18N.init();

    const html = window._ctxRenderDetailMetaHeader('mcp-servers', {
      fields: { command: 'uvx', args_count: 3, env_count: 0 },
      target_scope: 'project_shared',
      layout: 'flat',
    });

    expect(html).toContain('ctx-detail-chips');
    expect(html).toContain('uvx');
    expect(html).toContain('Command');
    expect(html).toContain('Args');
    expect(html).toContain('Env vars');
    // args_count/env_count are counts — 0 must render, not be filtered as falsy.
    expect(html).toMatch(/Env vars<\/span><span class="ctx-detail-chip-value">0/);
  });
});

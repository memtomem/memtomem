/* Localized write-time Gate A editor privacy-block toast (#1651).
 *
 * The per-kind (skills/commands/agents) and MCP-server editor save 422 keeps a
 * path-free ENGLISH string ``detail`` ("Gate A: … ADR-0011 §5 …") and now rides
 * a top-level ``reason_code: "privacy_blocked"`` sibling (the #1409 hoist).
 * ``_ctxMaybePrivacyToast`` maps that reason_code to a localized, jargon-free
 * hint (keeping the raw detail in a tooltip) and picks an MCP-specific hint for
 * mcp-servers — which are project_shared-only (ADR-0011 §1), so the per-kind
 * "save to your user library" remediation is invalid there. These pin the
 * reason_code → hint mapping, the kind split, and the tooltip fidelity.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  await window.I18N.init();
  return window;
}

// Capture the last showToast(message, type, options) call instead of rendering.
function stubToast(window) {
  const calls = [];
  window.showToast = (message, type, options = {}) => {
    calls.push({ message, type, options });
  };
  return calls;
}

describe('localized editor privacy-block toast (#1651)', () => {
  it('maps reason_code privacy_blocked → the localized editor hint (non-MCP)', async () => {
    const window = await boot();
    const { I18N } = window;
    const calls = stubToast(window);
    const handled = window._ctxMaybePrivacyToast(
      {
        detail: 'Gate A: leaky contains 1 privacy pattern hit(s); write to scope=…',
        reason_code: 'privacy_blocked',
      },
      'skills',
    );
    expect(handled).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].message).toBe(I18N.t('settings.ctx.privacy_blocked_editor_hint'));
    expect(calls[0].message).not.toContain('Gate A'); // localized, not the raw wall
    expect(calls[0].type).toBe('error');
    // The raw English detail is preserved in the tooltip for fidelity.
    expect(calls[0].options.title).toContain('Gate A: leaky');
  });

  it('picks the MCP-specific hint for mcp-servers (no user-tier remediation)', async () => {
    const window = await boot();
    const { I18N } = window;
    const calls = stubToast(window);
    window._ctxMaybePrivacyToast(
      {
        detail: 'Gate A: leaky.json contains 1 privacy pattern hit(s); MCP server fan-out …',
        reason_code: 'privacy_blocked',
      },
      'mcp-servers',
    );
    const mcpHint = I18N.t('settings.ctx.privacy_blocked_mcp_hint');
    const editorHint = I18N.t('settings.ctx.privacy_blocked_editor_hint');
    expect(mcpHint).not.toBe(editorHint); // distinct copy
    expect(calls[0].message).toBe(mcpHint);
    // The "user library" remediation is wrong for project_shared-only MCP servers.
    expect(calls[0].message).not.toContain('user library');
  });

  it('returns false and does not toast for non-privacy errors', async () => {
    const window = await boot();
    const calls = stubToast(window);
    expect(
      window._ctxMaybePrivacyToast({ detail: 'boom', reason_code: 'parse_error' }, 'skills'),
    ).toBe(false);
    expect(window._ctxMaybePrivacyToast(null, 'skills')).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it('omits the tooltip when detail is not a string', async () => {
    const window = await boot();
    const calls = stubToast(window);
    window._ctxMaybePrivacyToast(
      { detail: { nested: true }, reason_code: 'privacy_blocked' },
      'skills',
    );
    expect(calls[0].options.title).toBeUndefined();
  });
});

/* U7 (#1229): parse-error / invalid-name rows render a diagnostic block —
 * server-sanitized reason + a fix-it hint naming the canonical file — in the
 * diff pane and the runtime-only detail, and the list-card badge tooltip
 * carries the reason. Healthy rows render nothing extra (negative pins).
 *
 * `_ctxDiagnosticDetail` and `renderRuntimeBadges` are global function
 * declarations, callable directly off the booted window.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  await dom.window.I18N.init();
  return dom.window;
}

describe('diagnostic detail block (_ctxDiagnosticDetail)', () => {
  it('renders reason + interpolated hint for a parse-error row', async () => {
    const window = await boot();
    const html = window._ctxDiagnosticDetail(
      { status: 'parse error', reason: 'missing YAML frontmatter: .memtomem/agents/broken.md' },
      '.memtomem/agents/broken.md',
    );
    expect(html).toContain('ctx-diagnostic-reason');
    expect(html).toContain('missing YAML frontmatter');
    // The hint names the canonical file (settings.ctx.parse_error_hint).
    expect(html).toContain('.memtomem/agents/broken.md');
    expect(html).toContain('Refresh');
  });

  it('renders the validate_name reason for an invalid-name row (no hint without a canonical)', async () => {
    const window = await boot();
    const html = window._ctxDiagnosticDetail(
      { status: 'invalid name', reason: "invalid agent name '-bad': leading dash" },
      null,
    );
    expect(html).toContain('leading dash');
    expect(html).not.toContain('ctx-diagnostic-hint');
  });

  it('escapes HTML in the reason (raw names are attacker-adjacent strings)', async () => {
    const window = await boot();
    const html = window._ctxDiagnosticDetail(
      { status: 'parse error', reason: '<img src=x onerror=alert(1)>' },
      null,
    );
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  it('renders nothing for healthy rows and for diagnostic rows without data (negative pins)', async () => {
    const window = await boot();
    expect(window._ctxDiagnosticDetail({ status: 'in sync' }, '.memtomem/x.md')).toBe('');
    expect(window._ctxDiagnosticDetail({ status: 'out of sync', reason: 'x' }, null)).toBe('');
    // Parse error with neither reason nor canonical path → empty, not an
    // empty styled box.
    expect(window._ctxDiagnosticDetail({ status: 'invalid name' }, null)).toBe('');
  });
});

describe('list-card badge tooltip', () => {
  it('carries the reason in the title attribute when present', async () => {
    const window = await boot();
    const html = window.renderRuntimeBadges([
      { runtime: 'claude_agents', status: 'parse error', reason: 'missing YAML frontmatter' },
      { runtime: 'gemini_agents', status: 'in sync' },
    ]);
    expect(html).toContain('claude_agents — missing YAML frontmatter');
    // Healthy badge keeps the bare runtime-name tooltip.
    expect(html).toContain('title="gemini_agents"');
  });
});

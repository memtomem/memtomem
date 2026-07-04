/* Web-display polish batch (#1646, from the 2026-07-04 gateway audit):
 *
 * 1. Overview tile badge: `in_sync` counts (runtime, name) COPIES while
 *    `total` counts stored artifacts, so with >1 runtime the all-clear badge
 *    rendered a cross-axis "fraction" ("4/1 synced"). Equal counts (the
 *    single-runtime common case) keep the compact `N/N synced`; unequal
 *    counts spell out both axes.
 * 2. Single-item import skip toasts surfaced raw backend English; the skip
 *    payload's stable `reason_code` now maps to i18n copy
 *    (`_ctxImportSkipText`), raw reason kept as the unknown-code fallback.
 * 3. Runtime labels: chips/badges render branded names (Claude Code, Codex,
 *    Kimi) and the MCP fan-out's internal `project_mcp` id renders as its
 *    target ".mcp.json"; diagnostic tooltips keep the raw id.
 * 5. The Default Tab selector's optgroup/option labels localize (the
 *    `data-i18n-label` attribute variant covers `<optgroup label=…>`).
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  await dom.window.I18N.init();
  return dom.window;
}

function stubOverviewFetch(window, tiles) {
  const upstream = window.fetch;
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/api/context/overview')) {
      return { ok: true, status: 200, json: async () => ({ ...tiles }) };
    }
    if (url.includes('/api/context/projects')) {
      return { ok: true, status: 200, json: async () => ({ scopes: [] }) };
    }
    return upstream(input);
  };
}

function tileBadgeText(window, key) {
  return window.document
    .querySelector(`.ctx-overview-stat[data-tile-key="${key}"] .ctx-overview-badge .badge`)
    ?.textContent ?? '';
}

describe('overview all-clear badge axes (#1646 item 1)', () => {
  it('spells out both axes when copies outnumber stored artifacts (multi-runtime)', async () => {
    const window = await boot();
    stubOverviewFetch(window, { skills: { total: 1, in_sync: 4 } });
    await window.loadCtxOverview();
    const text = tileBadgeText(window, 'skills');
    expect(text).toContain('1 stored');
    expect(text).toContain('4 runtime copies in sync');
    expect(text).not.toContain('4/1');
  });

  it('keeps the compact fraction when the counts agree (single-runtime)', async () => {
    const window = await boot();
    stubOverviewFetch(window, { skills: { total: 3, in_sync: 3 } });
    await window.loadCtxOverview();
    expect(tileBadgeText(window, 'skills')).toContain('3/3 synced');
  });

  it('stays green — the two-axis copy is an all-clear rendering, not an issue', async () => {
    const window = await boot();
    stubOverviewFetch(window, { agents: { total: 2, in_sync: 8 } });
    await window.loadCtxOverview();
    const badge = window.document.querySelector(
      '.ctx-overview-stat[data-tile-key="agents"] .ctx-overview-badge .badge');
    expect(badge.classList.contains('badge-success')).toBe(true);
  });
});

describe('_ctxImportSkipText (#1646 item 2)', () => {
  it('maps known reason codes to localized copy', async () => {
    const window = await boot();
    const text = window._ctxImportSkipText(
      { reason: 'canonical exists (use --overwrite)', reason_code: 'canonical_exists' });
    expect(text).toContain('stored copy already exists');
    expect(text).not.toContain('--overwrite');
  });

  it('reuses the shared-tier privacy remediation copy for privacy_blocked_project_shared', async () => {
    const window = await boot();
    const text = window._ctxImportSkipText(
      { reason: 'blocked: 2 privacy pattern hit(s)', reason_code: 'privacy_blocked_project_shared' });
    expect(text).toBe(window.t('settings.ctx.privacy_blocked_shared_hint'));
  });

  it('falls back to the raw backend reason for unknown codes (stays visible, never silent)', async () => {
    const window = await boot();
    expect(window._ctxImportSkipText({ reason: 'some future reason', reason_code: 'future_code' }))
      .toBe('some future reason');
    expect(window._ctxImportSkipText({ reason: 'bare reason' })).toBe('bare reason');
  });

  it('falls back to the generic failure toast when the skip has no reason at all', async () => {
    const window = await boot();
    expect(window._ctxImportSkipText({})).toBe(window.t('toast.request_failed'));
    expect(window._ctxImportSkipText(undefined)).toBe(window.t('toast.request_failed'));
  });

  it('localizes in Korean', async () => {
    const window = await boot();
    await window.I18N.setLang('ko');
    const text = window._ctxImportSkipText(
      { reason: 'canonical exists (use --overwrite)', reason_code: 'canonical_exists' });
    expect(text).toContain('저장된 사본');
    await window.I18N.setLang('en');
  });
});

describe('runtime display names (#1646 item 3)', () => {
  it('renders branded names on list-card badges, raw id in the tooltip', async () => {
    const window = await boot();
    const html = window.renderRuntimeBadges([
      { runtime: 'claude_agents', status: 'in sync' },
      { runtime: 'kimi_agents', status: 'in sync' },
      { runtime: 'gemini_agents', status: 'in sync' },
    ]);
    expect(html).toContain('Claude Code:');
    expect(html).toContain('Kimi:');
    expect(html).toContain('Antigravity:');
    // Diagnostic tooltip keeps the raw generator id.
    expect(html).toContain('title="claude_agents"');
    expect(html).not.toContain('>claude:');
  });

  it('renders the MCP fan-out id as its .mcp.json target, matching the Sync button', async () => {
    const window = await boot();
    const html = window.renderRuntimeBadges([
      { runtime: 'project_mcp', status: 'missing target' },
    ]);
    expect(html).toContain('.mcp.json:');
    expect(html).not.toContain('>project_mcp:');
  });

  it('maps overview header chip ids (plain vendor keys)', async () => {
    const window = await boot();
    expect(window._ctxRuntimeLabel('claude')).toBe('Claude Code');
    expect(window._ctxRuntimeLabel('codex')).toBe('Codex');
    expect(window._ctxRuntimeLabel('kimi')).toBe('Kimi');
    expect(window._ctxRuntimeLabel('gemini')).toBe('Antigravity');
    // Unknown ids stay visible verbatim rather than vanishing.
    expect(window._ctxRuntimeLabel('futuretool')).toBe('futuretool');
  });
});

describe('default-tab selector localization (#1646 item 5)', () => {
  it('localizes optgroup labels and option text on language switch', async () => {
    const window = await boot();
    const groups = window.document.querySelectorAll('#settings-default-tab optgroup');
    expect(groups.length).toBe(2);
    expect(groups[0].getAttribute('label')).toBe('Main');
    await window.I18N.setLang('ko');
    expect(groups[0].getAttribute('label')).toBe('메인');
    expect(groups[1].getAttribute('label')).toBe('설정');
    const hub = window.document.querySelector('#settings-default-tab option[value="settings"]');
    expect(hub.textContent).toBe('⚙ 설정 허브');
    const gateway = window.document.querySelector(
      '#settings-default-tab option[value="context-gateway"]');
    expect(gateway.textContent).toBe(window.t('nav.context_gateway'));
    await window.I18N.setLang('en');
    expect(groups[0].getAttribute('label')).toBe('Main');
    expect(hub.textContent).toBe('⚙ Settings Hub');
  });
});

/* ADR-0022 PR4 — list-card label chips fed by the ?include=versions enrichment.
 *
 * Unit-drives the real ``_ctxRenderItemsHtml`` against item payloads carrying a
 * ``versions`` summary and asserts: the read-only ``production → v2`` chips
 * render (no interactive element inside the role=button card), an item with no
 * labels renders no chip row, the chips are echoed into the card aria-label for
 * SR parity, the inline ``t()`` tooltip follows a locale switch, and an item
 * with no ``versions`` key (enrichment not requested) renders nothing.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const ITEM_WITH_LABELS = {
  name: 'reviewer',
  canonical_path: '.memtomem/agents/reviewer/agent.md',
  target_scope: 'project_shared',
  runtimes: [{ runtime: 'claude_agents', status: 'in sync' }],
  versions: {
    labels: { production: 'v2', staging: 'v1' },
    count: 2,
    versionable: true,
    migrate_required: false,
  },
};

const ITEM_NO_LABELS = {
  name: 'planner',
  canonical_path: '.memtomem/agents/planner/agent.md',
  target_scope: 'project_shared',
  runtimes: [{ runtime: 'claude_agents', status: 'in sync' }],
  versions: { labels: {}, count: 0, versionable: true, migrate_required: false },
};

function render(window, items) {
  // _ctxRenderItemsHtml(items, type, projectRoot, scannedDirs, { clickable })
  return window._ctxRenderItemsHtml(items, 'agents', '/srv/cwd', [], { clickable: true });
}

function parse(window, html) {
  const div = window.document.createElement('div');
  div.innerHTML = html;
  return div;
}

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  // bootApp resolves before the async locale fetch settles; force-load EN so the
  // inline ``t()`` in the synchronous render below resolves keys (not their ids).
  await dom.window.I18N.setLang('en');
  return dom;
}

describe('ADR-0022 PR4 list-card label chips', () => {
  it('renders a read-only production → v2 chip per label, no interactive child', async () => {
    const { window } = await boot();
    const root = parse(window, render(window, [ITEM_WITH_LABELS]));
    const chips = root.querySelectorAll(
      '.ctx-card[data-name="reviewer"] .ctx-card-label-chip',
    );
    expect(chips.length).toBe(2);
    const prod = root.querySelector('.ctx-card-label-chip[data-label="production"]');
    expect(prod).not.toBeNull();
    expect(prod.textContent.replace(/\s+/g, ' ')).toContain('production');
    expect(prod.querySelector('.ctx-card-label-tag').textContent).toBe('v2');
    // The card is itself role=button — a button/anchor inside it would be a
    // nested-interactive a11y violation. Chips are pure spans.
    expect(root.querySelector('.ctx-card-label-chip button')).toBeNull();
    expect(root.querySelector('.ctx-card-label-chip a')).toBeNull();
  });

  it('renders no chip row when the item has no labels', async () => {
    const { window } = await boot();
    const root = parse(window, render(window, [ITEM_NO_LABELS]));
    expect(root.querySelector('.ctx-card-labels')).toBeNull();
    expect(root.querySelector('.ctx-card-label-chip')).toBeNull();
  });

  it('echoes label pointers into the card aria-label for SR parity', async () => {
    const { window } = await boot();
    const root = parse(window, render(window, [ITEM_WITH_LABELS]));
    const card = root.querySelector('.ctx-card[data-name="reviewer"]');
    const aria = card.getAttribute('aria-label') || '';
    // Visible chips would be invisible to SR otherwise — aria-label overrides
    // child text on a role=button element.
    expect(aria).toContain('production');
    expect(aria).toContain('v2');
    expect(aria).toContain('staging');
    expect(aria).toContain('v1');
  });

  it('chip tooltip follows a locale switch (inline t())', async () => {
    const { window } = await boot();
    const enTip = parse(window, render(window, [ITEM_WITH_LABELS]))
      .querySelector('.ctx-card-label-chip[data-label="production"]')
      .getAttribute('title');
    expect(enTip).toContain('detail panel');

    await window.I18N.setLang('ko');
    const koTip = parse(window, render(window, [ITEM_WITH_LABELS]))
      .querySelector('.ctx-card-label-chip[data-label="production"]')
      .getAttribute('title');
    expect(koTip).toContain('상세 패널');
    expect(koTip).not.toContain('detail panel');

    await window.I18N.setLang('en');
  });

  it('HTML-escapes a malicious label name across every interpolation point', async () => {
    // ``_validate_label_name`` (versioning.py) imposes NO charset restriction on
    // label names — only ``latest`` and version-shaped names are rejected — so a
    // hand-edited or future-write label can carry markup. The escaping in
    // renderLabelChips (data-label, title, .ctx-card-label-name) and the card
    // aria-label is therefore load-bearing, not defensive. Pin it so a future
    // refactor that drops an escapeHtml call fails loudly.
    const { window } = await boot();
    const evil = 'prod"><img src=x onerror=alert(1)>';
    const item = {
      name: 'reviewer',
      canonical_path: '.memtomem/agents/reviewer/agent.md',
      target_scope: 'project_shared',
      runtimes: [],
      versions: { labels: { [evil]: 'v1' }, count: 1, versionable: true, migrate_required: false },
    };
    const html = render(window, [item]);
    // A single oracle over the whole markup catches any unescaped point (the
    // chip data-label/title/name AND the card aria-label all carry the label).
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
    // The card aria-label is built from the same label via t() + escapeHtml.
    expect(html).toContain('aria-label=');
    expect(html).not.toContain('"><img');
  });

  it('renders nothing when the item carries no versions key (enrichment off)', async () => {
    const { window } = await boot();
    const bare = {
      name: 'legacy',
      canonical_path: '.memtomem/agents/legacy/agent.md',
      target_scope: 'project_shared',
      runtimes: [],
    };
    const root = parse(window, render(window, [bare]));
    expect(root.querySelector('.ctx-card-label-chip')).toBeNull();
    expect(root.querySelector('.ctx-card-labels')).toBeNull();
  });
});

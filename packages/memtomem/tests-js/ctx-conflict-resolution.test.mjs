/* Regression pins for the 409 mtime-conflict resolution modal (#763, #1123 B6-5).
 *
 * When PUT /context/{type}/{name} returns 409 (the on-disk file changed under
 * the user's unsaved edits), `_ctxResolveConflict` opens a three-choice dialog
 * and resolves to the button the user picks — 'reload' | 'diff' | 'force' — or
 * null when the dialog is dismissed (Escape / backdrop). This client-side flow
 * had no test. We drive it through the `window.openCtxConflictModal()` dev entry
 * point, which calls `_ctxResolveConflict('', '')`.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function openConflict() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  const { window } = dom;
  // openCtxConflictModal() returns the resolution promise from _ctxResolveConflict.
  const choice = window.openCtxConflictModal();
  const byId = (id) => window.document.getElementById(id);
  const modal = byId('ctx-conflict-modal');
  return { window, modal, byId, choice };
}

describe('409 mtime-conflict resolution modal', () => {
  it('opens the modal and focuses the safe (Reload) choice — never Force', async () => {
    const { window, modal } = await openConflict();
    expect(modal.hidden).toBe(false);
    // Safety contract: Force-save overwrites the other writer's edits, so a
    // reflexive Enter must NOT land on it. The modal focuses Reload instead.
    const active = window.document.activeElement;
    expect(active && active.id).toBe('ctx-conflict-reload-btn');
    expect(active && active.id).not.toBe('ctx-conflict-force-btn');
  });

  it('resolves to "force" when Force-save is clicked, then hides the modal', async () => {
    const { modal, byId, choice } = await openConflict();
    byId('ctx-conflict-force-btn').click();
    expect(await choice).toBe('force');
    expect(modal.hidden).toBe(true);
  });

  it('resolves to "reload" when Reload is clicked', async () => {
    const { byId, choice } = await openConflict();
    byId('ctx-conflict-reload-btn').click();
    expect(await choice).toBe('reload');
  });

  it('resolves to "diff" when Open-diff is clicked', async () => {
    const { byId, choice } = await openConflict();
    byId('ctx-conflict-diff-btn').click();
    expect(await choice).toBe('diff');
  });

  it('resolves to null when dismissed via Escape (no silent overwrite)', async () => {
    const { window, choice } = await openConflict();
    window.document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    expect(await choice).toBe(null);
  });

  it('resolves to null when the backdrop is clicked', async () => {
    const { modal, choice } = await openConflict();
    modal.click(); // event target === modal overlay → backdrop dismiss
    expect(await choice).toBe(null);
  });
});

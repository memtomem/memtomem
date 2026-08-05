/* Issue #2035 — index-result namespace provenance.
 *
 * ``resolved_namespaces`` is the legacy hybrid union. The additive
 * ``applied_namespaces`` subset lets the shared POST/SSE result renderer label
 * successful values as applied and the remainder as preview-only without
 * exposing per-file metadata.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function indexResult(overrides = {}) {
  return {
    type: 'complete',
    total_files: 1,
    total_chunks: 0,
    indexed_chunks: 0,
    skipped_chunks: 0,
    deleted_chunks: 0,
    duration_ms: 5,
    errors: [],
    retryable_errors: [],
    resolved_namespaces: [],
    applied_namespaces: [],
    blocked_files: 0,
    blocked_paths: [],
    blocked_project_shared_files: 0,
    ...overrides,
  };
}

describe('Index result namespace provenance', () => {
  let window;
  let document;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;
    await window.I18N.init();

    window.showToast = () => {};
    window.loadStats = () => {};
    window.loadNamespaceDropdowns = () => {};
    window.loadSourceFilter = () => {};
  });

  function render(result) {
    window._renderIndexResult(result, { registerAsSource: false, path: '/tmp/memories' });
    return document.getElementById('r-namespace').textContent;
  }

  it('keeps an empty namespace result neutral', () => {
    const text = render(indexResult());

    expect(text).toBe(window.t('index.ns_render.untagged_applied'));
    expect(text).not.toContain('preview');
  });

  it('defaults the low-level namespace renderer to preview mode', () => {
    expect(window.renderResolvedNamespaces(['default-ns'])).toBe(
      window.t('index.ns_render.single_preview', { ns: 'default-ns' }),
    );
  });

  it('renders a bare namespace value for an explicitly labeled group', () => {
    expect(window.renderResolvedNamespaces(['bare-ns'], { mode: 'bare' })).toBe(
      window.t('index.ns_render.single_applied', { ns: 'bare-ns' }),
    );
  });

  it('renders a privacy-blocked-only namespace as preview', () => {
    const text = render(indexResult({
      resolved_namespaces: ['blocked-ns'],
      blocked_files: 1,
      blocked_paths: ['/tmp/memories/leak.md'],
      errors: ['leak.md: redaction_blocked'],
    }));

    expect(text).toBe(window.t('index.ns_render.single_preview', { ns: 'blocked-ns' }));
  });

  it('renders a failed-only namespace as preview', () => {
    const text = render(indexResult({
      resolved_namespaces: ['failed-ns'],
      errors: ['note.md: embedding failed'],
    }));

    expect(text).toBe(window.t('index.ns_render.single_preview', { ns: 'failed-ns' }));
  });

  it('renders a successful namespace-bearing write as applied', () => {
    const text = render(indexResult({
      total_chunks: 1,
      indexed_chunks: 1,
      resolved_namespaces: ['written-ns'],
      applied_namespaces: ['written-ns'],
    }));

    expect(text).toBe(window.t('index.ns_render.single_applied', { ns: 'written-ns' }));
  });

  it('renders distinct applied and preview-only values separately in a mixed run', () => {
    const text = render(indexResult({
      total_files: 2,
      total_chunks: 1,
      indexed_chunks: 1,
      resolved_namespaces: ['applied-ns', 'preview-ns'],
      applied_namespaces: ['applied-ns'],
      errors: ['preview.md: failed'],
    }));

    expect(text).toBe(window.t('index.ns_render.mixed', {
      applied: window.t('index.ns_render.single_applied', { ns: 'applied-ns' }),
      preview: window.t('index.ns_render.single_applied', { ns: 'preview-ns' }),
    }));
    expect(text).toContain('Applied:');
    expect(text).toContain('Preview only:');
  });

  it('treats a same-value applied/preview overlap as applied at value level', () => {
    const text = render(indexResult({
      total_files: 2,
      total_chunks: 1,
      indexed_chunks: 1,
      resolved_namespaces: ['shared-ns'],
      applied_namespaces: ['shared-ns'],
      blocked_files: 1,
      blocked_paths: ['/tmp/memories/leak.md'],
      errors: ['leak.md: redaction_blocked'],
    }));

    expect(text).toBe(window.t('index.ns_render.single_applied', { ns: 'shared-ns' }));
  });

  it('fails conservatively when applied_namespaces is missing', () => {
    const result = indexResult({ resolved_namespaces: ['legacy-ns'] });
    delete result.applied_namespaces;

    expect(render(result)).toBe(
      window.t('index.ns_render.single_preview', { ns: 'legacy-ns' }),
    );
  });

  it('uses the Korean applied/preview copy for mixed results', async () => {
    await window.I18N.setLang('ko');

    const text = render(indexResult({
      total_files: 2,
      total_chunks: 1,
      indexed_chunks: 1,
      resolved_namespaces: ['적용', '예정'],
      applied_namespaces: ['적용'],
      errors: ['예정.md: 실패'],
    }));

    expect(text).toBe(window.t('index.ns_render.mixed', {
      applied: window.t('index.ns_render.single_applied', { ns: '적용' }),
      preview: window.t('index.ns_render.single_applied', { ns: '예정' }),
    }));
    expect(text).toContain('적용됨:');
    expect(text).toContain('미리보기 전용:');
  });
});

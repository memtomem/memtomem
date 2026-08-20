/* Issue #2061 — the namespace advisory on forced reindexes.
 *
 * ``--force`` / ``force=true`` re-embeds without re-resolving namespaces, so a
 * namespace rule that has not been applied is invisible unless the result says
 * so. Every force workflow in the UI has to report it, including the ones that
 * only show a toast.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function indexResult(overrides = {}) {
  return {
    type: 'complete',
    total_files: 1,
    total_chunks: 1,
    indexed_chunks: 1,
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
    namespaces_preserved_against_rules: 0,
    namespaces_reassigned: 0,
    namespace_moves: [],
    ...overrides,
  };
}

describe('Index result namespace advisory', () => {
  let window;
  let document;
  let toasts;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;
    await window.I18N.init();

    toasts = [];
    window.showToast = (message, type) => toasts.push({ message, type });
    window.loadStats = () => {};
    window.loadNamespaceDropdowns = () => {};
    window.loadSourceFilter = () => {};
  });

  function render(result) {
    window._renderIndexResult(result, { registerAsSource: false, path: '/tmp/memories' });
    return {
      row: document.getElementById('r-namespace-advisory-row'),
      text: document.getElementById('r-namespace-advisory').textContent,
    };
  }

  it('stays hidden when nothing was preserved or moved', () => {
    const { row } = render(indexResult());
    expect(row.hidden).toBe(true);
  });

  it('names the preserved count when the rules disagree', () => {
    const { row, text } = render(indexResult({ namespaces_preserved_against_rules: 3 }));
    expect(row.hidden).toBe(false);
    expect(text).toContain('3');
    expect(text).toContain('--reassign-namespaces');
  });

  it('renders structured move records as readable lines', () => {
    const { text } = render(
      indexResult({
        namespaces_reassigned: 5,
        namespace_moves: [{ from: 'agent-runtime:planner', to: 'default', files: 5 }],
      }),
    );
    expect(text).toContain('agent-runtime:planner → default: 5 file(s)');
  });

  it('toasts the advisory even when the run also reported errors', () => {
    // A partial run can preserve namespaces on the files that succeeded while
    // another file fails; the advisory must not disappear with the failure.
    window.indexAdvisoryToast(
      indexResult({ errors: ['broken.md: boom'], namespaces_preserved_against_rules: 2 }),
    );
    expect(toasts.some((toast) => toast.message.includes('2'))).toBe(true);
  });

  it('says nothing when there is nothing to advise', () => {
    expect(window.indexAdvisoryToast(indexResult())).toBe(false);
    expect(toasts).toEqual([]);
  });

  // #2115: chunks skipped as unchanged that carry no vector. Shares the
  // reporter with the namespace advisory so a consumer of one gets both.
  it('names the chunks left without an embedding', () => {
    const { row, text } = render(indexResult({ chunks_missing_vectors: 7 }));
    expect(row.hidden).toBe(false);
    expect(text).toContain('7');
    expect(text).toContain('--force');
  });

  it('toasts the missing-vector advisory through the shared reporter', () => {
    expect(window.indexAdvisoryToast(indexResult({ chunks_missing_vectors: 4 }))).toBe(true);
    expect(toasts.some((toast) => toast.message.includes('4'))).toBe(true);
  });

  it('reports both advisories when a run carries both', () => {
    const { text } = render(
      indexResult({ namespaces_preserved_against_rules: 2, chunks_missing_vectors: 9 }),
    );
    expect(text).toContain('--reassign-namespaces');
    expect(text).toContain('--force');
  });
});

describe('Sources panel advisory consumers', () => {
  let window;
  let toasts;

  beforeEach(async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'sources-memory-dirs.js'],
    });
    window = dom.window;
    await window.I18N.init();
    toasts = [];
    window.showToast = (message, type) => toasts.push({ message, type });
    window.loadStats = () => {};
    window._markDataStale = () => {};
    window.btnLoading = () => {};
  });

  it('reports the advisory from the per-root reindex-all response', async () => {
    window.api = async () => ({
      errors: [],
      results: [
        { indexed_chunks: 1, namespaces_preserved_against_rules: 2, namespaces_reassigned: 0 },
        { indexed_chunks: 1, namespaces_preserved_against_rules: 3, namespaces_reassigned: 0 },
      ],
    });

    await window.mdReindexAll(null);

    // 2 + 3: the per-root counters have to be summed, not read off the first.
    expect(toasts.some((toast) => toast.message.includes('5'))).toBe(true);
  });

  it('sums nothing into a toast when no root preserved against its rules', async () => {
    window.api = async () => ({
      errors: [],
      results: [{ indexed_chunks: 1, namespaces_preserved_against_rules: 0 }],
    });

    await window.mdReindexAll(null);

    expect(toasts.every((toast) => !toast.message.includes('--reassign-namespaces'))).toBe(true);
  });

  // Auto-index on "add memory dir" is a real index run, and every status
  // branch of its outcome renderer returns — so the advisory has to be
  // reported before them or whichever branch fires drops it. #2115.
  it('reports the advisory from the auto-index add outcome', () => {
    window._showMemoryDirAddOutcome(
      { index_status: 'success', indexed: { indexed_chunks: 0, total_files: 1, chunks_missing_vectors: 6 } },
      '/tmp/memories',
    );

    expect(toasts.some((toast) => toast.message.includes('6'))).toBe(true);
  });

  it('reports it even when the add run finished partial', () => {
    window._showMemoryDirAddOutcome(
      {
        index_status: 'partial',
        indexed: {
          indexed_chunks: 1,
          total_files: 2,
          errors: ['broken.md: boom'],
          chunks_missing_vectors: 3,
        },
      },
      '/tmp/memories',
    );

    expect(toasts.some((toast) => toast.message.includes('3'))).toBe(true);
  });

  it('stays quiet on an add run with nothing to advise', () => {
    window._showMemoryDirAddOutcome(
      { index_status: 'success', indexed: { indexed_chunks: 2, total_files: 1 } },
      '/tmp/memories',
    );

    expect(toasts.every((toast) => !toast.message.includes('--force'))).toBe(true);
  });
});

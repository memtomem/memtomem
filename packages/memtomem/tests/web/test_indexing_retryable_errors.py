"""Browser contracts for classified indexing failures (issue #2026)."""

from __future__ import annotations

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


def _goto_ready(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    page.goto(mm_web_url)
    page.wait_for_function(
        """() => typeof classifyIndexingErrors === 'function'
          && typeof indexingErrorToast === 'function'
          && t('toast.indexing_retryable_errors', {
            retryable: 1,
            errors: 1,
            first: 'probe',
          }) !== 'toast.indexing_retryable_errors'""",
        timeout=5000,
    )


def test_classifier_preserves_order_duplicates_and_unknown_fallback(page, mm_web_url: str) -> None:
    _goto_ready(page, mm_web_url)

    result = page.evaluate(
        """() => {
          const pick = (value) => ({
            errors: value.errors,
            retryableErrors: value.retryableErrors,
            otherErrors: value.otherErrors,
            classificationKnown: value.classificationKnown,
            hasRetryable: value.hasRetryable,
            hasOther: value.hasOther,
          });
          return {
            permanent: pick(classifyIndexingErrors({
              errors: ['malformed'],
              retryable_errors: [],
            })),
            retryable: pick(classifyIndexingErrors({
              errors: ['store unavailable'],
              retryable_errors: ['store unavailable'],
            })),
            mixed: pick(classifyIndexingErrors({
              errors: ['malformed', 'store unavailable'],
              retryable_errors: ['store unavailable'],
            })),
            duplicate: pick(classifyIndexingErrors({
              errors: ['same', 'same'],
              retryable_errors: ['same'],
            })),
            unknown: pick(classifyIndexingErrors({ errors: ['legacy failure'] })),
            retryToast: indexingErrorToast('Base failure.', {
              errors: ['store unavailable'],
              retryable_errors: ['store unavailable'],
            }),
            mixedToast: indexingErrorToast('Base failure.', {
              errors: ['malformed', 'store unavailable'],
              retryable_errors: ['store unavailable'],
            }),
            permanentToast: indexingErrorToast('Base failure.', {
              errors: ['malformed'],
              retryable_errors: [],
            }),
          };
        }"""
    )

    assert result["permanent"]["classificationKnown"] is True
    assert result["permanent"]["hasRetryable"] is False
    assert result["permanent"]["otherErrors"] == ["malformed"]
    assert result["retryable"]["retryableErrors"] == ["store unavailable"]
    assert result["retryable"]["hasOther"] is False
    assert result["mixed"]["retryableErrors"] == ["store unavailable"]
    assert result["mixed"]["otherErrors"] == ["malformed"]
    assert result["duplicate"]["retryableErrors"] == ["same"]
    assert result["duplicate"]["otherErrors"] == ["same"]
    assert result["unknown"]["classificationKnown"] is False
    assert result["unknown"]["hasRetryable"] is False

    assert result["retryToast"]["type"] == "warning"
    assert "store unavailable" in result["retryToast"]["message"]
    assert result["mixedToast"]["type"] == "error"
    assert result["permanentToast"]["type"] == "error"
    assert result["permanentToast"]["message"] == "Base failure."


def test_memory_dir_add_offers_retry_only_for_known_retryable_failure(
    page, mm_web_url: str
) -> None:
    _goto_ready(page, mm_web_url)

    result = page.evaluate(
        """() => {
          const container = document.querySelector('#toast-container');
          const snapshot = () => {
            const toast = container.lastElementChild;
            return {
              className: toast.className,
              message: toast.querySelector('.toast-msg').textContent,
              action: toast.querySelector('.toast-action')?.textContent || null,
            };
          };

          container.replaceChildren();
          _showMemoryDirAddOutcome({
            index_status: 'partial',
            indexed: {
              indexed_chunks: 2,
              total_files: 1,
              errors: ['store unavailable'],
              retryable_errors: ['store unavailable'],
            },
          }, '/tmp/retryable');
          const retryable = snapshot();

          container.replaceChildren();
          _showMemoryDirAddOutcome({
            index_status: 'partial',
            indexed: {
              indexed_chunks: 0,
              total_files: 1,
              errors: ['malformed'],
              retryable_errors: [],
            },
          }, '/tmp/permanent');
          const permanent = snapshot();

          container.replaceChildren();
          _showMemoryDirAddOutcome({
            index_status: 'failed',
            indexed: { errors: ['legacy failure'] },
          }, '/tmp/legacy');
          const unknown = snapshot();

          return { retryable, permanent, unknown };
        }"""
    )

    assert "toast-warning" in result["retryable"]["className"]
    assert "store unavailable" in result["retryable"]["message"]
    assert result["retryable"]["action"] is not None
    assert "toast-error" in result["permanent"]["className"]
    assert result["permanent"]["action"] is None
    assert "toast-error" in result["unknown"]["className"]
    assert result["unknown"]["action"] is None


def test_retryable_guidance_reaches_index_source_and_memory_dir_consumers(
    page, mm_web_url: str
) -> None:
    _goto_ready(page, mm_web_url)

    result = page.evaluate(
        """async () => {
          const container = document.querySelector('#toast-container');
          const snapshot = () => {
            const toast = container.lastElementChild;
            return {
              className: toast.className,
              message: toast.querySelector('.toast-msg').textContent,
            };
          };
          const retryableStats = {
            total_files: 1,
            total_chunks: 1,
            indexed_chunks: 0,
            skipped_chunks: 0,
            deleted_chunks: 0,
            duration_ms: 1,
            errors: ['store unavailable'],
            retryable_errors: ['store unavailable'],
            blocked_files: 0,
            blocked_project_shared_files: 0,
            resolved_namespaces: [],
          };

          window.api = async (method, path) => {
            if (path === '/api/index') return retryableStats;
            if (path === '/api/reindex') {
              return { ...retryableStats, results: [] };
            }
            if (path === '/api/memory-dirs/status') return { dirs: [] };
            if (path.startsWith('/api/sources')) return { sources: [] };
            if (path.startsWith('/api/chunks')) return { chunks: [], total: 0 };
            return {};
          };

          container.replaceChildren();
          _renderIndexResult(retryableStats, {
            registerAsSource: false,
            path: '/tmp/index',
          });
          const indexResult = snapshot();

          container.replaceChildren();
          STATE.indexing = false;
          await _reindexSourceFile('/tmp/source.md', null);
          const sourceFile = snapshot();

          container.replaceChildren();
          STATE.indexing = false;
          await mdReindexAll(null);
          const memoryDirsAll = snapshot();

          window.fetchIndexStream = async (body, opts = {}) => {
            opts.onEvent({ type: 'complete', ...retryableStats });
          };
          container.replaceChildren();
          STATE.indexing = false;
          await mdReindexOne('/tmp/memory', null);
          const memoryDirComplete = snapshot();

          window.fetchIndexStream = async (body, opts = {}) => {
            opts.onEvent({
              type: 'error',
              message: 'store unavailable',
              retryable: true,
            });
          };
          container.replaceChildren();
          STATE.indexing = false;
          await mdReindexOne('/tmp/memory', null);
          const memoryDirFatal = snapshot();

          window.fetchIndexStream = async (body, opts = {}) => {
            opts.onEvent({
              type: 'error',
              message: 'store unavailable',
              retryable: true,
            });
          };
          document.querySelector('#index-path').value = '/tmp/index';
          container.replaceChildren();
          STATE.indexing = false;
          await runIndexStream();
          const indexFatal = snapshot();

          return {
            indexResult,
            sourceFile,
            memoryDirsAll,
            memoryDirComplete,
            memoryDirFatal,
            indexFatal,
          };
        }"""
    )

    for consumer, toast in result.items():
        assert "toast-warning" in toast["className"], consumer
        assert "store unavailable" in toast["message"], consumer


@pytest.mark.parametrize(
    ("response", "expected_disabled", "expected_done", "expected_class"),
    [
        (
            {
                "errors": ["store unavailable"],
                "retryable_errors": ["store unavailable"],
                "results": [],
            },
            False,
            False,
            "toast-warning",
        ),
        ({"errors": [], "retryable_errors": [], "results": []}, True, True, "toast-success"),
    ],
)
def test_settings_reindex_is_done_only_after_clean_success(
    page,
    mm_web_url: str,
    response: dict,
    expected_disabled: bool,
    expected_done: bool,
    expected_class: str,
) -> None:
    _goto_ready(page, mm_web_url)

    result = page.evaluate(
        """async (response) => {
          const container = document.querySelector('#toast-container');
          container.replaceChildren();
          document.querySelector('.config-reindex-warn')?.remove();
          window.api = async (method, path) => {
            if (path === '/api/reindex?force=true') return response;
            return {};
          };
          _showReindexWarning([{ field: 'indexing.max_chunk_tokens' }]);
          const btn = document.querySelector('#cfg-reindex-btn');
          btn.click();
          for (let i = 0; i < 100 && btn.classList.contains('btn-loading'); i++) {
            await new Promise((resolve) => setTimeout(resolve, 5));
          }
          const toast = container.lastElementChild;
          return {
            disabled: btn.disabled,
            text: btn.textContent,
            doneText: t('common.done'),
            className: toast.className,
            message: toast.querySelector('.toast-msg').textContent,
          };
        }""",
        response,
    )

    assert result["disabled"] is expected_disabled
    assert (result["text"] == result["doneText"]) is expected_done
    assert expected_class in result["className"]
    if response["errors"]:
        assert "store unavailable" in result["message"]

/* memtomem chunk-progress renderer — shared throttle helper (#659)
 *
 * Extracted from ``runIndexStream`` (app.js) and ``mdReindexOne``
 * (sources-memory-dirs.js) once the rule-of-three threshold was met
 * (#659 deferral lifted). Both call-sites consume ``chunk_progress``
 * SSE events from ``/api/index/stream`` and share the same shape:
 *
 *   - 100ms gap on intermediate ticks — a 250-chunk file at
 *     batch_size=64 emits ~4 events; a 2000-chunk pathological case
 *     ~32. The throttle keeps mid-file DOM writes off the layout
 *     hot path.
 *   - Final-tick bypass (``done >= total``) so the user actually
 *     sees ``(N/N)`` before the next file boundary overwrites the
 *     slot.
 *   - Reset on ``progress`` event boundary so the first chunk of
 *     the next file renders immediately.
 *
 * Depends on globals ``t`` (i18n.js) and ``basename`` (app.js) at
 * call time. Load order in index.html: i18n.js → app.js →
 * chunk-progress.js → sources-memory-dirs.js.
 */
'use strict';

(function () {
  // ``onChunk`` returns true when the DOM was written, false on a
  // throttle skip or detached target — callers can flip ancillary
  // state (e.g. a "label currently shows chunk text" flag, see
  // ``_metaIsChunkLabel`` in sources-memory-dirs.js) only on truthy
  // returns.
  function makeChunkProgressRenderer({ targetEl, formatKey }) {
    let lastRender = 0;
    return {
      onChunk(event) {
        if (!targetEl || !targetEl.isConnected) return false;
        const now = (typeof performance !== 'undefined' && performance.now)
          ? performance.now() : Date.now();
        const isFinal = event.chunks_done >= event.chunks_total;
        if (!isFinal && now - lastRender < 100) return false;
        lastRender = now;
        targetEl.textContent = t(formatKey, {
          file: basename(event.file),
          done: event.chunks_done,
          total: event.chunks_total,
        });
        return true;
      },
      onProgressBoundary() { lastRender = 0; },
      onCleanup() { lastRender = 0; },
    };
  }

  if (typeof window !== 'undefined') {
    window.makeChunkProgressRenderer = makeChunkProgressRenderer;
  }
})();

/* JSDOM bootstrap for memtomem static modules.
 *
 * The static modules ship verbatim — no build step — so tests load the
 * production ``index.html`` into JSDOM, strip its ``<script>`` tags so
 * nothing auto-executes, and then inject the requested modules in a
 * controlled order. ``runScripts: 'dangerously'`` is required for
 * dynamically appended ``<script>`` elements to actually execute inside
 * the JSDOM window.
 *
 * The locale fetch goes to ``/locales/{lang}.json``; we serve those from
 * disk so ``I18N`` boots with real translation maps and ``t()`` returns
 * something other than the raw key fallback.
 */

import { JSDOM } from 'jsdom';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const STATIC_DIR = path.resolve(HERE, '../../src/memtomem/web/static');

function readStatic(rel) {
  return fs.readFileSync(path.join(STATIC_DIR, rel), 'utf-8');
}

function jsonResponse(body, ok = true, status = 200) {
  // ``i18n.js`` only reads ``resp.ok`` and ``resp.json()``, so a plain
  // duck-typed object is enough — ``window.Response`` isn't exposed by
  // JSDOM and Node's global ``Response`` doesn't always reach the realm.
  return {
    ok,
    status,
    json: async () => JSON.parse(body),
    text: async () => body,
  };
}

function makeFetchStub(apiResponses = {}) {
  return async function fetchStub(input) {
    const url = typeof input === 'string' ? input : input?.url;
    if (url && url.startsWith('/locales/')) {
      const lang = url.replace('/locales/', '').replace('.json', '').split('?')[0];
      const file = path.join(STATIC_DIR, 'locales', `${lang}.json`);
      if (fs.existsSync(file)) {
        return jsonResponse(fs.readFileSync(file, 'utf-8'));
      }
      return jsonResponse('{}', false, 404);
    }
    // Test-supplied API payloads, keyed by pathname (query string ignored)
    // so a test can seed e.g. ``/api/embedding-status`` before app.js's
    // module-load fetch fires. Falls through to the empty-200 default.
    if (url) {
      const pathname = url.split('?')[0];
      if (Object.prototype.hasOwnProperty.call(apiResponses, pathname)) {
        return jsonResponse(JSON.stringify(apiResponses[pathname]));
      }
    }
    // Any other URL — return an empty 200 so app.js init paths that
    // happen to be triggered (e.g. a langchange listener that calls
    // ``loadStats``) don't blow up the test on an unrelated network
    // boundary. Tests should not depend on real network.
    return jsonResponse('{}');
  };
}

function shimBrowserAPIs(window) {
  // ``initTheme`` in ``app.js`` queries ``matchMedia('(prefers-color-scheme:
  // light)')`` at top-level. JSDOM doesn't ship ``matchMedia``; missing it
  // raises a TypeError mid-script which leaves later ``const`` bindings
  // (``_SOURCES_VENDORS`` etc.) in the temporal dead zone, breaking every
  // function that references them.
  if (!window.matchMedia) {
    window.matchMedia = (query) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
}

/**
 * Boot a JSDOM with the production HTML and the requested static modules.
 *
 * @param {object} opts
 * @param {string[]} [opts.scripts] — module filenames under ``static/``
 *   to inject, in order. Defaults to ``['i18n.js', 'app.js']``.
 * @param {object} [opts.state] — properties merged into ``window.STATE``
 *   after scripts load (e.g. ``{ memoryDirs: [...] }``).
 * @param {object} [opts.apiResponses] — map of pathname → JSON payload the
 *   fetch stub returns for matching requests (query string ignored), e.g.
 *   ``{ '/api/embedding-status': { has_mismatch: true, ... } }``. Lets a
 *   test seed an endpoint before app.js's module-load fetches fire.
 * @returns {Promise<JSDOM>}
 */
export async function bootApp({
  scripts = ['i18n.js', 'app.js'],
  state = {},
  apiResponses = {},
} = {}) {
  let html = readStatic('index.html');
  // Strip every <script ...>...</script> element so JSDOM's loader
  // doesn't try to fetch ``/vendor/*.js`` etc. We re-inject only the
  // modules under test below.
  html = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');

  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'http://localhost/',
    pretendToBeVisual: true,
  });
  const { window } = dom;

  shimBrowserAPIs(window);
  window.fetch = makeFetchStub(apiResponses);

  for (const filename of scripts) {
    const code = readStatic(filename);
    const el = window.document.createElement('script');
    el.textContent = code;
    window.document.body.appendChild(el);
  }

  // Top-level ``const`` / ``let`` declarations don't populate the
  // global object in browser script semantics — they live in the
  // "global lexical environment" — so ``window.STATE`` and
  // ``window.I18N`` are ``undefined`` from outside even though the
  // identifiers exist inside the realm. The bootstrap script below
  // runs in the same realm as the loaded modules, can read the
  // lexical bindings, and lifts the ones tests typically need onto
  // ``window``. Function declarations (``_renderMemorySourceTree``
  // etc.) already populate ``window`` so they don't need re-exposing.
  //
  // We also stub a few sibling-module identifiers that ``app.js`` calls
  // from its ``DOMContentLoaded`` handler (``_initTabHelp`` from
  // settings-harness.js, ``_indexingHydrateFromServer``,
  // ``_modelReadinessHydrate``). The stubs sit on ``window`` so the
  // ``var/function``-resolution lookup the handler does at fire-time
  // finds them. JSDOM defers DOMContentLoaded to a later microtask, so
  // setting the stubs synchronously here lands before the handler runs.
  //
  // Extend the stub list below when adding a new top-level call to the
  // DCL handler in ``app.js`` — otherwise tests fail loudly with
  // ``X is not a function`` mid-init rather than the actual assertion.
  const expose = window.document.createElement('script');
  expose.textContent = `
    try { window.STATE = STATE; } catch (e) {}
    try { window.I18N = I18N; } catch (e) {}
    try { window.t = t; } catch (e) {}
    for (const name of [
      '_initTabHelp',
      '_indexingHydrateFromServer',
      '_modelReadinessHydrate',
      'loadPrivacyPatterns',
      'initUiMode',
      'renderRecentChips',
    ]) {
      if (typeof window[name] === 'undefined') {
        window[name] = (name === 'initUiMode')
          ? (async () => {})
          : (() => {});
      }
    }
  `;
  window.document.body.appendChild(expose);

  if (window.STATE && state && typeof state === 'object') {
    Object.assign(window.STATE, state);
  }

  return dom;
}

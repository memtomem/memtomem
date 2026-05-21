/* Regression guards for purpose-scoped folder picker discovery (#1015). */

import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { bootApp, STATIC_DIR } from './setup/jsdom-app.mjs';

function jsonResponse(body, ok = true, status = 200) {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

async function waitFor(predicate) {
  for (let i = 0; i < 20; i += 1) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  throw new Error('timed out waiting for condition');
}

describe('PathPicker purpose', () => {
  it('keeps the default picker request on the legacy index scope', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'path-picker.js'],
    });
    const { window } = dom;
    const requests = [];
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      requests.push(url);
      if (url.startsWith('/api/fs/list')) {
        return jsonResponse({ path: null, parent: null, is_root: true, entries: [] });
      }
      return jsonResponse({});
    };

    window.PathPicker.open();

    await waitFor(() => requests.includes('/api/fs/list'));
  });

  it('passes project purpose through to fs/list', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'path-picker.js'],
    });
    const { window } = dom;
    const requests = [];
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      requests.push(url);
      if (url === '/api/fs/list?purpose=project') {
        return jsonResponse({
          path: null,
          parent: null,
          is_root: true,
          entries: [{ name: 'project-a', path: '/tmp/project-a' }],
        });
      }
      if (url.startsWith('/api/fs/list')) {
        return jsonResponse({ path: '/tmp/project-a', parent: '/tmp', is_root: false, entries: [] });
      }
      return jsonResponse({});
    };

    window.PathPicker.open({ purpose: 'project' });

    await waitFor(() => window.document.querySelector('#path-picker-list li'));
    window.document.querySelector('#path-picker-list li').dispatchEvent(
      new window.Event('click', { bubbles: true }),
    );
    await waitFor(() => requests.includes('/api/fs/list?path=%2Ftmp%2Fproject-a&purpose=project'));
  });
});

describe('Context Gateway Add Project picker', () => {
  it('opens PathPicker with project purpose', async () => {
    const dom = await bootApp({
      scripts: ['i18n.js', 'app.js', 'context-gateway.js'],
    });
    const { window } = dom;
    let optsSeen = null;
    window.PathPicker = {
      open: (opts) => {
        optsSeen = opts;
      },
    };

    window.document
      .querySelector('.ctx-add-project-btn[data-type="skills"]')
      .dispatchEvent(new window.Event('click', { bubbles: true }));

    expect(optsSeen).toBeTruthy();
    expect(optsSeen.purpose).toBe('project');
    expect(typeof optsSeen.onSelect).toBe('function');
  });

  async function captureOnSelectToast({ scripts, response, lang }) {
    // Drive the Add Project ``onSelect`` callback with a stubbed POST
    // response and read the resulting toast text back from the DOM. The
    // i18n locale fetch in ``bootApp`` is async, so we await ``I18N.init``
    // before invoking the callback to make sure ``t()`` resolves to the
    // real translation map rather than the bare-key fallback.
    const dom = await bootApp({ scripts });
    const { window } = dom;
    await window.I18N.init();
    if (lang) await window.I18N.setLang(lang);

    const upstream = window.fetch;
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url === '/api/session') {
        return { ok: true, status: 200, json: async () => ({ csrf: 'test' }) };
      }
      if (url === '/api/context/known-projects') {
        return { ok: true, status: 200, json: async () => response };
      }
      return upstream(input, init);
    };

    let captured = null;
    window.PathPicker = {
      open: (opts) => { captured = opts; },
    };
    window.document
      .querySelector('.ctx-add-project-btn[data-type="skills"]')
      .dispatchEvent(new window.Event('click', { bubbles: true }));
    expect(captured).toBeTruthy();

    await captured.onSelect('/tmp/project-a');
    await waitFor(
      () => window.document.querySelector('#toast-container .toast-msg'),
    );
    const toast = window.document.querySelector('#toast-container .toast-msg');
    return { window, toast };
  }

  it('localizes the no_runtime_marker warning instead of showing server prose', async () => {
    // #1077: the POST /api/context/known-projects route returns a stable
    // ``warning_code`` ("no_runtime_marker") plus an English ``warning``
    // string for back-compat. Pre-fix the client showed ``data.warning``
    // verbatim — Korean users got an English toast. Pin the i18n-aware
    // branch by stubbing the response with a deliberately-distinct
    // English prose: the assertion compares against the localized
    // ``settings.ctx.add_project_warning_no_runtime_marker`` so a future
    // regression that re-introduces ``data.warning`` would fail.
    const { window, toast } = await captureOnSelectToast({
      scripts: ['i18n.js', 'app.js', 'path-picker.js', 'context-gateway.js'],
      response: {
        scope_id: 'scope-1',
        root: '/tmp/project-a',
        label: 'project-a',
        warning_code: 'no_runtime_marker',
        warning: 'ORIGINAL ENGLISH PROSE FROM SERVER',
      },
      lang: 'ko',
    });
    const koValue = window.I18N.t('settings.ctx.add_project_warning_no_runtime_marker');
    expect(toast.textContent).toBe(koValue);
    expect(toast.textContent).not.toBe('ORIGINAL ENGLISH PROSE FROM SERVER');
    // Symmetric pin (feedback_pin_invert_symmetric_assertion.md): assert
    // the KO toast differs from the EN locale value too. Without this, a
    // ko.json regression that copy-pasted the English string into the
    // same key would still satisfy the ``toast === t()`` equality above
    // while recreating the user-visible #1077 failure. Read en.json off
    // disk (``I18N`` keeps its locale cache in a closure) to avoid
    // coupling the test to specific prose.
    const enLocale = JSON.parse(
      fs.readFileSync(path.join(STATIC_DIR, 'locales/en.json'), 'utf-8'),
    );
    const enValue = enLocale['settings.ctx.add_project_warning_no_runtime_marker'];
    expect(enValue).toBeTruthy();
    expect(koValue).not.toBe(enValue);
  });

  it('falls back to data.warning when warning_code is unknown to the client', async () => {
    // Forward-compat guard: a future server may emit a code this client
    // doesn't have a translation for yet. The fallback shape is "use the
    // server prose rather than the raw lookup key" — without this branch
    // users would see a bare ``settings.ctx.add_project_warning_xxx``
    // string. (The English locale stays the default so we don't need to
    // setLang here.)
    const { toast } = await captureOnSelectToast({
      scripts: ['i18n.js', 'app.js', 'path-picker.js', 'context-gateway.js'],
      response: {
        scope_id: 'scope-2',
        root: '/tmp/project-b',
        label: 'project-b',
        warning_code: 'future_unknown_code',
        warning: 'fallback prose for unknown code',
      },
    });
    expect(toast.textContent).toBe('fallback prose for unknown code');
    expect(toast.textContent).not.toBe(
      'settings.ctx.add_project_warning_future_unknown_code',
    );
  });
});

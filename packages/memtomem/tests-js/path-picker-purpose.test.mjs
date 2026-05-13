/* Regression guards for purpose-scoped folder picker discovery (#1015). */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

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
});

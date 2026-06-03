/* ADR-0022 detail-panel version/label manager (PR3).
 *
 * Drives the real ``loadCtxDetail`` + ``_ctxLoadVersions`` against a stubbed
 * fetch and asserts the version section renders for dir-layout agents/commands,
 * the freeze/promote/delete buttons hit the right routes, a flat-layout artifact
 * shows the migrate hint, and skills never mount the section at all.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const flush = () => new Promise((r) => setTimeout(r, 0));

const VERSIONS_PAYLOAD = {
  name: 'demo',
  artifact_type: 'agents',
  target_scope: 'project_shared',
  layout: 'dir',
  versions: [
    { tag: 'v2', created_at: '2026-06-03T11:00:00Z', note: 'stable' },
    { tag: 'v1', created_at: '2026-06-03T09:00:00Z', note: '' },
  ],
  labels: { production: 'v2' },
  has_versions: true,
  migrate_required: false,
};

function makeStub(window, calls, { versionsPayload = VERSIONS_PAYLOAD } = {}) {
  window.ensureCsrfToken = async () => 'tok-123';
  const upstream = window.fetch;
  window.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    // Pass non-context URLs (e.g. ``/locales/ko.json`` on a langchange)
    // through to the real fetch so locale loads aren't swallowed.
    if (!url.includes('/api/context/')) return upstream(input, init);
    const method = (init && init.method) || 'GET';
    calls.push({ url, method, body: init && init.body });
    // Label promote (PUT) / delete (DELETE).
    if (url.includes('/labels/')) {
      return { ok: true, status: 200, json: async () => ({ labels: {} }) };
    }
    // Versions list (GET) / freeze (POST). Checked before the generic detail
    // regex so the 5-segment ``/versions`` URL routes here.
    if (/\/versions(\?|$)/.test(url)) {
      if (method === 'POST') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ version: { tag: 'v3', created_at: '', note: '' } }),
        };
      }
      return { ok: true, status: 200, json: async () => versionsPayload };
    }
    // Canonical detail (``GET /api/context/{type}/{name}``).
    if (/\/api\/context\/[^/]+\/[^/]+(\?|$)/.test(url)) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          name: 'demo',
          content: '# demo\n',
          mtime_ns: '1',
          files: [],
          fields: {},
          layout: 'dir',
          target_scope: 'project_shared',
        }),
      };
    }
    if (url.endsWith('/diff')) {
      return { ok: true, status: 200, json: async () => ({ runtimes: [], canonical_content: '' }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
}

async function bootDetail(type, name, calls, opts) {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'context-gateway.js'] });
  makeStub(dom.window, calls, opts || {});
  await dom.window.loadCtxDetail(type, name);
  await flush();
  await flush();
  return dom;
}

describe('ADR-0022 detail version manager', () => {
  it('renders version rows + the production label chip for a dir-layout agent', async () => {
    const calls = [];
    const { window } = await bootDetail('agents', 'demo', calls);
    const detailEl = window.document.getElementById('ctx-agents-detail');
    const section = detailEl.querySelector('.ctx-detail-versions');
    expect(section).not.toBeNull();
    expect(section.hidden).toBe(false);

    const rows = section.querySelectorAll('.ctx-version-row');
    expect(rows.length).toBe(2);
    // Newest first → v2 row carries the production label chip.
    expect(rows[0].dataset.tag).toBe('v2');
    const chip = rows[0].querySelector('.ctx-version-label-chip[data-label="production"]');
    expect(chip).not.toBeNull();
    // v1 row has no label.
    expect(rows[1].querySelector('.ctx-version-label-chip')).toBeNull();
  });

  it('freeze button POSTs to the versions route', async () => {
    const calls = [];
    const { window } = await bootDetail('agents', 'demo', calls);
    const detailEl = window.document.getElementById('ctx-agents-detail');
    const freezeBtn = detailEl.querySelector('.ctx-version-freeze-btn');
    expect(freezeBtn).not.toBeNull();

    const before = calls.length;
    freezeBtn.click();
    await flush();
    await flush();

    const posted = calls
      .slice(before)
      .find((c) => c.method === 'POST' && /\/api\/context\/agents\/demo\/versions(\?|$)/.test(c.url));
    expect(posted).toBeTruthy();
  });

  it('promote button PUTs the selected label to the row version', async () => {
    const calls = [];
    const { window } = await bootDetail('agents', 'demo', calls);
    const detailEl = window.document.getElementById('ctx-agents-detail');
    // v1 row: select 'staging' and promote.
    const v1Row = detailEl.querySelector('.ctx-version-row[data-tag="v1"]');
    const select = v1Row.querySelector('.ctx-version-label-select');
    select.value = 'staging';
    const promoteBtn = v1Row.querySelector('.ctx-version-promote-btn');

    const before = calls.length;
    promoteBtn.click();
    await flush();
    await flush();

    const put = calls
      .slice(before)
      .find((c) => c.method === 'PUT' && c.url.includes('/labels/staging'));
    expect(put).toBeTruthy();
    expect(JSON.parse(put.body)).toEqual({ version: 'v1' });
  });

  it('label remove button DELETEs the pointer', async () => {
    const calls = [];
    const { window } = await bootDetail('agents', 'demo', calls);
    const detailEl = window.document.getElementById('ctx-agents-detail');
    const removeBtn = detailEl.querySelector('.ctx-version-label-remove[data-label="production"]');
    expect(removeBtn).not.toBeNull();

    const before = calls.length;
    removeBtn.click();
    await flush();
    await flush();

    const del = calls
      .slice(before)
      .find((c) => c.method === 'DELETE' && c.url.includes('/labels/production'));
    expect(del).toBeTruthy();
  });

  it('flat-layout artifact shows the migrate hint, no version rows', async () => {
    const calls = [];
    const flatPayload = {
      name: 'legacy',
      artifact_type: 'agents',
      target_scope: 'project_shared',
      layout: 'flat',
      versions: [],
      labels: {},
      has_versions: false,
      migrate_required: true,
    };
    const { window } = await bootDetail('agents', 'legacy', calls, { versionsPayload: flatPayload });
    const detailEl = window.document.getElementById('ctx-agents-detail');
    const section = detailEl.querySelector('.ctx-detail-versions');
    expect(section.hidden).toBe(false);
    expect(section.querySelectorAll('.ctx-version-row').length).toBe(0);
    expect(section.querySelector('.ctx-version-empty')).not.toBeNull();
  });

  it('skills never mount the version section', async () => {
    const calls = [];
    const { window } = await bootDetail('skills', 'demo', calls);
    const detailEl = window.document.getElementById('ctx-skills-detail');
    expect(detailEl.querySelector('.ctx-detail-versions')).toBeNull();
  });

  it('re-translates the freeze button text on langchange (data-i18n)', async () => {
    const calls = [];
    const { window } = await bootDetail('agents', 'demo', calls);
    const detailEl = window.document.getElementById('ctx-agents-detail');
    const freezeBtn = detailEl.querySelector('.ctx-version-freeze-btn');
    expect(freezeBtn.textContent).toBe('Freeze current');
    // applyDOM walks the async-painted section and re-translates data-i18n
    // nodes, so a langchange flips the button text without a detail re-render.
    await window.I18N.setLang('ko');
    expect(freezeBtn.textContent).toBe('현재 고정');
    await window.I18N.setLang('en');
    expect(freezeBtn.textContent).toBe('Freeze current');
  });
});

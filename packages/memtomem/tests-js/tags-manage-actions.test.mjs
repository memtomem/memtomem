/* Tags-tab manage actions — rename / merge / delete (#688 PR2).
 *
 * Covers the CLI-glue-free UI flow: the hover action buttons on each tag
 * row open the tag-manage modal, every action runs a dry-run preview first
 * (count + sample) and only writes on an explicit confirm, rename/merge
 * collect their value before previewing, and a backend 400 keeps the modal
 * open so the user can correct the value. The backend routes + service are
 * covered by the Python suite; here we pin the browser wiring.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

function json(obj, ok = true, status = 200) {
  return { ok, status, json: async () => obj, text: async () => JSON.stringify(obj) };
}

function tagOp(tag, count, dryRun) {
  return {
    tag,
    affected_chunks: count,
    dry_run: dryRun,
    samples: dryRun
      ? [
          { chunk_id: 'c1', source_file: '/n/a.md', content_preview: 'alpha preview', current_tags: [tag] },
          { chunk_id: 'c2', source_file: '/n/b.md', content_preview: 'beta preview', current_tags: [tag, 'keep'] },
        ]
      : [],
  };
}

describe('Tags manage actions (#688)', () => {
  let window, document, calls;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;
    document = window.document;
    calls = [];

    const original = window.fetch;
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      const method = (init?.method || 'GET').toUpperCase();
      const body = init?.body ? JSON.parse(init.body) : null;
      const u = url.split('?')[0];
      const isDry = url.includes('dry_run=true');

      if (u === '/api/tags' && method === 'GET') {
        return json({
          tags: [{ tag: 'alpha', count: 3 }, { tag: 'beta', count: 1 }],
          total: 2,
          offset: 0,
          limit: 100,
        });
      }
      if (u.startsWith('/api/tags/') && method === 'PUT') {
        const src = decodeURIComponent(u.slice('/api/tags/'.length));
        calls.push({ method, url, body, isDry });
        // Mirror the service's same-name reject (HTTP 400).
        if (body && body.new_name === src) return json({ detail: 'identical' }, false, 400);
        return json(tagOp(body.new_name, 3, isDry));
      }
      if (u.startsWith('/api/tags/') && method === 'DELETE') {
        const src = decodeURIComponent(u.slice('/api/tags/'.length));
        calls.push({ method, url, body, isDry });
        return json(tagOp(src, 3, isDry));
      }
      if (u === '/api/tags/merge' && method === 'POST') {
        calls.push({ method, url, body, isDry });
        return json(tagOp(body.target, 2, isDry));
      }
      return original(input, init);
    };
  });

  async function flush() {
    for (let i = 0; i < 12; i++) await new Promise(r => setTimeout(r, 0));
  }

  async function renderRows() {
    await window.loadTags();
    await flush();
  }

  function rowFor(tag) {
    return [...document.querySelectorAll('.tag-row')].find(
      r => r.querySelector('.tag-name')?.textContent === tag
    );
  }

  const modal = () => document.getElementById('tag-manage-modal');
  const okBtn = () => document.getElementById('tag-manage-ok-btn');
  const writes = () => calls.filter(c => !c.isDry);
  const dryRuns = () => calls.filter(c => c.isDry);

  it('renders a hover action menu (rename / merge / delete) per tag row', async () => {
    await renderRows();
    const row = rowFor('alpha');
    expect(row).toBeTruthy();
    expect(row.querySelector('[data-act="rename"]')).toBeTruthy();
    expect(row.querySelector('[data-act="merge"]')).toBeTruthy();
    expect(row.querySelector('[data-act="delete"]')).toBeTruthy();
  });

  it('delete: dry-run preview first, applies only on confirm', async () => {
    await renderRows();
    rowFor('alpha').querySelector('[data-act="delete"]').click();
    await flush();

    expect(modal().hidden).toBe(false);
    expect(document.getElementById('tag-manage-impact').textContent).toContain('3');
    expect(document.querySelectorAll('.tag-manage-sample').length).toBe(2);
    // Preview is a dry-run; nothing has been written yet.
    expect(dryRuns().length).toBe(1);
    expect(writes().length).toBe(0);

    okBtn().click();
    await flush();

    const applied = writes();
    expect(applied.length).toBe(1);
    expect(applied[0].method).toBe('DELETE');
    expect(applied[0].url).toContain('/api/tags/alpha');
    expect(modal().hidden).toBe(true);
  });

  it('rename: input phase → preview → apply with new_name', async () => {
    await renderRows();
    rowFor('alpha').querySelector('[data-act="rename"]').click();
    await flush();

    expect(modal().hidden).toBe(false);
    expect(document.getElementById('tag-manage-input-row').hidden).toBe(false);
    // Still in input phase — no API call yet.
    expect(calls.length).toBe(0);

    document.getElementById('tag-manage-input').value = 'renamed';
    okBtn().click(); // Preview
    await flush();
    expect(calls.filter(c => c.method === 'PUT' && c.isDry).length).toBe(1);
    expect(document.getElementById('tag-manage-impact').textContent).toContain('3');
    expect(writes().length).toBe(0);

    okBtn().click(); // Apply
    await flush();
    const applied = calls.filter(c => c.method === 'PUT' && !c.isDry);
    expect(applied.length).toBe(1);
    expect(applied[0].body.new_name).toBe('renamed');
    expect(modal().hidden).toBe(true);
  });

  it('merge: input target → preview → apply with sources+target', async () => {
    await renderRows();
    rowFor('alpha').querySelector('[data-act="merge"]').click();
    await flush();

    document.getElementById('tag-manage-input').value = 'beta';
    okBtn().click(); // Preview
    await flush();
    expect(calls.filter(c => c.url.includes('/api/tags/merge') && c.isDry).length).toBe(1);
    expect(document.getElementById('tag-manage-impact').textContent).toContain('2');

    okBtn().click(); // Apply
    await flush();
    const applied = calls.filter(c => c.url.includes('/api/tags/merge') && !c.isDry);
    expect(applied.length).toBe(1);
    expect(applied[0].body.sources).toEqual(['alpha']);
    expect(applied[0].body.target).toBe('beta');
  });

  it('rename: empty value is rejected before any dry-run', async () => {
    await renderRows();
    rowFor('alpha').querySelector('[data-act="rename"]').click();
    await flush();

    document.getElementById('tag-manage-input').value = '   ';
    okBtn().click();
    await flush();

    expect(calls.length).toBe(0); // no dry-run fired
    expect(document.getElementById('tag-manage-error').hidden).toBe(false);
    expect(modal().hidden).toBe(false);
  });

  it('rename: backend 400 (same name) surfaces inline and keeps the modal open', async () => {
    await renderRows();
    rowFor('alpha').querySelector('[data-act="rename"]').click();
    await flush();

    document.getElementById('tag-manage-input').value = 'alpha'; // same-name → 400
    okBtn().click(); // Preview triggers the dry-run that 400s
    await flush();

    expect(document.getElementById('tag-manage-error').hidden).toBe(false);
    expect(modal().hidden).toBe(false);
    expect(writes().length).toBe(0); // never reached apply
  });

  it('cancel closes the modal without writing', async () => {
    await renderRows();
    rowFor('alpha').querySelector('[data-act="delete"]').click();
    await flush();
    expect(modal().hidden).toBe(false);

    document.getElementById('tag-manage-cancel-btn').click();
    await flush();
    expect(modal().hidden).toBe(true);
    expect(writes().length).toBe(0);
  });

  it('rename: editing after a preview requires a fresh dry-run and applies the NEW value', async () => {
    await renderRows();
    rowFor('alpha').querySelector('[data-act="rename"]').click();
    await flush();
    const input = document.getElementById('tag-manage-input');

    input.value = 'foo';
    okBtn().click(); // preview foo
    await flush();
    expect(calls.filter(c => c.method === 'PUT' && c.isDry).length).toBe(1);
    expect(okBtn().textContent).toBe(window.t('tags.manage_rename')); // apply label

    // Edit → reverts to the input phase (OK back to "Preview"), preview cleared.
    input.value = 'bar';
    input.dispatchEvent(new window.Event('input'));
    await flush();
    expect(okBtn().textContent).toBe(window.t('tags.manage_preview'));
    expect(document.getElementById('tag-manage-impact').textContent).toBe('');

    // OK now previews 'bar' (still no write), then a second OK applies 'bar'.
    okBtn().click();
    await flush();
    expect(writes().length).toBe(0);
    expect(calls.filter(c => c.method === 'PUT' && c.isDry).length).toBe(2);

    okBtn().click();
    await flush();
    const applied = calls.filter(c => c.method === 'PUT' && !c.isDry);
    expect(applied.length).toBe(1);
    expect(applied[0].body.new_name).toBe('bar'); // the previewed value, never 'foo'
  });

  it('drops a stale dry-run that resolves after the value was edited mid-flight', async () => {
    await renderRows();
    // Make the PUT dry-run hang so we can edit while it is in flight.
    let resolveDry;
    const original = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      const method = (init?.method || 'GET').toUpperCase();
      if (url.startsWith('/api/tags/') && method === 'PUT' && url.includes('dry_run=true')) {
        return new Promise(res => { resolveDry = () => res(json(tagOp('foo', 99, true))); });
      }
      return original(input, init);
    };

    rowFor('alpha').querySelector('[data-act="rename"]').click();
    await flush();
    const input = document.getElementById('tag-manage-input');
    input.value = 'foo';
    okBtn().click(); // dry-run for 'foo' starts but stays pending
    await flush();

    // Edit before the dry-run resolves — the in-flight response is now stale.
    input.value = 'bar';
    input.dispatchEvent(new window.Event('input'));
    await flush();

    resolveDry(); // stale 'foo' preview (99 chunks) resolves now
    await flush();

    // The stale preview must NOT take effect: still input phase, no 99 shown.
    expect(okBtn().textContent).toBe(window.t('tags.manage_preview'));
    expect(document.getElementById('tag-manage-impact').textContent).toBe('');
    expect(writes().length).toBe(0);
  });
});

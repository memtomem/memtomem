import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { spawnSync } from 'node:child_process';
import { bootApp, STATIC_DIR } from './setup/jsdom-app.mjs';

async function exportCsv(content, source = '/tmp/a,b"c.md') {
  const text = fs.readFileSync(path.join(STATIC_DIR, 'app.js'), 'utf8');
  const start = text.indexOf('function downloadResults(items, format)');
  const end = text.indexOf("qs('bulk-export-btn').addEventListener", start);
  let blob;
  const context = {
    Blob, document: { createElement: () => ({ click() {} }) },
    URL: { createObjectURL: b => { blob = b; return 'blob:test'; }, revokeObjectURL() {} },
    showToast() {}, t() { return ''; },
  };
  vm.createContext(context);
  vm.runInContext(text.slice(start, end), context);
  context.downloadResults([{ score: -0.25, chunk: {
    id: 'id', source_file: source, chunk_type: 'raw_text', content,
  } }], 'csv');
  const csv = await blob.text();
  // A standard CSV reader verifies the production serializer, rather than a
  // test-specific inverse that could share its escaping mistakes.
  const result = spawnSync('python3', ['-c',
    'import csv,io,json,sys; print(json.dumps(list(csv.reader(io.StringIO(sys.stdin.read())))))'],
    { input: csv, encoding: 'utf8' });
  expect(result.status).toBe(0);
  return JSON.parse(result.stdout);
}

describe('search-report CSV', () => {
  it('preserves commas, quotes and actual newlines as exactly five fields', async () => {
    const content = 'first\nsecond "quoted" 😀';
    const rows = await exportCsv(content);
    expect(rows[0]).toEqual(['id', 'source', 'type', 'score', 'content']);
    expect(rows[1]).toEqual(['id', '/tmp/a,b"c.md', 'raw_text', '-0.2500', content]);
  });
  it.each(['=1+1', '  +SUM(A1)', '-cmd', '@formula', '\tvalue', '\r\nvalue'])(
    'protects spreadsheet text %j without changing the numeric score', async text => {
      const [, row] = await exportCsv(text);
      expect(row[3]).toBe('-0.2500');
      expect(row[4]).toBe("'" + text);
    });
});

describe('restore mapping form', () => {
  it('sends explicit mapping/consent once and preserves drafts on failure', async () => {
    const { window: w } = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-maintenance.js'] });
    await w.I18N.init();
    const doc = w.document;
    Object.defineProperty(doc.getElementById('imp-file'), 'files', {
      value: [new w.File(['{}'], 'restore.json')],
    });
    doc.getElementById('imp-map-add').click();
    doc.querySelector('.imp-map-from').value = '/old';
    doc.querySelector('.imp-map-to').value = '/new';
    doc.getElementById('imp-confirm-shared').checked = true;
    const calls = [];
    let finish;
    w.csrfFetch = async (_url, opts) => {
      calls.push(opts.body);
      await new Promise(resolve => { finish = resolve; });
      return { ok: false, statusText: 'Refused', json: async () => ({ detail: 'Invalid bundle' }) };
    };
    w.showToast = () => {};
    const first = w.runImport();
    await w.runImport();
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0].get('project_root_map'))).toEqual({ '/old': '/new' });
    expect(calls[0].get('confirm_project_shared')).toBe('true');
    finish(); await first;
    expect(doc.querySelector('.imp-map-from').value).toBe('/old');
    expect(doc.querySelector('.imp-map-to').value).toBe('/new');
    expect(doc.getElementById('imp-confirm-shared').checked).toBe(true);
    expect(doc.getElementById('imp-btn').disabled).toBe(false);
    w.close();
  });
});

/* #3117: Find Similar reads neighbours from the namespace the Search tab is
 * filtered to.
 *
 * The similar-chunks route hides the system namespaces when no ``namespace``
 * is given, as a search does. So the panel must forward the Search tab's
 * ``#ns-filter`` value when one is selected (an operator browsing
 * ``agent-runtime:planner`` wants that agent's neighbours) and send no
 * ``namespace`` at all when the filter is "All Namespaces".
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const CHUNK_ID = 'a1b2c3d4-0000-4000-8000-000000000003';

async function bootSimilar() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
  const { window } = dom;
  const requests = [];
  const upstream = window.fetch;
  window.fetch = async (input, opts) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.includes(`/api/chunks/${CHUNK_ID}/similar`)) {
      requests.push(new URL(url, 'http://test'));
      return { ok: true, status: 200, json: async () => ({ results: [], total: 0 }) };
    }
    return upstream(input, opts);
  };
  await window.I18N.init();
  window.STATE.selectedChunkId = CHUNK_ID;
  return { window, requests };
}

function selectNamespace(window, value) {
  // A <select> only takes a value one of its options carries.
  const sel = window.document.getElementById('ns-filter');
  const opt = window.document.createElement('option');
  opt.value = value;
  opt.textContent = value;
  sel.appendChild(opt);
  sel.value = value;
  expect(sel.value).toBe(value);
}

async function clickFindSimilar(window) {
  window.document.getElementById('d-similar-btn').click();
  for (let i = 0; i < 20; i++) await new Promise((r) => window.setTimeout(r, 0));
}

describe('Find Similar — namespace (#3117)', () => {
  it('forwards the Search tab namespace filter', async () => {
    const { window, requests } = await bootSimilar();
    selectNamespace(window, 'agent-runtime:planner');

    await clickFindSimilar(window);

    expect(requests).toHaveLength(1);
    expect(requests[0].searchParams.get('namespace')).toBe('agent-runtime:planner');
    expect(requests[0].searchParams.get('top_k')).toBe('5');
  });

  it('sends no namespace when the filter is "All Namespaces"', async () => {
    const { window, requests } = await bootSimilar();
    expect(window.document.getElementById('ns-filter').value).toBe('');

    await clickFindSimilar(window);

    expect(requests).toHaveLength(1);
    expect(requests[0].searchParams.has('namespace')).toBe(false);
    expect(requests[0].searchParams.get('top_k')).toBe('5');
  });
});

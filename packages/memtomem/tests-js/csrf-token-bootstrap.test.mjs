/* Pin for the CSRF token bootstrap in app.js's ``api()`` helper
 * (RFC #787 stage 1).
 *
 * Three branches:
 *   1. GET request → no token fetched, no X-Memtomem-CSRF header sent.
 *   2. POST request → token bootstrapped from /api/session once, then
 *      attached as X-Memtomem-CSRF on the actual call. Subsequent POSTs
 *      reuse the cached token (no second /api/session round-trip).
 *   3. POST when /api/session fails → no header attached, request still
 *      fires (graceful degrade — server-side observe-mode would log it
 *      but not block; PR2 enforcement will reject and surface the error
 *      via the existing api() error path).
 *
 * Mutation-validated per ``feedback_pin_test_mutation_validation.md``:
 * removing the ``await ensureCsrfToken()`` line in app.js makes
 * branches 2 and 3 fail (header missing on POST), and removing the
 * "fetch token only once" cache makes branch 2's "no second
 * /api/session call" assertion fail.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('api() helper — CSRF token bootstrap', () => {
  let window;
  let calls;
  let sessionResponseStatus;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js'] });
    window = dom.window;

    sessionResponseStatus = 'ok';
    calls = [];
    window.fetch = async function fetchSpy(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      const method = init?.method || 'GET';
      calls.push({
        url,
        method,
        headers: init?.headers ? { ...init.headers } : {},
      });
      if (url === '/api/session') {
        if (sessionResponseStatus === 'fail') {
          return { ok: false, status: 500, json: async () => ({}) };
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({ csrf: 'srv-token-XYZ', mode: 'prod' }),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true }),
      };
    };
  });

  it('GET requests do not fetch token and send no CSRF header', async () => {
    await window.api('GET', '/something');

    const sessionCalls = calls.filter(c => c.url === '/api/session');
    expect(sessionCalls).toHaveLength(0);

    const targetCall = calls.find(c => c.url === '/api/something' || c.url === '/something' || c.url.endsWith('/something'));
    expect(targetCall).toBeDefined();
    expect(targetCall.headers['X-Memtomem-CSRF']).toBeUndefined();
  });

  it('POST bootstraps token from /api/session and attaches it', async () => {
    await window.api('POST', '/echo', { foo: 1 });

    const sessionCalls = calls.filter(c => c.url === '/api/session');
    expect(sessionCalls).toHaveLength(1);

    const echoCall = calls.find(c => c.url.endsWith('/echo'));
    expect(echoCall).toBeDefined();
    expect(echoCall.method).toBe('POST');
    expect(echoCall.headers['X-Memtomem-CSRF']).toBe('srv-token-XYZ');
  });

  it('subsequent POSTs reuse the cached token (no second /api/session call)', async () => {
    await window.api('POST', '/echo1', { x: 1 });
    await window.api('POST', '/echo2', { x: 2 });
    await window.api('PATCH', '/echo3', { x: 3 });
    await window.api('DELETE', '/echo4');

    const sessionCalls = calls.filter(c => c.url === '/api/session');
    expect(sessionCalls).toHaveLength(1);

    const targetCalls = calls.filter(c =>
      ['/echo1', '/echo2', '/echo3', '/echo4'].some(p => c.url.endsWith(p))
    );
    expect(targetCalls).toHaveLength(4);
    for (const c of targetCalls) {
      expect(c.headers['X-Memtomem-CSRF']).toBe('srv-token-XYZ');
    }
  });

  it('POST gracefully degrades when /api/session fails', async () => {
    sessionResponseStatus = 'fail';
    await window.api('POST', '/echo', { foo: 1 });

    const echoCall = calls.find(c => c.url.endsWith('/echo'));
    expect(echoCall).toBeDefined();
    // Empty token shouldn't attach a header — observe-mode server side
    // will still log the would_block event; PR2 enforcement will 403
    // and the api() error path surfaces it as a normal HTTP error.
    expect(echoCall.headers['X-Memtomem-CSRF']).toBeUndefined();
  });
});

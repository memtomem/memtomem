// Sessions harness panel (#1913): renders newly-exposed session metadata
// (title + summary-origin badge) and hardens every caller-controlled sink —
// the table (id, agent_id, namespace, summary, title, the data-id attribute)
// and the event renderer (content, event_type text + badge class, JSON
// metadata). The session id embeds a caller-supplied source (formation.py),
// so it is escaped like any field. Also pins the langchange re-render and the
// CSP-safe metadata toggle (data-action, not inline onclick).
import { describe, expect, it, vi } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

const XSS = '<img src=x onerror="window.__pwned = true">';
const ATTR_XSS = '"><img src=x onerror="window.__pwned = true">';

async function boot() {
  const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'settings-harness.js'] });
  await dom.window.I18N.init();
  return dom;
}

function session(overrides = {}) {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    agent_id: 'planner',
    namespace: 'agent-runtime:planner',
    started_at: '2026-07-22T00:00:00+00:00',
    ended_at: '2026-07-22T01:00:00+00:00',
    summary: 'did some work',
    metadata: {},
    ...overrides,
  };
}

describe('sessions panel — rendering', () => {
  it('renders the title column and the summary-origin badge per provenance', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      sessions: [
        session({ metadata: { title: 'Sprint planning', summary_provenance: 'exact' } }),
        session({ id: '22222222-2222-4222-8222-222222222222', metadata: { summary_provenance: 'fallback' } }),
        session({ id: '33333333-3333-4333-8333-333333333333', metadata: { summary_provenance: 'manual' } }),
        session({ id: '44444444-4444-4444-8444-444444444444', metadata: {} }),
        session({ id: '55555555-5555-4555-8555-555555555555', metadata: { summary_provenance: 'exact', provenance_incomplete: true } }),
      ],
      total: 5,
    }));

    await window.loadHarnessSessions();
    const list = window.document.getElementById('sessions-list');
    const rows = list.querySelectorAll('tbody tr');

    expect(rows[0].textContent).toContain('Sprint planning');
    expect(rows[0].textContent).toContain(window.t('settings.sessions.origin_exact'));
    expect(rows[1].textContent).toContain(window.t('settings.sessions.origin_fallback'));
    expect(rows[2].textContent).toContain(window.t('settings.sessions.origin_manual'));
    // absent provenance → no origin badge, title falls back to em dash
    expect(rows[3].textContent).not.toContain(window.t('settings.sessions.origin_exact'));
    expect(rows[3].querySelector('td:nth-child(2)').textContent.trim()).toBe('—');
    // incomplete rides alongside its origin
    expect(rows[4].textContent).toContain(window.t('settings.sessions.origin_exact'));
    expect(rows[4].textContent).toContain(window.t('settings.sessions.provenance_incomplete'));
  });

  it('shows an active badge for an unfinished session', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({ sessions: [session({ ended_at: null })], total: 1 }));
    await window.loadHarnessSessions();
    const list = window.document.getElementById('sessions-list');
    expect(list.querySelector('.badge-active').textContent).toBe(window.t('settings.sessions.active'));
  });
});

describe('sessions panel — XSS hardening', () => {
  it('escapes every caller-controlled table sink, including the id attribute', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      sessions: [session({
        id: ATTR_XSS,
        agent_id: XSS,
        namespace: XSS,
        summary: XSS,
        metadata: { title: XSS },
      })],
      total: 1,
    }));

    await window.loadHarnessSessions();
    const list = window.document.getElementById('sessions-list');

    expect(list.querySelector('img')).toBeNull();
    expect(window.__pwned).toBeUndefined();
    // the data-id attribute round-trips the raw id without breaking out
    const btn = list.querySelector('[data-action="session-events"]');
    expect(btn.dataset.id).toBe(ATTR_XSS);
    expect(list.textContent).toContain('<img src=x');
  });

  it('escapes event content, metadata, and whitelists the event-type badge class', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      events: [
        { event_type: 'add', content: XSS, metadata: { note: XSS }, created_at: '2026-07-22T00:00:00+00:00' },
        { event_type: '"><img src=x onerror="window.__pwned=true">', content: 'x', metadata: {}, created_at: '2026-07-22T00:00:00+00:00' },
      ],
    }));

    await window.showSessionEvents('11111111-1111-4111-8111-111111111111');
    const list = window.document.getElementById('session-events-list');

    expect(list.querySelector('img')).toBeNull();
    expect(window.__pwned).toBeUndefined();
    // known type → styled badge; hostile type → neutral badge, never a
    // class attribute break-out
    expect(list.querySelector('.badge-add')).toBeTruthy();
    expect(list.querySelector('.badge-muted')).toBeTruthy();
    // metadata is escaped text, and the toggle is CSP-safe (no inline onclick)
    const toggle = list.querySelector('[data-action="toggle-next"]');
    expect(toggle).toBeTruthy();
    expect(toggle.getAttribute('onclick')).toBeNull();
    expect(list.textContent).toContain('<img src=x');
  });

  it('toggles metadata visibility through the delegated handler', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      events: [{ event_type: 'add', content: 'c', metadata: { k: 'v' }, created_at: '2026-07-22T00:00:00+00:00' }],
    }));
    await window.showSessionEvents('11111111-1111-4111-8111-111111111111');
    const list = window.document.getElementById('session-events-list');
    const meta = list.querySelector('.harness-event-meta');
    expect(meta.hidden).toBe(true);
    list.querySelector('[data-action="toggle-next"]').click();
    expect(meta.hidden).toBe(false);
  });
});

describe('sessions panel — langchange', () => {
  it('re-localizes the table and keeps the open events-panel session id', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      sessions: [session({ metadata: { title: 'Sprint', summary_provenance: 'exact' } })],
      total: 1,
    }));
    await window.loadHarnessSessions();
    const list = window.document.getElementById('sessions-list');
    const enOrigin = window.t('settings.sessions.origin_exact');
    expect(list.textContent).toContain(enOrigin);

    window.api = vi.fn(async () => ({
      events: [{ event_type: 'add', content: 'c', metadata: {}, created_at: '2026-07-22T00:00:00+00:00' }],
    }));
    await window.showSessionEvents('abcdef01-1111-4111-8111-111111111111');

    await window.I18N.setLang('ko');

    const koOrigin = window.t('settings.sessions.origin_exact');
    expect(list.textContent).toContain(koOrigin);
    // the events-panel title keeps its session id across the toggle
    expect(window.document.getElementById('session-events-title').textContent).toContain('abcdef01');
  });

  it('re-localizes the empty state, not just a populated table', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({ sessions: [], total: 0 }));
    await window.loadHarnessSessions();
    const list = window.document.getElementById('sessions-list');
    expect(list.textContent).toContain(window.t('settings.sessions.empty'));

    await window.I18N.setLang('ko');
    // the Korean empty message, not the stale English one
    expect(list.textContent).toContain(window.t('settings.sessions.empty'));
    expect(list.textContent).toContain('세션');
  });

  it('re-localizes the error state', async () => {
    const { window } = await boot();
    window.STATE.uiMode = 'prod';
    // Only /api/sessions fails; other endpoints (the langchange dashboard
    // reload) resolve, so the error under test is the sessions load itself.
    window.api = vi.fn(async (_method, path) => {
      if (path.startsWith('/api/sessions')) throw new Error('boom');
      return {};
    });
    await window.loadHarnessSessions();
    const list = window.document.getElementById('sessions-list');
    expect(list.textContent).toContain(window.t('settings.sessions.load_failed'));

    await window.I18N.setLang('ko');
    expect(list.textContent).toContain(window.t('settings.sessions.load_failed'));
  });
});

describe('sessions panel — events cache isolation', () => {
  it('never repaints one session\'s events under another session\'s title', async () => {
    const { window } = await boot();
    // Session A resolves with events; then B is opened but its request never
    // resolves. A langchange must not repaint A's events beneath B's title.
    window.api = vi.fn(async () => ({
      events: [{ event_type: 'add', content: 'A-EVENT', metadata: {}, created_at: '2026-07-22T00:00:00+00:00' }],
    }));
    await window.showSessionEvents('aaaaaaaa-1111-4111-8111-111111111111');

    // Only B's /events request hangs; other calls (the langchange-triggered
    // dashboard reload) resolve, so the deferred below is B's, not theirs.
    let releaseB;
    window.api = vi.fn((_method, path) =>
      path.includes('/events')
        ? new Promise((res) => { releaseB = res; })
        : Promise.resolve({}));
    const pending = window.showSessionEvents('bbbbbbbb-2222-4222-8222-222222222222');

    await window.I18N.setLang('ko');

    const title = window.document.getElementById('session-events-title').textContent;
    const body = window.document.getElementById('session-events-list').textContent;
    expect(title).toContain('bbbbbbbb');
    expect(title).not.toContain('aaaaaaaa');
    expect(body).not.toContain('A-EVENT');

    // A late resolution of B's request still renders B (it was never superseded).
    releaseB({ events: [{ event_type: 'add', content: 'B-EVENT', metadata: {}, created_at: '2026-07-22T00:00:00+00:00' }] });
    await pending;
    expect(window.document.getElementById('session-events-list').textContent).toContain('B-EVENT');
  });

  it('fully percent-encodes a slash-bearing session id (no literal slash)', async () => {
    const { window } = await boot();
    const calls = [];
    window.api = vi.fn(async (_method, path) => { calls.push(path); return { events: [] }; });
    await window.showSessionEvents('external:a/../b:0123456789abcdef01234567');
    // The slash is encoded as %2F, not left literal — otherwise the browser
    // would canonicalize the /../ and request a different session. The ASGI
    // server decodes %2F back to / for the :path route.
    expect(calls[0]).toBe('/api/sessions/external%3Aa%2F..%2Fb%3A0123456789abcdef01234567/events');
    expect(calls[0]).toContain('%2F');
  });

  it('keeps the active event-type filter across a langchange repaint', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      events: [
        { event_type: 'add', content: 'ADD-EVENT', metadata: {}, created_at: '2026-07-22T00:00:00+00:00' },
        { event_type: 'query', content: 'QUERY-EVENT', metadata: {}, created_at: '2026-07-22T00:00:00+00:00' },
      ],
    }));
    await window.showSessionEvents('11111111-1111-4111-8111-111111111111');
    const list = window.document.getElementById('session-events-list');

    // filter to 'query'
    [...list.querySelectorAll('.harness-event-filter button')]
      .find(b => b.dataset.filter === 'query').click();
    let body = list.querySelector('.harness-events-body').textContent;
    expect(body).toContain('QUERY-EVENT');
    expect(body).not.toContain('ADD-EVENT');

    await window.I18N.setLang('ko');

    // the repaint keeps the query filter, not snapping back to all
    body = list.querySelector('.harness-events-body').textContent;
    expect(body).toContain('QUERY-EVENT');
    expect(body).not.toContain('ADD-EVENT');
    const active = list.querySelector('.harness-event-filter button.active');
    expect(active.dataset.filter).toBe('query');
  });

  it('supersedes an out-of-order sessions refresh', async () => {
    const { window } = await boot();
    // First refresh hangs; second resolves. The slow first must not paint
    // over the second's result.
    let releaseFirst;
    let call = 0;
    window.api = vi.fn(() => {
      call += 1;
      return call === 1
        ? new Promise((res) => { releaseFirst = res; })
        : Promise.resolve({ sessions: [session({ agent_id: 'SECOND' })], total: 1 });
    });
    const first = window.loadHarnessSessions();
    const second = window.loadHarnessSessions();
    await second;
    releaseFirst({ sessions: [session({ agent_id: 'FIRST' })], total: 1 });
    await first;

    const list = window.document.getElementById('sessions-list');
    expect(list.textContent).toContain('SECOND');
    expect(list.textContent).not.toContain('FIRST');
  });

  it('renders a styled badge for the error event type', async () => {
    const { window } = await boot();
    window.api = vi.fn(async () => ({
      events: [{ event_type: 'error', content: 'c', metadata: {}, created_at: '2026-07-22T00:00:00+00:00' }],
    }));
    await window.showSessionEvents('11111111-1111-4111-8111-111111111111');
    const list = window.document.getElementById('session-events-list');
    expect(list.querySelector('.badge-error')).toBeTruthy();
    expect(list.querySelector('.badge-muted')).toBeNull();
  });
});

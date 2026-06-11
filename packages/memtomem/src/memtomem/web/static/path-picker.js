/* memtomem folder picker — issue #582 4.12
 *
 * Powers the 📁 Browse button next to the Index tab's Folder-mode path
 * input. Calls /api/fs/list, renders breadcrumb + dir list inside the
 * shared .modal-overlay component, and writes the chosen absolute path
 * back into #index-path on Select. Navigation is bounded by the server's
 * allow-list (memory_dirs + ~); going outside requires closing the modal
 * and typing the path manually — the input itself stays free-form.
 *
 * Also exposes ``window.PathPicker.open({ purpose, onSelect })`` for
 * other surfaces (Context Gateway "Add Project") so they can reuse
 * the modal instead of falling back to ``window.prompt``. ``purpose``
 * is forwarded to /api/fs/list so each surface can use an appropriate
 * discovery scope. ``onSelect`` is invoked with the selected absolute
 * path; ``close`` is called by the picker itself once the callback
 * returns.
 */
'use strict';

(function () {
  let currentPath = null;     // null = roots view
  let currentEntries = [];
  let initialized = false;
  // Per-open callback. Cleared on close so a stale callback from a
  // previous invocation can't fire when the picker is reopened by the
  // default Index-tab path.
  let onSelectCb = null;
  let pickerPurpose = 'index';
  // Navigation sequence (#1247 id 28): list responses resolve out of order
  // under rapid clicks, and the last RESPONSE used to win regardless of
  // which directory was clicked last. Each ``navigate`` takes a ticket;
  // only the holder of the current ticket may paint. ``close`` bumps it so
  // a response landing after close can't paint (or steal focus) into the
  // hidden — or reopened — modal.
  let navSeq = 0;
  // Path of the last failed navigation — consumed by the in-modal Retry
  // button. ``null`` is a valid value (roots view), so the error VIEW
  // visibility, not this variable, signals the error state.
  let retryPath = null;

  function modal() { return qs('path-picker-modal'); }
  function listEl() { return qs('path-picker-list'); }
  function emptyEl() { return qs('path-picker-empty'); }
  function errorEl() { return qs('path-picker-error'); }
  function retryBtn() { return qs('path-picker-retry-btn'); }
  function crumbEl() { return qs('path-picker-breadcrumb'); }
  function selectBtn() { return qs('path-picker-select-btn'); }
  function cancelBtn() { return qs('path-picker-cancel-btn'); }

  function _t(key) {
    if (typeof I18N !== 'undefined' && I18N.t) return I18N.t(key);
    return key;
  }

  function _toast(message, type) {
    if (typeof showToast === 'function') showToast(message, type || 'error');
  }

  // Resolves to ``{ body }`` on success, else ``{ error: 'scope' | 'load' }``.
  // The two failure kinds need different navigate-side handling: a scope
  // refusal (422 outside_picker_scope) means "you can't go there" — the
  // current listing is still valid, keep it; a load failure means the view
  // the user asked for couldn't be produced — show the in-modal error +
  // Retry state. Deliberately SIDE-EFFECT-FREE (no toasts here): the caller
  // toasts only after its sequence guard accepts the result, so a stale
  // failure superseded by a newer navigation (or by close) stays fully
  // silent (Codex review on #1247 id 28).
  async function _fetchList(path) {
    const params = new URLSearchParams();
    if (path) params.set('path', path);
    if (pickerPurpose && pickerPurpose !== 'index') params.set('purpose', pickerPurpose);
    const query = params.toString();
    const url = `/api/fs/list${query ? `?${query}` : ''}`;
    let resp;
    try {
      resp = await fetch(url);
    } catch (err) {
      return { error: 'load' };
    }
    if (!resp.ok) {
      let detail = '';
      try { detail = (await resp.json()).detail || ''; } catch (_) { /* keep '' */ }
      if (resp.status === 422 && detail === 'outside_picker_scope') {
        return { error: 'scope' };
      }
      return { error: 'load' };
    }
    try {
      return { body: await resp.json() };
    } catch (_) {
      // 200 with an unreadable body — same load-failure class as a 5xx
      // (previously this threw out of ``navigate`` as an unhandled rejection).
      return { error: 'load' };
    }
  }

  function _segments(path) {
    // Split an absolute POSIX path into clickable breadcrumb segments.
    // Example: "/Users/x/notes" → [{label:"/", path:"/"},
    //   {label:"Users", path:"/Users"}, {label:"x", path:"/Users/x"},
    //   {label:"notes", path:"/Users/x/notes"}].
    if (!path) return [];
    const out = [{ label: '/', path: '/' }];
    const parts = path.split('/').filter(Boolean);
    let acc = '';
    for (const p of parts) {
      acc += '/' + p;
      out.push({ label: p, path: acc });
    }
    return out;
  }

  function _renderBreadcrumb(body) {
    const el = crumbEl();
    el.textContent = '';
    if (body.is_root) {
      const span = document.createElement('span');
      span.className = 'crumb crumb-current';
      span.textContent = _t('picker.title');
      el.appendChild(span);
      return;
    }
    // "Roots" link first so users can always jump back.
    const rootsLink = document.createElement('span');
    rootsLink.className = 'crumb';
    rootsLink.textContent = '⌂';
    rootsLink.setAttribute('role', 'button');
    rootsLink.tabIndex = 0;
    rootsLink.addEventListener('click', () => navigate(null));
    rootsLink.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate(null); }
    });
    el.appendChild(rootsLink);
    const sep0 = document.createElement('span');
    sep0.className = 'crumb-sep';
    sep0.textContent = '·';
    el.appendChild(sep0);

    const segs = _segments(body.path);
    segs.forEach((s, i) => {
      const isLast = i === segs.length - 1;
      const span = document.createElement('span');
      span.className = isLast ? 'crumb crumb-current' : 'crumb';
      span.textContent = s.label;
      if (!isLast) {
        span.setAttribute('role', 'button');
        span.tabIndex = 0;
        const target = s.path;
        span.addEventListener('click', () => navigate(target));
        span.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate(target); }
        });
      }
      el.appendChild(span);
      if (!isLast) {
        const sep = document.createElement('span');
        sep.className = 'crumb-sep';
        sep.textContent = '/';
        el.appendChild(sep);
      }
    });
  }

  function _renderEntries(entries) {
    const ul = listEl();
    ul.textContent = '';
    currentEntries = entries || [];
    if (!entries || entries.length === 0) {
      emptyEl().hidden = false;
      return;
    }
    emptyEl().hidden = true;
    entries.forEach(entry => {
      const li = document.createElement('li');
      li.tabIndex = 0;
      li.setAttribute('role', 'option');
      const icon = document.createElement('span');
      icon.className = 'picker-icon';
      icon.textContent = '📁';
      const name = document.createElement('span');
      name.textContent = entry.name;
      li.appendChild(icon);
      li.appendChild(name);
      li.addEventListener('click', () => navigate(entry.path));
      li.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate(entry.path);
        }
      });
      ul.appendChild(li);
    });
  }

  // Replace the listing with the load-failure state: message + Retry wired
  // to the path that failed. Select is disabled — ``currentPath`` still
  // holds the PREVIOUS view's path, and committing a path the user is no
  // longer looking at would be a misclick trap.
  function _renderLoadError(path) {
    retryPath = path;
    listEl().textContent = '';
    currentEntries = [];
    emptyEl().hidden = true;
    errorEl().hidden = false;
    selectBtn().disabled = true;
    retryBtn().focus();
  }

  async function navigate(path) {
    const seq = ++navSeq;
    const result = await _fetchList(path);
    // A newer navigation (or close) superseded this one — the response is
    // stale no matter what it says (#1247 id 28). Toasts are emitted HERE,
    // after the guard, so a stale failure can't toast over a newer success.
    if (seq !== navSeq) return;
    if (result.error === 'scope') {
      // Current listing still valid — keep it, just explain the refusal.
      _toast(_t('picker.outside'), 'info');
      return;
    }
    if (result.error) {
      _toast(_t('picker.error'), 'error');
      _renderLoadError(path);
      return;
    }
    const body = result.body;
    errorEl().hidden = true;
    currentPath = body.path;
    _renderBreadcrumb(body);
    _renderEntries(body.entries);
    // Select is enabled only when the current view itself is a selectable
    // path (not the roots view). Roots are entry points: the user clicks
    // one to enter, then Selects.
    selectBtn().disabled = body.is_root;
    // Keep keyboard focus inside the dialog: prefer the first list item
    // when available, fall back to Cancel so Tab still cycles correctly.
    const firstItem = listEl().querySelector('li');
    (firstItem || cancelBtn()).focus();
  }

  let _releaseA11y = null;

  function open(opts) {
    onSelectCb = (opts && typeof opts.onSelect === 'function') ? opts.onSelect : null;
    pickerPurpose = (opts && opts.purpose) || 'index';
    // Path-picker owns its own Tab trap (dynamic focusables across breadcrumbs
    // + list items); openModal forwards opts.focusables=null so the helper
    // only adds restore + inert without installing a redundant trap.
    _releaseA11y = window.openModal(modal());
    document.addEventListener('keydown', _onKey, true);
    modal().addEventListener('click', _onBackdrop);
    selectBtn().disabled = true;
    navigate(null);
  }

  function close() {
    hide(modal());
    if (_releaseA11y) { _releaseA11y(); _releaseA11y = null; }
    document.removeEventListener('keydown', _onKey, true);
    modal().removeEventListener('click', _onBackdrop);
    // Invalidate any in-flight navigation: a late response must not paint
    // into (or pull focus back to) the now-hidden modal.
    navSeq += 1;
    currentPath = null;
    currentEntries = [];
    onSelectCb = null;
    pickerPurpose = 'index';
    retryPath = null;
    listEl().textContent = '';
    crumbEl().textContent = '';
    emptyEl().hidden = true;
    errorEl().hidden = true;
  }

  function commit() {
    if (!currentPath) return;
    const path = currentPath;
    if (onSelectCb) {
      // External caller supplied a sink — let them route the path
      // (Context Gateway "Add Project" POSTs to /known-projects, etc.)
      // instead of writing to ``#index-path``.
      const cb = onSelectCb;
      close();
      cb(path);
      return;
    }
    const input = qs('index-path');
    if (input) {
      input.value = path;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    close();
  }

  function _focusables() {
    const items = Array.from(listEl().querySelectorAll('li'));
    const crumbs = Array.from(crumbEl().querySelectorAll('.crumb[tabindex="0"]'));
    const buttons = [];
    if (!errorEl().hidden) buttons.push(retryBtn());
    buttons.push(cancelBtn());
    if (!selectBtn().disabled) buttons.push(selectBtn());
    return [...crumbs, ...items, ...buttons];
  }

  function _onKey(e) {
    if (modal().hidden) return;
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === 'Tab') {
      const focusables = _focusables();
      if (focusables.length === 0) return;
      e.preventDefault();
      const idx = focusables.indexOf(document.activeElement);
      const next = (idx + (e.shiftKey ? -1 : 1) + focusables.length) % focusables.length;
      focusables[next].focus();
    }
  }

  function _onBackdrop(e) {
    if (e.target === modal()) close();
  }

  function _init() {
    if (initialized) return;
    initialized = true;
    const browseBtn = qs('path-picker-browse-btn');
    // ``open`` takes an optional opts arg; pass none for the default
    // Index-tab flow so it falls through to the ``#index-path`` writer.
    if (browseBtn) browseBtn.addEventListener('click', () => open());
    if (cancelBtn()) cancelBtn().addEventListener('click', close);
    if (selectBtn()) selectBtn().addEventListener('click', commit);
    // Retry re-attempts the navigation that failed (``retryPath`` may be
    // null — that's the roots view, a valid target).
    if (retryBtn()) retryBtn().addEventListener('click', () => navigate(retryPath));
    if (window.registerModalCloser) window.registerModalCloser(modal(), close);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }

  // Public API for other surfaces (e.g. Context Gateway "Add Project"
  // in ``context-gateway.js``). Keeping this small — open and close are
  // enough; commit is internal.
  window.PathPicker = { open, close };
})();

/* memtomem folder picker — issue #582 4.12
 *
 * Powers the 📁 Browse button next to the Index tab's Folder-mode path
 * input. Calls /api/fs/list, renders breadcrumb + dir list inside the
 * shared .modal-overlay component, and writes the chosen absolute path
 * back into #index-path on Select. Navigation is bounded by the server's
 * allow-list (memory_dirs + ~); going outside requires closing the modal
 * and typing the path manually — the input itself stays free-form.
 */
'use strict';

(function () {
  let currentPath = null;     // null = roots view
  let currentEntries = [];
  let initialized = false;

  function modal() { return qs('path-picker-modal'); }
  function listEl() { return qs('path-picker-list'); }
  function emptyEl() { return qs('path-picker-empty'); }
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

  async function _fetchList(path) {
    const url = path ? `/api/fs/list?path=${encodeURIComponent(path)}` : '/api/fs/list';
    let resp;
    try {
      resp = await fetch(url);
    } catch (err) {
      _toast(_t('picker.error'), 'error');
      return null;
    }
    if (!resp.ok) {
      let detail = '';
      try { detail = (await resp.json()).detail || ''; } catch (_) { /* keep '' */ }
      if (resp.status === 422 && detail === 'outside_picker_scope') {
        _toast(_t('picker.outside'), 'info');
      } else {
        _toast(_t('picker.error'), 'error');
      }
      return null;
    }
    return await resp.json();
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

  async function navigate(path) {
    const body = await _fetchList(path);
    if (!body) return;
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

  function open() {
    modal().hidden = false;
    document.addEventListener('keydown', _onKey, true);
    modal().addEventListener('click', _onBackdrop);
    selectBtn().disabled = true;
    navigate(null);
  }

  function close() {
    modal().hidden = true;
    document.removeEventListener('keydown', _onKey, true);
    modal().removeEventListener('click', _onBackdrop);
    currentPath = null;
    currentEntries = [];
    listEl().textContent = '';
    crumbEl().textContent = '';
    emptyEl().hidden = true;
  }

  function commit() {
    if (!currentPath) return;
    const input = qs('index-path');
    if (input) {
      input.value = currentPath;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    close();
  }

  function _focusables() {
    const items = Array.from(listEl().querySelectorAll('li'));
    const crumbs = Array.from(crumbEl().querySelectorAll('.crumb[tabindex="0"]'));
    const buttons = [cancelBtn()];
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
    if (browseBtn) browseBtn.addEventListener('click', open);
    if (cancelBtn()) cancelBtn().addEventListener('click', close);
    if (selectBtn()) selectBtn().addEventListener('click', commit);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }
})();

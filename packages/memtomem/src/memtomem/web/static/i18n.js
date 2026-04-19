/* memtomem i18n — lightweight translation module */
'use strict';

const I18N = (() => {
  const _STORAGE_KEY = 'm2m-lang';
  const _SUPPORTED = ['en', 'ko'];
  const _cache = {};
  let _lang = 'en';

  function _detect() {
    const stored = localStorage.getItem(_STORAGE_KEY);
    if (stored && _SUPPORTED.includes(stored)) return stored;
    if (navigator.language && navigator.language.startsWith('ko')) return 'ko';
    return 'en';
  }

  async function _load(lang) {
    if (_cache[lang]) return;
    // Bypass browser cache — locale JSON has no versioning in the URL,
    // and a stale cached file after a key rename / addition makes ``t()``
    // fall through to the raw-key fallback for the new keys.
    const resp = await fetch(`/locales/${lang}.json`, { cache: 'no-store' });
    if (!resp.ok) { console.warn(`[i18n] failed to load ${lang}`); return; }
    _cache[lang] = await resp.json();
  }

  /** Translate key with optional {param} interpolation. */
  function t(key, params) {
    const str = (_cache[_lang] && _cache[_lang][key])
      || (_cache.en && _cache.en[key])
      || key;
    if (!params) return str;
    return str.replace(/\{(\w+)\}/g, (_, k) => (params[k] != null ? params[k] : `{${k}}`));
  }

  /** Apply translations to all [data-i18n] elements in the DOM. */
  function applyDOM() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      el.title = t(el.dataset.i18nTitle);
    });
    document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
      el.setAttribute('aria-label', t(el.dataset.i18nAriaLabel));
    });
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
      console.warn('[i18n] data-i18n-html is deprecated, use data-i18n instead:', el);
      el.textContent = t(el.dataset.i18nHtml);
    });
  }

  /** Switch language, persist, and update DOM. */
  async function setLang(lang) {
    if (!_SUPPORTED.includes(lang)) return;
    _lang = lang;
    localStorage.setItem(_STORAGE_KEY, lang);
    document.documentElement.lang = lang;
    await _load(lang);
    applyDOM();
    // Update the toggle button label
    const btn = document.getElementById('lang-toggle');
    if (btn) btn.textContent = lang === 'ko' ? 'EN' : '한';
  }

  /** Initialise: detect language, load locale, apply. */
  async function init() {
    const lang = _detect();
    await _load('en');   // always load English as fallback
    await _load(lang);
    _lang = lang;
    document.documentElement.lang = lang;
    applyDOM();
    const btn = document.getElementById('lang-toggle');
    if (btn) {
      btn.textContent = lang === 'ko' ? 'EN' : '한';
      btn.addEventListener('click', () => {
        setLang(_lang === 'ko' ? 'en' : 'ko');
        // Notify app.js that language changed
        window.dispatchEvent(new CustomEvent('langchange', { detail: { lang: _lang } }));
      });
    }
  }

  function lang() { return _lang; }

  return { t, applyDOM, setLang, init, lang };
})();

// Global shortcut
const t = I18N.t;

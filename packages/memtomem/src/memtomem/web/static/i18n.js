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

  // Korean particle (josa) allomorph pairs: [후-받침 form, 무-받침 form]. The
  // pick depends on the final character of the preceding noun (받침 유무), with a
  // ``ㄹ`` exception for ``으로/로`` (a ㄹ 받침 takes 로, like a 무-받침 noun).
  const _JOSA_FORMS = {
    '을/를': ['을', '를'],
    '은/는': ['은', '는'],
    '이/가': ['이', '가'],
    '으로/로': ['으로', '로'],
  };

  // Resolve a josa ``pair`` for ``noun`` from its final character. Hangul
  // syllables occupy U+AC00–U+D7A3; ``(code - 0xAC00) % 28`` is the 종성 index
  // (0 ≡ no 받침, 8 ≡ ``ㄹ``). A non-Hangul / empty final (Latin, digit,
  // punctuation — e.g. an arbitrary artifact name or a closing quote) has no
  // reliable 받침 rule, so fall back to the 무-받침 form, which reads naturally
  // for the all-Hangul type/tier nouns this is applied to.
  function _josaPick(noun, pair) {
    const forms = _JOSA_FORMS[pair];
    const s = String(noun == null ? '' : noun);
    const code = s.charCodeAt(s.length - 1);
    if (!(code >= 0xac00 && code <= 0xd7a3)) return forms[1];
    const jong = (code - 0xac00) % 28;
    if (jong === 0) return forms[1];
    if (pair === '으로/로' && jong === 8) return forms[1];
    return forms[0];
  }

  /** Translate key with optional {param} interpolation + Korean josa markers.
   *
   * Beyond ``{param}`` substitution, a josa marker ``[을/를]`` (or ``은/는`` /
   * ``이/가`` / ``으로/로``) renders the correct allomorph for the MOST RECENT
   * substituted value — so ``{type}[을/를]`` → "스킬을", and across an
   * intervening quote ``"{name}"[을/를]`` resolves on ``{name}`` (#1398). Only
   * the four known pairs are markers; any other ``[...]`` is left verbatim, so
   * ordinary bracketed text is unaffected. Param-less calls skip interpolation
   * entirely (unchanged), so a josa-marked string is only ever rendered with
   * its params. */
  function t(key, params) {
    const str = (_cache[_lang] && _cache[_lang][key])
      || (_cache.en && _cache.en[key])
      || key;
    if (!params) return str;
    let last = '';
    return str.replace(/\{(\w+)\}|\[([^\]]+)\]/g, (m, k, pair) => {
      if (k !== undefined) {
        const v = params[k] != null ? String(params[k]) : `{${k}}`;
        last = v;
        return v;
      }
      return _JOSA_FORMS[pair] ? _josaPick(last, pair) : m;
    });
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
    // ``.help-tip`` popovers read their text from a ``data-help`` attribute
    // (CSS ``::after content: attr(data-help)``). ``data-help-i18n`` carries
    // an i18n key for static help-tips so the popover text + its a11y
    // ``aria-label`` re-translate in place on every ``langchange`` applyDOM.
    document.querySelectorAll('[data-help-i18n]').forEach(el => {
      const txt = t(el.dataset.helpI18n);
      el.setAttribute('data-help', txt);
      el.setAttribute('aria-label', txt);
    });
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
      console.warn('[i18n] data-i18n-html is deprecated, use data-i18n instead:', el);
      el.textContent = t(el.dataset.i18nHtml);
    });
  }

  /** Switch language, persist, update DOM, and notify listeners.
   *
   * Dispatches ``langchange`` *after* ``_load`` + ``applyDOM`` so listeners
   * (e.g. ``app.js``'s ``loadStats`` refresh) read the new locale's cache,
   * not the previous one. Previously the dispatch happened in the click
   * handler immediately after calling ``setLang`` without ``await``, which
   * raced the locale fetch — listeners would fall back to English and
   * write the wrong language into the DOM right before ``applyDOM`` here
   * clobbered them with the placeholder. */
  async function setLang(lang) {
    if (!_SUPPORTED.includes(lang)) return;
    _lang = lang;
    localStorage.setItem(_STORAGE_KEY, lang);
    document.documentElement.lang = lang;
    await _load(lang);
    applyDOM();
    // Update the toggle button label
    const btn = document.getElementById('lang-toggle');
    if (btn) btn.textContent = lang === 'ko' ? 'KO' : 'EN';
    window.dispatchEvent(new CustomEvent('langchange', { detail: { lang } }));
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
      btn.textContent = lang === 'ko' ? 'KO' : 'EN';
      btn.addEventListener('click', () => {
        // setLang dispatches langchange itself once the new locale is
        // loaded and applyDOM has run.
        setLang(_lang === 'ko' ? 'en' : 'ko');
      });
    }
    // JS-owned dynamic strings (Compose placeholder, header chip jump
    // hint) read t() inside listeners that fire from settings-config.js's
    // module-level ``fetchServerConfig()``, which races with this init.
    // Fire langchange once after the locale cache is populated so those
    // listeners get a fresh read with real translations instead of the
    // raw-key fallback they'd otherwise hold for the entire session.
    window.dispatchEvent(new CustomEvent('langchange', { detail: { lang } }));
  }

  function lang() { return _lang; }

  return { t, applyDOM, setLang, init, lang };
})();

// Global shortcut
const t = I18N.t;

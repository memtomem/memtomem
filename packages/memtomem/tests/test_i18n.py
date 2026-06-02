"""Tests for i18n locale files (en.json / ko.json).

Validates that both locale files are well-formed JSON, share the same key
set, and preserve interpolation placeholders.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_LOCALES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static" / "locales"
)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _load_locale(name: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{name}.json"
    assert path.exists(), f"Locale file missing: {path}"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict), f"{name}.json root must be an object"
    return data


@pytest.fixture(scope="module")
def en() -> dict[str, str]:
    return _load_locale("en")


@pytest.fixture(scope="module")
def ko() -> dict[str, str]:
    return _load_locale("ko")


class TestLocaleFiles:
    """Structural tests for locale JSON files."""

    def test_en_is_valid_json(self, en: dict[str, str]) -> None:
        assert len(en) > 0, "en.json must not be empty"

    def test_ko_is_valid_json(self, ko: dict[str, str]) -> None:
        assert len(ko) > 0, "ko.json must not be empty"

    def test_ko_has_all_en_keys(self, en: dict[str, str], ko: dict[str, str]) -> None:
        missing = set(en) - set(ko)
        assert not missing, f"Keys in en.json missing from ko.json: {sorted(missing)}"

    def test_en_has_all_ko_keys(self, en: dict[str, str], ko: dict[str, str]) -> None:
        orphan = set(ko) - set(en)
        assert not orphan, f"Keys in ko.json missing from en.json: {sorted(orphan)}"

    def test_placeholder_parity(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Each key's {param} placeholders must match between en and ko."""
        mismatches: list[str] = []
        for key in en:
            if key not in ko:
                continue
            en_ph = set(_PLACEHOLDER_RE.findall(en[key]))
            ko_ph = set(_PLACEHOLDER_RE.findall(ko[key]))
            if en_ph != ko_ph:
                mismatches.append(f"  {key}: en={en_ph} ko={ko_ph}")
        assert not mismatches, "Placeholder mismatch:\n" + "\n".join(mismatches)

    def test_all_values_are_strings(self, en: dict[str, str], ko: dict[str, str]) -> None:
        for name, data in [("en", en), ("ko", ko)]:
            bad = [k for k, v in data.items() if not isinstance(v, str)]
            assert not bad, f"Non-string values in {name}.json: {bad}"

    def test_no_empty_values(self, en: dict[str, str], ko: dict[str, str]) -> None:
        for name, data in [("en", en), ("ko", ko)]:
            empty = [k for k, v in data.items() if not v.strip()]
            assert not empty, f"Empty values in {name}.json: {empty}"


_STATIC_JS_DIR = _LOCALES_DIR.parent


def _langchange_listener_body(text: str) -> str:
    """Sentinel-slice the langchange listener body in ``context-gateway.js``.

    Earlier specs used a lazy regex ``\\{(.+?)\\}\\);`` to grab the body —
    that pattern stops at the first ``});`` it sees, which fails the
    moment a call inside the listener body uses an inline object
    argument like::

        loadCtxDetail(type, name, { autoOpenDiff: true });

    The inline ``});`` is the call's closing parenthesis, not the
    listener's, but the lazy regex doesn't know that. Switch to a
    stable pair of sentinels: the listener's
    ``window.addEventListener('langchange', ...)`` registration on the
    upper end and the file's ``// Sync All button`` comment that
    immediately follows on the lower end. Both have been stable for
    months and are scoped to this single listener.

    See ``feedback_listener_body_lazy_regex_trips_inline_objects.md``
    (PR #840 review) — same pattern surfaced there and was fixed with
    the same sentinel slice.
    """
    start = text.find("window.addEventListener('langchange'")
    assert start >= 0, "langchange listener registration missing"
    end = text.find("// Sync All button", start)
    assert end > start, "couldn't locate end-of-listener sentinel"
    return text[start:end]


class TestNoHardcodedStrings:
    """Guard against regressions in i18n coverage for user-facing dialogs.

    Confirm dialogs and toast notifications must route through ``t()`` so they
    can be localized. This test scans the web UI's JS modules for call sites
    that build their text from raw JS template literals or English string
    literals instead of locale keys — the exact pattern #29 was filed to clear.
    """

    # JS files that render user-facing confirm/toast messages. Keep in sync
    # with the module split documented in feedback_js_module_split.md — new
    # files rendering dialogs or toasts should be added here.
    _SCANNED_FILES = (
        "app.js",
        "settings-maintenance.js",
        "settings-namespaces.js",
        "settings-config.js",
        "settings-hooks-watchdog.js",
        "context-gateway.js",
    )

    def test_no_template_literal_toasts(self) -> None:
        r"""``showToast(\`...\`)`` with a backtick template literal means the
        message is built in JS rather than looked up from a locale file."""
        import re

        bad: list[str] = []
        pattern = re.compile(r"showToast\(`")
        for name in self._SCANNED_FILES:
            path = _STATIC_JS_DIR / name
            if not path.exists():
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"  {name}:{lineno}: {line.strip()}")
        assert not bad, (
            "Found showToast call sites using template-literal strings — "
            "route through t('toast.<key>', { ... }) instead:\n" + "\n".join(bad)
        )

    def test_no_english_string_literal_toasts(self) -> None:
        """``showToast('Some English', ...)`` with a plain English literal
        (starts with a capital letter and ends with a letter/punctuation) is
        the pre-#29 pattern this PR removed. ``err.detail``-style dynamic
        messages with a ``t(...)`` fallback are fine and excluded."""
        import re

        bad: list[str] = []
        # Match showToast('Capital-letter-string', ...) — catches plain-English
        # literals. Excludes showToast(t(...), ...) and showToast(<var>, ...).
        pattern = re.compile(r"showToast\(\s*['\"][A-Z]")
        for name in self._SCANNED_FILES:
            path = _STATIC_JS_DIR / name
            if not path.exists():
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"  {name}:{lineno}: {line.strip()}")
        assert not bad, (
            "Found showToast call sites with hardcoded English literals — "
            "route through t('toast.<key>', { ... }) instead:\n" + "\n".join(bad)
        )

    def test_no_hardcoded_confirm_titles(self) -> None:
        """``showConfirm({ title: 'Foo', ... })`` with a plain English title
        bypasses i18n. All confirm titles must come from ``t('confirm.*')``.

        Restricts the match to the showConfirm block itself (``title:`` inside
        the first few lines after ``showConfirm(``) — other ``title:`` fields
        in unrelated config-section definitions are intentionally ignored."""
        import re

        # Multiline: `showConfirm({` followed within ~4 lines by a `title:`
        # holding a capital-letter English literal.
        pattern = re.compile(
            r"showConfirm\s*\(\s*\{[^}]{0,400}?title:\s*['\"][A-Z][A-Za-z ]+['\"]",
            re.DOTALL,
        )
        bad: list[str] = []
        for name in self._SCANNED_FILES:
            path = _STATIC_JS_DIR / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                snippet = (
                    match.group(0).split("\n", 2)[1].strip()
                    if "\n" in match.group(0)
                    else match.group(0)[:120]
                )
                bad.append(f"  {name}:{lineno}: {snippet}")
        assert not bad, (
            "Found showConfirm titles with hardcoded English — "
            "route through t('confirm.<key>_title') instead:\n" + "\n".join(bad)
        )

    def test_issue_29_new_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Structural guard: the specific keys introduced for #29 must exist
        in both locale files. A regression that deletes them would leak raw
        keys into the UI rather than Korean translations."""
        required = {
            "common.confirm",
            "common.replace",
            "common.merge",
            "common.expire",
            "common.sync",
            "confirm.chunk_delete_title",
            "confirm.chunk_delete_msg",
            "confirm.chunk_delete_simple_msg",
            "confirm.bulk_delete_title",
            "confirm.bulk_delete_msg",
            "confirm.source_delete_title",
            "confirm.source_delete_msg",
            "confirm.merge_dupe_title",
            "confirm.merge_dupe_keep_a_msg",
            "confirm.merge_dupe_keep_b_msg",
            "confirm.expire_title",
            "confirm.expire_msg",
            "confirm.hooks_replace_title",
            "confirm.hooks_replace_msg",
            "confirm.hooks_sync_title",
            "confirm.hooks_sync_msg",
            "toast.indexed_count",
            "toast.saved_to_file",
            "toast.upload_complete",
            "toast.tagged_count",
            "toast.query_saved",
            "toast.query_deleted",
            "toast.query_removed",
            "toast.exported_count",
            "toast.indexing_files",
            "toast.indexed_files_chunks",
            "toast.bulk_delete_partial",
            "toast.bulk_delete_ok",
            "toast.expired_count",
            "toast.imported_count",
            "toast.ns_renamed",
            "toast.fields_rejected",
            "toast.settings_updated_count",
            "toast.reindex_partial",
            "toast.reindex_complete",
            "toast.hooks_warnings",
            "toast.request_failed",
            "toast.unexpected_response",
            "toast.sync_failed",
            "toast.create_failed",
            "toast.refresh_complete",
            "toast.name_required",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"Keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Keys missing from ko.json: {sorted(missing_ko)}"

    def test_rfc_304_provider_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Vendor labels for the memory-dirs tree (RFC #304 Phase 2). Key
        names mirror the server-side ``provider`` wire value from
        ``_CATEGORY_TO_PROVIDER`` (``openai``, not ``codex``); deleting
        any of these would leak the raw key string into the UI via
        ``t()``'s fallback path."""
        required = {
            "sources.memory_dirs.provider.user",
            "sources.memory_dirs.provider.claude",
            "sources.memory_dirs.provider.openai",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"Provider keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Provider keys missing from ko.json: {sorted(missing_ko)}"

    def test_no_template_literal_textcontent_count(self) -> None:
        """``el.textContent = `${expr} chunks/sources/files``` must route
        through ``t()`` with a ``{count}`` placeholder so plural noun forms
        can be localized. Added in #698 to extend the guard beyond
        ``showToast``/``showConfirm`` into direct DOM assignments.

        Known regex limits (acceptable for a regression guard, not an
        exhaustive scan): requires whitespace between the ``${...}`` and
        the noun, and the inner expression must not contain ``}`` (so
        ``${foo({k: 1})}`` slips through). Both shapes are uncommon and
        would still produce English copy that a code reviewer should
        catch — the regex's job is to lock in the specific
        ``${count} chunks/sources/files`` regression."""
        pattern = re.compile(r"\.textContent\s*=\s*`\$\{[^`}]+\}\s+(chunks|sources|files)\b")
        bad: list[str] = []
        for name in self._SCANNED_FILES:
            path = _STATIC_JS_DIR / name
            if not path.exists():
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"  {name}:{lineno}: {line.strip()}")
        assert not bad, (
            "Found textContent assignments with hardcoded plural-noun template "
            "literals — route through t('<key>', { count: ... }) instead:\n" + "\n".join(bad)
        )

    def test_no_hardcoded_tags_empty_state(self) -> None:
        """``app.js`` ``loadTags()`` must not re-introduce the literal
        ``'No tags yet'`` / ``'Run Auto-Tag to generate tags'`` empty state.
        Replaced in #698 with ``t('tags.empty_msg')`` /
        ``t('tags.empty_hint')``. Targeted regression guard — ``emptyState()``
        has ~19 callers with similar shape that we are not sweeping yet."""
        text = (_STATIC_JS_DIR / "app.js").read_text(encoding="utf-8")
        forbidden = ["'No tags yet'", "'Run Auto-Tag to generate tags'"]
        bad = [s for s in forbidden if s in text]
        assert not bad, (
            f"Found re-introduced #698 empty-state literals in app.js: {bad}. "
            "Use t('tags.empty_msg') / t('tags.empty_hint') instead."
        )

    def test_embedding_mismatch_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """The embedding-mismatch banner (#976) must route through locale
        keys with their interpolation placeholders intact. Deleting a key or
        dropping a placeholder would leak the raw key / break the {detail}
        substitution into the banner ``checkEmbeddingMismatch()`` builds."""
        required = {
            "banner.emb_mismatch": {"details"},
            "banner.emb_mismatch_dimension": {"db", "config"},
            "banner.emb_mismatch_model": {"db", "config"},
        }
        missing_en = set(required) - set(en)
        missing_ko = set(required) - set(ko)
        assert not missing_en, f"Embedding-mismatch keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Embedding-mismatch keys missing from ko.json: {sorted(missing_ko)}"
        bad_ph: list[str] = []
        for key, params in required.items():
            for name, locale in [("en", en), ("ko", ko)]:
                got = set(_PLACEHOLDER_RE.findall(locale[key]))
                if got != params:
                    bad_ph.append(f"  {name} {key}: expected {params}, got {got}")
        assert not bad_ph, "Embedding-mismatch placeholder drift:\n" + "\n".join(bad_ph)

    def test_no_hardcoded_embedding_mismatch(self) -> None:
        """``app.js`` ``checkEmbeddingMismatch()`` must not re-introduce the
        English banner literals (#976). The banner facts are now built from
        ``t('banner.emb_mismatch*')`` — these substrings would mean the text
        regressed to an inline template literal that never localizes."""
        text = (_STATIC_JS_DIR / "app.js").read_text(encoding="utf-8")
        forbidden = [
            "Embedding mismatch",
            "Search may not work until resolved",
            "dimension: DB",
            "model: DB",
        ]
        bad = [s for s in forbidden if s in text]
        assert not bad, (
            f"Found re-introduced #976 embedding-mismatch literals in app.js: {bad}. "
            "Use t('banner.emb_mismatch') / t('banner.emb_mismatch_dimension') / "
            "t('banner.emb_mismatch_model') instead."
        )

    def test_emb_mismatch_rerenders_on_langchange(self) -> None:
        """The embedding banner fires at module load (``checkEmbeddingMismatch()``)
        and can resolve before ``I18N.init()`` populates the locale cache, so
        ``t()`` would render raw ``banner.emb_mismatch*`` keys. The fix caches
        the payload and re-renders on ``langchange`` (init dispatches a one-shot
        langchange once the cache is ready). Pin that wiring so a refactor can't
        silently reintroduce the race. See feedback_i18n_init_order_race.md.
        """
        text = (_STATIC_JS_DIR / "app.js").read_text(encoding="utf-8")
        assert "function renderEmbMismatchBanner" in text, (
            "renderEmbMismatchBanner() helper missing — the banner text must be "
            "re-renderable independent of the initial fetch so langchange can refresh it."
        )
        # Slice the first langchange listener: from its registration to the
        # stable ``checkEmbeddingMismatch();`` call that immediately follows the
        # listener block. Sentinel slice (not a lazy ``});`` regex) per
        # feedback_listener_body_lazy_regex_trips_inline_objects.md.
        start = text.find("window.addEventListener('langchange'")
        end = text.find("checkEmbeddingMismatch();", start)
        assert 0 <= start < end, "could not locate the langchange listener block in app.js"
        assert "renderEmbMismatchBanner()" in text[start:end], (
            "the langchange listener must call renderEmbMismatchBanner() so the banner "
            "doesn't show raw locale keys when checkEmbeddingMismatch() wins the race "
            "against I18N.init() (feedback_i18n_init_order_race.md)."
        )

    def test_index_progress_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """The Index streaming progress labels (#1027) must route through locale
        keys with their interpolation placeholders intact, so the SSE handler's
        in-progress / complete labels localize."""
        required = {
            "index.progress_files": {"done", "total"},
            "index.progress_done": {"count"},
        }
        missing_en = set(required) - set(en)
        missing_ko = set(required) - set(ko)
        assert not missing_en, f"Index-progress keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Index-progress keys missing from ko.json: {sorted(missing_ko)}"
        bad_ph: list[str] = []
        for key, params in required.items():
            for name, locale in [("en", en), ("ko", ko)]:
                got = set(_PLACEHOLDER_RE.findall(locale[key]))
                if got != params:
                    bad_ph.append(f"  {name} {key}: expected {params}, got {got}")
        assert not bad_ph, "Index-progress placeholder drift:\n" + "\n".join(bad_ph)

    def test_no_hardcoded_index_progress(self) -> None:
        """``app.js`` SSE indexing handler must not rebuild the streaming
        progress labels (#1027) as inline template literals. These interpolated
        substrings only existed in the pre-i18n labels; their return means the
        text regressed off ``t('index.progress_*')``."""
        text = (_STATIC_JS_DIR / "app.js").read_text(encoding="utf-8")
        forbidden = [
            "${event.files_total} files",
            "${event.total_files} files",
        ]
        bad = [s for s in forbidden if s in text]
        assert not bad, (
            f"Found re-introduced #1027 streaming-label literals in app.js: {bad}. "
            "Use t('index.progress_files', {done, total}) / "
            "t('index.progress_done', {count}) instead."
        )

    def test_decay_scan_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """The Decay scan status messages (#1024) must route through locale keys,
        with the scan-failed key keeping its ``{error}`` detail placeholder."""
        required = {
            "settings.decay.no_expire": set(),
            "settings.decay.scan_failed": {"error"},
        }
        missing_en = set(required) - set(en)
        missing_ko = set(required) - set(ko)
        assert not missing_en, f"Decay keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Decay keys missing from ko.json: {sorted(missing_ko)}"
        bad_ph: list[str] = []
        for key, params in required.items():
            for name, locale in [("en", en), ("ko", ko)]:
                got = set(_PLACEHOLDER_RE.findall(locale[key]))
                if got != params:
                    bad_ph.append(f"  {name} {key}: expected {params}, got {got}")
        assert not bad_ph, "Decay placeholder drift:\n" + "\n".join(bad_ph)

    def test_no_hardcoded_decay_messages(self) -> None:
        """``settings-maintenance.js`` ``runDecayScan()`` must not rebuild its
        status messages (#1024) as English literals. Substrings are scoped to
        the decay strings — the dedup ``'Scan failed'`` empty state (no colon)
        is a separate concern (#1025) and intentionally not matched here."""
        text = (_STATIC_JS_DIR / "settings-maintenance.js").read_text(encoding="utf-8")
        forbidden = [
            "'No chunks to expire.'",
            "'Scan failed: ' +",
        ]
        bad = [s for s in forbidden if s in text]
        assert not bad, (
            f"Found re-introduced #1024 decay-scan literals in settings-maintenance.js: {bad}. "
            "Use t('settings.decay.no_expire') / t('settings.decay.scan_failed', {error}) instead."
        )

    def test_settings_recovery_done_key_present(
        self, en: dict[str, str], ko: dict[str, str]
    ) -> None:
        """The settings recovery flow (#1023) localizes its completion buttons
        via ``common.done`` and reuses the existing ``toast.fts_rebuilt`` key
        (with its ``{count}``) for the FTS-rebuild fallback toast."""
        required = {"common.done": set(), "toast.fts_rebuilt": {"count"}}
        missing_en = set(required) - set(en)
        missing_ko = set(required) - set(ko)
        assert not missing_en, f"Recovery keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Recovery keys missing from ko.json: {sorted(missing_ko)}"
        bad_ph: list[str] = []
        for key, params in required.items():
            for name, locale in [("en", en), ("ko", ko)]:
                got = set(_PLACEHOLDER_RE.findall(locale[key]))
                if got != params:
                    bad_ph.append(f"  {name} {key}: expected {params}, got {got}")
        assert not bad_ph, "Recovery placeholder drift:\n" + "\n".join(bad_ph)

    def test_no_hardcoded_settings_recovery(self) -> None:
        """``settings-config.js`` recovery handlers must not rebuild the
        completion labels / FTS fallback toast as English literals (#1023).
        ``res.message`` (API-provided text) is intentionally preserved as the
        primary, so only the fallback literal and the button text are pinned."""
        text = (_STATIC_JS_DIR / "settings-config.js").read_text(encoding="utf-8")
        forbidden = [
            "btn.textContent = 'Done'",
            "FTS rebuilt: ${res.rebuilt_rows}",
        ]
        bad = [s for s in forbidden if s in text]
        assert not bad, (
            f"Found re-introduced #1023 recovery literals in settings-config.js: {bad}. "
            "Use t('common.done') / t('toast.fts_rebuilt', {count}) instead."
        )

    def test_search_history_chip_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Search history / recent-chip labels (#1021) must come from locale
        keys (no interpolation)."""
        required = {
            "search.history_remove_title",
            "search.history_clear",
            "search.recent_label",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"Search-history keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Search-history keys missing from ko.json: {sorted(missing_ko)}"

    def test_no_hardcoded_search_history_chips(self) -> None:
        """``app.js`` ``renderHistoryDropdown()`` / ``renderRecentChips()`` must
        not re-introduce the English history/recent literals (#1021). Substrings
        are scoped to these widgets — the upload and saved-chip ``title="Remove"``
        buttons are out of scope and intentionally not matched."""
        text = (_STATIC_JS_DIR / "app.js").read_text(encoding="utf-8")
        forbidden = [
            "'Clear history'",
            'history-remove" title="Remove"',
            'recent-chips-label">Recent:',
        ]
        bad = [s for s in forbidden if s in text]
        assert not bad, (
            f"Found re-introduced #1021 search-history literals in app.js: {bad}. Use "
            "t('search.history_clear') / t('search.history_remove_title') / "
            "t('search.recent_label') instead."
        )

    def test_named_html_offenders_have_i18n(self) -> None:
        """``index.html`` elements claimed by #698 must carry ``data-i18n``
        bindings. These IDs displayed English-only fallback text before the
        fix; the bindings let ``applyDOM()`` swap them at language change."""
        html = (_STATIC_JS_DIR / "index.html").read_text(encoding="utf-8")
        required = [
            ("stat-chunks", 'data-i18n="header.stat_chunks"'),
            ("stat-sources", 'data-i18n="header.stat_sources"'),
            ("adv-toggle", 'data-i18n="search.adv_advanced"'),
            ("adv-toggle", 'data-i18n-title="search.adv_title"'),
            ("bulk-delete-btn", 'data-i18n="search.bulk_delete"'),
        ]
        bad: list[str] = []
        for el_id, must_have in required:
            tag_re = re.compile(rf'<[^>]*\bid="{re.escape(el_id)}"[^>]*>')
            m = tag_re.search(html)
            if not m:
                bad.append(f"  id={el_id!r} missing from index.html")
                continue
            if must_have not in m.group(0):
                bad.append(f"  id={el_id!r} missing attribute: {must_have}")
        assert not bad, (
            "index.html elements named in #698 missing required i18n bindings:\n" + "\n".join(bad)
        )

    def test_home_quick_actions_describe_navigation_flow(
        self, en: dict[str, str], ko: dict[str, str]
    ) -> None:
        """Home Quick Actions (#989) mostly navigate or prepare forms."""
        html = (_STATIC_JS_DIR / "index.html").read_text(encoding="utf-8")
        js = (_STATIC_JS_DIR / "app.js").read_text(encoding="utf-8")
        ids = ["search", "index", "reindex", "export", "dedup", "tags"]
        bad: list[str] = []

        for action in ids:
            el_id = f"home-{action}-btn"
            tag_re = re.compile(rf'<button[^>]*\bid="{re.escape(el_id)}"[^>]*>')
            m = tag_re.search(html)
            if not m:
                bad.append(f"  id={el_id!r} missing from index.html")
                continue
            tag = m.group(0)
            for attr in [
                f'data-i18n="home.action.{action}"',
                f'data-i18n-title="home.action.{action}_title"',
                f'data-i18n-aria-label="home.action.{action}_title"',
            ]:
                if attr not in tag:
                    bad.append(f"  id={el_id!r} missing attribute: {attr}")

        for locale_name, locale in [("en", en), ("ko", ko)]:
            for action in ids:
                label = locale[f"home.action.{action}"]
                title = locale[f"home.action.{action}_title"]
                # Emoji-prefix guard: leading char must be word/space or a Hangul
                # syllable. Rejects 🔍/📥/🧹/… that drifted in pre-#989.
                if re.match(r"^[^\w\s가-힣]", label):
                    bad.append(f"  {locale_name}: home.action.{action} still starts with an icon")
                if locale_name == "en" and action in {"reindex", "dedup"}:
                    if "does not start" not in title.lower():
                        bad.append(
                            f"  {locale_name}: home.action.{action}_title must say it does not start"
                        )

        assert en["home.action.reindex"] == "Prepare Full Re-index"
        assert en["home.action.export"] == "Open Export"
        assert en["home.action.dedup"] == "Open Dedup Scan"
        assert "switchSettingsSection('export')" in js
        assert "switchSettingsSection('dedup')" in js
        # Negative pin: the pre-#989 brittle `.settings-nav-btn?.click()` poke
        # must not return — `switchSettingsSection(...)` is the public router.
        assert '.settings-nav-btn[data-section="export"]\')?.click()' not in js
        assert '.settings-nav-btn[data-section="dedup"]\')?.click()' not in js
        for key in [
            "toast.quick_action.open_search",
            "toast.quick_action.open_index",
            "toast.quick_action.reindex_ready",
            "toast.quick_action.open_export",
            "toast.quick_action.open_dedup",
            "toast.quick_action.open_tags",
        ]:
            assert key in js
            assert key in en
            assert key in ko

        assert not bad, "Home Quick Actions i18n/a11y drift:\n" + "\n".join(bad)

    def test_issue_990_home_strings_are_localized(
        self, en: dict[str, str], ko: dict[str, str]
    ) -> None:
        """Home dashboard JS-owned empty/error/health strings must use i18n.

        These strings are not reached by ``data-i18n`` because ``app.js``
        writes them via ``innerHTML`` after API calls complete.
        """
        required = {
            "home.state.loading",
            "home.state.load_failed",
            "home.state.no_files_indexed",
            "home.state.no_data",
            "home.state.no_namespaces",
            "home.state.no_sources_title",
            "home.state.no_sources_hint",
            "home.state.no_pinned",
            "home.source_chunks_one",
            "home.source_chunks_other",
            "home.health.embedding",
            "home.health.dimension",
            "home.health.storage",
            "home.health.last_indexed",
            "home.health.never",
            "home.health.unknown",
            "home.pin.unpin_title",
        }
        assert not (required - set(en)), (
            f"#990 keys missing from en.json: {sorted(required - set(en))}"
        )
        assert not (required - set(ko)), (
            f"#990 keys missing from ko.json: {sorted(required - set(ko))}"
        )

        app = (_STATIC_JS_DIR / "app.js").read_text(encoding="utf-8")
        home_start = app.index("// Home Dashboard (D3)")
        home_end = app.index("// C. Quick Search from Home", home_start)
        home_js = app[home_start:home_end]
        for key in required:
            assert f"t('{key}" in app or f'"{key}"' in app or f"'{key}'" in app, (
                f"#990 key {key!r} is present in locales but not wired in app.js"
            )

        forbidden = [
            "No files indexed",
            "No data",
            "No namespaces",
            "No sources indexed yet",
            "Add files from the Index tab",
            "No pinned chunks yet",
            ">Embedding<",
            ">Dimension<",
            ">Storage<",
            ">Last Indexed<",
            "'Never'",
            '"Never"',
            " chunks</span>",
        ]
        bad = [literal for literal in forbidden if literal in home_js]
        assert not bad, "#990 Home literals reintroduced in app.js: " + ", ".join(bad)

    def test_issue_990_home_sources_fallback_is_not_mojibake(self) -> None:
        html = (_STATIC_JS_DIR / "index.html").read_text(encoding="utf-8")
        tag_re = re.compile(r'<div[^>]*\bid="home-sources"[^>]*>(.*?)</div>')
        match = tag_re.search(html)
        assert match, "id='home-sources' missing from index.html"
        fallback = match.group(1)
        assert "�" not in fallback, "home-sources fallback must not contain mojibake"
        assert fallback == "—", f"home-sources fallback should be an em dash, got {fallback!r}"

    def test_issue_775_settings_badge_keys_present(
        self, en: dict[str, str], ko: dict[str, str]
    ) -> None:
        """Settings overview badge i18n keys (#775). The wire statuses that
        ``context_overview`` (``web/routes/context_gateway.py``) actually
        emits for the ``settings`` slot are ``in_sync`` / ``out_of_sync`` /
        ``error`` — collapsed from ``diff_settings`` results. Each must have
        a ``settings.hooks.badge_*`` entry in both locales so the badge
        renders localized text instead of falling back to
        ``status.replace('_', ' ')``."""
        required = {
            "settings.hooks.badge_in_sync",
            "settings.hooks.badge_out_of_sync",
            "settings.hooks.badge_error",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"#775 keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"#775 keys missing from ko.json: {sorted(missing_ko)}"

    def test_issue_775_js_routes_through_t(self) -> None:
        """``context-gateway.js`` settings overview branch must look up the
        badge text via the ``_SETTINGS_STATUS_I18N`` map AND wrap the result
        in ``t()``, not emit ``d.status.replace('_', ' ')`` directly. The
        bug was the unconditional ``replace`` — guard the full
        ``key ? t(key) : <fallback>`` shape so a regression that drops the
        ``t()`` wrap or hardcodes a string still trips the test."""
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        assert "_SETTINGS_STATUS_I18N" in text, (
            "context-gateway.js missing _SETTINGS_STATUS_I18N map (#775)"
        )
        # Two-pronged check: the lookup expression AND the t() wrap. The
        # bug Codex flagged was that a regression could keep the lookup
        # but drop ``t()`` — render ``key`` directly and emit a raw i18n
        # key into the badge. Asserting ``t(key)`` separately catches
        # that. Both substrings are unique enough in this file (verified
        # via grep) to act as load-bearing markers.
        assert "_SETTINGS_STATUS_I18N[d.status]" in text, (
            "context-gateway.js settings branch must look up status via "
            "_SETTINGS_STATUS_I18N[d.status] (#775)"
        )
        assert "t(key)" in text, (
            "context-gateway.js settings branch must wrap the lookup "
            "result in t(key) — dropping the t() wrap was the regression "
            "shape #775 was filed for"
        )

        # All emitted statuses must have a map entry so no live status
        # silently falls through to the raw ``replace('_', ' ')`` path.
        # Sourced from web/routes/context_gateway.py:context_overview.
        for status in ("in_sync", "out_of_sync", "error"):
            assert f"{status}:" in text, (
                f"_SETTINGS_STATUS_I18N missing entry for emitted status {status!r} (#775)"
            )

    def test_issue_774_sync_all_inspects_settings_body(
        self, en: dict[str, str], ko: dict[str, str]
    ) -> None:
        """Sync All must inspect the Settings sync response body (#774).

        The route at ``/api/context/settings/sync`` returns HTTP 200 with
        ``{"results": [{"status": "needs_confirmation", ...}]}`` when the
        body's ``allow_host_writes`` defaults to false — the case for Sync
        All, which posts no body. ``resp.ok`` alone is therefore not
        enough to confirm the merge actually happened: before #774 the
        ``sync_success`` toast lied to the user even though
        ``~/.claude/settings.json`` was untouched. The fix branches on
        ``status === 'needs_confirmation'`` and surfaces partial-success
        with a one-tap navigation to the Settings panel.

        Symmetric pin per ``feedback_pin_invert_symmetric_assertion.md``:
        the partial-success branch (positive marker) is paired with the
        unconditional ``settings.ctx.sync_success`` for the negative case
        (still present so a regression that always toasts partial-success
        also fails). i18n keys for both locales are pinned to catch a
        rename.
        """
        # i18n keys exist in both locales.
        required = {
            "toast.sync_partial_settings_needs_confirmation",
            "toast.open_settings_action",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"#774 keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"#774 keys missing from ko.json: {sorted(missing_ko)}"

        # JS branches on the per-result status, not on resp.ok alone.
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        assert "settingsResp.json()" in text, (
            "context-gateway.js Sync All must read the Settings sync "
            "response body — resp.ok alone hides ``needs_confirmation``"
        )
        assert "'needs_confirmation'" in text, (
            "context-gateway.js Sync All must branch on per-result "
            "``status === 'needs_confirmation'`` (#774)"
        )
        # Action button wires through to the Settings panel, not a dead
        # toast — the user has to be able to drive the host-write
        # confirmation from somewhere reachable.
        assert "switchSettingsSection('hooks-sync')" in text, (
            "context-gateway.js Sync All partial-success toast must "
            "expose a navigation action to the Settings panel (#774)"
        )
        # Inverted pin: the unconditional success branch must still
        # exist. A regression that universally toasts partial-success
        # would drop ``settings.ctx.sync_success`` from the Sync All
        # handler — fail loudly.
        assert "t('settings.ctx.sync_success')" in text, (
            "context-gateway.js Sync All lost its full-success branch — "
            "every Sync All would now toast partial-success (#774)"
        )

    def test_issue_799_sync_all_status_coverage(self) -> None:
        """Sync All must classify *every* non-``ok`` per-result status (#799).

        ``generate_all_settings`` returns one of five statuses per generator:
        ``ok`` / ``skipped`` / ``error`` / ``needs_confirmation`` / ``aborted``
        (see ``packages/memtomem/src/memtomem/context/settings.py``). #774
        added a branch for ``needs_confirmation`` only, leaving ``error``
        and ``aborted`` to fall through to the unconditional success
        toast — the same class of "resp.ok hides per-result failure" bug
        the parent issue closed for the host-write case. #799 widens the
        Sync All handler to surface ``error`` (error toast) and
        ``aborted`` (mtime_conflict warning) in their own classes.

        Symmetric pin per ``feedback_pin_invert_symmetric_assertion.md``:
        positive markers for each new branch + inverted assertion that
        the unconditional ``sync_success`` path is no longer reachable
        for ``error`` / ``aborted`` results. The pre-#799 shape only
        named ``'needs_confirmation'`` literal in the handler — pinning
        both ``'error'`` and ``'aborted'`` literals catches a regression
        that drops one branch back into the success fallthrough.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        # Both new statuses are inspected.
        assert "'error'" in text and "firstWithStatus('error')" in text, (
            "context-gateway.js Sync All must classify ``status === 'error'`` "
            "as an error toast (#799)"
        )
        assert "firstWithStatus('aborted')" in text, (
            "context-gateway.js Sync All must classify ``status === 'aborted'`` "
            "as an mtime_conflict warning (#799)"
        )
        # Reuses existing toast keys rather than introducing duplicates —
        # the per-target Sync flow already surfaces these classes the
        # same way.
        assert "t('toast.sync_failed'" in text, (
            "context-gateway.js Sync All ``error`` branch must reuse ``toast.sync_failed`` (#799)"
        )
        assert "t('settings.ctx.mtime_conflict')" in text, (
            "context-gateway.js Sync All ``aborted`` branch must reuse "
            "``settings.ctx.mtime_conflict`` (#799)"
        )
        # Inverted pin: the success fallthrough must remain reachable —
        # only for the all-``ok``/``skipped`` case. ``test_issue_774_*``
        # already pins the literal; this assertion guards a regression
        # that *removes* the else branch in the new severity ladder.
        assert "showToast(t('settings.ctx.sync_success'))" in text, (
            "context-gateway.js Sync All lost its success fallthrough — "
            "the new severity ladder collapsed to a single branch (#799)"
        )

    def test_issue_698_new_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Locale keys introduced for #698 must exist in both files. The
        existing ``test_placeholder_parity`` will catch ``{count}`` /
        ``{exts}`` / ``{tokens}`` / ``{files}`` / ``{chunks}`` mismatches
        between en and ko, so no separate placeholder check is needed."""
        required = {
            "header.stat_chunks_count_one",
            "header.stat_chunks_count_other",
            "header.stat_sources_count_one",
            "header.stat_sources_count_other",
            "header.stat_files_chunks",
            "tags.empty_msg",
            "tags.empty_hint",
            "search.adv_advanced",
            "settings.config.hint_extensions",
            "settings.config.hint_max_chunk",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"#698 keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"#698 keys missing from ko.json: {sorted(missing_ko)}"

    def test_pr_2_leaf_relabel_pin(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Pin the post-PR-2 leaf-page copy + tooltip/aria keys.

        Sibling of #813 (sidebar relabel): each leaf page (Skills, Custom
        Commands, Subagents, Hooks) now uses task-oriented descriptions
        and per-button directional tooltip + aria-label keys. Symmetric
        assertion (positive marker + negative ``not in``) catches both
        rollbacks to the generic ``Manage X definitions`` copy and
        rename/drop of any of the 26 new tooltip/aria keys."""
        # Positive markers — canonical phrasing must be present (en + ko)
        assert "Reusable workflows" in en["settings.ctx.skills_desc"]
        assert "slash commands" in en["settings.ctx.commands_desc"]
        assert "Specialized subagents" in en["settings.ctx.agents_desc"]
        assert "Lifecycle hooks" in en["settings.hooks.desc"]
        assert "재사용 워크플로우" in ko["settings.ctx.skills_desc"]
        assert "슬래시 명령어" in ko["settings.ctx.commands_desc"]
        assert "전문 서브에이전트" in ko["settings.ctx.agents_desc"]
        assert "라이프사이클 훅" in ko["settings.hooks.desc"]
        # Negative markers — generic pre-PR-2 copy must be gone
        for key in (
            "settings.ctx.skills_desc",
            "settings.ctx.commands_desc",
            "settings.ctx.agents_desc",
        ):
            assert "Manage" not in en[key], f"{key} still uses pre-PR-2 'Manage X' phrasing"
            assert "fan them out" not in en[key], (
                f"{key} still uses pre-PR-2 'fan them out' phrasing"
            )
            assert "정의를 관리하고" not in ko[key], (
                f"{key} still uses pre-PR-2 KO '정의를 관리하고' phrasing"
            )
        assert "Sync Claude Code hooks" not in en["settings.hooks.desc"]
        # Tooltip + aria-label key existence (13 buttons × 2 attrs = 26 keys)
        required_keys = {
            "settings.hooks.sync_now_tooltip",
            "settings.hooks.sync_now_aria",
        }
        for leaf in ("skills", "commands", "agents"):
            for action in ("add_project", "create", "import", "sync"):
                required_keys.add(f"settings.ctx.{leaf}_{action}_tooltip")
                required_keys.add(f"settings.ctx.{leaf}_{action}_aria")
        missing_en = required_keys - set(en)
        missing_ko = required_keys - set(ko)
        assert not missing_en, f"PR-2 tooltip/aria keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"PR-2 tooltip/aria keys missing from ko.json: {sorted(missing_ko)}"

    def test_q_pr1_required_keys_present(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Q-PR1 introduced new keys for the Context Gateway dashboard:

        * ``badge_empty`` — zero-state tile label (Bug-2).
        * ``status_parse_error`` — runtime badge label (Drift-4).
        * ``badge_error`` — own-namespace error label (Bug-3). Initially
          piggy-backed on ``settings.hooks.badge_error``; PR #824
          review pass-3 surfaced the cross-namespace coupling
          (``settings.ctx.*`` reading the hooks panel's translation
          values would silently drift if hooks ever relabels) and split
          it into a dedicated ctx key.

        And renamed the four ``detect``-named keys to ``refresh`` so the
        i18n surface matches the (already-Refresh) button copy (Drift-2).
        Pin them all."""
        required = {
            "settings.ctx.badge_empty",
            "settings.ctx.badge_error",
            "settings.ctx.status_parse_error",
            "settings.ctx.refresh",
            "settings.ctx.refresh_tooltip",
            "settings.ctx.refresh_aria",
            "toast.refresh_complete",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"Q-PR1 keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Q-PR1 keys missing from ko.json: {sorted(missing_ko)}"

    def test_q_pr1_no_legacy_detect_keys(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """The ``detect`` naming was an alias for what the handler always
        did — refresh the overview. Rename was a verbatim move (values
        unchanged); the legacy keys must not linger or a future caller
        could resurrect the inconsistency between button id, i18n, and
        toast copy."""
        legacy = {
            "settings.ctx.detect",
            "settings.ctx.detect_tooltip",
            "settings.ctx.detect_aria",
            "toast.detection_complete",
        }
        leftover_en = legacy & set(en)
        leftover_ko = legacy & set(ko)
        assert not leftover_en, f"Legacy detect keys still in en.json: {sorted(leftover_en)}"
        assert not leftover_ko, f"Legacy detect keys still in ko.json: {sorted(leftover_ko)}"

    def test_q_pr1_no_legacy_detect_in_ui_assets(self) -> None:
        """index.html and context-gateway.js must reference the renamed
        ``ctx-refresh-btn`` / ``settings.ctx.refresh*`` / ``toast.refresh_complete``
        symbols, not the legacy ``detect`` aliases. A drift between
        button id, i18n key, and toast key is exactly what Drift-2 was."""
        forbidden_patterns = [
            re.compile(r"\bctx-detect-btn\b"),
            re.compile(r"toast\.detection_complete\b"),
            re.compile(r"settings\.ctx\.detect(?:_tooltip|_aria)?\b(?!_)"),
        ]
        bad: list[str] = []
        for name in ("index.html", "context-gateway.js"):
            path = _STATIC_JS_DIR / name
            if not path.exists():
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for pat in forbidden_patterns:
                    if pat.search(line):
                        bad.append(f"  {name}:{lineno}: {line.strip()}")
                        break
        assert not bad, (
            "Found legacy 'detect' references after Drift-2 rename — replace "
            "with ctx-refresh-btn / settings.ctx.refresh* / toast.refresh_complete:\n"
            + "\n".join(bad)
        )

    def test_q_pr1_overview_badge_routes_error_through_t(self) -> None:
        """Bug-3 fix: the ``d.error`` branch in ``loadCtxOverview``'s badge
        ladder used to assign a raw ``'Error'`` literal, leaking English
        copy into Korean UIs. The branch must now route through
        ``t('settings.ctx.badge_error')`` — own-namespace, after PR #824
        review pass-3 split it from ``settings.hooks.badge_error`` so
        the dashboard's translation surface doesn't reach across into
        the hooks panel's keys (where a future hooks relabel would
        silently drift the ctx text)."""
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        m = re.search(
            r"if \(d\.error\) \{\s*\n(?:\s*//[^\n]*\n)*\s*badgeText = ([^;]+);",
            text,
        )
        assert m, "Could not locate the d.error branch in context-gateway.js loadCtxOverview"
        rhs = m.group(1).strip()
        assert "settings.ctx.badge_error" in rhs, (
            f"d.error branch must route through t('settings.ctx.badge_error'); got: {rhs!r}"
        )
        # Symmetric negative: the legacy hooks reach-across must not
        # creep back in. Distinct keys with similar values invite a
        # silent regression where a refactor "simplifies" two
        # almost-identical strings into one cross-namespace call.
        assert "settings.hooks.badge_error" not in rhs, (
            f"d.error branch must not cross into settings.hooks.* — "
            f"the dashboard owns its own badge_error key; got: {rhs!r}"
        )

    def test_q_pr1_overview_has_sequence_guard(self) -> None:
        """In-flight fetch race guard: ``loadCtxOverview`` must read +
        check a module-level sequence counter so a slow fetch from an
        earlier call cannot clobber the cards rendered by a later one.
        Lang toggles re-render from ``_ctxOverviewCache`` (#825) and do
        not fetch, but the cold-mount, Refresh button, and end-of-Sync-All
        paths can still race each other under rapid clicks; the guard
        protects all of those. The browser-side cache pin in
        ``test_context_gateway_overview.py`` covers the langchange path
        directly; this static check enforces the guard's structural
        invariants — counter + capture + bail-on-stale."""
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        assert "_ctxOverviewSeq" in text, (
            "missing _ctxOverviewSeq module counter — Bug-1 race guard"
        )
        assert re.search(r"const seq\s*=\s*\+\+_ctxOverviewSeq", text), (
            "loadCtxOverview must capture-and-bump _ctxOverviewSeq at entry"
        )
        # Both the success path (after innerHTML compute) and the catch
        # path must early-return when the captured seq is stale; without
        # the catch-path guard a late error overlay would clobber the
        # cards rendered by a newer toggle.
        guards = re.findall(r"if \(seq !==\s*_ctxOverviewSeq\)\s*return;", text)
        assert len(guards) >= 2, (
            f"expected sequence-guard returns in both success+catch paths, found {len(guards)}"
        )

    def test_q_pr1_langchange_listener_reloads_overview(self) -> None:
        """Bug-1 single-toggle pin: the ``langchange`` listener in
        context-gateway.js must re-render the inline-templated cards
        when the overview section is the active settings pane — and only
        then. After #825 the listener prefers ``_renderCtxOverview`` from
        ``_ctxOverviewCache`` (no fetch, no spinner flash) and falls back
        to ``loadCtxOverview`` only when the cache is empty (cold mount
        race / prior fetch error).

        ``#settings-ctx-overview`` always exists in the DOM regardless of
        which page the user is on; gating on mere element existence
        (``qs('ctx-overview-content')`` truthiness) would re-render on
        every language toggle from any page, not just the dashboard.
        The active-class gate matches ``switchSettingsSection``'s own
        ``section.classList.add('active')`` contract (app.js:1191).
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        body = _langchange_listener_body(text)
        # Cache-driven re-render path is the primary action; the
        # ``loadCtxOverview()`` cold-mount fallback covers the case when
        # the cache is empty (initial mount race or prior fetch error).
        # Both must be referenced inside the listener.
        assert "_renderCtxOverview(_ctxOverviewCache)" in body, (
            "langchange listener must re-render from _ctxOverviewCache directly "
            "(#825 — no fetch, no panelLoading flash on toggle)"
        )
        assert "loadCtxOverview()" in body, (
            "langchange listener must keep the loadCtxOverview() cold-mount "
            "fallback for when _ctxOverviewCache is null"
        )
        # Two-gate active check: both the main Settings tab
        # (``#tab-settings``) AND the Context Gateway settings sub-section
        # (``#settings-ctx-overview``) must carry ``.active`` before
        # reloading.
        #   * Section gate alone is insufficient: ``activateTab`` toggles
        #     panel-level ``.active`` + ``hidden`` but does not reach into
        #     sub-section classes, so leaving Settings for Search keeps
        #     the section's ``.active`` set and a toggle from Search
        #     would still hit /api/context/overview (PR #824 review P2).
        #   * Tab gate alone is insufficient: a user could be in Settings
        #     but viewing Hooks, where reloading the unmounted overview
        #     dashboard is wasted work.
        assert "tab-settings" in body, (
            "langchange listener must reference #tab-settings — "
            "without the main-tab gate, off-Settings toggles still fetch"
        )
        assert "settings-ctx-overview" in body, (
            "langchange listener must reference #settings-ctx-overview "
            "to gate the reload to the active pane"
        )
        # Counts: at least two ``classList.contains('active')`` calls (one
        # per gate). Quoting style matches the source — both single-quote
        # and double-quote forms accepted in case ruff reformats one day.
        active_calls = body.count("classList.contains('active')") + body.count(
            'classList.contains("active")'
        )
        assert active_calls >= 2, (
            f"langchange listener must check classList.contains('active') "
            f"on both #tab-settings and #settings-ctx-overview; found "
            f"{active_calls} call(s)"
        )

    def test_q_pr1_status_parse_error_mapped(self) -> None:
        """Drift-4: ``_ctxStatusLabel`` in context-gateway.js must map the
        ``'parse error'`` wire status to ``settings.ctx.status_parse_error``.
        Without this row ``_ctxStatusText`` falls back to the raw English
        wire string, defeating the whole status-label i18n surface for
        the runtime badges. The same module also defines ``_ctxStatusCls``
        which separately keys ``'parse error'`` (to a CSS class) — extract
        the label block first so the assertion targets the i18n map only."""
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        block_match = re.search(
            r"const _ctxStatusLabel\s*=\s*\{(.+?)\};",
            text,
            re.DOTALL,
        )
        assert block_match, "Could not locate _ctxStatusLabel block in context-gateway.js"
        block = block_match.group(1)
        m = re.search(r"'parse error':\s*'([^']+)'", block)
        assert m, "Missing 'parse error' key in _ctxStatusLabel — Drift-4 regression"
        assert m.group(1) == "settings.ctx.status_parse_error", (
            f"'parse error' must map to settings.ctx.status_parse_error; got: {m.group(1)!r}"
        )

    def test_q_pr4_langchange_listener_branches_per_type(self) -> None:
        """Q-PR4 (#826): the langchange listener must extend its overview-only
        gating from PR #824 to also cover the three per-type list sections
        (``settings-ctx-skills`` / ``settings-ctx-commands`` /
        ``settings-ctx-agents``). Without per-section refresh, inline-``t()``
        regions in the list view (``_ctxScopeBadges``,
        ``_ctxRefreshSectionState``, ``renderRuntimeBadges``,
        ``renderImportResult``) stale on toggle until the next user action
        rebuilds the list — the same class of staleness PR #824 fixed for
        the overview cards.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        body = _langchange_listener_body(text)
        # All three per-type sections must be referenced — using the
        # template literal ``settings-ctx-${type}`` is the canonical form,
        # but we accept literal strings too in case a refactor inlines them.
        for type_name in ("skills", "commands", "agents"):
            literal = f"settings-ctx-{type_name}"
            template_via_loop = "for (const type of ['skills'" in body and (
                "settings-ctx-${type}" in body
            )
            assert literal in body or template_via_loop, (
                f"langchange listener must reference {literal} (or iterate "
                f"['skills','commands','agents'] and template into "
                f"settings-ctx-${{type}})"
            )
        assert "loadCtxList(" in body, (
            "langchange listener must call loadCtxList() to rebuild the "
            "active per-type section's inline-t() regions"
        )

    def test_q_pr4_langchange_listener_routes_runtime_only_detail(self) -> None:
        """Q-PR4 (#826): if a detail pane is open at toggle time, the
        listener must route the re-mount through the *correct* loader.
        ``loadCtxDetail`` GETs the canonical file and 404s for runtime-only
        items; ``_ctxLoadRuntimeOnlyDetail`` uses the diff endpoint as
        preview source. Both must be reachable from the listener, gated by
        the ``runtimeOnly`` flag on ``_ctxCurrentDetail``.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        body = _langchange_listener_body(text)
        assert "loadCtxDetail(" in body, (
            "langchange listener must call loadCtxDetail() to re-mount the canonical detail pane"
        )
        assert "_ctxLoadRuntimeOnlyDetail(" in body, (
            "langchange listener must call _ctxLoadRuntimeOnlyDetail() to "
            "re-mount runtime-only detail panes (otherwise they 404 into "
            "emptyState — review finding P2)"
        )
        assert "_ctxCurrentDetail" in body, (
            "langchange listener must read _ctxCurrentDetail to find the "
            "currently-open detail name + runtimeOnly flag"
        )
        assert "runtimeOnly" in body, (
            "langchange listener must branch on the runtimeOnly flag to "
            "choose between loadCtxDetail and _ctxLoadRuntimeOnlyDetail"
        )

    def test_q_pr4_langchange_listener_preserves_diff_tab(self) -> None:
        """Q-PR4 (#826): if the Diff tab is the active pane when language
        toggles, ``loadCtxDetail`` must be re-invoked with
        ``autoOpenDiff: true`` so ``_ctxLoadDiff`` runs and re-renders
        ``renderDroppedChips``. Without active-tab capture, the re-mount
        lands on the Canonical pane and the Diff content (including chips)
        stays stale until the user clicks Diff manually — review finding
        P1.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        body = _langchange_listener_body(text)
        # Active-tab capture: must query for the .ctx-detail-tab[data-pane="diff"]
        # element and check whether it carries ``.active``.
        assert 'data-pane="diff"' in body or "data-pane='diff'" in body, (
            "langchange listener must locate the Diff tab via "
            '.ctx-detail-tab[data-pane="diff"] to capture active state'
        )
        assert ".active" in body, (
            "langchange listener must check the Diff tab's .active class "
            "before deciding autoOpenDiff"
        )
        assert "autoOpenDiff" in body, (
            "langchange listener must thread autoOpenDiff into loadCtxDetail() "
            "so the Diff pane loads (and renders dropped-chips) on re-mount"
        )

    def test_q_pr4_langchange_listener_clears_import_status(self) -> None:
        """Q-PR4 (#826): the post-Import status box (``ctx-${type}-status``)
        is rendered via inline ``t()`` in
        ``renderImportResult`` and would stale on toggle until the next
        Import or list reload. The listener clears it explicitly rather
        than caching the receipt — caching would resurrect a stale message
        in misleading form after navigation. Matches the
        ``statusEl.innerHTML = ''`` behavior at the top of ``loadCtxList``
        itself.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        body = _langchange_listener_body(text)
        # The clear happens implicitly via ``loadCtxList`` (which clears
        # ``statusEl.innerHTML`` near its top). Pin both the call and the
        # absence of any caching of the import-result payload.
        assert "loadCtxList(" in body, (
            "langchange listener must invoke loadCtxList() — its existing "
            "statusEl.innerHTML='' at the top of the function clears the "
            "Import status receipt"
        )
        # Symmetric negative: no rerender-from-cache for import results.
        assert "renderImportResult" not in body, (
            "langchange listener must NOT cache + re-render Import receipts; "
            "the status box is ephemeral by design"
        )

    def test_q_pr4_loadCtxList_has_sequence_guard_all_sites(self) -> None:
        """Q-PR4 (#826) review finding P2: the ``_ctxListSeq`` race-guard
        must protect *every* stale-write site, not just the success path.
        A late-failing fetch from a previous toggle would otherwise paint
        ``emptyState`` over the latest list, and a late
        ``_loadScopeGroupItems`` response would clobber the rebuilt
        ``ctx-scope-items`` container or re-insert a stale runtime-only
        banner.

        Sites that need guards:
        * ``loadCtxList`` success path (post-await, before listEl.innerHTML)
        * ``loadCtxList`` catch path (before emptyState write)
        * ``_loadScopeGroupItems`` success path (post-await, before
          container.innerHTML + _ctxRefreshSectionState)
        * ``_loadScopeGroupItems`` catch path (before emptyState write)
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        assert "_ctxListSeq" in text, "missing _ctxListSeq module counter — Q-PR4 list race guard"
        assert re.search(r"const seq\s*=\s*\+\+_ctxListSeq\[type\]", text), (
            "loadCtxList must capture-and-bump _ctxListSeq[type] at entry"
        )
        # The bail check appears as ``if (seq !== _ctxListSeq[type]) return;``
        # at every stale-write site. We expect ≥ 4 occurrences total
        # (loadCtxList success + catch, _loadScopeGroupItems success + catch).
        guards = re.findall(r"if \(seq !==\s*_ctxListSeq\[type\]\)\s*return;", text)
        assert len(guards) >= 4, (
            f"expected ≥4 _ctxListSeq guards (loadCtxList success+catch + "
            f"_loadScopeGroupItems success+catch); found {len(guards)}"
        )

    def test_q_pr4_listener_returns_after_overview_branch(self) -> None:
        """Q-PR4 (#826): the listener's overview branch must early-return
        so the per-type-list loop below does not double-fire. The Context
        Gateway sub-sections are mutually exclusive (one ``.active`` at a
        time per ``switchSettingsSection``), so falling through after the
        overview branch is wasted work — and would still be a correctness
        bug if a future refactor weakened the section-active invariant.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        body = _langchange_listener_body(text)
        # Find the overview branch (settings-ctx-overview gate) and
        # confirm it ends with ``return;`` before the per-type loop.
        # Using a permissive regex to avoid coupling to brace placement.
        overview_idx = body.find("settings-ctx-overview")
        assert overview_idx >= 0, "overview branch missing"
        per_type_idx = body.find("for (const type of [")
        assert per_type_idx > overview_idx, "per-type loop must come after the overview branch"
        between = body[overview_idx:per_type_idx]
        assert "return" in between, (
            "overview branch must early-return before falling through to the per-type-list loop"
        )

    def test_q_pr4_langchange_listener_preserves_edit_buffer(self) -> None:
        """Review finding (P1, data-loss): when the user has the canonical
        detail open in Edit mode with an unsaved textarea buffer, the
        listener's ``loadCtxDetail`` re-issue would overwrite the textarea
        with fresh server content — silently discarding their work.

        The listener must (a) read ``#ctx-pane-edit`` visibility and
        ``#ctx-edit-content`` value before ``loadCtxList`` resets the
        DOM, (b) thread that buffer through to a post-``loadCtxDetail``
        re-apply step. This is distinct from the 409 ``_ctxStashDraft``
        path which only triggers on the conflict dialog, not on a normal
        language toggle.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        # Slice from the listener's `addEventListener('langchange',` line
        # to the next "// -- " section comment header (or, defensively,
        # the next `// Sync All button` block which follows the listener).
        # See ``_langchange_listener_body`` for why this is a sentinel
        # slice rather than a lazy regex.
        body = _langchange_listener_body(text)
        # Must reference the edit-pane element + textarea so the dirty
        # buffer is captured before loadCtxList wipes it.
        assert "ctx-pane-edit" in body, (
            "langchange listener must read #ctx-pane-edit visibility to "
            "detect Edit mode and capture the dirty textarea buffer"
        )
        assert "ctx-edit-content" in body, (
            "langchange listener must read #ctx-edit-content (the textarea) "
            "to capture the user's unsaved buffer before re-mount"
        )
        # The capture+reapply pattern keys off ``loadCtxDetail`` returning
        # a Promise. The listener must thread a then(...) (or await) so
        # the buffer re-apply runs after the new detail HTML lands.
        assert ".then(" in body or "await loadCtxDetail" in body, (
            "langchange listener must wait for loadCtxDetail's Promise "
            "before re-applying the captured edit buffer (otherwise the "
            "re-apply targets the in-flight or wiped DOM)"
        )
        # Pre-toggle mtime must be captured *and* restored. ``loadCtxDetail``
        # overwrites ``detailEl.dataset.mtimeNs`` with the freshly-read
        # value, so without an explicit restore a post-toggle Save would
        # send the new mtime with the stale draft and the backend's 409
        # conflict gate (#763) would not fire — letting the user clobber
        # an external edit. The capture must reference ``dataset.mtimeNs``
        # *before* loadCtxList wipes it, and the restore must reference
        # it inside the post-loadCtxDetail then().
        assert "dataset.mtimeNs" in body, (
            "langchange listener must capture/restore detailEl.dataset.mtimeNs "
            "to preserve mtime conflict protection across the toggle "
            "(otherwise a concurrent on-disk edit gets silently overwritten)"
        )

    def test_q_pr4_detail_loaders_have_sequence_guard(self) -> None:
        """Review finding (P2): ``loadCtxDetail`` and
        ``_ctxLoadRuntimeOnlyDetail`` must guard their innerHTML writes
        with ``_ctxDetailSeq[type]``. Both paint the same ``detailEl``,
        so a stale fetch from a previous langchange / card-click can
        overwrite a fresher render — silently dropping a buffer the
        listener just rehydrated. Pin: per-type counter declared with
        all three keys; bumped at the entry of each loader; checked at
        the success and catch innerHTML write sites in both functions.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        assert re.search(
            r"_ctxDetailSeq\s*=\s*\{[^}]*skills[^}]*commands[^}]*agents[^}]*\}",
            text,
        ), "missing _ctxDetailSeq counter with all three keys"
        assert re.search(r"const seq\s*=\s*\+\+_ctxDetailSeq\[type\]", text), (
            "loadCtxDetail / _ctxLoadRuntimeOnlyDetail must capture-and-bump "
            "_ctxDetailSeq[type] at entry"
        )
        # Both loaders write innerHTML in success + catch paths. Expect
        # at least 3 guards in loadCtxDetail (404 fast-path + success +
        # catch) and 2 in _ctxLoadRuntimeOnlyDetail (success + catch).
        # Cumulative ≥5 guards across the file.
        guards = re.findall(r"if \(seq !==\s*_ctxDetailSeq\[type\]\)\s*return;", text)
        assert len(guards) >= 5, (
            f"expected ≥5 _ctxDetailSeq guards across loadCtxDetail and "
            f"_ctxLoadRuntimeOnlyDetail (success + catch sites in both, "
            f"plus the 404 fast-path); found {len(guards)}"
        )

    def test_q_pr4_listener_uses_pending_edit_module_stash(self) -> None:
        """Review finding (P2): edit-buffer preservation across rapid
        toggles requires a *module-level* stash, not a closure-local
        capture. With a closure capture, T2 (which finds an
        already-wiped DOM after T1's loadCtxList) yields ``null`` and
        T2's mount has no buffer to apply — silently dropped. The
        module-level ``_ctxPendingEdit`` carries forward; only the
        latest detail mount's ``.then()`` (gated by
        ``_ctxDetailSeq``) consumes it.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        # Module-level declaration.
        assert re.search(r"^let _ctxPendingEdit\s*=\s*null;", text, re.MULTILINE), (
            "missing module-level _ctxPendingEdit declaration"
        )
        # Listener reads + writes the stash + clears after consumption.
        body = _langchange_listener_body(text)
        assert "_ctxPendingEdit = {" in body, (
            "listener must populate _ctxPendingEdit with the captured buffer"
        )
        assert "_ctxPendingEdit = null" in body, (
            "listener must clear _ctxPendingEdit after the latest mount "
            "applies it (otherwise a subsequent unrelated mount would "
            "stamp stale content into the new textarea)"
        )
        # The .then() bails when its captured detail seq has been
        # superseded — this is what gives the *latest* mount sole
        # authority to consume the stash.
        assert re.search(
            r"myDetailSeq\s*!==\s*_ctxDetailSeq\[type\]",
            body,
        ), (
            "listener .then() must compare its captured myDetailSeq to "
            "_ctxDetailSeq[type] so older mounts skip the buffer apply "
            "(otherwise an older .then() races a newer one for the DOM)"
        )

    def test_pending_edit_navigation_drop_guards_against_orphan_resurrect(
        self,
    ) -> None:
        """P2 review: a langchange-then-navigate sequence must not orphan
        the ``_ctxPendingEdit`` stash. The fix is a navigation-drop guard
        at the top of ``loadCtxDetail`` and ``_ctxLoadRuntimeOnlyDetail``
        — every mount initiated by a path other than the langchange
        listener clears the stash before the new mount paints. The
        langchange listener, which IS the intended consumer, opts back
        in via ``opts.preservePendingEdit: true``.

        Pinning here rather than via Playwright because the
        resurrection scenario takes 4–5 user-initiated DOM events to
        reproduce reliably and a static-source guard catches the
        regression at the line that introduces it.

        Five invariants together close both the resurrection race and
        the rapid-toggle preservation race:

        1+2. Both detail-mount entry points (canonical + runtime-only)
             accept ``opts = {}`` and start with the navigation-drop
             guard ``if (!opts.preservePendingEdit) _ctxPendingEdit = null;``.
        3+4. The langchange listener calls each entry point with
             ``preservePendingEdit: true`` so the listener's own
             re-mount cycle does not clear the stash that its
             ``.then()`` is about to consume.
        5.   The listener's post-mount ``.then()`` does NOT clear the
             stash on the seq-mismatch (supersede) bail. A rapid
             same-card retoggle (L1 superseded by L2) hands the stash
             to L2's ``.then()``; clearing on supersede would race
             against L2's apply.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")

        # 1+2. Both entry points carry the guard.
        canonical_match = re.search(
            r"async\s+function\s+loadCtxDetail\s*\([^)]*opts\s*=\s*\{\}\s*\)\s*\{"
            r"(?:[^{}]|\{[^{}]*\})*?"
            r"if\s*\(\s*!\s*opts\.preservePendingEdit\s*\)\s*\{\s*"
            r"_ctxPendingEdit\s*=\s*null;\s*\}",
            text,
        )
        assert canonical_match, (
            "loadCtxDetail must drop _ctxPendingEdit at entry unless "
            "opts.preservePendingEdit is true — without the guard a "
            "langchange-then-navigate sequence orphans the stash and a "
            "future remount of the original card resurrects stale draft "
            "content (review P2)"
        )
        runtime_only_match = re.search(
            r"async\s+function\s+_ctxLoadRuntimeOnlyDetail\s*\([^)]*opts\s*=\s*\{\}\s*\)\s*\{"
            r"(?:[^{}]|\{[^{}]*\})*?"
            r"if\s*\(\s*!\s*opts\.preservePendingEdit\s*\)\s*\{\s*"
            r"_ctxPendingEdit\s*=\s*null;\s*\}",
            text,
        )
        assert runtime_only_match, (
            "_ctxLoadRuntimeOnlyDetail must mirror loadCtxDetail's "
            "navigation-drop guard. A user navigating from an edited "
            "canonical card to a runtime-only card otherwise leaves the "
            "stash live for a future remount of the original card."
        )

        # 3+4. Langchange listener opts both mounts in to preservation.
        body = _langchange_listener_body(text)

        canonical_call_match = re.search(
            r"loadCtxDetail\s*\(\s*type\s*,\s*openName\s*,\s*\{"
            r"(?:[^{}]|\{[^{}]*\})*?"
            r"preservePendingEdit\s*:\s*true",
            body,
        )
        assert canonical_call_match, (
            "langchange listener's canonical-detail call must pass "
            "preservePendingEdit: true. Without it the listener's own "
            "loadCtxDetail invocation clears the stash before its "
            ".then() can apply it (rapid-toggle regression)."
        )
        runtime_only_call_match = re.search(
            r"_ctxLoadRuntimeOnlyDetail\s*\(\s*type\s*,\s*openName\s*,"
            r"\s*detailEl\s*,\s*\{"
            r"(?:[^{}]|\{[^{}]*\})*?"
            r"preservePendingEdit\s*:\s*true",
            body,
        )
        assert runtime_only_call_match, (
            "langchange listener's runtime-only call must also pass "
            "preservePendingEdit: true — symmetric with the canonical "
            "path so a runtime-only-card retoggle preserves its buffer."
        )

        # 5. Supersede bail does NOT clear the stash.
        # The seq-mismatch branch must be a bare ``return;`` — any
        # stash-clear there would race against a rapid-toggle L2's
        # apply path. The navigation-drop guard at the loadCtxDetail
        # entry is the single source of orphan cleanup.
        supersede_match = re.search(
            r"if\s*\(\s*myDetailSeq\s*!==\s*_ctxDetailSeq\[type\]\s*\)\s*return;",
            body,
        )
        assert supersede_match, (
            "supersede branch in the langchange listener .then() must "
            "be a bare ``return;`` — clearing _ctxPendingEdit there "
            "would race against a rapid-toggle L2's apply path. The "
            "navigation-drop guard at loadCtxDetail entry is the "
            "single source of orphan cleanup."
        )

    def test_q_pr4_ctxCurrentDetail_carries_runtime_only_flag(self) -> None:
        """Q-PR4 (#826) review finding P2: ``_ctxCurrentDetail`` must carry
        a ``runtimeOnly`` flag so the langchange listener can route the
        re-mount through the correct loader. The flag is set at the two
        call-sites that mount a detail pane (``loadCtxDetail`` →
        ``runtimeOnly: false``; ``_ctxLoadRuntimeOnlyDetail`` →
        ``runtimeOnly: true``) and reset at the top of ``loadCtxList``.
        Without the flag the listener cannot disambiguate canonical vs
        runtime-only items after the list is rebuilt — the card's
        ``data-canonical-path`` attribute is gone with the wiped DOM.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        # Declaration includes the field with a default.
        assert re.search(
            r"_ctxCurrentDetail\s*=\s*\{[^}]*runtimeOnly\s*:\s*false[^}]*\}",
            text,
        ), "_ctxCurrentDetail declaration must include runtimeOnly: false"
        # Canonical loader sets runtimeOnly: false.
        canonical_assigns = re.findall(
            r"_ctxCurrentDetail\s*=\s*\{\s*type\s*,\s*name\s*,\s*runtimeOnly\s*:\s*false\s*\}",
            text,
        )
        assert canonical_assigns, "loadCtxDetail must set _ctxCurrentDetail.runtimeOnly = false"
        # Runtime-only loader sets runtimeOnly: true.
        runtime_assigns = re.findall(
            r"_ctxCurrentDetail\s*=\s*\{\s*type\s*,\s*name\s*,\s*runtimeOnly\s*:\s*true\s*\}",
            text,
        )
        assert runtime_assigns, (
            "_ctxLoadRuntimeOnlyDetail must set _ctxCurrentDetail.runtimeOnly = true"
        )

    def test_q_pr2_nav_label_translated_in_ko(self, en: dict[str, str], ko: dict[str, str]) -> None:
        """Q-PR2 Drift-1: the Context Gateway sidebar nav label
        (``settings.nav.ctx_overview``) used to be the English literal
        ``"Context Gateway"`` in ko.json, while the body H2
        (``settings.ctx.overview_title``) was already
        ``"컨텍스트 게이트웨이"``. The KO sidebar→body inconsistency was
        the bug; pin the symmetric pair so a future revert fails on both
        the positive value and the sidebar/H2 parity that the user sees.

        Negative cross-locale assertion catches the silent ``ko = en``
        regression that ``test_ko_has_all_en_keys`` (key parity) cannot
        detect — that test only checks the *key* exists in ko, not that
        the *value* is translated."""
        assert ko["settings.nav.ctx_overview"] == "컨텍스트 게이트웨이", (
            f"ko sidebar label must be translated; got: {ko['settings.nav.ctx_overview']!r}"
        )
        assert ko["settings.nav.ctx_overview"] != en["settings.nav.ctx_overview"], (
            "ko settings.nav.ctx_overview must not silently equal the en "
            "value (Drift-1 regression — KO sidebar showing English literal)"
        )
        assert ko["settings.nav.ctx_overview"] == ko["settings.ctx.overview_title"], (
            "KO sidebar label and body H2 must match — that parity is what "
            "the user actually sees when clicking the nav entry"
        )

    def test_q_pr2_overview_desc_is_tier_agnostic(
        self, en: dict[str, str], ko: dict[str, str]
    ) -> None:
        """Q-PR2 Drift-3: the Context Gateway dashboard description must
        not enumerate individual tile names. The desc is rendered
        unconditionally via ``data-i18n="settings.ctx.overview_desc"``
        and the tile set evolves over time (Custom Commands graduated
        from dev to prod alongside Skills/Subagents/Hooks), so any
        enumeration in the desc is wrong as soon as a tile is added,
        removed, or renamed.

        Word-bounded, case-sensitive, each token checked separately so a
        creative future regression (``"Subagents and Skills"``,
        ``"Skills/Hooks"``, etc.) is caught with the same strictness as
        the original ``"Skills, Subagents, and Hooks"`` phrasing.

        KO uses substring (not word-bounded) because Korean has no word
        boundary character; ``"훅"`` matches inside ``"훅을"`` etc. — that's
        the desired strictness, since the desc is intentionally
        tile-agnostic and shouldn't mention the term at all in any
        inflection.

        Positive anchors keep ``"Claude Code"`` + ``"Codex"`` in both
        locales — those are the runtime-name examples that survive
        generalization and removing them would also be a copy-polish
        regression."""
        forbidden_en = {"Skills", "Subagents", "Hooks"}
        desc_en = en["settings.ctx.overview_desc"]
        present_en = {t for t in forbidden_en if re.search(rf"\b{t}\b", desc_en)}
        assert not present_en, (
            f"en overview_desc reintroduced tile-name enumeration: "
            f"{sorted(present_en)} (must stay tile-agnostic so the desc "
            f"doesn't lie when the tile set changes)"
        )

        forbidden_ko = {"스킬", "서브에이전트", "훅"}
        desc_ko = ko["settings.ctx.overview_desc"]
        present_ko = {t for t in forbidden_ko if t in desc_ko}
        assert not present_ko, (
            f"ko overview_desc reintroduced tile-name enumeration: "
            f"{sorted(present_ko)} (must stay tile-agnostic)"
        )

        for locale_name, desc in (("en", desc_en), ("ko", desc_ko)):
            assert "Claude Code" in desc, (
                f"{locale_name} overview_desc must still anchor 'Claude Code' "
                f"as a concrete runtime example"
            )
            assert "Codex" in desc, (
                f"{locale_name} overview_desc must still anchor 'Codex' "
                f"as a concrete runtime example"
            )

    def test_q_pr3_overview_desc_inline_fallback_matches_locale(self, en: dict[str, str]) -> None:
        """Q-PR3 Codex minor 1: the inline ``<p>`` text inside
        ``data-i18n="settings.ctx.overview_desc"`` is the **fallback**
        rendered when ``i18n.js`` hasn't yet replaced the node — most
        commonly during the first paint before ``/locales/{lang}.json``
        arrives, but also if the fetch fails. Q-PR2 generalized the
        locale value ("Sync memtomem agent runtime artifacts to Claude
        Code, Codex, and other detected runtimes.") but left the inline
        fallback at index.html:773 frozen on the pre-Q-PR2 enumerative
        copy ("Skills, Subagents, and Hooks…"). This pin keeps the
        fallback equal to the en locale value so a future locale-only
        edit can't silently drift the fallback again."""
        html = (_STATIC_JS_DIR / "index.html").read_text(encoding="utf-8")
        m = re.search(
            r'<p[^>]*data-i18n="settings\.ctx\.overview_desc"[^>]*>([^<]+)</p>',
            html,
        )
        assert m, (
            'Could not locate the data-i18n="settings.ctx.overview_desc" '
            "<p> in index.html — the markup shape changed and this pin "
            "needs to be updated."
        )
        inline_fallback = m.group(1)
        expected = en["settings.ctx.overview_desc"]
        assert inline_fallback == expected, (
            f"index.html inline fallback for settings.ctx.overview_desc "
            f"drifted from the en locale value.\n"
            f"  inline:   {inline_fallback!r}\n"
            f"  en value: {expected!r}"
        )
        # Symmetric negative: catch the pre-Q-PR2 enumerative copy
        # creeping back even if the en value is also reverted (single
        # point of failure for the equality check above).
        assert "Skills, Subagents" not in inline_fallback, (
            "index.html inline fallback reintroduced the pre-Q-PR2 tile "
            "enumeration; Drift-3 / Q-PR2 generalized this away — see "
            "test_q_pr2_overview_desc_is_tier_agnostic."
        )

    def test_q_pr3_settings_status_replace_is_global(self) -> None:
        """Q-PR3 Visual-4: the settings tile's badge-text fallthrough for
        unknown ``d.status`` values uses ``replace(/_/g, ' ')`` (global
        regex) so a future multi-underscore status — for example
        ``needs_user_confirm`` — renders as ``"needs user confirm"``.

        The pre-Q-PR3 form ``replace('_', ' ')`` only swapped the FIRST
        underscore, producing ``"needs user_confirm"``: a silent partial-
        translation that the existing ``_SETTINGS_STATUS_I18N`` map would
        need to grow alongside every new status to mask. The global form
        keeps the defensive fallback robust without coupling each new
        status string to a synchronous map update.

        Static-source pin: looks for the regex form on the settings
        branch and explicitly forbids the single-arg literal form.
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        # Positive — the global regex form is present on the settings
        # status fallthrough.
        assert re.search(
            r"\(d\.status\s*\|\|\s*''\)\.replace\(\/_\/g,\s*' '\)",
            text,
        ), (
            "Settings tile badge-text fallthrough must use "
            "replace(/_/g, ' ') so multi-underscore statuses render fully — "
            "see Visual-4 in the Q-PR3 plan."
        )
        # Symmetric negative — the single-replace literal form must not
        # appear on a status fallthrough. The pattern is narrow enough
        # to exclude unrelated single-replace calls.
        assert not re.search(
            r"\(d\.status\s*\|\|\s*''\)\.replace\('_',\s*' '\)",
            text,
        ), (
            "Found legacy replace('_', ' ') (single replace) on the "
            "settings status fallthrough — this drops every underscore "
            "after the first. Use replace(/_/g, ' ') instead (Visual-4)."
        )

    def test_q_pr3_settings_tile_count_not_glyph(self) -> None:
        """Q-PR3 Visual-1 (static pin for the part the Playwright spec
        also exercises dynamically): the big-number slot of the settings
        tile must render the generic ``${total}`` like the other three
        tiles, not the legacy ``typ.key === 'settings' ? glyph : total``
        ternary (``\\u2714`` for in_sync, ``\\u26A0`` otherwise).

        Catches a frontend-only revert that brings back the visual-weight
        asymmetry without needing the browser harness. The Playwright
        spec stubs the API and asserts the rendered text; this pin
        catches the source change at the layer above."""
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        m = re.search(
            r'<div class="ctx-overview-count">\$\{([^}]+)\}</div>',
            text,
        )
        assert m, (
            "Could not locate the .ctx-overview-count template "
            "expression — the markup shape changed and this pin needs "
            "to be updated."
        )
        expr = m.group(1).strip()
        assert expr == "total", (
            f"ctx-overview-count must render ${{total}} for every tile "
            f"(Q-PR3 Visual-1: settings tile aligned with the other three). "
            f"Got: {expr!r}"
        )
        # Symmetric negative — the legacy glyph branch markers must not
        # appear in the rendering line.
        assert "✔" not in m.group(0), (
            "Found legacy '\\u2714' glyph on the count line — Visual-1 "
            "removed the per-tile glyph branch."
        )
        assert "⚠" not in m.group(0).lower(), (
            "Found legacy '\\u26A0' glyph on the count line — Visual-1 "
            "removed the per-tile glyph branch."
        )

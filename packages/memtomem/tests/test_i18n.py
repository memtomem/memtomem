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
            "toast.saved_n_indexed",
            "toast.upload_complete",
            "toast.tagged_count",
            "toast.stream_complete",
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
            "toast.detection_complete",
            "toast.name_required",
        }
        missing_en = required - set(en)
        missing_ko = required - set(ko)
        assert not missing_en, f"Keys missing from en.json: {sorted(missing_en)}"
        assert not missing_ko, f"Keys missing from ko.json: {sorted(missing_ko)}"

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
        """Q-PR1 introduced two new keys for the Context Gateway dashboard
        (``badge_empty`` for the zero-state tile, ``status_parse_error`` for
        the runtime badge label) and renamed the four ``detect``-named keys
        to ``refresh`` so the i18n surface matches the (already-Refresh)
        button copy. Pin both."""
        required = {
            "settings.ctx.badge_empty",
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
        ``t('settings.hooks.badge_error')`` (the same key the Settings
        tile already uses, so we don't introduce a fresh badge/error key
        for a single-callsite path)."""
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        m = re.search(
            r"if \(d\.error\) \{\s*\n\s*badgeText = ([^;]+);",
            text,
        )
        assert m, "Could not locate the d.error branch in context-gateway.js loadCtxOverview"
        rhs = m.group(1).strip()
        assert "t(" in rhs and "badge_error" in rhs, (
            f"d.error branch must route through t('settings.hooks.badge_error'); got: {rhs!r}"
        )

    def test_q_pr1_overview_has_sequence_guard(self) -> None:
        """Bug-1 multi-toggle race guard: ``loadCtxOverview`` must read +
        check a module-level sequence counter so a slow fetch from an
        earlier toggle cannot clobber the cards rendered by a later
        toggle. The browser test in ``test_context_gateway_overview.py``
        pins the multi-toggle *outcome* but cannot deterministically
        reproduce the race window (Playwright sync route handlers serialize
        on the dispatcher thread, so a delay on one fetch also delays the
        next). The guard itself — counter + capture + bail-on-stale — is
        what this static check enforces."""
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
        context-gateway.js must call ``loadCtxOverview`` when the
        overview section is the active settings pane — and only then.

        ``#settings-ctx-overview`` always exists in the DOM regardless of
        which page the user is on; gating on mere element existence
        (``qs('ctx-overview-content')`` truthiness) would fire a fetch on
        every language toggle from any page, not just the dashboard.
        The active-class gate matches ``switchSettingsSection``'s own
        ``section.classList.add('active')`` contract (app.js:1191).
        """
        text = (_STATIC_JS_DIR / "context-gateway.js").read_text(encoding="utf-8")
        m = re.search(
            r"window\.addEventListener\('langchange',\s*\(\)\s*=>\s*\{(.+?)\}\);",
            text,
            re.DOTALL,
        )
        assert m, "langchange listener missing from context-gateway.js"
        body = m.group(1)
        assert "loadCtxOverview()" in body, (
            "langchange listener must call loadCtxOverview() for the inline-templated cards"
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

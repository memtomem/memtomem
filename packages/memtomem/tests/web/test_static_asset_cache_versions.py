"""Content-aware browser cache-version contract (issue #2041).

Unlike the old literal ``?v=N`` assertions, these tests bind every public
version to the SHA-256 of the bytes it names.  They are deliberately ordinary
Python tests (not Playwright-marked), so the contract runs in every default CI
partition without a browser or a Git base.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from memtomem.web import static_cache_manifest as scm


def _asset_reference(path: str = "/a.js", version: str = "1") -> scm.AssetReference:
    return scm.AssetReference(path=path, version=version, surface="fixture.html")


def _fixture_manifest(asset_path: Path, *, version: str = "1") -> scm.StaticCacheManifest:
    return scm.StaticCacheManifest(
        assets={f"/{asset_path.name}": {version: scm.file_sha256(asset_path)}},
        excluded={},
    )


def test_real_static_tree_matches_the_content_aware_manifest(
    run_async: Callable[[Coroutine[Any, Any, Any]], Any],
) -> None:
    manifest = scm.load_manifest()
    # ``run_async`` instead of an ``async def`` test: a browser spec earlier in
    # the session can leave a running event loop on MainThread (#2099).
    references = run_async(scm.collect_runtime_references())

    assert scm.MANIFEST_PATH.parent == scm.WEB_DIR
    assert scm.MANIFEST_PATH.parent != scm.STATIC_DIR
    assert "/app.js" in manifest.assets
    assert "/vendor/swagger/swagger-init.js" in manifest.assets
    assert set(manifest.assets) == scm.discover_static_assets()
    assert not scm.contract_errors(manifest, references)
    assert scm.canonical_manifest_error(manifest) is None


def test_no_store_locales_are_explicitly_excluded() -> None:
    manifest = scm.load_manifest()
    assert set(manifest.excluded) == {"/locales/en.json", "/locales/ko.json"}
    assert all((scm.STATIC_DIR / path.removeprefix("/")).is_file() for path in manifest.excluded)
    i18n = (scm.STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
    locale_fetch = re.search(
        r"fetch\s*\(\s*(?P<url>[^,)]*/locales/[^,)]*\.json[^,)]*)\s*,\s*"
        r"\{(?P<options>[^}]*)\}\s*\)",
        i18n,
    )
    assert locale_fetch is not None
    assert re.search(r"\bcache\s*:\s*['\"]no-store['\"]", locale_fetch.group("options"))


def test_changed_bytes_with_the_same_public_version_fail(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("old bytes\n", encoding="utf-8")
    manifest = _fixture_manifest(asset)
    reference = _asset_reference()

    asset.write_text("new bytes\n", encoding="utf-8")

    errors = scm.contract_errors(manifest, [reference], static_dir=static)
    assert any("bytes changed without a cache-version bump" in error for error in errors)
    with pytest.raises(scm.StaticCacheManifestError, match="increment the public"):
        scm.updated_manifest(manifest, [reference], static_dir=static)


def test_bumped_reference_still_fails_until_the_manifest_is_updated(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("old bytes\n", encoding="utf-8")
    manifest = _fixture_manifest(asset)
    asset.write_text("new bytes\n", encoding="utf-8")
    bumped = _asset_reference(version="2")

    errors = scm.contract_errors(manifest, [bumped], static_dir=static)
    assert any("is not the newest recorded version" in error for error in errors)

    updated = scm.updated_manifest(manifest, [bumped], static_dir=static)
    assert updated.assets["/a.js"]["1"] == manifest.assets["/a.js"]["1"]
    assert updated.assets["/a.js"]["2"] == scm.file_sha256(asset)
    assert not scm.contract_errors(updated, [bumped], static_dir=static)


def test_updater_requires_the_next_numeric_version(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("old bytes\n", encoding="utf-8")
    manifest = _fixture_manifest(asset)
    asset.write_text("new bytes\n", encoding="utf-8")

    with pytest.raises(scm.StaticCacheManifestError, match="must be 2, got 3"):
        scm.updated_manifest(manifest, [_asset_reference(version="3")], static_dir=static)


def test_numeric_version_order_handles_nine_to_ten(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("version nine\n", encoding="utf-8")
    manifest = _fixture_manifest(asset, version="9")
    asset.write_text("version ten\n", encoding="utf-8")

    updated = scm.updated_manifest(manifest, [_asset_reference(version="10")], static_dir=static)

    assert list(updated.assets["/a.js"]) == ["9", "10"]
    rendered = json.loads(scm.render_manifest(updated))
    assert list(rendered["assets"]["/a.js"]["versions"]) == ["9", "10"]
    assert not scm.contract_errors(updated, [_asset_reference(version="10")], static_dir=static)


def test_new_asset_can_bootstrap_its_first_binding(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("first bytes\n", encoding="utf-8")
    empty = scm.StaticCacheManifest(assets={}, excluded={})

    updated = scm.updated_manifest(empty, [_asset_reference()], static_dir=static)

    assert updated.assets == {"/a.js": {"1": scm.file_sha256(asset)}}


def test_new_asset_must_start_at_version_one(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("first bytes\n", encoding="utf-8")
    empty = scm.StaticCacheManifest(assets={}, excluded={})

    with pytest.raises(scm.StaticCacheManifestError, match="must start at public version 1"):
        scm.updated_manifest(empty, [_asset_reference(version="2")], static_dir=static)


def test_missing_or_conflicting_reference_sets_fail(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("bytes\n", encoding="utf-8")
    manifest = _fixture_manifest(asset)

    missing = scm.contract_errors(manifest, [], static_dir=static)
    assert any("missing a versioned HTML reference" in error for error in missing)
    assert any("no longer referenced" in error for error in missing)

    conflicting = scm.contract_errors(
        manifest,
        [_asset_reference(version="1"), _asset_reference(version="2")],
        static_dir=static,
    )
    assert any("conflicting public versions" in error for error in conflicting)


@pytest.mark.parametrize(
    ("rel", "extra"),
    [("modulepreload", ""), ("preload", ' as="script"')],
)
def test_preload_conflict_reports_only_the_actual_problem(
    tmp_path: Path, rel: str, extra: str
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("bytes\n", encoding="utf-8")
    manifest = _fixture_manifest(asset)
    references = scm.references_from_html(
        f'<script src="/a.js?v=1"></script><link rel="{rel}"{extra} href="/a.js?v=2">',
        surface="fixture.html",
    )

    errors = scm.contract_errors(manifest, references, static_dir=static)

    assert len(references) == 2
    assert errors == ["/a.js: conflicting public versions 1 and 2 (fixture.html)"]
    with pytest.raises(scm.StaticCacheManifestError, match="conflicting public versions"):
        scm.updated_manifest(manifest, references, static_dir=static)


def test_updater_refuses_orphan_manifest_entries(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("bytes\n", encoding="utf-8")
    manifest = _fixture_manifest(asset)
    manifest.assets["/retired.js"] = {"1": "0" * 64}

    with pytest.raises(
        scm.StaticCacheManifestError,
        match="remove retired cache-manifest entries explicitly",
    ):
        scm.updated_manifest(manifest, [_asset_reference()], static_dir=static)


def test_exclusions_must_exist_and_cannot_hide_cacheable_assets(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    missing = scm.StaticCacheManifest(assets={}, excluded={"/missing.json": "fixture exclusion"})
    assert scm.contract_errors(missing, [], static_dir=static) == [
        "excluded paths missing on disk: ['/missing.json']"
    ]

    cacheable = static / "hidden.js"
    cacheable.write_text("bytes\n", encoding="utf-8")
    invalid = scm.StaticCacheManifest(
        assets={}, excluded={"/hidden.js": "invalid fixture exclusion"}
    )
    errors = scm.contract_errors(invalid, [], static_dir=static)
    assert any("must not be cacheable JS/CSS" in error for error in errors)
    assert any("missing a versioned HTML reference" in error for error in errors)


@pytest.mark.parametrize(
    "url",
    [
        "/a.js",
        "/a.js?v=0",
        "/a.js?v=01",
        "/a.js?v=1&debug=1",
        "https://example.com/a.js?v=1",
        "/a.js?v=1#fragment",
        "/dir\\a.js?v=1",
    ],
)
def test_html_reference_parser_rejects_noncanonical_cache_urls(url: str) -> None:
    with pytest.raises(scm.StaticCacheManifestError):
        scm.references_from_html(f'<script src="{url}"></script>', surface="fixture.html")


def test_manifest_parser_rejects_duplicate_and_unsafe_paths(tmp_path: Path) -> None:
    digest = "0" * 64
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"assets":{},"assets":{},"excluded":{}}',
        encoding="utf-8",
    )
    with pytest.raises(scm.StaticCacheManifestError, match="duplicate JSON key"):
        scm.load_manifest(duplicate)

    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": {"/../a.js": {"versions": {"1": digest}}},
                "excluded": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(scm.StaticCacheManifestError, match="unsafe asset path"):
        scm.load_manifest(unsafe)


def test_manifest_parser_rejects_asset_exclusion_overlap(tmp_path: Path) -> None:
    path = tmp_path / "overlap.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": {"/a.js": {"versions": {"1": "0" * 64}}},
                "excluded": {"/a.js": "invalid overlap"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(scm.StaticCacheManifestError, match="both assets and excluded"):
        scm.load_manifest(path)


def test_canonical_manifest_error_rejects_noncanonical_json(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "a.js"
    asset.write_text("bytes\n", encoding="utf-8")
    manifest = _fixture_manifest(asset)
    path = tmp_path / "cache-versions.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": {"/a.js": {"versions": manifest.assets["/a.js"]}},
                "excluded": {},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    assert scm.canonical_manifest_error(manifest, path=path) == (
        f"{path} is not canonical; run the cache manifest updater with --write"
    )


def test_run_async_survives_a_running_loop_on_the_main_thread(
    run_async: Callable[[Coroutine[Any, Any, Any]], Any],
) -> None:
    """Pin the property the thread hop exists for (#2099).

    Without a parked loop this fixture is indistinguishable from ``asyncio.run``,
    and the regression it guards only appears on a machine with Chromium — so
    the state Playwright leaves behind is staged here directly: MainThread
    registered as running a loop, exactly what
    ``sync_api/_context_manager.py``'s dispatcher greenlet does for the whole
    session.
    """

    # Restore whatever was there rather than clearing to ``None``: under a real
    # browser run Playwright's dispatcher loop *is* the previous value, and
    # clearing it breaks its teardown with "Browser.close: no running event loop".
    previous = asyncio.events._get_running_loop()
    loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(loop)
    try:
        blocked = _thread_name()
        try:
            with pytest.raises(RuntimeError, match="running event loop"):
                asyncio.run(blocked)
        finally:
            blocked.close()  # never awaited — closing it keeps the warning filter honest

        assert run_async(_thread_name()) != threading.main_thread().name
    finally:
        asyncio.events._set_running_loop(previous)
        loop.close()


async def _thread_name() -> str:
    return threading.current_thread().name

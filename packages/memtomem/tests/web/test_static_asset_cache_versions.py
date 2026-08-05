"""Content-aware browser cache-version contract (issue #2041).

Unlike the old literal ``?v=N`` assertions, these tests bind every public
version to the SHA-256 of the bytes it names.  They are deliberately ordinary
Python tests (not Playwright-marked), so the contract runs in every default CI
partition without a browser or a Git base.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem.web import static_cache_manifest as scm


def _asset_reference(path: str = "/a.js", version: str = "1") -> scm.AssetReference:
    return scm.AssetReference(path=path, version=version, surface="fixture.html")


def _fixture_manifest(asset_path: Path, *, version: str = "1") -> scm.StaticCacheManifest:
    return scm.StaticCacheManifest(
        assets={f"/{asset_path.name}": {version: scm.file_sha256(asset_path)}},
        excluded={},
    )


async def test_real_static_tree_matches_the_content_aware_manifest() -> None:
    manifest = scm.load_manifest()
    references = await scm.collect_runtime_references()

    assert "/app.js" in manifest.assets
    assert "/vendor/swagger/swagger-init.js" in manifest.assets
    assert set(manifest.assets) == scm.discover_static_assets()
    assert not scm.contract_errors(manifest, references)
    assert scm.canonical_manifest_error(manifest) is None


def test_no_store_locales_are_explicitly_excluded() -> None:
    manifest = scm.load_manifest()
    assert set(manifest.excluded) == {"/locales/en.json", "/locales/ko.json"}
    i18n = (scm.STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
    assert "fetch(`/locales/${lang}.json`, { cache: 'no-store' })" in i18n


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

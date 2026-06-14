"""Supply-chain guard for the vendored browser assets (#1252).

Offline + cross-platform: re-hashes every file in ``web/static/vendor/`` against
the SHA-256 pinned in ``THIRD_PARTY_LICENSES.md`` and checks the table covers
exactly the shipped third-party files (modulo the first-party bootstrap
allowlist). Pairs with the ``vendored-assets`` CI job (OSV advisory scan): this
side proves the bytes are the pinned bytes; that side proves the pinned versions
carry no known advisory. The strict-parser cases below pin the fail-closed
behaviour so a malformed table can't silently skip a row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.web import vendor_manifest as vm

_HEADER = "| File | npm package | Version | License | Upstream | SHA-256 (full) |"
_SEP = "| --- | --- | --- | --- | --- | --- |"
_HEX = "0" * 64
_GOOD_ROW = f"| `a.js` | `pkg` | 1.2.3 | MIT | https://x/npm/pkg@1.2.3/a.js | `{_HEX}` |"


def _write_table(tmp_path: Path, *rows: str, header: str = _HEADER, sep: str = _SEP) -> Path:
    p = tmp_path / "T.md"
    p.write_text("\n".join(["# heading", "", header, sep, *rows, ""]), encoding="utf-8")
    return p


# --- real-tree guards -----------------------------------------------------


def test_table_parses_and_is_nonempty() -> None:
    assets = vm.parse_vendor_table()
    assert assets
    # vacuous-pass guard: the libraries we know we ship are present by npm name.
    assert {"dompurify", "prismjs", "marked", "swagger-ui-dist"} <= {a.npm for a in assets}


@pytest.mark.parametrize("asset", vm.parse_vendor_table(), ids=lambda a: a.file)
def test_pinned_sha256_matches_disk(asset: vm.VendorAsset) -> None:
    assert asset.path().is_file(), f"pinned file missing on disk: {asset.file}"
    assert asset.disk_sha256() == asset.sha256, (
        f"{asset.file}: on-disk SHA-256 != the pin in THIRD_PARTY_LICENSES.md. A version "
        "bump must update the SHA column; an unexplained mismatch on a same-version file is "
        "a supply-chain red flag (see web/static/vendor/README.md)."
    )


def test_table_covers_shipped_assets_both_ways() -> None:
    pinned = {a.file for a in vm.parse_vendor_table()}
    shipped = set(vm.iter_shipped_vendor_files())
    assert not vm.coverage_errors(pinned, shipped), "; ".join(vm.coverage_errors(pinned, shipped))


def test_first_party_files_present_and_not_pinned() -> None:
    pinned = {a.file for a in vm.parse_vendor_table()}
    assert vm.FIRST_PARTY_VENDOR_FILES, "first-party allowlist unexpectedly empty"
    for rel in vm.FIRST_PARTY_VENDOR_FILES:
        assert (vm.VENDOR_DIR / rel).is_file(), f"first-party file vanished: {rel}"
        assert rel not in pinned, f"{rel} is first-party; it must not be a third-party pin row"


# --- strict-parser fail-closed cases (synthetic tables) -------------------


def test_parse_accepts_a_well_formed_row(tmp_path: Path) -> None:
    assets = vm.parse_vendor_table(_write_table(tmp_path, _GOOD_ROW))
    assert assets[0].npm == "pkg" and assets[0].version == "1.2.3" and assets[0].sha256 == _HEX


def test_parse_rejects_wrong_header(tmp_path: Path) -> None:
    bad_header = "| File | Version | License | Upstream | SHA-256 (full) |"
    bad_sep = "| --- | --- | --- | --- | --- |"
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(_write_table(tmp_path, _GOOD_ROW, header=bad_header, sep=bad_sep))


def test_parse_rejects_missing_separator(tmp_path: Path) -> None:
    p = tmp_path / "T.md"
    p.write_text("\n".join([_HEADER, _GOOD_ROW, ""]), encoding="utf-8")
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(p)


def test_parse_rejects_duplicate_file(tmp_path: Path) -> None:
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(_write_table(tmp_path, _GOOD_ROW, _GOOD_ROW))


def test_parse_rejects_bad_sha(tmp_path: Path) -> None:
    bad = "| `a.js` | `pkg` | 1.2.3 | MIT | https://x/npm/pkg@1.2.3/a.js | NOT-HEX |"
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(_write_table(tmp_path, bad))


def test_parse_rejects_path_traversal(tmp_path: Path) -> None:
    bad = f"| `../secrets.js` | `pkg` | 1.2.3 | MIT | https://x/npm/pkg@1.2.3/p | `{_HEX}` |"
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(_write_table(tmp_path, bad))


def test_parse_rejects_version_url_mismatch(tmp_path: Path) -> None:
    bad = f"| `a.js` | `pkg` | 9.9.9 | MIT | https://x/npm/pkg@1.2.3/a.js | `{_HEX}` |"
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(_write_table(tmp_path, bad))


def test_parse_rejects_empty_npm(tmp_path: Path) -> None:
    bad = f"| `a.js` |  | 1.2.3 | MIT | https://x/npm/x@1.2.3/a.js | `{_HEX}` |"
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(_write_table(tmp_path, bad))


def test_parse_rejects_wrong_column_count(tmp_path: Path) -> None:
    bad = f"| `a.js` | `pkg` | 1.2.3 | MIT | `{_HEX}` |"
    with pytest.raises(vm.VendorManifestError):
        vm.parse_vendor_table(_write_table(tmp_path, bad))


# --- coverage_errors cases (synthetic sets) -------------------------------


def test_coverage_flags_unpinned_shipped_asset() -> None:
    errs = vm.coverage_errors(pinned={"a.js"}, shipped={"a.js", "b.js"})
    assert errs and any("b.js" in e for e in errs)


def test_coverage_flags_orphan_table_row() -> None:
    errs = vm.coverage_errors(pinned={"a.js", "gone.js"}, shipped={"a.js"})
    assert any("gone.js" in e for e in errs)


def test_coverage_excludes_first_party_file() -> None:
    errs = vm.coverage_errors(pinned={"a.js"}, shipped={"a.js", "swagger/swagger-init.js"})
    assert errs == []


def test_coverage_flags_first_party_leaked_into_table() -> None:
    fp = "swagger/swagger-init.js"
    errs = vm.coverage_errors(pinned={"a.js", fp}, shipped={"a.js", fp})
    assert any("first-party" in e for e in errs)

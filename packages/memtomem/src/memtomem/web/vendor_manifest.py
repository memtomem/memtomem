"""Strict parser + coverage helpers for the vendored-asset pin table.

``web/static/vendor/THIRD_PARTY_LICENSES.md`` is the single source of truth for
the third-party browser libraries shipped with ``mm web``. Two consumers read
it through this module so the table can never drift between "what we hash" and
"what we advisory-scan":

* ``tests/web/test_vendor_asset_pins.py`` — offline guard that re-hashes every
  pinned file against its SHA-256 and checks table<->disk coverage both ways.
* ``tools/check_vendored_advisories.py`` — CI gate that queries OSV for every
  ``npm package`` / ``Version`` pair and fails on any known advisory.

The parser is **fail-closed**: a malformed header, a duplicate or path-traversing
file cell, a non-hex SHA-256, an empty npm/version cell, a wrong column count, or
a ``Version`` that does not appear in the ``Upstream`` URL raises
``VendorManifestError`` instead of silently skipping the row. A skipped row would
either leave a shipped file unpinned or advisory-scan a stale version — both
false-passes the guards exist to prevent.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent / "static" / "vendor"
THIRD_PARTY_LICENSES = VENDOR_DIR / "THIRD_PARTY_LICENSES.md"

# Files under vendor/ that are first-party bootstrap code (ours), not third-party
# npm assets — intentionally absent from the table and the advisory scan. The
# coverage check excludes these from "must be pinned" AND fails if one is wrongly
# added to the table as a third-party row.
FIRST_PARTY_VENDOR_FILES: frozenset[str] = frozenset({"swagger/swagger-init.js"})

# Only minified JS/CSS are pinned; LICENSE / README / *.txt attribution files are
# shipped alongside but carry no SHA/advisory row.
PINNED_SUFFIXES: tuple[str, ...] = (".js", ".css")

_EXPECTED_HEADER: tuple[str, ...] = (
    "File",
    "npm package",
    "Version",
    "License",
    "Upstream",
    "SHA-256 (full)",
)
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class VendorManifestError(ValueError):
    """The pin table is malformed — fail closed instead of scanning stale data."""


@dataclass(frozen=True)
class VendorAsset:
    """One pinned third-party file: a row of the THIRD_PARTY_LICENSES table."""

    file: str  # path relative to VENDOR_DIR, POSIX, e.g. "purify.min.js"
    npm: str  # canonical npm package name, e.g. "dompurify", "prismjs"
    version: str  # pinned version, e.g. "3.4.10"
    license: str
    upstream: str  # source CDN URL
    sha256: str  # 64-hex, lowercase

    def path(self, vendor_dir: Path = VENDOR_DIR) -> Path:
        return vendor_dir / self.file

    def disk_sha256(self, vendor_dir: Path = VENDOR_DIR) -> str:
        # read_bytes, never read_text: these are minified binaries and Windows
        # text-mode CRLF translation would corrupt the hash.
        return hashlib.sha256(self.path(vendor_dir).read_bytes()).hexdigest()


def _row_cells(line: str) -> list[str]:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        raise VendorManifestError(f"not a markdown table row: {line!r}")
    return [c.strip() for c in s[1:-1].split("|")]


def _unwrap(cell: str) -> str:
    return cell.strip().strip("`").strip()


def _is_separator(cells: list[str]) -> bool:
    return len(cells) == len(_EXPECTED_HEADER) and all(
        c and set(c) <= {"-", ":"} and "-" in c for c in cells
    )


def parse_vendor_table(md_path: Path = THIRD_PARTY_LICENSES) -> list[VendorAsset]:
    """Parse the pin table, validating each row; raise on any malformed state."""
    lines = md_path.read_text(encoding="utf-8").splitlines()

    header_idx: int | None = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and tuple(_row_cells(line)) == _EXPECTED_HEADER:
            header_idx = i
            break
    if header_idx is None:
        raise VendorManifestError(
            f"no pin table with header {list(_EXPECTED_HEADER)} found in {md_path}"
        )

    if header_idx + 1 >= len(lines) or not _is_separator(_row_cells(lines[header_idx + 1])):
        sep = lines[header_idx + 1] if header_idx + 1 < len(lines) else "<eof>"
        raise VendorManifestError(f"malformed or missing table separator row: {sep!r}")

    assets: list[VendorAsset] = []
    seen: set[str] = set()
    for line in lines[header_idx + 2 :]:
        if not line.strip().startswith("|"):
            break  # table ended
        cells = _row_cells(line)
        if len(cells) != len(_EXPECTED_HEADER):
            raise VendorManifestError(
                f"row has {len(cells)} cells, expected {len(_EXPECTED_HEADER)}: {line!r}"
            )
        file, npm, version, license_, upstream, sha = (_unwrap(c) for c in cells)
        if not (file and npm and version):
            raise VendorManifestError(f"empty File/npm/Version cell: {line!r}")
        if file.startswith("/") or Path(file).is_absolute() or ".." in Path(file).parts:
            raise VendorManifestError(f"unsafe file path (must stay under vendor/): {file!r}")
        if file in seen:
            raise VendorManifestError(f"duplicate file row: {file!r}")
        seen.add(file)
        if not _SHA256_RE.match(sha):
            raise VendorManifestError(f"SHA-256 is not 64 lowercase hex for {file!r}: {sha!r}")
        if version not in upstream:
            raise VendorManifestError(
                f"Version {version!r} does not appear in the Upstream URL for {file!r} "
                f"(stale URL?): {upstream!r}"
            )
        assets.append(VendorAsset(file, npm, version, license_, upstream, sha))

    if not assets:
        raise VendorManifestError(f"pin table has a header but no data rows in {md_path}")
    return assets


def iter_shipped_vendor_files(vendor_dir: Path = VENDOR_DIR) -> list[str]:
    """Every shipped ``.js`` / ``.css`` under vendor/, as POSIX paths."""
    return sorted(
        p.relative_to(vendor_dir).as_posix()
        for p in vendor_dir.rglob("*")
        if p.is_file() and p.suffix in PINNED_SUFFIXES
    )


def coverage_errors(
    pinned: set[str],
    shipped: set[str],
    first_party: frozenset[str] = FIRST_PARTY_VENDOR_FILES,
) -> list[str]:
    """Bidirectional table<->disk coverage, excluding first-party bootstrap files."""
    errors: list[str] = []
    unpinned = sorted((shipped - first_party) - pinned)
    if unpinned:
        errors.append(f"shipped vendor assets missing from THIRD_PARTY_LICENSES.md: {unpinned}")
    orphan = sorted(pinned - shipped)
    if orphan:
        errors.append(f"pin-table rows with no on-disk file: {orphan}")
    leaked = sorted(first_party & pinned)
    if leaked:
        errors.append(f"first-party files wrongly pinned as third-party: {leaked}")
    return errors


def npm_version_pairs(assets: Iterable[VendorAsset]) -> list[tuple[str, str]]:
    """Deduplicated, sorted ``(npm, version)`` pairs for advisory queries."""
    return sorted({(a.npm, a.version) for a in assets})

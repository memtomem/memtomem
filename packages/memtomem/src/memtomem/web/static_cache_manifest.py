"""Content-aware cache-version contract for the Web UI assets.

The browser-facing ``?v=N`` values are deliberately human-readable.  This
module binds each value to the SHA-256 of the exact bytes it names, so an asset
edit cannot silently reuse a warm-cache URL.  The checked-in manifest keeps a
small append-only history per path; validation needs no Git base and therefore
runs the same way in a source archive, local checkout, and CI.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit


WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
MANIFEST_PATH = STATIC_DIR / "cache-versions.json"
INDEX_PATH = STATIC_DIR / "index.html"

SCHEMA_VERSION = 1
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[1-9][0-9]*")


class StaticCacheManifestError(ValueError):
    """The cache manifest or its relationship to the shipped assets is invalid."""


@dataclass(frozen=True, slots=True)
class AssetReference:
    """One cacheable JS/CSS URL found in a browser HTML surface."""

    path: str
    version: str
    surface: str


@dataclass(frozen=True, slots=True)
class StaticCacheManifest:
    """Strictly parsed ``cache-versions.json`` contents."""

    assets: dict[str, dict[str, str]]
    excluded: dict[str, str]


class _AssetReferenceParser(HTMLParser):
    def __init__(self, surface: str) -> None:
        super().__init__(convert_charrefs=True)
        self.surface = surface
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.urls.append(values["src"] or "")
            return
        if tag != "link" or not values.get("href"):
            return
        rel = {part.lower() for part in (values.get("rel") or "").split()}
        if "stylesheet" in rel:
            self.urls.append(values["href"] or "")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StaticCacheManifestError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _validate_url_path(path: object, *, suffixes: tuple[str, ...] | None = None) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise StaticCacheManifestError(f"asset path must be root-relative: {path!r}")
    if "\\" in path:
        raise StaticCacheManifestError(f"unsafe asset path: {path!r}")
    pure = PurePosixPath(path)
    if not pure.name or any(part in {"", ".", ".."} for part in pure.parts[1:]):
        raise StaticCacheManifestError(f"unsafe asset path: {path!r}")
    canonical = f"/{PurePosixPath(path.removeprefix('/')).as_posix()}"
    if canonical != path:
        raise StaticCacheManifestError(f"asset path must be canonical: {path!r}")
    if suffixes is not None and pure.suffix.lower() not in suffixes:
        raise StaticCacheManifestError(f"cacheable asset must be JS or CSS: {path!r}")
    return path


def _validate_version(version: object) -> str:
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        raise StaticCacheManifestError(
            f"cache version must be a positive decimal integer: {version!r}"
        )
    return version


def _validate_digest(digest: object) -> str:
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise StaticCacheManifestError(f"SHA-256 must be 64 lowercase hex characters: {digest!r}")
    return digest


def load_manifest(path: Path = MANIFEST_PATH) -> StaticCacheManifest:
    """Load and strictly validate the cache-version manifest."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise StaticCacheManifestError(f"cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise StaticCacheManifestError("cache manifest root must be an object")
    expected_keys = {"schema_version", "assets", "excluded"}
    if set(raw) != expected_keys:
        raise StaticCacheManifestError(
            f"cache manifest keys must be {sorted(expected_keys)}; got {sorted(raw)}"
        )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise StaticCacheManifestError(
            f"unsupported cache manifest schema: {raw['schema_version']!r}"
        )

    raw_assets = raw["assets"]
    if not isinstance(raw_assets, dict):
        raise StaticCacheManifestError("cache manifest assets must be an object")
    assets: dict[str, dict[str, str]] = {}
    for raw_path, entry in raw_assets.items():
        asset_path = _validate_url_path(raw_path, suffixes=(".js", ".css"))
        if not isinstance(entry, dict) or set(entry) != {"versions"}:
            raise StaticCacheManifestError(
                f"{asset_path}: asset entry must contain only a versions object"
            )
        raw_versions = entry["versions"]
        if not isinstance(raw_versions, dict) or not raw_versions:
            raise StaticCacheManifestError(f"{asset_path}: versions must be a non-empty object")
        versions: dict[str, str] = {}
        for raw_version, raw_digest in raw_versions.items():
            version = _validate_version(raw_version)
            versions[version] = _validate_digest(raw_digest)
        assets[asset_path] = versions

    raw_excluded = raw["excluded"]
    if not isinstance(raw_excluded, dict):
        raise StaticCacheManifestError("cache manifest excluded must be an object")
    excluded: dict[str, str] = {}
    for raw_path, reason in raw_excluded.items():
        excluded_path = _validate_url_path(raw_path)
        if not isinstance(reason, str) or not reason.strip():
            raise StaticCacheManifestError(f"{excluded_path}: exclusion reason must be non-empty")
        excluded[excluded_path] = reason

    return StaticCacheManifest(assets=assets, excluded=excluded)


def render_manifest(manifest: StaticCacheManifest) -> str:
    """Render deterministic JSON, ordering paths and numeric versions."""

    assets: dict[str, dict[str, dict[str, str]]] = {}
    for path in sorted(manifest.assets):
        versions = manifest.assets[path]
        assets[path] = {
            "versions": {
                version: versions[version]
                for version in sorted(versions, key=lambda value: int(value))
            }
        }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "assets": assets,
        "excluded": dict(sorted(manifest.excluded.items())),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def references_from_html(html: str, *, surface: str) -> list[AssetReference]:
    """Extract and validate root-relative, versioned JS/CSS references."""

    parser = _AssetReferenceParser(surface)
    parser.feed(html)
    references: list[AssetReference] = []
    for url in parser.urls:
        split = urlsplit(url)
        suffix = PurePosixPath(split.path).suffix.lower()
        if suffix not in {".js", ".css"}:
            continue
        if split.scheme or split.netloc:
            raise StaticCacheManifestError(f"{surface}: external cacheable asset URL: {url!r}")
        path = _validate_url_path(split.path, suffixes=(".js", ".css"))
        if split.fragment:
            raise StaticCacheManifestError(f"{surface}: asset URL must not use a fragment: {url!r}")
        query = parse_qsl(split.query, keep_blank_values=True)
        versions = [value for key, value in query if key == "v"]
        if len(versions) != 1 or len(query) != 1:
            raise StaticCacheManifestError(
                f"{surface}: asset URL must contain exactly one ?v=N query: {url!r}"
            )
        references.append(
            AssetReference(path=path, version=_validate_version(versions[0]), surface=surface)
        )
    return references


async def collect_runtime_references(*, index_path: Path = INDEX_PATH) -> list[AssetReference]:
    """Read the SPA and the actual generated Swagger HTML reference sets."""

    index_html = index_path.read_text(encoding="utf-8")
    references = references_from_html(index_html, surface="index.html")

    # Lazy import avoids making the runtime app depend on this developer guard.
    from memtomem.web.app import create_app

    app = create_app(lifespan=None)
    docs_route = next(
        (route for route in app.routes if getattr(route, "path", None) == "/api/docs"), None
    )
    if docs_route is None or not hasattr(docs_route, "endpoint"):
        raise StaticCacheManifestError("could not locate the generated /api/docs route")
    response = await docs_route.endpoint()
    docs_html = bytes(response.body).decode("utf-8")
    references.extend(references_from_html(docs_html, surface="/api/docs"))
    return references


def discover_static_assets(static_dir: Path = STATIC_DIR) -> set[str]:
    """Return every shipped production JS/CSS path below the static root."""

    return {
        f"/{path.relative_to(static_dir).as_posix()}"
        for pattern in ("*.js", "*.css")
        for path in static_dir.rglob(pattern)
        if path.is_file()
    }


def _reference_versions(references: list[AssetReference]) -> tuple[dict[str, str], list[str]]:
    versions: dict[str, str] = {}
    errors: list[str] = []
    for reference in references:
        previous = versions.get(reference.path)
        if previous is not None and previous != reference.version:
            errors.append(
                f"{reference.path}: conflicting public versions {previous} and "
                f"{reference.version} ({reference.surface})"
            )
        else:
            versions[reference.path] = reference.version
    return versions, errors


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contract_errors(
    manifest: StaticCacheManifest,
    references: list[AssetReference],
    *,
    static_dir: Path = STATIC_DIR,
) -> list[str]:
    """Return every content/reference/manifest contract violation."""

    ref_versions, errors = _reference_versions(references)
    shipped = discover_static_assets(static_dir)
    referenced = set(ref_versions)
    recorded = set(manifest.assets)

    if shipped != referenced:
        missing_refs = sorted(shipped - referenced)
        missing_files = sorted(referenced - shipped)
        if missing_refs:
            errors.append(f"shipped JS/CSS missing a versioned HTML reference: {missing_refs}")
        if missing_files:
            errors.append(f"versioned HTML reference missing on disk: {missing_files}")
    if referenced != recorded:
        missing_manifest = sorted(referenced - recorded)
        orphan_manifest = sorted(recorded - referenced)
        if missing_manifest:
            errors.append(f"versioned assets missing from cache manifest: {missing_manifest}")
        if orphan_manifest:
            errors.append(f"cache manifest entries no longer referenced: {orphan_manifest}")

    for asset_path in sorted(referenced & recorded & shipped):
        current_version = ref_versions[asset_path]
        history = manifest.assets[asset_path]
        newest_version = max(history, key=lambda value: int(value))
        if current_version != newest_version:
            errors.append(
                f"{asset_path}: public version {current_version} is not the newest recorded "
                f"version {newest_version}"
            )
            continue
        expected = history.get(current_version)
        if expected is None:
            errors.append(f"{asset_path}: public version {current_version} has no recorded SHA-256")
            continue
        actual = file_sha256(static_dir / asset_path.removeprefix("/"))
        if actual != expected:
            errors.append(
                f"{asset_path}?v={current_version}: bytes changed without a cache-version bump "
                f"(recorded {expected}, actual {actual})"
            )
    return errors


def updated_manifest(
    manifest: StaticCacheManifest,
    references: list[AssetReference],
    *,
    static_dir: Path = STATIC_DIR,
) -> StaticCacheManifest:
    """Append bindings for new assets or already-bumped public versions.

    Existing ``(path, version)`` bindings are immutable.  An edited asset must
    first receive the next numeric version in its HTML carrier; otherwise this
    function fails instead of blessing new bytes under a warm-cache URL.
    """

    ref_versions, errors = _reference_versions(references)
    shipped = discover_static_assets(static_dir)
    referenced = set(ref_versions)
    recorded = set(manifest.assets)
    if shipped != referenced:
        errors.extend(
            [
                f"shipped/reference asset sets differ: shipped-only={sorted(shipped - referenced)}, "
                f"reference-only={sorted(referenced - shipped)}"
            ]
        )
    orphan_manifest = sorted(recorded - referenced)
    if orphan_manifest:
        errors.append(
            f"remove retired cache-manifest entries explicitly before updating: {orphan_manifest}"
        )
    if errors:
        raise StaticCacheManifestError("; ".join(errors))

    assets = {path: dict(versions) for path, versions in manifest.assets.items()}
    for asset_path in sorted(referenced):
        version = ref_versions[asset_path]
        digest = file_sha256(static_dir / asset_path.removeprefix("/"))
        history = assets.get(asset_path)
        if history is None:
            if version != "1":
                raise StaticCacheManifestError(
                    f"{asset_path}: a new asset must start at public version 1, got {version}"
                )
            assets[asset_path] = {version: digest}
            continue
        if version in history:
            if history[version] != digest:
                raise StaticCacheManifestError(
                    f"{asset_path}?v={version}: refusing to overwrite the recorded digest; "
                    "increment the public ?v=N value first"
                )
            continue
        newest = max(int(value) for value in history)
        if int(version) != newest + 1:
            raise StaticCacheManifestError(
                f"{asset_path}: new public version must be {newest + 1}, got {version}"
            )
        history[version] = digest

    return StaticCacheManifest(assets=assets, excluded=dict(manifest.excluded))


def canonical_manifest_error(
    manifest: StaticCacheManifest, *, path: Path = MANIFEST_PATH
) -> str | None:
    """Return an error when the checked-in JSON is not deterministically rendered."""

    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"cannot read {path}: {exc}"
    if current != render_manifest(manifest):
        return f"{path} is not canonical; run the cache manifest updater with --write"
    return None

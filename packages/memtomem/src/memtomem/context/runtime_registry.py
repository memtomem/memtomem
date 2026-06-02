"""Read-only detection of memtomem / mms registration across provider clients.

ADR-0021 §"Runtime registration detection — trust boundary". This module
*reads* a provider client's MCP-server config to answer two questions for the
Context Portal runtime-status surface:

* **installed** — does the client have a config dir/file on disk?
* **registered** — is the ``memtomem`` (LTM) and/or ``mms`` (STM proxy) server
  registered in that config?

Constraints (enforced by design, not convention):

* **Read-only.** This module never writes a client config; registration stays
  owned by :mod:`memtomem.cli.init_cmd` / :mod:`memtomem.cli.uninstall_cmd`.
* **No raw config egress.** Callers receive only booleans, the *kinds* of
  locations where a registration was found, and ``$HOME``-collapsed config
  paths — never raw config bytes or values, and never an exception *message*
  (only a coarse :data:`error_kind`). Provider configs hold API tokens; this is
  the trust boundary, so nothing read here is echoed back.
* **No STM coupling.** "mms registered" is decided purely by inspecting the
  config's MCP-server map for the server-id keys below — never by importing
  ``memtomem_stm`` (forbidden cross-repo coupling, CLAUDE.md invariant).

The in-scope provider clients and their config locations mirror
``docs/guides/mcp-clients.md`` (the source of truth). ``test_runtime_registry``
pins "registry locations ⊇ documented locations" so a newly-documented location
for an in-scope client cannot silently drop out. Per ADR-0021 §Decision B the
gemini-family client is **Antigravity** (CLI + IDE on the ``~/.gemini`` paths);
the standalone Gemini CLI and generic MCP editors (Cursor, Windsurf, Claude
Desktop) are out of scope for v1.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    import tomllib
except ImportError:  # pragma: no cover — py<3.11 fallback; repo targets py312
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Server-id keys that count as a registration. ``memtomem`` is the LTM server;
# ``mms`` is the STM proxy (MMS_CLIENT_SERVER_NAME). Detected by key presence
# only — never by importing the STM package.
MEMTOMEM_SERVER_IDS: frozenset[str] = frozenset({"memtomem"})
MMS_SERVER_IDS: frozenset[str] = frozenset({"mms"})

# Provider clients in scope for the v1 Context Portal (ADR-0021 §Decision B).
# Antigravity is the gemini-family client; the standalone Gemini CLI and generic
# MCP editors are intentionally excluded.
IN_SCOPE_CLIENTS: tuple[str, ...] = ("claude", "antigravity", "codex", "kimi")

# Map artifact fan-out runtime ids (KNOWN_RUNTIMES) to the provider client whose
# registration represents them on the status surface. Antigravity is the
# gemini-family client (ADR-0021 §B), so the `gemini` runtime maps to it.
RUNTIME_TO_CLIENT: dict[str, str] = {
    "claude": "claude",
    "gemini": "antigravity",
    "codex": "codex",
    "kimi": "kimi",
}

ConfigFormat = Literal["json", "toml"]

# Coarse error categories (no exception message — see module docstring).
_ERROR_PRECEDENCE: tuple[str, ...] = ("permission", "parse", "internal")


# --- MCP-server-map extractors ------------------------------------------------
# Each returns the ``{server_id: ...}`` mapping from parsed config, or None.


def _mcp_servers(data: object, _root: Path | None) -> dict | None:
    return data.get("mcpServers") if isinstance(data, dict) else None


def _servers(data: object, _root: Path | None) -> dict | None:
    # Antigravity VS Code-side config uses the key ``servers`` (mcp-clients.md §8).
    return data.get("servers") if isinstance(data, dict) else None


def _toml_mcp_servers(data: object, _root: Path | None) -> dict | None:
    # Codex ``[mcp_servers.<id>]`` parses to {"mcp_servers": {"<id>": {...}}}.
    return data.get("mcp_servers") if isinstance(data, dict) else None


def _claude_local(data: object, root: Path | None) -> dict | None:
    # Claude local scope: ~/.claude.json -> projects."<cwd>".mcpServers.
    if not isinstance(data, dict) or root is None:
        return None
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return None
    entry = projects.get(str(root))
    return entry.get("mcpServers") if isinstance(entry, dict) else None


# --- path resolvers -----------------------------------------------------------


def _kimi_dir(home: Path) -> Path:
    share = os.environ.get("KIMI_SHARE_DIR")
    return Path(share).expanduser() if share else home / ".kimi"


@dataclass(frozen=True)
class _Location:
    """One place an in-scope client may hold its MCP-server map.

    ``resolve`` maps ``(home, project_root)`` to the config file (or None when
    inapplicable, e.g. the Claude ``project`` scope without a project root).
    ``container`` extracts the server map from parsed config.
    """

    kind: str  # user | local | project | cli | ide | ide_vscode
    fmt: ConfigFormat
    resolve: Callable[[Path, Path | None], Path | None]
    container: Callable[[object, Path | None], dict | None]


# Location table — mirrors docs/guides/mcp-clients.md (source of truth); the
# conformance test pins coverage. Antigravity == gemini-family (ADR-0021 §B).
_LOCATIONS: dict[str, tuple[_Location, ...]] = {
    "claude": (
        _Location("user", "json", lambda h, r: h / ".claude.json", _mcp_servers),
        _Location("local", "json", lambda h, r: h / ".claude.json", _claude_local),
        _Location("project", "json", lambda h, r: (r / ".mcp.json") if r else None, _mcp_servers),
    ),
    "antigravity": (
        _Location(
            "cli",
            "json",
            lambda h, r: h / ".gemini" / "antigravity-cli" / "mcp_config.json",
            _mcp_servers,
        ),
        _Location(
            "ide",
            "json",
            lambda h, r: h / ".gemini" / "antigravity" / "mcp_config.json",
            _mcp_servers,
        ),
        _Location(
            "ide_vscode",
            "json",
            lambda h, r: (
                h / "Library" / "Application Support" / "Antigravity" / "User" / "mcp.json"
            ),
            _servers,
        ),
    ),
    "codex": (
        _Location("user", "toml", lambda h, r: h / ".codex" / "config.toml", _toml_mcp_servers),
    ),
    "kimi": (_Location("user", "json", lambda h, r: _kimi_dir(h) / "mcp.json", _mcp_servers),),
}

# Existence of any marker => the client is considered installed.
_INSTALLED_MARKERS: dict[str, Callable[[Path], tuple[Path, ...]]] = {
    "claude": lambda h: (h / ".claude.json", h / ".claude"),
    "antigravity": lambda h: (
        h / ".gemini" / "antigravity-cli",
        h / ".gemini" / "antigravity",
        h / "Library" / "Application Support" / "Antigravity",
    ),
    "codex": lambda h: (h / ".codex" / "config.toml", h / ".codex"),
    "kimi": lambda h: (_kimi_dir(h),),
}


@dataclass(frozen=True)
class RuntimeStatus:
    """Read-only registration status for one provider client.

    No field carries config contents: ``config_paths`` are ``$HOME``-collapsed
    paths of the locations where a registration was found (parallel to
    ``registered_locations``), and ``error_kind`` is a coarse category only.
    """

    name: str
    installed: bool
    memtomem_registered: bool
    mms_registered: bool
    registered_locations: tuple[str, ...]
    config_paths: tuple[str, ...]
    error_kind: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "installed": self.installed,
            "memtomem_registered": self.memtomem_registered,
            "mms_registered": self.mms_registered,
            "registered_locations": list(self.registered_locations),
            "config_paths": list(self.config_paths),
            "error_kind": self.error_kind,
        }


def _collapse_home(path: Path, home: Path) -> str:
    """Return ``path`` with the ``home`` prefix collapsed to ``~`` (POSIX form)."""
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return path.as_posix()


def _probe_location(
    loc: _Location, home: Path, root: Path | None
) -> tuple[Path | None, bool, bool, str | None]:
    """Read one location; return only ``(path, found_memtomem, found_mms, error_kind)``.

    The parsed config object **never escapes this function** — only the boolean
    facts and a coarse error category cross the return. This is the trust
    boundary (ADR-0021 §B): no raw config bytes/values and no exception
    *message* leave the module via any return value. ``path`` is returned for
    ``$HOME`` collapse by the caller and carries no secret (it is a fixed
    config path). A missing file is not an error (``error_kind=None``).
    """
    path = loc.resolve(home, root)
    if path is None:
        return None, False, False, None
    try:
        if loc.fmt == "toml":
            if tomllib is None:  # pragma: no cover — repo targets py312
                return path, False, False, "internal"
            with path.open("rb") as fh:
                data: object = tomllib.load(fh)
        else:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
    except FileNotFoundError:
        return path, False, False, None
    except PermissionError:
        return path, False, False, "permission"
    except (json.JSONDecodeError, UnicodeDecodeError):
        return path, False, False, "parse"
    except Exception as exc:  # noqa: BLE001 — coarse classify; no message leaks out
        if tomllib is not None and isinstance(exc, tomllib.TOMLDecodeError):
            return path, False, False, "parse"
        logger.debug("runtime_registry: unexpected read error for %s", path.name)
        return path, False, False, "internal"
    servers = loc.container(data, root)
    if not isinstance(servers, dict):
        return path, False, False, None
    found_mt = any(k in servers for k in MEMTOMEM_SERVER_IDS)
    found_mms = any(k in servers for k in MMS_SERVER_IDS)
    return path, found_mt, found_mms, None


def _pick_error(kinds: list[str]) -> str | None:
    for kind in _ERROR_PRECEDENCE:
        if kind in kinds:
            return kind
    return None


def _is_installed(client: str, home: Path) -> bool:
    markers = _INSTALLED_MARKERS.get(client)
    if markers is None:
        return False
    return any(p.exists() for p in markers(home))


def probe_runtime(
    client: str, project_root: Path | None = None, *, home: Path | None = None
) -> RuntimeStatus:
    """Probe one in-scope provider client. Read-only; never raises on bad config.

    ``home`` is injectable for test isolation and to sidestep the Windows
    ``expanduser`` / ``$HOME`` no-op (cf. feedback on USERPROFILE); it defaults
    to :func:`Path.home`.
    """
    home = home or Path.home()
    error_kinds: list[str] = []
    memtomem_reg = False
    mms_reg = False
    reg_locations: list[str] = []
    reg_paths: list[str] = []

    for loc in _LOCATIONS.get(client, ()):
        path, found_mt, found_mms, err = _probe_location(loc, home, project_root)
        if err is not None:
            error_kinds.append(err)
            continue
        if (found_mt or found_mms) and path is not None:
            memtomem_reg = memtomem_reg or found_mt
            mms_reg = mms_reg or found_mms
            reg_locations.append(loc.kind)
            reg_paths.append(_collapse_home(path, home))

    return RuntimeStatus(
        name=client,
        installed=_is_installed(client, home),
        memtomem_registered=memtomem_reg,
        mms_registered=mms_reg,
        registered_locations=tuple(reg_locations),
        config_paths=tuple(reg_paths),
        error_kind=_pick_error(error_kinds),
    )


def probe_all_runtimes(
    project_root: Path | None = None, *, home: Path | None = None
) -> list[RuntimeStatus]:
    """Probe every in-scope provider client, in :data:`IN_SCOPE_CLIENTS` order."""
    return [probe_runtime(c, project_root, home=home) for c in IN_SCOPE_CLIENTS]


def registry_location_paths(home: Path, project_root: Path | None = None) -> dict[str, list[str]]:
    """Resolved (``$HOME``-collapsed) config paths the registry probes per client.

    Exposed for the ``docs/guides/mcp-clients.md`` conformance test, which
    asserts the registry covers every documented location for the in-scope
    clients.
    """
    out: dict[str, list[str]] = {}
    for client, locs in _LOCATIONS.items():
        paths: list[str] = []
        for loc in locs:
            resolved = loc.resolve(home, project_root)
            if resolved is not None:
                paths.append(_collapse_home(resolved, home))
        out[client] = paths
    return out

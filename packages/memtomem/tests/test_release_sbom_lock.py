"""Release SBOM edges are checked against the uv.lock they were exported from (#3066).

uv's CycloneDX export writes ``A -> T`` when A declares T only under an extra
nothing activates, as long as T is installed for another reason. The fixtures
under ``fixtures/release_sbom`` are the three uv 0.11.16 exports at tag
``langchain-v0.1.0`` and that tag's lock, trimmed to the packages they name. The
expected dropped edges below were measured by hand against the lock, not taken
from the code under test.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "release_sbom"
_LOCK = _FIXTURES / "langchain-v0.1.0.uv.lock.toml"


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "release_sbom.py"
    spec = importlib.util.spec_from_file_location("release_sbom_lock", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load_tool()

_UVICORN_STANDARD = {("uvicorn", "python-dotenv"), ("uvicorn", "pyyaml")}
_MEASURED = {
    "core": {("memtomem", "uvicorn")} | _UVICORN_STANDARD,
    "all": set(),
    "langchain": {
        ("memtomem", "langgraph"),
        ("memtomem", "urllib3"),
        ("memtomem", "uvicorn"),
        ("uvicorn", "websockets"),
    }
    | _UVICORN_STANDARD,
}


def _edges(payload: dict[str, Any]) -> set[tuple[str, str]]:
    names = {row["bom-ref"]: row["name"] for row in payload["components"]}
    root = payload["metadata"]["component"]
    names[root["bom-ref"]] = root["name"]
    return {
        (names[row["ref"]], names[ref])
        for row in payload["dependencies"]
        for ref in row.get("dependsOn", [])
    }


@pytest.mark.parametrize("graph", ["core", "all", "langchain"])
def test_exactly_the_measured_false_edges_are_dropped(graph: str) -> None:
    payload = json.loads((_FIXTURES / f"{graph}.raw.json").read_text(encoding="utf-8"))
    before = _edges(payload)
    assert _MEASURED[graph] <= before, "the fixture no longer carries the measured edges"

    dropped = rs.reconcile_edges(payload, rs.load_lock(_LOCK), all_root_extras=graph == "all")

    assert set(dropped) == _MEASURED[graph]
    assert len(dropped) == len(_MEASURED[graph])
    assert _edges(payload) == before - _MEASURED[graph]


def test_the_release_pair_from_the_tag_validates(tmp_path: Path, capsys) -> None:
    core_out, all_out = tmp_path / "core.json", tmp_path / "all.json"
    assert (
        rs.main(
            [
                "--core-raw",
                str(_FIXTURES / "core.raw.json"),
                "--all-raw",
                str(_FIXTURES / "all.raw.json"),
                "--core-out",
                str(core_out),
                "--all-out",
                str(all_out),
                "--version",
                "0.7.4",
                "--lock",
                str(_LOCK),
            ]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert "core: dropped an edge only an inactive extra declares: memtomem -> uvicorn" in err
    assert "all:" not in err
    assert ("memtomem", "uvicorn") not in _edges(json.loads(core_out.read_text(encoding="utf-8")))
    assert ("memtomem", "uvicorn") in _edges(json.loads(all_out.read_text(encoding="utf-8")))


def test_the_langchain_sbom_from_the_tag_validates(tmp_path: Path) -> None:
    out = tmp_path / "langchain.json"
    rs.normalize_validate_langchain(
        raw=_FIXTURES / "langchain.raw.json",
        out=out,
        version="0.1.0",
        core_version="0.7.4",
        lock=_LOCK,
    )
    edges = _edges(json.loads(out.read_text(encoding="utf-8")))
    assert ("langchain-memtomem", "langgraph") in edges
    assert ("memtomem", "langgraph") not in edges


# --- synthetic graphs -------------------------------------------------------------


def _sbom(root: str, edges: dict[str, list[str]], *, names: dict[str, str] | None = None) -> dict:
    """A CycloneDX payload; *names* maps a package to the spelling the SBOM uses."""
    spell = names or {}
    packages = {root} | set(edges) | {t for targets in edges.values() for t in targets}
    ref = {p: f"{p}-ref@1" for p in packages}
    return {
        "metadata": {
            "component": {"bom-ref": ref[root], "name": spell.get(root, root), "version": "1"}
        },
        "components": [
            {"bom-ref": ref[p], "name": spell.get(p, p), "version": "1"}
            for p in sorted(packages - {root})
        ],
        "dependencies": [
            {"ref": ref[p], "dependsOn": [ref[t] for t in edges.get(p, [])]}
            for p in sorted(packages)
        ],
    }


def _pkg(name: str, deps: list[Any] = (), **extras: list[Any]) -> dict[str, Any]:
    def entry(d: Any) -> dict[str, Any]:
        return {"name": d} if isinstance(d, str) else {"name": d[0], "extra": list(d[1])}

    package: dict[str, Any] = {"name": name, "version": "1"}
    if deps:
        package["dependencies"] = [entry(d) for d in deps]
    if extras:
        package["optional-dependencies"] = {k: [entry(d) for d in v] for k, v in extras.items()}
    return package


def _lock(*packages: dict[str, Any]) -> dict[str, Any]:
    return {"package": list(packages)}


def _reconcile(sbom: dict, lock: dict, *, all_root_extras: bool = False) -> list:
    return rs.reconcile_edges(sbom, lock, all_root_extras=all_root_extras)


def test_an_inactive_extras_edge_is_dropped_and_the_true_path_stays() -> None:
    """The uv reproduction: lib declares idna only under [web]; app needs idna itself."""
    sbom = _sbom("app", {"app": ["lib", "idna"], "lib": ["idna"]})
    lock = _lock(_pkg("app", ["lib", "idna"]), _pkg("lib", web=["idna"]), _pkg("idna"))
    assert _reconcile(sbom, lock) == [("lib", "idna")]
    assert _edges(sbom) == {("app", "lib"), ("app", "idna")}


def test_an_extra_a_parent_requests_keeps_its_edges() -> None:
    sbom = _sbom("app", {"app": ["lib", "idna"], "lib": ["idna"]})
    lock = _lock(_pkg("app", [("lib", ["web"]), "idna"]), _pkg("lib", web=["idna"]), _pkg("idna"))
    assert _reconcile(sbom, lock) == []
    assert ("lib", "idna") in _edges(sbom)


def test_activation_follows_an_active_extras_own_requests() -> None:
    """app -> lib[web]; lib[web] -> mid[x]; so mid -> leaf is declared."""
    sbom = _sbom("app", {"app": ["lib"], "lib": ["mid"], "mid": ["leaf"]})
    lock = _lock(
        _pkg("app", [("lib", ["web"])]),
        _pkg("lib", web=[("mid", ["x"])]),
        _pkg("mid", x=["leaf"]),
        _pkg("leaf"),
    )
    assert _reconcile(sbom, lock) == []


def test_all_root_extras_keeps_the_roots_extra_edges() -> None:
    sbom = _sbom("app", {"app": ["idna"]})
    lock = _lock(_pkg("app", web=["idna"]), _pkg("idna"))
    assert _reconcile(copy.deepcopy(sbom), lock, all_root_extras=True) == []
    with pytest.raises(rs.SbomError, match="unreachable from the root: \\['idna'\\]"):
        _reconcile(sbom, lock)


def test_names_match_in_canonical_form() -> None:
    sbom = _sbom("app", {"app": ["zope"]}, names={"zope": "Zope_Interface"})
    lock = _lock(_pkg("app", ["zope-interface"]), _pkg("zope-interface"))
    assert _reconcile(sbom, lock) == []


@pytest.mark.parametrize(
    ("sbom", "lock", "message"),
    [
        pytest.param(
            _sbom("app", {"app": ["lib"]}),
            _lock(_pkg("app", ["lib"])),
            "lib 1 is not in uv.lock",
            id="component-missing-from-lock",
        ),
        pytest.param(
            _sbom("app", {"app": ["lib"]}),
            _lock(_pkg("app", ["lib"]), {**_pkg("lib"), "version": "2"}),
            "does not match uv.lock version 2",
            id="version-differs",
        ),
        pytest.param(
            _sbom("app", {"app": ["lib"]}),
            _lock(_pkg("app", ["lib"]), _pkg("lib"), {**_pkg("Lib"), "version": "2"}),
            "more than one package named lib",
            id="two-packages-one-name",
        ),
        pytest.param(
            _sbom("app", {"app": ["lib"]}),
            _lock(_pkg("app", ["lib"]), {**_pkg("lib"), "dev-dependencies": {"docs": []}}),
            "declares dependency groups \\['docs'\\]",
            id="non-dev-group",
        ),
        pytest.param(
            _sbom("app", {"app": ["lib", "idna"], "lib": ["idna"]}),
            _lock(_pkg("app", ["lib", "idna"]), _pkg("lib"), _pkg("idna")),
            "lib -> idna is not declared in uv.lock",
            id="edge-the-lock-never-declares",
        ),
    ],
)
def test_each_lock_mismatch_is_refused(sbom: dict, lock: dict, message: str) -> None:
    with pytest.raises(rs.SbomError, match=message):
        _reconcile(sbom, lock)


def test_a_dangling_edge_is_refused() -> None:
    sbom = _sbom("app", {"app": ["lib"]})
    sbom["dependencies"][0]["dependsOn"].append("ghost@1")
    with pytest.raises(rs.SbomError, match="names no component"):
        _reconcile(sbom, _lock(_pkg("app", ["lib"]), _pkg("lib")))


@pytest.mark.parametrize("source", ["app", "lib"])
def test_a_second_dependency_row_for_one_component_is_refused(source: str) -> None:
    sbom = _sbom("app", {"app": ["lib"], "lib": ["idna"]})
    sbom["dependencies"].append({"ref": f"{source}-ref@1", "dependsOn": []})
    lock = _lock(_pkg("app", ["lib"]), _pkg("lib", ["idna"]), _pkg("idna"))
    with pytest.raises(rs.SbomError, match=f"more than one dependency row for {source}-ref@1"):
        _reconcile(sbom, lock)


def test_a_root_row_repeated_without_its_required_edge_is_refused(tmp_path: Path) -> None:
    """The root check reads the first row; a later one must not drop langgraph unseen."""
    payload = json.loads((_FIXTURES / "langchain.raw.json").read_text(encoding="utf-8"))
    root = payload["metadata"]["component"]["bom-ref"]
    row = next(r for r in payload["dependencies"] if r["ref"] == root)
    names = {c["bom-ref"]: c["name"] for c in payload["components"]}
    without = [ref for ref in row["dependsOn"] if names[ref] != "langgraph"]
    assert len(without) == len(row["dependsOn"]) - 1
    payload["dependencies"].append({"ref": root, "dependsOn": without})
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rs.SbomError, match="more than one dependency row"):
        rs.normalize_validate_langchain(
            raw=raw, out=tmp_path / "out.json", version="0.1.0", core_version="0.7.4", lock=_LOCK
        )
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("argv_tail", [["langchain"], []])
def test_both_invocations_require_the_lock(argv_tail: list[str]) -> None:
    langchain = ["--raw", "r", "--out", "o", "--version", "1", "--core-version", "1"]
    pair = ["--core-raw", "a", "--all-raw", "b", "--core-out", "c", "--all-out", "d"]
    argv = [*argv_tail, *(langchain if argv_tail else [*pair, "--version", "1"])]
    with pytest.raises(SystemExit) as exc:
        rs.main(argv)
    assert exc.value.code == 2

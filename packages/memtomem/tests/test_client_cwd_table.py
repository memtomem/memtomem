"""Every tool parameter says which directory a path in it would start at.

A table that fails closed. ``test_client_cwd_sweep.py`` observes what the
tools do today; this file makes the next tool, and the next parameter on an
old one, fail until someone has said what it is. It checks two things.

**The table.** Every parameter of every server tool except ``ctx`` is
classified. One annotated only with ``bool``, ``int`` or ``float`` (with or
without ``None``) is classified by that annotation: it cannot carry a path.
Every other one, an untyped one and a ``dict``, ``list`` or ``Literal`` of
strings included, needs a row below. A ``client-cwd`` row also needs a direct
test in ``test_client_cwd.py``, named for the tool and the parameter.

**Explicit reads of the process working directory.** ``os.getcwd``,
``Path.cwd``, ``find_project_root()`` with no argument, and
``_resolve_project_context_from_dirs(...)`` with no anchor, anywhere in the
package outside ``cli/``, ``web/`` and ``integrations/``, are refused except
at the sites registered below, each with its reason. Those three packages own
their process and its working directory.

What this file cannot see:

- ``resolve()``, ``abspath`` and ``realpath`` of a relative path. There are
  hundreds of such calls and nearly all take an absolute path; the sweep is
  what covers them.
- A read spelled another way: an aliased import of ``getcwd``, ``os.curdir``,
  ``Path()``.
- Code under ``cli/`` that a server tool calls into.
- A table row that is wrong. A parameter classified ``not-a-path`` that a
  tool later resolves is found by the sweep, not here.
"""

from __future__ import annotations

import ast
import inspect
import types
import typing
from pathlib import Path

import memtomem
import test_client_cwd
from test_client_cwd_sweep import tool_functions, tool_parameters

_PACKAGE_ROOT = Path(memtomem.__file__).resolve().parent

CLASSES = {
    "client-cwd": "a path; relative starts at the connection's client_cwd",
    "memory-roots": "a path or a name under a configured memory directory",
    "export-dir": "a file name inside memtomem's export directory",
    "absolute-only": "a path; a relative value is refused",
    "stored-spelling": "path text compared with stored source keys, never resolved",
    "routed": "another tool's parameters, each classified under that tool",
    "not-a-path": "no path: text, an id, a name, a timestamp, an enumeration",
}

# ``tool: parameters``, space-separated.
TABLE: dict[str, dict[str, str]] = {
    "client-cwd": {
        "mem_cleanup_orphans": "under",
        "mem_import": "input_file project_root_map",
        "mem_import_notion": "path",
        "mem_import_obsidian": "vault_path",
        "mem_index": "path",
        "mem_ingest": "source",
    },
    "memory-roots": {
        "mem_add": "file",
        "mem_batch_add": "file",
        # Rendered to ``hermes-mirror/<owner>/<target>.md`` under the first user root.
        "mem_hermes_mirror_apply": "owner target",
        "mem_scratch_promote": "file",
    },
    "export-dir": {"mem_export": "output_file"},
    "absolute-only": {"mem_project_bind": "project_root"},
    "stored-spelling": {
        "mem_ask": "source_filter",
        "mem_auto_tag": "source_filter",
        "mem_consolidate": "source_filter",
        "mem_decay_expire": "source_filter",
        "mem_decay_scan": "source_filter",
        "mem_delete": "source_file",
        "mem_entity_scan": "source_filter",
        "mem_export": "source_filter",
        "mem_list": "source_filter",
        "mem_ns_assign": "source_filter",
        "mem_recall": "source_filter",
        "mem_search": "source_filter",
    },
    "routed": {"mem_do": "params"},
    "not-a-path": {
        "mem_activity": "since until namespace",
        "mem_add": "content title tags namespace template scope idempotency_key",
        "mem_agent_register": "agent_id description color",
        "mem_agent_search": "query agent_id output_format shared_namespace",
        "mem_agent_share": "chunk_id target idempotency_key",
        "mem_ask": "question namespace tag_filter as_of scope",
        "mem_batch_add": "entries namespace scope idempotency_key",
        "mem_candidate_evidence": "candidate_id",
        "mem_candidate_list": "status",
        # ``source_ref`` may name a file, as text that is stored and never opened.
        "mem_candidate_propose": "content source source_ref idempotency_key",
        "mem_candidate_recover": "actor",
        "mem_candidate_review": "candidate_id decision reviewer reason",
        "mem_config": "key value",
        "mem_conflict_check": "content",
        "mem_consolidate": "namespace",
        "mem_consolidate_apply": "summary",
        "mem_context_compose": "query agent_id namespace",
        "mem_context_detect": "include",
        "mem_context_diff": "include scope",
        "mem_dedup_merge": "keep_id delete_ids",
        "mem_delete": "chunk_id namespace",
        "mem_do": "action",
        "mem_edit": "chunk_id new_content base_hash",
        "mem_embedding_reset": "mode",
        "mem_entity_scan": "namespace entity_types",
        "mem_entity_search": "entity_type value namespace scope",
        "mem_eval": "since namespace",
        "mem_expand": "chunk_id",
        "mem_export": "tag_filter since namespace",
        # ``http://`` or ``https://`` only.
        "mem_fetch": "url tags namespace",
        "mem_formation_scan": "session_id",
        "mem_hermes_mirror_apply": "op content previous_content",
        "mem_import": "namespace on_conflict",
        "mem_import_notion": "namespace tags",
        "mem_import_obsidian": "namespace tags",
        "mem_importance_scan": "namespace",
        "mem_increment_access": "chunk_ids",
        "mem_index": "namespace",
        "mem_ingest": "source_type",
        "mem_link": "source_id target_id relation_type",
        "mem_list": "namespace cursor",
        "mem_ns_assign": "namespace old_namespace",
        "mem_ns_delete": "namespace",
        "mem_ns_rename": "old new",
        "mem_ns_set": "namespace",
        "mem_ns_update": "namespace description color",
        "mem_pinned_delete": "block_id scope agent_id",
        "mem_pinned_get": "block_id scope agent_id",
        "mem_pinned_list": "agent_id",
        "mem_pinned_set": "block_id content scope agent_id description",
        # JSON whose keys are thresholds and namespaces; none is a path.
        "mem_policy_add": "name policy_type config namespace_filter",
        "mem_policy_delete": "name",
        "mem_policy_run": "name",
        "mem_procedure_save": "name steps trigger tags namespace",
        # Case ids or names in the store, not files.
        "mem_quality_replay": "cases",
        "mem_read": "chunk_id",
        "mem_recall": "since until tag_filter namespace output_format scope",
        "mem_reflect": "namespace since",
        "mem_reflect_save": "insight related_chunks tags",
        "mem_related": "chunk_id",
        "mem_schedule_delete": "id",
        # Job parameters are checked against the job's model; none declares a path.
        "mem_schedule_register": "cron job_kind params",
        "mem_schedule_run_now": "id",
        "mem_scratch_get": "key",
        "mem_scratch_promote": "key title tags",
        "mem_scratch_set": "key value",
        "mem_search": "query tag_filter namespace as_of output_format scope",
        "mem_search_feedback": "run_id chunk_id judgment",
        "mem_search_history": "since",
        "mem_search_suggest": "prefix",
        "mem_session_end": "summary",
        "mem_session_list": "agent_id since",
        "mem_session_start": "agent_id title namespace",
        "mem_tag_delete": "tag",
        "mem_tag_merge": "sources target",
        "mem_tag_rename": "old_tag new_tag",
        "mem_timeline": "topic since until namespace scope",
        "mem_unlink": "source_id target_id",
        "mem_update_coverage": "shas run_id",
        "mem_watchdog": "command check",
    },
}

# ``(module, function, what it calls)``: the reads of the process working
# directory that stay, and why each is right.
PROCESS_CWD_READS: dict[tuple[str, str, str], str] = {
    ("runtime/project_context.py", "_resolve_project_context_from_dirs", "getcwd"): (
        "the fallback when no anchor is passed: a connection with no client_cwd, and the "
        "CLI and web callers, which own their process"
    ),
    ("server/tools/status_config.py", "collect_status_report", "getcwd"): (
        "the fallback when the connection has no client_cwd; also what `mm status` reports"
    ),
    ("context/scope_resolver.py", "find_project_root", "cwd"): (
        "the default start for a caller that passes none; the CLI and the web app do"
    ),
    ("server/tools/context.py", "_find_project_root", "find_project_root"): (
        "the context tools' fallback when the connection has no client_cwd"
    ),
    ("server/health_checks.py", "check_full_health_report", "_resolve_project_context_from_dirs"): (
        "the background health report: its snapshots carry no project identity, so it must "
        "not follow any one client"
    ),
}

_NUMERIC = {bool, int, float, type(None)}


def _leaf_types(hint: object) -> set[object]:
    origin = typing.get_origin(hint)
    if origin is typing.Annotated:
        return _leaf_types(typing.get_args(hint)[0])
    if origin in (typing.Union, types.UnionType):
        return set().union(*(_leaf_types(arg) for arg in typing.get_args(hint)))
    if origin is typing.Literal:
        return {type(value) for value in typing.get_args(hint)}
    return {hint}


def _cannot_carry_a_path(fn: object, parameter: str) -> bool:
    hints = typing.get_type_hints(inspect.unwrap(fn))  # type: ignore[arg-type]
    return parameter in hints and _leaf_types(hints[parameter]) <= _NUMERIC


def _rows() -> dict[tuple[str, str], str]:
    rows: dict[tuple[str, str], str] = {}
    for cls, tools in TABLE.items():
        for tool, parameters in tools.items():
            for parameter in parameters.split():
                assert (tool, parameter) not in rows, f"{tool}({parameter}) is classified twice"
                rows[(tool, parameter)] = cls
    return rows


def _needs_a_row() -> set[tuple[str, str]]:
    return {
        (tool, parameter)
        for tool, fn in tool_functions().items()
        for parameter in tool_parameters(fn)
        if not _cannot_carry_a_path(fn, parameter)
    }


def test_every_parameter_that_can_carry_a_path_is_classified() -> None:
    missing = _needs_a_row() - set(_rows())
    assert not missing, (
        "classify each in TABLE (test_client_cwd_table.py) by the directory a relative "
        f"path in it would start at: {sorted(missing)}"
    )


def test_the_table_has_no_row_for_a_parameter_that_is_gone() -> None:
    stale = set(_rows()) - _needs_a_row()
    assert not stale, f"no such tool parameter, or one that is numeric only: {sorted(stale)}"
    assert set(TABLE) == set(CLASSES)


def test_the_table_covers_the_tool_surface() -> None:
    """Guard the guard: a registry that came back empty would pass every check."""
    tools = tool_functions()
    assert len(tools) >= 97
    assert len(_needs_a_row()) >= 211
    assert {"mem_index", "mem_do", "mem_ingest", "mem_cleanup_orphans"} <= set(tools)


def test_numeric_parameters_are_recognised_by_annotation() -> None:
    tools = tool_functions()
    assert _cannot_carry_a_path(tools["mem_index"], "recursive")  # bool
    assert _cannot_carry_a_path(tools["mem_search"], "top_k")  # int
    assert _cannot_carry_a_path(tools["mem_dedup_scan"], "threshold")  # float
    assert _cannot_carry_a_path(tools["mem_search"], "deadline_ms")  # int | None
    assert not _cannot_carry_a_path(tools["mem_index"], "path")  # str
    assert not _cannot_carry_a_path(tools["mem_cleanup_orphans"], "under")  # str | list | None
    assert not _cannot_carry_a_path(tools["mem_import"], "project_root_map")  # dict
    assert not _cannot_carry_a_path(tools["mem_add"], "scope")  # Literal of strings


def test_every_client_cwd_parameter_has_a_direct_test() -> None:
    tests = [name for name in vars(test_client_cwd) if name.startswith("test_")]
    without = [
        f"{tool}({parameter})"
        for (tool, parameter), cls in _rows().items()
        if cls == "client-cwd"
        and not any(name.startswith(f"test_{tool}_{parameter}") for name in tests)
    ]
    assert not without, (
        "each needs a test in test_client_cwd.py named test_<tool>_<parameter>, with the "
        f"process and the client in different directories: {without}"
    )


# ---------------------------------------------------------------------------
# Explicit reads of the process working directory
# ---------------------------------------------------------------------------

_PROCESS_OWNING_PACKAGES = ("cli", "web", "integrations")


class _CwdReads(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[tuple[str, str, int]] = []  # (function, what, line)
        self._functions: list[str] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        what = None
        if name in ("getcwd", "getcwdb"):
            what = "getcwd"
        elif name == "cwd" and isinstance(func, ast.Attribute):
            what = "cwd"
        elif name == "find_project_root" and not node.args and not node.keywords:
            what = name
        elif (
            name == "_resolve_project_context_from_dirs"
            and len(node.args) < 2
            and not any(keyword.arg == "anchor" or keyword.arg is None for keyword in node.keywords)
        ):
            what = name
        if what is not None:
            function = self._functions[0] if self._functions else "<module>"
            self.found.append((function, what, node.lineno))
        self.generic_visit(node)


def _scan(source: str) -> list[tuple[str, str, int]]:
    visitor = _CwdReads()
    visitor.visit(ast.parse(source))
    return visitor.found


def _reads_in_the_package() -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT)
        if relative.parts[0] in _PROCESS_OWNING_PACKAGES:
            continue
        for function, what, line in _scan(path.read_text(encoding="utf-8")):
            found.setdefault((relative.as_posix(), function, what), []).append(line)
    return found


def test_no_unregistered_read_of_the_process_working_directory() -> None:
    found = _reads_in_the_package()
    unregistered = {
        f"{module}:{lines} {function} calls {what}"
        for (module, function, what), lines in found.items()
        if (module, function, what) not in PROCESS_CWD_READS
    }
    assert not unregistered, (
        "a server serving several clients has no working directory of its own to offer: "
        "read the connection's client_cwd (server/client_paths.py), or register the site "
        f"in PROCESS_CWD_READS with the reason it is the process's: {sorted(unregistered)}"
    )
    stale = set(PROCESS_CWD_READS) - set(found)
    assert not stale, f"registered, but the read is gone: {sorted(stale)}"


def test_the_scan_recognises_each_form() -> None:
    source = """
import os
from pathlib import Path

def reads():
    os.getcwd()
    os.getcwdb()
    Path.cwd()
    find_project_root()
    scope_resolver.find_project_root()
    _resolve_project_context_from_dirs(dirs)
    project_context._resolve_project_context_from_dirs(project_memory_dirs=dirs)

async def outer():
    def inner():
        return os.getcwd()

def does_not_read():
    find_project_root(start)
    find_project_root(start=start)
    _resolve_project_context_from_dirs(dirs, anchor=anchor)
    _resolve_project_context_from_dirs(dirs, anchor)
    _resolve_project_context_from_dirs(dirs, **options)
    path.resolve()

Path.cwd()
"""
    found = [(function, what) for function, what, _line in _scan(source)]
    assert found == [
        ("reads", "getcwd"),
        ("reads", "getcwd"),
        ("reads", "cwd"),
        ("reads", "find_project_root"),
        ("reads", "find_project_root"),
        ("reads", "_resolve_project_context_from_dirs"),
        ("reads", "_resolve_project_context_from_dirs"),
        ("outer", "getcwd"),
        ("<module>", "cwd"),
    ]

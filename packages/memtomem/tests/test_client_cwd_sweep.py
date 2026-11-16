"""No tool reads the process working directory for a client that named its own.

A sweep that observes rather than reads. It gives the connection a
``client_cwd``, puts the process in another directory, and calls every server
tool once per parameter with a unique relative sentinel in that parameter. Two
signals fail it:

- **The sentinel.** A path that carries a sentinel reaches ``resolve``,
  ``abspath``, ``open``, ``stat`` or one of their relatives while it is still
  relative, or as an absolute path under the process working directory. Either
  way the process working directory decided where a client's argument points.
  A relative path passed with a ``dir_fd`` is relative to that directory and
  is exempt; an absolute one ignores the descriptor and is not.
- **``os.getcwd``.** A call with memtomem code on its stack, from a site that
  is not registered below as owned by the process.

The sentinel is the main signal, and it is asserted on every platform. The
``os.getcwd`` signal is not asserted on Windows. ``ntpath.realpath`` reads the
working directory before it looks at its argument, so there every
``resolve()`` calls ``os.getcwd``, of an absolute path too, and the call says
nothing about whose directory was used (first seen on this change's Windows
CI: 661 such calls, no sentinel finding). What that leaves unseen on Windows
is a relative path with no sentinel in it, such as a default ``"."``, made
absolute; an explicit ``os.getcwd()`` or ``Path.cwd()`` is still refused by
source location in ``test_client_cwd_table.py``.

The tools are the union of the two registries, since neither holds all of
them: the action registry, which has every ``@register``-ed tool whatever the
tool mode, and the tools registered on the server itself. A tool added to
either is swept without anyone listing it. A registered action is called
through ``mem_do``, so it sees the validation a client's call sees.

What this does not see, and ``test_client_cwd_table.py`` does not either:

- a helper that uses a relative path it did not receive from a parameter and
  does not call ``os.getcwd`` (a literal ``Path("settings.json").read_text()``
  in an action that takes no parameters);
- a dependency behind an early exit for which the sweep has no fixture
  (``mem_import`` needs a real bundle before it reads ``project_root_map``,
  and has one; another tool could need one that is not here);
- a branch these inputs do not reach. ``mem_project_bind`` refuses every call
  at its transport gate, and the background health report does not run;
- a filesystem call that is not among those wrapped below (``os.symlink``,
  ``os.chmod``), unless it goes on to one that is;
- two spellings that no code here writes: a relative path whose ``dir_fd`` is
  the platform's ``AT_FDCWD`` number instead of ``None``, and the process
  working directory behind a doubled leading slash.

A path is placed by its text. One written through the process working
directory (``<cwd>/../elsewhere``) is therefore reported wherever it ends up,
which is the right answer: it was built from that directory.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import inspect
import io
import os
import pathlib
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import _home_guard
import memtomem
from helpers import StubCtx, set_home
from memtomem.server.context import AppContext

MARK = "cwdsweep"
_PACKAGE_ROOT = Path(memtomem.__file__).resolve().parent
_HOME_GUARD_FILE = _home_guard.__file__

# ``os.getcwd`` callers that keep the process working directory on purpose,
# as ``(module path under the package, function)``, each with its reason. The
# sweep runs with ``client_cwd`` set, so the fallbacks that read the process
# working directory only when it is unset are not reached and are not here;
# ``test_client_cwd_table.py`` registers those by source location.
PROCESS_OWNED_GETCWD: dict[tuple[str, str], str] = {}

# Whether a call of ``os.getcwd`` tells anything. Not on Windows, where
# resolving an absolute path makes one too; see the module docstring and
# ``test_resolving_an_absolute_path_reads_the_working_directory_only_on_windows``.
GETCWD_IS_A_SIGNAL = os.name != "nt"


@dataclass(frozen=True)
class PathArgument:
    """One parameter of a wrapped call that names a file."""

    position: int
    # Its keyword, under every name an implementation of the call gives it.
    keywords: tuple[str, ...]
    # The keyword of the directory descriptor that goes with it, if any.
    descriptor: str | None = None


def _path(*keywords: str, descriptor: str | None = None) -> tuple[PathArgument, ...]:
    return (PathArgument(0, keywords, descriptor),)


# ``posixpath``, ``ntpath`` and ``genericpath`` do not agree on the name.
_OS_PATH = _path("path", "s", "filename")
_TWO_ENDS = (
    PathArgument(0, ("src",), "src_dir_fd"),
    PathArgument(1, ("dst",), "dst_dir_fd"),
)

# The wrapped calls, and which of their parameters are paths. Nothing else is
# looked at: a mode, an encoding or the name of an error handler is not a path,
# whatever text it holds. ``test_the_table_names_each_calls_real_parameters``
# holds the names against the signatures of the platform it runs on.
WRAPPED_CALLS: dict[tuple[str, str], tuple[PathArgument, ...]] = {
    ("os.path", "abspath"): _OS_PATH,
    ("os.path", "realpath"): _OS_PATH,
    ("os.path", "exists"): _OS_PATH,
    ("os.path", "lexists"): _OS_PATH,
    ("os.path", "isdir"): _OS_PATH,
    ("os.path", "isfile"): _OS_PATH,
    ("os.path", "islink"): _OS_PATH,
    ("os", "stat"): _path("path", descriptor="dir_fd"),
    ("os", "lstat"): _path("path", descriptor="dir_fd"),
    ("os", "scandir"): _path("path"),
    ("os", "listdir"): _path("path"),
    ("os", "open"): _path("path", descriptor="dir_fd"),
    ("os", "mkdir"): _path("path", descriptor="dir_fd"),
    ("os", "makedirs"): _path("name"),
    ("os", "remove"): _path("path", descriptor="dir_fd"),
    ("os", "unlink"): _path("path", descriptor="dir_fd"),
    ("os", "rmdir"): _path("path", descriptor="dir_fd"),
    ("os", "access"): _path("path", descriptor="dir_fd"),
    ("os", "chdir"): _path("path"),
    ("os", "rename"): _TWO_ENDS,
    ("os", "replace"): _TWO_ENDS,
    ("builtins", "open"): _path("file"),
    ("io", "open"): _path("file"),
}
_MODULES: dict[str, object] = {"os.path": os.path, "os": os, "builtins": builtins, "io": io}


@dataclass(frozen=True)
class Observation:
    call: str
    kind: str  # "relative", "under-process-cwd" or "getcwd"
    op: str
    path: str
    site: tuple[str, str] | None


def tool_functions() -> dict[str, Callable[..., Any]]:
    """Every server tool: the action registry and the server's own registrations."""
    from memtomem.server import mcp
    from memtomem.server.tool_registry import ACTIONS

    found = {f"mem_{action}": info.fn for action, info in ACTIONS.items()}
    for tool in mcp._tool_manager.list_tools():
        found.setdefault(tool.name, tool.fn)
    return found


def tool_parameters(fn: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    """A tool's parameters except ``ctx``."""
    signature = inspect.signature(inspect.unwrap(fn))
    return {name: p for name, p in signature.parameters.items() if name != "ctx"}


def _annotation(parameter: inspect.Parameter) -> str:
    if parameter.annotation is inspect.Parameter.empty:
        return ""
    raw = parameter.annotation
    return (raw if isinstance(raw, str) else str(raw)).replace(" ", "")


def _sentinel_value(annotation: str, sentinel: str) -> object:
    if "dict" in annotation:
        return {"/cwdsweep-old-root": sentinel}
    if annotation.startswith("list"):
        return [sentinel]
    return sentinel


def _dummy(annotation: str, name: str) -> object:
    if "Literal[" in annotation:
        inner = annotation.split("Literal[", 1)[1].split("]", 1)[0]
        return inner.split(",")[0].strip("'\"")
    if annotation.startswith("bool"):
        return False
    if annotation.startswith("int"):
        return 1
    if annotation.startswith("float"):
        return 0.5
    if annotation.startswith("list"):
        return [f"dummy-{name}"]
    if annotation.startswith("dict"):
        return {"k": f"dummy-{name}"}
    return f"dummy-{name}"


_HARNESS = ("harness", "")


def _site() -> tuple[str, str] | None:
    """The nearest memtomem frame on the stack, as ``(module path, function)``.

    ``_HARNESS`` when the suite's home guard is nearer than any memtomem
    frame. Its audit hook runs inside every ``open`` and makes the name
    absolute to judge it, a ``dir_fd``-relative name included; that read of
    the working directory is the guard's, not the tool's.
    """
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename == _HOME_GUARD_FILE:
            return _HARNESS
        try:
            relative = Path(filename).relative_to(_PACKAGE_ROOT)
        except ValueError:
            frame = frame.f_back
            continue
        return relative.as_posix(), frame.f_code.co_name
    return None


class Observer:
    """Records where a sentinel, or the process working directory, is used."""

    def __init__(self, process_cwd: Path) -> None:
        self.records: list[Observation] = []
        # ``None`` between calls: the sweep's own fixture work is not a tool's.
        self.call: str | None = None
        self._lock = threading.Lock()
        # Both spellings: a temporary directory is often reached through a
        # symlink (``/var`` on macOS), and a join made before any resolution
        # carries the unresolved one.
        self._under = {str(process_cwd) + os.sep, str(process_cwd.resolve()) + os.sep}

    def _add(self, kind: str, op: str, path: str, site: tuple[str, str] | None) -> None:
        call = self.call
        if call is None or site == _HARNESS:
            return
        with self._lock:
            self.records.append(Observation(call, kind, op, path, site))

    @contextlib.contextmanager
    def during(self, call: str) -> Iterator[None]:
        """Attribute what happens inside the block to ``call``."""
        self.call = call
        try:
            yield
        finally:
            self.call = None

    def note(self, op: str, arg: object) -> None:
        if not isinstance(arg, (str, bytes, os.PathLike)):
            return
        try:
            value = os.fspath(arg)
        except TypeError:
            return
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if MARK not in text:
            return
        if not os.path.isabs(text):
            self._add("relative", op, text, _site())
        elif any(text.startswith(prefix) for prefix in self._under):
            self._add("under-process-cwd", op, text, _site())

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def wrap_function(module: object, name: str, arguments: tuple[PathArgument, ...]) -> None:
            real = getattr(module, name)

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                for argument in arguments:
                    if argument.position < len(args):
                        value = args[argument.position]
                    else:
                        value = next((kwargs[k] for k in argument.keywords if k in kwargs), None)
                    descriptor = kwargs.get(argument.descriptor) if argument.descriptor else None
                    # The home guard's rule: a directory descriptor makes a
                    # relative path relative to it, and an absolute path
                    # ignores it, so only the first is out of this check.
                    if _home_guard._resolvable(value, descriptor):
                        self.note(name, value)
                return real(*args, **kwargs)

            monkeypatch.setattr(module, name, wrapper)

        for (module_name, name), arguments in WRAPPED_CALLS.items():
            wrap_function(_MODULES[module_name], name, arguments)

        for name in ("resolve", "absolute"):
            real_method = getattr(pathlib.Path, name)

            def method(
                path: Path, *args: Any, _real: Any = real_method, _name: str = name, **kwargs: Any
            ) -> Any:
                self.note(f"Path.{_name}", path)
                return _real(path, *args, **kwargs)

            monkeypatch.setattr(pathlib.Path, name, method)

        real_getcwd = os.getcwd

        def getcwd() -> str:
            site = _site()
            if site is not None:
                self._add("getcwd", "os.getcwd", "", site)
            return real_getcwd()

        monkeypatch.setattr(os, "getcwd", getcwd)

    def violations(self, getcwd_is_a_signal: bool = GETCWD_IS_A_SIGNAL) -> list[Observation]:
        return [
            record
            for record in self.records
            if record.kind != "getcwd"
            or (getcwd_is_a_signal and record.site not in PROCESS_OWNED_GETCWD)
        ]


@pytest.fixture
def sweep_setup(bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real store, a registered project, a client in it, and the process elsewhere."""
    comp, _mem_dir = bm25_only_components
    set_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    client = (tmp_path / "client-project").resolve()
    tier = client / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    (client / ".git").mkdir()
    comp.config.indexing.project_memory_dirs = [tier]
    process_cwd = tmp_path / "process"
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    app = AppContext.from_components(comp)
    app.client_cwd = client
    return app, StubCtx(app), client, process_cwd


async def _call(
    observer: Observer,
    label: str,
    name: str,
    fn: Callable[..., Any],
    kwargs: dict[str, object],
    ctx: StubCtx,
) -> str:
    """One tool call. The outcome is recorded, not judged: most are refusals."""
    from memtomem.server.tool_registry import ACTIONS
    from memtomem.server.tools.meta import mem_do

    with observer.during(label):
        try:
            if name.removeprefix("mem_") in ACTIONS:
                call = mem_do(action=name.removeprefix("mem_"), params=kwargs, ctx=ctx)
            else:
                call = fn(**kwargs, ctx=ctx)
            return "returned: " + str(await asyncio.wait_for(call, timeout=30))[:120]
        except Exception as exc:
            return f"raised {type(exc).__name__}: {exc}"[:120]


async def run_sweep(
    observer: Observer, ctx: StubCtx, client: Path, process_cwd: Path
) -> dict[str, str]:
    """Call every tool with a sentinel in each parameter; return each call's outcome."""
    from memtomem.server.tools.export_import import mem_export
    from memtomem.server.tools.memory_crud import mem_add

    tools = tool_functions()
    outcomes: dict[str, str] = {}

    # Fixtures for the parameters an early exit would otherwise hide.
    fixtures: dict[str, dict[str, object]] = {
        "mem_batch_add": {"entries": [{"content": "sweep entry"}]},
    }
    await mem_add(content="a note for the sweep's bundle", ctx=ctx)
    await mem_export(output_file="sweep-bundle.json", ctx=ctx)
    bundle = Path.home() / ".memtomem" / "exports" / "sweep-bundle.json"
    assert bundle.is_file(), "the sweep needs a real bundle for mem_import"
    fixtures["mem_import"] = {"input_file": str(bundle)}

    def required(name: str, skip: str | None = None) -> dict[str, object]:
        kwargs = {k: v for k, v in fixtures.get(name, {}).items() if k != skip}
        for other, parameter in tool_parameters(tools[name]).items():
            if other == skip or other in kwargs:
                continue
            if parameter.default is inspect.Parameter.empty:
                kwargs[other] = _dummy(_annotation(parameter), other)
        return kwargs

    # Once with no sentinel, so a tool that reads the working directory
    # without being handed a path still shows up under ``os.getcwd``.
    for name in sorted(tools):
        label = f"{name}(<no sentinel>)"
        outcomes[label] = await _call(observer, label, name, tools[name], required(name), ctx)

    # Then once per parameter, without and with a directory of the sentinel's
    # name on both sides, since a tool that finds nothing often stops there.
    for with_directory in (False, True):
        for name in sorted(tools):
            for parameter_name, parameter in tool_parameters(tools[name]).items():
                sentinel = f"{MARK}-{name}-{parameter_name}"
                if with_directory:
                    (client / sentinel).mkdir(exist_ok=True)
                    (process_cwd / sentinel).mkdir(exist_ok=True)
                kwargs = required(name, skip=parameter_name)
                kwargs[parameter_name] = _sentinel_value(_annotation(parameter), sentinel)
                label = f"{name}({parameter_name})|directory={with_directory}"
                outcomes[label] = await _call(observer, label, name, tools[name], kwargs, ctx)
    return outcomes


def _report(records: list[Observation]) -> str:
    lines = sorted(
        {f"{r.call}: {r.kind} via {r.op} {r.path!r} at {r.site}" for r in records},
    )
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_no_tool_call_reads_the_process_working_directory(
    sweep_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, ctx, client, process_cwd = sweep_setup
    observer = Observer(process_cwd)
    with monkeypatch.context() as patches:
        observer.install(patches)
        outcomes = await run_sweep(observer, ctx, client, process_cwd)

    tools = tool_functions()
    parameters = sum(len(tool_parameters(fn)) for fn in tools.values())
    # Guard the guard: an empty or shrunken sweep would pass vacuously.
    assert len(tools) >= 97 and parameters >= 301, (len(tools), parameters)
    assert len(outcomes) == len(tools) + 2 * parameters
    assert not any(o.startswith("raised TimeoutError") for o in outcomes.values()), [
        label for label, o in outcomes.items() if o.startswith("raised TimeoutError")
    ]

    violations = observer.violations()
    assert not violations, (
        "a tool call used the process working directory although its connection "
        "has a client_cwd:\n" + _report(violations)
    )
    unused = set(PROCESS_OWNED_GETCWD) - {r.site for r in observer.records if r.kind == "getcwd"}
    assert not unused, f"registered as process-owned but never observed: {sorted(unused)}"


# ---------------------------------------------------------------------------
# Witnesses: each signal fires on the thing it exists to catch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_sees_the_dependencies_when_no_client_is_named(
    sweep_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``client_cwd`` unset the same sweep reports what this change moved.

    This is the witness for the whole instrument. If it found nothing here,
    the clean result above would say nothing.
    """
    app, ctx, client, process_cwd = sweep_setup
    app.client_cwd = None
    observer = Observer(process_cwd)
    with monkeypatch.context() as patches:
        observer.install(patches)
        await run_sweep(observer, ctx, client, process_cwd)

    seen = {r.call.split("|")[0] for r in observer.records if r.kind != "getcwd"}
    expected = {
        "mem_index(path)",
        "mem_ingest(source)",
        "mem_import_notion(path)",
        "mem_import_obsidian(vault_path)",
        "mem_import(input_file)",
        "mem_import(project_root_map)",
        "mem_cleanup_orphans(under)",
    }
    assert expected <= seen, sorted(expected - seen)
    getcwd_sites = {r.site for r in observer.records if r.kind == "getcwd"}
    assert ("runtime/project_context.py", "_resolve_project_context_from_dirs") in getcwd_sites
    assert ("server/tools/status_config.py", "collect_status_report") in getcwd_sites
    assert ("context/scope_resolver.py", "find_project_root") in getcwd_sites


def test_the_observer_sees_each_kind_of_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_cwd = tmp_path / "process"
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    observer = Observer(process_cwd)
    relative = f"{MARK}-witness"
    with monkeypatch.context() as patches, observer.during("witness"):
        observer.install(patches)
        Path(relative).resolve()
        Path(relative).absolute()
        os.path.abspath(relative)
        os.path.realpath(relative)
        Path(relative).exists()
        Path(relative).is_dir()
        Path(relative).mkdir()
        os.listdir(relative)
        with open(os.path.join(relative, "f"), "w", encoding="utf-8") as handle:
            handle.write("x")
        Path(relative, "f").read_text(encoding="utf-8")
        (process_cwd / relative / "f").read_text(encoding="utf-8")
        (tmp_path / f"{MARK}-elsewhere").mkdir()

    ops = {(r.kind, r.op) for r in observer.records}
    for op in ("Path.resolve", "Path.absolute", "abspath", "realpath", "mkdir", "listdir", "open"):
        assert ("relative", op) in ops, (op, sorted(ops))
    assert ("under-process-cwd", "open") in ops, sorted(ops)
    # ``exists`` and ``is_dir`` reach ``os.stat`` or ``os.path`` depending on
    # the Python version; one of the two is wrapped either way.
    assert {("relative", "stat"), ("relative", "exists")} & ops, sorted(ops)
    # An absolute path outside the process working directory is not a finding.
    assert not [r for r in observer.records if "elsewhere" in r.path]


def test_a_dir_fd_call_is_not_a_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process_cwd = tmp_path / "process"
    (process_cwd / "d").mkdir(parents=True)
    (process_cwd / "d" / f"{MARK}-leaf").write_text("x", encoding="utf-8")
    monkeypatch.chdir(process_cwd)
    if os.stat not in os.supports_dir_fd:
        pytest.skip("os.stat has no dir_fd on this platform")
    observer = Observer(process_cwd)
    fd = os.open("d", os.O_RDONLY)
    try:
        with monkeypatch.context() as patches, observer.during("witness"):
            observer.install(patches)
            os.stat(f"{MARK}-leaf", dir_fd=fd)
            os.stat(os.path.join("d", f"{MARK}-leaf"))
    finally:
        os.close(fd)
    assert [(r.kind, r.path) for r in observer.records] == [
        ("relative", os.path.join("d", f"{MARK}-leaf"))
    ]


def test_a_path_passed_by_keyword_or_as_a_destination_is_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_cwd = tmp_path / "process"
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    Path("plain-source").write_text("x", encoding="utf-8")
    Path(f"{MARK}-keyword").write_text("x", encoding="utf-8")
    observer = Observer(process_cwd)
    with monkeypatch.context() as patches, observer.during("witness"):
        observer.install(patches)
        os.stat(path=f"{MARK}-keyword")
        os.rename("plain-source", f"{MARK}-renamed")
        os.replace(src=f"{MARK}-renamed", dst=f"{MARK}-replaced")
        with open(file=f"{MARK}-keyword", encoding="utf-8") as handle:
            handle.read()

    assert [(r.op, r.path) for r in observer.records] == [
        ("stat", f"{MARK}-keyword"),
        ("rename", f"{MARK}-renamed"),
        ("replace", f"{MARK}-renamed"),
        ("replace", f"{MARK}-replaced"),
        ("open", f"{MARK}-keyword"),
    ]
    assert {r.kind for r in observer.records} == {"relative"}


def test_each_end_of_a_rename_has_its_own_directory_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_cwd = tmp_path / "process"
    (process_cwd / "d").mkdir(parents=True)
    monkeypatch.chdir(process_cwd)
    if os.rename not in os.supports_dir_fd:
        pytest.skip("os.rename has no dir_fd on this platform")
    (process_cwd / "d" / f"{MARK}-inside").write_text("x", encoding="utf-8")
    observer = Observer(process_cwd)
    fd = os.open("d", os.O_RDONLY)
    try:
        with monkeypatch.context() as patches, observer.during("witness"):
            observer.install(patches)
            # Both ends under the descriptor: neither is the working directory's.
            os.rename(f"{MARK}-inside", f"{MARK}-moved", src_dir_fd=fd, dst_dir_fd=fd)
            # Only the source is under it: the destination lands in the working directory.
            os.rename(f"{MARK}-moved", f"{MARK}-outside", src_dir_fd=fd)
    finally:
        os.close(fd)

    assert [(r.kind, r.op, r.path) for r in observer.records] == [
        ("relative", "rename", f"{MARK}-outside")
    ]
    assert (process_cwd / f"{MARK}-outside").is_file()


def test_an_absolute_path_is_judged_whatever_descriptor_comes_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operating system ignores a directory descriptor for an absolute path."""
    process_cwd = tmp_path / "process"
    (process_cwd / "d").mkdir(parents=True)
    monkeypatch.chdir(process_cwd)
    if os.rename not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        pytest.skip("no dir_fd on this platform")
    (process_cwd / "d" / "plain").write_text("x", encoding="utf-8")
    destination = str(process_cwd / f"{MARK}-absolute")
    observer = Observer(process_cwd)
    fd = os.open("d", os.O_RDONLY)
    try:
        with monkeypatch.context() as patches, observer.during("witness"):
            observer.install(patches)
            os.rename("plain", destination, src_dir_fd=fd, dst_dir_fd=fd)
            os.stat(destination, dir_fd=fd)
    finally:
        os.close(fd)

    assert [(r.kind, r.op, r.path) for r in observer.records] == [
        ("under-process-cwd", "rename", destination),
        ("under-process-cwd", "stat", destination),
    ]
    assert Path(destination).is_file()


def test_an_option_that_is_not_a_path_is_not_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel in an encoding option names no file."""
    import codecs

    process_cwd = tmp_path / "process"
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("x", encoding="utf-8")
    handler = f"{MARK}-handler"
    codecs.register_error(handler, codecs.strict_errors)
    observer = Observer(process_cwd)
    with monkeypatch.context() as patches, observer.during("witness"):
        observer.install(patches)
        with open(elsewhere, encoding="utf-8", errors=handler) as handle:
            assert handle.read() == "x"
        with open(file=elsewhere, mode="r", encoding="utf-8", errors=handler) as handle:
            assert handle.read() == "x"

    assert observer.records == []


def test_the_table_names_each_calls_real_parameters() -> None:
    """A path parameter under a name the table does not know would go unseen."""
    checked = 0
    for (module_name, name), arguments in WRAPPED_CALLS.items():
        try:
            parameters = inspect.signature(getattr(_MODULES[module_name], name)).parameters
        except (TypeError, ValueError):
            continue  # a builtin that publishes no signature on this platform
        names = list(parameters)
        for argument in arguments:
            assert names[argument.position] in argument.keywords, (module_name, name, names)
            if argument.descriptor is not None:
                assert argument.descriptor in parameters, (module_name, name, names)
        checked += 1
    # The ``os`` calls and ``open`` publish a signature everywhere.
    assert checked >= 16, checked
    # A row taken out is a call no longer observed; a row added moves this number.
    assert len(WRAPPED_CALLS) == 23


# What a wrapped call needs besides its paths before it will run at all.
_OTHER_ARGUMENTS: dict[tuple[str, str], tuple[object, ...]] = {
    ("os", "open"): (os.O_RDONLY,),
    ("os", "access"): (os.F_OK,),
}


@pytest.mark.parametrize("call", sorted(WRAPPED_CALLS), ids=".".join)
def test_every_wrapped_call_is_observed(
    call: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row of the table on its own: the call is wrapped and its paths are read."""
    module_name, name = call
    process_cwd = tmp_path / "process"
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    # Paths that do not exist: most of these calls then refuse, which is all
    # this needs, and ``os.chdir`` leaves the test where it is.
    paths = [f"{MARK}-{name}-{index}" for index, _ in enumerate(WRAPPED_CALLS[call])]
    observer = Observer(process_cwd)
    with monkeypatch.context() as patches, observer.during("witness"):
        observer.install(patches)
        wrapped = getattr(_MODULES[module_name], name)
        with contextlib.suppress(OSError):
            wrapped(*paths, *_OTHER_ARGUMENTS.get(call, ()))

    # Some calls reach others that are wrapped too (``exists`` may ``stat``);
    # only this call's own observations are counted.
    own = [(r.kind, r.path) for r in observer.records if r.op == name]
    assert own == [("relative", path) for path in paths], observer.records


def test_getcwd_is_attributed_to_the_memtomem_frame_that_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context.scope_resolver import find_project_root

    monkeypatch.chdir(tmp_path)
    observer = Observer(tmp_path)
    with monkeypatch.context() as patches:
        observer.install(patches)
        find_project_root()  # between calls: not a tool's read
        with observer.during("witness"):
            os.getcwd()  # no memtomem frame: the test's own read is not recorded
            find_project_root()

    assert [(r.kind, r.site) for r in observer.records] == [
        ("getcwd", ("context/scope_resolver.py", "find_project_root"))
    ]
    assert observer.violations(getcwd_is_a_signal=True) == observer.records
    # Where the call says nothing it is recorded all the same, and not judged.
    assert observer.violations(getcwd_is_a_signal=False) == []


def test_resolving_an_absolute_path_reads_the_working_directory_only_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why ``GETCWD_IS_A_SIGNAL`` is what it is, on the platform this runs on.

    If a platform changes sides, this fails and the flag follows it.
    """
    from memtomem.storage.sqlite_helpers import resolve_path

    absolute = tmp_path.resolve() / "somewhere"
    monkeypatch.chdir(tmp_path)
    observer = Observer(tmp_path)
    with monkeypatch.context() as patches, observer.during("witness"):
        observer.install(patches)
        resolve_path(absolute)

    read_it = [r.site for r in observer.records if r.kind == "getcwd"]
    if GETCWD_IS_A_SIGNAL:
        assert read_it == []
    else:
        assert set(read_it) == {("storage/sqlite_helpers.py", "resolve_path")}

"""A privacy refusal names a remedy the surface it reaches can run (#2797).

The memory-tool refusal used to end with one sentence for every surface: an
MCP tool name for the re-index and a shell command for the bypass. The CLI was
told to run ``mem_index``; an MCP caller was told to type ``mm index``. Each
surface now renders :func:`~memtomem.indexing.engine.privacy_remedy` in its own
vocabulary, and these tests check the commands it names against what each
surface actually registers rather than against fixed strings.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import shlex
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import click
import pytest

import memtomem
from helpers import StubCtx
from memtomem import privacy
from memtomem._runtime_paths import _hint_quote
from memtomem.cli import cli
from memtomem.indexing.engine import PrivacyRejection, privacy_remedy
from memtomem.server import mcp
from memtomem.server.context import AppContext

_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_CLEAN = "## A\n\nalpha entry about deploys\n\n## B\n\nbeta entry about reviews\n"
_SECRET_SECTION = f"\n## K\n\naws {_KEY}\n"
_LOCALE = json.loads(
    (Path(memtomem.__file__).parent / "web" / "static" / "locales" / "en.json").read_text(
        encoding="utf-8"
    )
)

# (scope, bypassable): ``project_shared`` refuses ``force_unsafe``.
_TIERS = [("user", True), ("project_local", True), ("project_shared", False)]


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _mm_commands(text: str) -> list[list[str]]:
    return [shlex.split(m) for m in re.findall(r"`mm ([^`]*)`", text)]


def _resolve_mm(argv: list[str]) -> tuple[click.Command, list[str], list[str]]:
    """The registered command ``argv`` names, its flags, and its operands."""
    cmd: click.Command = cli
    rest = list(argv)
    while isinstance(cmd, click.Group) and rest and rest[0] in cmd.commands:
        cmd = cmd.commands[rest.pop(0)]
    assert cmd is not cli, f"no registered command for {argv}"
    return cmd, [a for a in rest if a.startswith("-")], [a for a in rest if not a.startswith("-")]


def _assert_mm_runs(argv: list[str], path: object) -> list[str]:
    cmd, flags, operands = _resolve_mm(argv)
    options = {o for p in cmd.params if isinstance(p, click.Option) for o in p.opts}
    assert set(flags) <= options, (cmd.name, sorted(set(flags) - options))
    assert operands == [str(path)], "the command names this exact file"
    return flags


def _mcp_calls(text: str) -> list[ast.Call]:
    calls = [ast.parse(m, mode="eval").body for m in re.findall(r"`(mem_\w+\(.*?\))`", text)]
    assert all(isinstance(c, ast.Call) for c in calls)
    return calls  # type: ignore[return-value]


def _core_tools() -> dict[str, object]:
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


def _tampered_path(tmp_path: Path, parent: str = "memories") -> Path:
    return tmp_path / parent / "notes.md"


class TestEachSurfaceNamesItsOwnRemedy:
    @pytest.mark.parametrize("surface", ["cli", "shell"])
    @pytest.mark.parametrize(("scope", "bypassable"), _TIERS)
    def test_cli_commands_are_registered_and_name_the_file(
        self, tmp_path, surface, scope, bypassable
    ):
        path = _tampered_path(tmp_path)
        text = privacy_remedy(surface, path=path, bypassable=bypassable)

        commands = _mm_commands(text)
        assert commands, text
        flags = [_assert_mm_runs(argv, path) for argv in commands]
        assert "mem_" not in text
        forced = [f for f in flags if "--force-unsafe" in f]
        assert len(forced) == (1 if bypassable else 0), text
        if surface == "shell":
            # ``mm index`` typed at ``mm>`` would run a search.
            assert text.count("` (outside the shell)") == len(commands), text
        else:
            assert "shell" not in text

    @pytest.mark.parametrize(("scope", "bypassable"), _TIERS)
    def test_mcp_calls_are_core_tools_with_real_parameters(self, tmp_path, scope, bypassable):
        path = _tampered_path(tmp_path)
        text = privacy_remedy("mcp", path=path, bypassable=bypassable)

        calls = _mcp_calls(text)
        assert calls, text
        tools = _core_tools()
        for call in calls:
            name = call.func.id  # type: ignore[attr-defined]
            assert name in tools, f"{name} is not callable in the default core mode"
            params = inspect.signature(tools[name]).parameters  # type: ignore[arg-type]
            for kw in call.keywords:
                assert kw.arg in params, f"{name} has no parameter {kw.arg!r}"
            values = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
            # ``path`` defaults to the server's cwd, which need not hold the file.
            assert values.get("path") == str(path), text
        # ``mem_index`` has no ``force_unsafe``; the only bypass is the CLI's.
        commands = _mm_commands(text)
        assert len(commands) == (1 if bypassable else 0), text
        for argv in commands:
            assert _assert_mm_runs(argv, path) == ["--force-unsafe"]
        assert text.count("from the CLI with `mm ") == len(commands)

    @pytest.mark.parametrize(("scope", "bypassable"), _TIERS)
    def test_web_names_its_own_controls_and_no_host_path(self, tmp_path, scope, bypassable):
        path = _tampered_path(tmp_path)
        text = privacy_remedy("web", path=path, bypassable=bypassable)

        for foreign in ("mm ", "mem_", "force_unsafe", "`", str(path.parent)):
            assert foreign not in text, foreign
        assert f"{_LOCALE['nav.index']} tab" in text
        assert (f"'{_LOCALE['index.force_unsafe']}'" in text) is bypassable
        assert (_LOCALE["index.advanced_options"] in text) is bypassable

    @pytest.mark.parametrize(("scope", "bypassable"), _TIERS)
    def test_neutral_names_no_command(self, tmp_path, scope, bypassable):
        path = _tampered_path(tmp_path)
        text = privacy_remedy("neutral", path=path, bypassable=bypassable)

        for foreign in ("mm ", "mem_", "force_unsafe", "`", str(path.parent)):
            assert foreign not in text, foreign
        assert ("bypassed" in text) is bypassable

    @pytest.mark.parametrize("surface", ["cli", "shell", "mcp", "web", "neutral"])
    @pytest.mark.parametrize(("scope", "bypassable"), _TIERS)
    def test_the_refusal_ends_with_the_remedy(self, tmp_path, surface, scope, bypassable):
        path = _tampered_path(tmp_path)
        exc = PrivacyRejection(path=path, hit_count=1, scope=scope, decision="blocked")

        for outcome in ("not_written", "rolled_back", "not_rolled_back", "superseded", "appended"):
            detail = exc.mutation_detail("op", outcome=outcome, surface=surface)
            assert detail.endswith(" " + privacy_remedy(surface, path=path, bypassable=bypassable))


class TestThePathSurvivesTheTrip:
    """A command naming the file has to name it when pasted, and print safely."""

    @pytest.mark.parametrize("parent", ["with space", "dollar$HOME", "quote'd"])
    def test_cli_and_mcp_round_trip_a_printable_path(self, tmp_path, parent):
        path = _tampered_path(tmp_path, parent)

        for argv in _mm_commands(privacy_remedy("cli", path=path, bypassable=True)):
            assert argv[-1] == str(path)
        for call in _mcp_calls(privacy_remedy("mcp", path=path, bypassable=True)):
            assert ast.literal_eval(call.keywords[0].value) == str(path)

    def test_a_terminal_control_in_a_parent_dir_is_never_printed_raw(self, tmp_path):
        path = _tampered_path(tmp_path, "esc\x1b]0;injected\x07")

        cli_text = privacy_remedy("cli", path=path, bypassable=True)
        mcp_text = privacy_remedy("mcp", path=path, bypassable=True)

        assert all(ch.isprintable() for ch in cli_text + mcp_text)
        assert f"`mm index {_hint_quote(path)}`" in cli_text
        (call,) = _mcp_calls(mcp_text)
        assert ast.literal_eval(call.keywords[0].value) == str(path)

    def test_a_windows_path_keeps_its_backslashes(self):
        path = PureWindowsPath(r"C:\Users\Ada Lovelace\memories\notes.md")

        (argv, _) = _mm_commands(privacy_remedy("cli", path=path, bypassable=True))  # type: ignore[arg-type]
        assert argv[-1] == str(path)
        (call,) = _mcp_calls(privacy_remedy("mcp", path=path, bypassable=False))  # type: ignore[arg-type]
        assert ast.literal_eval(call.keywords[0].value) == str(path)


# ── every renderer passes the surface of the code that calls it ──────────────

_SRC = Path(memtomem.__file__).parent
_RENDERERS = {"mutation_detail", "preflight_refusal", "privacy_remedy"}
# The surface a package's callers speak to, unless named below.
_PACKAGE_SURFACE = {"cli": "cli", "server": "mcp", "web": "web"}
# Whole modules that speak to another surface than their package's.
_MODULE_SURFACE = {
    "cli/shell.py": "shell",
    # ``MemtomemStore`` can run on an isolated config, where ``mm index`` would
    # index a different database and never lift this store's hold.
    "integrations/langgraph.py": "neutral",
}
# Functions that take ``surface`` and hand it on unchanged.
_FORWARDERS = {
    ("indexing/engine.py", "mutation_detail"),
    ("tools/memory_mutation.py", "preflight_refusal"),
}
# Functions that store a refusal as a formation candidate's reason, which
# ``mm review`` and ``mem_candidate_list`` both print: that render is neutral.
_PERSISTERS = {
    ("server/tools/formation.py", "mem_candidate_review"),
    ("cli/review_cmd.py", "_decide"),
}


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _surface_node(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "surface":
            return kw.value
    if name == "privacy_remedy" and call.args:
        return call.args[0]
    return None


_Site = tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]


def _sites_in(rel: str, tree: ast.AST) -> list[_Site]:
    sites = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        nested = {
            id(n)
            for inner in ast.walk(fn)
            if inner is not fn and isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
            for n in ast.walk(inner)
        }
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and id(node) not in nested
                and _callee(node) in _RENDERERS
            ):
                sites.append((rel, fn, node))
    return sites


def _render_sites() -> list[_Site]:
    sites = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        sites += _sites_in(rel, ast.parse(py.read_text(encoding="utf-8")))
    return sites


def _persisted_names(fn: ast.AST) -> set[str]:
    """Names handed as ``reason=`` to a candidate quarantine, which stores them."""
    return {
        kw.value.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and (_callee(node) or "").endswith("_write_uncertain")
        for kw in node.keywords
        if kw.arg == "reason" and isinstance(kw.value, ast.Name)
    }


def _persisted(rel: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call) -> bool:
    return (rel, fn.name) in _PERSISTERS and bool(_assigned_to(fn, call) & _persisted_names(fn))


def _assigned_to(fn: ast.AST, call: ast.Call) -> set[str]:
    return {
        t.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign) and node.value is call
        for t in node.targets
        if isinstance(t, ast.Name)
    }


def _expected_surface(
    rel: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call
) -> str | None:
    if rel in _MODULE_SURFACE:
        surface = _MODULE_SURFACE[rel]
    else:
        surface = _PACKAGE_SURFACE.get(rel.split("/", 1)[0])
    if surface is not None and _persisted(rel, fn, call):
        return "neutral"
    return surface


def _surface_problems(sites: list[_Site]) -> list[str]:
    problems = []
    for rel, fn, call in sites:
        name = _callee(call)
        where = f"{rel}:{call.lineno} ({fn.name} -> {name})"
        node = _surface_node(call, name or "")
        if node is None:
            problems.append(f"{where}: no surface")
        elif (rel, fn.name) in _FORWARDERS:
            if not (isinstance(node, ast.Name) and node.id == "surface"):
                problems.append(f"{where}: a forwarder must pass its own `surface`")
        elif not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            problems.append(f"{where}: surface must be a literal outside a forwarder")
        else:
            expected = _expected_surface(rel, fn, call)
            if expected is None:
                problems.append(f"{where}: no surface is mapped for this module")
            elif node.value != expected:
                problems.append(f"{where}: {node.value!r}, expected {expected!r}")
    return problems


class TestEveryCallerPassesItsOwnSurface:
    def test_every_render_site_passes_the_callers_surface(self):
        assert _surface_problems(_render_sites()) == []

    @pytest.mark.parametrize(
        ("rel", "source"),
        [
            # A ``reason=`` that stores nothing is not an exemption, even in an
            # approve path.
            (
                "cli/review_cmd.py",
                "def _decide(exc):\n"
                "    message = exc.mutation_detail('x', outcome='appended', surface='neutral')\n"
                "    return dict(reason=message)\n",
            ),
            # Nor is a quarantine outside the two approve paths.
            (
                "server/tools/scratch.py",
                "async def f(exc, s):\n"
                "    r = exc.mutation_detail('x', outcome='appended', surface='neutral')\n"
                "    await s.mark_memory_candidate_write_uncertain('c', reason=r)\n",
            ),
            # An approve path's returned render keeps the caller's surface.
            (
                "cli/review_cmd.py",
                "async def _decide(exc, s):\n"
                "    r = exc.mutation_detail('x', outcome='appended', surface='neutral')\n"
                "    d = exc.mutation_detail('x', outcome='appended', surface='neutral')\n"
                "    await s.mark_memory_candidate_write_uncertain('c', reason=r)\n"
                "    return d\n",
            ),
            # A module no surface is mapped for has no right answer.
            (
                "storage/anything.py",
                "async def _decide(exc, s):\n"
                "    r = exc.mutation_detail('x', outcome='appended', surface='neutral')\n"
                "    await s.mark_memory_candidate_write_uncertain('c', reason=r)\n",
            ),
            ("tools/helper.py", "def f(exc, surface):\n    return privacy_remedy(surface)\n"),
            (
                "cli/memory.py",
                "def f(exc):\n    return exc.mutation_detail('x', outcome='appended')\n",
            ),
        ],
        ids=[
            "unstored-reason",
            "other-quarantine",
            "returned-render",
            "unmapped",
            "forward",
            "none",
        ],
    )
    def test_the_sweep_rejects(self, rel, source):
        assert len(_surface_problems(_sites_in(rel, ast.parse(source)))) == 1

    def test_the_sweep_sees_every_surface(self):
        rels = {rel for rel, _, _ in _render_sites()}
        # A lower bound, so a renamed renderer cannot leave the sweep vacuous.
        assert {
            "cli/memory.py",
            "cli/shell.py",
            "cli/review_cmd.py",
            "server/tools/memory_crud.py",
            "server/tools/formation.py",
            "server/tools/url_index.py",
            "web/routes/_errors.py",
            "integrations/langgraph.py",
        } <= rels

    @pytest.mark.parametrize(("rel", "func"), sorted(_FORWARDERS))
    def test_forwarders_require_a_surface(self, rel, func):
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        (fn,) = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == func
        ]
        index = [a.arg for a in fn.args.kwonlyargs].index("surface")
        assert fn.args.kw_defaults[index] is None, "no default: a missed caller must fail"

    def test_the_persisted_rule_is_live(self):
        """Both approve paths store a neutral render next to the one they return."""
        neutral = {
            rel
            for rel, fn, call in _render_sites()
            if _assigned_to(fn, call) & _persisted_names(fn)
        }
        assert {"server/tools/formation.py", "cli/review_cmd.py"} <= neutral


# ── end to end, one per surface ──────────────────────────────────────────────


async def _indexed_then_tampered(comp, mem_dir) -> Path:
    target = mem_dir / "notes.md"
    target.write_text(_CLEAN, encoding="utf-8")
    await comp.index_engine.index_file(target)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(_SECRET_SECTION)
    return target


@pytest.fixture
def cli_comp(bm25_only_components, monkeypatch, tmp_path):
    from helpers import set_home

    comp, mem_dir = bm25_only_components
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    set_home(monkeypatch, home)

    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    monkeypatch.setattr(
        "memtomem.runtime.project_context._resolve_project_context_root", lambda comp: None
    )
    return comp, mem_dir


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_mm_add_names_mm_index(self, cli_comp):
        from memtomem.cli.memory import _add

        comp, mem_dir = cli_comp
        target = await _indexed_then_tampered(comp, mem_dir)

        with pytest.raises(click.ClickException) as ei:
            await _add("second", None, [], "notes.md")

        message = ei.value.message
        assert "refused" in message and "mem_index" not in message
        commands = _mm_commands(message)
        assert len(commands) == 2
        for argv in commands:
            _resolve_mm(argv)
            assert Path(argv[-1]).resolve() == target.resolve()

    @pytest.mark.asyncio
    async def test_mem_add_names_mem_index_on_the_file(self, bm25_only_components):
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)

        result = await mem_add(  # type: ignore[arg-type]
            content="gamma entry", file=str(target), ctx=StubCtx(AppContext.from_components(comp))
        )

        assert "refused" in result
        (call,) = _mcp_calls(result)
        assert Path(ast.literal_eval(call.keywords[0].value)).resolve() == target.resolve()
        assert "run mem_index" not in result

    @pytest.mark.parametrize(
        ("restore", "phrase"),
        [
            ("failed", "the rollback failed"),
            ("source_removed", "another process removed or replaced the file"),
            ("source_replaced", "another process removed or replaced the file"),
        ],
    )
    @pytest.mark.asyncio
    async def test_mem_edit_after_an_unrestored_refusal(
        self, bm25_only_components, monkeypatch, restore, phrase
    ):
        """The MCP edit words the outcome as its web twin does, remedy included."""
        from memtomem.server.tools import memory_crud
        from memtomem.tools.memory_writer import RestoreOutcome

        comp, mem_dir = bm25_only_components
        target = mem_dir / "notes.md"
        target.write_text(_CLEAN, encoding="utf-8")
        await comp.index_engine.index_file(target)
        chunks = await comp.storage.list_chunks_by_source(target, limit=None)
        chunk_id = next(str(c.id) for c in chunks if "alpha" in c.content)
        refusal = PrivacyRejection(path=target, hit_count=1, scope="user", decision="blocked")
        refusal.held = True
        monkeypatch.setattr(comp.index_engine, "index_file", AsyncMock(side_effect=refusal))
        monkeypatch.setattr(
            memory_crud, "restore_pre_image_quietly", lambda *_: RestoreOutcome[restore]
        )

        result = await memory_crud.mem_edit(  # type: ignore[arg-type]
            chunk_id=chunk_id,
            new_content="alpha entry, revised",
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result and phrase in result
        assert "redaction_blocked" not in result, "the exception text is not the message"
        assert "held until the file indexes cleanly" in result
        assert result.endswith(privacy_remedy("mcp", path=target, bypassable=True))

    @pytest.mark.asyncio
    async def test_mem_fetch_names_the_saved_file(self, bm25_only_components, monkeypatch):
        from memtomem.server.tools.url_index import mem_fetch

        comp, mem_dir = bm25_only_components
        saved = mem_dir / "_fetched" / "page.md"

        async def fake_fetch(url, output_dir, **_kw):
            saved.parent.mkdir(parents=True, exist_ok=True)
            text = f"# Page\n\naws {_KEY}\n"
            saved.write_text(text, encoding="utf-8")
            return saved, text

        monkeypatch.setattr("memtomem.indexing.url_fetcher.fetch_url_with_text", fake_fetch)

        result = await mem_fetch(  # type: ignore[arg-type]
            url="https://example.com/page", ctx=StubCtx(AppContext.from_components(comp))
        )

        assert "NOT indexed" in result
        (call,) = _mcp_calls(result)
        assert Path(ast.literal_eval(call.keywords[0].value)).resolve() == saved.resolve()
        ((*_, operand),) = _mm_commands(result)
        assert Path(operand).resolve() == saved.resolve()

    @pytest.mark.asyncio
    async def test_mcp_approve_stores_a_reason_any_surface_can_read(self, monkeypatch):
        from memtomem.server.tools.formation import mem_candidate_review

        refusal = PrivacyRejection(
            path=Path("/m/2026-10-19.md"), hit_count=1, scope="user", decision="blocked"
        )
        refusal.mutation_outcome = "appended"
        storage = SimpleNamespace(
            get_memory_candidate=AsyncMock(
                return_value={
                    "id": "candidate-1",
                    "status": "pending",
                    "destination": "memory",
                    "content": "Decision: persisted once",
                    "kind": "decision",
                }
            ),
            claim_memory_candidate=AsyncMock(return_value={"status": "writing"}),
            release_memory_candidate=AsyncMock(),
            mark_claimed_candidate_write_uncertain=AsyncMock(return_value=True),
            mark_memory_candidate_write_uncertain=AsyncMock(return_value=True),
        )
        app = SimpleNamespace(storage=storage, ensure_initialized=AsyncMock())
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
        monkeypatch.setattr(
            "memtomem.server.tools.memory_crud._mem_add_core", AsyncMock(side_effect=refusal)
        )

        result = json.loads(
            await mem_candidate_review("candidate-1", "approve", reviewer="alice", ctx=ctx)
        )

        assert "mem_index(path=" in result["reason"]
        stored = storage.mark_claimed_candidate_write_uncertain.await_args.kwargs["reason"]
        assert "refused" in stored
        for foreign in ("mm ", "mem_", "`"):
            assert foreign not in stored, foreign

    @pytest.mark.asyncio
    async def test_cli_approve_stores_a_reason_any_surface_can_read(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.cli.review_cmd import _decide

        comp, _ = bm25_only_components
        storage = SimpleNamespace(
            get_memory_candidate=AsyncMock(
                return_value={
                    "id": "candidate-1",
                    "status": "pending",
                    "destination": "memory",
                    "content": "Decision: persisted once",
                    "kind": "decision",
                }
            ),
            claim_memory_candidate=AsyncMock(return_value={"status": "writing"}),
            release_memory_candidate=AsyncMock(),
            mark_claimed_candidate_write_uncertain=AsyncMock(return_value=True),
            mark_memory_candidate_write_uncertain=AsyncMock(return_value=True),
        )

        async def refuse(path, **_kw):
            raise PrivacyRejection(path=path, hit_count=1, scope="user", decision="blocked")

        monkeypatch.setattr(comp.index_engine, "index_file", refuse)
        fake_comp = SimpleNamespace(
            storage=storage, config=comp.config, index_engine=comp.index_engine
        )

        @asynccontextmanager
        async def fake_components():
            yield fake_comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)

        with pytest.raises(click.ClickException) as ei:
            await _decide("candidate-1", "approved", "alice", "")

        assert "`mm index " in ei.value.message
        stored = storage.mark_claimed_candidate_write_uncertain.await_args.kwargs["reason"]
        assert "refused" in stored
        for foreign in ("mm ", "mem_", "`"):
            assert foreign not in stored, foreign

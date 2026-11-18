"""The write guard also scans its input with JSON-style escapes resolved (#3300).

A JSON artifact spells ``"`` as ``\\"``, a newline as ``\\n`` and any character
as ``\\uXXXX``. Whoever reads the artifact decodes it, so the guard scans the
text plus up to two views with those escapes resolved.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from memtomem import privacy
from memtomem.privacy import VIEW_SEPARATOR, enforce_write_guard, escape_views

PEM = "BEGIN\nRSA\nPRIVATE\nKEY"
QUOTED = 'db config: {"password": "synthetic-example"}'

# Each passes a raw scan and is caught once the escapes are resolved.
HIDDEN = {
    "quoted label in a JSON string": json.dumps({"text": QUOTED}),
    "PEM in a JSON string": json.dumps({"text": PEM}),
    "escaped key": '{"api_\\u006bey":"synthetic-example"}',
    "duplicate key keeps the PEM": '{"x":' + json.dumps(PEM) + ',"x":"clean"}',
    "duplicate key with an escaped key": '{"api_\\u006bey":"synthetic-example","api_key":null}',
    "deep nesting": "[" * 5000 + json.dumps(PEM) + "]" * 5000,
    "leading BOM": "\ufeff" + json.dumps({"t": PEM}),
    "JSON after a header": "# fetched\n\n" + json.dumps({"t": PEM}),
    "shell string inside JSON": json.dumps(
        {"command": "echo " + json.dumps(json.dumps({"api_key": "synthetic-example"}))}
    ),
    "settings hook": json.dumps(
        {"PreToolUse": [{"hooks": [{"command": "echo '" + QUOTED + "'"}]}]}
    ),
}

# Each passes both ways.
CLEAN = {
    "adjacent list strings": json.dumps(["password", "= synthetic-example"]),
    "Windows path": r"Install to C:\new\tools\bin and run C:\temp\run.bat",
    "regex source": r"use \d+ and \s* and \bword\b in the pattern",
    "lone surrogate": '{"t": "\\ud800 abc"}',
    "Markdown": "# Notes\n\nNothing to see.",
}


@pytest.mark.parametrize("text", HIDDEN.values(), ids=HIDDEN.keys())
def test_hidden_secrets_are_found_in_a_view(text):
    assert not privacy.scan(text)
    assert privacy.scan_with_escape_views(text)


@pytest.mark.parametrize("text", CLEAN.values(), ids=CLEAN.keys())
def test_clean_text_stays_clean(text):
    assert not privacy.scan_with_escape_views(text)


def test_text_without_a_backslash_has_no_view():
    assert escape_views('{"a": "b"}') == ['{"a": "b"}']


@pytest.mark.parametrize(
    ("text", "first_view"),
    [
        ("a\\\\nb", "a\\nb"),  # an escaped backslash, then a literal n
        ("\\u0061\\ud83d\\ude00", "a\U0001f600"),  # ordinary escape, then a pair
        ("\\ud83d\\ude00\\ud83d\\ude00", "\U0001f600\U0001f600"),
        ("\\ud800\\u0041", "\\ud800A"),  # a high surrogate without its low half
        ("\\udc00", "\\udc00"),
        ("\\q and \\x41", "\\q and \\x41"),  # not JSON escapes
    ],
)
def test_escape_resolution(text, first_view):
    views = escape_views(text)
    resolved = views[1] if len(views) > 1 else views[0]
    assert resolved == first_view
    assert all("\ud800" not in v and "\udc00" not in v for v in views)


def test_at_most_two_levels_are_resolved():
    three = json.dumps(json.dumps(json.dumps(PEM)))
    assert len(escape_views(three)) == 3
    assert not privacy.scan_with_escape_views(three)


@pytest.mark.parametrize(
    "text",
    [
        r"C:\docs\password\t=notes.txt",
        r'pattern = r"password\t="',
        json.dumps({"pattern": "password\\t="}),
    ],
)
def test_a_resolved_tab_can_complete_a_label(text):
    # Declared: resolving \t turns these into "password<TAB>=", which the
    # label rule refuses. Fail-closed, and named in the CHANGELOG.
    assert not privacy.scan(text)
    assert privacy.scan_with_escape_views(text)


@pytest.mark.parametrize(
    "text",
    ['x: "\\x42EGIN\\nRSA\\nPRIVATE\\nKEY"', 'x = "\\U00000042EGIN\\nRSA\\nPRIVATE\\nKEY"'],
    ids=["YAML \\x", "TOML \\U"],
)
def test_other_escape_syntaxes_are_not_resolved(text):
    # Only JSON-style escapes are resolved; YAML's \x and TOML's \U stay hidden.
    assert not privacy.scan_with_escape_views(text)


def test_spans_point_into_the_joined_views():
    text = json.dumps({"text": PEM})
    joined = VIEW_SEPARATOR.join(escape_views(text))
    (hit,) = privacy.scan_with_escape_views(text)
    assert hit.span[0] >= len(text)
    assert joined[hit.span[0] : hit.span[1]].startswith("BEGIN")


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


@pytest.mark.parametrize("scope", ["user", "project_local", "project_shared"])
def test_guard_blocks_a_hidden_secret(scope):
    result = enforce_write_guard(HIDDEN["settings hook"], surface="t", scope=scope)
    assert result.decision == "blocked"
    assert privacy.snapshot()["by_tool"]["t"]["blocked"] == 1


def test_force_unsafe_keeps_its_scope_rule(caplog):
    text = HIDDEN["PEM in a JSON string"]
    with caplog.at_level(logging.WARNING):
        private = enforce_write_guard(text, surface="t", scope="user", force_unsafe=True)
    shared = enforce_write_guard(text, surface="t", scope="project_shared", force_unsafe=True)
    assert private.decision == "bypassed"
    assert shared.decision == "blocked_project_shared"
    assert f"content_chars={len(text)}," in caplog.text
    assert "RSA" not in caplog.text


def test_declared_exemption_does_not_cover_a_quoted_label_in_a_view():
    result = enforce_write_guard(
        HIDDEN["quoted label in a JSON string"],
        surface="t",
        scope="user",
        declared_exemption=privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS,
    )
    assert result.decision == "blocked"


def test_audit_value_hidden_by_an_escape_is_redacted():
    value = "\\u0073k-" + "a" * 30
    assert not privacy.scan(value)
    assert privacy.sanitize_audit_value(value) == "<redacted: secret-shape>"


def _dense_escapes(rows: int) -> str:
    # Two levels of escapes (so both views are built), a non-BMP pair in every
    # row, and no rule hit.
    inner = [{f"k{i}": f'say "hi" \U0001f600 and\nwave {i}'} for i in range(rows)]
    return json.dumps({"payload": json.dumps(inner)})


def _best(fn, text: str) -> float:
    times = []
    for _ in range(3):
        start = time.perf_counter()
        fn(text)
        times.append(time.perf_counter() - start)
    return min(times)


def _guard(text: str) -> None:
    assert enforce_write_guard(text, surface="t", scope="user", record_outcome=False).hits == []


def test_guard_cost_is_a_bounded_multiple_of_one_scan():
    text = _dense_escapes(5_000)
    assert len(escape_views(text)) == 3
    ratio = _best(_guard, text) / _best(privacy.scan, text)
    # The content plus two views of about its length: about three scans.
    assert ratio < 6, ratio


def test_guard_cost_grows_linearly():
    small, large = _dense_escapes(2_000), _dense_escapes(8_000)
    ratio = _best(_guard, large) / _best(_guard, small)
    size = len(large) / len(small)
    assert ratio < size * 2, (ratio, size)


@pytest.mark.asyncio
async def test_mem_add_refuses_a_json_note_with_a_hidden_secret(bm25_only_components):
    from memtomem.server.context import AppContext
    from memtomem.server.tools.memory_crud import mem_add

    from helpers import StubCtx

    comp, mem_dir = bm25_only_components
    target = mem_dir / "note.md"
    result = await mem_add(  # type: ignore[arg-type]
        content=HIDDEN["PEM in a JSON string"],
        file=str(target),
        ctx=StubCtx(AppContext.from_components(comp)),
    )
    assert "privacy pattern" in result
    assert not target.exists()


@pytest.mark.asyncio
async def test_indexing_refuses_a_json_file_with_a_hidden_secret(components, memory_dir):
    from unittest.mock import AsyncMock

    from memtomem.indexing.engine import PrivacyRejection

    engine = components.index_engine
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts])
    embedder.dimension = 1024
    engine._embedder = embedder
    path = memory_dir / "config.json"
    path.write_text(HIDDEN["PEM in a JSON string"], encoding="utf-8")
    with pytest.raises(PrivacyRejection):
        await engine.index_file(path)
    assert not await components.storage.bm25_search("PRIVATE")


@pytest.mark.asyncio
async def test_mem_add_keeps_escaped_clean_content_verbatim(bm25_only_components):
    from memtomem.server.context import AppContext
    from memtomem.server.tools.memory_crud import mem_add

    from helpers import StubCtx

    comp, mem_dir = bm25_only_components
    target = mem_dir / "note.md"
    content = json.dumps({"text": 'say "hi"\nthen leave'})
    result = await mem_add(  # type: ignore[arg-type]
        content=content, file=str(target), ctx=StubCtx(AppContext.from_components(comp))
    )
    assert "Error" not in result
    assert content in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_forced_private_write_stores_the_original_bytes(bm25_only_components, caplog):
    from memtomem.server.context import AppContext
    from memtomem.server.tools.memory_crud import mem_add

    from helpers import StubCtx

    comp, mem_dir = bm25_only_components
    target = mem_dir / "note.md"
    content = HIDDEN["PEM in a JSON string"]
    with caplog.at_level(logging.DEBUG):
        result = await mem_add(  # type: ignore[arg-type]
            content=content,
            file=str(target),
            force_unsafe=True,
            ctx=StubCtx(AppContext.from_components(comp)),
        )
    stored = target.read_text(encoding="utf-8")
    assert content in stored  # escaped as written
    assert PEM not in stored  # no resolved view was written
    assert PEM not in result
    assert PEM not in caplog.text

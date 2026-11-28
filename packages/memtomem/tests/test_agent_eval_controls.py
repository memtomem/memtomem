"""Offline tests for the agent-eval control cases and tools/agent-eval/check_controls.py (#3650).

The control cases in ``tools/agent-eval/plugin/controls/`` exercise two clauses of the save
cases' ``fact`` rubric. A control's FAIL counts only when ``check_controls.py`` finds that the
run made the prescribed calls, so these tests pin what that script accepts and refuses. The
record shapes follow kept traces from Claude Code 2.1.331. Nothing here calls a model.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

import pytest

from memtomem.server.tools.memory_crud import _REPLAY_MARKER
from memtomem.templates import render_template

_EVAL = Path(__file__).resolve().parents[3] / "tools" / "agent-eval"
_CONTROLS = _EVAL / "plugin" / "controls"
_REGRESSION_FACT = _EVAL / "plugin" / "evals" / "save-dashboard-workspace" / "graders" / "fact.md"
_NOTE = "/r/home/cwd/mm-notes/2026-11-26.md"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location(
        "agent_eval_check_controls", _EVAL / "check_controls.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _use(*calls: tuple[str, str, dict]) -> str:
    content = [
        {"type": "tool_use", "id": cid, "name": name, "input": args, "caller": {"type": "direct"}}
        for cid, name, args in calls
    ]
    return json.dumps({"type": "assistant", "message": {"content": content}})


def _result(cid: str, text: str, *, type_first: bool = False) -> str:
    payload = json.dumps({"result": text})
    block = (
        {"type": "tool_result", "tool_use_id": cid, "content": payload}
        if type_first
        else {"tool_use_id": cid, "type": "tool_result", "content": payload}
    )
    return json.dumps({"type": "user", "message": {"content": [block]}})


def _text(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _added(path: str = _NOTE) -> str:
    return f"Memory added to {path}\n- Namespace: default\n- Chunks indexed: 1\n- File: {path}"


def _replay_trace(
    checker, *, second=None, marker=True, type_first=False, note: str = _NOTE
) -> list[str]:
    first = {"content": checker.WRONG, "idempotency_key": checker.REPLAY_KEY}
    second = second or {"content": checker.FACT, "idempotency_key": checker.REPLAY_KEY}
    replay = _added(note) + (_REPLAY_MARKER if marker else "")
    return [
        json.dumps({"type": "system", "subtype": "init"}),
        _use(("t1", checker.MEM_ADD, first)),
        _result("t1", _added(note), type_first=type_first),
        _use(("t2", checker.MEM_ADD, second)),
        _result("t2", replay, type_first=type_first),
        _text("DONE"),
    ]


def _replay_notes(checker) -> dict[str, str]:
    return {_NOTE: f"\n## Entry x\n\n{checker.WRONG}\n"}


def _template_trace(checker, args: dict, *, extra: list[str] = ()) -> list[str]:
    return [
        _use(
            ("t1", checker.MEM_ADD, args), *[("t9", checker.MEM_ADD, {"content": e}) for e in extra]
        ),
        _result("t1", _added()),
        *([_result("t9", _added())] if extra else []),
        _text("DONE"),
    ]


def _extra_arg(checker) -> dict:
    return {**checker.TEMPLATE_ARGS, "content": "Dashboard location", "context": checker.FACT}


def _json_key(checker) -> dict:
    return {**checker.TEMPLATE_ARGS, "content": checker.JSON_KEY_LITERAL}


_EXTRA_NOTE = {_NOTE: "## Decision: Analytics dashboards\n\n**Options**: Dashboard location\n"}
_JSON_NOTE = {
    _NOTE: "## Decision: Analytics dashboards\n\n**Chosen**: Keep the dashboards in Looker\n"
}


def test_every_control_grades_with_the_regression_rubric_byte_for_byte():
    facts = sorted(_CONTROLS.glob("*/graders/fact.md"))
    assert len(facts) == 3
    for fact in facts:
        assert fact.read_bytes() == _REGRESSION_FACT.read_bytes(), fact
        assert [p.name for p in fact.parent.iterdir()] == ["fact.md"], fact


def test_checker_marker_is_the_servers(checker):
    assert _REPLAY_MARKER.strip().startswith(checker.REPLAY_MARKER)


def test_prompts_prescribe_what_the_checker_expects(checker):
    replay = (_CONTROLS / "control-replay-wrong-first" / "prompt.md").read_text()
    assert replay.count(f"`{checker.REPLAY_KEY}`") == 2
    assert f"`{checker.WRONG}`" in replay and f"`{checker.FACT}`" in replay
    extra = (_CONTROLS / "control-template-extra-arg" / "prompt.md").read_text()
    for key, value in _extra_arg(checker).items():
        assert f"- `{key}`: `{value}`" in extra


def test_json_key_content_is_dropped_by_the_renderer(checker):
    prompt = (_CONTROLS / "control-template-json-key" / "prompt.md").read_text()
    (block,) = re.findall(r"```json\n(.*?)\n```", prompt, re.S)
    assert block == checker.JSON_KEY_LITERAL
    assert json.loads(block) == checker.JSON_KEY_CONTENT
    rendered = render_template("decision", block, title="Analytics dashboards")
    assert checker.FACT_TOKEN not in rendered
    assert "**Chosen**: Keep the dashboards in Looker" in rendered


def _assess(checker, case: str, lines: list[str], notes: dict[str, str]):
    """Assess a run whose judge saw the whole trace, as all 9 live control runs did."""
    return checker.assess(case, lines, notes, "\n".join(lines))


@pytest.mark.parametrize("type_first", [False, True])
def test_replay_as_prescribed_is_eligible(checker, type_first):
    lines = _replay_trace(checker, type_first=type_first)
    verdict = _assess(checker, "control-replay-wrong-first", lines, _replay_notes(checker))
    assert (verdict.eligible, verdict.reason, verdict.detail) == (True, "eligible", "evidence")


def test_a_deciding_record_missing_from_the_evidence_is_judge_window(checker):
    lines = _replay_trace(checker)
    cut = "\n".join(lines[:4])
    verdict = checker.assess("control-replay-wrong-first", lines, _replay_notes(checker), cut)
    assert verdict.reason == "judge-window"


def test_a_run_without_evidence_is_not_counted(checker):
    lines = _replay_trace(checker)
    verdict = checker.assess("control-replay-wrong-first", lines, _replay_notes(checker), None)
    assert (verdict.eligible, verdict.reason) == (False, "judge-visibility-unverified")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("same-wrong-twice", "wrong-args"),
        ("marker-in-text-only", "call-rejected"),
        ("second-write-elsewhere", "target-mismatch"),
        ("namespace-day-file", "target-mismatch"),
        ("other-tool", "extra-calls"),
        ("third-mem-add", "extra-calls"),
        ("result-of-another-call", "call-rejected"),
        ("second-call-shares-first-results-record", "wrong-order"),
    ],
)
def test_replay_off_the_prescribed_path_is_not_reached(checker, change, reason):
    notes = _replay_notes(checker)
    lines = _replay_trace(checker)
    if change == "same-wrong-twice":
        wrong = {"content": checker.WRONG, "idempotency_key": checker.REPLAY_KEY}
        lines = _replay_trace(checker, second=wrong)
    elif change == "marker-in-text-only":
        lines = _replay_trace(checker, marker=False)
        lines.insert(-1, _text(f"That was an {checker.REPLAY_MARKER}."))
    elif change in ("second-write-elsewhere", "namespace-day-file"):
        other = "/r/home/cwd/mm-notes/custom.md"
        if change == "namespace-day-file":
            other = "/r/home/cwd/mm-notes/2026-11-26.shared.md"
        notes = {**notes, other: f"{checker.FACT}\n"}
    elif change == "other-tool":
        lines.insert(1, _use(("s1", "mcp__plugin_memtomem-agent-eval_memtomem__mem_search", {})))
    elif change == "third-mem-add":
        lines[-1:-1] = [
            _use(("t3", checker.MEM_ADD, {"content": checker.FACT})),
            _result("t3", "x"),
        ]
    elif change == "result-of-another-call":
        # Call 2's own result is missing; the next result belongs to a call the trace never shows.
        lines[4] = _result("t7", _added() + _REPLAY_MARKER)
    else:
        first_result = json.loads(lines[2])["message"]["content"]
        second_call = json.loads(lines[3])["message"]["content"]
        merged = {"type": "user", "message": {"content": second_call + first_result}}
        lines[2:4] = [json.dumps(merged)]
    verdict = _assess(checker, "control-replay-wrong-first", lines, notes)
    assert (verdict.eligible, verdict.reason) == (False, reason)


def test_template_controls_as_prescribed_are_eligible(checker):
    extra = _template_trace(checker, _extra_arg(checker))
    assert _assess(checker, "control-template-extra-arg", extra, _EXTRA_NOTE).eligible
    key = _template_trace(checker, _json_key(checker))
    assert _assess(checker, "control-template-json-key", key, _JSON_NOTE).eligible


def test_extra_arg_missing_from_the_trace_is_its_own_reason(checker):
    args = _extra_arg(checker)
    del args["context"]
    lines = _template_trace(checker, args)
    verdict = _assess(checker, "control-template-extra-arg", lines, _EXTRA_NOTE)
    assert verdict.reason == "arg-absent-from-trace"


def test_a_sibling_call_in_the_same_record_is_not_reached(checker):
    lines = _template_trace(checker, _extra_arg(checker), extra=[checker.FACT])
    verdict = _assess(checker, "control-template-extra-arg", lines, _EXTRA_NOTE)
    assert verdict.reason == "extra-calls"


def test_a_note_holding_the_fact_is_not_reached(checker):
    lines = _template_trace(checker, _json_key(checker))
    note = {_NOTE: _JSON_NOTE[_NOTE] + f"**Context**: {checker.FACT}\n"}
    verdict = _assess(checker, "control-template-json-key", lines, note)
    assert verdict.reason == "target-mismatch"


def test_a_json_key_renamed_to_a_rendered_field_is_wrong_args(checker):
    renamed = {"chosen": "Keep the dashboards in Looker", "rationale": checker.FACT}
    args = {**checker.TEMPLATE_ARGS, "content": json.dumps(renamed)}
    lines = _template_trace(checker, args)
    assert _assess(checker, "control-template-json-key", lines, _JSON_NOTE).reason == "wrong-args"


def test_a_duplicate_json_key_is_wrong_args(checker):
    # json.loads keeps the last `chosen`, so a decoded comparison would pass this; the judge sees
    # the fact under `chosen` as well.
    content = (
        f'{{"chosen": "{checker.FACT}", "chosen": "Keep the dashboards in Looker", '
        f'"context": "{checker.FACT}"}}'
    )
    assert json.loads(content) == checker.JSON_KEY_CONTENT
    lines = _template_trace(checker, {**checker.TEMPLATE_ARGS, "content": content})
    assert _assess(checker, "control-template-json-key", lines, _JSON_NOTE).reason == "wrong-args"


def test_read_trace_splits_on_lf_only(checker, tmp_path):
    # Separators JSON leaves raw inside a string, and a bare CR, stay inside their record.
    record = json.dumps({"type": "assistant", "text": "a\u2028b\x85c\rd"}, ensure_ascii=False)
    path = tmp_path / "trace.jsonl"
    path.write_bytes(f"{record}\n\n{record}\n".encode())
    assert checker.read_trace(path) == [record, "", record]


def _report(tmp_path: Path, runs: list[dict]) -> Path:
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps({"cases": [{"name": "control-replay-wrong-first", "arms": {"with": runs}}]})
    )
    return path


def test_main_reports_a_run_without_a_trace_path_as_unread(checker, tmp_path, capsys):
    assert checker.main(["check_controls.py", str(_report(tmp_path, [{}]))]) == 1
    assert capsys.readouterr().out == (
        "control-replay-wrong-first run1: unread, the result has no tracePath\n"
    )


def test_main_leaves_a_run_error_to_the_report(checker, tmp_path, capsys):
    assert checker.main(["check_controls.py", str(_report(tmp_path, [{"error": "timeout"}]))]) == 0
    assert capsys.readouterr().out == "control-replay-wrong-first run1: run-error 'timeout'\n"


def test_main_lets_a_missing_trace_raise(checker, tmp_path):
    run = {"tracePath": str(tmp_path / "gone" / "out" / "trace.jsonl")}
    with pytest.raises(FileNotFoundError):
        checker.main(["check_controls.py", str(_report(tmp_path, [run]))])


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="mode 000 must refuse a listing"
)
def test_main_reports_sealed_notes_and_goes_on(checker, tmp_path, capsys):
    # A space and a quote: the printed chmod command must survive being pasted into a shell.
    root = tmp_path / "e run's"
    (root / "out").mkdir(parents=True)
    trace = root / "out" / "trace.jsonl"
    trace.write_text("\n".join(_replay_trace(checker)) + "\n", newline="\n")
    notes = root / "sealed" / "home" / "cwd" / "mm-notes"
    notes.mkdir(parents=True)
    (notes / "2026-11-26.md").write_text(checker.WRONG)
    report = _report(tmp_path, [{"tracePath": str(trace)}, {"error": "timeout"}])
    (root / "sealed").chmod(0)
    try:
        assert checker.main(["check_controls.py", str(report)]) == 1
    finally:
        (root / "sealed").chmod(0o700)
    out = capsys.readouterr().out.splitlines()
    prefix = "control-replay-wrong-first run1: unread, sealed: "
    assert out[0].startswith(prefix)
    command = shlex.split(out[0].removeprefix(prefix))
    assert command == ["chmod", "700", str(root), str(root / "sealed")]
    assert out[1] == "control-replay-wrong-first run2: run-error 'timeout'"


def test_main_reports_an_eligible_run_from_an_opened_kept_directory(checker, tmp_path, capsys):
    root = tmp_path / "e-run"
    (root / "out").mkdir(parents=True)
    note = root / "home" / "cwd" / "mm-notes" / "2026-11-26.md"
    lines = _replay_trace(checker, note=str(note))
    tool_search = {"query": f"select:{checker.MEM_ADD}", "max_results": 1}
    reference = [{"type": "tool_reference", "tool_name": checker.MEM_ADD}]
    lines[1:1] = [
        _use(("ts", "ToolSearch", tool_search)),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "ts", "content": reference}]
                },
            }
        ),
    ]
    trace = root / "out" / "trace.jsonl"
    # LF as Claude Code writes it: a text-mode write on Windows would turn it into CRLF.
    trace.write_text("\n".join(lines) + "\n", newline="\n")
    sealed_note = root / "sealed" / "home" / "cwd" / "mm-notes" / "2026-11-26.md"
    sealed_note.parent.mkdir(parents=True)
    sealed_note.write_text(f"\n## Entry x\n\n{checker.WRONG}\n")
    fact = {
        "name": "fact",
        "passed": False,
        "judgeVotes": [False, False, False],
        "evidence": "\n".join(lines),
    }
    report = _report(tmp_path, [{"tracePath": str(trace), "graders": [fact]}])
    assert checker.main(["check_controls.py", str(report)]) == 0
    assert capsys.readouterr().out == (
        "control-replay-wrong-first run1: eligible (evidence) fact=FAIL votes=FAIL,FAIL,FAIL\n"
    )

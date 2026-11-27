"""Decide whether each run of a control case in ``plugin/controls/`` took the path it tests.

The control cases (#3650) ask the model for an exact sequence of ``mem_add`` calls, and their only
grader is a copy of the save cases' ``fact`` judge, which must FAIL. A FAIL counts only if the run
did what the control prescribes. ``claude plugin eval`` has no grader that joins a call to its
result, so this script reads the kept trace instead. It parses the records, pairs each call with
its result by ``tool_use_id``, compares the decoded arguments, reads the file the server reported
writing, and checks that the deciding records were in what the judge saw.

Usage, after ``claude plugin eval ... --eval-dir controls --keep-temp --json <file>``::

    python3 -I tools/agent-eval/check_controls.py <file>

The kept directories are sealed (mode 000). Open each one first with the ``chmod`` command that
``claude plugin eval`` prints. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MEM_ADD = "mcp__plugin_memtomem-agent-eval_memtomem__mem_add"
# ToolSearch is how Claude Code loads a deferred tool's schema before the first call.
HARNESS_TOOLS = frozenset({"ToolSearch"})
FACT = "The analytics dashboards now live in the Looker workspace named heron-metrics."
WRONG = "The analytics dashboards now live in the Looker workspace named stork-metrics."
FACT_TOKEN = "heron-metrics"
REPLAY_KEY = "ctl-3650-replay"
# The prefix of memory_crud._REPLAY_MARKER; the offline tests pin the two together.
REPLAY_MARKER = "[idempotent replay]"
SUCCESS_PREFIX = "Memory added to "
HEADING = "## Decision: Analytics dashboards"
TEMPLATE_ARGS = {"template": "decision", "title": "Analytics dashboards"}
JSON_KEY_CONTENT = {"chosen": "Keep the dashboards in Looker", "context": FACT}
# The exact `content` string the json-key prompt prescribes. It is compared as text, because
# decoding would hide a duplicate key: the renderer keeps the last value, the judge reads them all.
JSON_KEY_LITERAL = json.dumps(JSON_KEY_CONTENT)


@dataclass(frozen=True)
class Call:
    id: str
    input: dict
    index: int
    line: str


@dataclass(frozen=True)
class Result:
    text: str
    is_error: bool
    index: int
    line: str


@dataclass(frozen=True)
class Verdict:
    eligible: bool
    reason: str
    detail: str = ""


def _blocks(record: dict) -> list[dict]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    if not isinstance(content, str):
        return ""
    # The server returns structured output: the result string sits in {"result": "..."}.
    try:
        decoded = json.loads(content)
    except ValueError:
        return content
    if isinstance(decoded, dict) and isinstance(decoded.get("result"), str):
        return decoded["result"]
    return content


def parse_trace(lines: list[str]) -> tuple[list[Call], dict[str, list[Result]], list[str]]:
    """Return the mem_add calls, every tool result by id, and the names of other tools called."""
    calls: list[Call] = []
    results: dict[str, list[Result]] = {}
    others: list[str] = []
    for index, line in enumerate(lines):
        record = json.loads(line)
        for block in _blocks(record):
            if block.get("type") == "tool_use":
                if block.get("name") == MEM_ADD:
                    args = block.get("input")
                    calls.append(
                        Call(
                            str(block.get("id")),
                            args if isinstance(args, dict) else {},
                            index,
                            line,
                        )
                    )
                elif block.get("name") not in HARNESS_TOOLS:
                    others.append(str(block.get("name")))
            elif block.get("type") == "tool_result":
                results.setdefault(str(block.get("tool_use_id")), []).append(
                    Result(_result_text(block), block.get("is_error") is True, index, line)
                )
    return calls, results, others


def read_trace(path: Path) -> list[str]:
    """Split a trace into its records on LF only, the way the judge's evidence joins them.

    ``str.splitlines`` also breaks on U+2028, U+0085 and other separators that JSON allows raw
    inside a string, and text-mode reading turns a bare CR into LF. Either would cut a record in
    two. An empty record is left in place, so ``json.loads`` refuses it.
    """
    text = path.read_bytes().decode("utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n") if text else []


def _success_target(result: Result) -> str | None:
    if result.is_error or not result.text.startswith(SUCCESS_PREFIX):
        return None
    return result.text[len(SUCCESS_PREFIX) :].split("\n", 1)[0].strip()


def _check_replay(calls: list[Call], results: dict[str, list[Result]]) -> Verdict | None:
    if len(calls) != 2:
        return Verdict(False, "extra-calls", f"{len(calls)} mem_add calls, 2 prescribed")
    first, second = calls
    if first.input != {"content": WRONG, "idempotency_key": REPLAY_KEY}:
        return Verdict(False, "wrong-args", f"call 1: {first.input!r}")
    if second.input != {"content": FACT, "idempotency_key": REPLAY_KEY}:
        return Verdict(False, "wrong-args", f"call 2: {second.input!r}")
    r1, r2 = results[first.id][0], results[second.id][0]
    if _success_target(r1) is None or REPLAY_MARKER in r1.text:
        return Verdict(False, "call-rejected", f"call 1 result: {r1.text[:120]!r}")
    if r1.index >= second.index:
        return Verdict(False, "wrong-order", "call 2 was made before call 1's result")
    if _success_target(r2) is None or REPLAY_MARKER not in r2.text:
        return Verdict(False, "call-rejected", f"call 2 is not a replay: {r2.text[:120]!r}")
    return None


def _check_template(
    case: str, calls: list[Call], results: dict[str, list[Result]]
) -> Verdict | None:
    if len(calls) != 1:
        return Verdict(False, "extra-calls", f"{len(calls)} mem_add calls, 1 prescribed")
    (call,) = calls
    args = dict(call.input)
    if case == "control-template-extra-arg":
        if "context" not in args:
            return Verdict(False, "arg-absent-from-trace", f"input: {args!r}")
        if args != {**TEMPLATE_ARGS, "content": "Dashboard location", "context": FACT}:
            return Verdict(False, "wrong-args", f"input: {args!r}")
    else:
        if args != {**TEMPLATE_ARGS, "content": JSON_KEY_LITERAL}:
            return Verdict(False, "wrong-args", f"input: {call.input!r}")
    if _success_target(results[call.id][0]) is None:
        return Verdict(False, "call-rejected", results[call.id][0].text[:120])
    return None


def _expected_note(case: str) -> tuple[list[str], list[str]]:
    """Return what the written note must contain and must not contain."""
    if case == "control-replay-wrong-first":
        return [WRONG], [FACT_TOKEN]
    if case == "control-template-extra-arg":
        return [HEADING, "**Options**: Dashboard location"], [FACT_TOKEN]
    return [HEADING, "**Chosen**: Keep the dashboards in Looker"], [FACT_TOKEN]


CASES = ("control-replay-wrong-first", "control-template-extra-arg", "control-template-json-key")


def assess(case: str, lines: list[str], notes: dict[str, str], evidence: str | None) -> Verdict:
    """Judge one run.

    ``notes`` maps every non-empty note file under the run's notes root to its content, keyed by
    the path as the server reports it. ``evidence`` is the judge input from the result JSON, or
    None when the result carries none. Without it the judge's view is not checked, and the run is
    not counted.
    """
    if case not in CASES:
        raise ValueError(f"not a control case: {case}")
    calls, results, others = parse_trace(lines)
    if others:
        return Verdict(False, "extra-calls", f"other tools: {others}")
    for call in calls:
        paired = results.get(call.id, [])
        if len(paired) != 1 or paired[0].index < call.index:
            return Verdict(False, "call-rejected", f"call {call.id} has {len(paired)} results")
    if case == "control-replay-wrong-first":
        verdict = _check_replay(calls, results)
    else:
        verdict = _check_template(case, calls, results)
    if verdict is not None:
        return verdict
    targets = {_success_target(results[c.id][0]) for c in calls}
    if len(targets) != 1 or set(notes) != targets:
        return Verdict(
            False, "target-mismatch", f"reported {sorted(targets)}, written {sorted(notes)}"
        )
    (target,) = targets
    must, must_not = _expected_note(case)
    note = notes[target]
    if any(s not in note for s in must) or any(s in note for s in must_not):
        return Verdict(False, "target-mismatch", f"{target} does not hold the prescribed entry")
    if evidence is None:
        return Verdict(False, "judge-visibility-unverified", "the result carries no evidence")
    deciding = [c.line for c in calls] + [results[c.id][0].line for c in calls]
    if any(line not in evidence for line in deciding):
        return Verdict(False, "judge-window", "a deciding record is not in the judge's evidence")
    return Verdict(True, "eligible", "evidence")


def _raise(error: OSError) -> None:
    raise error


def _visible_notes(root: Path) -> dict[str, str]:
    """Read the run's non-empty note files, keyed by the path the server saw."""
    sealed = root / "sealed"
    base = sealed if sealed.is_dir() else root
    notes_dir = base / "home" / "cwd" / "mm-notes"
    files = []
    # os.walk with an onerror that raises: Path.rglob skips a directory it cannot list, which
    # would turn a sealed run into an empty note map.
    for dirpath, _dirs, names in os.walk(notes_dir, onerror=_raise):
        for name in names:
            path = Path(dirpath) / name
            if name.endswith(".md") and not path.is_symlink() and path.is_file():
                files.append(path)
    out = {}
    for path in sorted(files):
        text = path.read_text(encoding="utf-8")
        if text.strip():
            out[str(root / path.relative_to(base))] = text
    return out


def _fact_grade(run: dict) -> str:
    if run.get("skippedPaidGraders"):
        return "fact=skipped"
    for grader in run.get("graders") or []:
        if grader.get("name") == "fact":
            votes = grader.get("judgeVotes")
            if not votes:
                return f"fact=judge-error ({grader.get('explanation')!r})"
            shown = ",".join("PASS" if v else "FAIL" for v in votes)
            return f"fact={'PASS' if grader.get('passed') else 'FAIL'} votes={shown}"
    return "fact=missing"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    status = 0
    for case in report.get("cases", []):
        name = case["name"]
        for n, run in enumerate(case.get("arms", {}).get("with", []), 1):
            if run.get("error"):
                print(f"{name} run{n}: run-error {run.get('error')!r}")
                continue
            if not run.get("tracePath"):
                print(f"{name} run{n}: unread, the result has no tracePath")
                status = 1
                continue
            trace = Path(run["tracePath"])
            root = trace.parent.parent
            evidence = next(
                (g.get("evidence") for g in run.get("graders") or [] if g.get("name") == "fact"),
                None,
            )
            lines = read_trace(trace)
            try:
                notes = _visible_notes(root)
            except PermissionError:
                print(f"{name} run{n}: unread, sealed: chmod 700 {root} {root / 'sealed'}")
                status = 1
                continue
            except OSError as exc:
                print(f"{name} run{n}: unread notes, {type(exc).__name__}: {exc}")
                status = 1
                continue
            verdict = assess(name, lines, notes, evidence if isinstance(evidence, str) else None)
            state = "eligible" if verdict.eligible else f"not-reached:{verdict.reason}"
            print(f"{name} run{n}: {state} ({verdict.detail}) {_fact_grade(run)}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))

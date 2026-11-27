"""Offline tests for the agent-eval capability cases (issue #3649).

They pin the shape of each case under tools/agent-eval/plugin/evals/cap-* and run every
committed regex grader against lines and trace records that must pass or must fail. The
meaning of a reply is graded by each case's ``llm`` judge, which these tests do not run.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

_PLUGIN = Path(__file__).resolve().parents[3] / "tools" / "agent-eval" / "plugin"
_EVALS = _PLUGIN / "evals"
_SEEDS = _PLUGIN / "seeds"
_READS = {"Read", "Grep", "Glob"}

# case -> {grader: (type, target or focus, match)}; "no_*" graders are checked separately.
_CAPABILITY = {
    "cap-superseded-registry": {
        "answer": ("regex", "last_message", "contains"),
        "retrieved": ("regex", "trace", "contains"),
        "retrieved_old": ("regex", "trace", "contains"),
        "correct": ("llm", None, None),
    },
    "cap-distractor-bucket": {
        # No retrieved_old: the prompt asks only for the current bucket, so a grounded answer
        # from the October note alone must not fail.
        "answer": ("regex", "last_message", "contains"),
        "retrieved": ("regex", "trace", "contains"),
        "correct": ("llm", None, None),
    },
    "cap-multihop-oncall": {
        "answer": ("regex", "last_message", "contains"),
        "retrieved": ("regex", "trace", "contains"),
        "retrieved_hop1": ("regex", "trace", "contains"),
        "correct": ("llm", None, None),
    },
    "cap-nohint-psql": {
        "answer": ("regex", "last_message", "contains"),
        "retrieved": ("regex", "trace", "contains"),
        "correct": ("llm", None, None),
    },
    "cap-nohint-deploy": {
        "answer": ("regex", "last_message", "contains"),
        "retrieved": ("regex", "trace", "contains"),
        "correct": ("llm", None, None),
    },
}


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    head, body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(head), body


def _grader(case: str, name: str) -> dict:
    return _frontmatter(_EVALS / case / "graders" / f"{name}.md")[0]


def _matches(case: str, name: str, text: str) -> bool:
    # The graders run as JavaScript regexes. The patterns stick to syntax both engines read the
    # same way, and no flags are set; the kept-trace controls in the README cover the real engine.
    return re.search(_grader(case, name)["pattern"], text) is not None


def _all_cases() -> list[Path]:
    return sorted(p.parent for p in _EVALS.glob("*/prompt.md"))


def _tags(case: Path) -> set[str]:
    return set(_frontmatter(case / "prompt.md")[0]["tags"])


def test_capability_cases_have_exactly_the_planned_graders():
    # Discovered by tag, so a promoted case (retagged, same directory) drops out of this set.
    found = {p.name for p in _all_cases() if "capability" in _tags(p)}
    assert found == set(_CAPABILITY)
    for case, planned in _CAPABILITY.items():
        names = {p.stem for p in (_EVALS / case / "graders").glob("*.md")}
        assert names == set(planned) | {f"no_{t.lower()}" for t in _READS}, case
        for name, (kind, where, match) in planned.items():
            grader = _grader(case, name)
            assert grader["type"] == kind, (case, name)
            if kind == "regex":
                # The exact key set: an added `flags` (say `i`) would change the live grader.
                assert set(grader) == {"type", "target", "pattern", "match", "weight"}
                assert grader["target"] == where and grader["match"] == match, (case, name)
            else:
                # No focus means the judge reads the final reply only (measured; README).
                assert set(grader) == {"type", "weight"}, (case, name)
        for tool in _READS:
            grader = _grader(case, f"no_{tool.lower()}")
            assert (grader["type"], grader["tool"], grader["min"], grader["max"]) == (
                "tool_used",
                tool,
                0,
                0,
            )


def test_capability_tag_stays_out_of_the_regression_tags():
    for case in _all_cases():
        tags = _tags(case)
        if case.name in _CAPABILITY:
            assert tags == {"capability"}, case.name
        else:
            assert "capability" not in tags, case.name


def _record(result: str, *, kind: str = "tool_result") -> str:
    """One trace line in the shape Claude Code 2.1.331 wrote a mem_search result.

    Compact JSON on one line; the result text appears in ``content``, ``tool_use_result`` and
    ``structuredContent``, as in the kept probe traces.
    """
    if kind == "tool_result":
        content = [{"tool_use_id": "toolu_x", "type": "tool_result", "content": result}]
        text = json.loads(result)["result"]
        record = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "tool_use_result": {"content": result, "structuredContent": {"result": text}},
        }
    else:
        record = {"type": "assistant", "message": {"content": [{"type": "text", "text": result}]}}
    return json.dumps(record, separators=(",", ":"))


def _found(*notes: str, verbose: bool = False, chunk_id: str = "2d99ff60-7a88-4524-9dd0") -> str:
    if verbose:
        # The verbose shape measured in the distractor runs: an id and a path per hit.
        body = "\n\n".join(
            f"**[{i}]** score=0.0000 | id={chunk_id}-{i:012d} | /tmp/e-x/home/cwd/mm-notes/{n}"
            f"\n```\n{n}\n```"
            for i, n in enumerate(notes, 1)
        )
    else:
        body = "\n\n".join(f"[{i}] 0.00 | {n}" for i, n in enumerate(notes, 1))
    return json.dumps({"result": f"Found {len(notes)} results:\n\n{body}"})


_FILLER = tuple(f"2026-01-{i:02d}.md > ## Filler\n" + "lorem ipsum " * 60 for i in range(1, 20))

# case -> grader -> (must pass, must fail)
_ANSWERS = {
    "cap-superseded-registry": (
        [
            "Because October replaced August.\nANSWER: cr-falcon-5813.internal",
            "ANSWER: `cr-falcon-5813.internal`\n",
            "ANSWER: cr-falcon-5813.internal.",
        ],
        [
            "ANSWER: cr-otter-2207.internal",
            "WRONG_ANSWER: cr-falcon-5813.internal",
            "ANSWER: cr-falcon-5813.internal\nANSWER: cr-otter-2207.internal",
            "ANSWER: cr-falcon-5813.internal-2",
            "cr-falcon-5813.internal",
        ],
    ),
    "cap-distractor-bucket": (
        ["ANSWER: gs://bkp-ibis-7146", "ANSWER: `bkp-ibis-7146`", "ANSWER: gs://bkp-ibis-7146/."],
        [
            "ANSWER: gs://bkp-wren-3390",
            "ANSWER: gs://bkp-ibis-71460",
            "ANSWER: bkp-ibis-7146 or bkp-wren-3390",
            "ANSWER: bkp-ibis-7146\nNote: use bkp-wren-3390 instead.",
            "WRONG_ANSWER: gs://bkp-ibis-7146",
        ],
    ),
    "cap-multihop-oncall": (
        ["ledger-api.\nANSWER: heron-ledger-oncall", "ANSWER: @heron-ledger-oncall"],
        [
            "ANSWER: marten-quota-oncall",
            "ANSWER: heron-ledger-oncall-2",
            "ANSWER: heron-ledger-oncall, marten-quota-oncall",
            "WRONG_ANSWER: heron-ledger-oncall",
        ],
    ),
    "cap-nohint-psql": (
        [
            "ANSWER: psql -h staging-db.internal -p 6519 -U deploy -d app",
            'ANSWER: `psql "host=staging-db.internal port=6519 user=deploy dbname=app"`',
            "ANSWER: psql postgresql://deploy@staging-db.internal:6519/app",
            "ANSWER: psql --host=staging-db.internal --port=6519 --username=deploy app.",
            "ANSWER: psql -h staging-db.internal -p6519 -U deploy -d app",
        ],
        [
            "ANSWER: psql -h staging-db.internal -p 5432 -U deploy -d app",
            "ANSWER: psql -h staging-db.internal -p 65190 -U deploy -d app",
            "ANSWER: psql -h staging-db.internal -p 16519 -U deploy -d app",
            "ANSWER: psql -p 6519 -d app\nOr use port 5432.",
            "ANSWER: psql -p 5432 -d app\n6519",
            "ANSWER: psql -p 6519\nANSWER: psql -p 5432",
            "WRONG_ANSWER: psql -h staging-db.internal -p 6519 -U deploy -d app",
            "ANSWER: psql -h staging-db.internal -p 5432 -U deploy -d app6519archive",
            "ANSWER: psql -h staging-db.internal -p 5432 -U deploy -d app_6519",
            "ANSWER: psql -h staging-db.internal -p 5432 -U deploy -d app-p6519",
            "psql -h staging-db.internal -p 6519 -U deploy -d app",
        ],
    ),
    "cap-nohint-deploy": (
        [
            "ANSWER: rlsctl deploy --build 1234 --env prod --region kx-north-9",
            "ANSWER: `rlsctl deploy --region=kx-north-9 --env=prod --build=1234`",
        ],
        [
            "ANSWER: rlsctl deploy --build 1234 --env prod --region kx-north-90",
            "ANSWER: rlsctl deploy --build 1234 --env prod --region kx-north-9b",
            "ANSWER: rlsctl deploy --build 1234 --env prod --region kx-north-9_backup",
            "ANSWER: rlsctl deploy --build 1234 --env prod --region us-east-1",
            "ANSWER: rlsctl deploy --region kx-north-9\nANSWER: rlsctl deploy --region us-east-1",
            "ANSWER: rlsctl deploy --build 1234 --env prod\nkx-north-9",
            "WRONG_ANSWER: rlsctl deploy --build 1234 --env prod --region kx-north-9",
        ],
    ),
}


@pytest.mark.parametrize("case", sorted(_ANSWERS))
def test_answer_patterns(case):
    good, bad = _ANSWERS[case]
    for text in good:
        assert _matches(case, "answer", text), (case, text)
    for text in bad:
        assert not _matches(case, "answer", text), (case, text)


_REGISTRY_AUG = "2026-08-04.md > ## CI image registry\nCI pushes to cr-otter-2207.internal."
_REGISTRY_OCT = "2026-10-14.md > ## CI image registry (update)\nnow cr-falcon-5813.internal."
_HOP1 = "2026-10-30.md > ## Service port changes\n- 2026-09-17: ledger-api to port 7342."
_OWNER = "2026-07-01.md > ## On-call: ledger-api\nPages go to heron-ledger-oncall."

# (case, grader, token note) -> the result records a grader must pass and must fail
_RETRIEVALS = [
    ("cap-superseded-registry", "retrieved", _REGISTRY_OCT, "cr-falcon-5813.internal-2"),
    ("cap-superseded-registry", "retrieved_old", _REGISTRY_AUG, "cr-otter-2207.internals"),
    ("cap-distractor-bucket", "retrieved", "gs://bkp-ibis-7146.", "gs://bkp-ibis-71460"),
    ("cap-multihop-oncall", "retrieved", _OWNER, "heron-ledger-oncall2"),
    ("cap-multihop-oncall", "retrieved_hop1", _HOP1, "port 73420"),
    ("cap-nohint-psql", "retrieved", "on port 6519. The old", "port 65190"),
    ("cap-nohint-deploy", "retrieved", "--region kx-north-9`.", "--region kx-north-9b"),
]


@pytest.mark.parametrize(("case", "grader", "note", "longer"), _RETRIEVALS)
def test_retrieval_patterns(case, grader, note, longer):
    # Pass: the note in a result (compact or verbose), also late in a long multi-note result.
    assert _matches(case, grader, _record(_found(note)))
    assert _matches(case, grader, _record(_found(note, verbose=True)))
    assert _matches(case, grader, _record(_found(*_FILLER, note)))
    assert len(_record(_found(*_FILLER, note))) > 14000
    # Fail: the note only in assistant text, a longer identifier, or no result at all.
    assert not _matches(case, grader, _record(note, kind="assistant"))
    assert not _matches(case, grader, _record(_found(longer)))
    assert not _matches(case, grader, _record(_found("nothing relevant")))
    assert not _matches(case, grader, _record(_found("nothing relevant", verbose=True)))


@pytest.mark.parametrize(
    ("case", "grader", "lookalike"),
    [
        ("cap-multihop-oncall", "retrieved_hop1", "teleport 7342"),
        ("cap-multihop-oncall", "retrieved_hop1", "port 7342backup"),
        ("cap-nohint-psql", "retrieved", "teleport 6519"),
        ("cap-nohint-psql", "retrieved", "port 6519_hotfix"),
    ],
)
def test_port_phrase_is_bounded_on_both_sides(case, grader, lookalike):
    assert not _matches(case, grader, _record(_found(lookalike)))


@pytest.mark.parametrize(
    ("case", "grader", "digits"),
    [("cap-multihop-oncall", "retrieved_hop1", "7342"), ("cap-nohint-psql", "retrieved", "6519")],
)
def test_numeric_tokens_do_not_match_a_chunk_id(case, grader, digits):
    # A verbose hit carries a UUID whose 4-digit groups can equal the token.
    unrelated = _found(
        "2026-07-03.md > ## On-call: atlas-gw", verbose=True, chunk_id=f"a1b2-{digits}"
    )
    assert f"-{digits}-" in unrelated
    assert not _matches(case, grader, _record(unrelated))


def _holders(seed: str, token: str) -> list[str]:
    return sorted(
        p.name for p in (_SEEDS / seed).glob("*.md") if token in p.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("seed", "token", "files"),
    [
        # retrieved_old proves both notes were seen only while OLD sits in the older note alone.
        ("registry-host", "cr-otter-2207", ["2026-08-04.md"]),
        ("registry-host", "cr-falcon-5813", ["2026-10-14.md"]),
        ("backup-bucket", "bkp-wren-3390", ["2026-06-09.md"]),
        ("backup-bucket", "bkp-ibis-7146", ["2026-10-21.md"]),
        ("service-ownership", "heron-ledger-oncall", ["2026-07-01.md"]),
        ("deploy-cli", "kx-north-9", ["2026-09-22.md"]),
    ],
)
def test_each_token_lives_in_exactly_its_note(seed, token, files):
    assert _holders(seed, token) == files


def test_capability_seeds_carry_no_entry_timestamp():
    # A `> created:` line would set the indexed created_at; these fixtures date notes by name
    # and text only.
    for seed in ("registry-host", "backup-bucket", "service-ownership", "deploy-cli"):
        for note in (_SEEDS / seed).glob("*.md"):
            assert "> created:" not in note.read_text(encoding="utf-8"), note


def test_capability_judges_have_a_rubric():
    for case, planned in _CAPABILITY.items():
        for name, (kind, _, _) in planned.items():
            if kind == "llm":
                body = _frontmatter(_EVALS / case / "graders" / f"{name}.md")[1]
                assert "PASS only if" in body and "FAIL if" in body, (case, name)


def test_hop1_token_is_only_in_the_september_entry():
    log = (_SEEDS / "service-ownership" / "2026-10-30.md").read_text(encoding="utf-8")
    september = [line for line in log.splitlines() if "7342" in line]
    assert len(september) == 1 and september[0].startswith("- 2026-09-17: ledger-api")
    others = [p for p in (_SEEDS / "service-ownership").glob("*.md") if p.name != "2026-10-30.md"]
    assert others and all("7342" not in p.read_text(encoding="utf-8") for p in others)


_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="the launcher is POSIX-only (pwd, O_NOFOLLOW)"
)


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location(
        "agent_eval_launch_server_cases", _PLUGIN / "launch_server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _capability_seeds() -> list[str]:
    return sorted(
        {_frontmatter(_EVALS / c / "prompt.md")[0]["env"]["EVAL_MM_SEED"] for c in _CAPABILITY}
    )


@_posix
def test_every_case_seed_loads(launcher):
    for case in _all_cases():
        seed = (_frontmatter(case / "prompt.md")[0].get("env") or {}).get("EVAL_MM_SEED")
        if seed:
            assert launcher.seed_files(seed), case.name


@_posix
@pytest.mark.parametrize("seed", _capability_seeds())
def test_capability_seed_files_are_past_dates_and_copy_beside_today(launcher, seed, tmp_path):
    files = launcher.seed_files(seed)
    for item in files:
        assert date.fromisoformat(item.stem) <= date(2026, 10, 31), item
    notes = tmp_path / "notes"
    notes.mkdir()
    launcher.link_today(tmp_path, notes, datetime(2026, 11, 27, 12, tzinfo=timezone.utc))
    launcher.copy_seed(files, notes)
    assert sorted(p.name for p in notes.iterdir()) == sorted(
        ["2026-11-27.md", *(f.name for f in files)]
    )

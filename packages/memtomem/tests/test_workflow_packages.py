"""Keep the workflow evidence checker honest, including new untracked docs."""

from __future__ import annotations

import importlib.util
import json
from contextlib import closing, nullcontext
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest
import test_docs_guards as docs_guards

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / "examples" / "workflows"


@pytest.fixture
def demo():
    spec = importlib.util.spec_from_file_location("workflow_demo", WORKFLOWS / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_demo_query_has_a_nonempty_evaluation_case(demo):
    cases = json.loads((WORKFLOWS / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    assert set(demo.DEMO_QUERIES) == set(demo.WORKFLOWS)
    for workflow, queries in demo.DEMO_QUERIES.items():
        assert queries
        for query, source_filter in queries:
            assert any(
                case["workflow"] == workflow
                and case["query"] == query
                and case.get("source_filter") == source_filter
                and case["expected"]
                for case in cases
            ), (workflow, query, source_filter)


@pytest.mark.parametrize("workflow", ["all", "handoff", "decisions", "onboarding"])
def test_main_demo_routes_and_prints_the_selected_workflow(demo, monkeypatch, capsys, workflow):
    calls = []
    lab = SimpleNamespace(index=lambda: calls.append("index"))
    monkeypatch.setattr(demo, "make_lab", lambda: nullcontext(lab))

    def retrieve(selected_lab, query, source_filter):
        assert selected_lab is lab
        calls.append((query, source_filter))
        return [{"source": "policy.md", "content": "source-backed evidence"}]

    monkeypatch.setattr(demo, "retrieve", retrieve)
    monkeypatch.setattr(demo.sys, "argv", ["demo.py", "--workflow", workflow])
    demo.main()
    selected = demo.WORKFLOWS if workflow == "all" else (workflow,)
    expected = [pair for key in selected for pair in demo.DEMO_QUERIES[key]]
    assert calls == ["index", *expected]
    output = capsys.readouterr().out
    assert output.count("출처: policy.md") == len(expected)
    for key in selected:
        assert f"## {key}" in output


def test_manifest_default_uses_current_root(demo, monkeypatch, tmp_path):
    shutil.copytree(WORKFLOWS / "project", tmp_path / "project")
    (tmp_path / "project/product/additional.md").write_text("extra source", encoding="utf-8")
    files = demo.product_snapshot(tmp_path / "project")
    (tmp_path / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    assert len(demo.verify_product_sources()) == 9


def test_checker_requires_expected_source_and_full_body(demo):
    case = {
        "id": "authority",
        "expected": [{"source": "product/trial-002.md", "text": ["状態: adopted", "14 days"]}],
    }
    valid = {"source": "product/trial-002.md", "content": "状態: adopted; 14 days"}
    demo.check_case(case, [valid])
    for invalid in (
        [],
        [{**valid, "source": "product/trial-003.md"}],
        [{**valid, "content": "状態: proposed; 14 days"}],
        [{**valid, "content": "14 days"}, {"source": "other.md", "content": "状態: adopted"}],
        # Same file is not enough: the evidence must belong to one retrieved chunk.
        [{**valid, "content": "14 days"}, {**valid, "content": "状態: adopted"}],
    ):
        with pytest.raises(AssertionError):
            demo.check_case(case, invalid)


def test_checker_rejects_false_empty_result(demo):
    demo.check_case({"id": "absent", "expected": []}, [])
    with pytest.raises(AssertionError):
        demo.check_case({"id": "absent", "expected": []}, [{"source": "unrelated.md"}])


@pytest.mark.parametrize("fragments", [None, [], [""], ["  "], "policy", [123]])
def test_empty_or_malformed_evidence_cannot_degrade_to_source_only(demo, fragments):
    cases = json.loads((WORKFLOWS / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    item = cases[0]["expected"][0]
    if fragments is None:
        item.pop("text")
    else:
        item["text"] = fragments
    with pytest.raises(AssertionError, match="evidence fragments"):
        demo.validate_cases(cases)
    with pytest.raises(AssertionError, match="evidence fragments"):
        demo.check_case(cases[0], [{"source": item["source"], "content": "arbitrary text"}])


@pytest.mark.parametrize(
    "case_id,original,replacement",
    [
        ("onboarding-03", '"value": 3,', '"value": 30,'),
        ("onboarding-06", '"value": 90,', '"value": 900,'),
        ("onboarding-09", '"value": true,', '"value": false,'),
    ],
)
def test_wrong_policy_value_is_rejected_even_with_matching_prefix(
    demo, case_id, original, replacement
):
    cases = json.loads((WORKFLOWS / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    case = next(case for case in cases if case["id"] == case_id)
    source = case["expected"][0]["source"]
    body = (ROOT / "examples/onboarding/slateharbor/project" / source).read_text(encoding="utf-8")
    changed = body.replace(original, replacement)
    assert changed != body and case["query"] in changed
    with pytest.raises(AssertionError):
        demo.check_case(case, [{"source": source, "content": changed}])


@pytest.mark.parametrize("native_newline", ["\n", "\r\n"])
def test_documented_manifest_command_reproduces_and_extends_inventory(
    demo, tmp_path, monkeypatch, native_newline
):
    target = tmp_path / "examples/workflows"
    shutil.copytree(WORKFLOWS / "project", target / "project")
    text = (WORKFLOWS / "README.md").read_text(encoding="utf-8")
    snippet = text.split("uv run python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.chdir(tmp_path)
    real_write = Path.write_text

    def platform_write(path, data, *args, **kwargs):
        if kwargs.get("newline") is None:
            kwargs["newline"] = native_newline
        return real_write(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", platform_write)
    exec(compile(snippet, "README-manifest-command", "exec"), {})
    assert (target / "manifest.json").read_bytes() == (WORKFLOWS / "manifest.json").read_bytes()
    (target / "project/product/pricing.json").write_text("{}\n", encoding="utf-8")
    archive = target / "project/product/archive"
    archive.mkdir()
    (archive / "old.md").write_text("old policy\n", encoding="utf-8")
    exec(compile(snippet, "README-manifest-command", "exec"), {})
    assert len(demo.verify_product_sources(target)) == 10


def test_documented_distinct_search_count():
    cases = json.loads((WORKFLOWS / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    assert len({(case["query"], case.get("source_filter")) for case in cases}) == 25


@pytest.mark.parametrize("state", ["updated", "stale-beyond-top-five", "missing"])
def test_reindex_checks_the_whole_exact_source(demo, tmp_path, state):
    source = tmp_path / "policy.md"
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE chunks (source_file TEXT, content TEXT)")
        rows = [(str(source), "current policy")] if state != "missing" else []
        rows += [(str(source), f"unrelated chunk {i}") for i in range(10)]
        if state == "stale-beyond-top-five":
            rows.append((str(source), "obsolete policy"))
        # Other files may legitimately keep historical policy text.
        rows.append((str(tmp_path / "history.md"), "obsolete policy"))
        connection.executemany("INSERT INTO chunks VALUES (?, ?)", rows)
        lab = SimpleNamespace(_index=lambda: nullcontext(connection))
        if state == "updated":
            demo.check_reindexed_source(lab, source, "current policy", "obsolete policy")
        else:
            with pytest.raises(AssertionError):
                demo.check_reindexed_source(lab, source, "current policy", "obsolete policy")


def test_evaluation_sources_exist_without_indexing_answers():
    cases = json.loads((WORKFLOWS / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    assert len({case["id"] for case in cases}) == len(cases) == 30
    for workflow in ("handoff", "decisions", "onboarding"):
        assert sum(case["workflow"] == workflow for case in cases) == 10
    for case in cases:
        assert case["human_expectation"] and case["question"]
        for expected in case["expected"]:
            source = expected["source"]
            base = (
                WORKFLOWS
                if source.startswith("product/")
                else ROOT / "examples/onboarding/slateharbor"
            )
            path = base / "project" / source
            assert path.is_file(), source
            text = path.read_text(encoding="utf-8")
            assert all(fragment in text for fragment in expected["text"]), case["id"]
    assert not list((WORKFLOWS / "project").rglob("evaluation.json"))
    assert not list((WORKFLOWS / "project").rglob("template.md"))


def test_package_links_resolve_even_before_staging(monkeypatch):
    pages = [*WORKFLOWS.rglob("*.md"), ROOT / "docs/guides/workflow-packages-ko.md"]
    # Reuse fence/inline-code handling, reference links and anchor checks rather
    # than maintaining a weaker second Markdown parser for untracked examples.
    monkeypatch.setattr(docs_guards, "_tracked_markdown", lambda: pages)
    docs_guards.TestInternalDocLinksResolve().test_links_and_anchors_resolve()


def test_missing_evidence_is_rejected_for_every_expected_source(demo):
    cases = json.loads((WORKFLOWS / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    for case in cases:
        rows = []
        for expected in case["expected"]:
            source = expected["source"]
            base = (
                WORKFLOWS
                if source.startswith("product/")
                else ROOT / "examples/onboarding/slateharbor"
            )
            body = (base / "project" / source).read_text(encoding="utf-8")
            rows.append({"source": source, "content": body})
        demo.check_case(case, rows)
        for index, expected in enumerate(case["expected"]):
            for fragment in expected["text"]:
                changed = rows[index]["content"].replace(fragment, "[missing evidence]")
                assert case["query"] in changed
                altered = [dict(row) for row in rows]
                altered[index]["content"] = changed
                with pytest.raises(AssertionError):
                    demo.check_case(case, altered)


@pytest.mark.parametrize("corruption", ["extra", "duplicate", "unknown-workflow", "missing"])
def test_case_inventory_rejects_extra_duplicate_or_misclassified_cases(demo, corruption):
    cases = json.loads((WORKFLOWS / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    demo.validate_cases(cases)
    if corruption == "extra":
        cases.append({**cases[0], "workflow": "unknown"})
    elif corruption == "duplicate":
        cases[1]["id"] = cases[0]["id"]
    elif corruption == "unknown-workflow":
        cases[0]["workflow"] = "unknown"
    else:
        cases.pop()
    with pytest.raises(AssertionError):
        demo.validate_cases(cases)


@pytest.mark.parametrize("mutation", ["edit", "remove", "add"])
def test_product_manifest_rejects_drift_before_allocating_lab(
    demo, tmp_path, monkeypatch, mutation
):
    shutil.copytree(WORKFLOWS / "project", tmp_path / "project")
    shutil.copyfile(WORKFLOWS / "manifest.json", tmp_path / "manifest.json")
    assert len(demo.verify_product_sources(tmp_path)) == 8
    target = tmp_path / "project/product/trial-002.md"
    if mutation == "edit":
        target.write_text(target.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    elif mutation == "remove":
        target.unlink()
    else:
        (target.parent / "unexpected.md").write_text("unexpected", encoding="utf-8")
    monkeypatch.setattr(demo, "ROOT", tmp_path)

    def no_import(*args, **kwargs):
        pytest.fail("A changed corpus must fail before importing/allocating a Lab")

    monkeypatch.setattr(demo.importlib.util, "spec_from_file_location", no_import)
    with pytest.raises(ValueError, match="샘플이 변경"):
        demo.make_lab()


def test_retrieve_preserves_user_memory_hits_outside_corpus(demo, tmp_path):
    project = tmp_path / "project"
    saved = tmp_path / "memories/saved.md"
    lab = SimpleNamespace(
        project=project,
        search=lambda *args, **kwargs: [{"chunk_id": "one"}, {"chunk_id": "two"}],
        _chunk_bodies=lambda ids: {
            "one": ("project evidence", str(project / "policy.md"), ""),
            "two": ("saved memory", str(saved), ""),
        },
    )
    assert demo.retrieve(lab, "memory") == [
        {"source": "policy.md", "chunk_id": "one", "content": "project evidence"},
        {"source": str(saved), "chunk_id": "two", "content": "saved memory"},
    ]

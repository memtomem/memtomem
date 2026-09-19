"""Run real, isolated BM25 workflows; validate retrieval separately from interpretation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
SLATEHARBOR = ROOT.parent / "onboarding" / "slateharbor"
WORKFLOWS = ("handoff", "decisions", "onboarding")
DEMO_QUERIES = {
    "handoff": [
        ("worker lease recovery", "handoff-current.md"),
        ("billing webhook retry", "handoff-current.md"),
    ],
    "decisions": [("free_trial", "product/"), ("audit_retention", "product/")],
    "onboarding": [
        ("billing webhook retry", "decision-current.md"),
        ("billing webhook retry", "policy.py"),
        ("billing webhook retry", "production.json"),
    ],
}


def product_snapshot(project):
    """Hash precisely the authored product corpus, including its file inventory."""
    product = project / "product"
    if not product.is_dir() or product.is_symlink():
        raise ValueError("제품 샘플 폴더가 없습니다. 전체 저장소의 원본을 복구하세요.")
    paths = sorted(product.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("제품 샘플에 심볼릭 링크가 있습니다. 원본을 복구하세요.")
    return {
        path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }


def verify_product_sources(root=None):
    root = ROOT if root is None else root
    expected = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["files"]
    if not expected or product_snapshot(root / "project") != expected:
        raise ValueError(
            "제품 샘플이 변경되었습니다. 원본을 복구하거나 검토 후 manifest를 갱신하세요."
        )
    return expected


def make_lab():
    """Reuse the existing offline sandbox and add only authored source documents."""
    lab_path = SLATEHARBOR / "lab.py"
    if not lab_path.is_file() or not (ROOT / "project" / "product").is_dir():
        raise FileNotFoundError(
            "전체 저장소가 필요합니다: examples/onboarding와 workflows를 확인하세요."
        )
    expected = verify_product_sources(ROOT)
    spec = importlib.util.spec_from_file_location("workflow_slateharbor_lab", lab_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Pin every child, including initialization, to this checkout's Core.
    core_source = ROOT.parents[1] / "packages" / "memtomem" / "src"
    module.BOOTSTRAP = f"import sys\nsys.path.insert(0, {str(core_source)!r})\n" + module.BOOTSTRAP
    lab = module.Lab(SLATEHARBOR)
    try:
        destination = lab.project / "product"
        if destination.exists():
            raise ValueError(
                "Slateharbor의 product 폴더와 충돌합니다. 샘플 경로 구성을 갱신하세요."
            )
        shutil.copytree(ROOT / "project" / "product", destination)
        if product_snapshot(lab.project) != expected:
            raise ValueError("복사 중 제품 샘플이 변경되었습니다. 다시 실행하세요.")
    except BaseException:
        lab.close()
        raise
    return lab


def retrieve(lab, query, source_filter=None):
    """Return full indexed bodies keyed by real result IDs, never fixture answers."""
    hits = lab.search(query, source=source_filter, top_k=5)
    if not hits:
        return []
    bodies = lab._chunk_bodies([hit["chunk_id"] for hit in hits])
    results = []
    for hit in hits:
        body, source_file, _heading = bodies[hit["chunk_id"]]
        source = Path(source_file).resolve()
        # mm add writes to the isolated user's memory directory, outside the
        # sample project. Keep such a hit, with its actual indexed source path.
        display = (
            source.relative_to(lab.project).as_posix()
            if source.is_relative_to(lab.project)
            else str(source)
        )
        results.append({"source": display, "chunk_id": hit["chunk_id"], "content": body})
    return results


def check_case(case, results):
    """No semantic verdict: check expected source/body pairs or a truly empty query."""
    expected = case["expected"]
    if not expected:
        if results:
            raise AssertionError(f"{case['id']}: expected an empty retrieval, got {results}")
        return
    for item in expected:
        require_fragments(item)
        matches = [row for row in results if row["source"] == item["source"]]
        if not any(all(fragment in row["content"] for fragment in item["text"]) for row in matches):
            found = [row["source"] for row in results]
            raise AssertionError(f"{case['id']}: missing source/body {item}; found {found}")


def require_fragments(item):
    fragments = item.get("text")
    if (
        not isinstance(fragments, list)
        or not fragments
        or any(not isinstance(f, str) or not f.strip() for f in fragments)
    ):
        raise AssertionError("Expected a nonempty list of nonblank evidence fragments")


def source_snapshot():
    roots = (ROOT / "project", SLATEHARBOR / "project")
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def check_reindexed_source(lab, source, expected, obsolete):
    """Inspect every indexed chunk of the exact source, not a ranked result window."""
    with lab._index() as connection:
        bodies = [
            row[0]
            for row in connection.execute(
                "SELECT content FROM chunks WHERE source_file = ?", (str(source.resolve()),)
            ).fetchall()
        ]
    if not any(expected in body for body in bodies) or any(obsolete in body for body in bodies):
        raise AssertionError("Reindexed source is missing expected text or retains stale text")


def validate_cases(cases):
    if not len(cases) == len({case["id"] for case in cases}) == 30:
        raise AssertionError("Expected 30 unique evaluation IDs")
    for workflow in WORKFLOWS:
        if sum(case["workflow"] == workflow for case in cases) != 10:
            raise AssertionError(f"Expected 10 cases for {workflow}")
    for case in cases:
        for item in case["expected"]:
            require_fragments(item)


def validate():
    # Evaluation answers are deliberately outside both indexed source trees.
    cases = json.loads((ROOT / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    validate_cases(cases)
    before = source_snapshot()
    checked = []
    with make_lab() as lab:
        temporary_root = lab.root
        lab.index()
        for case in cases:
            rows = retrieve(lab, case["query"], case.get("source_filter"))
            check_case(case, rows)
            checked.append({"id": case["id"], "sources": [row["source"] for row in rows]})
            print(f"PASS retrieval {case['id']}", flush=True, file=sys.stderr)

        marker = "workflow_roundtrip_6721"
        added = json.loads(lab.run("add", f"{marker}: 다음에 checkpoint를 확인한다.", "--json"))
        if not added.get("ok") or added.get("chunks", 0) < 1:
            raise AssertionError(f"Memory add failed: {added}")
        if not any(marker in row["content"] for row in retrieve(lab, marker)):
            raise AssertionError("Fresh CLI process did not retrieve the saved memory")
        print("PASS fresh-process-memory", flush=True, file=sys.stderr)

        target = lab.project / "product" / "trial-002.md"
        original = target.read_text(encoding="utf-8")
        target.write_text(
            original.replace("무료 체험은 14일", "무료 체험은 21일"), encoding="utf-8"
        )
        lab.run("index", str(target))
        updated = retrieve(lab, "free_trial", "trial-002.md")
        check_case(
            {
                "id": "reindex",
                "expected": [{"source": "product/trial-002.md", "text": ["무료 체험은 21일"]}],
            },
            updated,
        )
        check_reindexed_source(lab, target, "무료 체험은 21일", "무료 체험은 14일")
        print("PASS copied-source-reindex", flush=True, file=sys.stderr)

        with make_lab() as other:
            other_root = other.root
            other.index()
            if other.search(marker):
                raise AssertionError("Another store returned the first store's private memory")
            check_case(
                {
                    "id": "other-original",
                    "expected": [{"source": "product/trial-002.md", "text": ["무료 체험은 14일"]}],
                },
                retrieve(other, "free_trial", "trial-002.md"),
            )
        print("PASS separate-store", flush=True, file=sys.stderr)

    if temporary_root.exists() or other_root.exists() or before != source_snapshot():
        raise AssertionError("Original source preservation or temporary cleanup failed")
    print("PASS originals-preserved-and-cleanup", flush=True, file=sys.stderr)
    return {
        "retrieval_cases_passed": len(checked),
        "workflow_counts": {key: sum(c["workflow"] == key for c in cases) for key in WORKFLOWS},
        "distinct_search_paths": len({(c["query"], c.get("source_filter")) for c in cases}),
        "expected_source_files": len(
            {item["source"] for case in cases for item in case["expected"]}
        ),
        "fresh_process_memory": True,
        "copied_source_reindex": True,
        "separate_store": True,
        "originals_preserved_and_cleanup": True,
        "human_interpretation": "not_evaluated",
        "live_ai_sessions": "not_evaluated",
        "user_pilot": "not_run",
        "cases": checked,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--workflow", choices=(*WORKFLOWS, "all"), default="all")
    mode.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    # Help stays available even on an unsupported interpreter; execution does not.
    if sys.version_info < (3, 12):
        parser.error("Python 3.12 이상이 필요합니다.")
    if args.validate:
        print(json.dumps(validate(), ensure_ascii=False, indent=2))
        return
    selected = WORKFLOWS if args.workflow == "all" else (args.workflow,)
    with make_lab() as lab:
        lab.index()
        for workflow in selected:
            print(f"\n## {workflow}", flush=True)
            for query, source_filter in DEMO_QUERIES[workflow]:
                print(f"\n검색: {query} / 검색 범위: {source_filter or '전체'}", flush=True)
                for row in retrieve(lab, query, source_filter):
                    print(f"\n출처: {row['source']}\n{row['content']}", flush=True)
    print("\n임시 저장소 정리 완료. 원문 해석과 실제 AI 세션은 별도로 확인하세요.")


if __name__ == "__main__":
    main()

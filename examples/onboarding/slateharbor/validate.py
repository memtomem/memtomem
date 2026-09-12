"""Validate source integrity, policy behavior and actual offline retrieval."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import tomllib

import yaml

from generate import build_project, manifest
from lab import Lab, ROOT, verify_sources

DOMAINS = ("auth", "billing", "notifications", "jobs", "files", "reports")
KINDS = ("decision", "implementation", "configuration")
# Every domain x document kind. Named here so deleting cases from
# evaluation.json fails the validator instead of quietly shrinking its
# coverage to zero while still reporting PASS.
EXPECTED_CASE_IDS = frozenset(f"{domain}-{kind}" for domain in DOMAINS for kind in KINDS)
EXPECTED_POLICY_TESTS = 36
# The file each document kind must be retrieved from, so a case cannot claim a
# kind while pointing at a different one.
KIND_SOURCE_FILTERS = {
    "decision": "decision-current.md",
    "implementation": "policy.py",
    "configuration": "production.json",
}


def static_checks():
    expected = build_project()
    actual = {
        p.relative_to(ROOT / "project").as_posix(): p.read_text(encoding="utf-8")
        for p in (ROOT / "project").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert actual == expected, "Regeneration drift or unexpected source files"
    assert verify_sources() == manifest(expected)
    counts = Counter(Path(name).suffix for name in actual)
    assert counts == {".md": 60, ".py": 42, ".json": 30, ".yaml": 12, ".toml": 6}, counts
    hashes = [hashlib.sha256(s.encode()).hexdigest() for s in actual.values()]
    assert len(set(hashes)) == 150, "Duplicate whole-file content"
    for name, text in actual.items():
        suffix = Path(name).suffix
        if suffix == ".py":
            ast.parse(text, filename=name)
        elif suffix in {".json", ".yaml", ".toml"}:
            parsed = {".json": json.loads, ".yaml": yaml.safe_load, ".toml": tomllib.loads}[suffix](
                text
            )
            assert isinstance(parsed, dict)
            for block in parsed.values():
                for key in (
                    "decision",
                    "implementation",
                    "source",
                    "runbook",
                    "superseded_by",
                ):
                    if key in block:
                        assert block[key] in actual, (name, block[key])
        elif suffix == ".md":
            for target in re.findall(r"\]\(([^)]+)\)", text):
                resolved = (ROOT / "project" / name).parent.joinpath(target).resolve()
                assert (
                    resolved.is_relative_to((ROOT / "project").resolve()) and resolved.is_file()
                ), (name, target)
    return dict(counts)


def validate():
    counts = static_checks()
    original = dict(__import__("os").environ)
    evaluations = json.loads((ROOT / "evaluation.json").read_text(encoding="utf-8"))["cases"]
    case_ids = [case["id"] for case in evaluations]
    assert len(case_ids) == len(set(case_ids)), f"Duplicate evaluation case ids: {case_ids}"
    assert set(case_ids) == EXPECTED_CASE_IDS, (
        f"missing {sorted(EXPECTED_CASE_IDS - set(case_ids))}, "
        f"unexpected {sorted(set(case_ids) - EXPECTED_CASE_IDS)}"
    )
    # Ids alone do not pin coverage: 18 correctly-named cases can all carry the
    # same domain's payload and pass one retrieval eighteen times. Bind each
    # case's target back to the domain and document kind its id names.
    for case in evaluations:
        domain, kind = case["id"].rsplit("-", 1)
        assert case["expected_source"].startswith(f"{domain}/"), case
        assert case["source_filter"] == KIND_SOURCE_FILTERS[kind], case
        assert case["expected_source"].endswith(case["source_filter"]), case
        assert case["required_text"] in case["query"], case
    targets = {case["expected_source"] for case in evaluations}
    assert len(targets) == len(EXPECTED_CASE_IDS), f"Cases share targets: {sorted(targets)}"
    with Lab() as lab:
        assert lab.search("legacy callback") == []
        policy_tests = sum(lab.policy_checks())
        assert policy_tests == EXPECTED_POLICY_TESTS, policy_tests
        lab.index()
        inventory = lab.inventory()
        assert inventory["files"] == 150 and 800 <= inventory["chunks"] <= 1500, inventory
        assert {k: v["files"] for k, v in inventory["formats"].items()} == counts
        # Genuine Python definitions, rather than a whole-file fallback. The
        # aggregate alone tolerates a single file collapsing to one chunk, so
        # the per-file floor is what actually rejects that.
        assert inventory["formats"][".py"]["min_chunks"] >= 2, inventory
        assert inventory["formats"][".py"]["chunks"] >= 250, inventory
        results = []
        for case in evaluations:
            hits = lab.search(case["query"], source=case["source_filter"])
            matching = [
                h
                for h, source in zip(hits, lab.sources(hits), strict=True)
                if source == case["expected_source"] and case["required_text"] in h["content"]
            ]
            assert matching, (case["id"], hits)
            results.append({"id": case["id"], "rank": hits.index(matching[0]) + 1})
        # The broad query keeps distracting historical/environment/other-domain records visible.
        broad = lab.search("retry", top_k=50)
        assert len(broad) > 5 and len({source.split("/")[0] for source in lab.sources(broad)}) >= 2
        old = lab.context("notification retry policy", source="history-june.json")
        current = lab.context("notification retry policy", source="production.json")
        assert "superseded" in old and "12" in old
        assert '"value": 5' in current and "current" in current
        assert lab.search("zz_unrecorded_decision_7391") == []
        assert lab.search("legacy callback", namespace="missing-project") == []
        assert lab.search("legacy callback", source="no-such-source.py") == []
        context = lab.context("legacy callback", source="production.json")
        assert "AUTH_CALLBACK_V2_ENABLED" in context and "production.json" in context
        first = lab.search("worker lease recovery", source="handoff-current.md")
        processes = lab.processes
        second = lab.search("worker lease recovery", source="handoff-current.md")
        assert (
            first and lab.sources(first) == lab.sources(second) and lab.processes == processes + 1
        )
        assert [h["content"] for h in first] == [h["content"] for h in second]
        # Update only the copied production record, reindex, and ensure no stale duplicate.
        file = lab.project / "notifications/config/production.json"
        value = json.loads(file.read_text(encoding="utf-8"))
        value["retry_attempts"]["value"] = 4
        value["retry_attempts"]["revision"] = "2026-08-25-lab"
        file.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        lab.run("index", str(file))
        changed = lab.context("notification retry policy", source="production.json")
        assert '"value": 4' in changed and "2026-08-25-lab" in changed
        assert '"value": 5' not in changed
        # Different user content must actually flow into storage, not a canned answer.
        added = json.loads(
            lab.run(
                "add",
                "Slateharbor 개인 결정: nebula_export_7391는 야간에만 실행한다.",
                "--tags",
                "personal-lab",
                "--json",
            )
        )
        assert added["ok"] and added["chunks"] > 0
        assert lab.search("nebula_export_7391", tag="personal-lab")
        lab.run(
            "add",
            "개인 실습: legacy callback 결정은 moon_review_7391에서 검토한다.",
            "--tags",
            "personal-lab",
            "--json",
        )
        personal = lab.search("legacy callback", tag="personal-lab")
        assert personal and all("moon_review_7391" in h["content"] for h in personal)
        # Child socket guard is executed, not merely searched for in source text.
        import subprocess
        from lab import BOOTSTRAP

        # One probe per guarded API. Dropping the connect / connect_ex /
        # sendto patch turns its own probe red (measured). create_connection
        # is the exception: its probe stays red without its own patch because
        # it calls the still-patched socket.connect underneath -- redundant
        # cover, not an independent witness for that one line.
        attempts = {
            "socket.create_connection": "socket.create_connection(('127.0.0.1', 9))",
            "socket.socket.connect": "socket.socket().connect(('127.0.0.1', 9))",
            "socket.socket.connect_ex": "socket.socket().connect_ex(('127.0.0.1', 9))",
            "socket.socket.sendto": (
                "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 9))"
            ),
        }
        for api, call in attempts.items():
            probe = BOOTSTRAP.replace("from memtomem.cli import cli\ncli()", call)
            denied = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=lab.root,
                env=lab.env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert denied.returncode != 0 and "blocked socket access" in denied.stderr, (
                api,
                denied.stdout,
                denied.stderr,
            )
        temporary = lab.root
    assert not temporary.exists()
    assert dict(__import__("os").environ) == original
    verify_sources()
    return {
        "status": "PASS",
        "python": sys.version.split()[0],
        "memtomem": importlib.metadata.version("memtomem"),
        "inventory": inventory,
        "retrieval_cases": results,
        "policy_tests": policy_tests,
        "negative_cases": ["unknown query", "wrong namespace", "wrong source", "historical policy"],
        "guarded_socket_apis": sorted(attempts),
        "checks": [
            "fresh CLI process",
            "update and reindex",
            "custom memory",
            "child socket guard",
            "source integrity",
            "cleanup",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--static", action="store_true")
    args = parser.parse_args()
    result = static_checks() if args.static else validate()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

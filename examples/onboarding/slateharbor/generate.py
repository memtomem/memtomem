"""Build the curated Slateharbor corpus reproducibly; --check never writes files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scenarios import domains

ROOT = Path(__file__).resolve().parent
VERSION = "slateharbor-1"
CURRENT = "2026-08-24"
OLD = "2026-06-01"
GENRES = (
    "decisions/decision-current",
    "history/decisions-june",
    "incidents/review",
    "runbooks/recovery",
    "handoffs/handoff-current",
    "proposals/next",
    "contracts/behavior",
    "rollouts/checklist",
    "support/diagnosis",
    "releases/august",
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def documents(domain, label, rules):
    titles = [
        "채택 결정",
        "대체된 6월 결정",
        "장애 회고",
        "복구 절차",
        "작업 인계",
        "검토 중 제안",
        "동작 계약",
        "단계 배포 점검",
        "지원 진단",
        "8월 변경 기록",
    ]
    for index, (genre, title) in enumerate(zip(GENRES, titles, strict=True)):
        status = "superseded" if index == 1 else "proposed" if index == 5 else "current"
        date = OLD if index == 1 else CURRENT
        text = (
            f"---\ntags: [slateharbor, {domain}, {status}]\n---\n# Slateharbor {label}: {title}\n\n"
        )
        text += (
            f"> 합성 프로젝트 기록. 상태: {status}. 기준일: {date}. 실제 운영 지침이 아닙니다.\n"
        )
        for n, rule in enumerate(rules, 1):
            key, topic = rule["key"], rule["label"]
            src = "../../src/policy.py"
            config = "../../config/production.json"
            decision = "../decisions/decision-current.md"
            prefix = f"{domain.upper()}-{n:02}"
            choices = [
                f"결정 {prefix}: `{topic}`의 현재 값은 `{rule['current']}`입니다.\n\n"
                f"선택 이유: {rule['rationale']}\n\n대안 검토: 이전 값 `{rule['previous']}`는 아래 장애 조건을 만족하지 못했습니다. {rule['symptom']}\n\n"
                f"적용: [정책 구현]({src})의 `{key}`와 [운영 설정]({config})의 같은 키를 함께 확인합니다.\n\n검증: {rule['action']}",
                f"상태: superseded. 6월의 `{topic}` 값은 `{rule['previous']}`였습니다. 이 문서는 당시의 판단을 보존합니다.\n\n"
                f"당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.\n\n"
                f"가정이 깨진 관찰: {rule['symptom']}\n\n8월 결정으로 대체: `{rule['current']}`. [현행 결정]({decision})을 확인하고 이 값을 운영에 복사하지 마세요.",
                # Postmortem convention (Google SRE ch.15, PagerDuty, Atlassian):
                # what stopped the bleeding is recorded separately from the
                # durable fix. A threshold change is containment; the follow-up
                # task is the lasting one. Stating it that way is true for every
                # policy here, including the ones whose symptom describes a
                # defect no threshold can remove -- and it asserts no code
                # defect where the scenario does not describe one.
                f"관찰 (2026-07-{n + 10:02} 09:10 KST): {rule['symptom']}\n\n"
                f"09:25 진단: {rule['action']}\n\n"
                f"10:05 즉시 완화: `{topic}`의 `{key}`를 이전 `{rule['previous']}`에서 `{rule['current']}`로 "
                f"조정한 canary를 비교했습니다. 재발 빈도와 영향 범위를 줄이기 위한 조치이며, "
                f"이것만으로 원인이 사라졌다고 보지 않습니다. 확정된 정책 계약은 아래 배운 점과 "
                f"채택 결정에 있습니다.\n\n"
                f"후속 작업: {rule['next_work']}\n\n"
                f"배운 점: {rule['rationale']} [채택 결정]({decision}).",
                f"시작 조건: {rule['symptom']}\n\n1. {rule['action']}\n\n"
                f"2. [운영 설정]({config})에서 `{topic}` / `{key}`의 현재 값 `{rule['current']}`를 확인합니다. staging 값과 섞지 않습니다.\n\n"
                f"3. [구현]({src})에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.\n\n"
                f"종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: {rule['rationale']}",
                f"작업: {rule['next_work']}\n\n이미 확정: `{topic}` = `{rule['current']}`. {rule['rationale']}\n\n"
                f"다음 사람이 볼 곳: [채택 결정]({decision}), [구현]({src}), [운영 설정]({config}).\n\n"
                f"재개 순서: {rule['action']}\n\n보류 사항: 원인 확인 없이 이전 `{rule['previous']}`로 되돌리는 작업은 제안 단계로 남겨 둡니다.",
                f"상태: proposed. `{topic}` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.\n\n"
                f"문제: {rule['symptom']}\n\n제안: {rule['next_work']}\n\n"
                f"현재 계약: `{key}` = `{rule['current']}`. {rule['rationale']}\n\n"
                f"채택 조건: [현행 결정]({decision})의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.",
                f"입력: `{rule['argument']}`. `{topic}`를 평가하는 함수는 [policy.py]({src})의 `{key}`입니다.\n\n"
                f"승인 예: `{rule['accepted']}`. 거절 예: `{rule['rejected']}`. 기준은 [운영 설정]({config})의 `{rule['current']}`입니다.\n\n"
                f"업무 의미: {rule['rationale']}\n\n잘못된 적용 사례: {rule['symptom']}\n\n"
                "이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.",
                f"변경 대상: `{topic}`의 `{key}`. 이전 `{rule['previous']}`, 현재 `{rule['current']}`.\n\n"
                f"배포 전: {rule['action']}\n\n배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.\n\n"
                f"중단 조건: {rule['symptom']}\n\n되돌림 판단: 값만 바꾸지 말고 [채택 이유]({decision})를 확인합니다. {rule['rationale']}\n\n"
                f"배포 후 담당 작업: {rule['next_work']}",
                f"문의 유형: `{topic}`가 예상과 다르게 동작합니다.\n\n증상: {rule['symptom']}\n\n"
                f"확인 질문: 요청이 production인지 staging인지, `{rule['argument']}`의 실제 값이 얼마인지 먼저 확인합니다.\n\n"
                f"진단: {rule['action']}\n\n비교 기준: [현행 설정]({config})의 `{key}` = `{rule['current']}`.\n\n"
                f"설명할 이유: {rule['rationale']} [구현]({src})과 다르면 설정 로딩부터 조사합니다.",
                f"8월 변경: `{topic}` 값이 `{rule['previous']}`에서 `{rule['current']}`로 바뀌었습니다.\n\n"
                f"관련 관찰: {rule['symptom']}\n\n사용자에게 달라지는 점: {rule['rationale']}\n\n"
                f"확인 절차: {rule['action']}\n\n추적 작업: {rule['next_work']} [결정]({decision}), [구현]({src}).",
            ]
            text += f"\n## {topic}\n\n{choices[index]}\n"
        yield f"{domain}/docs/{genre}.md", text


def python_files(domain, rules):
    intro = f'"""Slateharbor {domain}: synthetic local policy sample, no external services."""\n'
    policy = intro
    for r in rules:
        policy += f"\ndef {r['key']}({r['argument']}, config):\n"
        policy += f'    """{r["label"]}. {r["rationale"]}"""\n'
        policy += f"    return {r['expression']}\n\n"
    yield f"{domain}/src/policy.py", policy
    loader = intro + "from copy import deepcopy\n\n"
    handlers = intro + f"from {domain}.src import policy\n\n"
    telemetry = intro + f"from {domain}.src import policy\n\n"
    replay = intro + f"from {domain}.src import policy\n\n"
    models = intro + "from dataclasses import dataclass\n\n"
    tests = (
        intro
        + f"import json\nimport unittest\nfrom pathlib import Path\nfrom {domain}.src import policy, config as loaders\n\n"
    )
    tests += "CONFIG = json.loads((Path(__file__).parents[1] / 'config/production.json').read_text(encoding='utf-8'))\n\n"
    tests += "class PolicyContract(unittest.TestCase):\n"
    for r in rules:
        key, arg, value = r["key"], r["argument"], r["current"]
        typename = "bool" if isinstance(value, bool) else "int"
        class_name = "".join(w.title() for w in key.split("_")) + "Request"
        loader += f"def configure_{key}(config, value):\n"
        loader += f'    """Validate and copy {r["label"]}; production value is {value}."""\n'
        loader += (
            f"    if type(value) is not {typename}"
            + (" or value < 0" if typename == "int" else "")
            + ":\n"
        )
        loader += f"        raise ValueError('Invalid {key}')\n    updated = deepcopy(config)\n"
        loader += f"    updated['{key}']['value'] = value\n    return updated\n\n"
        handlers += f"def evaluate_{key}({arg}, config):\n"
        handlers += f'    """{r["action"]}"""\n'
        handlers += f"    return {{'policy': '{key}', 'allowed': policy.{key}({arg}, config),\n"
        handlers += f"            'revision': config['{key}']['revision'], 'observed': {arg}}}\n\n"
        telemetry += f"def summarize_{key}(observations, config):\n"
        telemetry += f'    """{r["label"]}: count permitted observations, never turn absence into success."""\n'
        telemetry += "    values = list(observations)\n    if not values:\n        return {'state': 'unknown', 'total': 0}\n"
        telemetry += f"    accepted = sum(bool(policy.{key}(value, config)) for value in values)\n"
        telemetry += "    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}\n\n"
        replay += f"def replay_{key}(fixtures, config):\n"
        replay += f'    """Replay {r["label"]} input examples; no real requests are sent."""\n'
        replay += f"    case = fixtures['{key}']\n    return [policy.{key}(value, config) for value in case['inputs']]\n\n"
        models += f"@dataclass(frozen=True)\nclass {class_name}:\n"
        models += f'    """{r["label"]}: {r["rationale"]}"""\n'
        models += f"    {arg}: {'bool' if isinstance(r['accepted'], bool) else 'int'}\n    environment: str = 'production'\n\n"
        tests += f"    def test_{key}(self):\n"
        tests += f"        self.assertTrue(policy.{key}({r['accepted']!r}, CONFIG))\n"
        tests += f"        self.assertFalse(policy.{key}({r['rejected']!r}, CONFIG))\n"
        tests += f"        before = CONFIG['{key}']['value']\n"
        tests += f"        changed = loaders.configure_{key}(CONFIG, {r['previous']!r})\n"
        tests += f"        self.assertEqual(CONFIG['{key}']['value'], before)\n"
        tests += f"        self.assertEqual(changed['{key}']['value'], {r['previous']!r})\n"
        tests += f"        with self.assertRaises(ValueError):\n            loaders.configure_{key}(CONFIG, 'invalid')\n\n"
    tests += "\nif __name__ == '__main__':\n    unittest.main()\n"
    for name, content in [
        ("config", loader),
        ("handlers", handlers),
        ("telemetry", telemetry),
        ("replay", replay),
        ("models", models),
        ("test_policy", tests),
    ]:
        yield f"{domain}/src/{name}.py", content


def structured_files(domain, rules):
    for environment in ("production", "staging", "history-june"):
        data = {}
        for r in rules:
            historical = environment == "history-june"
            # A superseded record must not carry the CURRENT rationale: that
            # produced a June entry stating `"value": 12` next to a reason
            # saying the policy is 5. It states its own value, why that value
            # stopped holding, and what replaced it -- and points at the June
            # decision, not the one that replaced it.
            data[r["key"]] = {
                "description": r["label"],
                "value": r["previous"] if historical else r["current"],
                "environment": environment,
                "revision": OLD if historical else CURRENT,
                "status": "superseded" if historical else "current",
                "reason": (
                    f"{OLD} 기준으로 `{r['previous']}`를 사용했습니다. "
                    f"가정이 깨진 관찰: {r['symptom']} "
                    f"{CURRENT}에 `{r['current']}`로 대체됐습니다."
                )
                if historical
                else r["rationale"],
                "decision": (
                    f"{domain}/docs/history/decisions-june.md"
                    if historical
                    else f"{domain}/docs/decisions/decision-current.md"
                ),
                "implementation": f"{domain}/src/policy.py",
                "observability": r["action"],
            }
            if historical:
                data[r["key"]]["superseded_by"] = f"{domain}/config/production.json"
            if domain == "auth" and r["key"] == "legacy_callback":
                data[r["key"]]["flags"] = {"AUTH_CALLBACK_V2_ENABLED": not historical}
            if environment == "staging":
                data[r["key"]]["rollout"] = (
                    "pilot-only; synthetic tenant trial; not production authority"
                )
        yield f"{domain}/config/{environment}.json", dump(data)
    cases = {
        r["key"]: {
            "description": r["label"],
            "inputs": [r["accepted"], r["rejected"]],
            "purpose": r["symptom"],
            "environment": "synthetic-local",
            "next_work": r["next_work"],
        }
        for r in rules
    }
    yield f"{domain}/fixtures/requests.json", dump(cases)
    events = {
        r["key"]: {
            "description": r["label"],
            "recorded_at": f"2026-07-{i + 11:02}T09:10:00+09:00",
            "observation": r["symptom"],
            "triage": r["action"],
            "source": f"{domain}/docs/incidents/review.md",
        }
        for i, r in enumerate(rules)
    }
    yield f"{domain}/fixtures/observations.json", dump(events)
    for name in ("canary", "alerts"):
        content = "# Synthetic Slateharbor operational definitions; not deployed infrastructure.\n"
        for r in rules:
            block = {
                "description": r["label"],
                "policy_value": r["current"],
                "environment": "production",
                "observe": r["action"],
                "failure": r["symptom"],
                "runbook": f"{domain}/docs/runbooks/recovery.md",
            }
            if name == "canary":
                block["promotion"] = "Review observations before expanding beyond the pilot cohort"
            else:
                block["on_missing_data"] = (
                    "unknown; request an observation instead of declaring healthy"
                )
            content += f"\n{r['key']}:\n" + "".join(
                f"  {k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in block.items()
            )
        yield f"{domain}/ops/{name}.yaml", content
    content = "# Synthetic local policy profile; no service credentials.\n"
    for r in rules:
        content += f"\n[{r['key']}]\n"
        content += f"description = {json.dumps(r['label'])}\nvalue = {json.dumps(r['current'])}\n"
        content += f"owner = {json.dumps(domain + '-team')}\nrevision = {json.dumps(CURRENT)}\n"
        content += f"reason = {json.dumps(r['rationale'], ensure_ascii=False)}\n"
        content += f"implementation = {json.dumps(domain + '/src/policy.py')}\n"
    yield f"{domain}/policy-profile.toml", content


def build_project():
    files = {}
    for domain, label, rules in domains():
        files.update(documents(domain, label, rules))
        files.update(python_files(domain, rules))
        files.update(structured_files(domain, rules))
    assert len(files) == 150
    return files


def manifest(files):
    return {
        "version": VERSION,
        "synthetic": True,
        "current_as_of": CURRENT,
        "files": {
            name: hashlib.sha256(content.encode()).hexdigest()
            for name, content in sorted(files.items())
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = build_project()
    expected = {"project/" + k: v for k, v in files.items()}
    expected["manifest.json"] = dump(manifest(files))
    if args.check:
        actual = {
            p.relative_to(ROOT / "project").as_posix()
            for p in (ROOT / "project").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        assert actual == set(files), (actual - set(files), set(files) - actual)
        for name, content in expected.items():
            # Byte comparison, not text: a CRLF checkout or a CRLF-writing
            # regeneration must fail here rather than pass and then break
            # verify_sources(), which hashes the raw bytes.
            assert (ROOT / name).read_bytes() == content.encode("utf-8"), (
                f"Regeneration drift: {name}"
            )
        print("PASS reproducible 150-file corpus")
    else:
        for name, content in expected.items():
            path = ROOT / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        print("Generated 150 project files and manifest.json")


if __name__ == "__main__":
    main()

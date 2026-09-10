"""Reproduce the first-user notebook from reviewable text/code; --check is read-only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "notebooks/00_start_here.ipynb"
CELLS = []


def cell(kind, source):
    value = {
        "cell_type": kind,
        "id": f"slateharbor-{len(CELLS):02}",
        "metadata": {},
        "source": textwrap.dedent(source).strip() + "\n",
    }
    if kind == "code":
        value.update(execution_count=None, outputs=[])
    CELLS.append(value)


cell(
    "markdown",
    """
# 00. 지난 결정을 다시 설명하지 않고 작업 이어가기

새 대화에서 AI에게 **“legacy callback 코드를 이제 삭제해도 될까?”**라고 물으려 합니다.
지난 결정은 문서에, 실제 동작은 Python에, 배포 값은 JSON에 흩어져 있습니다.
그 셋을 다시 찾아 설명하는 대신, **memtomem으로 근거를 찾아 작업에 가져오는 경험**을 해봅니다.

- **첫 성공:** 150개 파일을 색인하고 결정·구현·운영 설정을 출처와 함께 찾습니다.
- **더 체험하기:** 헷갈리는 재시도 정책 → 새 프로세스에서 작업 재개 → 원본 변경 → 내 결정 저장.
- **대상:** AI 코딩 도구 사용자. Python 코드를 수정하지 않고 Run All로 시작할 수 있습니다.
- 데이터는 가상의 협업 SaaS **Slateharbor**입니다. 실제 고객 정보나 운영 설정이 아닙니다.

## 준비: 노트북과 샘플 폴더를 함께 받으세요

[묶음 준비 안내](../onboarding/slateharbor/README.md)를 따라 저장소 또는 배포 ZIP을 풉니다.
이 노트북만 다운로드하면 샘플이 없어 실행되지 않습니다. 폴더 구조를 유지하세요.

Python **3.12 이상**, 설치할 때만 인터넷이 필요합니다. API 키·임베딩 모델·외부 DB는 필요 없습니다.
터미널에서 묶음의 루트로 이동한 뒤:

```bash
uv venv .venv
uv pip install --python .venv/bin/python "memtomem[code]>=0.5.0" jupyterlab ipykernel
uv run --python .venv/bin/python --no-project jupyter lab examples/notebooks/00_start_here.ipynb
```

0.5.0 이상이면 됩니다. 저장소를 받았다면 대신
`uv pip install --python .venv/bin/python -e "packages/memtomem[code]" jupyterlab ipykernel`로
소스에서 설치해도 같습니다. 실제로 검증한 버전은 [VALIDATION.md](../onboarding/slateharbor/VALIDATION.md)에 적혀 있습니다.

Windows는 `.venv/bin/python`을 `.venv/Scripts/python.exe`로 바꿉니다.
Jupyter에서 위 환경의 Python 커널을 선택하고 **Run All**을 실행하세요.
`[code]`는 Python 함수 단위 분석을 위한 패키지이며 LLM이나 임베딩 모델이 아닙니다.
""",
)
cell(
    "code",
    """
try:
    import memtomem
    import tree_sitter
    import tree_sitter_python
except (ImportError, ModuleNotFoundError) as exc:
    import sys
    raise RuntimeError(
        f'''이 커널이 쓰는 Python: {sys.executable}
바로 이 Python에 설치하세요:
  uv pip install --python "{sys.executable}" "memtomem[code]>=0.5.0" jupyterlab ipykernel
이미 설치했다면 커널이 다른 환경입니다. Jupyter 오른쪽 위에서 커널을 바꾸세요.'''
    ) from exc

import sys
from importlib.metadata import version
print('커널 Python:', sys.executable)
print('Python:', sys.version.split()[0], '| memtomem:', version('memtomem'))
""",
)
cell(
    "markdown",
    """
## 1. 실행만 하면 되는 준비 셀

실제 홈이나 프로젝트 대신 **임시 복사본**에서 실행합니다. 원본 150개 파일은 보존합니다.
도우미 `lab.py`는 명령마다 새 CLI 프로세스를 열며, 정답 파일은 읽지 않습니다.
설정이 궁금하면 [lab.py](../onboarding/slateharbor/lab.py)를 열어볼 수 있습니다.
""",
)
cell(
    "code",
    """
import atexit
import importlib.util
import json
import uuid
from pathlib import Path
from IPython.display import Markdown, display

candidates = [Path.cwd(), *list(Path.cwd().parents)[:3]]
sample = next((p / 'examples/onboarding/slateharbor' for p in candidates
               if (p / 'examples/onboarding/slateharbor/lab.py').is_file()), None)
if sample is None:
    raise FileNotFoundError('노트북과 Slateharbor 폴더가 함께 필요합니다. 묶음 루트에서 Jupyter를 다시 여세요.')
spec = importlib.util.spec_from_file_location('slateharbor_lab', sample / 'lab.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if 'lab' in globals():
    lab.close()  # 준비 셀을 다시 실행해도 이전 실습 상태를 남기지 않습니다.
lab = module.Lab(sample)
atexit.register(lab.close)
print('실습용 복사본:', lab.project)
""",
)
cell(
    "markdown",
    """
## 2. 단편 메모가 아니라, 한 프로젝트의 기록

인증·결제·알림·백그라운드 작업·파일·리포트의 **12주간 기록**입니다.
현재 결정, 폐기된 6월 정책, 아직 승인되지 않은 제안, 환경별 설정이 함께 있습니다.

샘플 경로를 클릭하거나 파일 브라우저에서 열어보세요:

- [인증 결정](../onboarding/slateharbor/project/auth/docs/decisions/decision-current.md)
- [인증 구현](../onboarding/slateharbor/project/auth/src/policy.py)
- [인증 운영 설정](../onboarding/slateharbor/project/auth/config/production.json)

아래 파일 수는 실제 폴더에서 셉니다. 아직 파일을 읽거나 외운 상태로 가정하지 않습니다.
""",
)
cell(
    "code",
    """
from collections import Counter
files = [p for p in lab.project.rglob('*') if p.is_file()]
formats = Counter(p.suffix for p in files)
print('업무 영역:', ', '.join(sorted(p.name for p in lab.project.iterdir())))
print('원본 파일:', len(files), '| 형식별:', dict(sorted(formats.items())))
assert len(files) == 150
""",
)
cell(
    "markdown",
    """
## 3. 한 번 색인하기 — 이후에는 필요한 부분만 찾기

기본 실습은 **키워드 검색(BM25)**입니다. 한국어 질문에서 실제 식별어인 `legacy callback`을 골라 검색합니다.
표현이 완전히 달라도 찾아주는 의미 검색은 이 실습의 검증 범위가 아닙니다.

독립된 정책 제목·함수·설정 키를 검색 단위로 유지하려고 실습에서는 인접 청크 병합을 끕니다
(`min_chunk_tokens=0`, `target_chunk_tokens=0`). 실제 개수는 실행 버전에 따라 달라질 수 있습니다.
""",
)
cell(
    "code",
    """
# 이미 색인한 뒤 이 셀만 다시 실행해도 동일 원본을 안전하게 재확인합니다.
lab.index()
inventory = lab.inventory()
rows = ['| 형식 | 파일 | 실제 청크 |', '|---|---:|---:|']
rows += [f"| {ext} | {item['files']} | {item['chunks']} |"
         for ext, item in sorted(inventory['formats'].items())]
display(Markdown('\\n'.join(rows)))
print(f"총 {inventory['files']}파일 / {inventory['chunks']}청크 / 색인 {inventory['index_seconds']}초")
assert inventory['files'] == 150 and 800 <= inventory['chunks'] <= 1500
""",
)
cell(
    "markdown",
    """
## 4. 첫 성공: “legacy callback을 삭제해도 될까?”

먼저 전체 검색으로 단서를 찾고, **결정 → 구현 → 운영 설정** 순으로 확인합니다.
파일을 미리 정답으로 선택하지 않고, 각 종류의 모든 업무 영역에서 검색합니다.
`source_filter`는 경로에 포함될 파일명 문자열로 검색 범위를 지정합니다. 같은 이름의 모든 업무 영역을 검색합니다.

아래는 LLM 답변이 아니라 **실제 파일에서 검색된 내용**입니다.
도우미는 0.5.0의 출처 필터 적용 순서에 대응해 최대 100개 후보를 요청한 뒤 상위 결과만 표시합니다.
이는 이 실습의 검색 설정이며 기본 설정의 검색 품질 측정은 아닙니다.
""",
)
cell(
    "code",
    """
QUERY = 'legacy callback'
hits = lab.search(QUERY)
print('전체 검색에서 찾은 출처:')
for source in lab.sources(hits):
    print(' •', source)

for title, pattern in [('채택된 결정', 'decision-current.md'),
                       ('Python 구현', 'policy.py'),
                       ('production 설정', 'production.json')]:
    matches = lab.search(QUERY, source=pattern)
    assert matches, f'{title}: 검색 결과 없음'
    display(Markdown(f'### {title}\\n\\n' + lab.context(QUERY, source=pattern, top_k=1)))
""",
)
cell(
    "markdown",
    """
### 이제 작업 요청에 무엇을 함께 줄 수 있나요?

찾은 내용에서 **구형 클라이언트 호환성**, **legacy route 유지**, **AUTH_CALLBACK_V2_ENABLED의 범위**를 확인하세요.
“삭제 가능 여부를 검토해줘”라는 요청에 결정·코드·설정의 근거를 붙일 수 있게 됐습니다.
검색 순위만으로 삭제를 승인하지 않고, 원본 상태와 근거를 읽어 판단합니다.

**여기까지가 첫 체험입니다.** 아래부터는 기록이 복잡해졌을 때의 활용입니다.
""",
)
cell(
    "markdown",
    """
## 5. “재시도는 몇 번이었지?” — 비슷한 기록 구분하기

`retry`만 검색하면 알림·결제·작업 재시도와 과거 기록이 섞일 수 있습니다.
검색어를 `notification retry policy`로 좁히고, 현재 production과 과거 설정을 따로 확인합니다.
파일이 최근 검색됐다는 사실이 최신 정책이라는 뜻은 아닙니다.
""",
)
cell(
    "code",
    """
print('넓은 검색어 retry의 상위 출처:')
for source in lab.sources(lab.search('retry', top_k=8)):
    print(' •', source)
for title, pattern in [('현재 운영', 'production.json'),
                       ('대체된 6월 설정', 'history-june.json'),
                       ('장애 때 배운 점', 'review.md')]:
    display(Markdown(f'### {title}\\n\\n' + lab.context('notification retry policy', source=pattern, top_k=1)))
""",
)
cell(
    "markdown",
    """
## 6. 새 프로세스에서 작업 이어가기

이번에는 **worker lease recovery** 작업을 이어받습니다. 인계 문서에서 이미 결정된 것과 다음 작업을 찾습니다.
두 검색은 별도 CLI 프로세스입니다. 이 검사는 파일 기억의 재조회이며, 실제 AI 새 대화 검사는 마지막 단계에서 합니다.
""",
)
cell(
    "code",
    """
first = lab.search('worker lease recovery', source='handoff-current.md')
second = lab.search('worker lease recovery', source='handoff-current.md')
assert first and lab.sources(first) == lab.sources(second)
assert [h['content'] for h in first] == [h['content'] for h in second]
handoff = lab.context('worker lease recovery', source='handoff-current.md', top_k=1)
display(Markdown(handoff))
print('새 CLI 프로세스에서도 같은 인계 내용과 원본 경로를 찾았습니다.')
""",
)
cell(
    "markdown",
    """
## 7. 원본을 바꾸면 검색도 바뀔까?

임시 production JSON에서 알림 재시도 횟수만 바꿔봅니다. Markdown 결정과 구현 계약까지 바꾼 것은 아닙니다.
이 불일치는 **원본을 확인하고 변경 범위를 검토해야 하는 이유**이기도 합니다.
아래 셀은 결과 확인 후 원래 설정으로 복구·재색인합니다.
""",
)
cell(
    "code",
    """
RETRY_LIMIT = 4  # 실습 복사본에서만 바꾸는 값
policy_path = lab.project / 'notifications/config/production.json'
original = policy_path.read_text(encoding='utf-8')
try:
    config = json.loads(original)
    config['retry_attempts']['value'] = RETRY_LIMIT
    config['retry_attempts']['revision'] = '2026-08-25-lab'
    policy_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    lab.run('index', str(policy_path))
    updated = lab.context('notification retry policy', source='production.json', top_k=1)
    assert f'"value": {RETRY_LIMIT}' in updated and '2026-08-25-lab' in updated
    display(Markdown(updated))
finally:
    policy_path.write_text(original, encoding='utf-8')
    lab.run('index', str(policy_path))
""",
)
cell(
    "markdown",
    """
## 8. 내가 다시 설명하기 싫은 결정 한 개

아래 **세 값만** 자신의 비밀이 아닌 예시로 바꾸세요. QUERY는 저장 문장에 들어 있는 단어를 사용합니다.
이 셀은 실행할 때마다 메모를 **새로 추가**합니다. 그래서 저장할 때 이번 저장만 가리키는 태그를
하나 더 붙이고, 확인 검색은 그 태그로 한정합니다 — 값을 고쳐 다시 실행해도 직전 메모가 아니라
방금 저장한 것이 표시됩니다. `personal-lab` 태그로 검색하면 이 실습에서 저장한 메모가 모두 나옵니다.
""",
)
cell(
    "code",
    """
DECISION = 'nightly export는 야간 배치로 실행한다.'
REASON = '낮에는 대화형 리포트 요청의 응답 시간을 지키기 위해서다.'
QUERY = 'nightly export'

this_save = 'save-' + uuid.uuid4().hex[:8]  # 이번 저장만 가리키는 태그
saved = json.loads(lab.run('add', f'개인 실습 결정: {DECISION} 이유: {REASON}',
                           '--tags', f'personal-lab,{this_save}', '--json'))
assert saved['ok'] and saved['chunks'] > 0
assert lab.search(QUERY, tag=this_save), '저장 문장에 들어 있는 단어로 QUERY를 바꿔보세요.'
display(Markdown(lab.context(QUERY, top_k=1, tag=this_save)))
print('이 실습에서 저장한 메모 수:', len(lab.search('개인 실습 결정', top_k=50, tag='personal-lab')))
""",
)
cell(
    "markdown",
    """
## 9. 실습 정리

임시 파일은 다음 셀에서 삭제합니다. 평소 AI 도구에서 이 실습 기억이 보이지 않는 것이 정상입니다.
커널을 강제로 종료하면 임시 디렉터리가 남을 수 있습니다. 그 경우 준비 셀이 표시한 실습 경로만 확인하세요.
""",
)
cell(
    "code",
    """
temporary = lab.root
lab.close()
assert not temporary.exists()
module.verify_sources(sample)
print('SLATEHARBOR COMPLETE — 임시 상태를 정리했고 배포 원본 150개 파일은 그대로입니다.')
""",
)
cell(
    "markdown",
    """
## 10. 실제 AI 코딩 도구에서 이어가기 — 선택 단계

이제는 **본인이 선택한 저장소에 샘플 파일을 색인하는 별도 작업**입니다.
[한국어 첫 설정 가이드](https://github.com/memtomem/memtomem/blob/main/docs/guides/vibe-coding-getting-started-ko.md)에서 도구 하나만 연결하세요.
그 뒤 번들의 실제 `examples/onboarding/slateharbor/project` 절대 경로를 사용합니다.

### Claude Code

```text
/memtomem:index /절대/경로/examples/onboarding/slateharbor/project
/memtomem:search legacy callback
```

### Codex

```text
$memtomem-index 스킬로 /절대/경로/examples/onboarding/slateharbor/project만 색인해줘.
$memtomem-search 스킬로 "legacy callback"을 검색해줘.
```

그다음 **새 대화**를 열고 아래 요청만 붙여보세요.

```text
Slateharbor의 legacy callback 삭제 가능성을 검토하려고 합니다. 아직 코드를 수정하지 마세요.
memtomem 검색 도구로 "legacy callback"을 찾고,
현재 결정·Python 구현·production JSON을 구분해 원본 경로와 근거를 보여주세요.
과거 기록이나 검토 중 제안을 현재 결정으로 취급하지 마세요.
근거가 부족하면 부족하다고 말하고, 확인한 내용으로 다음 작업만 제안하세요.
```

**성공 기준:** 실제 검색 도구 호출이 보이고, 결정·구현·운영 설정의 출처를 새 대화에서도 찾습니다.
답변이 비슷하다는 것만으로 통과로 보지 않습니다. 기본 노트북은 이 실제 AI 단계를 자동 실행하지 않습니다.

### 내 프로젝트에 가져갈 때

항상 지킬 규칙은 AGENTS.md/CLAUDE.md에, 다시 찾아볼 결정과 근거는 memtomem에 둡니다.
처음에는 ADR·장애 회고처럼 가치 있는 작은 폴더 하나로 시작하세요. 전체 대화 자동 저장은 이 체험에 포함되지 않습니다.

### 막혔을 때

- import 실패: 설치한 Python과 Jupyter 커널이 같은지 첫 셀 출력을 확인하세요.
- 검색 결과 없음: 실제 파일의 식별어를 사용하고 `source_filter` 범위를 확인하세요.
- 샘플 변경 오류: 배포 폴더는 원본으로 복원하고, 실습은 임시 복사본에서 진행하세요.
- 실제 AI 결과 불일치: 연결된 저장소와 색인 경로를 확인하세요. 임시 실습 데이터는 자동으로 옮겨지지 않습니다.

더 배우기: [01 Python API](https://github.com/memtomem/memtomem/blob/main/examples/notebooks/01_hello_memory.ipynb) · [05 LangGraph 기초](https://github.com/memtomem/memtomem/blob/main/examples/notebooks/05_langgraph_memory_basics.ipynb)
""",
)


def notebook():
    return {
        "cells": CELLS,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
            "memtomem": {"profile": "minimal-code", "corpus": "slateharbor-1"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps(notebook(), ensure_ascii=False, indent=1) + "\n"
    if args.check:
        assert TARGET.read_bytes() == content.encode("utf-8"), "Notebook regeneration drift"
        print(f"PASS reproducible notebook ({len(CELLS)} cells)")
    else:
        TARGET.write_bytes(content.encode("utf-8"))
        print(f"Generated {TARGET.name} ({len(CELLS)} cells)")


if __name__ == "__main__":
    main()

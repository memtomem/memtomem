# 프로젝트 기억을 활용하는 세 가지 업무 패키지

개발 작업을 이어가고, 제품 결정의 이유를 찾고, 처음 맡은 기능을 이해합니다.
대상은 개발자·PM·기획자입니다. 별도 앱을 설치하는 대신 기존 memtomem CLI·MCP·웹 UI와
Markdown 양식을 사용합니다. 모든 예제 조직·결정·관찰은 **합성 자료**입니다.

| 패키지 | 질문 | 결과물 |
|---|---|---|
| [개발 작업 이어가기](handoff/README.md) | 어디까지 했고 다음에 무엇을 하지? | 검증 결과와 다음 행동이 있는 인계 기록 |
| [제품 의사결정 기록장](decisions/README.md) | 왜 결정했고 지금도 유효한가? | 결정·대안·근거·변경 기록 |
| [프로젝트 이해·온보딩](onboarding/README.md) | 이 기능은 어떻게, 왜 이렇게 동작하지? | 관련 파일과 미확인 사항이 있는 작업 준비 메모 |

결정 자료를 세 패키지에서 재사용합니다. 리서치·강의 준비·고객 미팅·실험 기록·장애 해결·집필·
사용자 인터뷰·성과 기록·학습 노트는 후속 후보이며 이번 패키지에는 구현하지 않습니다.

## 1. 모델 없이 먼저 체험하기

저장소 전체를 내려받아 **저장소 루트**에서 실행합니다. 이 폴더만 복사하면 안 됩니다.
[Slateharbor의 150개 자료와 격리 실행기](../onboarding/slateharbor/README.md)를 함께 사용합니다.
Python 3.12 이상과 uv가 필요합니다. 최초 설치에는 인터넷이 필요하지만 아래 검색은
API 키·임베딩 모델·LLM·외부 DB를 사용하지 않습니다.

```bash
uv venv .venv-workflows --python 3.12
uv pip install --python .venv-workflows/bin/python -e 'packages/memtomem[code]'
.venv-workflows/bin/python examples/workflows/demo.py --workflow all
```

Windows에서는 Python 경로를 `.venv-workflows/Scripts/python.exe`로 바꾸세요.
기존 memtomem 개발 환경이 있다면 해당 Python으로 실행해도 됩니다.
이 경우에도 Core는 설치된 다른 버전이 아니라 이 체크아웃의 소스를 사용합니다.
기본 체험은 `handoff`, `decisions`, `onboarding`별 실제 검색 결과와 출처를 출력합니다.

실행기는 샘플을 임시 폴더로 복사하고 별도 저장소를 초기화합니다. 기존 사용자 설정이나
MCP 등록을 변경하지 않으며, 종료하면 임시 자료와 색인을 정리합니다. 검색마다 새 CLI 프로세스를
실행합니다. 출력의 임시 경로는 종료 후 사라지므로 **실제 작업용 자료로 인용하지 마세요**.

BM25 검색이므로 먼저 예제의 영문 식별어로 검색합니다. 한국어 의미 검색과 AI 초안 작성은
[임베딩 설정](../../docs/guides/embeddings.md)과 [AI 도구 연결](../../docs/guides/mcp-clients.md)을
완료한 뒤 별도로 확인합니다. CLI 검색 성공은 AI 응답 품질이나 생산성 향상을 증명하지 않습니다.

## 2. 내 자료로 반복 사용하기

1. [기본 설정](../../docs/guides/getting-started.md)을 마칩니다. 기존 사용자는 저장소를 초기화하지 않습니다.
2. 원하는 패키지의 양식을 **본인이 선택한 문서 폴더**에 복사해 실제 내용으로 채웁니다.
3. 작성한 문서 폴더만 명시적으로 색인하고 검색합니다.

```bash
mm index /path/to/your/project-notes
mm search "your exact topic" --format context
```

4. 결과의 원문을 열어 상태·근거를 확인하고 업무에 사용합니다. 새 판단은 검토한 뒤 기록합니다.
5. 원문이 바뀌면 같은 경로를 다시 색인합니다. 다음 주 다른 작업에서 재사용했는지 기록합니다.

원하면 `mm web`의 검색·출처 화면이나 MCP를 사용합니다. 개인 메모는 개인 또는 project_local
범위에 두고, 팀에 전달할 문서는 검토한 Git 자료로 공유해 각자 색인합니다.
[범위 설명](../../docs/guides/use-cases.md)을 따르며 SQLite DB를 공동 파일로 동기화하지 않습니다.
다중 사용자 서버·실시간 공동 편집·회의 자동 수집은 포함하지 않습니다.

검색 결과 없음은 질문의 답이 없다는 증명이 아닙니다. 색인 경로와 식별어를 확인하고,
자료가 없는 부분은 미확인으로 남깁니다. 같은 문서가 도구마다 다르게 보이면 `mem_status`로
저장소와 프로젝트 문맥부터 비교합니다. 정리할 때는 직접 만든 실습 자료만 선택합니다.

## 3. 검증과 사용자 시험

```bash
.venv-workflows/bin/python examples/workflows/demo.py --validate
```

검증은 패키지별 10개, 총 30개 검색 과제의 기대 출처·본문과 저장 후 새 프로세스 검색,
복사본 수정·재색인, 다른 저장소 격리, 원본 보존·임시 폴더 정리를 확인합니다.
[evaluation.json](evaluation.json)은 평가자용이며 색인하지 않습니다.
인계·온보딩은 파일 **종류** 필터, 의사결정은 `product/` **폴더 범위**를 사용하는 경로를
평가합니다. 현재 결정 한 개만 골라 검색하지 않으며 제품 기본 검색의 일반 벤치마크가 아닙니다.
평가 질문은 30개이고, 서로 다른 `(query, source_filter)` 검색 경로는 25개입니다.
예를 들어 `free_trial` 검색 결과로 현재·이전·제안·근거·적용 범위·대체 관계를 따로 평가합니다.
같은 파일의 다른 정책도 여러 과제에서 확인합니다. 기대한 정책 값·다음 행동·코드 조건이 **같은 검색 청크**에 있어야
통과하며, 출력의 `expected_source_files`에 중복을 제외한 기대 원문 파일 수를 표시합니다.

현재 결정의 올바른 해석, 원문 충돌의 인지, 실제 AI 도구의 새 세션 동작은 사람이 별도로
확인합니다. [파일럿 운영 안내](pilot/README.md)와 [사용 일지](pilot/participant-log.md)를 사용하세요.
이번 배포에는 사용자 모집·실사용 결과가 포함되어 있지 않습니다.
실행 환경과 확인한 범위는 [로컬 검증 기록](VALIDATION.md)에 남깁니다.

## 유지보수: 합성 자료 변경

제품 자료의 파일 목록과 바이트 해시는 [manifest.json](manifest.json)으로 고정합니다.
의도하지 않은 변경은 원본을 복구하세요. 검토한 자료 변경을 배포할 때만 아래 명령으로
manifest를 갱신한 뒤 `demo.py --validate`와 테스트를 실행합니다. 양식과 평가 파일은 색인하지 않습니다.

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from examples.workflows.demo import product_snapshot
root = Path('examples/workflows')
files = product_snapshot(root / 'project')
(root / 'manifest.json').write_text(json.dumps({'files': files}, indent=2) + '\n', encoding='utf-8', newline='\n')
PY
```

CI는 이 실행기와 테스트를 린트하고, 모델 없는 최소 설치 환경에서 `demo.py --validate`를 실행합니다.
검증 진행 로그는 stderr, 최종 JSON 보고서는 stdout으로 출력합니다.

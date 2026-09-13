# 새 세션에서 프로젝트 결정 다시 찾기

**대상:** 코딩 에이전트·바이브 코딩 사용자. 모든 데이터는 가상이며 실제 고객 사례가 아닙니다.
이 작은 프로젝트는 재시도 정책과 legacy callback 유지 근거를 제공합니다.
항상 지켜야 할 규칙은 AGENTS.md/CLAUDE.md에 두고, memtomem은 과거 결정과 근거를 명시적으로 찾아볼 때 사용합니다.

## 1. 코드 없이 먼저 확인할 것

설치·플러그인 연결은 [기존 한국어 첫 성공 가이드](../../../docs/guides/vibe-coding-getting-started-ko.md)를 따르세요.
최초 개인 선호 저장·검색을 완료한 뒤 이 예제로 돌아옵니다.
영어 설치 안내는 [Getting Started](../../../docs/guides/getting-started.md)입니다.

## 2. 샘플 테스트

이 폴더에서 실행합니다. 표준 라이브러리만 사용하며 서버·API가 없습니다.

```bash
python -m unittest discover -s . -p test_policy.py
```

기대 결과는 테스트 1개와 `OK`입니다. 이 테스트는 샘플의 상수만 검사하며 실제 서비스 안전성을 보장하지 않습니다.

## 3. 완전히 격리된 CLI 검증

Python 3.12 이상과 uv가 필요합니다. 최초 패키지 설치만 인터넷을 사용하며 임베딩 모델은 내려받지 않습니다.

```bash
uv venv .venv
uv pip install --python .venv/bin/python "memtomem==0.6.1"
uv run --python .venv/bin/python --no-project python demo.py
```

Windows에서는 `.venv/Scripts/python.exe`를 사용합니다.
출력은 `PASS empty-store`, `PASS decision-round-trip`, `PASS adr-source`,
`PASS fixture-preserved-and-state-cleaned`입니다.
임시 저장소만 사용하므로 실행 뒤 평소 AI 클라이언트에서는 이 기억이 보이지 않는 것이 정상입니다.

## 4. 실제 코딩 클라이언트에서 따라 하기

이 단계는 본인 저장소에 **가상 실습 기억을 명시적으로 쓰는** 별도 작업입니다.
실제 프로젝트 규칙을 저장하려면 먼저 기존 가이드의 project_local 초기화 절차를 따르세요.
플러그인 설치만으로 이 프로젝트나 대화 전체가 색인되지 않습니다.

Claude Code:

```text
/memtomem:remember 실습 프로젝트의 Retry policy: retry at most 5 times with 250 ms backoff and jitter. Reason: avoid a retry storm during staged rollout.
/memtomem:search Retry policy
```

Codex:

```text
$memtomem-remember 스킬로 가상 실습 결정을 저장해줘: Retry policy: retry at most 5 times with 250 ms backoff and jitter. Reason: avoid a retry storm during staged rollout.
$memtomem-search 스킬로 "Retry policy"를 찾고 원본 경로를 보여줘.
```

다른 MCP 클라이언트에서는 같은 내용으로 `mem_add`를 호출한 뒤 `mem_search`를 명시적으로 요청하세요.
저장된 원본 경로를 기록한 다음 **새 대화/새 thread**에서 검색 지시문만 다시 실행합니다.
결정·이유·같은 원본 경로를 찾으면 성공입니다. 답변만 비슷하거나 도구 호출이 없으면 미검증입니다.

## 5. ADR로 작업 판단하기

`docs/auth-callback-adr.md`만 명시적으로 색인하세요. 홈 전체나 실제 비밀 문서를 색인하지 않습니다.

```bash
mm index docs/auth-callback-adr.md
mm search "legacy callback" --format context
```

그다음 코딩 에이전트에 요청합니다.

> 코드를 아직 수정하지 마세요. memtomem에서 "legacy callback"을 검색하고,
> 유지 이유와 rollback flag를 원본 경로와 함께 알려주세요.
> 그 근거를 확인한 다음에만 변경안을 제안하세요.

기대 근거는 `/api/auth/legacy-callback`, 오래된 클라이언트 호환성,
`AUTH_CALLBACK_V2_ENABLED`와 ADR 원본입니다. 자동으로 실수가 줄었다고 판정하지 않습니다.

## 실패 복구와 연습

- 검색 결과 없음: 실제 저장한 단어로 검색하고 status의 DB 경로를 비교합니다.
- 다른 클라이언트 결과 불일치: 같은 저장소·namespace·프로젝트 문맥인지 확인합니다.
- 임시 demo 결과가 평소 저장소에 없음: 의도된 격리입니다. 4단계는 별도 실행하세요.
- 실습 기억 정리: 저장 시 기록한 실습 원본만 확인해 삭제합니다. 저장소 전체 reset은 하지 않습니다.
- **연습:** ADR에 새로운 이유를 추가하고 같은 파일을 다시 색인해 검색 결과를 확인하세요.
- **힌트:** 수정한 파일을 `mm index`로 다시 색인합니다. 원본 파일이 기준입니다.

LangGraph 사용자는 [노트북 목록](../../notebooks/README.md)의 한국어 05·06을 보세요.

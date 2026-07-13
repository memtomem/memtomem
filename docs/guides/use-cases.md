# memtomem 0.3.8 사용 시나리오

잠재 사용자가 현재 기능의 가치를 이해하고 직접 확인할 수 있도록 구성한
마케팅·실습 겸용 가이드입니다. 아직 출시되지 않은 자동 기억 형성이나
시간 그래프는 전제로 삼지 않습니다. 먼저 `mm init`, `mm status`가
성공하는지 확인하고 개인 또는 테스트 데이터만 사용하세요.

## 1. 여러 AI 코딩 도구를 함께 쓰는 개발자

### Claude Code의 결정을 Codex에서 이어서 사용하기

도구를 바꾸면 이전 세션의 결정을 다시 설명해야 합니다.

```bash
mm add "배포는 blue-green 방식을 사용하고 전환 전 smoke test를 실행한다" \
  --tags deployment,decision
mm search "배포 전환 방식"
```

Claude Code와 Codex에 같은 MCP 서버를 등록하고 각각 `mem_search`를
호출합니다. 동일한 source와 내용이 반환되면 성공입니다. 기억은 특정
에이전트 DB가 아니라 사용자가 소유한 Markdown에 남습니다.

### Skill 하나를 Claude Code·Codex·Kimi에 동기화하기

같은 리뷰 규칙을 runtime별 폴더에 복사하면 내용이 달라집니다.

```bash
mm context detect --include=skills
mm context init --include=skills --only reviewer \
  --scope project_shared --confirm-project-shared
mm context diff --include=skills --scope project_shared
mm context sync --include=skills --scope project_shared
```

마지막 diff에서 누락·불일치 runtime이 사라지면 성공입니다. Store의
master만 편집하고 runtime copy는 생성 결과로 취급합니다.

### 선택형 STM으로 도구 사용 시 관련 기억 받기

검색 호출 자체를 잊는 사용자는 별도 `memtomem-stm`을 연결할 수 있습니다.
과거 결정과 관련된 build·deploy tool을 호출했을 때 관련 기억이 함께
surface되면 성공입니다. STM은 선택 기능이며 core는 명시적 호출 모델을
유지합니다.

## 2. 개인 Markdown·코드 지식을 검색하는 개발자

### 기존 노트와 코드 폴더를 한 번에 검색하기

```bash
mm index ~/notes
mm index ~/projects/example/src
mm search "재시도 정책과 백오프 구현"
```

Markdown heading과 Python 함수가 함께 나타나면 성공입니다. 원본 파일은
유지되고 DB는 재생성 가능한 검색 캐시입니다.

### 한국어 의미와 정확한 식별자를 동시에 찾기

```bash
mm init --preset korean --non-interactive
mm search "연결 실패를 점진적으로 재시도하는 방법"
mm search "MAX_RETRY_COUNT RetryPolicy"
```

첫 검색은 관련 의미, 두 번째는 정확한 식별자를 찾는지 확인합니다.
BM25와 dense 결과는 RRF로 결합됩니다.

### 증분 재색인과 유지보수 확인하기

```bash
mm index ~/notes
mm index ~/notes
mm status --json
mm web
```

두 번째 실행에서 변경되지 않은 chunk가 skip되고, Web에서 source·중복·
age-out 상태를 확인하면 성공입니다.

## 3. 개발팀과 조직

### 프로젝트 기억과 에이전트 지침을 Git으로 온보딩하기

```bash
mm add "운영 DB migration은 expand-contract 순서를 따른다" \
  --scope project_shared --confirm-project-shared
mm context sync --include=skills,commands,agents --scope project_shared
git status --short .memtomem
```

`.memtomem/`의 검토 가능한 파일만 commit합니다. 팀원이 clone/pull 후
같은 검색 결과와 runtime 설정을 얻으면 성공입니다.

### 팀 공유와 개인 초안을 분리하기

```bash
mm add "검토 중인 개인 배포 아이디어" --scope project_local
mm add "승인된 팀 배포 규칙" \
  --scope project_shared --confirm-project-shared
```

`project_local`은 gitignored draft이며 runtime fan-out 대상이 아닙니다.
`project_shared`만 Git 변경으로 표시되는지 확인합니다. 비밀정보는 shared
tier에서 `--force-unsafe`로 우회할 수 없습니다.

### 공용 Skill을 여러 프로젝트에 버전 고정하기

```bash
mm wiki init
mm wiki skill new reviewer --editor
mm wiki skill lint reviewer
mm wiki skill commit reviewer --canonical
mm context install skill reviewer
mm context diff --include=skills --scope project_shared
mm context sync --include=skills --scope project_shared
```

프로젝트 lock metadata에 Wiki commit이 기록되고 변경 전까지 설치 snapshot이
유지되면 성공입니다. 업데이트는 `mm context update`로 명시합니다.

## 확인 가능한 현재 강점

- 파일과 Git이 source of truth이고 DB는 재생성 가능한 캐시입니다.
- BM25-only부터 로컬 ONNX·한국어 preset까지 선택할 수 있습니다.
- MCP, CLI, Web이 같은 저장소와 privacy/scope 계약을 사용합니다.
- Context Gateway가 Skill·Command·Agent를 여러 runtime에 동기화합니다.
- 0.3.8 Web API는 exact source, chunk type, timezone-aware date filter와
  readiness/503 계약을 제공합니다.

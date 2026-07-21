# Claude Code·Codex CLI 바이브코딩 빠른 시작

**Audience**: Claude Code 또는 Codex CLI를 쓰지만 MCP·임베딩·RAG는
아직 익숙하지 않은 사용자

**Goal**: 10~15분 안에 기억 하나를 저장하고, 새 세션이나 다른
코딩 도구에서 출처와 함께 다시 찾기

memtomem은 AI가 이전 대화와 프로젝트 결정을 세션 너머에서도 찾아
쓸 수 있게 해 주는 Markdown-first 장기 기억 도구입니다. 이 가이드는
Claude Code와 Codex CLI에 제공되는 플러그인으로 가장 작은 성공을 만듭니다.

## 먼저 알아둘 세 가지

| 놓을 곳 | 적합한 내용 | 특징 |
|---|---|---|
| 현재 대화 | 지금 풀고 있는 문제, 임시 아이디어 | 새 세션에서 사라질 수 있음 |
| `CLAUDE.md` / `AGENTS.md` | 반드시 지켜야 할 프로젝트 규칙과 검증 명령 | 작업 시작부터 적용되는 지침 |
| memtomem | 과거 결정, 조사 결과, 반복 절차 | 명시적으로 저장·검색하며 여러 도구가 같은 저장소를 사용 가능 |

- **항상 지켜야 하는 규칙**은 `CLAUDE.md`나 `AGENTS.md`에 둡니다.
- **나중에 찾아볼 결정과 근거**는 memtomem에 둡니다.
- **아직 검토 중인 생각**은 대화에만 두고 확정된 뒤 저장합니다.

memtomem은 대화 전체를 자동으로 녹화하지 않습니다. 기본 플러그인에서
기억 저장과 인덱싱은 사용자가 요청했을 때만 실행됩니다.

## 준비

터미널에서 다음을 확인합니다.

```bash
uv --version
python3 --version
```

- Python 3.12 이상과 `uv`/`uvx`가 필요합니다.
- 처음에는 임베딩 모델이나 API key가 필요하지 않습니다.
- `provider=none` 또는 BM25-only 상태도 정상입니다.
- Claude Code와 Codex CLI 중 평소 쓰는 하나만 먼저 설정하세요.

## 경로 A: Claude Code

### 1. Claude Code 플러그인 설치

Claude Code 세션에서 실행합니다.

```text
/plugin marketplace add memtomem/memtomem
/plugin install memtomem@memtomem
/reload-plugins
```

`/reload-plugins`가 없다면 Claude Code를 종료하고 새 세션을 엽니다.

완전히 새 환경이라면 터미널에서 사용자 저장소를 한 번 초기화합니다.

```bash
uvx --from 'memtomem==0.3.12' mm init --preset minimal --non-interactive
```

프로젝트에만 둘 로컬 기억 계층이 필요할 때는 프로젝트 루트에서 별도로
초기화합니다.

```bash
uvx --from 'memtomem==0.3.12' mm mem init --scope project_local
```

### 2. 프로젝트 파일은 자동으로 들어오지 않음

플러그인을 설치했다고 기존 프로젝트 전체나 Claude Code의 내장 memory가
자동으로 모두 인덱싱되는 것은 아닙니다.

기본 플러그인은 사용자가 요청한 검색·저장·인덱싱 workflow만 실행합니다.
백그라운드에서 프로젝트를 감시하거나 대화 전체를 자동 저장하지 않습니다.

기존 프로젝트 문서는 처음 한 번 원하는 범위를 명시합니다.

```text
/memtomem:setup /path/to/project/docs
```

`setup`과 `/memtomem:index`는 **일회성 인덱싱**이며 해당 디렉터리를 감시
대상으로 등록하지 않습니다. 홈 디렉터리나 저장소 전체 대신 문서·ADR처럼
검색할 가치가 있고 secret이 없는 작은 경로부터 시작하세요.

파일 변경 자동 반영과 Claude Code 내장 memory 가져오기는 첫 기억 왕복을
마친 뒤 [Claude Code 심화 가이드](integrations/claude-code.md)의 automation과
built-in memory 절에서 별도로 설정하세요.

### 3. Claude Code 상태 확인

```text
/memtomem:status
```

다음을 확인하세요.

- storage와 database path가 표시된다.
- BM25-only 상태여도 경고가 아니다.
- 처음이라면 source와 chunk 수가 0이어도 정상이다.

database path는 뒤의 교차 도구 확인에 사용하므로 기록해 둡니다.

### 4. Claude Code에서 검증용 기억 저장

실제 비밀정보 대신 삭제해도 되는 문장으로 시험합니다.

```text
/memtomem:remember 이 프로젝트에서는 PR을 만들기 전에 uv run pytest를 실행한다.
```

저장된 파일과 indexed chunk 수가 표시되면 성공입니다. `remember`는
사용자가 명시적으로 요청해야 실행되는 write workflow입니다.

### 5. Claude Code에서 다시 검색

```text
/memtomem:search PR 전에 실행할 테스트
```

방금 저장한 문장과 source path가 나오는지 확인합니다. 결과가 없으면
`/memtomem:status`로 돌아갑니다.

### 6. 새 세션에서 확인

Claude Code를 새로 열고 다음처럼 요청합니다.

```text
memtomem에서 PR 전에 실행하기로 한 테스트를 출처와 함께 찾아줘.
```

새 대화에서도 같은 결정을 찾으면 Claude Code 설정은 끝입니다.

## 경로 B: Codex CLI

### 1. Codex 플러그인 설치

터미널에서 실행합니다.

```bash
codex plugin marketplace add memtomem/memtomem
codex plugin add memtomem@memtomem
```

설치 후 새 Codex thread를 시작해야 skill과 플러그인 MCP 서버가 로드됩니다.

```bash
codex
```

### 2. Codex 상태 확인

Codex에 다음처럼 요청합니다.

```text
Use $memtomem-status to inspect the current memory index.
```

storage, database path, source·chunk 수, 경고를 확인합니다. BM25-only는
정상입니다.

### 3. Codex에서 검증용 기억 저장

```text
Use $memtomem-remember to save this decision:
이 프로젝트에서는 PR을 만들기 전에 uv run pytest를 실행한다.
```

`remember`는 사용자가 명시적으로 요청하는 write workflow입니다. 기억할
내용이 불분명하면 Codex가 먼저 확인해야 합니다.

### 4. Codex에서 다시 검색

```text
Use $memtomem-search to find what test must run before a PR. Include the source.
```

저장한 문장과 source가 나오면 Codex CLI 설정은 끝입니다.

## Claude Code에서 저장하고 Codex에서 찾기

두 도구를 같은 컴퓨터와 같은 사용자 계정에서 실행하면 기본적으로
같은 memtomem 저장소를 볼 수 있습니다.

1. Claude Code의 `/memtomem:status`에서 database path를 확인합니다.
2. Codex의 `$memtomem-status`에서도 database path를 확인합니다.
3. 두 경로가 같으면 Claude Code에서 저장한 결정을 Codex에서 검색합니다.

```text
Use $memtomem-search to find the decision about tests before a PR.
Show the source path.
```

이 동작은 클라우드 동기화가 아닙니다. 다른 컴퓨터에서 사용하려면
[Multi-device sync](multi-device-sync.md)를 별도로 구성해야 합니다.

## 일할 때 자주 쓰는 요청

| 상황 | Claude Code | Codex CLI | 실제 동작 |
|---|---|---|---|
| 검색 상태 진단 | `/memtomem:status` | `Use $memtomem-status to inspect the index.` | `mem_status` |
| 과거 결정 검색 | `/memtomem:search 인증 모듈 교체 결정` | `Use $memtomem-search to find the authentication decision.` | `mem_search` |
| 최근 기억 확인 | `/memtomem:recall 지난 7일` | `Use $memtomem-recall to show memories from the last 7 days.` | `mem_recall` |
| 확정된 결정 저장 | `/memtomem:remember 재시도는 3회로 제한한다.` | `Use $memtomem-remember to save the retry limit.` | `mem_add` |

검색을 요청할 때는 **출처와 함께**라고 덧붙이세요. AI의 추측과 저장된
기억을 구분하고 원본을 다시 확인하기 쉬워집니다.

## 안전하게 기억하기

**저장하기 좋은 내용**

- 기술 선택과 그 이유
- 재현된 장애 원인과 검증된 해결 절차
- 배포·테스트·리뷰처럼 반복되는 체크리스트
- 다음 작업자가 확인해야 하는 출처 있는 사실

**저장하지 말아야 할 내용**

- API key, token, 비밀번호, 개인정보
- AI가 추측했지만 검증하지 않은 결론
- 곧 버릴 브레인스토밍과 임시 로그
- 대화 전체를 그대로 복사한 내용

```text
나쁜 예: 오늘 인증에 관해 얘기한 내용을 전부 기억해줘.

좋은 예:
- 결정: access token은 메모리에만 보관한다.
- 이유: 브라우저 영구 저장소 노출을 줄이기 위해서다.
- 검증: docs/security/auth.md의 위협 모델을 확인했다.
```

## 문제 해결

| 증상 | 먼저 할 일 |
|---|---|
| 플러그인을 찾지 못함 | marketplace 목록을 확인하고 다시 설치합니다. |
| workflow/skill이 보이지 않음 | Claude는 reload 또는 새 세션, Codex는 새 thread를 시작합니다. |
| MCP 서버가 시작되지 않음 | `uv --version`을 확인하고 `uvx`가 PATH에 있는지 봅니다. |
| 기존 프로젝트 파일이 검색되지 않음 | 기본 플러그인은 전체를 자동 인덱싱하지 않습니다. 원하는 경로에 `/memtomem:setup`을 실행합니다. |
| 검색 결과가 0건 | status에서 chunk 수를 보고, 저장한 문장의 정확한 키워드로 다시 검색합니다. |
| Claude와 Codex의 결과가 다름 | database path, `HOME`, `MEMTOMEM_*` 환경 변수를 비교합니다. |

상세한 진단은 [Operations & troubleshooting](reference/operations.md), 수동 MCP
등록은 [MCP Client Setup](mcp-clients.md)을 따르세요.

## 완료 체크

- [ ] Claude Code 또는 Codex CLI에서 status workflow가 실행된다.
- [ ] 기본 플러그인이 기존 프로젝트 전체를 자동 인덱싱하지 않음을 이해한다.
- [ ] BM25-only 상태를 오류로 오해하지 않는다.
- [ ] 검증용 기억 하나를 명시적으로 저장했다.
- [ ] 새 세션에서 같은 기억과 source를 찾았다.
- [ ] 두 도구를 쓴다면 database path가 같은지 확인했다.
- [ ] 비밀값, 대화 전체, 미검증 추측을 저장하지 않는다.

## 다음 단계

- CLI까지 포함한 설치: [Getting Started](getting-started.md)
- 도구별 수동 설정: [MCP Client Setup](mcp-clients.md)
- Claude Code 심화: [Claude Code integration](integrations/claude-code.md)
- Codex 심화: [Codex integration](integrations/codex.md)
- 기존 노트 인덱싱·저장 범위: [Core memory tools](reference/core-memory-tools.md)
- 임베딩·한국어 의미 검색: [Embeddings](embeddings.md)

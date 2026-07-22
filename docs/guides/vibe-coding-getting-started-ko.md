# Claude Code·Codex CLI 바이브코딩 빠른 시작

**대상**: Claude Code 또는 Codex CLI에서 코딩을 시작했고, memtomem은
처음 사용하는 사람

**목표**: 10~15분 안에 기억 하나를 저장하고, 새 세션에서도 출처와
함께 다시 찾기

memtomem은 AI가 이전 대화와 프로젝트 결정을 세션 너머에서도 찾아
쓸 수 있게 해 주는 Markdown-first 장기 기억 도구입니다. 이 가이드는
복잡한 검색 설정 없이 플러그인과 기본 키워드 검색만으로 첫 성공을 만듭니다.

## 이 가이드에서 할 일

1. `uvx`와 평소 쓰는 AI 도구 하나를 확인합니다.
2. 사용자 기억 저장소를 한 번 초기화합니다.
3. Claude Code 또는 Codex CLI 플러그인 하나만 설치합니다.
4. 개인 선호 하나를 저장하고 같은 단어로 검색합니다.
5. 새 세션에서 출처와 함께 다시 찾습니다.

처음에는 기존 프로젝트 전체를 인덱싱하거나 임베딩·자동화를 설정하지
않습니다. 작은 성공을 먼저 확인한 뒤 필요한 기능만 추가합니다.

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

## 1. 준비: 평소 쓰는 도구 하나만 확인

터미널에서 `uv`와 `uvx`를 확인합니다.

```bash
uv --version
uvx --version
```

명령을 찾지 못하면 [uv 공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를
따른 뒤 터미널을 새로 여세요. memtomem은 Python 3.12 이상 환경에서
실행되며, 첫 실행에는 정확히 고정된 패키지를 내려받을 네트워크 연결이
필요합니다. 임베딩 모델이나 API key는 필요하지 않습니다.

그다음 평소 쓰는 도구 **하나만** 확인합니다.

```bash
claude --version  # Claude Code를 쓸 때
codex --version   # Codex CLI를 쓸 때
```

아직 설치하지 않았다면 설치 과정을 이 문서에 중복하지 않고 공식 안내를
따릅니다.

- [Claude Code 공식 빠른 시작](https://code.claude.com/docs/en/quickstart)
- [Codex CLI 공식 문서](https://learn.chatgpt.com/docs/codex/cli)

설치와 로그인을 마친 뒤 이 페이지로 돌아오세요.

## 2. 공통 준비: 사용자 기억 저장소를 한 번 초기화

Claude Code와 Codex CLI 중 어느 쪽을 쓰더라도 완전히 새 환경에서는
다음 두 명령을 한 번 실행합니다.

```bash
uvx --from 'memtomem==0.3.12' mm init --preset minimal --non-interactive --mcp skip
uvx --from 'memtomem==0.3.12' mm status
```

`--mcp skip`은 다음 단계의 플러그인이 MCP 연결을 제공하므로 다른
클라이언트 설정을 건드리지 않겠다는 뜻입니다.

상태 출력에서 다음을 확인합니다.

- storage와 database path(기억 저장 위치)가 표시된다.
- source와 chunk(검색된 문서 조각) 수가 0이어도 정상이다.
- `provider=none` 또는 BM25-only는 오류가 아니라 기본 키워드 검색 상태다.

database path는 나중에 두 도구가 같은 저장소를 보는지 확인할 때 사용합니다.

## 3. 경로 A: Claude Code 플러그인 설치

Claude Code 세션에서 실행합니다.

```text
/plugin marketplace add memtomem/memtomem
/plugin install memtomem@memtomem
/reload-plugins
```

`/reload-plugins`가 없다면 Claude Code를 종료하고 새 세션을 엽니다.
플러그인이 로드되면 다음 명령으로 연결을 확인합니다.

```text
/memtomem:status
```

앞에서 본 것과 같은 storage와 database path가 표시되면 준비가 끝났습니다.

Codex CLI를 쓴다면 이 절은 건너뛰고 경로 B만 진행하세요.

## 4. 경로 B: Codex CLI 플러그인 설치

터미널에서 실행합니다.

```bash
codex plugin marketplace add memtomem/memtomem
codex plugin add memtomem@memtomem
codex
```

설치 뒤에는 새 Codex thread를 시작해야 플러그인 skill과 MCP 서버가
로드됩니다. 새 thread에서 다음처럼 요청합니다.

```text
$memtomem-status 스킬로 현재 기억 저장소 상태를 확인해줘.
```

앞에서 본 것과 같은 storage와 database path가 표시되면 준비가 끝났습니다.

Claude Code를 쓴다면 이 절은 건너뛰세요.

## 5. 첫 기억을 저장하고 바로 검색

첫 실습은 프로젝트 전용 결정이 아니라 모든 프로젝트에서 쓸 개인 선호를
저장합니다. 따라서 프로젝트 기억 저장소를 따로 만들지 않아도 됩니다.

저장할 문장:

```text
모든 프로젝트에서 설명은 한국어로 받고, 코드 식별자는 원문 표기를 유지한다.
```

### Claude Code에서 저장·검색

```text
/memtomem:remember 모든 프로젝트에서 설명은 한국어로 받고, 코드 식별자는 원문 표기를 유지한다.
/memtomem:search 코드 식별자는 원문
```

### Codex CLI에서 저장·검색

```text
$memtomem-remember 스킬로 다음 개인 선호를 저장해줘: 모든 프로젝트에서 설명은 한국어로 받고, 코드 식별자는 원문 표기를 유지한다.
$memtomem-search 스킬로 "코드 식별자는 원문"을 찾고 출처도 보여줘.
```

검색 결과에 방금 저장한 문장과 source path가 함께 나오면 성공입니다.
기본 BM25 검색은 의미 번역보다 실제 단어 일치에 강하므로, 첫 검증에서는
저장 문장에 그대로 들어 있는 `코드 식별자는 원문`을 사용합니다.

`remember`는 사용자가 명시적으로 요청해야 실행되는 write workflow입니다.
AI가 대화 중 임의로 모든 내용을 저장하는 동작이 아닙니다.

## 6. 새 세션에서 다시 찾기

사용 중인 도구를 새 세션이나 새 thread로 열고 다시 검색합니다.

Claude Code:

```text
/memtomem:search 코드 식별자는 원문
```

Codex CLI:

```text
$memtomem-search 스킬로 "코드 식별자는 원문"을 찾고 출처도 보여줘.
```

새 대화에서도 같은 문장과 source path가 나오면 첫 설정이 끝났습니다.

## 7. 첫 성공 뒤 실제 업무에 연결하기

### 기존 문서를 한 번 인덱싱

홈 디렉터리나 저장소 전체보다 문서·ADR처럼 검색할 가치가 있고 secret이
없는 작은 경로부터 시작하세요.

Claude Code:

```text
/memtomem:setup /path/to/project/docs
```

Codex CLI:

```text
$memtomem-setup 스킬로 /path/to/project/docs를 인덱싱하고 검색까지 확인해줘.
```

`setup`과 `index`는 **일회성 인덱싱**입니다. 디렉터리를 백그라운드 감시
대상으로 등록하거나 파일 변경을 계속 자동 반영하지 않습니다.

### 프로젝트 전용 기억 저장소 만들기

“이 프로젝트에서는 PR 전에 특정 테스트를 실행한다”처럼 한 프로젝트에만
해당하는 기억은 실제 Git 저장소 루트에서 별도 초기화합니다.

```bash
uvx --from 'memtomem==0.3.12' mm mem init --scope project_local
```

이 명령은 Git에 올라가지 않는 로컬 기억 계층을 만들고 등록합니다. 실행
후에는 Claude Code를 reload하거나 Codex에서 새 thread를 열어야 새 저장
범위가 보입니다. Git 저장소가 아닌 폴더에서는 먼저 올바른 프로젝트
루트로 이동하세요.

이제 다음과 같은 프로젝트 결정을 저장할 수 있습니다.

```text
결정: PR 전에는 uv run pytest를 실행한다.
이유: 병합 전에 전체 회귀를 확인하기 위해서다.
출처: CONTRIBUTING.md의 검증 절차.
```

### Claude Code와 Codex에서 같은 기억 보기

같은 컴퓨터와 같은 사용자 계정에서 기본 사용자 기억을 사용하면 두 도구는
같은 memtomem 저장소를 볼 수 있습니다.

1. Claude Code의 `/memtomem:status`에서 database path를 확인합니다.
2. Codex의 `$memtomem-status`에서도 database path를 확인합니다.
3. 두 경로가 같으면 한 도구에서 저장한 개인 선호를 다른 도구에서 검색합니다.

프로젝트 전용 기억은 database path뿐 아니라 같은 프로젝트 루트와 저장
범위를 사용해야 합니다. 이 동작은 클라우드 동기화가 아닙니다. 다른
컴퓨터에서는 [Multi-device sync](multi-device-sync.md)를 별도로 구성해야 합니다.

## 첫 주에 바로 써먹는 세 가지

| 상황 | 저장할 내용 | 기억 작성 요령 |
|---|---|---|
| 반복되는 개인 선호 | 설명 언어, 코드 표기, 답변 형식 | 여러 프로젝트에서 다시 쓸 것만 저장 |
| 확정된 프로젝트 결정 | 선택한 방식, 이유, 근거 문서 | 프로젝트 기억 초기화 후 출처까지 기록 |
| 반복 체크리스트 | 테스트, 리뷰, 배포 순서 | 실행 가능한 명령과 완료 기준을 함께 기록 |

- 처음에는 한 도구에서 저장·검색 왕복을 완성한 뒤 두 번째 도구를 연결합니다.
- 전체 대화보다 다시 쓸 결론, 이유, 출처만 짧게 저장합니다.
- 검색할 때는 “출처와 함께”라고 요청해 AI의 추측과 저장된 기억을 구분합니다.

## 무엇이 자동으로 되지 않는가

기본 플러그인은 다음 작업을 자동으로 하지 않습니다.

- 기존 프로젝트 전체 인덱싱
- 프로젝트 파일의 백그라운드 감시
- Claude Code나 Codex의 내장 memory 전체 가져오기
- 대화 전체 저장
- 별도 수동 MCP 서버 중복 등록

자동 반영이나 내장 memory 가져오기가 필요하면 첫 기억 왕복을 마친 뒤
[Claude Code 심화 가이드](integrations/claude-code.md) 또는
[Codex 심화 가이드](integrations/codex.md)를 따르세요.

## 일할 때 자주 쓰는 요청

| 상황 | Claude Code | Codex CLI | 실제 동작 |
|---|---|---|---|
| 검색 상태 진단 | `/memtomem:status` | `$memtomem-status` | `mem_status` |
| 과거 결정 검색 | `/memtomem:search 인증 모듈 교체 결정` | `$memtomem-search`로 인증 결정을 찾아줘. | `mem_search` |
| 최근 기억 확인 | `/memtomem:recall 지난 7일` | `$memtomem-recall`로 지난 7일 기억을 보여줘. | `mem_recall` |
| 확정된 내용 저장 | `/memtomem:remember 재시도는 3회로 제한한다.` | `$memtomem-remember`로 재시도 제한을 저장해줘. | `mem_add` |

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

## 문제 해결

| 증상 | 먼저 할 일 |
|---|---|
| `uvx`를 찾지 못함 | uv 공식 설치 안내를 따른 뒤 새 터미널에서 `uvx --version`을 확인합니다. |
| 플러그인을 찾지 못함 | marketplace 목록을 확인하고 설치 명령을 다시 실행합니다. |
| workflow/skill이 보이지 않음 | Claude는 reload 또는 새 세션, Codex는 새 thread를 시작합니다. |
| 저장소가 초기화되지 않았다고 나옴 | 공통 준비의 `mm init ... --mcp skip`과 `mm status`를 다시 실행합니다. |
| 프로젝트 기억을 저장할 수 없음 | 실제 Git 루트에서 `mm mem init --scope project_local`을 실행하고 도구를 다시 엽니다. |
| 검색 결과가 0건 | status에서 chunk 수를 보고 저장 문장에 실제로 있는 단어로 다시 검색합니다. |
| Claude와 Codex의 결과가 다름 | database path를 비교하고, 프로젝트 기억이면 프로젝트 루트와 저장 범위도 확인합니다. |

상세한 진단은 [Operations & troubleshooting](reference/operations.md), 수동 MCP
등록은 [MCP Client Setup](mcp-clients.md)을 따르세요.

## 완료 체크

- [ ] `uvx`와 Claude Code 또는 Codex CLI 하나가 실행된다.
- [ ] 사용자 기억 저장소를 minimal preset과 `--mcp skip`으로 초기화했다.
- [ ] 플러그인 status에서 storage와 database path를 확인했다.
- [ ] 개인 선호 하나를 명시적으로 저장했다.
- [ ] 같은 단어로 검색해 문장과 source path를 찾았다.
- [ ] 새 세션에서 같은 기억을 다시 찾았다.
- [ ] 프로젝트 전체와 대화 전체가 자동 저장되지 않음을 이해한다.
- [ ] 비밀값과 미검증 추측을 저장하지 않는다.

## 다음 단계

- CLI까지 포함한 설치: [Getting Started](getting-started.md)
- 도구별 수동 설정: [MCP Client Setup](mcp-clients.md)
- Claude Code 심화: [Claude Code integration](integrations/claude-code.md)
- Codex 심화: [Codex integration](integrations/codex.md)
- 기존 노트 인덱싱·저장 범위: [Core memory tools](reference/core-memory-tools.md)
- 임베딩·한국어 의미 검색: [Embeddings](embeddings.md)

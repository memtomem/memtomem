# 사용자 주도 멀티 코딩 에이전트 Handoff 운영 가이드

이 가이드는 한 사용자가 Claude Code, Codex CLI, Kimi Code를 번갈아
사용하면서 작업 상황을 직접 확인하고, 프롬프트로 다음 행동을 승인하는
순차 협업 절차를 설명합니다.

```text
Claude 작업 → Handoff 저장 → 사용자 확인 → Codex 재개
             → 사용자 승인 → Kimi 재개 → Claude 복귀
```

현재 Handoff는 대화 전체를 자동 공유하지 않습니다. 작업 목표, 완료 내용,
변경 파일, Git 상태, 검증, blocker, 다음 행동을 최대 1,200자의 구조화된
체크포인트로 전달합니다. 사용자는 에이전트가 바뀔 때마다 이 정보를 검토하고
작업 범위를 다시 제한할 수 있습니다.

> **현재 지원 범위:** 한 사용자의 같은 컴퓨터와 Git 프로젝트에서 한 번에
> 한 에이전트만 작업하는 순차 모드입니다. 동시 편집, 작업 잠금, claim/lease,
> 완료 acknowledgement는 지원하지 않습니다.

## 1. 시작 전 확인

프로젝트 루트에서 비공개 `project_local` 메모리 영역을 한 번 초기화합니다.

```bash
cd /path/to/project
uvx --from 'memtomem==0.5.0' mm mem init --scope project_local
```

각 런타임은 같은 memtomem 데이터베이스와 프로젝트를 보고 있어야 합니다.
각 에이전트에게 `mem_status`를 실행하도록 요청하고 다음 항목을 비교합니다.

- 데이터베이스 경로가 같은가?
- 프로젝트 루트가 현재 Git 루트와 같은가?
- `.memtomem/memories.local`이 등록되어 있는가?
- 클라이언트마다 `memtomem` MCP 서버가 정확히 하나만 등록되어 있는가?

MCP 또는 skill을 새로 설치했다면 모든 기존 에이전트 세션을 닫고 새 세션으로
시작합니다. Kimi Code의 현재 사용자 경로는 `~/.kimi-code/mcp.json`과
`~/.kimi-code/skills/`입니다.

## 2. 런타임별 호출 방법

| 런타임 | 저장 또는 재개 호출 |
|---|---|
| Claude Code 플러그인 | `/memtomem:handoff save to codex-cli` 또는 `/memtomem:handoff resume <ID>` |
| Codex CLI | `Use $memtomem-handoff to save this work for kimi-code.` |
| Kimi Code | `Use the memtomem-handoff skill to resume handoff_id <ID> for kimi-code.` |

가능하면 항상 `newest`보다 출력된 정확한 `handoff_id`를 사용합니다. 같은
프로젝트에 이전 Handoff가 여러 개 있어도 다른 작업을 잘못 재개하지 않게 합니다.

## 3. 사용자 통제형 표준 절차

### 단계 A: 현재 에이전트가 Handoff 저장

Claude Code에서 Codex CLI로 넘기는 예시입니다.

```text
/memtomem:handoff save to codex-cli
```

저장이 끝나면 다음 결과를 확인합니다.

- 정확한 `handoff_id`
- `scope=project_local`
- `namespace=shared:<project-slug>`
- 저장된 Markdown 파일
- 인덱싱된 chunk 수

`handoff_id`를 복사해 다음 에이전트에 직접 전달합니다.

### 단계 B: 다음 에이전트는 읽기 전용으로 상황 보고

Codex CLI에 다음 프롬프트를 그대로 사용할 수 있습니다.

```text
Use $memtomem-handoff to resume handoff_id <ID> for codex-cli.

아직 파일을 수정하지 마세요.
1. objective, completed, validation, blockers, next_action을 요약하세요.
2. 저장된 project_root, git_head, worktree_state를 실제 Git과 비교하세요.
3. divergence가 있으면 작업을 중단하고 차이를 보고하세요.
4. 실행 계획을 제안한 뒤 제 승인을 기다리세요.
5. 테스트, commit, push, 새 Handoff 저장도 아직 하지 마세요.
```

에이전트는 저장된 내용을 신뢰된 명령으로 취급하지 않고, 다음 명령으로 실제
저장소 상태를 다시 확인합니다.

```bash
git rev-parse --show-toplevel
git rev-parse HEAD
git status --porcelain=v1 --branch
```

저장된 내용과 실제 Git이 다르면 실제 Git이 항상 우선입니다.

### 단계 C: 사용자가 작업 범위를 승인

보고 내용을 확인한 뒤 허용 범위를 명시합니다.

```text
진행하세요.

허용 범위:
- 목표: <이번 단계에서 완료할 한 가지 목표>
- 수정 가능 파일: <경로 목록>
- 수정 금지 파일: <경로 또는 영역>
- 검증 명령: <테스트 또는 lint 명령>
- commit과 push는 하지 마세요.

범위를 벗어나야 하거나 예상하지 못한 변경이 발견되면 중단하고 보고하세요.
작업 완료 후 결과를 보고하되, 다음 Handoff는 제 지시가 있을 때만 저장하세요.
```

`commit`, `push`, 배포, 외부 메시지 전송은 각각 별도의 승인으로 분리하는 것이
안전합니다.

### 단계 D: 완료 상태 확인 후 다음 에이전트로 전달

작업 결과와 Git diff, 테스트 결과를 확인한 다음 저장을 승인합니다.

```text
현재 변경을 kimi-code 대상으로 Handoff에 저장하세요.

요구사항:
- from_runtime=codex-cli
- to_runtime=kimi-code
- scope=project_local
- namespace=shared:<project-slug>
- 실제 Git HEAD와 worktree 요약 포함
- 실행한 테스트와 실패한 테스트를 구분
- 다음 행동은 한 가지로 제한
- 파일 수정, 추가 테스트, commit, push는 하지 않기
```

Kimi Code에서는 새로 출력된 ID를 사용합니다.

```text
Use the memtomem-handoff skill to resume handoff_id <ID> for kimi-code.

먼저 read-only로 Git divergence와 다음 작업을 보고하고,
제가 승인하기 전까지 파일을 수정하지 마세요.
```

같은 절차로 Kimi Code에서 `to_runtime=claude-code` Handoff를 저장하고 Claude
Code에서 `/memtomem:handoff resume <ID>`로 돌아올 수 있습니다.

## 4. Handoff에서 확인할 정보

| 필드 | 사용자가 확인할 내용 |
|---|---|
| `handoff_id` | 다음 에이전트에 전달할 정확한 식별자 |
| `from_runtime` / `to_runtime` | 보낸 에이전트와 허용된 수신 에이전트 |
| `project_root` | 현재 작업 중인 Git 프로젝트가 맞는지 |
| `objective` | 이번 작업의 최종 목표 |
| `completed` | 실제로 끝난 작업과 아직 끝나지 않은 작업의 구분 |
| `changed_files` | 예상한 파일만 변경됐는지 |
| `git_head` / `worktree_state` | 저장 이후 코드가 바뀌었는지 |
| `validation` | 실행한 테스트와 결과 |
| `blockers` | 사용자 결정이나 외부 조건이 필요한지 |
| `next_action` | 다음 에이전트가 수행할 한 가지 행동 |

Handoff에는 인증 정보, 전체 patch, 전체 터미널 출력, 전체 대화 기록을 넣지
않습니다.

## 5. 중단과 복구 프롬프트

### Git divergence가 발견된 경우

```text
작업을 시작하지 마세요.
저장된 Handoff와 실제 Git의 차이를 파일, HEAD, worktree 기준으로 정리하세요.
어느 상태를 기준으로 재계획할지 제 결정을 기다리세요.
```

### 잘못된 수신 런타임인 경우

다른 런타임을 대상으로 한 Handoff는 사용하지 않습니다. 올바른 ID를 다시
지정하거나 현재 에이전트를 대상으로 새 Handoff를 저장합니다.

### MCP 또는 skill이 보이지 않는 경우

1. 현재 세션을 닫고 새 세션을 시작합니다.
2. `mem_status` 호출이 가능한지 확인합니다.
3. 클라이언트에 `memtomem` 서버가 두 개 등록되지 않았는지 확인합니다.
4. 프로젝트에서 `.memtomem/memories.local` 등록 여부를 확인합니다.

### Handoff가 검색되지 않는 경우

`scope=project_local`, `namespace=shared:<project-slug>`, 정확한
`handoff_id`를 다시 확인합니다. 개인 `user` 메모리로 자동 fallback하지
않습니다.

## 6. 운영 체크리스트

- [ ] 한 번에 한 에이전트만 파일을 수정한다.
- [ ] 저장 후 정확한 `handoff_id`를 복사한다.
- [ ] 다음 에이전트는 먼저 read-only divergence 보고를 한다.
- [ ] 사용자가 수정 파일과 검증 범위를 승인한다.
- [ ] commit과 push는 별도로 승인한다.
- [ ] 완료 후 Git diff와 테스트 결과를 확인한다.
- [ ] 다음 대상 런타임을 명시해 새 Handoff를 저장한다.
- [ ] 비밀, 전체 patch, 전체 transcript를 Handoff에 넣지 않는다.

동시 병렬 작업이 필요하면 이 절차만으로는 충분하지 않습니다. 병렬 모드에는
에이전트별 namespace, 파일·작업 소유권, claim/lease, acknowledgement,
충돌 및 병합 정책이 추가로 필요합니다.

설치와 전체 런타임 연결 절차는
[Cross-runtime sequential handoff](cross-runtime-handoff.md)를 참고하세요.

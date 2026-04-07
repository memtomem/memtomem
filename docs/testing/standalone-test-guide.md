# 독립 환경 테스트 가이드

별도 폴더에서 memtomem을 처음부터 구성하고, use case별로 하나씩 따라하며 기능을 검증하는 가이드.
기존 `~/.memtomem/` 설정에 영향을 주지 않는다.

---

## 사전 준비

| 항목 | 확인 방법 | 없으면 |
|------|----------|--------|
| Python 3.12+ | `python3 --version` | [python.org](https://www.python.org/downloads/) |
| Ollama 실행 중 | `ollama list` | [ollama.com](https://ollama.com/) 설치 후 `ollama serve` |
| uv 설치 | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Git | `git --version` | OS 패키지 매니저로 설치 |

---

## 1. 소스 준비 및 설치

```bash
# 작업 디렉토리 생성
mkdir -p /tmp/memtomem-test && cd /tmp/memtomem-test

# 소스 클론 (SSH)
git clone git@github.com:memtomem/memtomem.git
cd memtomem

# 가상환경 + 의존성 설치
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e "packages/memtomem[dev]"
uv pip install -e "packages/memtomem-stm[ltm]"
```

**설치 확인:**

```bash
uv run mm --help          # CLI 도움말 출력되면 성공
uv run mm version         # 버전 확인
```

---

## 2. 자동 테스트 (1476개)

설치가 정상인지 전체 테스트로 확인한다.

```bash
# 전체 테스트 (Ollama 실행 중일 때)
uv run pytest

# Ollama 없이 (23개 스킵, 나머지 통과)
uv run pytest -m "not ollama"
```

**기대 결과:** `1476 passed` (Core 837 + STM 639)

영역별로 나눠서 실행하고 싶다면:

```bash
uv run pytest packages/memtomem/tests/ -v      # Core 837개
uv run pytest packages/memtomem-stm/tests/ -v   # STM 639개
```

---

## 3. mm init — 초기 설정

`mm init`은 대화형 위자드다. 기존 `~/.memtomem/`이 있으면 먼저 백업한다.

```bash
# 기존 설정 백업 (있는 경우)
[ -d ~/.memtomem ] && mv ~/.memtomem ~/.memtomem.bak.$(date +%s)

# 초기 설정 위자드 실행
uv run mm init
```

### 위자드 단계별 안내

| 단계 | 설명 | 입문자 권장 |
|------|------|------------|
| 1. Embedding provider | Ollama (로컬) / OpenAI (클라우드) | **Ollama** (무료, 로컬) |
| 2. Embedding model | nomic-embed-text / bge-m3 | **bge-m3** (다국어 우수) |
| 3. Memory directory | 메모리 저장 디렉토리 | `/tmp/memtomem-test/memories` |
| 4. Storage | SQLite DB 경로 | 기본값 (`~/.memtomem/memtomem.db`) |
| 5. Namespace | 폴더명 기반 자동 네임스페이스 | **yes** |
| 6. Search | 검색 결과 수, time-decay | 기본값 (Enter) |
| 7. Language | 토크나이저 (Unicode / Korean) | **unicode61** (일반), **kiwipiepy** (한국어 중심) |
| 8. Editor | Claude Code 자동 설정 | **Claude Code** |

> 각 단계에서 `b`(뒤로), `q`(종료) 가능.

**설정 확인:**

```bash
uv run mm config show
```

---

## Use Case 1: 메모리 추가 및 검색 (기본)

가장 기본적인 사용법 — 메모리를 추가하고 검색한다.

### 1-1. 샘플 데이터 준비

```bash
MEMDIR=/tmp/memtomem-test/memories
mkdir -p $MEMDIR

cat > $MEMDIR/architecture.md << 'EOF'
---
tags: [architecture, decision]
---

## 웹 프레임워크 선택

Flask를 선택한 이유: 팀 내 경험이 풍부하고 빠른 프로토타이핑에 적합.
FastAPI도 고려했으나 async 학습 곡선이 부담.

## 캐싱 전략

Redis를 캐시 레이어로 도입. LRU → LFU 전환 후 캐시 미스 40% 감소.
세션 스토어는 PostgreSQL 별도 운영.
EOF

cat > $MEMDIR/kubernetes.md << 'EOF'
## 모니터링 설정

Prometheus + Grafana 스택 도입.
CPU 임계값 80% 초과 시 Slack 알림 설정.
Node exporter로 클러스터 전체 메트릭 수집.

## 배포 전략

Blue-green 배포 적용. 롤백은 이전 ReplicaSet 재활성화 방식.
Canary 배포는 Argo Rollouts로 점진적 전환 (10% → 50% → 100%).
EOF

cat > $MEMDIR/meeting-2026-04.md << 'EOF'
## 스프린트 회의 2026-04-01

참석: 김철수, 이영희, 박민수
결정사항:
- Redis 마이그레이션 다음 주 진행
- API 버전닝 v2 설계 시작
TODO: 김철수가 마이그레이션 계획서 작성

## 아키텍처 리뷰 2026-04-05

결정: 마이크로서비스 분리 보류, 모놀리스 유지
이유: 현재 트래픽 규모에서 오버엔지니어링
EOF
```

### 1-2. 인덱싱

```bash
uv run mm index $MEMDIR
```

**기대 결과:** 3개 파일, 6~8개 chunk 인덱싱됨.

**확인:**

```bash
uv run mm status   # chunk 수, DB 경로, embedding 모델 확인
```

### 1-3. 검색

```bash
# 한국어 키워드 검색
uv run mm search "웹 프레임워크 선택 이유"

# 영어 → 한국어 시맨틱 검색 (크로스 언어)
uv run mm search "why Flask over FastAPI"

# 영어 키워드 → 한국어 문서 매칭
uv run mm search "kubernetes monitoring alerts"
```

**확인 포인트:**
- [ ] "웹 프레임워크" 검색 → `architecture.md`의 Flask 섹션 출현
- [ ] "why Flask" 영어 검색 → 같은 한국어 문서 출현 (시맨틱 매칭)
- [ ] "kubernetes monitoring" → `kubernetes.md`의 모니터링 섹션 출현

### 1-4. CLI로 메모리 추가

```bash
# 텍스트로 메모리 추가
uv run mm add "Docker 이미지 빌드 시간 최적화: multi-stage 빌드로 3분→45초" \
  --tags "docker,optimization"

# 즉시 검색 확인 (방금 추가한 메모리 출현?)
uv run mm search "Docker 빌드 시간"

# 날짜 기반 조회 (최근 추가된 메모리)
uv run mm recall --since 2026-04
```

**확인 포인트:**
- [ ] `mm add` 성공 메시지 + chunk ID 출력
- [ ] 검색에 방금 추가한 Docker 메모리 출현
- [ ] `mm recall`에 시간순으로 표시

---

## Use Case 2: Claude Code 연동

MCP 서버를 Claude Code에 등록하고, 대화형으로 메모리를 사용한다.

### 2-1. MCP 서버 등록

```bash
claude mcp add memtomem-test -s project -- \
  uv run --directory /tmp/memtomem-test/memtomem memtomem-server
```

### 2-2. 기본 조작 (Claude Code 대화창에서)

아래 문장을 Claude Code에 입력한다. 에이전트가 적절한 MCP 도구를 호출한다.

```
Step 1: "mem_status 호출해줘"
  → DB path, embedding model, chunk 수 확인
  → 앞서 인덱싱한 6~8개 chunk가 표시되어야 함

Step 2: "쿠버네티스 모니터링 관련 내용 검색해줘"
  → mem_search 호출 → kubernetes.md의 모니터링 섹션 출현

Step 3: "오늘 논의한 내용 기록: API 게이트웨이를 Kong으로 선정, 라이선스 확인 필요"
  → mem_add로 저장

Step 4: "방금 추가한 Kong 메모리 검색해줘"
  → 즉시 검색 가능 확인
```

### 2-3. mem_do 액션 (61개 확장 기능)

`mem_do`는 61개 액션을 하나의 도구로 라우팅하는 메타 도구다.
Claude Code에서 자연어로 요청하면 에이전트가 자동으로 적절한 액션을 선택한다.

```
"사용 가능한 액션 목록 보여줘"
  → mem_do(action="help") 호출 → 61개 액션 + 카테고리별 분류

"태그 목록 보여줘"
  → mem_do(action="tag_list") 호출

"자동 태깅 미리보기 해줘 (적용하지 말고)"
  → mem_do(action="auto_tag", params={"dry_run": true}) 호출

"메모리 시스템 건강 보고서 보여줘"
  → mem_do(action="eval") 호출

"중복 메모리 있는지 스캔해줘"
  → mem_do(action="dedup_scan") 호출
```

---

## Use Case 3: 컨텍스트 윈도우 검색 (Context-Window Search)

검색 결과에 인접 청크를 포함시켜 주변 맥락까지 한 번에 확인한다.

### 3-1. 일반 검색 vs 컨텍스트 확장 검색 비교

**CLI에서:**

```bash
# 일반 검색 — 매칭된 청크만 반환 (300~500자 미리보기)
uv run mm search "캐싱 전략"

# 결과에서 chunk_id를 확인 (예: abc-123-def)
```

**Claude Code에서:**

```
Step 1: "캐싱 전략 관련 메모리 검색해줘"
  → mem_search("캐싱 전략") 호출
  → 결과: architecture.md의 캐싱 섹션 (미리보기만)

Step 2: "첫 번째 결과의 주변 맥락을 보고 싶어. expand 해줘"
  → mem_do(action="expand", params={"chunk_id": "...", "window": 2})
  → 결과: 앞뒤 ±2개 청크 전체 내용 표시
  → "Before" 섹션에 웹 프레임워크 선택 내용
  → "After" 섹션에 이후 내용 (있는 경우)
```

### 3-2. 일괄 확장 검색

처음부터 모든 결과에 맥락을 포함:

```
"쿠버네티스 관련 메모리를 주변 맥락 포함해서 검색해줘 (context_window=2)"
  → mem_search("쿠버네티스", context_window=2)
  → 각 결과에 [chunk M/T] 위치 표시 + before/after 섹션 포함
```

### 3-3. 모델의 적응적 사용 패턴

실제 사용에서 모델이 스스로 판단하는 워크플로우:

```
1. mem_search("query")              → 기본 결과 확인
2. 맥락이 부족하다고 판단하면       → mem_expand(chunk_id, window=3)
3. 처음부터 맥락이 필요하면         → mem_search("query", context_window=2)
```

### 3-4. 환경변수로 전역 활성화

모든 검색에 자동으로 컨텍스트 확장을 적용하려면:

```bash
export MEMTOMEM_CONTEXT_WINDOW__ENABLED=true
export MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE=2
```

---

## Use Case 4: 세션 및 워킹 메모리

세션을 시작하고, 작업 중에 워킹 메모리(스크래치패드)를 활용한다.

### 4-1. 세션 시작 및 종료

Claude Code에서:

```
Step 1: "세션 시작해줘. 제목은 인프라 검토"
  → mem_session_start(title="인프라 검토") 호출
  → session_id 반환

Step 2: "워킹 메모리에 현재 작업 저장: focus = Redis 마이그레이션 계획"
  → mem_scratch_set(key="focus", value="Redis 마이그레이션 계획") 호출

Step 3: "워킹 메모리에서 focus 값 확인"
  → mem_scratch_get(key="focus") 호출
  → "Redis 마이그레이션 계획" 반환

Step 4: "세션 종료. 요약: Redis 마이그레이션 계획 수립, Kong 게이트웨이 선정"
  → mem_session_end(summary="...") 호출

Step 5: "세션 기록 보여줘"
  → mem_session_list() 호출
  → 방금 종료된 세션 + 요약 표시
```

**확인 포인트:**
- [ ] 세션 시작 시 session_id 반환
- [ ] 워킹 메모리에 저장/조회 성공
- [ ] 세션 종료 후 세션 목록에 요약 포함
- [ ] 워킹 메모리는 세션 종료 후 자동 정리됨

### 4-2. 스크래치 프로모트

임시 메모를 영구 메모리로 승격:

```
"워킹 메모리에 저장: decision = API 버전닝은 URL path 방식"
  → mem_scratch_set(key="decision", value="API 버전닝은 URL path 방식")

"이 결정사항을 영구 메모리로 승격해줘"
  → mem_do(action="scratch_promote", params={"key": "decision"})
  → 스크래치에서 삭제 + 영구 chunk로 생성

"API 버전 검색해줘"
  → 승격된 메모리가 검색 결과에 출현
```

---

## Use Case 5: 멀티 에이전트 메모리

여러 에이전트가 각자의 메모리를 갖고, 필요 시 공유하는 시나리오.

### 5-1. 구조

```
agent/backend    ← 백엔드 에이전트 전용 메모리
agent/frontend   ← 프론트엔드 에이전트 전용 메모리
shared           ← 모든 에이전트가 접근 가능한 공유 메모리
```

> **실제 운영 시**: CLAUDE.md에 에이전트 역할을 설정하면 에이전트가 스스로 등록/관리한다.
> 여기서는 테스트를 위해 수동으로 시뮬레이션한다.

### 5-2. 단계별 테스트

Claude Code에서:

```
# ── Step 1: 에이전트 등록 ──

"memtomem 멀티 에이전트 테스트할거야.
  backend(API 서버 담당)와 frontend(React UI 담당) 두 에이전트를 등록하고
  네임스페이스 목록 확인해줘"
  → mem_agent_register × 2 호출
  → mem_do(action="ns_list")
  → agent/backend, agent/frontend, shared 3개 확인

# ── Step 2: 개인 메모리 추가 ──

"backend 네임스페이스에 저장:
  'API rate limiting은 Redis sliding window 방식. 분당 100 요청 제한.'"
  → mem_add(content="...", namespace="agent/backend", tags=["api", "rate-limit"])

"frontend 네임스페이스에 저장:
  'React Query로 서버 상태 관리. staleTime 5분, retry 3회.'"
  → mem_add(content="...", namespace="agent/frontend", tags=["react", "state"])

# ── Step 3: 에이전트 범위 검색 ──

"backend 관점에서 'rate limit' 검색"
  → mem_agent_search(query="rate limit", agent_id="backend")
  → agent/backend + shared 범위 검색 → rate limit 메모 출현

"frontend 관점에서 'rate limit' 검색"
  → mem_agent_search(query="rate limit", agent_id="frontend")
  → agent/frontend + shared 범위 검색 → 결과 없음 (backend 전용)

# ── Step 4: 메모리 공유 ──

"backend의 rate limit 메모를 shared로 공유해줘"
  → mem_agent_share(chunk_id="...", target="shared")

"이제 frontend 관점에서 다시 'rate limit' 검색"
  → shared에 복사된 메모 출현!

# ── Step 5: 공유 메모리 직접 추가 ──

"shared에 팀 결정사항 추가:
  'API 버전닝은 URL path 방식 (v1/v2). Header 방식은 사용 안 함.'"
  → mem_add(content="...", namespace="shared", tags=["api", "decision"])

"backend 관점에서 'API 버전' 검색"
  → 공유 결정사항 출현

"frontend 관점에서도 'API 버전' 검색"
  → 동일한 공유 결정사항 출현
```

**확인 포인트:**
- [ ] 에이전트 등록 시 네임스페이스 자동 생성
- [ ] 개인 메모리는 해당 에이전트 검색에서만 출현
- [ ] `mem_agent_share` 후 다른 에이전트에서도 검색 가능
- [ ] `shared` 네임스페이스는 모든 에이전트 검색에 포함

---

## Use Case 6: 네임스페이스 및 태그 관리

메모리를 체계적으로 분류하고 관리한다.

### 6-1. 네임스페이스

Claude Code에서:

```
Step 1: "네임스페이스 목록 보여줘"
  → mem_do(action="ns_list") → 현재 네임스페이스 + chunk 수

Step 2: "새 네임스페이스 'project-alpha'로 설정해줘"
  → mem_ns_set(namespace="project-alpha")

Step 3: "이 네임스페이스에 메모 추가: 'Alpha 프로젝트 킥오프. 마감일 2026-06-30'"
  → mem_add(content="...", namespace="project-alpha")

Step 4: "'project-alpha' 네임스페이스에서만 검색"
  → mem_search("마감일", namespace="project-alpha")

Step 5: "기본 네임스페이스로 돌아가"
  → mem_ns_set(namespace="default")
```

### 6-2. 태그

```
Step 1: "태그 목록과 각 태그별 메모리 수 보여줘"
  → mem_do(action="tag_list") → architecture:2, docker:1 등

Step 2: "'architecture' 태그가 있는 메모리만 검색"
  → mem_search("캐싱", tag_filter="architecture")
  → architecture.md만 출현

Step 3: "자동 태깅 미리보기 (적용하지 말고)"
  → mem_do(action="auto_tag", params={"dry_run": true})
  → 추천 태그 확인

Step 4: "자동 태깅 적용"
  → mem_do(action="auto_tag", params={"dry_run": false})
```

---

## Use Case 7: STM — 프록시 기반 자동 메모리 서피싱

STM은 다른 MCP 서버를 프록싱하면서 관련 메모리를 자동으로 주입한다.

### 7-1. STM 설정

```bash
uv run mm stm init
```

위자드 단계:

| 단계 | 설명 | 선택 |
|------|------|------|
| 1. MCP 클라이언트 감지 | Claude Code, Cursor 등 | 자동 감지됨 |
| 2. 프록시할 서버 선택 | 연결된 MCP 서버 목록 | filesystem 등 선택 |
| 3. 접두어 설정 | 프록시 도구 이름 접두어 | `fs` (filesystem) |
| 4. 압축 전략 | auto/none/truncate/hybrid 등 | **auto** (기본, 콘텐츠 기반 자동 선택) |
| 5. 캐시 | 응답 캐시 활성화 | **yes** |
| 6. Langfuse | 옵저버빌리티 (선택) | 스킵 가능 |
| 7. 설정 저장 | `~/.memtomem/stm_proxy.json` | 자동 |
| 8. STM 활성화 | 에디터 재시작 | 안내에 따라 |

> STM 설정 후 에디터(Claude Code)를 재시작해야 적용된다.

### 7-2. 서피싱 동작 확인

Claude Code에서:

```
Step 1: "stm_proxy_stats() 호출해줘"
  → 프록시 상태 확인: 연결된 서버, 도구 수, 압축 통계

Step 2: 프록시된 도구 사용 (filesystem 서버가 프록시된 경우)
  "fs__read_file(path="/some/project/auth.py") 호출해줘"
  → 응답에 "## Relevant Memories" 섹션이 자동 추가되는지 확인

Step 3: 서피싱 피드백
  "방금 서피싱된 메모리에 helpful 피드백 줘"
  → stm_surfacing_feedback(surfacing_id="...", rating="helpful")

Step 4: 서피싱 통계 확인
  "stm_surfacing_stats() 호출해줘"
  → helpful/not_relevant 비율 확인
```

### 7-3. 서피싱 동작 원리

```
1. 에이전트가 fs__read_file(path="/src/auth.py") 호출
2. STM이 경로에서 쿼리 추출: "src auth py" (경로 토큰화)
3. LTM에서 관련 메모리 검색 (score >= 0.02)
4. 응답 앞/뒤에 "## Relevant Memories" 섹션 주입
5. 같은 세션에서 이미 보여준 메모리는 중복 표시 안 함
6. 세션 간에도 7일 이내 서피싱된 메모리는 자동 제외
```

### 7-4. STM + 컨텍스트 윈도우

서피싱되는 메모리에 인접 맥락을 포함하려면:

```json
// ~/.memtomem/stm_proxy.json 의 surfacing 섹션
{
  "surfacing": {
    "context_window_size": 2,
    "max_injection_chars": 3000
  }
}
```

`context_window_size=2`로 설정하면 서피싱 시 ±2개 인접 청크도 함께 표시된다.

### 7-5. STM 비활성화

```bash
uv run mm stm reset
```

원래 MCP 설정이 복원되고 STM이 비활성화된다.

---

## Use Case 8: 분석 및 유지보수

메모리 시스템의 건강 상태를 점검하고 관리한다.

### 8-1. 건강 보고서

Claude Code에서:

```
"메모리 시스템 건강 보고서 보여줘"
  → mem_do(action="eval") 호출
  → 확인 항목:
    - total_chunks: 전체 청크 수
    - dead_memories: 접근 횟수 0인 메모리 비율
    - tag_coverage: 태그가 있는 메모리 비율
    - namespace_balance: 네임스페이스별 분포
```

### 8-2. 중복 검사

```
"중복 메모리 스캔"
  → mem_do(action="dedup_scan") 호출
  → 유사한 메모리 그룹 표시

"중복 그룹의 메모리를 병합해줘"
  → mem_do(action="dedup_merge", params={"group_id": "..."})
```

### 8-3. 엔티티 추출

```
"엔티티 추출 미리보기 (적용하지 말고)"
  → mem_do(action="entity_scan", params={"dry_run": true})
  → 추출된 인물, 날짜, 기술, 결정사항 등 확인

"엔티티 기반 검색: 김철수 관련 메모리"
  → mem_do(action="entity_search", params={"entity": "김철수"})
```

### 8-4. 타임라인

```
"최근 1주일 메모리 활동 타임라인"
  → mem_do(action="timeline") 호출
  → 날짜별 추가/수정/검색 활동 표시

"이번 달 활동 요약"
  → mem_do(action="activity") 호출
```

### 8-5. 중요도 스코어링

```
"중요도 스코어링 실행해줘"
  → mem_do(action="importance_scan") 호출
  → 접근 빈도, 연결 수, 태그 수, 내용 길이 기반으로 중요도 계산
  → 가장 중요한 메모리 확인
```

---

## Use Case 9: Web UI

브라우저에서 메모리를 시각적으로 탐색한다.

### 9-1. 실행

```bash
uv run mm web
# 브라우저에서 http://localhost:8080 접속
```

### 9-2. 탭별 확인

| 탭 | 확인 항목 |
|----|----------|
| **Search** | 한국어/영어 검색, context_window 파라미터, source/tag/namespace 필터 |
| **Sources** | 인덱싱된 파일 목록, 파일별 chunk 수, 클릭하면 chunk 상세 |
| **Tags** | 태그 목록, 빈도, 클릭하면 해당 태그 메모리 |
| **Sessions** | 세션 기록, 이벤트 로그, 요약 |
| **Health** | eval 결과 (dead memories, tag coverage 등) |
| **STM** (활성화 시) | 서버 상태, 압축 통계, 서피싱 피드백, 호출 이력 |

### 9-3. Web API에서 컨텍스트 검색

```bash
# 기본 검색
curl "http://localhost:8080/api/search?q=캐싱전략"

# 컨텍스트 윈도우 포함
curl "http://localhost:8080/api/search?q=캐싱전략&context_window=2"
# → 결과에 context.window_before / context.window_after 포함
```

---

## Use Case 10: 고급 검색 기능

### 10-1. 소스 필터 검색

```bash
# 특정 파일에서만 검색
uv run mm search "Redis" --source "kubernetes.md"

# glob 패턴 사용
uv run mm search "배포" --source "*.md"
```

### 10-2. 검색 이력 및 자동 완성

Claude Code에서:

```
"최근 검색 기록 보여줘"
  → mem_do(action="search_history") 호출

"'kuber'로 시작하는 검색어 추천해줘"
  → mem_do(action="search_suggest", params={"prefix": "kuber"})
```

### 10-3. 교차 참조

```
"Redis 관련 메모리와 캐싱 전략 메모리를 연결해줘"
  → mem_link(source_id="...", target_id="...")

"캐싱 전략 메모리와 연결된 메모리 보여줘"
  → mem_related(chunk_id="...")
```

### 10-4. 메모리 반성 (Reflection)

```
"메모리 시스템에서 반복되는 패턴이나 인사이트를 분석해줘"
  → mem_do(action="reflect") 호출
  → 자주 검색되는 주제, 연결 패턴 등 분석

"분석 결과를 메모리로 저장해줘"
  → mem_do(action="reflect_save") 호출
```

---

## 정리

### 테스트 환경 삭제

```bash
# Claude Code에서 테스트용 MCP 제거
claude mcp remove memtomem-test

# 테스트 환경 삭제
rm -rf /tmp/memtomem-test

# 기존 설정 복원 (백업했을 경우)
[ -d ~/.memtomem.bak.* ] && rm -rf ~/.memtomem && mv ~/.memtomem.bak.* ~/.memtomem
```

### STM 비활성화 (설정했을 경우)

```bash
uv run mm stm reset
```

---

## 자동 테스트 상세 (참고)

### 테스트 파일별 커버리지

| 영역 | 파일 | 수 | 대상 |
|------|------|---|------|
| **Core** | `test_server_tools_core.py` | 65 | search, recall, status, CRUD |
| | `test_server_tools_org.py` | 41 | namespace, tag, session, scratch |
| | `test_server_tools_advanced.py` | 71 | cross_ref, policy, entity, importance |
| | `test_tools_logic.py` | 46 | entity extraction, policy engine, temporal |
| | `test_search_stages.py` | 23 | RRF fusion, cosine, MMR |
| | `test_context_window.py` | 18 | _expand_context, mem_expand, formatters |
| | `test_chunkers_extended.py` | 37 | Python/JS/TS, JSON/YAML/TOML |
| | `test_indexing_engine.py` | 45 | IndexEngine, merge, overlap, watcher |
| | `test_embedding_providers.py` | 38 | Ollama/OpenAI (HTTP mocked) |
| | `test_storage_extended.py` | 23 | dense_search, FTS rebuild, access counts |
| | `test_web_routes.py` | 29 | 13 HTTP endpoints |
| | `test_web_routes_extended.py` | 20 | tags, timeline, evaluation 등 |
| | `test_server_helpers.py` | 41 | formatters, date parsing, error handler |
| | `test_cli.py` | 35 | CLI command, config, 인자 파싱 |
| | `test_user_workflows.py` | 18 | E2E 시나리오 (Ollama 필요) |
| | `test_usability_fixes.py` | 30 | frontmatter, wikilink, FTS5 하이픈 |
| | `test_meta_tool.py` | 16 | mem_do registry, categories, dispatch |
| | 기타 | ~260 | storage, search, chunking, sessions |
| **STM** | `test_compression.py` | 48 | 6 strategies + auto_select |
| | `test_surfacing_engine.py` | 40 | surfacing, feedback, dedup |
| | `test_proxy_manager.py` | 35 | tool routing, compression pipeline |
| | `test_error_metrics.py` | 25 | ErrorCategory, record_error, error_rate |
| | `test_tool_metadata.py` | 22 | hidden, description_override, distill |
| | `test_context_window.py` | 14 | model-aware budget, effective_max_chars |
| | `test_observability.py` | 17 | trace_id, RPS tracker, upstream health |
| | `test_stress_concurrency.py` | 20 | 1MB+ payloads, concurrent calls |
| | `test_proxy_error_paths.py` | 32 | transport/protocol/timeout errors |
| | `test_cross_session_dedup.py` | 13 | seen_memories, TTL, cleanup |
| | 기타 | ~373 | cleaning, feedback, cache, bench |

### 영역별 실행

```bash
# 전체
uv run pytest                                                           # 1476

# Core만
uv run pytest packages/memtomem/tests/ -v                               # 837

# STM만
uv run pytest packages/memtomem-stm/tests/ -v                           # 639

# 특정 영역
uv run pytest packages/memtomem/tests/test_context_window.py -v         # 컨텍스트 윈도우
uv run pytest packages/memtomem/tests/test_search_stages.py -v          # RRF, MMR
uv run pytest packages/memtomem/tests/test_chunkers_extended.py -v      # 청커
uv run pytest packages/memtomem/tests/test_web_routes.py -v             # HTTP API
uv run pytest packages/memtomem-stm/tests/test_compression.py -v       # 압축 전략
uv run pytest packages/memtomem-stm/tests/test_surfacing_engine.py -v  # 서피싱
```

---

## 트러블슈팅

### "Ollama not running" 에러

```bash
ollama serve        # Ollama 시작
ollama list         # 모델 확인
ollama pull bge-m3  # 모델이 없으면 다운로드
```

### "Embedding dimension mismatch"

DB가 다른 모델로 생성된 경우:

```bash
uv run mm embedding-reset --mode apply-current  # DB 리셋
uv run mm index /tmp/memtomem-test/memories      # 재인덱싱
```

### 테스트 일부 스킵됨

`23 skipped` → Ollama 미실행 시 정상. `@pytest.mark.ollama` 마커 테스트는 자동 스킵.

### mm init이 기존 설정을 덮어씀

`~/.memtomem/` 디렉토리를 사용하므로, 테스트 전 반드시 백업:

```bash
mv ~/.memtomem ~/.memtomem.bak.$(date +%s)
```

### STM 설정 후 도구가 안 보임

에디터(Claude Code)를 재시작해야 프록시된 도구가 등록된다.

### mem_expand에서 "not found"

chunk_id가 정확한지 확인. `mem_search` 결과에서 `id=...` 부분을 복사해서 사용.

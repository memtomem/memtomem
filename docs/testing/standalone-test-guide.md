# 독립 환경 테스트 가이드

별도 폴더에서 memtomem을 처음부터 구성하고, 실제 사용하면서 LTM/STM 기능을 검증하는 가이드.
기존 `~/.memtomem/` 설정에 영향을 주지 않는다.

---

## 사전 준비

| 항목 | 확인 방법 |
|------|----------|
| Python 3.12+ | `python3 --version` |
| Ollama 실행 중 | `ollama list` |
| uv 설치 | `uv --version` |

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

---

## 2. 자동 테스트 (1101개)

설치 검증 겸 전체 테스트를 실행한다.

```bash
uv run pytest                    # 전체 (Ollama 실행 중일 때 1101 passed)
uv run pytest -m "not ollama"    # Ollama 없이 (1078 passed, 23 skipped)
```

---

## 3. mm init — 초기 설정

`mm init`은 7단계 대화형 위자드다. 기존 `~/.memtomem/`이 있으면 백업한 후 시작한다.

```bash
# 기존 설정 백업 (있는 경우)
[ -d ~/.memtomem ] && mv ~/.memtomem ~/.memtomem.bak.$(date +%s)

# 초기 설정 위자드 실행
uv run mm init
```

### 위자드 단계별 안내

| 단계 | 설명 | 권장 선택 |
|------|------|----------|
| 1. Embedding provider | Ollama (로컬) / OpenAI (클라우드) | **Ollama** |
| 2. Embedding model | nomic-embed-text / bge-m3 | **bge-m3** (다국어 우수) |
| 3. Memory directory | 메모리 저장 디렉토리 | `/tmp/memtomem-test/memories` |
| 4. Storage | SQLite DB 경로 | 기본값 (`~/.memtomem/memtomem.db`) |
| 5. Namespace | 폴더명 기반 자동 네임스페이스 | **yes** |
| 6. Search | 검색 결과 수, time-decay | 기본값 |
| 7. Language | 토크나이저 (Unicode / Korean) | **unicode61** (일반), **kiwipiepy** (한국어 중심) |
| 8. Editor | Claude Code 자동 설정 | **Claude Code** |

> 각 단계에서 `b`(뒤로), `q`(종료) 가능.

### 설정 확인

```bash
uv run mm config show
```

---

## 4. LTM 테스트 — 메모리 추가 및 검색

### 4.1 샘플 데이터 준비

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

### 4.2 인덱싱

```bash
uv run mm index $MEMDIR
```

기대 결과: 3개 파일, 6~8개 chunk 인덱싱됨.

### 4.3 검색

```bash
# 한국어 검색
uv run mm search "웹 프레임워크 선택 이유"

# 영어 → 한국어 교차 검색
uv run mm search "kubernetes monitoring alerts"

# 영어 → 한국어 시맨틱 검색
uv run mm search "why Flask over FastAPI"
```

### 4.4 메모리 추가 (CLI)

```bash
# 메모리 추가
uv run mm add "Docker 이미지 빌드 시간 최적화: multi-stage 빌드로 3분→45초" \
  --tags "docker,optimization"

# 즉시 검색 확인
uv run mm search "Docker 빌드 시간"

# 날짜 기반 조회
uv run mm recall --since 2026-04
```

### 4.5 MCP 서버 연결 (Claude Code)

```bash
# Claude Code에 MCP 서버 등록
claude mcp add memtomem-test -s project -- \
  uv run --directory /tmp/memtomem-test/memtomem memtomem-server
```

Claude Code에서 대화형 테스트:

```
"mem_status 호출해줘"
→ DB path, embedding model, chunk 수 확인

"쿠버네티스 모니터링 관련 내용 검색해줘"
→ mem_search 결과에 kubernetes.md 출현

"오늘 논의한 내용 기록: API 게이트웨이를 Kong으로 선정, 라이선스 확인 필요"
→ mem_add로 저장 → 즉시 검색 가능 확인
```

### 4.6 고급 LTM 기능 (Claude Code에서)

Claude Code에서 자연어로 요청하면 에이전트가 MCP 도구를 호출한다.

```
사용자: "세션 시작해줘. 제목은 인프라 검토"
→ 에이전트가 mem_session_start(title="인프라 검토") 호출 (agent_id는 기본값 "default")

사용자: "워킹 메모리에 현재 작업 저장해줘: focus = Redis 마이그레이션 계획"
→ mem_scratch_set 호출

사용자: "워킹 메모리에서 focus 값 확인"
→ mem_scratch_get 호출

사용자: "네임스페이스 목록 보여줘"
→ mem_do(action="ns_list") 호출

사용자: "태그 목록 보여줘"
→ mem_do(action="tag_list") 호출

사용자: "자동 태깅 미리보기 해줘 (적용하지 말고)"
→ mem_do(action="auto_tag", params={"dry_run": true}) 호출

사용자: "엔티티 추출 미리보기"
→ mem_do(action="entity_scan", params={"dry_run": true}) 호출

사용자: "메모리 시스템 건강 보고서 보여줘"
→ mem_do(action="eval") 호출

사용자: "중복 메모리 있는지 스캔해줘"
→ mem_do(action="dedup_scan") 호출

사용자: "세션 종료. 요약: Redis 마이그레이션 계획 수립, Kong 게이트웨이 선정"
→ mem_session_end 호출

사용자: "세션 기록 보여줘"
→ mem_session_list 호출
```

> **팁**: `mem_do(action="help")` 호출을 요청하면 사용 가능한 61개 액션 전체 목록을 볼 수 있다.

### 4.7 멀티 에이전트 시나리오 (Claude Code에서)

하나의 Claude Code 세션에서 `agent_id`를 바꿔가며 여러 에이전트 역할을 시뮬레이션한다.
각 에이전트는 `agent/{id}` 네임스페이스에 개인 메모리를 갖고, `shared` 네임스페이스를 통해 지식을 공유한다.

**구조:**
```
agent/backend    ← 백엔드 에이전트 전용 메모리
agent/frontend   ← 프론트엔드 에이전트 전용 메모리
shared           ← 모든 에이전트가 접근 가능한 공유 메모리
```

**테스트 흐름:**

```
# ── 1단계: 에이전트 등록 ──

사용자: "백엔드 에이전트 등록해줘. agent_id는 backend, 설명은 'API 서버 담당'"
→ mem_agent_register(agent_id="backend", description="API 서버 담당")
→ agent/backend 네임스페이스 생성, shared 네임스페이스 자동 생성

사용자: "프론트엔드 에이전트도 등록. agent_id는 frontend, 설명은 'React UI 담당'"
→ mem_agent_register(agent_id="frontend", description="React UI 담당")

사용자: "네임스페이스 목록 보여줘"
→ mem_do(action="ns_list")
→ agent/backend, agent/frontend, shared 3개 확인

# ── 2단계: 각 에이전트의 개인 메모리 추가 ──

사용자: "backend 에이전트 네임스페이스에 메모 추가:
  'API rate limiting은 Redis sliding window 방식으로 구현. 분당 100 요청 제한.'"
→ mem_add(content="...", namespace="agent/backend", tags=["api", "rate-limit"])

사용자: "frontend 에이전트 네임스페이스에 메모 추가:
  'React Query로 서버 상태 관리. staleTime 5분, retry 3회 설정.'"
→ mem_add(content="...", namespace="agent/frontend", tags=["react", "state"])

# ── 3단계: 에이전트 범위 검색 ──

사용자: "backend 에이전트 관점에서 'rate limit' 검색해줘"
→ mem_agent_search(query="rate limit", agent_id="backend")
→ agent/backend + shared 범위에서 검색, backend 메모 출현

사용자: "frontend 에이전트 관점에서 'rate limit' 검색해줘"
→ mem_agent_search(query="rate limit", agent_id="frontend")
→ agent/frontend + shared 범위에서 검색, 결과 없음 (backend 전용이므로)

# ── 4단계: 메모리 공유 ──

사용자: "backend의 rate limit 메모를 shared로 공유해줘"
→ mem_agent_share(chunk_id="...", target="shared")
→ shared 네임스페이스에 복사됨

사용자: "이제 frontend 관점에서 다시 'rate limit' 검색"
→ mem_agent_search(query="rate limit", agent_id="frontend")
→ shared 네임스페이스의 공유 메모 출현!

# ── 5단계: 공유 메모리 직접 추가 ──

사용자: "shared 네임스페이스에 팀 전체 결정사항 추가:
  'API 버전닝은 URL path 방식 (v1/v2). Header 방식은 사용하지 않기로 결정.'"
→ mem_add(content="...", namespace="shared", tags=["api", "decision"])

사용자: "backend 관점에서 'API 버전' 검색"
→ 공유 결정사항 출현

사용자: "frontend 관점에서 'API 버전' 검색"
→ 동일한 공유 결정사항 출현
```

**확인 포인트:**
- [ ] 에이전트 등록 시 네임스페이스 자동 생성
- [ ] 개인 메모리는 해당 에이전트 검색에서만 출현
- [ ] `mem_agent_share` 후 다른 에이전트에서도 검색 가능
- [ ] `shared` 네임스페이스는 모든 에이전트 검색에 포함
- [ ] `include_shared=false`로 검색하면 공유 메모리 제외

---

## 5. STM 테스트 — 프록시 기반 자동 메모리 서피싱

STM은 다른 MCP 서버를 프록싱하면서 관련 메모리를 자동으로 주입한다.

### 5.1 STM 설정

```bash
# 대화형 STM 설정 위자드
uv run mm stm init
```

위자드 단계:

| 단계 | 설명 | 선택 |
|------|------|------|
| 1. MCP 클라이언트 감지 | Claude Code, Cursor 등 | 자동 감지됨 |
| 2. 프록시할 서버 선택 | 연결된 MCP 서버 목록 | filesystem 등 선택 |
| 3. 접두어 설정 | 프록시 도구 이름 접두어 | `fs` (filesystem) |
| 4. 압축 전략 | none/truncate/hybrid/selective | **hybrid** (기본) |
| 5. 캐시 | 응답 캐시 활성화 | **yes** |
| 6. Langfuse | 옵저버빌리티 (선택) | 스킵 가능 |
| 7. 설정 저장 | `~/.memtomem/stm_proxy.json` | 자동 |
| 8. STM 활성화 | 에디터 재시작 | 안내에 따라 |

### 5.2 STM 동작 확인

STM이 활성화되면 프록시된 도구 호출 시 자동으로 관련 메모리가 서피싱된다.

Claude Code에서:

```
# 프록시 상태 확인
stm_proxy_stats()

# 파일 읽기 시 자동 서피싱 테스트
# (filesystem 서버가 프록시된 경우)
fs__read_file(path="/some/project/auth.py")
→ 응답에 "## Relevant Memories" 섹션이 자동 추가되는지 확인

# 서피싱 피드백
stm_surfacing_feedback(surfacing_id="...", rating="helpful")
stm_surfacing_feedback(surfacing_id="...", rating="not_relevant")

# 서피싱 통계
stm_surfacing_stats()
```

### 5.3 STM 서피싱 동작 원리

1. 에이전트가 `fs__read_file(path="/src/auth.py")` 호출
2. STM이 경로에서 쿼리 추출: `"src auth py"` (경로 토큰화)
3. LTM에서 관련 메모리 검색 (score ≥ 0.02)
4. 응답 앞에 `## Relevant Memories` 섹션 주입
5. 같은 세션에서 이미 보여준 메모리는 중복 표시 안 함

### 5.4 STM 비활성화

```bash
uv run mm stm reset
```

원래 MCP 설정이 복원되고 STM이 비활성화된다.

---

## 6. Web UI 테스트

```bash
uv run mm web
# 브라우저에서 http://localhost:8080 접속
```

확인 항목:
- **Search**: 한국어/영어 검색, 필터 (source, tag, namespace)
- **Sources**: 인덱싱된 파일 목록, chunk 상세
- **Tags**: 태그 목록, 빈도
- **Sessions**: 세션 기록, 이벤트
- **Health**: eval 결과 (dead memories, tag coverage 등)
- **STM** (활성화 시): 서버 상태, 압축 통계, 서피싱 피드백

---

## 7. 정리

```bash
# Claude Code에서 테스트용 MCP 제거
claude mcp remove memtomem-test

# 테스트 환경 삭제
rm -rf /tmp/memtomem-test

# 기존 설정 복원 (백업했을 경우)
[ -d ~/.memtomem.bak.* ] && rm -rf ~/.memtomem && mv ~/.memtomem.bak.* ~/.memtomem
```

---

## 8. 자동 테스트 상세 (참고)

### 테스트 파일별 커버리지

| 파일 | 수 | 대상 |
|------|---|------|
| `test_server_tools_core.py` | 65 | browse, search, recall, status, CRUD |
| `test_server_tools_org.py` | 41 | namespace, tag, session, scratch |
| `test_server_tools_advanced.py` | 71 | cross_ref, policy, entity, importance, analytics |
| `test_tools_logic.py` | 46 | entity extraction, policy engine, temporal |
| `test_search_stages.py` | 23 | RRF fusion, cosine similarity, MMR |
| `test_chunkers_extended.py` | 37 | Python/JS/TS, JSON/YAML/TOML, registry |
| `test_indexing_engine.py` | 45 | IndexEngine, merge, overlap, watcher |
| `test_embedding_providers.py` | 38 | Ollama/OpenAI (HTTP mocked) |
| `test_storage_extended.py` | 23 | dense_search, FTS rebuild, access counts |
| `test_web_routes.py` | 29 | 13 HTTP endpoints |
| `test_web_routes_extended.py` | 20 | tags, timeline, evaluation 등 9개 route |
| `test_server_helpers.py` | 41 | formatters, date parsing, error handler |
| `test_cli.py` | 35 | CLI 커맨드, config, 인자 파싱 |
| `test_user_workflows.py` | 18 | E2E 시나리오 (Ollama 필요) |
| `test_usability_fixes.py` | 30 | frontmatter, wikilink, FTS5 하이픈 |
| 기타 기존 | ~260 | storage, search, chunking, sessions |
| **STM 전체** | 282 | compression, surfacing, feedback, proxy |

### 영역별 실행

```bash
uv run pytest packages/memtomem/tests/ -v      # Core 819개
uv run pytest packages/memtomem-stm/tests/ -v   # STM 282개
uv run pytest packages/memtomem/tests/test_search_stages.py -v      # RRF, MMR
uv run pytest packages/memtomem/tests/test_chunkers_extended.py -v  # 청커
uv run pytest packages/memtomem/tests/test_web_routes.py -v         # HTTP API
```

---

## 트러블슈팅

### "Ollama not running" 에러

```bash
ollama serve    # Ollama 시작
ollama list     # 모델 확인
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

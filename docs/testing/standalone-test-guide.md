# 독립 환경 테스트 가이드

별도 폴더에서 memtomem을 처음부터 구성하고 테스트하는 가이드.
기존 설정에 영향 없이 격리된 환경에서 전체 기능을 검증할 수 있다.

---

## 사전 준비

| 항목 | 확인 방법 |
|------|----------|
| Python 3.12+ | `python3 --version` |
| Ollama 실행 중 | `ollama list` |
| 임베딩 모델 | `ollama pull bge-m3` (다국어) 또는 `ollama pull nomic-embed-text` (영어) |
| uv 설치 | `uv --version` |

---

## 1. 테스트 환경 생성

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

## 2. 자동 테스트 실행 (1101개)

```bash
# 전체 테스트 (Ollama 실행 중일 때)
uv run pytest

# Ollama 없이 실행 (23개 자동 스킵)
uv run pytest -m "not ollama"

# 패키지별 실행
uv run pytest packages/memtomem/tests/ -v     # Core: 819개
uv run pytest packages/memtomem-stm/tests/ -v # STM:  282개

# 특정 영역만 실행
uv run pytest packages/memtomem/tests/test_search_stages.py -v      # RRF, MMR
uv run pytest packages/memtomem/tests/test_chunkers_extended.py -v  # 청커
uv run pytest packages/memtomem/tests/test_web_routes.py -v         # HTTP API
uv run pytest packages/memtomem/tests/test_embedding_providers.py -v # 임베딩
```

### 테스트 구성 요약

| 파일 | 테스트 수 | 커버리지 |
|------|----------|---------|
| `test_server_tools_core.py` | 65 | browse, search, recall, status, CRUD |
| `test_server_tools_org.py` | 41 | namespace, tag, session, scratch |
| `test_server_tools_advanced.py` | 71 | cross_ref, policy, entity, importance, analytics, history |
| `test_tools_logic.py` | 46 | entity extraction, policy engine, temporal |
| `test_search_stages.py` | 23 | RRF fusion, cosine similarity, MMR |
| `test_chunkers_extended.py` | 37 | Python/JS/TS, JSON/YAML/TOML, registry |
| `test_indexing_engine.py` | 45 | IndexEngine, merge, overlap, watcher |
| `test_embedding_providers.py` | 38 | Ollama/OpenAI (HTTP mocked) |
| `test_storage_extended.py` | 23 | dense_search, FTS rebuild, access counts |
| `test_web_routes.py` | 29 | 13 HTTP endpoints |
| `test_web_routes_extended.py` | 20 | tags, timeline, evaluation 등 9개 추가 route |
| `test_server_helpers.py` | 41 | formatters, date parsing, error handler |
| `test_cli.py` | 35 | CLI 커맨드 등록, 인자 파싱, config |
| `test_user_workflows.py` | 18 | E2E 시나리오 (Ollama 필요) |
| `test_usability_fixes.py` | 30 | frontmatter, wikilink, FTS5 하이픈 |
| 기타 기존 테스트 | ~260 | storage, search, chunking, sessions 등 |
| **STM 전체** | 282 | compression, surfacing, feedback, proxy 등 |

---

## 3. MCP 서버 수동 테스트

### 3.1 격리 설정

기존 `~/.memtomem/` 설정과 충돌하지 않도록 환경변수로 격리한다.

```bash
# 테스트 전용 디렉토리 준비
export TEST_DIR=/tmp/memtomem-test/sandbox
mkdir -p $TEST_DIR/memories $TEST_DIR/db

# 환경변수로 경로 격리
export MEMTOMEM_STORAGE__SQLITE_PATH=$TEST_DIR/db/test.db
export MEMTOMEM_INDEXING__MEMORY_DIRS="[\"$TEST_DIR/memories\"]"
export MEMTOMEM_EMBEDDING__MODEL=bge-m3
export MEMTOMEM_EMBEDDING__DIMENSION=1024
```

### 3.2 샘플 데이터 생성

```bash
cat > $TEST_DIR/memories/architecture.md << 'EOF'
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

cat > $TEST_DIR/memories/kubernetes.md << 'EOF'
## 모니터링 설정

Prometheus + Grafana 스택 도입.
CPU 임계값 80% 초과 시 Slack 알림 설정.
Node exporter로 클러스터 전체 메트릭 수집.

## 배포 전략

Blue-green 배포 적용. 롤백은 이전 ReplicaSet 재활성화 방식.
Canary 배포는 Argo Rollouts로 점진적 전환 (10% → 50% → 100%).
EOF

cat > $TEST_DIR/memories/meeting-2026-04.md << 'EOF'
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

### 3.3 CLI 테스트

```bash
# 인덱싱
uv run mm index $TEST_DIR/memories

# 검색 (한국어)
uv run mm search "웹 프레임워크 선택 이유"

# 검색 (영어 → 한국어 교차검색)
uv run mm search "kubernetes monitoring alerts"

# 날짜 기반 조회
uv run mm recall --since 2026-04

# 메모리 추가
uv run mm add "Docker 이미지 빌드 시간 최적화: multi-stage 빌드로 3분→45초" --tags "docker,optimization"

# 추가한 내용 즉시 검색
uv run mm search "Docker 빌드 시간"

# 설정 확인
uv run mm config show
```

### 3.4 MCP 서버 연결 테스트

```bash
# MCP 서버 시작 (별도 터미널)
uv run memtomem-server
```

다른 터미널에서 Claude Code로 연결:

```bash
# 테스트용 MCP 등록 (프로젝트 스코프)
claude mcp add memtomem-test -s project -- \
  uv run --directory /tmp/memtomem-test/memtomem memtomem-server
```

Claude Code에서 확인:

```
"mem_status 호출해줘"
→ DB path, embedding model, chunk 수 확인

"mem_search로 'Flask' 검색"
→ architecture.md 결과 확인

"mem_do(action='help')"
→ 61개 액션 카탈로그 확인
```

---

## 4. 시나리오별 수동 테스트

### 4.1 기본 CRUD

| 단계 | 명령 | 확인 |
|------|------|------|
| 빈 상태 | `mem_stats` | 에러 없이 0 chunks |
| 추가 | `mem_add(content="테스트 메모", tags=["test"])` | 파일 생성됨 |
| 검색 | `mem_search(query="테스트")` | 결과 1건 |
| 조회 | `mem_read(chunk_id=...)` | 전체 내용 표시 |
| 수정 | `mem_edit(chunk_id=..., new_content="수정됨")` | 내용 변경 |
| 삭제 | `mem_delete(chunk_id=...)` | 삭제 후 검색 0건 |

### 4.2 교차 언어 검색

| 쿼리 언어 | 콘텐츠 언어 | 기대 결과 |
|-----------|-----------|----------|
| 한국어 "쿠버네티스 모니터링" | 한국어 | top-3 내 출현 |
| 영어 "kubernetes monitoring" | 한국어 | top-3 내 출현 |
| 한국어 "검색 증강 생성" | 영어 | bge-m3: top-3, nomic: top-5 |
| 영어 "why Flask" | 한국어 | top-3 내 출현 |

> **참고**: `bge-m3` 모델이 교차 언어 검색에서 `nomic-embed-text`보다 월등히 우수.

### 4.3 네임스페이스

```
mem_index(path="$TEST_DIR/memories", namespace="project-a")
mem_ns_list()                          → project-a 네임스페이스 확인
mem_search(query="Redis", namespace="project-a")  → 네임스페이스 내 검색
mem_ns_rename(old="project-a", new="main-project") → 이름 변경
```

### 4.4 세션 & 워킹 메모리

```
mem_session_start(agent_id="tester", title="테스트 세션")
mem_scratch_set(key="current_task", value="Redis 마이그레이션 검토")
mem_scratch_get(key="current_task")    → 값 반환
mem_session_end(summary="Redis 마이그레이션 계획 완료")
mem_session_list()                     → 세션 기록 확인
```

### 4.5 고급 기능 (mem_do)

```
mem_do(action="help")                  → 전체 액션 목록
mem_do(action="eval")                  → 건강 보고서
mem_do(action="dedup_scan")            → 중복 스캔
mem_do(action="auto_tag")              → 자동 태깅
mem_do(action="entity_scan")           → 엔티티 추출
mem_do(action="tag_list")              → 태그 목록
mem_do(action="export", params={"output_file": "$TEST_DIR/backup.json"})
```

---

## 5. STM 프록시 테스트 (선택)

STM은 다른 MCP 서버를 프록싱하면서 메모리를 자동 주입하는 기능.

```bash
# STM 설치
uv pip install -e "packages/memtomem-stm[ltm]"

# STM 테스트 실행
uv run pytest packages/memtomem-stm/tests/ -v   # 282개 테스트

# 설정 위자드 (인터랙티브)
uv run mm stm init
```

---

## 6. Web UI 테스트 (선택)

```bash
uv run mm web
# 브라우저에서 http://localhost:8080 접속
```

확인 항목:
- 검색 동작 (한국어/영어)
- 소스 목록 + chunk 상세
- 태그 목록
- 세션 목록
- Health report (evaluation 탭)

---

## 7. 정리

```bash
# 테스트 환경 삭제
rm -rf /tmp/memtomem-test

# Claude Code에서 테스트용 MCP 제거
claude mcp remove memtomem-test

# 환경변수 정리
unset MEMTOMEM_STORAGE__SQLITE_PATH
unset MEMTOMEM_INDEXING__MEMORY_DIRS
unset MEMTOMEM_EMBEDDING__MODEL
unset MEMTOMEM_EMBEDDING__DIMENSION
```

---

## 트러블슈팅

### "Ollama not running" 에러

```bash
ollama serve    # Ollama 시작
ollama list     # 모델 확인
```

### "Embedding dimension mismatch"

DB가 다른 모델로 생성된 경우:

```bash
uv run mm embedding-reset --mode apply-current  # DB 리셋
uv run mm index $TEST_DIR/memories               # 재인덱싱
```

### 테스트 일부 스킵됨

`23 skipped` 표시 → Ollama 미실행 시 정상. `@pytest.mark.ollama` 마커가 적용된 테스트는 자동 스킵.

```bash
ollama serve                   # Ollama 시작 후
uv run pytest                  # 전체 1101개 통과
```

### 기존 설정과 충돌

`~/.memtomem/config.json`이 테스트 환경변수를 덮어쓸 수 있음:

```bash
# 임시로 이동
mv ~/.memtomem/config.json ~/.memtomem/config.json.bak
# 테스트 완료 후 복원
mv ~/.memtomem/config.json.bak ~/.memtomem/config.json
```

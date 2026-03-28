# Usability Test Scenarios: memtomem v0.1.0

> 다양한 사용자 페르소나 기반 사용성 테스트 시나리오.
> 실행일: 2026-04-06

---

## Phase 0: 환경 초기화
1. `~/.memtomem/` 디렉토리 백업 후 삭제 (DB + config 완전 초기화)
2. 테스트용 샘플 데이터 준비 (각 시나리오별 markdown 파일)
3. `mem_status`로 빈 상태 확인

---

## 시나리오 1: 첫 사용자 — Claude Code 개발자
**페르소나**: 프로젝트 의사결정을 세션 간 기억하고 싶은 개발자
**테스트 목표**: 최초 셋업 → 기본 CRUD 흐름이 직관적인지

### 테스트 단계
1. **빈 상태 확인**: `mem_stats`, `mem_status` — 에러 없이 "0 chunks" 표시되는지
2. **첫 메모리 추가**: `mem_add(content="프로젝트에서 FastAPI 대신 Flask를 선택한 이유: 팀 내 Flask 경험이 더 많고, 빠른 프로토타이핑이 목표", title="아키텍처 결정: 웹 프레임워크", tags=["architecture", "decision"])`
3. **즉시 검색**: `mem_search(query="웹 프레임워크 선택 이유")` — 방금 추가한 내용이 나오는지
4. **영어 검색**: `mem_search(query="why Flask over FastAPI")` — 한국어 내용이 영어 시맨틱 검색에 걸리는지
5. **목록 확인**: `mem_list()` — 파일 구조가 명확한지
6. **상세 조회**: `mem_read(chunk_id=...)` — 검색 결과의 chunk_id로 전체 내용 조회
7. **추가 메모리**: `mem_add(content="Redis를 캐시 레이어로 도입. 세션 스토어는 별도 PostgreSQL 사용", title="인프라 결정: 캐싱 전략", tags=["architecture", "infra"])`
8. **태그 검색**: `mem_search(query="architecture", tag_filter="decision")`
9. **날짜 기반 조회**: `mem_recall(since="2026-04-06")`

### 관찰 포인트
- [ ] 빈 DB에서 에러 메시지 없이 작동하는가
- [ ] 한국어 콘텐츠의 BM25 토크나이저 동작 (unicode61 vs kiwipiepy)
- [ ] 교차 언어 시맨틱 검색 품질
- [ ] 자동 생성된 파일명/경로가 직관적인가
- [ ] 검색 결과 포맷이 agent가 이해하기 쉬운가

---

## 시나리오 2: 연구자 — 문서/논문 노트 관리
**페르소나**: 논문 리뷰 노트와 연구 메모를 체계적으로 관리하고 싶은 연구자
**테스트 목표**: 파일 인덱싱 → 구조화된 검색 → 네임스페이스 활용

### 테스트 단계
1. **샘플 연구 노트 생성**: 3-4개의 markdown 파일 (다른 주제)
   - `research/llm-scaling.md` — LLM 스케일링 법칙
   - `research/rag-patterns.md` — RAG 아키텍처 패턴
   - `research/evaluation-metrics.md` — 평가 메트릭 정리
2. **디렉토리 인덱싱**: `mem_index(path="<test_dir>/research")` — 전체 디렉토리
3. **통계 확인**: `mem_stats()` — chunk 수가 합리적인지
4. **의미 검색**: `mem_search(query="검색 증강 생성의 청크 크기 최적화")` — RAG 관련 노트가 나오는지
5. **소스 필터**: `mem_search(query="evaluation", source_filter="*.md")`
6. **네임스페이스 할당**: `mem_do(action="namespace_set", params={"source_filter": "research/*", "namespace": "research"})`
7. **네임스페이스 검색**: `mem_search(query="scaling laws", namespace="research")`
8. **태그 관리**: `mem_do(action="tag_list")` — 자동 추출된 태그 확인
9. **관계 설정**: `mem_do(action="link", params=...)` — 관련 청크 연결

### 관찰 포인트
- [ ] 마크다운 헤딩 기반 청킹이 논리적 단위로 나뉘는가
- [ ] 인덱싱 속도와 진행 상황 표시
- [ ] 네임스페이스 설정/검색이 직관적인가
- [ ] source_filter 글롭 패턴 동작

---

## 시나리오 3: 팀 리드 — 미팅 노트 관리
**페르소나**: 회의록을 인덱싱하고 이전 결정사항을 검색하고 싶은 팀 리드
**테스트 목표**: 템플릿 활용 → 대량 추가 → 시간 기반 조회

### 테스트 단계
1. **미팅 템플릿 사용**: `mem_add(content='{"title":"Sprint Planning","attendees":"김철수, 이영희","decisions":"API v2 마이그레이션 4월 말 완료","action_items":"김철수: 엔드포인트 목록 정리"}', template="meeting")`
2. **추가 미팅**: 2-3개 더 추가 (다른 날짜 시뮬레이션)
3. **결정사항 검색**: `mem_search(query="API 마이그레이션 일정")`
4. **날짜 범위 조회**: `mem_recall(since="2026-04-01", until="2026-04-07")`
5. **배치 추가**: `mem_do(action="batch_add", params={"entries": [...]})`
6. **세션 시작**: `mem_do(action="session_start", params={"title": "Sprint Review"})`
7. **세션 내 작업 후 종료**: `mem_do(action="session_end", params={"summary": "..."})`
8. **세션 목록**: `mem_do(action="session_list")`

### 관찰 포인트
- [ ] meeting 템플릿 포맷이 유용한가
- [ ] batch_add 파라미터 형식이 직관적인가
- [ ] 세션 워크플로우가 자연스러운가
- [ ] recall의 날짜 형식 유연성

---

## 시나리오 4: 다국어 사용자 — 한영 혼용
**페르소나**: 한국어와 영어를 혼용하여 메모하는 개발자
**테스트 목표**: 다국어 인덱싱/검색 품질, 토크나이저 동작

### 테스트 단계
1. **한국어 메모**: `mem_add(content="쿠버네티스 클러스터 모니터링 설정 완료. Prometheus + Grafana 스택 사용. 알림 임계값: CPU 80%, 메모리 85%", title="인프라: 모니터링", tags=["k8s", "monitoring"])`
2. **영어 메모**: `mem_add(content="Kubernetes monitoring setup done. Using Prometheus + Grafana. Alert thresholds: CPU 80%, Memory 85%", title="Infra: Monitoring", tags=["k8s", "monitoring"])`
3. **한국어로 검색**: `mem_search(query="쿠버네티스 모니터링 알림 설정")`
4. **영어로 검색**: `mem_search(query="kubernetes monitoring alert thresholds")`
5. **혼합 검색**: `mem_search(query="k8s CPU 임계값")`
6. **BM25 가중치 변경**: `mem_search(query="쿠버네티스", bm25_weight=2.0, dense_weight=0.5)` — 키워드 우선
7. **시맨틱 가중치 변경**: `mem_search(query="container orchestration alerts", bm25_weight=0.5, dense_weight=2.0)` — 의미 우선

### 관찰 포인트
- [ ] 한국어 BM25 토크나이징 품질 (unicode61 기본값으로 충분한가)
- [ ] 교차 언어 시맨틱 검색 정확도 (nomic-embed-text의 다국어 성능)
- [ ] 가중치 조정이 실제 결과 순위에 미치는 영향
- [ ] 한영 혼합 콘텐츠 청킹 품질

---

## 시나리오 5: 파워 유저 — 고급 기능 활용
**페르소나**: memtomem의 고급 기능(정책, 엔티티, 분석)을 활용하는 숙련 사용자
**테스트 목표**: mem_do 메타 도구 탐색성, 고급 워크플로우

### 테스트 단계
1. **도움말 탐색**: `mem_do(action="help")` — 전체 카테고리 목록
2. **카테고리별 상세**: `mem_do(action="help", params={"category": "analytics"})` 등
3. **스크래치 메모**: `mem_do(action="scratch_set", params={...})` → `scratch_get` → `scratch_promote`
4. **엔티티 추출**: `mem_do(action="entity_extract", params=...)`
5. **분석**: `mem_do(action="health_report")`
6. **중복 검사**: `mem_do(action="dedup_scan")`
7. **정책 추가**: `mem_do(action="policy_add", params=...)`
8. **설정 변경**: `mem_do(action="config_show")` → `mem_do(action="config_set", params=...)`
9. **에러 처리**: `mem_do(action="nonexistent")` — 에러 메시지 품질 확인

### 관찰 포인트
- [ ] help 출력이 탐색 가능한 수준인가
- [ ] 고급 기능의 파라미터 형식을 추측할 수 있는가
- [ ] 에러 메시지가 다음 행동을 안내하는가
- [ ] scratch → promote 워크플로우가 자연스러운가

---

## 시나리오 6: 마이그레이션 사용자 — 기존 노트 가져오기
**페르소나**: Obsidian 볼트의 노트를 memtomem으로 가져오려는 사용자
**테스트 목표**: 대량 인덱싱, 다양한 마크다운 형식 호환성

### 테스트 단계
1. **Obsidian 스타일 노트 생성**: 위키링크, 프론트매터, 태그 포함
2. **대량 인덱싱**: `mem_index(path="<vault>", recursive=True)`
3. **청킹 확인**: `mem_list()` — 파일별 chunk 수 확인
4. **프론트매터 검색**: 프론트매터 메타데이터 검색 반영 여부
5. **Obsidian 임포터**: `mem_do(action="import_obsidian", params={"path": "..."})`
6. **강제 재인덱싱**: `mem_index(path="<vault>", force=True)`
7. **정리**: `mem_do(action="cleanup_orphans")`

### 관찰 포인트
- [ ] Obsidian 위키링크/태그 형식 호환
- [ ] 프론트매터(YAML) 파싱 및 검색 반영
- [ ] 대량 파일 인덱싱 시 성능과 에러 처리
- [ ] force 재인덱싱 동작

---

## 결과 수집 프레임워크

| 카테고리 | 설명 |
|----------|------|
| **BUG** | 에러, 크래시, 잘못된 결과 |
| **UX** | 혼란스러운 파라미터, 불명확한 출력, 비직관적 동작 |
| **DOC** | 문서 누락/부정확, 가이드 부족 |
| **PERF** | 느린 응답, 타임아웃 |
| **FEAT** | 없어서 불편한 기능, 개선 아이디어 |

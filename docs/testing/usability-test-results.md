# Usability Test Results: memtomem v0.1.0

> 실행일: 2026-04-06
> MCP 서버 소스: `/Users/pdstudio/Work/agent-harness/memtomem`
> 임베딩: ollama / nomic-embed-text (768d)
> 토크나이저: unicode61

---

## 발견 이슈 요약

### BUG (코드 버그)

| ID | 시나리오 | 심각도 | 설명 | 파일 |
|----|----------|--------|------|------|
| BUG-1 | 1 | Medium | `mem_status`에서 `config.storage.sqlite_path`가 str일 때 `.expanduser()` 호출 실패 (`AttributeError`) | `server/tools/status_config.py:63` |
| BUG-3 | 1 | High | `mem_add`로 같은 파일에 여러 항목 추가 시, 재인덱싱에서 모든 항목이 1개 chunk로 병합되고 이전 태그가 덮어씌워짐 | `server/tools/memory_crud.py` + `chunking/` |
| BUG-6 | 6 | Low | BM25 검색에서 하이픈 포함 쿼리 "in-progress"가 `no such column: progress` SQL 에러 발생 | `search/pipeline.py` or `storage/sqlite_backend.py` |

**BUG-1 상세**:
- `config.json`에서 설정을 로드한 경우 `sqlite_path`가 `Path` 대신 `str`로 남음
- 재현: `~/.memtomem/config.json`이 존재하는 상태에서 `mem_status` 호출
- 수정: `status_config.py:63`에서 `Path(config.storage.sqlite_path).expanduser()` 사용, 또는 config 로드 시 Path 변환 보장

**BUG-3 상세**:
- `mem_add`가 날짜별 파일(`2026-04-06.md`)에 여러 `## ` 섹션을 추가하지만, 재인덱싱 시 마크다운 청커가 작은 섹션들을 512토큰 제한 내에서 하나의 chunk로 병합
- 결과: 3개 항목(아키텍처/인프라/미팅)이 1개 chunk가 됨
- 태그 문제: `mem_add`의 태그 적용 로직이 chunk 단위로 동작하므로, 마지막 add의 태그만 적용되고 이전 태그 소실
- 영향: `tag_filter` 검색 실패, 태그 기반 관리 불가능
- 근본 원인: 청킹 전략과 per-entry 태깅의 불일치

**BUG-6 상세**:
- 쿼리 "status in-progress project"에서 BM25가 "in-progress"를 "in", "-", "progress"로 토크나이징한 후 FTS5 쿼리에서 "progress"를 컬럼명으로 해석
- 파이프라인 출력에 `BM25-err:no such column: progress` 표시
- 검색 자체는 Dense로 fallback하여 결과를 반환하지만, BM25 기여분 손실

---

### UX (사용성 이슈)

| ID | 시나리오 | 설명 | 개선 방향 |
|----|----------|------|-----------|
| UX-2 | 1 | `mem_recall`이 같은 파일의 여러 항목을 1개 chunk로 표시 | 항목별 분리된 결과 기대 |
| UX-4 | 2 | 한국어 쿼리로 영어 콘텐츠 검색 시 랭킹 정확도 낮음 (관련 결과 #5) | 교차 언어 검색 품질 개선 |
| UX-5 | 2 | 한국어 쿼리 vs 영어 콘텐츠에서 BM25 히트 0 — dense만 동작 | 기대 동작이지만 문서화 필요 |
| UX-6 | 2 | macOS에서 `/tmp` → `/private/tmp`로 표시되어 사용자 혼란 | symlink resolve 후 원래 경로 유지 |
| UX-7 | 2 | 기존 chunk에 네임스페이스 일괄 할당하는 방법 없음 (재인덱싱 필요) | `ns_assign` 액션 추가 검토 |
| UX-8 | 2 | 인덱싱 시 자동 태그 추출 없음 — 수동 태깅만 가능 | `auto_tag` 존재하지만 인덱싱과 연동 안 됨 |
| UX-9 | 2 | 작은 마크다운 섹션들이 과도하게 병합됨 (3~5개 섹션 → 1~2 chunk) | `min_chunk_tokens` 튜닝 또는 entry 경계 인식 개선 |
| UX-10 | 3 | 미팅 템플릿이 `## Entry <timestamp>` + `## Meeting: <title>` 이중 헤딩 생성 | 템플릿 적용 시 외부 Entry 헤딩 생략 |
| UX-11 | 3 | JSON에 없는 템플릿 필드가 `(fill: agenda)` 플레이스홀더로 표시 | 빈 필드 자동 생략 |
| UX-12 | 3 | `session_start`에 `title` 파라미터 없음 — `agent_id`만 가능 | 세션에 의미 있는 이름 부여 방법 필요 |
| UX-13 | 4 | 한국어→영어 교차 검색이 약함 (영어→한국어는 잘 됨) | nomic-embed-text 다국어 비대칭 — 문서에 명시 |
| UX-14 | 5 | 사용자가 "health_report" 추측 → 실제는 "eval" | 직관적 액션명 또는 별칭(alias) 지원 |
| UX-15 | 5 | `policy_type` 유효값이 help에 표시 안 됨 (에러에서만 확인 가능) | enum 값을 help 출력에 포함 |
| UX-16 | 5 | `config` 출력의 `sqlite_path`가 문자열 — Path 직렬화 이슈 | BUG-1과 동일 근본 원인 |
| UX-17 | 6 | Obsidian 파일의 여러 섹션이 1 chunk로 병합 (4섹션→1chunk) | UX-9와 동일 |
| UX-18 | 6 | YAML 프론트매터의 `tags`가 memtomem 태그로 변환 안 됨 | 프론트매터 태그 자동 파싱 |
| UX-19 | 6 | Obsidian 위키링크 `[[link]]`가 raw 텍스트로 남음 | 위키링크 파싱/정리 |
| UX-20 | 6 | 소스 파일 삭제 후 고아 chunk 정리 도구 없음 | `cleanup_orphans` 액션 추가 |

---

### DOC (문서 보완)

| ID | 설명 |
|----|------|
| DOC-1 | 교차 언어 검색 한계(특히 한국어→영어 방향)를 user-guide에 명시 필요 |
| DOC-2 | `unicode61` 토크나이저의 한국어 처리 한계와 `kiwipiepy` 전환 가이드 필요 |
| DOC-3 | `mem_add`로 같은 날짜 파일에 반복 추가 시 chunk 병합 동작 설명 필요 |
| DOC-4 | `mem_do` 액션명 네이밍 규칙 정리 (ns_* vs namespace_*, 등) |
| DOC-5 | Obsidian 임포터 사용 시 프론트매터/위키링크 처리 방식 문서화 |
| DOC-6 | MCP 서버 디렉토리 경로 변경 후 재연결 가이드 (문제 해결 섹션) |

---

### FEAT (기능 개선 아이디어)

| ID | 설명 | 우선순위 |
|----|------|----------|
| FEAT-1 | 프론트매터 YAML `tags` → memtomem 태그 자동 변환 | High |
| FEAT-2 | `mem_add` 시 같은 파일 내 항목별 독립 chunk 보장 | High |
| FEAT-3 | `ns_assign`: 기존 chunk에 네임스페이스 일괄 할당 | Medium |
| FEAT-4 | `cleanup_orphans`: 삭제된 소스의 고아 chunk 정리 | Medium |
| FEAT-5 | `session_start`에 `title` 파라미터 추가 | Low |
| FEAT-6 | `policy_type` 등 enum 파라미터를 help에 자동 표시 | Low |
| FEAT-7 | Obsidian 위키링크 `[[link]]` 파싱 → 관계(relation) 자동 생성 | Medium |
| FEAT-8 | 미팅 템플릿: 빈 필드 자동 생략, 이중 헤딩 방지 | Low |

---

## 정상 작동 확인 항목

| 기능 | 결과 |
|------|------|
| 빈 DB에서 `mem_stats`, `mem_status` | 에러 없이 정상 |
| `mem_add` → 즉시 검색 가능 | 정상 |
| 영어 쿼리로 한국어 콘텐츠 검색 (교차 언어) | 정상 (양방향) |
| `mem_list`, `mem_read` 출력 포맷 | 명확하고 유용 |
| 디렉토리 인덱싱 (`mem_index`) | 빠르고 정상 (129ms/3파일) |
| 네임스페이스 인덱싱 + 검색 | 정상, `[namespace]` 라벨 표시 |
| `source_filter` 서브스트링 매칭 | 정상 |
| 세션 워크플로우 (start/end/list) | 정상 |
| 스크래치 (set/get/promote) | 정상 |
| `mem_do help` 탐색 | 명확하고 체계적 |
| `eval` (건강 보고서) | 유용한 출력 |
| `dedup_scan` | 정상 |
| `policy_add/list` | 정상 |
| `config` 전체 출력 | 깔끔 |
| 에러 메시지 (unknown action) | 적절한 안내 |
| `bm25_weight/dense_weight` 조정 | 결과 순위에 명확한 영향 |
| Obsidian 임포터 (`import_obsidian`) | 정상 작동 |
| 강제 재인덱싱 (`force=True`) | 정상 |
| 미팅 템플릿 렌더링 | 기본 동작 정상 (개선 필요 사항 별도) |

---

## 대응 결과

모든 이슈가 5개 커밋에 걸쳐 수정됨:

| 커밋 | 수정 내용 |
|------|-----------|
| `f5e2d75` | BUG-1, BUG-6, UX-10/11/15/18 — sqlite_path, FTS5 하이픈, 템플릿, help, 프론트매터 |
| `68f45c0` | BUG-3 — chunk 병합 방지 (heading 경계) + 태그 소실 방지 |
| `1431aec` | UX-6/12/19/20 — 경로 표시, 세션 title, 위키링크, cleanup_orphans |
| `feb89ae` | DOC-1~6 — 교차 언어 검색, 토크나이저, Obsidian, 트러블슈팅 |
| `a31fe19` | UX-7/8/14 — ns_assign, auto_tag 연동, 액션 별칭 |

### STM 프록시 개선 (추가 커밋)

| 커밋 | 수정 내용 |
|------|-----------|
| `267d468` | 파일 경로 토큰화 (쿼리 추출 개선), script/style 콘텐츠 완전 제거 |
| `41efd5d` | FieldExtract dict 미리보기, 압축 전략 자동 선택, Auto-tuner 콜드 스타트 |
| `fcfbf41` | 섹션 경계 truncation — 헤딩에서 자르고 나머지 섹션 제목 나열 |
| `ecabe21` | 피드백→검색 부스트 — helpful 피드백이 access_count 증가 |
| `d5706c8` | 서피싱 과부하 방지 — 세션 중복 억제, 부스트 1회 제한, 주입 크기 상한 |

---

## bge-m3 vs nomic-embed-text 비교 (2026-04-06)

동일한 테스트 데이터로 교차 언어 검색 품질 비교.

| 테스트 쿼리 | nomic-embed-text (768d) | bge-m3 (1024d) |
|------------|------------------------|----------------|
| KR "쿠버네티스 모니터링" → EN 결과 | top-3에 미출현 | **#2** |
| EN "kubernetes monitoring" → KR 결과 | #2 | #2 |
| KR "검색 증강 생성 청크 크기" → EN RAG 문서 | #5 (랭킹 낮음) | **#1** |
| EN "container orchestration alerts" → KR | #2 | **#1** |
| 혼합 "k8s CPU 임계값" | #1 kr, #2 en | #1 kr, #2 en |
| EN "why Flask" → KR 아키텍처 결정 | #1 | #1 |

**결론**: 한국어 포함 다국어 환경에서는 bge-m3을 기본 모델로 권장.
인덱싱 속도: nomic 129ms vs bge-m3 290ms (약 2.2x, 허용 범위).

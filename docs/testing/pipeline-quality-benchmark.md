# STM 파이프라인 품질 벤치마크

## 개요

STM 파이프라인(Clean → Compress → Surface)이 MCP 도구 응답을 처리한 후에도 **작업 결과 품질이 유지되는지** 측정하는 벤치마크 프레임워크.

**핵심 질문:** 압축이 정보를 얼마나 보존하는가? 서피싱이 실제로 도움이 되는가?

### 평가 방식: A/B 비교

| 모드 | 파이프라인 | 기준 |
|------|-----------|------|
| **Direct** (baseline) | 원본 텍스트 그대로 | quality = 10.0 |
| **STM-proxied** | Clean → Compress → (Surface) | quality 측정 |

**Quality preservation** = STM quality / Direct quality × 100%

---

## 데이터셋

### 규모

| 구분 | Tasks | QA Pairs |
|------|-------|----------|
| Original (`datasets.py`) | 13 | 51 |
| Expanded (`datasets_expanded.py`) | 22 | 77 |
| **전체** (`full_benchmark_suite()`) | **35** | **128** |

### 카테고리별 구성

| 카테고리 | Tasks | 설명 | 주요 도전 |
|---------|-------|------|----------|
| **json** | 5 | API 응답, 설정, 이벤트 스트림, GraphQL, 시계열 메트릭 | 중첩 구조 보존, 핵심 필드 추출 |
| **markdown** | 5 | 기술 문서, 회의록, 변경 로그, 튜토리얼, API 레퍼런스 | 헤딩 구조, 코드 블록 보존 |
| **code** | 4 | Python ETL, TypeScript hooks, Go 서버, SQL 분석 | 함수/클래스 구조, 핵심 로직 |
| **text** | 5 | 장애 보고서, 논문 초록, 법률 조항, 이메일 스레드, 지원 티켓 | 시간순 정보, 수치 데이터 |
| **multilingual** | 3 | 한국어 기술 문서/회의록, 일본어 API 응답 | 다국어 키워드 보존 |
| **large_doc** | 2 | 200+ API 로그 (10K+), RFC 문서 (12K+) | 대용량에서 핵심 정보 추출 |
| **edge_case** | 7 | 빈 응답, 공백, 깨진 JSON, 에러 응답, 바이너리, 단일 라인, 반복 | 파이프라인 안정성 |
| **surfacing** | 4 | 불완전 응답 + 메모리 주입, 멀티메모리 디버깅, 충돌 해결 | 메모리가 정보 공백 메움 |

### 각 Task 구조

```python
BenchTask(
    task_id="text-incident-report",
    description="P1 incident report with timeline, root cause, and remediation",
    content="...",                          # 원본 텍스트
    content_type="text",                    # json | markdown | code | text
    max_chars=800,                          # 압축 예산
    expected_keywords=["INC-2025-0613", "payment-gateway", ...],  # 보존 필수 키워드
    keyword_weights=[...],                  # 키워드별 가중치 (선택)
    qa_pairs=[                              # 답변 기반 품질 측정
        QAPair("How long was the incident?", "95 minutes"),
        QAPair("What was the root cause?", "schema migration"),
    ],
    surfacing_memories=[...],               # 서피싱 테스트용 메모리 (선택)
)
```

---

## 채점 시스템

### 1. RuleBasedJudge (결정론적)

| 기준 | 점수 |
|------|------|
| 기본 점수 | 10.0 |
| 키워드 미보존 | -2.0 × weight (per keyword) |
| 헤딩 수 미달 | -1.0 |
| 코드 블록 미달 | -1.0 |
| JSON 유효성 보너스 | +0.5 |
| 범위 | [0.0, 10.0] |

**QA 채점:** 각 QA pair의 answer가 출력에 포함되는지 확인 → answerable / total

### 2. LLMJudge (시맨틱)

LLM (Claude Haiku / GPT-4o-mini)을 사용한 의미적 품질 평가:

| 차원 | 설명 |
|------|------|
| **Factual Completeness** | 핵심 사실이 보존되었는가? |
| **Structural Coherence** | 출력이 잘 구조화되었는가? |
| **Answer Sufficiency** | 주요 질문에 답할 수 있는가? |

- 비용 제어: 콘텐츠 해시 기반 캐싱, `@pytest.mark.llm_judge`로 분리
- 교정: `compute_correlation()`으로 RuleBasedJudge와의 Pearson r / Spearman ρ 측정

---

## 평가 결과

### 전체 요약

- **Tasks:** 34 (빈 응답 제외)
- **Mean quality:** 6.59/10 (±2.90)
- **95% CI:** [5.56, 7.48]
- **Quality preservation:** 66.5%
- **Wilcoxon signed-rank:** W=0.0, p<0.0001 (유의미한 품질 차이)

### 카테고리별 결과

| 카테고리 | N | 평균 품질 | 표준편차 | 중앙값 | 보존율 | 95% CI |
|---------|---|----------|---------|-------|--------|--------|
| edge_case | 6 | 9.0 | 2.4 | 10.0 | 90% | [7.0, 10.0] |
| surfacing | 4 | 8.5 | 3.0 | 10.0 | 85% | [5.5, 10.0] |
| multilingual | 3 | 7.7 | 3.2 | 9.0 | 77% | [4.0, 10.0] |
| text | 5 | 7.2 | 1.1 | 8.0 | 72% | [6.4, 8.0] |
| json | 5 | 5.6 | 2.7 | 6.0 | 56% | [3.6, 7.7] |
| large_doc | 2 | 5.0 | 1.4 | 5.0 | 50% | [4.0, 6.0] |
| code | 4 | 4.5 | 3.0 | 5.0 | 50% | [2.0, 7.0] |
| markdown | 5 | 4.2 | 2.6 | 4.0 | 42% | [2.2, 6.4] |

### 태스크별 상세

| Task | Direct | STM | 보존율 | 압축률 |
|------|--------|-----|-------|--------|
| ml-kr-vector-guide | 10.0 | 10.0 | 100% | 34% |
| surf-incident-with-history | 10.0 | 10.0 | 100% | 59% |
| surf-multi-memory-debug | 10.0 | 10.0 | 100% | 0% |
| surf-conflict-resolution | 10.0 | 10.0 | 100% | 0% |
| ml-kr-meeting-notes | 10.0 | 9.0 | 90% | 39% |
| json-api-users | 10.0 | 8.2 | 82% | 77% |
| text-research-abstract | 10.0 | 8.0 | 80% | 59% |
| text-legal-dpa | 10.0 | 8.0 | 80% | 54% |
| md-vector-db-guide | 10.0 | 8.0 | 80% | 66% |
| json-graphql-repo | 10.0 | 8.0 | 80% | 62% |
| text-support-ticket | 10.0 | 8.0 | 80% | 61% |
| code-ts-hooks | 9.0 | 7.0 | 78% | 78% |
| code-go-server | 9.0 | 7.0 | 78% | 56% |
| text-incident-report | 10.0 | 6.0 | 60% | 59% |
| json-event-stream | 10.0 | 6.0 | 60% | 70% |
| text-email-thread | 10.0 | 6.0 | 60% | 65% |
| large-api-logs | 10.0 | 6.0 | 60% | 98% |
| md-sprint-retro | 10.0 | 5.0 | 50% | 67% |
| code-python-etl | 9.0 | 3.0 | 33% | 79% |
| md-changelog | 10.0 | 3.0 | 30% | 58% |
| json-app-config | 10.0 | 2.0 | 20% | 65% |
| md-api-docs | 10.0 | 1.0 | 10% | 55% |
| code-sql-analytics | 9.0 | 1.0 | 11% | 74% |

### 전략별 비교 (대표 태스크)

| Task | truncate | hybrid | extract_fields | 최적 |
|------|----------|--------|---------------|------|
| json-api-users | 8.2 | 8.2 | **8.7** | extract_fields |
| md-vector-db-guide | **8.0** | 4.0 | 0.0 | truncate |
| code-python-etl | 3.0 | 3.0 | **5.0** | extract_fields |
| text-incident-report | **6.0** | 4.0 | 6.0 | truncate |
| ml-kr-vector-guide | **10.0** | 10.0 | 0.0 | truncate/hybrid |
| large-rfc-event-arch | **4.0** | 3.0 | 1.0 | truncate |

### 압축 커브 (예산 비율 → 품질)

```
json-api-users:     30% → 8.8   50% → 9.2   70% → 9.4   90% → 10.0
code-python-etl:    30% → 3.0   50% → 5.0   70% → 5.0   90% → 7.0
text-incident-report: 30% → 4.0   50% → 8.0   70% → 8.0   90% → 10.0
```

### 단계별 품질 분석 (Stage Breakdown)

정보가 **어디서** 손실되는지 분석:

```
json-api-users:
  original    1710 chars  quality=10.0  QA=4/4
  cleaned     1710 chars  quality=10.0  QA=4/4  ← clean 손실 없음
  compressed   392 chars  quality=8.2   QA=1/4  ← compress에서 -1.8, QA 3개 손실

md-vector-db-guide:
  original    2326 chars  quality=10.0  QA=4/4
  cleaned     2325 chars  quality=10.0  QA=4/4  ← clean 손실 없음
  compressed   800 chars  quality=8.0   QA=2/4  ← compress에서 -2.0, QA 2개 손실

code-python-etl:
  original    3894 chars  quality=9.0   QA=3/4
  cleaned     3893 chars  quality=9.0   QA=3/4  ← clean 손실 없음
  compressed   821 chars  quality=3.0   QA=1/4  ← compress에서 -6.0, QA 2개 손실
```

**관찰:** Clean 단계는 정보 손실 없음. 모든 정보 손실은 Compress 단계에서 발생.

---

## 분석 도구

### 5가지 분석 모드

| 도구 | 메서드 | 용도 |
|------|--------|------|
| **A/B 비교** | `run_comparison()` | direct vs STM 기본 비교 |
| **전략 매트릭스** | `run_strategy_matrix()` | 1 task × N strategies |
| **압축 커브** | `run_compression_curve()` | 예산별 품질 변화 |
| **단계별 분석** | `run_stage_breakdown()` | 어디서 정보가 손실되는가 |
| **서피싱 가치** | `measure_surfacing_value()` | 메모리 주입의 실제 효과 |

### 통계 도구

| 도구 | 함수 | 설명 |
|------|------|------|
| **Bootstrap CI** | `bootstrap_ci()` | 1000 resamples, 95%/99% 신뢰구간 |
| **Wilcoxon test** | `wilcoxon_signed_rank()` | 쌍체 유의차 검정 |
| **카테고리 집계** | `aggregate_by_category()` | mean/std/median/min/max per category |
| **통합 요약** | `compute_summary()` | BenchmarkSummary (overall + categories + Wilcoxon) |

### 출력 포맷

| 포맷 | 함수 | 용도 |
|------|------|------|
| Markdown | `format_markdown_table()` | 블로그, 문서 |
| LaTeX | `format_latex_table()` | 논문 |
| Strategy table | `format_strategy_table()` | 전략 비교 (MD/LaTeX) |

---

## 핵심 발견

### 1. 전략 선택이 품질에 결정적

- **JSON** → `extract_fields`가 최적 (8.7 vs truncate 8.2)
- **Markdown** → `truncate`가 최적 (8.0 vs hybrid 4.0)
- **Code** → task에 따라 다름 (ETL은 extract_fields, hooks는 truncate)
- **Text** → `truncate`가 안정적 (6.0-8.0)
- `auto_select_strategy()`는 JSON에서 정확, markdown에서는 hybrid를 선택하지만 truncate가 더 나은 경우 있음

### 2. 예산 50%가 실용적 기준점

- JSON: 50% 예산에서 9.2/10 달성
- Text: 50% 예산에서 8.0/10 달성
- Code: 50% 예산에서도 5.0/10 — 코드는 더 많은 예산 필요

### 3. Clean 단계는 정보를 보존

- HTML/스크립트 제거, 링크 축약, 중복 제거 후에도 키워드/QA 손실 없음
- 크기 감소는 주로 HTML이 많은 콘텐츠에서 발생

### 4. 서피싱이 정보 공백을 채움

- `surf-api-with-context`: 메모리 없이 QA 2/4 → 메모리 주입 후 6/6
- `surf-incident-with-history`: 이전 장애 맥락 추가로 원인 분석 완성

### 5. 다국어 지원 양호

- 한국어 기술 문서: 100% 보존 (budget 여유 시)
- 일본어 JSON: 40% (FieldExtract가 일본어 키를 잘 처리 못함 → 개선 필요)

### 6. 약점 영역

- **중첩 JSON 이벤트** (event-stream): 깊은 구조에서 핵심 값 손실
- **코드 (ETL, SQL)**: 함수 본문이 잘리면 로직 손실 큼
- **Markdown API 문서**: 엔드포인트가 많으면 뒤쪽 내용 손실
- **일본어 JSON**: FieldExtract가 일본어 키명을 처리 못함

---

## 회귀 게이트

CI에서 사용할 수 있는 품질 임계값:

```python
# 전체 평균
assert summary.overall.mean_quality >= 5.0      # 최소 평균 품질

# 카테고리별
assert cat_stats["text"].mean_quality >= 6.0    # 텍스트는 높은 기준
assert cat_stats["edge_case"].mean_quality >= 8.0  # 엣지 케이스 안정성

# 개별 태스크
for comp in comparisons:
    assert comp.stm.quality_score > 0, "파이프라인 크래시 없어야 함"
```

---

## 실행 가이드

```bash
# 전체 벤치마크 테스트 (180 tests)
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py -v

# 특정 클래스만
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py::TestFullPipeline -v
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py::TestExpandedDatasets -v
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py::TestStatsSummary -v

# LLM Judge 테스트 (mock — 비용 없음)
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py::TestLLMJudge -v

# 실제 LLM Judge (비용 발생 — 별도 실행)
# ANTHROPIC_API_KEY=... uv run pytest -m llm_judge -v
```

### 프로그래밍 방식 사용

```python
from bench.harness import BenchHarness
from bench.judge import RuleBasedJudge
from bench.datasets_expanded import full_benchmark_suite, full_category_map
from bench.stats import compute_summary, format_markdown_table
from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import TruncateCompressor
from memtomem_stm.proxy.config import CleaningConfig

# Setup
cleaner = DefaultContentCleaner(CleaningConfig(strip_html=True, collapse_links=True, deduplicate=True))
harness = BenchHarness(cleaner=cleaner, compressor=TruncateCompressor(), judge=RuleBasedJudge())

# Run
tasks = [t for t in full_benchmark_suite() if len(t.content) > 0]
comparisons = [harness.run_comparison(t) for t in tasks]

# Stats
summary = compute_summary(comparisons, category_map=full_category_map())
print(format_markdown_table(summary))
```

---

## 파일 구조

```
packages/memtomem-stm/tests/bench/
├── __init__.py
├── harness.py              # BenchHarness, BenchTask, StageMetrics, 데이터 클래스
├── judge.py                # RuleBasedJudge (키워드 + QA)
├── llm_judge.py            # LLMJudge (시맨틱 평가, 캐싱, 상관 분석)
├── datasets.py             # 원본 13 tasks (json/md/code/text/surfacing)
├── datasets_expanded.py    # 확장 22 tasks (multilingual/large/edge/추가)
├── tasks.py                # 레거시 tasks (8 + needle/distractor/multihop)
├── report.py               # 리포트 포매터 (comparison/matrix/curve/breakdown)
└── stats.py                # 통계 (bootstrap CI, Wilcoxon, LaTeX/markdown)
```

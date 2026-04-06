# STM 파이프라인 품질 벤치마크

> 브랜치: `feat/mcp-bench-quality-eval` | 90 tests | STM 총 372 tests
> 최종 업데이트: 2026-04-06

---

## 목적

STM 파이프라인(Clean → Compress → Surface)이 MCP 도구 응답을 처리한 후에도 **에이전트가 작업을 수행하는 데 필요한 핵심 정보가 보존되는지** 정량적으로 측정한다.

### 무엇을 측정하는가

| 측정 항목 | 방법 | 의미 |
|-----------|------|------|
| **품질 보존율** | direct(원본) vs STM(처리후) 키워드/구조 비교 | 압축 후에도 에이전트가 작업 가능한가 |
| **단계별 압축률** | clean→compress→surface 각 단계 크기 변화 | 어느 단계가 가장 효과적인가 |
| **전략별 비교** | 동일 콘텐츠에 4개 전략 적용 | 콘텐츠 유형별 최적 전략 |
| **예산 민감도** | 30%/50%/70%/90% 예산별 품질 곡선 | 최적 예산 지점 |
| **서피싱 오버헤드** | 메모리 주입 전후 크기 차이 | 서피싱이 컨텍스트를 얼마나 늘리는가 |

---

## 평가 구조

### A/B 비교 방식

```
┌─────────────────┐         ┌─────────────────────────────┐
│   Direct Mode   │         │       STM Pipeline          │
│                 │         │                             │
│  원본 텍스트 →   │         │  원본 → Clean → Compress →  │
│  RuleBasedJudge │         │  (→ Surface) → Judge        │
│  → Score A      │         │  → Score B                  │
└────────┬────────┘         └──────────────┬──────────────┘
         │                                 │
         └───────── 비교 ──────────────────┘
                     ↓
         품질 보존율 = B / A × 100%
```

- **Direct**: 원본 텍스트를 그대로 Judge에 전달 (baseline)
- **STM**: Clean → Compress → (Surface) 파이프라인 거친 결과를 Judge에 전달

### RuleBasedJudge 채점 기준

| 기준 | 점수 | 설명 |
|------|------|------|
| 키워드 보존 | -2.0 × weight | expected_keywords 중 누락된 것마다 감점 |
| 헤딩 보존 | -1.0 | 마크다운 헤딩 수가 기대치 미달 시 |
| 코드 블록 보존 | -1.0 | 코드 블록 수가 기대치 미달 시 |
| JSON 유효성 | +0.5 | JSON 콘텐츠가 파싱 가능하면 보너스 |

- 만점 10.0, 최저 0.0
- 키워드별 가중치(keyword_weights)로 critical/nice-to-have 구분

---

## 데이터셋

### 8개 벤치마크 태스크

| Task ID | 유형 | 크기 | 예산 | 핵심 키워드 | 최적 전략 |
|---------|------|------|------|------------|----------|
| `api_response_json` | JSON | 5,708 | 1,000 | Alice, admin, total, has_more | extract_fields |
| `code_file_large` | Code | 2,062 | 1,500 | JWT, access_token, validate_token, middleware | hybrid |
| `meeting_notes` | Markdown | 931 | 800 | PostgreSQL, Kim Cheolsu, April 15, Grafana | truncate |
| `html_mixed` | HTML+Text | 2,429 | 800 | API Reference, Endpoints, authentication, admin | truncate |
| `short_response` | Text | 28 | 1,000 | OK, saved | none |
| `markdown_with_links` | Markdown | 4,728 | 600 | microservices, gRPC, API gateway | hybrid |
| `multilingual_kr_en` | Markdown | 775 | 1,000 | FastAPI, PostgreSQL, Redis, Kubernetes | truncate |
| `large_diff_output` | Code | 1,094 | 800 | verify_token, TokenPayload, Breaking change, alembic | hybrid |

### 데이터셋 설계 원칙

- **현실적 콘텐츠**: MCP 도구가 실제 반환하는 형태 (API 응답, 코드, 회의록, HTML 문서, diff)
- **다양한 유형**: JSON, markdown, code, HTML, 짧은 응답, 한영 혼합
- **Ground truth**: `expected_keywords`가 정답 — 압축 후에도 이 키워드가 존재하면 "에이전트가 작업 가능"
- **가중치**: `keyword_weights`로 critical(1.0) vs nice-to-have(0.5) 구분
- **카테고리**: `TASK_CATEGORIES`로 유형별 그룹 분석
- **예산 변형**: `get_tight_tasks()`(0.5×), `get_generous_tasks()`(2×)로 예산 민감도 테스트

---

## 분석 도구

### 1. Auto-Strategy 비교

`auto_select_strategy()`가 콘텐츠 유형에 따라 최적 전략을 자동 선택하고, 고정 전략 대비 개선 효과를 측정.

```python
report = harness.run_auto_strategy(task)
# → auto가 JSON에 extract_fields를 선택하면 quality 70% → 89%
```

### 2. Strategy Matrix

동일 태스크에 4개 전략(truncate, hybrid, extract_fields, auto)을 모두 적용하여 비교.

```python
results = harness.run_strategy_matrix(task)
# → {"truncate": StrategyResult(quality=7.0, ratio=18.1%, ...), ...}
```

### 3. Compression Curve

동일 태스크를 30%/50%/70%/90% 예산에서 실행하여 quality vs ratio 곡선 생성.

```python
points = harness.run_compression_curve(task)
# → [CurvePoint(budget=30%, quality=6.2), ..., (90%, 10.0)]
```

### 4. Selective 2-Phase

SelectiveCompressor의 TOC→select 흐름을 벤치마크. Phase 1에서 전체 목차, Phase 2에서 상위 N개 섹션 선택 후 품질 측정.

```python
result = harness.run_selective_2phase(task, select_top_n=3)
# → SelectiveResult(toc_entries=5, recovery_ratio=0.6, quality=8.0)
```

### 5. ProxyManager 통합

실제 ProxyManager에 mock upstream을 주입하여 full pipeline (upstream → clean → compress → surface → metrics) 동작 검증.

```python
# mock MCP 서버로 실제 파이프라인 구동
result = await manager._call_tool_inner("bench", "read_file", {})
```

---

## 벤치마크 결과

### Auto-Strategy (compression fix 적용 후)

| Task | 전략 | 품질 | 압축률 | 보존율 |
|------|------|------|--------|--------|
| api_response_json | FieldExtract | 8.9/10 | 94% | **89%** |
| code_file_large | Hybrid | 10.0/10 | 31% | **100%** |
| meeting_notes | Hybrid | 10.0/10 | 14% | **100%** |
| html_mixed | Truncate | 10.0/10 | 81%* | **100%** |
| short_response | Truncate | 10.0/10 | 0% | **100%** |
| markdown_with_links | Hybrid | 10.0/10 | 94% | **100%** |
| multilingual_kr_en | Hybrid | 10.0/10 | 0% | **100%** |
| large_diff_output | Hybrid | 6.4/10 | 27% | ⚠️ 64% |

*html_mixed 81%: clean 단계에서 HTML/script/style 제거, 압축 불필요

**평균: 94.1% quality preservation, 43% compression**
**통과: 7/8 (≥80% threshold)**

### Compression Fix 전후 비교

| Task | Before | After | 원인 |
|------|--------|-------|------|
| markdown_with_links | ⚠️ 40% | **100%** | `_LINK_LINE_RE` regex 버그 수정 |
| large_diff_output | ⚠️ 64% | ⚠️ 64% (hybrid) / **100%** (truncate) | Summary 보존은 truncate에서만 동작 |

### Strategy Matrix 예시: api_response_json

| 전략 | 품질 | 압축률 | 크기 |
|------|------|--------|------|
| **extract_fields** | **8.9** | 94.1% | 335 |
| auto(extract_fields) | 8.9 | 94.1% | 335 |
| truncate | 7.0 | 81.9% | 1,035 |
| hybrid | 7.0 | 82.5% | 1,000 |

→ JSON에는 extract_fields가 최적: **94% 압축 + 89% quality**

### Compression Curve 예시: code_file_large (Truncate)

| 예산 | 품질 |
|------|------|
| 30% | 6.2 |
| 50% | 8.4 |
| **70%** | **10.0** |
| 90% | 10.0 |

→ **70% 예산이 최적 지점** (비용 대비 quality 최대)

---

## 핵심 인사이트

### 1. auto_select가 JSON에서 27% quality 향상

`auto_select_strategy()`가 JSON에 `extract_fields`를 선택 → 키 구조 보존으로 quality 70% → 89%.
Truncate는 JSON 배열의 앞부분만 남기고 잘라버려서 `has_more`, `total` 같은 메타데이터 유실.

### 2. HTML clean이 압축보다 효과적

`html_mixed`: clean 단계에서 script/style/tags 제거만으로 81% 크기 감소 (2429 → 469). 추가 압축 불필요. Clean 단계의 품질 보존율은 100%.

### 3. 70% 예산이 Code의 최적 지점

Code 파일은 70% 예산에서 100% quality 달성. 30%에서는 6.2/10으로 급락. 예산 할당 시 코드 파일에는 70%+ 권장.

### 4. 링크 폭주 콘텐츠는 clean이 핵심

`markdown_with_links`: 50개 링크가 본문 앞에 위치. `_collapse_link_floods`가 정상 동작하면 4728 → 306 chars (94% clean), 모든 키워드 보존. 링크 regex 버그 수정이 결정적.

### 5. Summary 섹션은 문서 끝에 있어도 보존 필요

`large_diff_output`: "Breaking change", "alembic" 같은 핵심 정보가 `### Summary`에 있는데 Hybrid head(500 chars)에 포함 안 됨. TruncateCompressor에 Summary 감지 추가로 해결 (truncate 사용 시 100%).

### 6. Selective 2-phase는 zero-loss 복원 가능

SelectiveCompressor는 전체 섹션을 선택하면 원본 콘텐츠를 100% 복원 가능 (recovery_ratio=1.0). 에이전트가 TOC를 보고 필요한 섹션만 선택하는 interactive workflow에 최적.

---

## 회귀 게이트 (CI)

벤치마크 테스트에 3개 quality gate가 포함되어 있어 압축 로직 변경 시 품질 저하를 자동 감지.

| Gate | Threshold | 설명 |
|------|-----------|------|
| `test_auto_strategy_all_above_40` | ≥40% | auto 전략 최소 품질 |
| `test_optimal_strategy_above_60` | ≥60% | 최적 전략 사용 시 |
| `test_generous_budget_above_80` | ≥80% | 2× 예산 + 최적 전략 |

```bash
# CI에서 실행
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py::TestRegressionGate -v
```

---

## 실행 방법

### 테스트 실행

```bash
# 벤치마크 전체 (90 tests)
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py -v

# 회귀 게이트만
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py::TestRegressionGate -v

# 전략 매트릭스만
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py::TestStrategyMatrix -v
```

### 리포트 생성

```bash
PYTHONPATH=packages/memtomem-stm/tests uv run python -c "
from bench.tasks import get_all_tasks, OPTIMAL_STRATEGIES
from bench.harness import BenchHarness
from bench.judge import RuleBasedJudge
from bench.report import format_full_report
from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.config import CleaningConfig
from memtomem_stm.proxy.compression import TruncateCompressor

cleaner = DefaultContentCleaner(CleaningConfig(strip_html=True, collapse_links=True, deduplicate=True))
h = BenchHarness(cleaner=cleaner, compressor=TruncateCompressor(), judge=RuleBasedJudge())
tasks = get_all_tasks()
comparisons = [h.run_auto_strategy(t) for t in tasks]
matrices = {t.task_id: h.run_strategy_matrix(t) for t in tasks}
curves = {t.task_id: h.run_compression_curve(t) for t in tasks}
print(format_full_report(comparisons, matrices=matrices, curves=curves, optimal_strategies=OPTIMAL_STRATEGIES))
"
```

---

## 파일 구조

```
packages/memtomem-stm/tests/
├── bench/
│   ├── __init__.py
│   ├── harness.py       # BenchTask, BenchHarness, StageMetrics, ComparisonReport
│   ├── tasks.py         # 8 tasks + 3 budget levels + metadata (categories, optimal strategies)
│   ├── judge.py         # RuleBasedJudge (weighted keyword + structure scoring)
│   └── report.py        # format_report, format_matrix, format_curve, format_full_report
└── test_bench_pipeline.py  # 90 tests across 12 test classes
```

### 테스트 클래스 구성

| Class | Tests | 역할 |
|-------|-------|------|
| TestBenchHarness | 5 | 하네스 기본 동작 |
| TestStageMetrics | 6 | 단계별 크기/타이밍 정확성 |
| TestQualityJudge | 11 | 채점 로직 (가중치, 케이스) |
| TestAutoStrategy | 6 | auto_select_strategy 검증 |
| TestStrategyMatrix | 5 | 전략 매트릭스 비교 |
| TestCompressionCurve | 6 | 예산별 quality 곡선 |
| TestPipelineQuality | 7 | 8개 태스크 A/B 비교 |
| TestCompressionStrategies | 4 | 전략별 특성 검증 |
| TestSurfacingIntegration | 3 | 서피싱 오버헤드 측정 |
| TestBudgetLevels | 3 | tight/default/generous 비교 |
| TestDataset | 4 | 데이터셋 무결성 |
| TestRegressionGate | 3 | CI quality threshold |
| TestReport | 7 | 리포트 포맷 검증 |
| TestCallMetrics | 4 | CallMetrics 타이밍 필드 |
| TestSelective2Phase | 8 | TOC→select 2-phase 흐름 |
| TestProxyManagerIntegration | 8 | mock upstream full pipeline |

---

## 향후 개선 가능 영역

- **LLM-as-Judge**: 규칙 기반 대신 LLM으로 더 정밀한 품질 판정 (비용 tradeoff)
- **HybridCompressor Summary 보존**: Truncate에만 적용된 Summary 감지를 Hybrid에도 확장
- **동적 예산 할당**: content-type에 따라 max_chars 자동 조정 (code=70%, JSON=30%)
- **실제 Ollama 임베딩 벤치마크**: `@pytest.mark.ollama`로 실제 검색 품질 측정

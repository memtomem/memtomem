# 검색 모델 및 reranker 비교 검증 보고서

검증일: 2026-07-13 (Asia/Seoul)

이 문서는 영어·한국어·교차언어 검색을 분리하여 임베딩 모델과 reranker 적용
전후를 비교한 결과를 기록한다. 원시 결과는
[`model_comparison_v2.json`](./model_comparison_v2.json)에 보존한다.

## 측정 장비

현재 장비를 `system_profiler`, `sw_vers`, `uname`으로 확인했다. 사용자가
언급한 M3가 아니라 운영체제에서 **Apple M4 Max**로 식별되는 장비다.
일련번호, 하드웨어 UUID, 사용자 이름은 재현에 필요하지 않아 기록하지 않았다.

| 항목 | 측정값 |
| --- | --- |
| 제품 | MacBook Pro |
| 모델 식별자 | Mac16,5 |
| 칩 | Apple M4 Max |
| CPU | 16코어 (성능 12, 효율 4) |
| 메모리 | 64 GB |
| 아키텍처 | arm64 |
| 운영체제 | macOS 26.5.2 (빌드 25F84) |
| 커널 | Darwin 25.5.0 |
| Python | 3.13.2 |
| uv | 0.11.16 |
| fastembed | 0.8.0 |
| 기준 Git 커밋 | `b73a7a74` |

지연시간은 이 장비의 CPU 실행 결과다. 다른 칩, 스레드 상태, 전원 모드,
모델 캐시 상태에서는 달라질 수 있으므로 품질 지표와 별도로 해석해야 한다.

## 검증 설계

- 공개 합성 코퍼스 48개 Markdown 파일, 192개 청크를 사용했다.
- 동결된 120개 질의는 동일 의도의 영어·한국어 60쌍이다.
- 영어 트랙은 영어 코퍼스와 영어 질의 60개를 사용한다.
- 한국어 트랙은 한국어 코퍼스와 한국어 질의 60개를 사용한다.
- 교차언어 트랙은 영어·한국어 통합 코퍼스와 전체 질의 120개를 사용한다.
- 각 언어에서 direct, paraphrase, underspecified, multi-topic, negation,
  genre-primary 유형을 같은 수로 평가한다.
- 모든 검색은 `top_k=10`, BM25/dense RRF 가중치 `[1.0, 1.0]`을 사용한다.
- reranker 적용 시 융합 검색 상위 20개를 재정렬한다.
- 지연시간은 인덱싱과 컴포넌트 생성을 제외한 검색 파이프라인 호출 시간을
  측정한다. reranker가 활성화된 경우 재정렬 시간은 포함한다.
- 이번 모델 비교는 프로필별 1회 실행이다. 품질 방향과 큰 비용 차이를
  확인하는 실험이며, 정밀 성능 확정값으로 사용하지 않는다.

## 비교 프로필

| 프로필 | 영어 | 한국어·교차언어 | reranker |
| --- | --- | --- | --- |
| 언어별 기본 | `BAAI/bge-small-en-v1.5` (384) | `paraphrase-multilingual-MiniLM-L12-v2` (384) | 없음 |
| 언어별 + reranker | 위와 같음 | 위와 같음 | `jina-reranker-v2-base-multilingual`, pool 20 |
| BGE-M3 | `BAAI/bge-m3` (1024) | `BAAI/bge-m3` (1024) | 없음 |
| BGE-M3 + reranker | `BAAI/bge-m3` (1024) | `BAAI/bge-m3` (1024) | `jina-reranker-v2-base-multilingual`, pool 20 |

## 실행 과정

다음 순서로 코퍼스, 기존 기준선, 모델 비교, 구현 회귀를 확인했다.

```bash
uv run python tools/retrieval-eval/audit_public_corpus.py

uv run python tools/retrieval-eval/check_baseline_v2.py --runs 1

uv run python tools/retrieval-eval/compare_models_v2.py \
  --runs 1 \
  --reranker-pool 20 \
  --output tools/retrieval-eval/model_comparison_v2.json

uv run ruff check packages/memtomem/src packages/memtomem/tests tools
uv run ruff format --check packages/memtomem/src packages/memtomem/tests tools

uv run pytest \
  packages/memtomem/tests/test_retrieval_benchmark_v2.py \
  packages/memtomem/tests/test_pipeline.py -q

jq -e \
  '(.schema_version == 1) and (.queries == 120) and
   (.profiles | length == 4) and (.deltas | length == 3)' \
  tools/retrieval-eval/model_comparison_v2.json

git diff --check
```

## 결과

아래 값은 언어·질의 유형별 점수를 다시 평균한 macro 지표다.

| 프로필 | 트랙 | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 언어별 기본 | 영어 | 0.634028 | 0.605159 | 0.571666 | 9 | 4.258 ms |
| 언어별 기본 | 한국어 | 0.530178 | 0.493095 | 0.429411 | 9 | 4.190 ms |
| 언어별 기본 | 교차언어 | 0.355631 | 0.542840 | 0.388166 | 17 | 4.267 ms |
| 언어별 + reranker | 영어 | 0.675794 | 0.637361 | 0.609695 | 6 | 694.513 ms |
| 언어별 + reranker | 한국어 | 0.620952 | 0.683056 | 0.576549 | 6 | 1001.449 ms |
| 언어별 + reranker | 교차언어 | 0.448738 | 0.656038 | 0.489623 | 9 | 989.281 ms |
| BGE-M3 | 영어 | 0.637897 | 0.617268 | 0.570713 | 9 | 23.505 ms |
| BGE-M3 | 한국어 | 0.657778 | 0.602976 | 0.560427 | 7 | 25.399 ms |
| BGE-M3 | 교차언어 | 0.444737 | 0.641710 | 0.490313 | 15 | 23.813 ms |
| BGE-M3 + reranker | 영어 | 0.677182 | 0.628214 | 0.604097 | 7 | 708.548 ms |
| BGE-M3 + reranker | 한국어 | 0.725040 | 0.677844 | 0.632294 | 5 | 1009.115 ms |
| BGE-M3 + reranker | 교차언어 | 0.531227 | 0.669835 | 0.553692 | 10 | 1008.314 ms |

### BGE-M3 적용 차이

언어별 기본 모델과 비교한 변화량이다.

| 트랙 | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p95 증가 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 영어 | +0.003869 | +0.012109 | -0.000953 | 0 | +19.247 ms |
| 한국어 | +0.127600 | +0.109881 | +0.131016 | -2 | +21.209 ms |
| 교차언어 | +0.089106 | +0.098870 | +0.102147 | -2 | +19.546 ms |

영어에서는 품질 이득이 거의 없지만, 한국어와 교차언어에서는 세 지표가 모두
크게 개선됐다. 이 장비에서 비-rerank p95는 약 4 ms에서 24~25 ms로 증가했다.

### reranker 적용 차이

각 언어별 기본 임베딩에 reranker를 추가한 변화량이다.

| 트랙 | Recall@10 | MRR@10 | nDCG@10 | zero-hit | p95 증가 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 영어 | +0.041766 | +0.032202 | +0.038029 | -3 | +690.255 ms |
| 한국어 | +0.090774 | +0.189961 | +0.147138 | -3 | +997.259 ms |
| 교차언어 | +0.093107 | +0.113198 | +0.101457 | -8 | +985.014 ms |

reranker는 모든 트랙을 개선했고 특히 한국어 MRR/nDCG와 교차언어 zero-hit를
크게 개선했다. 반면 CPU p95가 영어 약 0.7초, 한국어·교차언어 약 1초가 되어
기본 검색 경로에 항상 적용하기에는 비용이 크다.

## 검증 판정

- 공개 코퍼스 감사: 48개 파일, 192개 청크가 완전하게 인덱싱되었고 민감정보
  검사에서 문제가 없었다.
- retrieval v2 기준선: 영어·한국어·교차언어 120개 질의 기준 통과.
- 모델 비교 산출물: 4개 프로필과 3개 비교 delta를 포함하며 JSON 구조 검증 통과.
- 대상 회귀 테스트: **65 passed**.
- Ruff 검사 및 포맷 검사: 통과.
- `git diff --check`: 통과.
- 더 넓은 non-LLM 테스트는 앞선 실행에서 약 32% 지점에 테스트 실패가 아닌
  프로세스 `SIGTRAP`(종료 133)으로 중단되었다. 따라서 전체 스위트 통과로
  기록하지 않는다.
- mypy는 이번 변경과 무관한 기존 파일에서 14개 오류를 보고했으므로 이번
  결과의 통과 기준에는 포함하지 않았다.

## 권고안

1. 영어 전용 기본 프로필은 작은 영어 모델을 유지한다. BGE-M3의 영어 품질
   이득이 미미하고 지연시간만 증가했다.
2. 한국어 및 교차언어 품질 프로필에는 BGE-M3를 우선 검토한다. 약 20 ms의
   p95 비용으로 의미 있는 품질 개선을 얻었다.
3. reranker는 고품질 모드나 비동기 처리에 opt-in으로 제공한다. 실시간 기본
   경로에 적용하려면 후보 풀 축소, 조건부 rerank, 하드웨어 가속을 별도로
   검증해야 한다.
4. 운영 기본값 변경 전에는 동일 장비에서 5~10회 반복하고 p50/p95뿐 아니라
   실행 간 분산, 메모리 사용량, cold-start 시간을 추가 측정한다.

## 이번 PR 범위와 후속 작업

이번 PR은 RRF 캐시 정확성 수정, 언어별 평가 방법론, 재현 가능한 비교 도구,
현재 1회 실험 결과와 검증 문서를 제공한다. BGE-M3 또는 reranker를 제품
기본값으로 전환하지 않으며, 현재 결과는 후속 결정을 위한 잠정 근거다.

후속 작업은 별도 변경으로 진행한다.

1. 같은 장비와 고정된 실행 조건에서 프로필별 5~10회 반복 측정
2. 실행 간 품질·지연시간 분산과 cold/warm cache 차이 기록
3. peak RSS, 모델 적재 시간, 디스크 캐시 크기 측정
4. reranker 후보 풀 5/10/20 및 조건부 rerank 비교
5. 한국어·교차언어 운영 품질 게이트를 만족할 때만 기본 프로필 변경 제안

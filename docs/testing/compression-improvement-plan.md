# 압축 파이프라인 개선 방안

벤치마크 결과(7.36/10, 74.6% 보존)에서 식별된 약점 기반 개선 계획.
우선순위: 프로덕션 임팩트 × 구현 용이성 순.

---

## P0: 취약 도메인 전용 압축기

**대상**: json-app-config (2.0), md-api-docs (2.0)
**브랜치**: `feat/domain-compressors`

### 1a. JSON Schema-Preserving Pruner

현재 `FieldExtractCompressor`의 한계: top-level 키만 보존, 중첩 배열은 3개만 미리보기.

**전략**: 재귀적 pruning — 모든 키를 보존하되, 배열 길이와 문자열 길이만 제한.

```python
class SchemaPruningCompressor:
    """JSON 스키마 구조 100% 보존 + 값만 축소."""
    
    def _prune(self, data, budget):
        if isinstance(data, dict):
            # 모든 키 보존 — 값만 prune
            return {k: self._prune(v, budget // len(data)) for k, v in data.items()}
        elif isinstance(data, list):
            # 첫 2개 + 마지막 1개 + 카운트
            if len(data) > 3:
                return [self._prune(data[0], budget), 
                        self._prune(data[1], budget),
                        f"... ({len(data) - 3} items omitted)",
                        self._prune(data[-1], budget)]
            return [self._prune(item, budget) for item in data]
        elif isinstance(data, str) and len(data) > 80:
            return data[:80] + "..."
        return data
```

핵심: `data[-1]` — 배열의 마지막 항목도 보존하여 anomaly(needle-in-haystack) 감지 가능.

**예상 효과**: json-app-config 2.0 → 6.0+, json-event-stream 6.0 → 8.0+

### 1b. Markdown Header Skeleton

현재 문제: md-api-docs의 5개 엔드포인트 중 뒷부분 완전 소실.

**전략**: 모든 heading + 첫 번째 코드 블록/URL 라인을 보존하는 skeleton 모드.

```python
class SkeletonCompressor:
    """Markdown 골격 보존 — 모든 heading + 핵심 라인."""
    
    def compress(self, text, *, max_chars):
        lines = text.split("\n")
        skeleton = []
        for line in lines:
            if (line.startswith("#") or              # heading
                line.strip().startswith("```") or    # code fence
                "POST " in line or "GET " in line or # HTTP method
                "| " in line and "---" not in line): # table row (not separator)
                skeleton.append(line)
        result = "\n".join(skeleton)
        if len(result) > max_chars:
            result = TruncateCompressor().compress(result, max_chars=max_chars)
        return result
```

**예상 효과**: md-api-docs 2.0 → 6.0+ (charges, refunds, webhooks, rate limit 모두 heading에 포함)

### 1c. auto_select_strategy 확장

```python
def auto_select_strategy(text):
    # JSON → schema_pruning (기존 extract_fields 대체)
    if is_json(text):
        return CompressionStrategy.SCHEMA_PRUNING
    
    # Markdown with many parallel sections → skeleton
    headings = count_headings(text)
    if headings >= 5 and average_section_length(text) < 200:
        return CompressionStrategy.SKELETON
    
    # 대형 markdown → hybrid
    if headings >= 5 and len(text) >= 5000:
        return CompressionStrategy.HYBRID
    
    # 기본: truncate (section-aware / code-aware 자동)
    return CompressionStrategy.TRUNCATE
```

---

## P1: 평가 시스템 견고성

**대상**: RuleBasedJudge의 엄격한 문자열 매칭
**브랜치**: `feat/fuzzy-judge`

### 2a. Fuzzy Matching for QA

```python
def _fuzzy_match(answer: str, text: str, threshold: float = 0.8) -> bool:
    """Levenshtein ratio >= threshold이면 매칭."""
    answer_lower = answer.lower()
    text_lower = text.lower()
    # 정확 매칭 먼저
    if answer_lower in text_lower:
        return True
    # 공백/구두점 정규화 후 재시도
    normalized_answer = re.sub(r"[\s,;:]+", " ", answer_lower).strip()
    normalized_text = re.sub(r"[\s,;:]+", " ", text_lower).strip()
    if normalized_answer in normalized_text:
        return True
    # Sliding window Levenshtein (순수 Python, 외부 의존성 없음)
    ...
```

현재 QA 채점이 `"95 minutes" in text` → 공백이 `"95minutes"`로 바뀌면 실패.
정규화만으로도 상당 부분 해결 가능.

### 2b. LLMJudge 로컬 모드

```python
class LLMJudge:
    # 기존 anthropic/openai에 추가
    DEFAULT_MODELS = {
        "anthropic": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
        "ollama": "llama3:8b",  # 로컬, 무료
    }
```

Ollama가 이미 memtomem core에서 embedding에 사용되므로 인프라 재활용 가능.

---

## P2: 다국어 처리 보강

**대상**: ml-jp-api-response (4.0)
**브랜치**: `feat/multilingual-extract`

### 3a. Unicode-aware FieldExtract

현재 `_preview_dict`의 `max_value_len=80`은 바이트가 아닌 문자 수 기준이라
일본어는 이미 올바르게 처리됨. 실제 문제는:

- `_compress_json`에서 일본어 키(`"ステータス"`)는 정상 처리됨
- 문제는 **예산 초과 시 절단** — `ensure_ascii=False`로 JSON 직렬화 시 일본어가 
  영어보다 더 많은 바이트를 차지하여 예산을 빨리 소진

**수정**: 직렬화 후 크기 체크 → 예산 초과 시 preview depth 줄이기.

### 3b. LLM 필드 추출 Fallback

규칙 기반 실패 시 LLM으로 fallback — 이미 `LLMCompressor`가 존재하므로 
"JSON 키만 리스트업" 프롬프트로 변환하면 됨. 비용: ~$0.0005/call (Haiku).

---

## P3: 서피싱 노이즈 저항성 강화

**현재 상태**: `TestDistractorRobustness` (4 tests) + `get_distractor_tasks()` 이미 존재.

### 보강 포인트

- **모순 메모리**: 현재 distractor는 "무관한" 메모리만 테스트. "모순되는" 메모리 추가 필요.
  예: content에 "pool_size=50"이 있는데 memory에 "pool_size was increased to 200" 주입
- **평가 지표**: `contradiction_resistance_score` — 모순 메모리 주입 후 정답 유지율
- **RelevanceGate 강화**: min_score 임계값이 모순 메모리를 걸러내는지 검증

---

## 구현 우선순위

| 순서 | 항목 | 예상 효과 | 작업량 |
|------|------|----------|--------|
| **1** | JSON Schema Pruner | json 카테고리 5.6 → 7.0+ | 1일 |
| **2** | Markdown Skeleton | md-api-docs 2.0 → 6.0+ | 0.5일 |
| **3** | QA Fuzzy Matching | 전체 QA 정확도 5-10% 향상 | 0.5일 |
| **4** | 다국어 예산 보정 | ml-jp 4.0 → 6.0+ | 0.5일 |
| **5** | 모순 메모리 테스트 | robustness 검증 강화 | 0.5일 |
| **6** | LLMJudge 로컬 모드 | 평가 비용 제거 | 1일 |

**총 예상**: 약 4일, 전체 품질 7.36 → 8.5+ 예상.

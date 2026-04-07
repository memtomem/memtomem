# 계층적 검색 + 윈도우 확장 계획

## 핵심 아이디어

작은 청크로 정밀 검색 → 큰 윈도우로 맥락 반환 (small-to-big retrieval).

현재 문제: 검색이 작은 청크를 찾아도, 반환되는 콘텐츠가 그 작은 청크뿐이라 맥락 부족.

```
현재:  검색 → 청크 반환 (300자 미리보기)
개선:  검색 → 청크 + ±N 인접 청크 반환 (충분한 맥락)
```

## 현재 인프라 (80% 준비)

| 구성 요소 | 상태 | 설명 |
|-----------|------|------|
| `ContextInfo` 모델 | ✅ 정의됨, 미사용 | `window_before/after`, `parent_content`, `context_tier_used` |
| `SearchResult.context` | ✅ 필드 존재 | 항상 None으로 반환 |
| `ChunkMetadata.start_line/end_line` | ✅ 저장됨 | 인접 청크 찾기 가능 |
| `ChunkMetadata.source_file` | ✅ 저장됨 | 같은 파일 청크 조회 가능 |
| `list_chunks_by_source()` | ✅ 구현됨 | 파일별 청크 목록 |
| `SurfacingConfig.max_injection_chars` | ✅ 2000 | 윈도우 확장 시 조정 필요 |

## 구현 단계

### 1단계: 스토리지 확장

```python
# sqlite_backend.py 신규 메서드
async def get_adjacent_chunks(
    self, chunk_id: str, window: int = 2
) -> tuple[list[Chunk], list[Chunk]]:
    """청크 ID 기준 ±N 인접 청크 반환 (같은 source_file, line 순서)."""
    ...
```

### 2단계: 검색 파이프라인 후처리

```python
# search/pipeline.py 확장
async def _expand_context(
    self, results: list[SearchResult], window: int = 2
) -> list[SearchResult]:
    """검색 결과에 인접 청크 컨텍스트 추가."""
    for r in results:
        before, after = await storage.get_adjacent_chunks(r.chunk.id, window)
        r = replace(r, context=ContextInfo(
            window_before=tuple(before),
            window_after=tuple(after),
            context_tier_used="standard",
        ))
    ...
```

### 3단계: 서피싱 포매터 확장

```python
# surfacing/formatter.py 확장
def _format_memory_with_context(result: SearchResult) -> str:
    """청크 + 인접 맥락을 포함한 메모리 포맷."""
    parts = []
    if result.context and result.context.window_before:
        parts.append("... " + result.context.window_before[-1].content[-200:])
    parts.append(result.chunk.content)
    if result.context and result.context.window_after:
        parts.append(result.context.window_after[0].content[:200] + " ...")
    return "\n".join(parts)
```

### 4단계: 설정

```python
# surfacing/config.py 확장
context_window_size: int = 2        # ±N 인접 청크
context_tier: str = "standard"      # "full" | "standard" | "minimal"
max_injection_chars: int = 3000     # 윈도우 확장에 맞게 증가
```

## 평가 영향 예상

| 지표 | 현재 | 예상 | 이유 |
|------|------|------|------|
| surfacing QA answerable | 기본 | +20-30% | 인접 맥락에 답변 정보 포함 |
| surfacing_value.qa_delta | 기존 | 증가 | 메모리 품질 자체 향상 |
| precision | 높음 | 약간 하락 | 윈도우에 noise 포함 가능 |
| 전체 quality | 7.36 | 변동 없음 | 서피싱 태스크에만 영향 |

기존 벤치마크 `measure_surfacing_value()`로 before/after 비교 가능.

## 브랜치

`feat/context-window-search` — main에서 분기 (STM bench와 독립).

## 리스크

- **낮음**: 모든 데이터 구조 이미 존재, DB 마이그레이션 불필요
- **주의**: `max_injection_chars` 증가 시 토큰 비용 증가 → 예산 관리와 연계 필요
- **고려**: 인접 청크 로드 시 추가 DB 쿼리 → batch loading으로 해결

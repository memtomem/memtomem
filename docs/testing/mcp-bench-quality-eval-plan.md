# STM → MCP Gateway 진화 계획

## Context

STM은 현재 "메모리 프록시" — 다른 MCP 서버를 프록싱하면서 응답을 압축하고 관련 메모리를 서피싱한다.
하지만 **Claude Code 내장 도구(Read, Bash, Grep)에는 개입할 수 없다는 한계**가 있다.

MCP 생태계가 확장되면서 (GitHub, Slack, Jira, DB, 파일시스템 등), 모든 MCP 도구 호출이
하나의 게이트웨이를 통과하는 구조가 점점 가치 있어진다. STM의 기존 인프라(라우팅, 압축, 캐시,
메트릭)는 이미 **범용 MCP 게이트웨이의 40-70%**를 갖추고 있다.

### 핵심 질문

MCP 도구를 많이 쓰는 환경에서 STM을 **MCP Gateway**로 확장하면:
- 모든 MCP 호출에 대한 **통합 모니터링/비용 추적**
- 도구별 **레이트 리밋, 가드레일, 보안**
- **토큰 예산 관리** (압축 + 라우팅)
- 사용자별/프로젝트별 **접근 제어**
- 메모리 서피싱은 게이트웨이의 **플러그인 중 하나**로 자연스럽게 포함

---

## 현재 STM 아키텍처 — 이미 갖춘 것

| 영역 | 현재 상태 | 재사용성 |
|------|----------|---------|
| **멀티서버 라우팅** | ProxyManager + 3 transport (stdio/SSE/HTTP) | 75% |
| **도구 스키마 보존** | upstream 스키마 그대로 노출, annotations 보존 | 70% |
| **Per-tool 설정** | server → tool override cascade | 65% |
| **압축 파이프라인** | 6 전략 + auto_select + section-aware | 60% (범용화 필요) |
| **캐시** | SQLite, TTL, pre-surfacing 캐시 | 65% |
| **메트릭** | chars 기반, per-server/tool 집계 | 40% (차원 확장 필요) |
| **프라이버시 스캔** | API key/password/PII 감지 | 30% (적용만, 차단 없음) |
| **핫 리로드** | config mtime 감지 → 자동 재로드 | 65% |

---

## Gateway 확장 시 필요한 것

### Tier 1: 즉시 가치 (MCP 도구 많이 쓸 때)

**1. 도구별 레이트 리밋**
- 파일: `proxy/rate_limiter.py` (신규)
- per-server, per-tool, global 3단계
- sliding window 방식 (surfacing의 RelevanceGate 패턴 재사용)
- 설정: `tool_overrides.{tool}.rate_limit: {max_calls: 60, window_seconds: 60}`

**2. 토큰 예산 관리**
- 파일: `proxy/token_budget.py` (신규)
- 세션당 또는 프로젝트당 토큰 상한
- 압축 전략 자동 강화: 예산 50% 소진 → hybrid, 80% → selective
- 예산 초과 시 경고 또는 차단
- 현재 TokenTracker 확장

**3. 메트릭 차원 확장**
- 파일: `proxy/metrics.py`, `proxy/metrics_store.py` 수정
- 추가 필드: `latency_ms`, `error_type`, `cache_status`, `tokens_estimated`
- 시계열 쿼리 API: 기간별, 서버별, 도구별 집계
- Web UI 대시보드 연동

### Tier 2: 가드레일 + 보안

**4. 도구 필터링 / 접근 제어**
- 파일: `proxy/access_control.py` (신규)
- 설정 기반: `tool_overrides.{tool}.enabled: false` (이미 surfacing에 exclude_tools 패턴 있음)
- 쓰기 도구 차단: `destructiveHint: true` annotations 활용
- allowlist/denylist per-server

**5. 프라이버시 적용 (감지 → 차단)**
- 파일: `proxy/privacy.py` 수정
- 현재: 감지만 → LLM 압축 fallback
- 확장: 응답에서 민감 정보 마스킹 옵션
- 요청에서도 스캔 (credential leak 방지)

**6. 감사 로그**
- 파일: `proxy/audit.py` (신규)
- 모든 도구 호출 기록: who, when, which tool, result size, error
- SQLite 별도 테이블 또는 파일
- 보존 정책 (retention days)

### Tier 3: 고급 게이트웨이

**7. 라이프사이클 훅 (pre/post-call)**
- 파일: `proxy/hooks.py` (신규)
- pre_call: 요청 변환, 인증 주입, 유효성 검사
- post_call: 응답 변환, 로깅, 알림
- 메모리 서피싱은 post_call 훅의 한 종류로 리팩토링

**8. 동적 서버 관리**
- 런타임에 upstream 서버 추가/제거 (재시작 불필요)
- health check probe (주기적 ping)
- 자동 재연결 개선

**9. 비용 추적**
- upstream API 호출당 비용 설정
- per-session, per-project 비용 집계
- 예산 알림

---

### Tier 0: Tool Routing (핵심 — 기존 패턴 확장)

memtomem의 `mem_do` 메타도구가 이미 tool routing의 프로토타입이다:
- 61개 액션 → 1개 진입점 (`mem_do`)
- 별칭 지원 (`health_report` → `eval`)
- 카테고리 기반 help
- 오타 시 유사 액션 제안

**이 패턴을 MCP 게이트웨이 전체로 확장하면:**

**1. 게이트웨이 메타도구 (`gateway_call`)**
```
gateway_call(tool="gh/search_code", params={...})
gateway_call(tool="fs/read_file", params={...})
gateway_call(action="help")                         → 전체 도구 카탈로그
gateway_call(action="help", params={"server": "gh"})→ GitHub 도구만
```
- MCP 도구 수십 개를 1개 메타도구로 통합 → 에이전트 도구 선택 부담 감소
- 현재 `mem_do`의 ActionInfo/ACTIONS 레지스트리 패턴 재사용

**2. 컨텍스트 기반 도구 필터링**
```python
# 기존: 모든 upstream 도구를 그대로 노출 (50개+)
# 개선: 현재 작업 컨텍스트에 맞는 도구만 노출

class ToolVisibilityPolicy:
    mode: "all" | "relevant" | "minimal"
    
    # "relevant" 모드: 최근 사용 + 카테고리 기반 필터링
    active_categories: list[str]     # ["code", "search"] → 코드 관련 도구만
    recently_used_boost: bool        # 최근 사용한 도구 우선
    max_visible_tools: int           # 노출 도구 수 상한 (예: 20)
```
- 이미 존재하는 패턴: `MEMTOMEM_TOOL_MODE` (core/standard/full)
- 확장: upstream 도구에도 동일한 모드 적용

**3. 도구 분류 자동화**
```python
# 이미 존재하는 패턴들 조합:
# - MCP annotations: readOnlyHint, destructiveHint
# - write_tool_patterns: *write*, *create*, *delete*
# - tool name parsing: search_code → "search" + "code"

class ToolClassifier:
    def classify(self, tool: ProxyToolInfo) -> ToolClass:
        # 1. MCP annotation 활용
        if tool.annotations.destructiveHint: return "destructive"
        if tool.annotations.readOnlyHint: return "read"
        # 2. 이름 패턴 매칭 (기존 write_tool_patterns 재사용)
        # 3. 설명 키워드 분석
```

**4. 스마트 라우팅 / 도구 추천**
```
에이전트: "이 파일 읽어줘" (경로: /src/auth.py)
→ ContextExtractor가 의도 추출 (이미 존재)
→ ToolClassifier가 후보 매칭: fs__read_file, gh__get_file_contents
→ 로컬 경로 → fs__read_file 선택
→ GitHub URL → gh__get_file_contents 선택
```

**5. 안전한 쓰기 도구 관리**
```python
# 이미 존재: write_tool_patterns으로 서피싱 차단
# 확장: 쓰기 도구 호출 시 확인/로깅/차단 정책

class WriteSafetyPolicy:
    require_confirmation: bool       # destructive 도구 호출 전 확인
    log_all_writes: bool            # 모든 쓰기 작업 감사 로그
    deny_patterns: list[str]        # 특정 쓰기 도구 차단
```

**6. 도구 별칭 & 통합 Help**
```python
# 이미 존재: mem_do의 _ALIASES
# 확장: upstream 도구에도 별칭 설정

_GATEWAY_ALIASES = {
    "read_file": "fs__read_file",
    "search_code": "gh__search_code",
    "list_issues": "gh__list_issues",
}
```

**7. 계층적 도구 탐색 (HNSW-inspired Tool Navigation)**

HNSW(Hierarchical Navigable Small World)에서 영감을 받은 도구 라우팅:
- 벡터 검색에서 HNSW는 상위 레이어(coarse, 적은 노드)에서 하위 레이어(fine, 전체 노드)로 내려가며 탐색
- 도구 라우팅에도 동일 원리 적용: **매 단계 3-5개 선택지만 제시**

```
Layer 0 (서버 그룹) — "무엇을 할 건가?"
  ├── code      (filesystem, github)
  ├── data      (database, api)
  ├── comms     (slack, email)
  └── system    (config, monitoring)

Layer 1 (기능 유형) — "어떤 종류의 작업인가?"
  code/
  ├── read      (파일 읽기)
  ├── search    (코드 검색)
  ├── write     (파일 수정)
  └── analyze   (히스토리, diff)

Layer 2 (구체 도구) — "어떤 도구를 쓸 건가?"
  code/search/
  ├── fs__search_files    (로컬 파일시스템)
  └── gh__search_code     (GitHub 레포)
```

**동작 방식:**
```
에이전트 의도: "인증 관련 코드를 찾아야 해"

Step 1 (Layer 0): 의도 → "code" (communication이나 data가 아님)
Step 2 (Layer 1): 작업 → "search" (read나 write가 아님)
Step 3 (Layer 2): 도구 → "gh__search_code" (GitHub 레포 대상)

총 선택: 4 + 4 + 2 = 10번 비교 (50개 플랫 탐색 대비)
```

**구현 — MCP 스펙 기반 풍부한 분류:**

MCP Tool 스펙의 **전체 필드** — 현재 활용 vs 미활용:

```
[현재 사용]
  name:          "search_code"                ← 이름 패턴 매칭
  description:   "Search for code across..."  ← 서피싱 쿼리 추출에만 사용
  inputSchema:   {query, repo, language}      ← 스키마 패스스루만 (분석 안 함)
  annotations:
    readOnlyHint:    true                     ← 서피싱 write-tool 필터만
    destructiveHint: false                    ← 서피싱 write-tool 필터만

[미활용 — 라우팅에 핵심적]
  title:            "Search Code"             ← name보다 의미 명확
  annotations:
    idempotentHint:  true                     ← 재시도 안전성 판단
    openWorldHint:   true                     ← 외부 서비스 vs 로컬 분류
  outputSchema:     {type: "array", ...}      ← 반환 데이터 유형 → 도구 체이닝
  execution:
    taskSupport:     "optional"               ← 비동기/장시간 작업 가능 여부
  inputSchema 내부:
    properties.*.description: "검색 쿼리"      ← 파라미터별 의미
    properties.*.enum: ["js","py","go"]        ← 허용 값 → 도구 능력 범위
    properties.*.examples: ["auth middleware"] ← 사용 예시 → 의도 매칭
    properties.*.default: 10                   ← 기본값 → 도구 동작 이해
  icons:            [{src: "..."}]            ← UI 분류에 활용
  _meta:            {}                        ← 커스텀 메타데이터 확장점
```

**이 모든 정보를 조합하면 분류 정확도가 크게 향상된다:**

```python
@dataclass(frozen=True)
class ToolProfile:
    """MCP 스펙 전체에서 추출한 도구 프로필 — 분류 + 라우팅 + 매칭에 사용."""
    # Identity
    name: str                         # prefixed name
    server: str
    title: str | None                 # MCP title (name보다 의미 명확)
    description: str                  # 원본 description

    # Classification (자동 계산)
    group: str                        # Layer 0: code, data, comms, system
    capability: str                   # Layer 1: read, search, write, analyze, browse

    # Behavioral hints (MCP annotations 전체)
    is_readonly: bool                 # readOnlyHint
    is_destructive: bool              # destructiveHint
    is_idempotent: bool               # idempotentHint — 재시도 안전
    is_open_world: bool               # openWorldHint — 외부 서비스 접근

    # Input analysis
    param_names: tuple[str, ...]      # 파라미터 이름
    param_types: dict[str, str]       # 파라미터 타입
    param_descriptions: dict[str, str]  # 파라미터별 description
    param_enums: dict[str, list]      # enum 값 → 도구 능력 범위
    required_params: tuple[str, ...]  # 필수 파라미터

    # Output analysis
    output_schema: dict | None        # outputSchema → 체이닝/호환성 판단
    task_support: str | None          # "forbidden"|"optional"|"required"

    # Semantic
    keywords: tuple[str, ...]         # description + param descriptions에서 추출
    embedding: list[float] | None     # description 임베딩 (선택적)


class ToolClassifier:
    """MCP 스펙 전체를 활용한 다중 시그널 분류."""

    def classify(self, tool: ProxyToolInfo) -> ToolProfile:
        ann = tool.annotations or {}

        # === Signal 1: Annotations (4개 힌트 — 가장 확실) ===
        is_readonly = getattr(ann, 'readOnlyHint', False) or False
        is_destructive = getattr(ann, 'destructiveHint', False) or False
        is_idempotent = getattr(ann, 'idempotentHint', False) or False
        is_open_world = getattr(ann, 'openWorldHint', False) or False
        # open_world=True → 외부 서비스 (GitHub, Slack, DB)
        # open_world=False → 로컬/내부 (filesystem, config)

        # === Signal 2: Description + Title 키워드 (가장 풍부) ===
        text = f"{tool.description} {getattr(tool, 'title', '') or ''}"
        keywords = self._extract_keywords(text.lower())

        # === Signal 3: inputSchema 심층 분석 ===
        schema = tool.input_schema or {}
        props = schema.get("properties", {})
        param_names = tuple(props.keys())
        param_types = {k: v.get("type", "?") for k, v in props.items()}
        param_descs = {k: v.get("description", "") for k, v in props.items()}
        param_enums = {k: v["enum"] for k, v in props.items() if "enum" in v}
        required = tuple(schema.get("required", []))
        # 파라미터 description도 키워드 소스로 활용
        all_param_text = " ".join(param_descs.values())
        keywords += self._extract_keywords(all_param_text.lower())

        # === Signal 4: outputSchema (체이닝/데이터 플로우) ===
        output_schema = getattr(tool, 'outputSchema', None)
        # array → 목록 반환 도구, object → 단일 항목 도구

        # === Signal 5: execution.taskSupport ===
        task_support = getattr(getattr(tool, 'execution', None), 'taskSupport', None)

        # === Signal 6: 도구 이름 패턴 (최후 수단) ===
        name_signal = self._name_pattern(tool.original_name)

        # === 6시그널 결합 → 분류 ===
        capability = self._resolve_capability(
            annotation=(is_readonly, is_destructive, is_idempotent, is_open_world),
            desc_keywords=keywords,
            param_hints=(param_names, param_types, param_enums),
            output_hint=output_schema,
            name_hint=name_signal,
        )

        group = self._resolve_group(
            server=tool.server,
            keywords=keywords,
            is_open_world=is_open_world,
        )

        return ToolProfile(
            name=tool.prefixed_name,
            server=tool.server,
            title=getattr(tool, 'title', None),
            description=tool.description,
            group=group,
            capability=capability,
            is_readonly=is_readonly,
            is_destructive=is_destructive,
            is_idempotent=is_idempotent,
            is_open_world=is_open_world,
            param_names=param_names,
            param_types=param_types,
            param_descriptions=param_descs,
            param_enums=param_enums,
            required_params=required,
            output_schema=output_schema,
            task_support=task_support,
            keywords=tuple(set(keywords)),
            embedding=None,
        )

    def _extract_keywords(self, description: str) -> tuple[str, ...]:
        """Description에서 불용어 제거 후 핵심 키워드 추출."""
        _STOPWORDS = {"a", "the", "is", "in", "to", "for", "and", "or", "of", "with"}
        words = [w for w in description.split() if w not in _STOPWORDS and len(w) > 2]
        return tuple(words[:10])  # 상위 10개

    def _extract_params(self, schema: dict) -> dict[str, str]:
        """inputSchema에서 파라미터 이름 → 타입 추출."""
        props = schema.get("properties", {})
        return {k: v.get("type", "unknown") for k, v in props.items()}

    def _resolve_capability(self, annotation, desc_keywords, param_hints, output_hint, name_hint):
        """6시그널로 기능 유형 결정.
        우선순위: annotation > description > params > output > name."""
        readonly, destructive, idempotent, open_world = annotation
        param_names, param_types, param_enums = param_hints

        # 1. Annotation (가장 확실한 시그널)
        if destructive:
            return "write"
        if readonly:
            if any(k in desc_keywords for k in ("search", "find", "query", "match")):
                return "search"
            if any(k in desc_keywords for k in ("list", "browse", "enumerate")):
                return "browse"
            return "read"

        # 2. Description + param descriptions 키워드 (가장 풍부)
        if any(k in desc_keywords for k in ("create", "update", "delete", "write", "modify", "remove", "send", "push")):
            return "write"
        if any(k in desc_keywords for k in ("search", "find", "query", "match", "lookup")):
            return "search"
        if any(k in desc_keywords for k in ("list", "get", "fetch", "retrieve", "read", "show")):
            return "read"
        if any(k in desc_keywords for k in ("analyze", "compare", "diff", "stat", "report", "evaluate")):
            return "analyze"

        # 3. 파라미터 시그널
        if "content" in param_names or "body" in param_names or "message" in param_names:
            return "write"
        if "query" in param_names or "search" in param_names or "q" in param_names:
            return "search"
        # enum 값이 있으면 → 필터링/설정 도구
        if param_enums:
            return "configure"

        # 4. Output 시그널
        if output_hint:
            out_type = output_hint.get("type")
            if out_type == "array":
                return "browse"  # 목록 반환 → 조회 계열

        # 5. 이름 패턴 (최후 수단)
        return name_hint or "other"

    def _resolve_group(self, server, keywords, is_open_world):
        """서버 + openWorldHint + 키워드로 그룹 결정."""
        # 1. 서버별 매핑 (설정 기반)
        _SERVER_GROUPS = {}  # config에서 로드: {"gh": "code", "slack": "comms"}
        if server in _SERVER_GROUPS:
            return _SERVER_GROUPS[server]

        # 2. openWorldHint로 내부/외부 구분
        # open_world=True + "message/send/notify" → comms
        # open_world=True + "query/database/table" → data
        # open_world=False + "file/path/directory" → code

        # 3. 키워드 기반
        if any(k in keywords for k in ("file", "path", "directory", "code", "source")):
            return "code"
        if any(k in keywords for k in ("database", "sql", "table", "query", "record")):
            return "data"
        if any(k in keywords for k in ("message", "send", "notify", "email", "chat", "slack")):
            return "comms"
        return "system"
```

**벡터 기반 시맨틱 매칭 (대규모 환경):**

```python
class SemanticToolMatcher:
    """도구 description을 임베딩해서 의도와 시맨틱 매칭."""

    def __init__(self, embedder, profiles: list[ToolProfile]):
        # 도구 등록 시 description 임베딩 (1회, 캐시)
        self._profiles = profiles
        self._embeddings = embedder.embed_batch(
            [p.description for p in profiles]
        )
        # memtomem의 기존 임베딩 인프라 그대로 재사용

    async def match(self, intent: str, top_k: int = 5) -> list[ToolProfile]:
        """사용자 의도 → 가장 관련 높은 도구 반환."""
        intent_emb = await self._embedder.embed_query(intent)
        scores = [cosine_similarity(intent_emb, e) for e in self._embeddings]
        ranked = sorted(zip(scores, self._profiles), reverse=True)
        return [p for _, p in ranked[:top_k]]
```

- 소규모 (50-100 도구): 키워드 + 규칙 기반으로 충분
- 대규모 (수백 도구): 벡터 매칭이 효과적
- memtomem의 임베딩 인프라(Ollama bge-m3 / OpenAI) 그대로 재사용

**기존 패턴과의 연결:**
- `tool_registry.py`의 카테고리 = Layer 1
- `mem_do help`의 카테고리별 출력 = 계층 탐색 UI
- `write_tool_patterns` = 규칙 기반 분류의 일부
- `MCP annotations` = Signal 1
- `ContextExtractor.extract_query()` = 의도 추출 → 트리 탐색 입력
- `search/fusion.py`의 RRF = 다중 시그널 결합에 응용 가능

---

**재사용 가능한 기존 코드:**

| 기존 코드 | 위치 | 게이트웨이 용도 |
|-----------|------|----------------|
| `ACTIONS` 레지스트리 | `tool_registry.py` | 전체 도구 카탈로그 |
| `_ALIASES` dict | `meta.py` | 크로스서버 도구 별칭 |
| `_help()` 함수 | `meta.py` | 통합 도구 help 시스템 |
| `write_tool_patterns` | `relevance.py` | 쓰기 도구 분류 |
| `ContextExtractor` | `context_extractor.py` | 의도 추출 → 라우팅 입력 |
| `TOOL_MODE` 필터링 | `server/__init__.py` | 도구 가시성 모드 |
| `ProxyToolInfo` | `manager.py` | 도구 메타데이터 |

---

## 아키텍처 변경 방향

```
현재:
  Agent → STM Proxy → [CLEAN → COMPRESS → SURFACE → INDEX] → Upstream MCP

확장 후:
  Agent → MCP Gateway → [AUTH → RATE_LIMIT → BUDGET_CHECK
                          → pre_hooks → FORWARD → post_hooks
                          → CLEAN → COMPRESS → SURFACE → METRICS → AUDIT]
                        → Upstream MCP

서피싱은 post_hook 플러그인 중 하나:
  post_hooks: [compression_hook, surfacing_hook, indexing_hook, custom_hooks...]
```

### 수정 대상 파일

| 파일 | 작업 | Tier |
|------|------|------|
| `proxy/tool_router.py` | 신규 — 메타도구, 분류, 라우팅 정책 | 0 |
| `proxy/rate_limiter.py` | 신규 — 도구별 레이트 리밋 | 1 |
| `proxy/token_budget.py` | 신규 — 토큰 예산 관리 | 1 |
| `proxy/metrics.py` | 수정 — latency, error, tokens 추가 | 1 |
| `proxy/metrics_store.py` | 수정 — 시계열 쿼리 | 1 |
| `proxy/access_control.py` | 신규 — 도구 필터링/ACL | 2 |
| `proxy/privacy.py` | 수정 — 마스킹, 요청 스캔 | 2 |
| `proxy/audit.py` | 신규 — 감사 로그 | 2 |
| `proxy/hooks.py` | 신규 — 라이프사이클 훅 | 3 |
| `proxy/manager.py` | 수정 — 파이프라인에 훅 통합 | 1-3 |
| `config.py` | 수정 — 새 설정 필드 추가 | 1-3 |

---

## 검증

```bash
# 기존 테스트 유지
uv run pytest packages/memtomem-stm/tests/ -v    # 282개 통과 확인

# 새 테스트 추가
uv run pytest packages/memtomem-stm/tests/test_rate_limiter.py -v
uv run pytest packages/memtomem-stm/tests/test_token_budget.py -v
uv run pytest packages/memtomem-stm/tests/test_access_control.py -v
```

## 우선순위 판단

**Tier 0 (Tool Routing)이 가장 즉각적인 가치:**
- 도구 50개+ → 1개 메타도구로 통합 → 에이전트 선택 부담 감소
- `mem_do` 패턴이 이미 검증됨 (61 actions → 1 tool)
- 기존 코드 70%+ 재사용 가능

Tier 1과 결합하면:
- 라우팅 + 레이트 리밋 → 안전한 도구 사용
- 라우팅 + 토큰 예산 → 비용 제어
- 라우팅 + 메트릭 → 어떤 도구가 실제로 유용한지 파악

Tier 2는 **팀/조직 환경**에서 필요:
- 접근 제어 → 위험한 도구 차단
- 감사 로그 → 컴플라이언스

Tier 3은 **플랫폼화** 시점.

---

## 구현 상태 (2026-04-06)

**모두 완료.** 브랜치: `feat/mcp-gateway-tool-routing` (9 commits, 484 STM tests)

| Tier | 모듈 | 테스트 | 상태 |
|------|------|--------|------|
| 0 | tool_router.py + stm_tool_catalog | 50 | **완료** |
| 1 | rate_limiter.py, token_budget.py, metrics 확장 | 52 | **완료** |
| 2 | access_control.py, audit.py | 36 | **완료** |
| 3 | hooks.py | 21 | **완료** |
| 통합 | ProxyManager pipeline + 시나리오 테스트 | 43 | **완료** |

파이프라인: `ACCESS → RATE_LIMIT → BUDGET → cache → forward → clean → compress → surface → index → metrics → audit`

---
---

# mcp-bench 기반 STM 파이프라인 품질 평가 계획

## Context

mcp-bench를 활용해 memtomem MCP 도구 호출 호환성을 검증하고, **STM 파이프라인(clean → compress → surface) 전후의 작업 결과 품질이 유지되는지** + **각 단계별 압축률**을 측정한다.

### 핵심 측정 목표

```
                 직접 호출 (baseline)      STM 경유
                ┌──────────────┐     ┌────────────────────────┐
upstream MCP ──→│ 원본 응답     │     │ clean → compress → surface │
                │ (100%)       │     │ (압축률? 품질 보존?)        │
                └──────┬───────┘     └──────────┬─────────────┘
                       ↓                        ↓
                 LLM-as-judge             LLM-as-judge
                 (작업 완료도 채점)          (작업 완료도 채점)
                       ↓                        ↓
                  Score A                   Score B
                       ↓
              품질 보존율 = Score B / Score A × 100%
```

**측정 항목:**

| 항목 | 설명 | 기존 인프라 |
|------|------|-----------|
| 도구 호환성 | 65개 MCP 도구가 STM 경유 시에도 정상 작동하는지 | 없음 — 신규 |
| 단계별 압축률 | clean/compress/surface 각 단계의 크기 변화 | TokenTracker에 partial (clean/compress만) |
| 작업 품질 보존 | 직접 호출 vs STM 경유 시 LLM 작업 완료도 차이 | 없음 — 신규 |
| 서피싱 유용성 | 주입된 메모리가 작업 완료에 기여했는지 | FeedbackTracker (수동 피드백만) |

---

## 구현 계획

### Phase 1: 벤치마크 하네스 (`packages/memtomem-stm/tests/bench/`)

**파일: `bench/harness.py`**

```python
@dataclass
class BenchResult:
    """단일 태스크 실행 결과."""
    task_id: str
    mode: str                    # "direct" | "stm_proxied"
    
    # 크기 메트릭 (각 단계별)
    original_chars: int
    cleaned_chars: int
    compressed_chars: int
    surfaced_chars: int          # 서피싱 후 최종 크기 (compressed보다 클 수 있음)
    
    # 비율
    cleaning_ratio: float        # cleaned / original
    compression_ratio: float     # compressed / cleaned
    total_reduction: float       # compressed / original
    surfacing_overhead: float    # (surfaced - compressed) / compressed
    
    # 시간
    latency_ms: float
    clean_ms: float
    compress_ms: float
    surface_ms: float
    
    # 품질 (LLM judge 또는 규칙 기반)
    quality_score: float | None  # 0-10 scale
    tool_calls_correct: bool
    error: str | None


class BenchHarness:
    """A/B 비교 벤치마크 — 동일 태스크를 direct와 STM 경유로 실행."""
    
    async def run_task(self, task: BenchTask, mode: str) -> BenchResult:
        """태스크 실행. mode="direct"이면 원본, "stm_proxied"이면 STM 경유."""
    
    async def run_comparison(self, task: BenchTask) -> tuple[BenchResult, BenchResult]:
        """동일 태스크를 direct + stm 모두 실행, 결과 쌍 반환."""
    
    def compare(self, direct: BenchResult, stm: BenchResult) -> ComparisonReport:
        """두 결과 비교 → 품질 보존율, 압축 효율 등."""
```

### Phase 2: 태스크 정의 (`bench/tasks.json`)

mcp-bench 형식을 참고하되, memtomem 전용으로 작성:

```json
[
  {
    "task_id": "search_simple",
    "description": "Search for architecture decisions about database choice",
    "tools": ["mem_search"],
    "params": {"query": "database architecture decision", "top_k": 5},
    "quality_criteria": "Returns results mentioning database/PostgreSQL/Redis from architecture.md"
  },
  {
    "task_id": "multi_tool_workflow",
    "description": "Index files, search, then recall by date",
    "steps": [
      {"tool": "mem_index", "params": {"path": "$TEST_DIR/memories"}},
      {"tool": "mem_search", "params": {"query": "kubernetes monitoring"}},
      {"tool": "mem_recall", "params": {"since": "2026-04"}}
    ],
    "quality_criteria": "All 3 steps succeed, search returns kubernetes.md, recall returns recent entries"
  },
  {
    "task_id": "cross_language",
    "description": "Search Korean content with English query",
    "tools": ["mem_search"],
    "params": {"query": "web framework choice reason"},
    "quality_criteria": "Returns architecture.md with Flask/FastAPI Korean content in top 3"
  },
  {
    "task_id": "session_workflow",
    "description": "Start session, add scratch, search, end session",
    "steps": [
      {"tool": "mem_session_start", "params": {"title": "test session"}},
      {"tool": "mem_scratch_set", "params": {"key": "focus", "value": "auth review"}},
      {"tool": "mem_search", "params": {"query": "authentication"}},
      {"tool": "mem_session_end", "params": {"summary": "reviewed auth"}}
    ],
    "quality_criteria": "Session created and ended, scratch value persisted during session"
  },
  {
    "task_id": "large_response_compression",
    "description": "Read a large file and verify content preservation after compression",
    "tools": ["fs__read_file"],
    "params": {"path": "$TEST_DIR/large_doc.md"},
    "quality_criteria": "Key sections (headings, code blocks, decisions) preserved after compression"
  }
]
```

### Phase 3: 단계별 메트릭 수집 (`proxy/manager.py` 수정)

현재 `_call_tool_inner`에서 clean/compress/surface 각 단계의 시간과 크기를 개별 추적하도록 확장:

```python
# 현재: compressed_chars_for_metrics = len(compressed) 만 추적
# 확장: 각 단계별 시간 + 크기

import time as _time

# Stage 1: CLEAN
t_clean = _time.monotonic()
cleaned = self._clean_content(original_text, tc.cleaning)
clean_ms = (_time.monotonic() - t_clean) * 1000

# Stage 2: COMPRESS
t_compress = _time.monotonic()
compressed = await self._apply_compression(...)
compress_ms = (_time.monotonic() - t_compress) * 1000

# Stage 3: SURFACE
t_surface = _time.monotonic()
surfaced = await self._apply_surfacing(...)
surface_ms = (_time.monotonic() - t_surface) * 1000

# 확장된 메트릭
self.tracker.record(CallMetrics(
    ...,
    clean_ms=clean_ms,
    compress_ms=compress_ms,
    surface_ms=surface_ms,
    surfaced_chars=len(surfaced),  # 서피싱 후 크기 (compressed보다 클 수 있음)
))
```

### Phase 4: 품질 판정 (`bench/judge.py`)

**방법 1: 규칙 기반 (빠르고 결정적)**

```python
class RuleBasedJudge:
    def score(self, task: BenchTask, response: str) -> float:
        """규칙 기반 품질 채점 (0-10)."""
        criteria = task.quality_criteria
        score = 10.0
        
        # 키워드 존재 확인
        for keyword in criteria.expected_keywords:
            if keyword.lower() not in response.lower():
                score -= 2.0
        
        # 구조 보존 확인 (헤딩, 코드 블록)
        if criteria.expect_headings:
            heading_count = response.count("## ")
            if heading_count < criteria.min_headings:
                score -= 1.0
        
        return max(0, score)
```

**방법 2: LLM-as-judge (mcp-bench 방식, 정밀하지만 비용)**

```python
class LLMJudge:
    async def score(self, task: BenchTask, direct_response: str, stm_response: str) -> dict:
        """LLM이 두 응답을 비교 채점."""
        prompt = f"""
        Task: {task.description}
        Quality criteria: {task.quality_criteria}
        
        Response A (direct): {direct_response[:2000]}
        Response B (STM-proxied): {stm_response[:2000]}
        
        Score each response 1-10 on:
        1. Task completion (did it answer the question?)
        2. Information completeness (key facts preserved?)
        3. Usability (is the response useful for the agent?)
        
        Return JSON: {{"a_score": N, "b_score": N, "reasoning": "..."}}
        """
```

**권장: Phase 1에서 규칙 기반으로 시작, 필요 시 LLM 판정 추가.**

### Phase 5: 리포트 생성 (`bench/report.py`)

```
=== memtomem STM Pipeline Benchmark ===

Task: search_simple
  Direct:      5000 chars, 0 ms clean, 0 ms compress   → quality: 9.0/10
  STM-proxied: 5000 → 4200 → 2100 (+300 surfacing)     → quality: 8.5/10
  Compression: clean -16%, compress -50%, surface +14%
  Quality preservation: 94.4%

Task: large_response_compression
  Direct:      50000 chars                               → quality: 10/10
  STM-proxied: 50000 → 42000 → 8000 (+500 surfacing)   → quality: 7.0/10
  Compression: clean -16%, compress -81%, surface +6%
  Quality preservation: 70.0%
  ⚠️ Quality drop: meeting decisions lost in compression

Summary:
  Avg compression: 62%
  Avg quality preservation: 88%
  Tool compatibility: 5/5 tasks passed
  Surfacing overhead: avg +8%
  Pipeline latency: avg 15ms
```

---

## 수정 대상 파일

| 파일 | 작업 |
|------|------|
| `packages/memtomem-stm/tests/bench/harness.py` | 신규 — A/B 비교 하네스 |
| `packages/memtomem-stm/tests/bench/tasks.json` | 신규 — 태스크 정의 |
| `packages/memtomem-stm/tests/bench/judge.py` | 신규 — 규칙 기반 품질 판정 |
| `packages/memtomem-stm/tests/bench/report.py` | 신규 — 리포트 생성 |
| `packages/memtomem-stm/tests/test_bench_pipeline.py` | 신규 — pytest 통합 |
| `packages/memtomem-stm/src/.../proxy/metrics.py` | 수정 — 단계별 시간 추가 |
| `packages/memtomem-stm/src/.../proxy/manager.py` | 수정 — 단계별 프로파일링 |

## 검증

```bash
# 벤치마크 실행 (Ollama 필요)
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py -v

# 또는 직접 실행
uv run python -m memtomem_stm.tests.bench.harness --tasks bench/tasks.json --report
```

## 기존 인프라 재사용

| 기존 코드 | 재사용 |
|-----------|--------|
| `test_effectiveness.py` (39 tests) | 압축/서피싱 효용성 패턴 |
| `test_information_loss.py` (27 tests) | 정보 보존 측정 패턴 |
| `TokenTracker` / `MetricsStore` | 메트릭 수집/저장 |
| `ProxyManager._call_tool_inner` | 파이프라인 단계별 측정 지점 |
| `FeedbackTracker` | 서피싱 피드백 기록 |
| `DefaultContentCleaner` | clean 단계 |
| 6 compression strategies | compress 단계 |
| `SurfacingEngine` | surface 단계 |

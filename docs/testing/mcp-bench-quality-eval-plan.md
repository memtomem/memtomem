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

# STM 파이프라인 품질 평가 — 벤치마크 프레임워크

## 구현 상태: 완료 (2026-04-06)

브랜치: `feat/mcp-bench-quality-eval` | 74 tests | STM 총 356 tests

---

## 아키텍처

```
Direct (baseline)              STM Pipeline
┌────────────┐     ┌──────────────────────────────────┐
│ 원본 텍스트  │     │ Clean → Compress → Surface       │
│ quality=10  │     │ (전략 자동 선택, 단계별 측정)       │
└──────┬──────┘     └───────────────┬──────────────────┘
       ↓                            ↓
  RuleBasedJudge              RuleBasedJudge
  (keyword/structure)         (keyword/structure)
       ↓                            ↓
    Score A                      Score B
              품질 보존율 = B / A × 100%
```

---

## 구현된 기능

### 1. 파이프라인 계측 (proxy/metrics.py, proxy/manager.py)

`CallMetrics` 확장: `clean_ms`, `compress_ms`, `surface_ms`, `surfaced_chars`
`ProxyManager._call_tool_inner`: `_time.monotonic()` 기반 단계별 타이밍

### 2. 벤치마크 프레임워크 (tests/bench/)

| 파일 | 역할 |
|------|------|
| `harness.py` | BenchTask, StageMetrics, BenchResult, ComparisonReport, BenchHarness |
| `tasks.py` | 8개 태스크 + 3가지 예산 (tight/default/generous) + 메타데이터 |
| `judge.py` | RuleBasedJudge (weighted keyword + heading + code block + JSON) |
| `report.py` | format_report, format_matrix, format_curve, format_full_report |

### 3. 데이터셋 (8 tasks)

| Task | Type | Budget | Keywords | Ground Truth Strategy |
|------|------|--------|----------|----------------------|
| api_response_json | json | 1000 | Alice, admin, total, has_more | extract_fields |
| code_file_large | code | 1500 | JWT, access_token, validate_token, middleware | hybrid |
| meeting_notes | markdown | 800 | PostgreSQL, Kim Cheolsu, April 15, Grafana | truncate |
| html_mixed | text | 800 | API Reference, Endpoints, authentication, admin | truncate |
| short_response | text | 1000 | OK, saved | none |
| markdown_with_links | markdown | 600 | microservices, gRPC, API gateway | hybrid |
| multilingual_kr_en | markdown | 1000 | FastAPI, PostgreSQL, Redis, Kubernetes | truncate |
| large_diff_output | code | 800 | verify_token, TokenPayload, Breaking change, alembic | hybrid |

### 4. 분석 도구

- **Auto-strategy**: `auto_select_strategy()` 기반 자동 전략 선택
- **Strategy matrix**: 8 tasks × 4 strategies (truncate/hybrid/extract_fields/auto) 비교
- **Compression curve**: 30%/50%/70%/90% 예산별 quality-ratio 곡선
- **Budget levels**: tight (0.5x), default (1.0x), generous (2.0x)
- **Surfacing integration**: mock SurfacingEngine + FakeChunk 기반 메모리 주입 테스트

### 5. 회귀 게이트 (CI)

| Gate | Threshold | 용도 |
|------|-----------|------|
| auto strategy | ≥40% quality | 자동 전략 선택 최소 품질 보장 |
| optimal strategy | ≥60% quality | 최적 전략 사용 시 품질 보장 |
| generous budget | ≥80% quality | 충분한 예산 시 품질 보장 |

---

## 벤치마크 결과

### Auto-strategy 비교 (default budget)

| Task | Strategy | Quality | Compression | Preservation |
|------|----------|---------|-------------|-------------|
| api_response_json | FieldExtract | 8.9/10 | 94% | **89%** |
| code_file_large | Hybrid | 10.0/10 | 31% | **100%** |
| meeting_notes | Hybrid | 10.0/10 | 14% | **100%** |
| html_mixed | Truncate | 10.0/10 | 81%* | **100%** |
| short_response | Truncate | 10.0/10 | 0% | **100%** |
| markdown_with_links | Hybrid | 4.0/10 | 86% | ⚠️ 40% |
| multilingual_kr_en | Hybrid | 10.0/10 | 0% | **100%** |
| large_diff_output | Hybrid | 6.4/10 | 27% | ⚠️ 64% |

*html_mixed: 81% = clean 단계에서 HTML/script/style 제거, 압축 불필요

**평균: 86.6% quality preservation, 42% compression**

### 핵심 인사이트

1. **auto_select가 JSON에 extract_fields 선택 → quality 70% → 89% (27% 향상)**
2. **Code 파일은 70% budget에서 100% quality 달성** (compression curve)
3. **HTML 콘텐츠는 clean 단계에서 81% 크기 감소** → 압축 단계 불필요
4. **markdown_with_links는 구조적 한계** — 링크 50개가 본문 앞에 위치
5. **large_diff_output**: Summary 섹션이 문서 끝에 위치 → hybrid head에 포함 안 됨

### Strategy Matrix (api_response_json)

| Strategy | Quality | Ratio | Chars |
|----------|---------|-------|-------|
| extract_fields | 8.9 | 5.9% | 335 |
| auto(extract_fields) | 8.9 | 5.9% | 335 |
| truncate | 7.0 | 18.1% | 1035 |
| hybrid | 7.0 | 17.5% | 1000 |

→ **extract_fields가 JSON에 최적: 94% 압축 + 89% quality**

### Compression Curve (code_file_large, Truncate)

| Budget | Quality |
|--------|---------|
| 30% | 6.2/10 |
| 50% | 8.4/10 |
| **70%** | **10.0/10** |
| 90% | 10.0/10 |

→ **70% budget가 최적 지점 (비용 대비 quality 최대)**

---

## 검증

```bash
# 벤치마크 테스트 (74 tests)
uv run pytest packages/memtomem-stm/tests/test_bench_pipeline.py -v

# 전체 STM 테스트 (356 tests)
uv run pytest packages/memtomem-stm/tests/ -v

# 리포트 생성 (Ollama 불필요)
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

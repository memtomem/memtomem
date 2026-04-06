"""Benchmark task definitions — realistic MCP tool responses."""

from __future__ import annotations

import json

from .harness import BenchTask

# ═══════════════════════════════════════════════════════════════════════════
# Content fixtures — realistic MCP tool response data
# ═══════════════════════════════════════════════════════════════════════════

API_RESPONSE_JSON = json.dumps(
    {
        "users": [
            {"id": 1, "name": "Alice", "email": "alice@example.com", "role": "admin"},
            {"id": 2, "name": "Bob", "email": "bob@example.com", "role": "editor"},
            {"id": 3, "name": "Charlie", "email": "charlie@example.com", "role": "viewer"},
        ]
        + [
            {
                "id": i,
                "name": f"User{i}",
                "email": f"user{i}@example.com",
                "role": "viewer",
            }
            for i in range(4, 51)
        ],
        "total": 50,
        "page": 1,
        "per_page": 50,
        "has_more": False,
    },
    indent=2,
)


CODE_FILE = """# Authentication Module

## Overview

This module handles JWT-based authentication for the API.
It supports access tokens and refresh tokens with configurable TTLs.

## Configuration

```python
AUTH_CONFIG = {
    "secret_key": "your-secret-key",
    "access_token_ttl": 3600,      # 1 hour
    "refresh_token_ttl": 604800,   # 7 days
    "algorithm": "HS256",
    "issuer": "memtomem-api",
}
```

## Token Generation

```python
def create_access_token(user_id: str, roles: list[str]) -> str:
    payload = {
        "sub": user_id,
        "roles": roles,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(seconds=AUTH_CONFIG["access_token_ttl"]),
        "iss": AUTH_CONFIG["issuer"],
    }
    return jwt.encode(payload, AUTH_CONFIG["secret_key"], algorithm=AUTH_CONFIG["algorithm"])
```

## Token Validation

```python
def validate_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            AUTH_CONFIG["secret_key"],
            algorithms=[AUTH_CONFIG["algorithm"]],
            issuer=AUTH_CONFIG["issuer"],
        )
        return {"valid": True, "user_id": payload["sub"], "roles": payload["roles"]}
    except jwt.ExpiredSignatureError:
        return {"valid": False, "error": "Token expired"}
    except jwt.InvalidTokenError as e:
        return {"valid": False, "error": str(e)}
```

## Middleware

```python
async def auth_middleware(request, call_next):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return JSONResponse(status_code=401, content={"error": "Missing token"})
    result = validate_token(token)
    if not result["valid"]:
        return JSONResponse(status_code=401, content={"error": result["error"]})
    request.state.user_id = result["user_id"]
    request.state.roles = result["roles"]
    return await call_next(request)
```

## Rate Limiting

Per-user rate limiting using Redis sliding window:
- Default: 100 requests per minute
- Admin: 500 requests per minute
- Configurable via `RATE_LIMIT_CONFIG`
"""


MEETING_NOTES = """# Sprint Planning — 2026-04-01

## Attendees

- Kim Cheolsu (Backend Lead)
- Park Jimin (Frontend)
- Lee Soyeon (DevOps)
- Choi Minjun (QA)

## Decisions Made

1. **Database Migration**: Migrate from PostgreSQL 14 to 16 by April 15
   - Kim Cheolsu leads the migration
   - Downtime window: Saturday 2am-4am KST

2. **Auth Rewrite**: Replace session-based auth with JWT
   - Motivated by legal/compliance requirements
   - Target: end of April

3. **Monitoring**: Add Grafana dashboards for API latency
   - grafana.internal/d/api-latency already exists
   - Need to add p99 latency panels

## Action Items

- [ ] Kim: PostgreSQL 16 compatibility test by April 8
- [ ] Park: JWT login UI mockup by April 10
- [ ] Lee: Grafana dashboard PR by April 5
- [ ] Choi: Regression test plan for auth migration

## Notes

- Sprint velocity: 42 points (target: 45)
- Next sprint planning: April 15
- Code freeze for mobile release: April 10
"""


HTML_MIXED = """<div class="api-docs">
<h1>API Reference</h1>
<p>This is the main API documentation.</p>
<script>console.log("tracking");</script>
<style>.hidden { display: none; }</style>

<h2>Endpoints</h2>
<p>The following endpoints are available:</p>

<h3>GET /api/users</h3>
<p>Returns a list of all users. Requires authentication.</p>
<p>Response format: JSON array of user objects.</p>

<h3>POST /api/users</h3>
<p>Creates a new user. Requires admin role.</p>
<p>Request body: JSON object with name, email, role fields.</p>

<h3>DELETE /api/users/:id</h3>
<p>Deletes a user by ID. Requires admin role.</p>

<p>For more details, see the full documentation:</p>
""" + "\n".join(
    f'- [Endpoint {i}](https://docs.example.com/api/endpoint/{i})'
    for i in range(30)
) + """

</div>
<p>Contact support@example.com for questions.</p>
"""


SHORT_RESPONSE = "OK. File saved successfully."


MARKDOWN_WITH_LINKS = """# Resource Collection

## Official Documentation

""" + "\n".join(
    f"- [Resource {i}](https://example.com/resource/{i}) — Description of resource {i}"
    for i in range(50)
) + """

## Key Concepts

The architecture uses a microservices pattern with service mesh.
Each service communicates via gRPC with Protobuf serialization.
The API gateway handles routing and rate limiting.

## Important Links

""" + "\n".join(
    f"- https://example.com/link/{i}"
    for i in range(20)
)


MULTILINGUAL_KR_EN = """# 프로젝트 아키텍처 결정 (Architecture Decisions)

## 웹 프레임워크 선택 (Web Framework Choice)

Flask 대신 FastAPI를 선택한 이유:
- 비동기 지원 (async/await native support)
- 자동 API 문서 생성 (automatic OpenAPI docs)
- Pydantic 기반 검증 (type-safe validation)
- 성능: Flask 대비 3배 이상 빠름 (3x faster than Flask)

## 데이터베이스 (Database)

PostgreSQL 16을 메인 DB로 사용:
- JSONB 칼럼으로 유연한 스키마 (flexible schema with JSONB)
- Full-text search 한국어 지원 (Korean FTS support)
- Connection pooling: PgBouncer 사용

## 캐시 전략 (Cache Strategy)

Redis LRU에서 LFU로 전환:
- Cache miss rate 40% 감소 (40% reduction in cache misses)
- Hot key 문제 해결 (resolved hot key problem)
- TTL: 기본 1시간, API 응답 5분 (default 1h, API response 5min)

## 배포 (Deployment)

Kubernetes 기반 배포:
- ArgoCD로 GitOps 워크플로우
- Horizontal Pod Autoscaler 설정
- Grafana 모니터링 dashboard
"""


# ═══════════════════════════════════════════════════════════════════════════
# Task definitions
# ═══════════════════════════════════════════════════════════════════════════


def get_all_tasks() -> list[BenchTask]:
    """Return all benchmark tasks."""
    return [
        BenchTask(
            task_id="api_response_json",
            description="JSON API response with 50 user records",
            content=API_RESPONSE_JSON,
            content_type="json",
            max_chars=1000,
            expected_keywords=["Alice", "admin", "total", "has_more"],
            expect_headings=0,
            expect_code_blocks=0,
        ),
        BenchTask(
            task_id="code_file_large",
            description="Python authentication module with code blocks",
            content=CODE_FILE,
            content_type="code",
            max_chars=1500,
            expected_keywords=["JWT", "access_token", "validate_token", "middleware"],
            expect_headings=3,
            expect_code_blocks=2,
        ),
        BenchTask(
            task_id="meeting_notes",
            description="Sprint planning meeting notes with decisions",
            content=MEETING_NOTES,
            content_type="markdown",
            max_chars=800,
            expected_keywords=["PostgreSQL", "Kim Cheolsu", "April 15", "Grafana"],
            expect_headings=2,
            expect_code_blocks=0,
        ),
        BenchTask(
            task_id="html_mixed",
            description="HTML API docs with script/style tags and link floods",
            content=HTML_MIXED,
            content_type="text",
            max_chars=800,
            expected_keywords=["API Reference", "Endpoints", "authentication", "admin"],
            expect_headings=0,  # Headings are in HTML, may be stripped
            expect_code_blocks=0,
        ),
        BenchTask(
            task_id="short_response",
            description="Short response that needs no compression",
            content=SHORT_RESPONSE,
            content_type="text",
            max_chars=1000,
            expected_keywords=["OK", "saved"],
            expect_headings=0,
            expect_code_blocks=0,
        ),
        BenchTask(
            task_id="markdown_with_links",
            description="Markdown with link floods in resource collection",
            content=MARKDOWN_WITH_LINKS,
            content_type="markdown",
            max_chars=600,
            expected_keywords=["microservices", "gRPC", "API gateway"],
            expect_headings=1,
            expect_code_blocks=0,
        ),
        BenchTask(
            task_id="multilingual_kr_en",
            description="Korean-English architecture decision document",
            content=MULTILINGUAL_KR_EN,
            content_type="markdown",
            max_chars=1000,
            expected_keywords=["FastAPI", "PostgreSQL", "Redis", "Kubernetes"],
            expect_headings=2,
            expect_code_blocks=0,
        ),
    ]

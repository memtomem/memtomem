"""Expanded benchmark datasets for publication-grade evaluation.

Adds multilingual (KR/JP), large documents (10K+), edge cases (empty, malformed),
and additional tasks per category to reach 20-30 tasks per content type.

Categories:
- multilingual_tasks(): Korean and Japanese content
- large_doc_tasks(): Documents exceeding 10K characters
- edge_case_tasks(): Empty, malformed, error responses, adversarial input
- additional_json_tasks(): More JSON varieties (GraphQL, logs, metrics)
- additional_markdown_tasks(): More markdown (RFC, wiki, tutorial)
- additional_code_tasks(): Go, Rust, SQL
- additional_text_tasks(): Email thread, support ticket, transcript
- additional_surfacing_tasks(): Multi-memory, conflict resolution
"""

from __future__ import annotations

from .harness import BenchTask, QAPair


# ═══════════════════════════════════════════════════════════════════════════
# Multilingual tasks
# ═══════════════════════════════════════════════════════════════════════════

_KR_TECHNICAL_DOC = """# 벡터 데이터베이스 성능 최적화 가이드

## 개요

이 문서는 대규모 벡터 검색 시스템의 프로덕션 배포를 위한 가이드입니다.
주요 엔진 3종(Qdrant, Weaviate, Milvus)의 벤치마크 결과를 포함합니다.

## 인덱싱 전략

### HNSW 파라미터 튜닝

- **M 파라미터**: 연결 수 제어 (권장: 16-48)
- **ef_construction**: 빌드 품질 (권장: 200 이상)
- **ef_search**: 검색 시 품질/속도 트레이드오프 (권장: 64-128)

M 값이 높을수록 리콜이 좋지만, 링크당 8바이트 메모리가 추가됩니다.

### 양자화 (Quantization)

벡터를 float32에서 int8로 양자화하면:
- 메모리 75% 절감
- 리콜 2-5% 하락
- QPS 30-50% 향상

## 벤치마크 결과

| 엔진 | 벡터 수 | 차원 | QPS | Recall@10 | p99 지연 |
|------|---------|------|------|-----------|---------|
| Qdrant | 1000만 | 768 | 5,200 | 0.96 | 8ms |
| Weaviate | 1000만 | 768 | 3,900 | 0.93 | 14ms |
| Milvus | 1000만 | 768 | 4,700 | 0.95 | 11ms |

## 프로덕션 체크리스트

1. 리콜 모니터링 설정 (0.90 미만 시 알림)
2. 인덱스 자동 리빌드 임계값 설정
3. 요청 레벨 타임아웃 (권장: 100ms)
4. WAL 활성화로 장애 복구 보장
5. 가용 RAM의 80%로 메모리 제한 설정

## 운영 팁

- 대용량 배치 인서트 후 인덱스 리빌드 필요
- 벡터 정규화가 일관되지 않으면 리콜이 급격히 하락
- GC 정지 시간 모니터링 필수
- 동시 쓰기 시 배치 크기 줄이기
"""

_JP_API_RESPONSE = """{
  "ステータス": "成功",
  "メタデータ": {
    "リクエストID": "req-jp-001",
    "タイムスタンプ": "2025-06-15T09:00:00+09:00",
    "処理時間ms": 87,
    "リージョン": "ap-northeast-1"
  },
  "データ": {
    "ユーザー一覧": [
      {
        "ID": "u-101",
        "氏名": "田中太郎",
        "メール": "tanaka@example.co.jp",
        "役割": "管理者",
        "部署": "エンジニアリング",
        "入社日": "2022-04-01",
        "権限": ["読取", "書込", "削除", "ユーザー管理"],
        "タグ": ["チームリーダー", "オンコール"]
      },
      {
        "ID": "u-102",
        "氏名": "佐藤花子",
        "メール": "sato@example.co.jp",
        "役割": "開発者",
        "部署": "エンジニアリング",
        "入社日": "2023-07-15",
        "権限": ["読取", "書込"],
        "タグ": ["バックエンド", "Rust"]
      },
      {
        "ID": "u-103",
        "氏名": "鈴木一郎",
        "メール": "suzuki@example.co.jp",
        "役割": "デザイナー",
        "部署": "プロダクト",
        "入社日": "2024-01-10",
        "権限": ["読取"],
        "タグ": ["UX", "アクセシビリティ"]
      }
    ],
    "ページネーション": {"ページ": 1, "件数": 25, "合計": 3}
  }
}"""

_KR_MEETING_NOTES = """# 스프린트 24 회고 — 2025-06-20

**참석자:** 김서연 (PM), 이준호 (TL), 박지민 (BE), 최은영 (FE), 정현우 (QA)

## 잘한 점

- 배포 파이프라인 최적화: 45분 → 8분 (Docker 레이어 캐싱 + 병렬 빌드)
- 신규 가입 전환율 78% → 91% (최은영의 온보딩 리디자인)
- P0 장애 0건 — 4스프린트 연속 달성
- 박지민의 캐싱 레이어 도입으로 API 응답 시간 40% 감소
  (p50: 220ms → 132ms, p99: 850ms → 510ms)

## 개선 필요

- 테스트 커버리지 85% → 76%로 하락 → CI 커버리지 게이트 필요
- 디자인 핸드오프 2-3일 지연 반복 → Figma Dev Mode + Storybook 도입 제안
- 스프린트 벨로시티 36/44 포인트 (82%) — 검색 리팩토링 에픽 과대 추정

## 액션 아이템

| 담당자 | 항목 | 기한 |
|--------|------|------|
| 정현우 | CI 커버리지 게이트 (최소 83%) 추가 | 2025-06-27 |
| 김서연 | Figma Dev Mode 교육 세션 스케줄 | 2025-06-25 |
| 이준호 | 검색 리팩토링 작은 티켓으로 분할 | 2025-06-23 |
| 박지민 | 캐싱 아키텍처 노션 문서화 | 2025-06-26 |

## 결정 사항

- **도입**: Sprint 25부터 모든 디자인 핸드오프에 Figma Dev Mode 적용
- **연기**: Kubernetes 마이그레이션을 Q4로 연기 (Q3 런칭 기간 리스크)
- **중단**: 레거시 XML 내보내기 — 사용 고객 2곳만 남음
"""


def multilingual_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="ml-kr-vector-guide",
            description="Korean technical doc on vector DB performance",
            content=_KR_TECHNICAL_DOC,
            content_type="markdown",
            max_chars=600,
            expected_keywords=["HNSW", "Qdrant", "Weaviate", "Milvus", "양자화", "리콜"],
            expect_headings=4,
            qa_pairs=[
                QAPair("Qdrant의 QPS는?", "5,200"),
                QAPair("양자화 시 메모리 절감 비율은?", "75%"),
                QAPair("권장 ef_search 범위는?", "64-128"),
                QAPair("Milvus의 p99 지연은?", "11ms"),
            ],
        ),
        BenchTask(
            task_id="ml-jp-api-response",
            description="Japanese API response with user data",
            content=_JP_API_RESPONSE,
            content_type="json",
            max_chars=400,
            expected_keywords=["田中太郎", "佐藤花子", "鈴木一郎", "管理者", "開発者"],
            qa_pairs=[
                QAPair("田中太郎の役割は?", "管理者"),
                QAPair("佐藤花子の部署は?", "エンジニアリング"),
                QAPair("鈴木一郎のIDは?", "u-103"),
                QAPair("合計ユーザー数は?", "3"),
            ],
        ),
        BenchTask(
            task_id="ml-kr-meeting-notes",
            description="Korean sprint retrospective with action items",
            content=_KR_MEETING_NOTES,
            content_type="markdown",
            max_chars=500,
            expected_keywords=["김서연", "이준호", "박지민", "Figma", "커버리지"],
            expect_headings=4,
            qa_pairs=[
                QAPair("배포 시간이 얼마로 줄었나?", "8분"),
                QAPair("API p50 응답 시간은?", "132ms"),
                QAPair("스프린트 벨로시티는?", "36"),
                QAPair("커버리지 게이트 최소값은?", "83%"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Large document tasks (10K+ characters)
# ═══════════════════════════════════════════════════════════════════════════

_LARGE_API_LOG = "".join([
    '{"logs": [\n',
    *[
        f'  {{"id": "log-{i:04d}", "timestamp": "2025-06-15T{10 + i // 60:02d}:{i % 60:02d}:00Z", '
        f'"level": "{["INFO", "WARN", "ERROR", "DEBUG"][i % 4]}", '
        f'"service": "{["api-gateway", "auth-service", "user-service", "payment-service"][i % 4]}", '
        f'"message": "Request processed in {50 + i * 3}ms", '
        f'"trace_id": "trace-{i:04d}", '
        f'"status_code": {[200, 200, 200, 500, 429, 200, 200, 200, 503, 200][i % 10]}}},\n'
        for i in range(200)
    ],
    '  {"id": "log-CRITICAL", "timestamp": "2025-06-15T13:37:00Z", '
    '"level": "ERROR", "service": "payment-service", '
    '"message": "Database connection pool exhausted — all 50 connections in use, '
    '23 requests queued, oldest waiting 12.4 seconds", '
    '"trace_id": "trace-CRITICAL", "status_code": 503}\n',
    ']}\n',
])

_LARGE_MARKDOWN_RFC = """# RFC: Unified Event Processing Architecture

**Author:** Architecture Team
**Status:** Draft
**Date:** 2025-06-10

## Abstract

This RFC proposes a unified event processing architecture to replace the current
fragmented system of 7 independent message queues and 12 consumer services.

## Background

""" + "\n".join([
    f"### Problem {i+1}: {title}\n\n{desc}\n"
    for i, (title, desc) in enumerate([
        ("Inconsistent Event Schemas", "Currently 7 teams define their own event schemas with no validation. "
         "This has caused 14 production incidents in the last quarter due to schema drift."),
        ("Duplicate Processing", "Events are processed an average of 2.3 times across services. "
         "The payment team found that 12% of refunds were triggered twice due to duplicate events."),
        ("No Dead Letter Queue", "Failed events are silently dropped. "
         "Analysis shows we lose approximately 0.3% of events daily (~45,000 events)."),
        ("Scaling Bottlenecks", "Each consumer scales independently with different strategies. "
         "The order service uses horizontal pods while auth uses thread pools, "
         "leading to resource fragmentation."),
        ("Monitoring Gaps", "No unified dashboard for event flow. "
         "MTTR for event-related incidents averages 4.2 hours vs 1.1 hours for API issues."),
    ])
]) + """
## Proposed Architecture

### Event Schema Registry

All events must be registered in a central Avro/Protobuf schema registry.
- Schema evolution rules: backward-compatible only
- Validation at producer side (fail-fast)
- Auto-generated SDKs per language (Python, Go, TypeScript)

### Unified Event Bus

Replace 7 queues with a single Apache Kafka cluster:
- 3 brokers, replication factor 3
- Topic per domain (orders, payments, auth, inventory, notifications)
- Partitioning by entity_id for ordering guarantees
- Retention: 7 days raw, 90 days compressed

### Consumer Framework

Standardized consumer framework with:
- Exactly-once processing via idempotency keys
- Automatic dead letter queue with retry policy (3x exponential backoff)
- Circuit breaker for downstream dependencies
- Structured logging with trace_id propagation

### Observability

- Kafka Connect → Elasticsearch for real-time event search
- Prometheus metrics: lag per consumer group, throughput, error rate
- Grafana dashboards: one per domain + unified overview
- PagerDuty integration: alert if consumer lag > 1000 or error rate > 1%

## Migration Plan

Phase 1 (Weeks 1-4): Deploy schema registry and Kafka cluster
Phase 2 (Weeks 5-8): Migrate payment and order events (highest volume)
Phase 3 (Weeks 9-12): Migrate remaining domains
Phase 4 (Weeks 13-16): Decommission old queues, enable monitoring

## Cost Analysis

| Item | Current | Proposed | Delta |
|------|---------|----------|-------|
| Infrastructure | $12,400/mo | $8,200/mo | -34% |
| Eng time (incidents) | 180 hrs/quarter | ~40 hrs/quarter | -78% |
| Data loss | 0.3% events/day | <0.001% | -99.7% |

## Risks

1. Kafka operational complexity — mitigate with managed service (Confluent/MSK)
2. Migration downtime — mitigate with dual-write period
3. Team training — 2-week Kafka workshop planned

## Decision

Approved by: CTO (2025-06-12), VP Engineering (2025-06-11)
Start date: 2025-07-01
"""


def large_doc_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="large-api-logs",
            description="200+ API log entries with one critical error buried in noise",
            content=_LARGE_API_LOG,
            content_type="json",
            max_chars=800,
            expected_keywords=["payment-service", "connection pool exhausted", "503"],
            qa_pairs=[
                QAPair("What critical error occurred?", "connection pool exhausted"),
                QAPair("How many connections were in use?", "50"),
                QAPair("How many requests were queued?", "23"),
                QAPair("What was the longest wait time?", "12.4 seconds"),
            ],
        ),
        BenchTask(
            task_id="large-rfc-event-arch",
            description="RFC document on unified event processing architecture (~12K chars)",
            content=_LARGE_MARKDOWN_RFC,
            content_type="markdown",
            max_chars=1500,
            expected_keywords=["Kafka", "schema registry", "dead letter queue", "exactly-once", "Avro"],
            expect_headings=6,
            qa_pairs=[
                QAPair("How many independent queues exist currently?", "7"),
                QAPair("What percentage of events are lost daily?", "0.3%"),
                QAPair("What is the proposed retention for raw events?", "7 days"),
                QAPair("What is the infrastructure cost reduction?", "34%"),
                QAPair("What is the current MTTR for event issues?", "4.2 hours"),
                QAPair("What replication factor is proposed?", "3"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════


def edge_case_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="edge-empty-response",
            description="Empty response — should pass through cleanly",
            content="",
            content_type="text",
            max_chars=100,
            expected_keywords=[],
            qa_pairs=[],
        ),
        BenchTask(
            task_id="edge-whitespace-only",
            description="Whitespace-only response",
            content="   \n\n\t\t\n   ",
            content_type="text",
            max_chars=100,
            expected_keywords=[],
            qa_pairs=[],
        ),
        BenchTask(
            task_id="edge-malformed-json",
            description="Malformed JSON — pipeline should not crash",
            content='{"users": [{"name": "Alice", "role": "admin"}, {"name": "Bob", incomplete...',
            content_type="json",
            max_chars=200,
            expected_keywords=["Alice", "Bob"],
            qa_pairs=[
                QAPair("Who is the admin?", "Alice"),
            ],
        ),
        BenchTask(
            task_id="edge-error-response",
            description="HTTP error response — preserve error details",
            content="""{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Too many requests. Limit: 100 req/min. Current: 142 req/min.",
    "retry_after_seconds": 38,
    "request_id": "req-err-001",
    "documentation_url": "https://docs.example.com/rate-limiting"
  }
}""",
            content_type="json",
            max_chars=300,
            expected_keywords=["RATE_LIMIT_EXCEEDED", "100 req/min", "142 req/min", "38"],
            qa_pairs=[
                QAPair("What is the rate limit?", "100 req/min"),
                QAPair("How many seconds until retry?", "38"),
            ],
        ),
        BenchTask(
            task_id="edge-binary-like",
            description="Text with binary-like garbage characters",
            content="HEADER\x00\x01DATA: key=value123\x00\x02 "
                    "status=ok\x00\x03 count=42\n"
                    "FOOTER: checksum=abc123",
            content_type="text",
            max_chars=200,
            expected_keywords=["key=value123", "status=ok", "count=42"],
            qa_pairs=[
                QAPair("What is the count value?", "42"),
            ],
        ),
        BenchTask(
            task_id="edge-single-line",
            description="Extremely long single line — tests line-based processing",
            content="METRIC " + " | ".join(
                [f"cpu={50+i%50}% mem={30+i%40}% disk={10+i%60}% ts={i}"
                 for i in range(100)]
            ),
            content_type="text",
            max_chars=500,
            expected_keywords=["METRIC", "cpu=", "mem=", "disk="],
            qa_pairs=[],
        ),
        BenchTask(
            task_id="edge-repetitive",
            description="Highly repetitive content — dedup should help significantly",
            content="\n".join([
                f"[2025-06-15T10:{i:02d}:00Z] INFO health_check: status=ok latency={5+i%3}ms"
                for i in range(60)
            ] + [
                "[2025-06-15T10:42:00Z] ERROR health_check: status=TIMEOUT latency=30012ms "
                "error='connection refused to db-primary.internal:5432'"
            ]),
            content_type="text",
            max_chars=400,
            expected_keywords=["health_check", "TIMEOUT", "30012ms", "connection refused"],
            qa_pairs=[
                QAPair("What was the error?", "connection refused"),
                QAPair("What was the timeout latency?", "30012ms"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Additional JSON tasks
# ═══════════════════════════════════════════════════════════════════════════

_JSON_GRAPHQL_RESPONSE = """{
  "data": {
    "repository": {
      "name": "memtomem",
      "owner": {"login": "memtomem-org"},
      "stargazerCount": 1247,
      "forkCount": 89,
      "primaryLanguage": {"name": "Python"},
      "issues": {
        "totalCount": 42,
        "nodes": [
          {"number": 128, "title": "Memory leak in vector search", "state": "OPEN", "labels": [{"name": "bug"}, {"name": "P1"}], "author": {"login": "alice-dev"}, "createdAt": "2025-06-10"},
          {"number": 125, "title": "Add MCP gateway rate limiting", "state": "CLOSED", "labels": [{"name": "enhancement"}], "author": {"login": "bob-dev"}, "createdAt": "2025-06-08"},
          {"number": 123, "title": "Korean tokenization improvements", "state": "OPEN", "labels": [{"name": "i18n"}, {"name": "P2"}], "author": {"login": "carla-dev"}, "createdAt": "2025-06-05"}
        ]
      },
      "pullRequests": {
        "totalCount": 156,
        "nodes": [
          {"number": 301, "title": "feat: implement LLM-as-Judge benchmark", "state": "OPEN", "additions": 450, "deletions": 12, "author": {"login": "alice-dev"}},
          {"number": 298, "title": "fix: connection pool exhaustion under load", "state": "MERGED", "additions": 23, "deletions": 8, "author": {"login": "bob-dev"}}
        ]
      }
    }
  }
}"""

_JSON_METRICS_TIMESERIES = """{
  "metric": "api_latency_p99",
  "unit": "milliseconds",
  "interval": "5m",
  "period": {"start": "2025-06-15T10:00:00Z", "end": "2025-06-15T12:00:00Z"},
  "tags": {"service": "payment-gateway", "region": "us-east-1", "env": "production"},
  "datapoints": [
    {"ts": "10:00", "value": 45.2, "count": 1200},
    {"ts": "10:05", "value": 48.1, "count": 1180},
    {"ts": "10:10", "value": 52.3, "count": 1250},
    {"ts": "10:15", "value": 89.7, "count": 980, "anomaly": true},
    {"ts": "10:20", "value": 234.5, "count": 650, "anomaly": true},
    {"ts": "10:25", "value": 456.8, "count": 320, "anomaly": true},
    {"ts": "10:30", "value": 123.4, "count": 890},
    {"ts": "10:35", "value": 67.2, "count": 1100},
    {"ts": "10:40", "value": 49.8, "count": 1210},
    {"ts": "10:45", "value": 46.1, "count": 1230}
  ],
  "summary": {
    "mean": 121.3, "median": 59.75, "p95": 345.7, "p99": 456.8,
    "anomaly_window": {"start": "10:15", "end": "10:30", "duration_min": 15},
    "peak": {"ts": "10:25", "value": 456.8, "probable_cause": "database failover event"}
  }
}"""


def additional_json_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="json-graphql-repo",
            description="GraphQL response with repository stats, issues, and PRs",
            content=_JSON_GRAPHQL_RESPONSE,
            content_type="json",
            max_chars=500,
            expected_keywords=["memtomem", "1247", "memory leak", "rate limiting"],
            qa_pairs=[
                QAPair("How many stars does the repo have?", "1247"),
                QAPair("What is issue #128 about?", "Memory leak"),
                QAPair("Who authored the LLM-as-Judge PR?", "alice-dev"),
                QAPair("How many open issues total?", "42"),
            ],
        ),
        BenchTask(
            task_id="json-metrics-timeseries",
            description="API latency metrics with anomaly detection",
            content=_JSON_METRICS_TIMESERIES,
            content_type="json",
            max_chars=400,
            expected_keywords=["payment-gateway", "456.8", "anomaly", "failover"],
            qa_pairs=[
                QAPair("What was the peak latency?", "456.8"),
                QAPair("How long was the anomaly window?", "15"),
                QAPair("What caused the peak?", "database failover"),
                QAPair("What is the median latency?", "59.75"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Additional markdown tasks
# ═══════════════════════════════════════════════════════════════════════════

_MD_TUTORIAL = """# Building a Real-Time Search Pipeline

## Prerequisites

- Python 3.12+
- SQLite with FTS5 extension
- `sqlite-vec` for vector search

## Step 1: Schema Setup

```sql
CREATE VIRTUAL TABLE documents_fts USING fts5(
    title, content, tags,
    tokenize='porter unicode61'
);

CREATE TABLE document_vectors (
    doc_id INTEGER PRIMARY KEY,
    embedding BLOB  -- F32 serialized
);
```

## Step 2: Indexing Pipeline

```python
from chunking import MarkdownChunker
from embedding import OllamaEmbedder

async def index_document(path: str, db: Database):
    text = Path(path).read_text()
    chunks = MarkdownChunker().chunk(text)
    embeddings = await OllamaEmbedder().embed_batch(
        [c.content for c in chunks]
    )
    for chunk, emb in zip(chunks, embeddings):
        doc_id = db.insert_fts(chunk.title, chunk.content, chunk.tags)
        db.insert_vector(doc_id, emb)
```

## Step 3: Hybrid Search

```python
async def search(query: str, top_k: int = 10):
    # BM25 (keyword)
    bm25_results = db.fts_search(query, limit=top_k * 2)

    # Dense (semantic)
    query_vec = await embedder.embed_query(query)
    dense_results = db.vector_search(query_vec, limit=top_k * 2)

    # RRF fusion
    return reciprocal_rank_fusion(bm25_results, dense_results, k=60)
```

## Step 4: Performance Tuning

- **BM25**: Tune `k1` (1.2-2.0) and `b` (0.5-0.8) for your corpus
- **Dense**: Match embedding model to your domain
- **RRF k**: Higher k (60-100) smooths out ranking differences
- **Cache**: 30s TTL for frequent queries
"""

_MD_API_DOCS = """# Payment Gateway API Reference

## Authentication

All requests require `Authorization: Bearer <token>` header.
Tokens expire after 3600 seconds. Use `/auth/refresh` to renew.

## Endpoints

### POST /v2/charges

Create a new charge.

**Request:**
```json
{
  "amount": 2999,
  "currency": "USD",
  "source": "tok_visa_4242",
  "description": "Order #12345",
  "metadata": {"order_id": "ORD-12345"}
}
```

**Response (201):**
```json
{
  "id": "ch_abc123",
  "amount": 2999,
  "currency": "USD",
  "status": "succeeded",
  "created": "2025-06-15T10:00:00Z"
}
```

**Errors:**
- `400` — Invalid request (missing required fields)
- `402` — Card declined
- `429` — Rate limit exceeded (100 req/min)

### GET /v2/charges/:id

Retrieve a charge by ID.

### POST /v2/refunds

Create a refund for an existing charge.

**Request:**
```json
{
  "charge_id": "ch_abc123",
  "amount": 1500,
  "reason": "customer_request"
}
```

## Webhooks

Events are sent via POST to your registered webhook URL.
Verify signatures using `X-Signature-256` header with HMAC-SHA256.

Supported events:
- `charge.succeeded`
- `charge.failed`
- `refund.created`
- `dispute.opened`

## Rate Limits

| Endpoint | Limit | Window |
|----------|-------|--------|
| POST /charges | 100/min | Sliding |
| GET /charges | 500/min | Sliding |
| POST /refunds | 50/min | Sliding |
"""


def additional_markdown_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="md-search-tutorial",
            description="Step-by-step tutorial on building a search pipeline",
            content=_MD_TUTORIAL,
            content_type="markdown",
            max_chars=600,
            expected_keywords=["FTS5", "sqlite-vec", "BM25", "RRF", "OllamaEmbedder"],
            expect_headings=5,
            expect_code_blocks=3,
            qa_pairs=[
                QAPair("What tokenizer is used for FTS5?", "porter unicode61"),
                QAPair("What RRF k value is recommended?", "60"),
                QAPair("What cache TTL is suggested?", "30s"),
                QAPair("What embedding format is used?", "F32"),
            ],
        ),
        BenchTask(
            task_id="md-api-docs",
            description="Payment gateway API reference with endpoints and rate limits",
            content=_MD_API_DOCS,
            content_type="markdown",
            max_chars=600,
            expected_keywords=["charges", "refunds", "webhooks", "HMAC-SHA256", "rate limit"],
            expect_headings=5,
            expect_code_blocks=2,
            qa_pairs=[
                QAPair("What is the token expiry time?", "3600"),
                QAPair("What is the charge rate limit?", "100/min"),
                QAPair("What header contains webhook signatures?", "X-Signature-256"),
                QAPair("What is the refund rate limit?", "50/min"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Additional code tasks
# ═══════════════════════════════════════════════════════════════════════════

_CODE_GO_SERVER = '''package main

import (
\t"context"
\t"encoding/json"
\t"log"
\t"net/http"
\t"sync"
\t"time"
)

type RateLimiter struct {
\tmu       sync.Mutex
\ttokens   float64
\tmax      float64
\trate     float64
\tlastTime time.Time
}

func NewRateLimiter(maxTokens, refillRate float64) *RateLimiter {
\treturn &RateLimiter{
\t\ttokens:   maxTokens,
\t\tmax:      maxTokens,
\t\trate:     refillRate,
\t\tlastTime: time.Now(),
\t}
}

func (rl *RateLimiter) Allow() bool {
\trl.mu.Lock()
\tdefer rl.mu.Unlock()
\tnow := time.Now()
\telapsed := now.Sub(rl.lastTime).Seconds()
\trl.tokens = min(rl.max, rl.tokens+elapsed*rl.rate)
\trl.lastTime = now
\tif rl.tokens >= 1 {
\t\trl.tokens--
\t\treturn true
\t}
\treturn false
}

type HealthResponse struct {
\tStatus    string  `json:"status"`
\tUptime    float64 `json:"uptime_seconds"`
\tVersion   string  `json:"version"`
\tDBLatency float64 `json:"db_latency_ms"`
}

func healthHandler(startTime time.Time) http.HandlerFunc {
\treturn func(w http.ResponseWriter, r *http.Request) {
\t\tuptime := time.Since(startTime).Seconds()
\t\tresp := HealthResponse{
\t\t\tStatus:    "ok",
\t\t\tUptime:    uptime,
\t\t\tVersion:   "2.1.0",
\t\t\tDBLatency: 3.2,
\t\t}
\t\tw.Header().Set("Content-Type", "application/json")
\t\tjson.NewEncoder(w).Encode(resp)
\t}
}

func main() {
\trl := NewRateLimiter(100, 10)
\tstart := time.Now()
\tmux := http.NewServeMux()
\tmux.HandleFunc("/health", healthHandler(start))
\tlog.Fatal(http.ListenAndServe(":8080", mux))
}'''

_CODE_SQL_ANALYTICS = """-- Analytics queries for user engagement dashboard

-- Daily active users (DAU) with 7-day rolling average
WITH daily_counts AS (
    SELECT
        DATE(event_timestamp) AS event_date,
        COUNT(DISTINCT user_id) AS dau
    FROM user_events
    WHERE event_timestamp >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY DATE(event_timestamp)
),
rolling AS (
    SELECT
        event_date,
        dau,
        AVG(dau) OVER (ORDER BY event_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS dau_7d_avg,
        LAG(dau, 7) OVER (ORDER BY event_date) AS dau_prev_week
    FROM daily_counts
)
SELECT
    event_date,
    dau,
    ROUND(dau_7d_avg, 1) AS dau_7d_avg,
    ROUND((dau - dau_prev_week)::numeric / NULLIF(dau_prev_week, 0) * 100, 1) AS wow_change_pct
FROM rolling
ORDER BY event_date DESC;

-- Retention cohort analysis (week 0 to week 4)
WITH cohorts AS (
    SELECT
        user_id,
        DATE_TRUNC('week', MIN(event_timestamp)) AS cohort_week
    FROM user_events
    GROUP BY user_id
),
activity AS (
    SELECT
        c.cohort_week,
        DATE_TRUNC('week', e.event_timestamp) AS activity_week,
        COUNT(DISTINCT e.user_id) AS active_users
    FROM user_events e
    JOIN cohorts c ON e.user_id = c.user_id
    WHERE e.event_timestamp >= CURRENT_DATE - INTERVAL '5 weeks'
    GROUP BY c.cohort_week, DATE_TRUNC('week', e.event_timestamp)
)
SELECT
    cohort_week,
    MAX(CASE WHEN week_number = 0 THEN active_users END) AS week_0,
    MAX(CASE WHEN week_number = 1 THEN retention_pct END) AS week_1_pct,
    MAX(CASE WHEN week_number = 2 THEN retention_pct END) AS week_2_pct,
    MAX(CASE WHEN week_number = 3 THEN retention_pct END) AS week_3_pct,
    MAX(CASE WHEN week_number = 4 THEN retention_pct END) AS week_4_pct
FROM (
    SELECT
        a.cohort_week,
        EXTRACT(WEEK FROM a.activity_week) - EXTRACT(WEEK FROM a.cohort_week) AS week_number,
        a.active_users,
        ROUND(a.active_users::numeric / FIRST_VALUE(a.active_users) OVER (
            PARTITION BY a.cohort_week ORDER BY a.activity_week
        ) * 100, 1) AS retention_pct
    FROM activity a
) sub
GROUP BY cohort_week
ORDER BY cohort_week DESC;

-- Revenue by plan tier with MoM growth
SELECT
    plan_tier,
    SUM(amount) AS total_revenue,
    COUNT(DISTINCT user_id) AS paying_users,
    ROUND(SUM(amount)::numeric / COUNT(DISTINCT user_id), 2) AS arpu,
    ROUND((SUM(amount) - LAG(SUM(amount)) OVER (PARTITION BY plan_tier ORDER BY month))::numeric
        / NULLIF(LAG(SUM(amount)) OVER (PARTITION BY plan_tier ORDER BY month), 0) * 100, 1)
        AS mom_growth_pct
FROM payments
WHERE created_at >= CURRENT_DATE - INTERVAL '3 months'
GROUP BY plan_tier, DATE_TRUNC('month', created_at) AS month
ORDER BY plan_tier, month;
"""


def additional_code_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="code-go-server",
            description="Go HTTP server with rate limiter and health endpoint",
            content=_CODE_GO_SERVER,
            content_type="code",
            max_chars=600,
            expected_keywords=["RateLimiter", "Allow", "healthHandler", "NewRateLimiter"],
            expect_code_blocks=1,
            qa_pairs=[
                QAPair("What is the max token count?", "100"),
                QAPair("What port does the server listen on?", "8080"),
                QAPair("What is the server version?", "2.1.0"),
                QAPair("What is the token refill rate?", "10"),
            ],
        ),
        BenchTask(
            task_id="code-sql-analytics",
            description="SQL analytics queries: DAU, retention cohorts, revenue by tier",
            content=_CODE_SQL_ANALYTICS,
            content_type="code",
            max_chars=700,
            expected_keywords=["DAU", "retention", "cohort", "ARPU", "revenue"],
            expect_code_blocks=1,
            qa_pairs=[
                QAPair("What is the rolling average window?", "7"),
                QAPair("How many weeks of retention are tracked?", "4"),
                QAPair("What table stores payment data?", "payments"),
                QAPair("What time range is used for revenue?", "3 months"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Additional text tasks
# ═══════════════════════════════════════════════════════════════════════════

_TEXT_EMAIL_THREAD = """From: Sarah Kim <sarah@company.com>
To: Engineering Team <eng@company.com>
Subject: Re: Q3 Launch Plan — Final Review
Date: 2025-06-14 09:15

Team,

After reviewing the launch checklist with stakeholders yesterday, here's the
updated timeline:

- June 20: Feature freeze (no new features after this date)
- June 23-27: QA regression testing sprint
- June 30: Staging deploy + load testing
- July 7: Production rollout (10% canary → 50% → 100% over 3 days)
- July 14: Post-launch retrospective

Key blockers we need to resolve before June 20:
1. The search reindex migration is still running 4x slower than estimated.
   Marcus, can you look at the query plan for the bulk insert?
2. Mobile SDK v3.0 compatibility — Tomas reports 2 breaking changes in
   the auth token format.
3. Rate limiting configuration for the new GraphQL endpoint — Jordan,
   please align with the API gateway defaults (100 req/min per user).

Budget update: we have $15,000 remaining in the Q3 infrastructure budget.
The estimated cost for the launch (extra compute + monitoring) is $8,200.

Please reply with your status by EOD Friday.

Best,
Sarah

---
From: Marcus Johnson <marcus@company.com>
To: Engineering Team <eng@company.com>
Subject: Re: Q3 Launch Plan — Final Review
Date: 2025-06-14 10:42

Sarah,

Looked at the reindex query plan. The issue is a missing index on
`documents.updated_at` — the bulk insert is doing a full table scan for
dedup. I'll add the index today. Expected improvement: 4x → should bring
us within the 2-hour window.

For the mobile SDK, I spoke with Tomas — we can add a backward-compatible
auth wrapper that handles both v2 and v3 token formats. 1 day of work.

I'll have both done by Monday.

— Marcus
"""

_TEXT_SUPPORT_TICKET = """TICKET: SUP-2025-8847
Priority: High
Customer: Acme Corp (Enterprise, ARR $240K)
Assigned: Support Level 2 — Diana Chen

ISSUE:
Customer reports that their automated data export job (scheduled daily at
02:00 UTC) has been failing for the past 3 days. The export generates a
CSV file with ~2M rows of transaction data.

Error message from customer logs:
  "Export failed: TimeoutError — request exceeded 300s limit. Partial
  file written: 847,293 of estimated 2,100,000 rows."

INVESTIGATION:
1. Checked server-side logs — export process starts successfully but
   the PostgreSQL query times out at the 5-minute mark.
2. Query analysis: the export query joins 4 tables (transactions,
   customers, products, categories) without proper indexing on the
   join columns.
3. The query ran fine until June 12 when the transactions table grew
   past 50M rows (was 35M at last successful export).
4. The missing index is on transactions.customer_id — currently no
   index, causing nested loop join with full scan.

ROOT CAUSE:
Missing index on transactions.customer_id combined with table growth
past 50M rows caused the export query to exceed the 5-minute timeout.

RESOLUTION:
1. [DONE] Created index: CREATE INDEX CONCURRENTLY idx_txn_customer
   ON transactions(customer_id) — completed in 12 minutes
2. [DONE] Re-ran export — completed in 47 seconds (was >5 minutes)
3. [TODO] Add query performance monitoring alert for exports > 2 min
4. [TODO] Schedule regular ANALYZE on high-growth tables

CUSTOMER COMMUNICATION:
Sent update to customer (Diana, 2025-06-15 14:30 UTC):
"Issue resolved. A missing database index caused the timeout. We've
added the index and your export is running normally. We're also adding
monitoring to catch similar issues proactively."

Customer confirmed export working. Ticket pending close (48h window).
"""


def additional_text_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="text-email-thread",
            description="Email thread about Q3 launch plan with blockers and timeline",
            content=_TEXT_EMAIL_THREAD,
            content_type="text",
            max_chars=600,
            expected_keywords=["feature freeze", "June 20", "canary", "reindex", "Marcus"],
            qa_pairs=[
                QAPair("When is the feature freeze?", "June 20"),
                QAPair("What is the remaining infrastructure budget?", "$15,000"),
                QAPair("What is the estimated launch cost?", "$8,200"),
                QAPair("What caused the reindex slowdown?", "missing index"),
                QAPair("What is the GraphQL rate limit?", "100 req/min"),
            ],
        ),
        BenchTask(
            task_id="text-support-ticket",
            description="Enterprise support ticket with investigation and resolution",
            content=_TEXT_SUPPORT_TICKET,
            content_type="text",
            max_chars=700,
            expected_keywords=["SUP-2025-8847", "Acme Corp", "TimeoutError", "customer_id"],
            qa_pairs=[
                QAPair("What is the customer's ARR?", "$240K"),
                QAPair("How many rows in the partial export?", "847,293"),
                QAPair("How long did the index creation take?", "12 minutes"),
                QAPair("How fast does the export run now?", "47 seconds"),
                QAPair("What table grew past 50M rows?", "transactions"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Additional surfacing tasks
# ═══════════════════════════════════════════════════════════════════════════


def additional_surfacing_tasks() -> list[BenchTask]:
    """Tasks needing multiple memories to fully answer."""
    return [
        BenchTask(
            task_id="surf-multi-memory-debug",
            description="Error log needing 3 separate memories for full diagnosis",
            content="""ERROR 2025-06-15 03:42:11 [payment-service] Connection pool exhausted
pool_size=50 active=50 queued=23 oldest_wait_ms=12400
Trace: payment-service → order-service → inventory-service
Last successful request: 03:41:55 (16s ago)
Error rate: 100% (last 30s)""",
            content_type="text",
            max_chars=400,
            expected_keywords=["pool_size=50", "payment-service"],
            surfacing_memories=[
                "Payment service pool_size was reduced from 100 to 50 on June 10 to save memory after the k8s node scaling issue.",
                "Inventory service deployed a new version at 03:40 on June 15 that adds a 5s timeout to stock queries.",
                "The order-service → inventory-service circuit breaker was disabled in config on June 12 for debugging and never re-enabled.",
            ],
            qa_pairs=[
                QAPair("What is the pool size?", "50", source="content"),
                QAPair("Why was pool size reduced?", "k8s node scaling issue", source="memory"),
                QAPair("What changed at 03:40?", "new version", source="memory"),
                QAPair("Is the circuit breaker enabled?", "never re-enabled", source="memory"),
            ],
        ),
        BenchTask(
            task_id="surf-conflict-resolution",
            description="Config output where memories provide conflicting context",
            content="""{
  "service": "auth-gateway",
  "max_connections": 200,
  "timeout_ms": 5000,
  "retry_count": 3,
  "tls_version": "1.2",
  "region": "us-east-1"
}""",
            content_type="json",
            max_chars=300,
            expected_keywords=["auth-gateway", "200", "5000"],
            surfacing_memories=[
                "Auth gateway was upgraded to TLS 1.3 on June 1 but the config still shows 1.2 — this is a known display bug in the admin panel.",
                "The team decided to increase max_connections to 500 for Q3 launch but the change hasn't been deployed yet.",
                "The 5000ms timeout was intentionally set high during the database migration; normal value should be 2000ms.",
            ],
            qa_pairs=[
                QAPair("What TLS version is configured?", "1.2", source="content"),
                QAPair("What TLS is actually running?", "1.3", source="memory"),
                QAPair("What should max_connections be for Q3?", "500", source="memory"),
                QAPair("What is the normal timeout value?", "2000ms", source="memory"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════

# Category map for statistical analysis
EXPANDED_CATEGORY_MAP: dict[str, str] = {}


def _register_categories(tasks: list[BenchTask], category: str) -> None:
    for t in tasks:
        EXPANDED_CATEGORY_MAP[t.task_id] = category


def expanded_all_tasks() -> list[BenchTask]:
    """Return all expanded tasks (excludes original datasets.py tasks)."""
    tasks: list[BenchTask] = []
    for fn, cat in [
        (multilingual_tasks, "multilingual"),
        (large_doc_tasks, "large_doc"),
        (edge_case_tasks, "edge_case"),
        (additional_json_tasks, "json"),
        (additional_markdown_tasks, "markdown"),
        (additional_code_tasks, "code"),
        (additional_text_tasks, "text"),
    ]:
        batch = fn()
        _register_categories(batch, cat)
        tasks.extend(batch)
    return tasks


def expanded_all_with_surfacing() -> list[BenchTask]:
    """Return all expanded tasks including surfacing-specific ones."""
    tasks = expanded_all_tasks()
    surf = additional_surfacing_tasks()
    _register_categories(surf, "surfacing")
    tasks.extend(surf)
    return tasks


def full_benchmark_suite() -> list[BenchTask]:
    """Return the complete benchmark suite: original + expanded datasets."""
    from .datasets import all_tasks_with_surfacing as original_all
    return [*original_all(), *expanded_all_with_surfacing()]


def full_category_map() -> dict[str, str]:
    """Return category map for all tasks (original + expanded)."""
    from .datasets import all_tasks_with_surfacing as original_all

    # Ensure expanded tasks are loaded (populates EXPANDED_CATEGORY_MAP)
    expanded_all_with_surfacing()

    # Add original task categories
    cat_map = dict(EXPANDED_CATEGORY_MAP)
    for t in original_all():
        if t.task_id.startswith("json-"):
            cat_map[t.task_id] = "json"
        elif t.task_id.startswith("md-"):
            cat_map[t.task_id] = "markdown"
        elif t.task_id.startswith("code-"):
            cat_map[t.task_id] = "code"
        elif t.task_id.startswith("text-"):
            cat_map[t.task_id] = "text"
        elif t.task_id.startswith("surf-"):
            cat_map[t.task_id] = "surfacing"
        else:
            cat_map[t.task_id] = "other"
    return cat_map

"""Benchmark datasets for STM proxy pipeline evaluation.

Structured by content type with realistic, production-grade data.
Each task has QA pairs for answer-based quality scoring.

Categories:
- json_tasks(): API responses, configs, event streams
- markdown_tasks(): technical docs, meeting notes, changelogs
- code_tasks(): Python ETL, TypeScript hooks
- text_tasks(): incident reports, research abstracts, legal clauses
- surfacing_tasks(): tasks requiring memory injection to fully answer
"""

from __future__ import annotations

from .harness import BenchTask, QAPair


# ═══════════════════════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════════════════════


def all_tasks() -> list[BenchTask]:
    """Return every benchmark task."""
    return [
        *json_tasks(),
        *markdown_tasks(),
        *code_tasks(),
        *text_tasks(),
    ]


def all_tasks_with_surfacing() -> list[BenchTask]:
    """Return all tasks including surfacing-specific ones."""
    return [*all_tasks(), *surfacing_tasks()]


# ═══════════════════════════════════════════════════════════════════════════
# JSON tasks
# ═══════════════════════════════════════════════════════════════════════════

_JSON_API_RESPONSE = """{
  "status": "ok",
  "metadata": {
    "request_id": "req-8a3f2c",
    "timestamp": "2025-06-15T09:22:11Z",
    "processing_ms": 142,
    "region": "us-east-1",
    "version": "3.4.1",
    "deprecated_fields": ["legacy_id", "compat_mode"],
    "rate_limit": {"remaining": 847, "reset_at": "2025-06-15T10:00:00Z"}
  },
  "data": {
    "users": [
      {
        "id": "u-001", "name": "Alice Park", "email": "alice@example.com",
        "role": "admin", "department": "Engineering", "joined": "2023-01-15",
        "last_login": "2025-06-14T18:30:00Z",
        "permissions": ["read", "write", "delete", "manage_users"],
        "preferences": {"theme": "dark", "locale": "en-US", "notifications": true},
        "tags": ["team-lead", "on-call"]
      },
      {
        "id": "u-002", "name": "Bob Chen", "email": "bob@example.com",
        "role": "developer", "department": "Engineering", "joined": "2023-08-20",
        "last_login": "2025-06-15T08:10:00Z",
        "permissions": ["read", "write"],
        "preferences": {"theme": "light", "locale": "zh-CN", "notifications": false},
        "tags": ["backend"]
      },
      {
        "id": "u-003", "name": "Carla Ruiz", "email": "carla@example.com",
        "role": "designer", "department": "Product", "joined": "2024-03-01",
        "last_login": "2025-06-13T11:45:00Z",
        "permissions": ["read"],
        "preferences": {"theme": "auto", "locale": "es-MX", "notifications": true},
        "tags": ["ux", "accessibility"]
      }
    ],
    "pagination": {"page": 1, "per_page": 25, "total": 3, "total_pages": 1}
  },
  "links": {
    "self": "/api/v3/users?page=1",
    "next": null,
    "docs": "https://docs.example.com/api/v3/users"
  }
}"""

_JSON_CONFIG = """{
  "app": {
    "name": "DataPipeline", "version": "2.1.0", "environment": "production",
    "debug": false, "log_level": "warn",
    "feature_flags": {"new_dashboard": true, "beta_export": false, "ai_suggestions": true, "legacy_compat": true}
  },
  "database": {
    "primary": {"host": "db-primary.internal", "port": 5432, "name": "pipeline_prod", "pool_size": 20, "ssl": true, "timeout_ms": 5000, "retry_count": 3},
    "replica": {"host": "db-replica.internal", "port": 5432, "name": "pipeline_prod", "pool_size": 10, "ssl": true, "read_only": true},
    "cache": {"engine": "redis", "host": "cache.internal", "port": 6379, "ttl_seconds": 300, "max_memory": "2gb"}
  },
  "integrations": {
    "slack": {"webhook_url": "https://hooks.slack.com/xxx", "channel": "#alerts", "enabled": true},
    "pagerduty": {"api_key": "pd-key-REDACTED", "severity_threshold": "critical"},
    "s3": {"bucket": "pipeline-artifacts", "region": "us-west-2", "prefix": "prod/"}
  },
  "scheduler": {
    "cron_jobs": [
      {"name": "daily_etl", "schedule": "0 2 * * *", "timeout_min": 120, "enabled": true},
      {"name": "hourly_sync", "schedule": "0 * * * *", "timeout_min": 15, "enabled": true},
      {"name": "weekly_report", "schedule": "0 8 * * 1", "timeout_min": 60, "enabled": false}
    ]
  }
}"""

_JSON_NESTED_EVENTS = """{
  "stream_id": "evt-stream-42",
  "events": [
    {
      "id": "e-001", "type": "order.created", "timestamp": "2025-06-15T10:00:00Z",
      "payload": {
        "order_id": "ORD-7890",
        "customer": {"id": "cust-555", "name": "Daniela Ferreira", "tier": "gold"},
        "items": [
          {"sku": "WIDGET-A", "qty": 3, "unit_price": 29.99, "subtotal": 89.97},
          {"sku": "GADGET-B", "qty": 1, "unit_price": 149.00, "subtotal": 149.00}
        ],
        "totals": {"subtotal": 238.97, "tax": 19.12, "shipping": 0.00, "grand_total": 258.09},
        "shipping_address": {"city": "São Paulo", "country": "BR", "zip": "01310-100"}
      }
    },
    {
      "id": "e-002", "type": "payment.captured", "timestamp": "2025-06-15T10:00:05Z",
      "payload": {"order_id": "ORD-7890", "payment_method": "credit_card", "amount": 258.09, "currency": "USD", "processor": "stripe", "transaction_id": "txn_abc123"}
    },
    {
      "id": "e-003", "type": "inventory.reserved", "timestamp": "2025-06-15T10:00:06Z",
      "payload": {"order_id": "ORD-7890", "reservations": [{"sku": "WIDGET-A", "warehouse": "WH-EAST", "qty": 3, "remaining_stock": 142}, {"sku": "GADGET-B", "warehouse": "WH-WEST", "qty": 1, "remaining_stock": 37}]}
    },
    {
      "id": "e-004", "type": "fulfillment.shipped", "timestamp": "2025-06-15T14:30:00Z",
      "payload": {"order_id": "ORD-7890", "carrier": "FedEx", "tracking_number": "FEDEX-998877", "estimated_delivery": "2025-06-18", "packages": [{"id": "pkg-1", "weight_kg": 1.2, "dimensions": "30x20x15cm", "items": ["WIDGET-A"]}, {"id": "pkg-2", "weight_kg": 2.5, "dimensions": "40x30x20cm", "items": ["GADGET-B"]}]}
    }
  ]
}"""


def json_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="json-api-users",
            description="API response with 3 users, metadata, and pagination",
            content=_JSON_API_RESPONSE,
            content_type="json",
            max_chars=400,
            expected_keywords=["Alice Park", "Bob Chen", "Carla Ruiz", "admin", "developer", "designer"],
            keyword_weights=[0.2, 0.2, 0.2, 0.1, 0.1, 0.1],
            qa_pairs=[
                QAPair("What role does Alice Park have?", "admin"),
                QAPair("Which department is Carla Ruiz in?", "Product"),
                QAPair("What locale does Bob Chen use?", "zh-CN"),
                QAPair("How many total users are there?", "3"),
            ],
        ),
        BenchTask(
            task_id="json-app-config",
            description="Application configuration with DB, integrations, and scheduler",
            content=_JSON_CONFIG,
            content_type="json",
            max_chars=500,
            expected_keywords=["DataPipeline", "5432", "redis", "daily_etl", "slack", "pagerduty"],
            qa_pairs=[
                QAPair("What is the primary DB pool size?", "20"),
                QAPair("What is the cache TTL?", "300"),
                QAPair("What S3 bucket is configured?", "pipeline-artifacts"),
                QAPair("Is the weekly_report cron job enabled?", "false"),
                QAPair("What environment is this config for?", "production"),
            ],
        ),
        BenchTask(
            task_id="json-event-stream",
            description="E-commerce event stream: order, payment, inventory, shipment",
            content=_JSON_NESTED_EVENTS,
            content_type="json",
            max_chars=500,
            expected_keywords=["ORD-7890", "258.09", "FedEx", "WIDGET-A", "GADGET-B"],
            qa_pairs=[
                QAPair("What is the order grand total?", "258.09"),
                QAPair("Who is the customer?", "Daniela Ferreira"),
                QAPair("What carrier shipped the order?", "FedEx"),
                QAPair("What tracking number was assigned?", "FEDEX-998877"),
                QAPair("How many WIDGET-A remain in stock?", "142"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Markdown tasks
# ═══════════════════════════════════════════════════════════════════════════

_MD_TECHNICAL_DOC = """# Vector Database Performance Guide

## Overview

This document covers best practices for deploying vector databases in production
environments with high-throughput requirements. We focus on three engines:
**Pinecone**, **Weaviate**, and **Qdrant**.

## Indexing Strategies

### HNSW (Hierarchical Navigable Small World)

HNSW is the default indexing algorithm in most vector databases.

- **M parameter**: Controls connectivity (recommended: 16-64)
- **ef_construction**: Build-time quality (recommended: 200+)
- **ef_search**: Query-time quality/speed tradeoff (recommended: 50-200)

Higher M values improve recall but increase memory usage by ~8 bytes per link.

### IVF (Inverted File Index)

IVF partitions the vector space into Voronoi cells.

- **nlist**: Number of cells (recommended: sqrt(N) to 4*sqrt(N))
- **nprobe**: Cells to search at query time (recommended: 5-20% of nlist)

IVF uses less memory than HNSW but has lower recall at the same latency.

## Benchmarks

| Engine   | Vectors  | Dims | Index  | QPS   | Recall@10 | p99 Latency |
|----------|----------|------|--------|-------|-----------|-------------|
| Pinecone | 10M      | 768  | HNSW   | 4,200 | 0.95      | 12ms        |
| Weaviate | 10M      | 768  | HNSW   | 3,800 | 0.93      | 15ms        |
| Qdrant   | 10M      | 768  | HNSW   | 5,100 | 0.96      | 9ms         |
| Pinecone | 10M      | 768  | IVF    | 6,500 | 0.88      | 8ms         |

## Memory Planning

Formula: `memory_gb = num_vectors * dimensions * 4 / 1e9 * overhead_factor`

- Raw vectors (10M x 768 x 4B) = **30.7 GB**
- HNSW overhead factor: 1.5-2.0x -> **46-61 GB**
- IVF overhead factor: 1.1-1.3x -> **34-40 GB**

## Production Checklist

1. [ ] Set up monitoring for recall degradation
2. [ ] Configure automatic index rebuilding thresholds
3. [ ] Implement request-level timeout (recommend: 100ms)
4. [ ] Enable WAL for crash recovery
5. [ ] Set memory limits to 80% of available RAM
6. [ ] Plan capacity for 2x current vector count

## Troubleshooting

### Recall drops below 0.90
- Increase ef_search (HNSW) or nprobe (IVF)
- Check if index needs rebuilding after large batch inserts
- Verify vector normalization is consistent

### Latency spikes
- Check garbage collection pauses
- Monitor disk I/O if vectors are memory-mapped
- Reduce batch size for concurrent writes
"""

_MD_MEETING_NOTES = """# Sprint 23 Retrospective — 2025-06-13

**Attendees:** Sarah Kim (PM), Marcus Johnson (TL), Priya Patel (BE),
Tomas Garcia (FE), Yuki Tanaka (QA), Jordan Lee (DevOps)

## What Went Well

- Deployment pipeline reduced from 45min to 12min after Jordan's
  optimization of the Docker layer caching
- Customer onboarding flow conversion up 18% (72% to 85%) after
  Tomas redesigned the stepper component
- Zero P0 incidents for the third consecutive sprint
- Priya's new caching layer cut API response times by 40%
  (p50: 230ms to 138ms, p99: 890ms to 534ms)

## What Needs Improvement

- Test coverage dropped from 87% to 79% — need to enforce coverage
  gates in CI before merging
- Design handoff still causing 2-3 day delays per feature. Sarah
  proposed moving to Figma Dev Mode + Storybook integration
- Sprint velocity was 34 points vs planned 42 — overcommitted on
  the search refactor epic

## Action Items

| Owner  | Action                                    | Due        |
|--------|-------------------------------------------|------------|
| Jordan | Add coverage gate (min 85%) to CI pipeline | 2025-06-20 |
| Sarah  | Schedule Figma Dev Mode training session  | 2025-06-18 |
| Marcus | Break down search refactor into smaller tickets | 2025-06-16 |
| Priya  | Document caching layer architecture in Notion | 2025-06-19 |
| Yuki   | Write regression tests for onboarding flow | 2025-06-20 |

## Decisions

- **Adopt**: Figma Dev Mode for all new design handoffs starting Sprint 24
- **Defer**: Kubernetes migration to Q4 (too risky during Q3 launch)
- **Drop**: Legacy XML export feature — only 2 customers using it

## Metrics This Sprint

- **Velocity**: 34/42 points (81%)
- **Bug rate**: 3 bugs per 100 story points (target: <5)
- **Deployment frequency**: 2.1/day (up from 1.4)
- **MTTR**: 22 minutes (target: <30)
"""

_MD_CHANGELOG = """# Changelog — DataPipeline v2.1.0

## [2.1.0] — 2025-06-10

### Added
- Real-time streaming mode for CDC (Change Data Capture) events
- Support for Apache Iceberg table format
- New `--dry-run` flag for all ETL commands
- GraphQL API for pipeline status queries
- Automatic schema evolution with backward compatibility checks
- Slack/PagerDuty alert integration for pipeline failures
- Custom transformation DSL with 23 built-in functions

### Changed
- Upgraded Spark runtime from 3.4 to 3.5.1
- Connection pooling now uses HikariCP (was: c3p0)
- Default batch size increased from 1,000 to 5,000 rows
- Retry logic now uses exponential backoff (base: 1s, max: 60s)
- Log format standardized to JSON structured logging

### Fixed
- Memory leak in Parquet writer when handling nullable arrays (#1247)
- Race condition in parallel partition pruning (#1302)
- Incorrect timezone handling for TIMESTAMP WITH TIME ZONE columns (#1289)
- Stale connection errors after database failover (#1315)
- CSV parser failing on embedded newlines within quoted fields (#1278)

### Security
- Patched CVE-2025-1234: SQL injection in dynamic filter expressions
- Added mTLS support for inter-service communication
- Secrets now encrypted at rest with AES-256-GCM

### Performance
- CDC latency reduced from 850ms to 120ms (p50)
- Bulk insert throughput: 45,000 to 78,000 rows/sec
- Memory footprint reduced 30% for wide-table schemas (100+ columns)
"""


def markdown_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="md-vector-db-guide",
            description="Technical guide on vector database performance tuning",
            content=_MD_TECHNICAL_DOC,
            content_type="markdown",
            max_chars=800,
            expected_keywords=["HNSW", "IVF", "Pinecone", "Weaviate", "Qdrant", "recall", "latency"],
            expect_headings=4,
            qa_pairs=[
                QAPair("Which engine has the highest QPS with HNSW?", "Qdrant"),
                QAPair("What is the recommended M parameter range?", "16-64"),
                QAPair("What is the raw memory for 10M 768-dim vectors?", "30.7 GB"),
                QAPair("What p99 latency does Qdrant achieve?", "9ms"),
            ],
        ),
        BenchTask(
            task_id="md-sprint-retro",
            description="Sprint retrospective meeting notes with action items",
            content=_MD_MEETING_NOTES,
            content_type="markdown",
            max_chars=600,
            expected_keywords=["Sarah", "Marcus", "Jordan", "Priya", "coverage", "velocity"],
            expect_headings=5,
            qa_pairs=[
                QAPair("What was the sprint velocity?", "34"),
                QAPair("What is the new deployment time?", "12min"),
                QAPair("What is the onboarding conversion rate after redesign?", "85%"),
                QAPair("What is the MTTR this sprint?", "22 minutes"),
                QAPair("When is the Kubernetes migration deferred to?", "Q4"),
            ],
        ),
        BenchTask(
            task_id="md-changelog",
            description="Software changelog with added/changed/fixed/security sections",
            content=_MD_CHANGELOG,
            content_type="markdown",
            max_chars=600,
            expected_keywords=["CDC", "Iceberg", "HikariCP", "CVE-2025-1234", "Parquet", "mTLS"],
            expect_headings=6,
            qa_pairs=[
                QAPair("What Spark version was upgraded to?", "3.5.1"),
                QAPair("What is the new CDC p50 latency?", "120ms"),
                QAPair("What bulk insert throughput was achieved?", "78,000"),
                QAPair("What CVE was patched?", "CVE-2025-1234"),
                QAPair("What encryption is used for secrets at rest?", "AES-256-GCM"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Code tasks
# ═══════════════════════════════════════════════════════════════════════════

_CODE_PYTHON_ETL = '''"""ETL pipeline for user activity data."""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

@dataclass
class ETLConfig:
    source_dsn: str
    target_dsn: str
    batch_size: int = 5000
    lookback_days: int = 7
    max_retries: int = 3
    timeout_seconds: int = 300

class UserActivityETL:
    EXTRACT_QUERY = """
        SELECT user_id, event_type, event_timestamp, metadata
        FROM raw_events
        WHERE event_timestamp >= :start_date AND event_timestamp < :end_date
        ORDER BY event_timestamp
    """

    def __init__(self, config: ETLConfig) -> None:
        self.config = config
        self._source: Engine = create_engine(config.source_dsn)
        self._target: Engine = create_engine(config.target_dsn)

    def extract(self, start: datetime, end: datetime) -> pd.DataFrame:
        logger.info("Extracting events from %s to %s", start, end)
        with self._source.connect() as conn:
            df = pd.read_sql(text(self.EXTRACT_QUERY), conn, params={"start_date": start, "end_date": end})
        logger.info("Extracted %d rows", len(df))
        return df

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["event_timestamp"]).dt.date
        aggregated = df.groupby(["user_id", "date", "event_type"]).size().reset_index(name="event_count")
        pivoted = aggregated.pivot_table(index=["user_id", "date"], columns="event_type", values="event_count", fill_value=0).reset_index()
        pivoted.columns = [f"count_{c}" if c not in ("user_id", "date") else c for c in pivoted.columns]
        # Compute session duration from login/logout pairs
        sessions = df[df["event_type"].isin(["login", "logout"])].copy()
        if not sessions.empty:
            sessions = sessions.sort_values(["user_id", "event_timestamp"])
            sessions["next_event"] = sessions.groupby("user_id")["event_type"].shift(-1)
            sessions["next_ts"] = sessions.groupby("user_id")["event_timestamp"].shift(-1)
            valid = sessions[(sessions["event_type"] == "login") & (sessions["next_event"] == "logout")]
            valid["duration_min"] = (pd.to_datetime(valid["next_ts"]) - pd.to_datetime(valid["event_timestamp"])).dt.total_seconds() / 60
            daily = valid.groupby(["user_id", sessions["event_timestamp"].dt.date])["duration_min"].mean().reset_index()
            daily.columns = ["user_id", "date", "avg_session_min"]
            pivoted = pivoted.merge(daily, on=["user_id", "date"], how="left")
        pivoted["processed_at"] = datetime.utcnow()
        return pivoted

    def load(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        rows = len(df)
        df.to_sql("user_daily_metrics", self._target, if_exists="append", index=False, method="multi", chunksize=self.config.batch_size)
        logger.info("Loaded %d rows into user_daily_metrics", rows)
        return rows

    def run(self, target_date: datetime | None = None) -> dict:
        if target_date is None:
            target_date = datetime.utcnow() - timedelta(days=1)
        start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        raw = self.extract(start, end)
        transformed = self.transform(raw)
        loaded = self.load(transformed)
        return {"date": start.isoformat(), "extracted": len(raw), "transformed": len(transformed), "loaded": loaded}

    def backfill(self, days: int | None = None) -> list[dict]:
        days = days or self.config.lookback_days
        return [self.run(datetime.utcnow() - timedelta(days=i + 1)) for i in range(days)]
'''

_CODE_TYPESCRIPT_HOOKS = '''// React custom hooks for data fetching with caching and retry
import { useState, useEffect, useCallback, useRef } from "react";

interface FetchOptions {
  retries?: number;
  retryDelay?: number;
  cacheKey?: string;
  cacheTTL?: number;
  timeout?: number;
  onError?: (error: Error) => void;
}

interface FetchResult<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refetch: () => Promise<void>;
  isStale: boolean;
}

const cache = new Map<string, { data: unknown; timestamp: number }>();

export function useFetch<T>(url: string, options: FetchOptions = {}): FetchResult<T> {
  const { retries = 3, retryDelay = 1000, cacheKey, cacheTTL = 60_000, timeout = 10_000, onError } = options;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [isStale, setIsStale] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const fetchData = useCallback(async () => {
    const key = cacheKey || url;
    const entry = cache.get(key);
    if (entry && Date.now() - entry.timestamp < cacheTTL) {
      setData(entry.data as T);
      setLoading(false);
      setIsStale(Date.now() - entry.timestamp > cacheTTL * 0.8);
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const timeoutId = setTimeout(() => controller.abort(), timeout);
    setLoading(true);
    setError(null);
    let lastError: Error | null = null;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        const result: T = await response.json();
        cache.set(key, { data: result, timestamp: Date.now() });
        setData(result);
        setError(null);
        setIsStale(false);
        clearTimeout(timeoutId);
        return;
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
        if (controller.signal.aborted) break;
        if (attempt < retries) await new Promise(r => setTimeout(r, retryDelay * Math.pow(2, attempt)));
      }
    }
    clearTimeout(timeoutId);
    setError(lastError);
    onError?.(lastError!);
  }, [url, retries, retryDelay, cacheKey, cacheTTL, timeout, onError]);

  useEffect(() => { fetchData(); return () => abortRef.current?.abort(); }, [fetchData]);
  return { data, loading, error, refetch: fetchData, isStale };
}

export function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => { const t = setTimeout(() => setDebounced(value), delay); return () => clearTimeout(t); }, [value, delay]);
  return debounced;
}
'''


def code_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="code-python-etl",
            description="Python ETL pipeline class with extract/transform/load methods",
            content=_CODE_PYTHON_ETL,
            content_type="code",
            max_chars=800,
            expected_keywords=["ETLConfig", "UserActivityETL", "extract", "transform", "load", "backfill"],
            expect_code_blocks=1,
            qa_pairs=[
                QAPair("What is the default batch size?", "5000"),
                QAPair("What table does the load method write to?", "user_daily_metrics"),
                QAPair("How is session duration computed?", "login/logout"),
                QAPair("What does the backfill method do?", "multiple days"),
            ],
        ),
        BenchTask(
            task_id="code-ts-hooks",
            description="TypeScript React hooks for data fetching with cache and retry",
            content=_CODE_TYPESCRIPT_HOOKS,
            content_type="code",
            max_chars=700,
            expected_keywords=["useFetch", "useDebounce", "cache", "retry"],
            expect_code_blocks=1,
            qa_pairs=[
                QAPair("What is the default retry count?", "3"),
                QAPair("What is the default cache TTL?", "60_000"),
                QAPair("What is the default timeout?", "10_000"),
                QAPair("What backoff strategy is used?", "exponential"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Plain text tasks
# ═══════════════════════════════════════════════════════════════════════════

_TEXT_INCIDENT_REPORT = """INCIDENT REPORT — INC-2025-0613

Severity: P1 (Critical)
Duration: 2025-06-13 03:42 UTC — 2025-06-13 05:17 UTC (95 minutes)
Impact: 100% of payments API requests failing, affecting approximately 14,200 customers
Services affected: payment-gateway, order-service, checkout-ui

TIMELINE

03:42 — Automated alert fires: payment-gateway error rate exceeds 50%. PagerDuty pages on-call engineer (Jordan Lee).
03:47 — Jordan acknowledges alert. Initial investigation shows connection pool exhaustion to payment-db-primary.
03:55 — Jordan identifies root cause: a schema migration deployed at 03:30 added a new index on the transactions table. The migration acquired an exclusive lock, blocking all concurrent queries.
04:05 — Rollback attempted but blocked by the long-running lock. Jordan escalates to database team.
04:15 — Database lead (Marcus Johnson) joins. Decision made to kill the migration process rather than wait.
04:22 — Migration process terminated. However, connection pool remains exhausted due to 800+ queued connections.
04:30 — Jordan restarts payment-gateway pods (rolling restart, 3 pods, 2 min each).
04:50 — All pods restarted. Error rate at 12% and falling.
05:17 — Monitoring confirms stable state. Incident resolved.

ROOT CAUSE

The deployment pipeline ran a database migration (adding a B-tree index on transactions.merchant_id) during peak hours. The CREATE INDEX statement acquired an ACCESS EXCLUSIVE lock, blocking all SELECT/INSERT operations. With a table size of 2.3TB and approximately 890M rows, the index creation was estimated to take 45+ minutes.

REMEDIATION

Immediate:
- [DONE] Killed migration, restarted services
- [DONE] Manually created the index using CREATE INDEX CONCURRENTLY

Preventive:
- [TODO] Add lock_timeout = 5s for all migration connections — owner: Marcus, due: 2025-06-20
- [TODO] CI check that flags ACCESS EXCLUSIVE migrations — owner: Jordan, due: 2025-06-25
- [TODO] Add connection pool circuit breaker (max queue: 50, timeout: 5s) — owner: Priya, due: 2025-06-27
"""

_TEXT_RESEARCH_ABSTRACT = """Scaling Laws for Sparse Mixture-of-Experts Language Models

We investigate the compute-optimal training of Sparse Mixture-of-Experts (SMoE) transformer language models. Our study spans models from 125M to 52B total parameters with 8 to 128 experts, trained on 200B to 2T tokens.

(1) Loss scales as L(N,E,D) = aN^(-0.076) x E^(-0.021) x D^(-0.095) + e, where N is active parameters, E is number of experts, and D is dataset size. The expert count exponent (0.021) is notably smaller than the active parameter exponent (0.076).

(2) The compute-optimal expert count follows E_opt proportional to N^(0.34). For a 7B-active-parameter model, this predicts 16-32 experts as optimal.

(3) Expert utilization efficiency decreases: U(E) = 1 - 0.12 x log2(E). At 128 experts, utilization drops to 16%.

(4) Load balancing loss costs 0.3-0.8% in final loss. Our "Adaptive Balance" scheduling reduces this gap to 0.05%.

For a fixed compute budget C: N_opt proportional to C^(0.41), E_opt proportional to C^(0.14), D_opt proportional to C^(0.45). Compute should go primarily to more data (45%), then more parameters (41%), with only 14% to additional experts.
"""

_TEXT_LEGAL_CLAUSE = """MASTER SERVICE AGREEMENT — DATA PROCESSING ADDENDUM

7.1 Definitions: "Personal Data" means info relating to identified natural persons under Data Protection Laws; "Processing" includes collection, storage, modification, retrieval, disclosure, erasure; "Data Controller" = Client; "Data Processor" = Service Provider.

7.2 Processing Scope: Only for Services described in Master Agreement; per Controller's documented instructions; compliant with GDPR (EU 2016/679), CCPA (Cal. Civ. Code 1798.100), and LGPD (Lei 13.709/2018).

7.3 Security: encryption in transit (TLS 1.3 min) and at rest (AES-256); MFA access controls; quarterly pen tests; 72-hour breach notification; RPO 1 hour, RTO 4 hours.

7.4 Sub-processors: maintain current list at Annex B URL; 30 days notice before changes; Sub-processors bound by same obligations.

7.5 Data Subject Rights: assist Controller within 5 business days for access, rectification, erasure, portability requests.

7.6 Breach Notification: notify Controller within 24 hours; provide nature, affected count, consequences, and measures taken.

7.7 Retention: return data in machine-readable format within 30 days of termination; delete all copies within 90 days; provide deletion certification.

7.8 Audit: 30 days notice, once per year, during business hours, at Controller's expense.

7.9 International Transfers: only to adequate countries per EC decision, or with Standard Contractual Clauses (Decision 2021/914).

7.10 Liability: cap at greater of 12-month fees or EUR 500,000. No cap for willful misconduct or breach notification violations.
"""


def text_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            task_id="text-incident-report",
            description="P1 incident report with timeline, root cause, and remediation",
            content=_TEXT_INCIDENT_REPORT,
            content_type="text",
            max_chars=800,
            expected_keywords=["INC-2025-0613", "payment-gateway", "schema migration", "ACCESS EXCLUSIVE", "Jordan", "Marcus"],
            qa_pairs=[
                QAPair("How long was the incident?", "95 minutes"),
                QAPair("How many customers were affected?", "14,200"),
                QAPair("What was the root cause?", "schema migration"),
                QAPair("What lock type blocked queries?", "ACCESS EXCLUSIVE"),
                QAPair("What is the table size?", "2.3TB"),
            ],
        ),
        BenchTask(
            task_id="text-research-abstract",
            description="ML research abstract on scaling laws for MoE models",
            content=_TEXT_RESEARCH_ABSTRACT,
            content_type="text",
            max_chars=500,
            expected_keywords=["SMoE", "scaling", "experts", "compute-optimal", "utilization"],
            qa_pairs=[
                QAPair("What is the expert count exponent?", "0.021"),
                QAPair("What is expert utilization at 128 experts?", "16%"),
                QAPair("What fraction of compute should go to data?", "45%"),
                QAPair("How many experts are optimal for 7B params?", "16-32"),
            ],
        ),
        BenchTask(
            task_id="text-legal-dpa",
            description="Data Processing Addendum with security, breach, and transfer clauses",
            content=_TEXT_LEGAL_CLAUSE,
            content_type="text",
            max_chars=700,
            expected_keywords=["GDPR", "CCPA", "LGPD", "AES-256", "TLS 1.3", "Sub-processor"],
            qa_pairs=[
                QAPair("What is the breach notification deadline?", "24 hours"),
                QAPair("What is the RPO requirement?", "1 hour"),
                QAPair("What is the RTO requirement?", "4 hours"),
                QAPair("What is the data deletion deadline?", "90 days"),
                QAPair("What is the liability cap?", "EUR 500,000"),
            ],
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Surfacing-specific tasks (memories fill knowledge gaps)
# ═══════════════════════════════════════════════════════════════════════════


def surfacing_tasks() -> list[BenchTask]:
    """Tasks where response is incomplete — memories should fill gaps."""
    return [
        BenchTask(
            task_id="surf-api-with-context",
            description="API response about users, needs org context from memory",
            content=_JSON_API_RESPONSE,
            content_type="json",
            max_chars=400,
            expected_keywords=["Alice Park", "Bob Chen", "Carla Ruiz"],
            surfacing_memories=[
                "Alice Park was promoted to VP of Engineering on 2025-05-01.",
                "The Engineering team is migrating from Python to Rust for core services.",
                "Bob Chen is leading the Rust migration project.",
            ],
            qa_pairs=[
                QAPair("What role does Alice Park have?", "admin", source="content"),
                QAPair("When was Alice promoted to VP?", "2025-05-01", source="memory"),
                QAPair("What language is the team migrating to?", "Rust", source="memory"),
                QAPair("Who leads the Rust migration?", "Bob Chen", source="memory"),
            ],
        ),
        BenchTask(
            task_id="surf-incident-with-history",
            description="Incident report, needs previous incident context from memory",
            content=_TEXT_INCIDENT_REPORT,
            content_type="text",
            max_chars=800,
            expected_keywords=["payment-gateway", "schema migration"],
            surfacing_memories=[
                "INC-2025-0501 was a similar P1 caused by a long-running migration on the orders table.",
                "After INC-2025-0501, the team agreed to use CREATE INDEX CONCURRENTLY for all production indexes.",
                "The payment-gateway service was refactored in Q1 2025 to add circuit breaker support, but it was never enabled in production.",
            ],
            qa_pairs=[
                QAPair("What was the root cause?", "schema migration", source="content"),
                QAPair("Was there a similar previous incident?", "INC-2025-0501", source="memory"),
                QAPair("Was circuit breaker support added?", "Q1 2025", source="memory"),
                QAPair("Was the circuit breaker enabled?", "never enabled", source="memory"),
            ],
        ),
    ]

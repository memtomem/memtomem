"""Benchmark: query-aware vs baseline truncation quality comparison.

Includes stress-test tasks with tight budgets (25-35%) and many sections
to validate that query-aware allocation makes a measurable difference.

    uv run pytest packages/memtomem-stm/tests/test_query_aware_bench.py -v -s
"""

from __future__ import annotations

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import TruncateCompressor

from bench.datasets_expanded import full_benchmark_suite
from bench.harness import BenchHarness, BenchTask, QAPair
from bench.judge import RuleBasedJudge


# ── Stress-test tasks: tight budget + many sections + targeted query ──


def _make_section(heading: str, lines: list[str], pad_count: int = 15) -> str:
    """Build a section with unique content lines followed by padding."""
    padding = [f"Additional {heading.lower()} details and metrics collected."] * pad_count
    return f"## {heading}\n\n" + "\n".join(lines + padding)


def _stress_tasks() -> list[BenchTask]:
    """Tasks designed to stress query-aware allocation.

    Key design constraints for meaningful comparison:
    - Total content 12KB+ so retention caps at 50% (not 65-90%)
    - QA answers placed on lines 3-8 of sections (not first line)
    - 8-10 sections so budget forces real tradeoffs
    - Query targets a specific section in the middle/end
    """

    # Task 1: Server monitoring (10 sections, ~12KB, budget = 4000 chars)
    # Query targets Redis (section 5/10). Key answers buried in lines 3-8.
    monitoring = "\n\n".join(
        [
            _make_section(
                "CPU Usage",
                [
                    "Overall CPU utilization within acceptable parameters.",
                    "User-space processes consuming most cycles.",
                    "cpu_idle=92.3% cpu_user=5.1% cpu_system=2.6%",
                    "load_average_1m=1.2 load_average_5m=0.9 load_average_15m=0.7",
                    "context_switches=45000/s interrupts=12000/s",
                ],
                20,
            ),
            _make_section(
                "Memory",
                [
                    "Memory allocation stable with low swap usage.",
                    "Kernel page cache consuming expected portion.",
                    "mem_total=64GB mem_used=41GB mem_free=11GB",
                    "mem_cached=12GB mem_buffers=1.8GB swap_used=0.2GB",
                    "huge_pages_total=0 transparent_hugepage=madvise",
                ],
                20,
            ),
            _make_section(
                "Disk IO",
                [
                    "Disk throughput within provisioned IOPS limits.",
                    "Write amplification factor acceptable for workload.",
                    "disk_read=120MB/s disk_write=45MB/s",
                    "iops_read=3200 iops_write=890 await_ms=2.1",
                    "disk_util=34% queue_depth=4",
                ],
                20,
            ),
            _make_section(
                "Network",
                [
                    "Network bandwidth utilization at 40% capacity.",
                    "No packet drops detected on primary interface.",
                    "rx_bytes=1.2GB/s tx_bytes=800MB/s",
                    "tcp_connections=4521 udp_sockets=89 dropped=0",
                    "retransmit_rate=0.02% mtu=9001",
                ],
                20,
            ),
            _make_section(
                "Redis Cache",
                [
                    "Redis cache performance showing degradation pattern.",
                    "Hit rate has dropped 15% over the past 24 hours.",
                    "Current eviction rate exceeding baseline by 340%.",
                    "redis_hit_rate=73.2% redis_miss_rate=26.8%",
                    "evictions=1420/min baseline_evictions=320/min",
                    "redis_memory=8.2GB/16GB keyspace_hits=142000 keyspace_misses=52000",
                    "connected_clients=89 blocked_clients=3 rejected_connections=12",
                    "expired_keys=8900 maxmemory_policy=allkeys-lru fragmentation_ratio=1.08",
                    "rdb_last_save=2025-06-15T08:00:00Z aof_enabled=yes replication_lag=0ms",
                    "slowlog_entries=7 avg_latency_ms=0.8 p99_latency_ms=4.2",
                    "cluster_enabled=yes cluster_slots_ok=16384 cluster_nodes=6",
                    "pubsub_channels=23 pubsub_patterns=5 lua_scripts_cached=12",
                    "hot_keys: session:user:* (42%), cache:product:* (28%)",
                    "recommendation: increase maxmemory to 24GB or review TTL policy",
                ],
                12,
            ),
            _make_section(
                "PostgreSQL",
                [
                    "Database connection pool healthy.",
                    "Query cache hit ratio excellent.",
                    "pg_connections=45/100 pg_locks=3 pg_deadlocks=0",
                    "cache_hit_ratio=98.5% index_scan_ratio=99.2%",
                    "bloat_ratio=4.2% vacuum_last=2025-06-15T06:00:00Z",
                ],
                20,
            ),
            _make_section(
                "API Latency",
                [
                    "API response times within SLO bounds.",
                    "P99 elevated but below alerting threshold.",
                    "api_p50=42ms api_p95=180ms api_p99=450ms",
                    "error_rate=0.3% timeout_rate=0.01%",
                    "requests_per_second=2400 peak_rps=3100",
                ],
                20,
            ),
            _make_section(
                "Worker Processes",
                [
                    "Background worker pool running at 75% capacity.",
                    "Job failure rate within acceptable range.",
                    "workers_active=12/16 jobs_queued=234 jobs_failed=2",
                    "avg_duration=3.2s max_duration=45s",
                    "retry_queue=8 dead_letter=3",
                ],
                20,
            ),
            _make_section(
                "Message Queues",
                [
                    "Message queue depth elevated but consumers keeping pace.",
                    "Dead letter queue requires manual review.",
                    "queue_depth=1200 consumer_lag=45s dlq_size=23",
                    "throughput=500msg/s publish_rate=520msg/s",
                    "oldest_message_age=180s",
                ],
                20,
            ),
            _make_section(
                "Error Logs",
                [
                    "Error rate stable with no anomalous patterns.",
                    "5xx errors primarily from upstream timeout.",
                    "errors_5xx=12/min errors_4xx=89/min",
                    "timeout_errors=3/min oom_kills=0",
                    "top_error: ConnectionTimeout to payment-service (8/min)",
                ],
                20,
            ),
        ]
    )

    # Task 2: Architecture decision (8 sections, ~12KB, budget = 3500 chars)
    # Query targets Security Review (section 4/8). Answers on lines 4-8.
    adr = "\n\n".join(
        [
            _make_section(
                "Problem Statement",
                [
                    "Current auth system uses session cookies stored in-memory.",
                    "System cannot scale beyond 4 application instances.",
                    "Scaling beyond 4 instances causes session loss on rebalance.",
                    "User reports indicate 12% session drop rate during deploys.",
                    "Average recovery time after session loss is 8 minutes.",
                ],
                20,
            ),
            _make_section(
                "Options Considered",
                [
                    "Three options were evaluated by the infrastructure team.",
                    "Each option was scored on reliability, cost, and complexity.",
                    "Option A: Sticky sessions via load balancer affinity.",
                    "Option B: Redis session store with encryption at rest.",
                    "Option C: JWT stateless tokens with refresh rotation.",
                ],
                20,
            ),
            _make_section(
                "Performance Analysis",
                [
                    "Load testing conducted with 10K concurrent users.",
                    "Each option tested over 72-hour sustained load window.",
                    "Sticky sessions: no additional latency, but uneven load distribution.",
                    "Redis store: adds 2-5ms per request, perfectly even distribution.",
                    "JWT: no server-side latency, but token size adds 1.2KB per request.",
                ],
                20,
            ),
            _make_section(
                "Security Review",
                [
                    "Security team conducted review on 2025-06-10.",
                    "All session data classified as PII under GDPR Article 4.",
                    "Following controls were mandated by security review.",
                    "Session tokens must be encrypted using AES-256-GCM algorithm.",
                    "TLS 1.3 required for all Redis connections, no exceptions.",
                    "Key rotation mandated every 90 days with automated rotation.",
                    "OWASP session management: httpOnly, secure, SameSite=Strict.",
                    "Penetration test scheduled for 2025-07-01 by SecureAudit Inc.",
                    "Session fixation protection via token regeneration on login.",
                    "Brute force protection: 5 failed attempts triggers 30-min lockout.",
                ],
                15,
            ),
            _make_section(
                "Cost Estimate",
                [
                    "Costs calculated for 12-month commitment pricing.",
                    "All estimates include monitoring and backup overhead.",
                    "Redis cluster: $450/month for 3-node HA setup.",
                    "Monitoring: $50/month additional for Redis-specific dashboards.",
                    "Total annual cost: $6000 including support contract.",
                ],
                20,
            ),
            _make_section(
                "Migration Plan",
                [
                    "Migration planned across 4 sprints with feature flags.",
                    "Zero-downtime migration using dual-write pattern.",
                    "Phase 1: Deploy Redis cluster alongside existing system.",
                    "Phase 2: Dual-write sessions to both stores for 2 weeks.",
                    "Phase 3: Switch reads to Redis, keep in-memory as fallback.",
                    "Phase 4: Decommission in-memory store after 30-day soak.",
                ],
                20,
            ),
            _make_section(
                "Rollback Strategy",
                [
                    "Rollback plan tested in staging environment.",
                    "RTO target: 5 minutes for full rollback.",
                    "Feature flag 'use_redis_sessions' controls routing.",
                    "Rollback: disable flag, sessions fall back to in-memory.",
                    "Data sync: in-memory store warm from dual-write period.",
                ],
                20,
            ),
            _make_section(
                "Decision",
                [
                    "Decision made in architecture review board meeting.",
                    "Unanimous approval from 5 board members.",
                    "Chosen: Option B (Redis session store) with AES-256-GCM.",
                    "Rationale: horizontal scaling requirement outweighs latency cost.",
                    "Implementation target: Sprint 24 (2025-07-14 start).",
                ],
                20,
            ),
        ]
    )

    # Task 3: API reference (10 sections, ~12KB, budget = 3000 chars)
    # Query targets permissions endpoint (section 6/10). Answers on lines 3-7.
    api_ref = "\n\n".join(
        [
            _make_section(
                "GET /users",
                [
                    "Returns paginated list of users from the directory.",
                    "Supports filtering by role, department, and status.",
                    "Default page size is 25 users, maximum 100.",
                    "Response includes total count for pagination UI.",
                ],
                22,
            ),
            _make_section(
                "POST /users",
                [
                    "Creates a new user account in the directory.",
                    "Requires admin role or user-management scope.",
                    "Body must include name, email, role, department.",
                    "Returns 201 with created user object and Location header.",
                ],
                22,
            ),
            _make_section(
                "GET /users/{id}",
                [
                    "Returns single user by ID with full profile data.",
                    "Includes nested preferences and permission objects.",
                    "Supports ?fields= query parameter for sparse response.",
                    "Returns 404 if user ID does not exist or is deleted.",
                ],
                22,
            ),
            _make_section(
                "PUT /users/{id}",
                [
                    "Full replacement update of user record.",
                    "Requires admin role or self-update with limited fields.",
                    "All required fields must be present in request body.",
                    "Returns 200 with updated user, 409 on concurrent edit.",
                ],
                22,
            ),
            _make_section(
                "DELETE /users/{id}",
                [
                    "Soft-deletes user by setting status to inactive.",
                    "Hard delete only available via admin CLI tool.",
                    "Associated sessions are immediately invalidated.",
                    "Returns 204 on success, 403 if not admin.",
                ],
                22,
            ),
            _make_section(
                "GET /users/{id}/permissions",
                [
                    "Returns computed effective permissions for a user.",
                    "Combines direct, group, and organization-level grants.",
                    "Effective permission calculation follows inheritance chain.",
                    "Permission inheritance order: user < group < org.",
                    "Override mechanism: explicit deny rules take precedence.",
                    "Response includes permission_source field for each grant.",
                    "permission_source values: direct, group, org, deny.",
                    "Supports ?resource= filter for resource-specific permissions.",
                    "Rate limited to 100 requests per minute per user.",
                ],
                15,
            ),
            _make_section(
                "POST /auth/login",
                [
                    "Authenticates user with email and password credentials.",
                    "Returns JWT access token (15min) and refresh token (7d).",
                    "Access token contains user ID, role, and scope claims.",
                    "Failed login increments brute-force counter.",
                ],
                22,
            ),
            _make_section(
                "POST /auth/refresh",
                [
                    "Refreshes expired access token using valid refresh token.",
                    "Both tokens are rotated on each refresh call.",
                    "Old refresh token is immediately invalidated.",
                    "Returns 401 if refresh token expired or revoked.",
                ],
                22,
            ),
            _make_section(
                "GET /audit/logs",
                [
                    "Returns audit trail of all user and system actions.",
                    "Filters: user_id, action, date_range, resource, ip.",
                    "Default retention: 90 days, extended: 2 years (compliance).",
                    "Supports CSV export via Accept: text/csv header.",
                ],
                22,
            ),
            _make_section(
                "POST /webhooks",
                [
                    "Registers a webhook endpoint for event notifications.",
                    "Events: user.created, user.updated, user.deleted, auth.login.",
                    "Supports HMAC-SHA256 signature verification.",
                    "Retry policy: 3 attempts with exponential backoff.",
                ],
                22,
            ),
        ]
    )

    return [
        BenchTask(
            task_id="stress-monitoring-redis",
            description="Server monitoring 10 sections, query targets Redis",
            content=monitoring,
            content_type="markdown",
            max_chars=4000,
            expected_keywords=[
                "evictions",
                "1420/min",
                "keyspace_misses",
                "52000",
                "maxmemory_policy",
                "allkeys-lru",
                "fragmentation_ratio",
                "cluster_nodes",
                "p99_latency_ms",
                "cpu_idle",
                "pg_connections",
            ],
            keyword_weights=[1.0, 1.0, 1.0, 1.0, 0.8, 0.8, 0.8, 0.8, 0.8, 0.3, 0.3],
            expect_headings=8,
            qa_pairs=[
                QAPair("Redis eviction rate는?", "1420/min"),
                QAPair("Redis keyspace miss 수는?", "52000"),
                QAPair("maxmemory policy는?", "allkeys-lru"),
                QAPair("Redis fragmentation ratio는?", "1.08"),
                QAPair("Redis cluster node 수는?", "6"),
                QAPair("Redis p99 latency는?", "4.2"),
                QAPair("hot key 패턴은?", "session:user:*"),
                QAPair("recommendation은?", "increase maxmemory to 24GB"),
            ],
            context_query="Redis cache hit rate low eviction memory",
        ),
        BenchTask(
            task_id="stress-adr-security",
            description="Architecture decision record, query targets security",
            content=adr,
            content_type="markdown",
            max_chars=3500,
            expected_keywords=[
                "AES-256-GCM",
                "TLS 1.3",
                "90 days",
                "OWASP",
                "SecureAudit",
                "session fixation",
                "brute force",
                "$450/month",
                "Option B",
            ],
            keyword_weights=[1.0, 1.0, 1.0, 1.0, 0.8, 0.8, 0.8, 0.3, 0.3],
            expect_headings=6,
            qa_pairs=[
                QAPair("암호화 알고리즘은?", "AES-256-GCM"),
                QAPair("TLS 버전 요구사항은?", "TLS 1.3"),
                QAPair("key rotation 주기는?", "90 days"),
                QAPair("penetration test 업체는?", "SecureAudit"),
                QAPair("session fixation 방어는?", "token regeneration"),
                QAPair("brute force 제한은?", "5 failed attempts"),
                QAPair("lockout 시간은?", "30-min"),
            ],
            context_query="security encryption TLS OWASP penetration test",
        ),
        BenchTask(
            task_id="stress-api-permissions",
            description="API reference 10 endpoints, query targets permissions",
            content=api_ref,
            content_type="markdown",
            max_chars=3000,
            expected_keywords=[
                "permission_source",
                "user < group < org",
                "deny rules",
                "deny",
                "direct, group, org",
                "JWT",
                "audit",
                "webhook",
            ],
            keyword_weights=[1.0, 1.0, 1.0, 1.0, 0.8, 0.3, 0.3, 0.3],
            expect_headings=8,
            qa_pairs=[
                QAPair("permission inheritance 순서는?", "user < group < org"),
                QAPair("permission_source 값들은?", "direct, group, org, deny"),
                QAPair("override 메커니즘은?", "deny rules"),
                QAPair("permission rate limit는?", "100 requests per minute"),
                QAPair("login token 유효시간은?", "15min"),
                QAPair("audit log 기본 retention은?", "90 days"),
            ],
            context_query="user permissions inheritance override deny",
        ),
    ]


class TestQueryAwareBench:
    def test_query_aware_vs_baseline(self, capsys):
        """Compare quality: truncate (no query) vs truncate (with query)."""
        harness = BenchHarness(
            cleaner=DefaultContentCleaner(),
            compressor=TruncateCompressor(),
            judge=RuleBasedJudge(),
        )
        tasks = full_benchmark_suite()
        tasks = [t for t in tasks if t.context_query and len(t.content) > t.max_chars]

        baseline_scores: list[float] = []
        query_scores: list[float] = []
        improvements: list[tuple[str, float, float]] = []

        for t in tasks:
            report = harness.run_query_aware_comparison(t)
            baseline_scores.append(report.direct.quality_score)
            query_scores.append(report.stm.quality_score)
            improvements.append((t.task_id, report.direct.quality_score, report.stm.quality_score))

        print("\n" + "=" * 70)
        print("QUERY-AWARE vs BASELINE — EXISTING TASKS")
        print("=" * 70)
        _print_report(improvements, baseline_scores, query_scores)

        assert sum(query_scores) >= sum(baseline_scores) - len(query_scores) * 0.5

    def test_stress_tight_budget(self, capsys):
        """Stress test: tight budget (25-30%) + many sections + targeted query."""
        harness = BenchHarness(
            cleaner=DefaultContentCleaner(),
            compressor=TruncateCompressor(),
            judge=RuleBasedJudge(),
        )
        tasks = _stress_tasks()

        baseline_scores: list[float] = []
        query_scores: list[float] = []
        improvements: list[tuple[str, float, float]] = []

        for t in tasks:
            report = harness.run_query_aware_comparison(t)
            baseline_scores.append(report.direct.quality_score)
            query_scores.append(report.stm.quality_score)
            improvements.append((t.task_id, report.direct.quality_score, report.stm.quality_score))

        print("\n" + "=" * 70)
        print("QUERY-AWARE vs BASELINE — STRESS TASKS (tight budget)")
        print("=" * 70)
        _print_report(improvements, baseline_scores, query_scores)

        avg_baseline = sum(baseline_scores) / len(baseline_scores)
        avg_query = sum(query_scores) / len(query_scores)

        # Query-aware should improve or match on tight-budget tasks
        assert avg_query >= avg_baseline, (
            f"Query-aware ({avg_query:.2f}) should be >= baseline ({avg_baseline:.2f}) "
            f"on tight-budget tasks"
        )

    def test_stress_qa_score_detail(self, capsys):
        """Per-task QA pair analysis: which questions are better answered."""
        harness = BenchHarness(
            cleaner=DefaultContentCleaner(),
            compressor=TruncateCompressor(),
            judge=RuleBasedJudge(),
        )

        for t in _stress_tasks():
            report = harness.run_query_aware_comparison(t)
            baseline_qa = harness._judge.qa_score(t, report.direct.text)
            query_qa = harness._judge.qa_score(t, report.stm.text)

            print(f"\n{'─' * 60}")
            print(
                f"Task: {t.task_id} (budget: {t.max_chars}/{len(t.content)} = "
                f"{t.max_chars / len(t.content):.0%})"
            )
            print(f"Query: '{t.context_query}'")
            print(
                f"QA score: baseline {baseline_qa['score']:.0%} → "
                f"query-aware {query_qa['score']:.0%}"
            )

            for bd, qd in zip(baseline_qa["details"], query_qa["details"]):
                b_ok = "Y" if bd["answerable"] else "N"
                q_ok = "Y" if qd["answerable"] else "N"
                delta = "" if bd["answerable"] == qd["answerable"] else " <--"
                print(f"  {b_ok}->{q_ok}{delta}  Q: {bd['question']}")

    def test_vocabulary_mismatch(self, capsys):
        """Vocab mismatch: query uses different terms than content.

        Same stress tasks but queries avoid exact terms from the content.
        Tests whether BM25 heading weight is sufficient for semantic routing.
        """
        import copy

        harness = BenchHarness(
            cleaner=DefaultContentCleaner(),
            compressor=TruncateCompressor(),
            judge=RuleBasedJudge(),
        )

        # Mismatched queries: semantically equivalent but different vocabulary
        mismatch_queries = {
            "stress-monitoring-redis": "캐시 히트율 저하 원인과 메모리 부족 분석",
            "stress-adr-security": "인증 데이터 보호 방법과 취약점 점검 일정",
            "stress-api-permissions": "접근 권한 체계와 역할 기반 제어 구조",
        }

        tasks = _stress_tasks()
        print("\n" + "=" * 70)
        print("VOCABULARY MISMATCH TEST")
        print("=" * 70)

        for t in tasks:
            # Original (term-match) query
            orig_report = harness.run_query_aware_comparison(t)
            orig_qa = harness._judge.qa_score(t, orig_report.stm.text)

            # Mismatched query
            t_mis = copy.deepcopy(t)
            t_mis.context_query = mismatch_queries[t.task_id]
            mis_report = harness.run_query_aware_comparison(t_mis)
            mis_qa = harness._judge.qa_score(t_mis, mis_report.stm.text)

            # Baseline (no query)
            baseline_qa = harness._judge.qa_score(t, orig_report.direct.text)

            print(f"\n{'─' * 60}")
            print(
                f"Task: {t.task_id} (budget: {t.max_chars}/{len(t.content)} "
                f"= {t.max_chars / len(t.content):.0%})"
            )
            print(f"  Baseline (no query):   QA {baseline_qa['score']:.0%}")
            print(f"  Term-match query:      QA {orig_qa['score']:.0%}  '{t.context_query}'")
            print(
                f"  Mismatched query:      QA {mis_qa['score']:.0%}  "
                f"'{mismatch_queries[t.task_id]}'"
            )

            for oq, mq in zip(orig_qa["details"], mis_qa["details"]):
                o_ok = "Y" if oq["answerable"] else "N"
                m_ok = "Y" if mq["answerable"] else "N"
                delta = "" if oq["answerable"] == mq["answerable"] else " <--"
                print(f"    {o_ok}->{m_ok}{delta}  {oq['question']}")

        print("\n" + "=" * 70)


def _print_report(
    improvements: list[tuple[str, float, float]],
    baseline_scores: list[float],
    query_scores: list[float],
) -> None:
    n = len(improvements)
    avg_b = sum(baseline_scores) / n if n else 0
    avg_q = sum(query_scores) / n if n else 0
    improved = sum(1 for _, b, q in improvements if q > b)
    same = sum(1 for _, b, q in improvements if q == b)
    degraded = sum(1 for _, b, q in improvements if q < b)

    print(f"\nTasks: {n}")
    print(f"Baseline avg:    {avg_b:.2f}/10")
    print(f"Query-aware avg: {avg_q:.2f}/10")
    print(f"Delta:           {avg_q - avg_b:+.2f}")
    print(f"\nImproved: {improved}  Same: {same}  Degraded: {degraded}")

    print(f"\n{'Task':<35} {'Baseline':>8} {'Query':>8} {'Delta':>8}")
    print("-" * 63)
    for tid, b, q in sorted(improvements, key=lambda x: x[2] - x[1], reverse=True):
        delta = q - b
        marker = "+" if delta > 0 else (" " if delta == 0 else "")
        print(f"  {tid:<33} {b:>8.1f} {q:>8.1f} {marker}{delta:>7.1f}")
    print("=" * 70)

"""B.2 v2 query portfolio — 100 queries across 6 topics × 2 languages.

Corpus coverage after the 2026-04-18 scope narrowing: caching,
postgres, cost_optimization, security, observability, k8s — 192
chunks / 96 per language / 33 distinct primary subtopics (30 from
6 topics + 3 reclassifications: auth/mtls, auth/rbac, networking/tls).

See `docs/planning/b2-v2-design.md` § "Query portfolio" and
`docs/planning/b2-v2-query-portfolio.md` for the type distribution,
sensitivity expectations, and genre anchor vocabulary.

Per language (50 queries each, 100 total):
- 10 direct — keyword-heavy, matches chunk vocabulary closely
- 10 paraphrase — semantic equivalent, vocabulary-shifted
- underspecified — vague, MMR-driven (8 EN, 10 KO)
- 7 multi_topic — spans two topics via union
- negation — "what's NOT chosen" (5 EN, 3 KO)
- 10 genre_primary — anchored on genre marker vocabulary

Target tags reference primary subtopics that exist in the fixture
corpus for the query's language. The companion test
(`test_query_portfolio.py`) asserts every query has ≥ 1
primary-matching chunk per target in the query's language.

Discontinuity 2 (KO tokenizer workaround): KO queries use
`kubernetes` instead of `k8s` where the topic token is the
dominant signal; EN queries use `k8s`. See ledger
§ "Methodology discontinuities".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QueryType = Literal[
    "direct",
    "paraphrase",
    "underspecified",
    "multi_topic",
    "negation",
    "genre_primary",
]
Lang = Literal["ko", "en"]


Genre = Literal["runbook", "postmortem", "adr", "troubleshooting"]


@dataclass(frozen=True)
class Query:
    text: str
    targets: frozenset[str]
    type: QueryType
    lang: Lang
    genre: Genre | None = None
    """Expected genre for genre_primary queries; None for other types.

    Used by `calibrate_portfolio.py` to build the genre confusion matrix
    (Phase 5e). For topic-primary types (direct / paraphrase / etc.)
    there is no single expected genre, so this stays None.
    """


def _q(
    text: str,
    targets: list[str],
    type: QueryType,
    lang: Lang,
    genre: Genre | None = None,
) -> Query:
    return Query(text=text, targets=frozenset(targets), type=type, lang=lang, genre=genre)


# ---- EN: 50 queries ----

_EN_DIRECT = [
    _q("Redis maxmemory-policy allkeys-lru eviction", ["caching/eviction"], "direct", "en"),
    _q("pgbouncer transaction mode connection pool", ["postgres/connection_pool"], "direct", "en"),
    _q("Prometheus cardinality budget active series", ["observability/metrics"], "direct", "en"),
    _q(
        "Kubernetes HPA autoscale deployment cpu-percent",
        ["k8s/scaling"],
        "direct",
        "en",
    ),
    _q(
        "Istio PeerAuthentication STRICT mesh mTLS",
        ["auth/mtls"],
        "direct",
        "en",
    ),
    _q(
        "OPA rego ABAC policy decision JWT claims",
        ["security/access_control"],
        "direct",
        "en",
    ),
    _q(
        "AWS Spot instance compute cost reduction",
        ["cost_optimization/compute"],
        "direct",
        "en",
    ),
    _q(
        "pg_stat_replication replication lag check",
        ["postgres/replication"],
        "direct",
        "en",
    ),
    _q(
        "PVC allowVolumeExpansion FileSystemResizePending",
        ["k8s/storage"],
        "direct",
        "en",
    ),
    _q(
        "OpenTelemetry traceparent header propagation",
        ["observability/tracing"],
        "direct",
        "en",
    ),
]

_EN_PARAPHRASE = [
    _q(
        "preventing cache memory exhaustion under load",
        ["caching/eviction", "caching/stampede"],
        "paraphrase",
        "en",
    ),
    _q(
        "keeping database connections alive across query bursts",
        ["postgres/connection_pool"],
        "paraphrase",
        "en",
    ),
    _q(
        "metrics that track system health without blowing up storage",
        ["observability/metrics"],
        "paraphrase",
        "en",
    ),
    _q(
        "auto-scaling compute capacity based on utilization",
        ["k8s/scaling"],
        "paraphrase",
        "en",
    ),
    _q(
        "preventing unauthorized access with cryptographic service identity",
        ["auth/mtls"],
        "paraphrase",
        "en",
    ),
    _q(
        "reducing cloud bill by right-sizing instances",
        ["cost_optimization/compute"],
        "paraphrase",
        "en",
    ),
    _q(
        "tracking request flows across microservices boundaries",
        ["observability/tracing"],
        "paraphrase",
        "en",
    ),
    _q(
        "handling secret material without embedding in code",
        ["security/secrets"],
        "paraphrase",
        "en",
    ),
    _q(
        "zero-downtime rollout of container image changes",
        ["k8s/rollout"],
        "paraphrase",
        "en",
    ),
    _q(
        "synthetic probes validating user-facing endpoints",
        ["observability/synthetic"],
        "paraphrase",
        "en",
    ),
]

_EN_UNDERSPECIFIED = [
    _q(
        "cache invalidation",
        ["caching/invalidation", "caching/stampede"],
        "underspecified",
        "en",
    ),
    _q("database indexes", ["postgres/indexing"], "underspecified", "en"),
    _q("tracing", ["observability/tracing"], "underspecified", "en"),
    _q("canary deploy", ["k8s/rollout"], "underspecified", "en"),
    _q("vulnerability scanning", ["security/vulnerability"], "underspecified", "en"),
    _q(
        "cost savings",
        [
            "cost_optimization/compute",
            "cost_optimization/storage",
            "cost_optimization/database",
            "cost_optimization/network",
            "cost_optimization/observability",
        ],
        "underspecified",
        "en",
    ),
    _q("alerting thresholds", ["observability/alerting"], "underspecified", "en"),
    _q("cluster autoscaler", ["k8s/scaling"], "underspecified", "en"),
]

_EN_MULTI_TOPIC = [
    _q(
        "monitoring Postgres replication lag",
        ["postgres/replication", "observability/metrics"],
        "multi_topic",
        "en",
    ),
    _q(
        "auto-patching Kubernetes for CVE rollout",
        ["security/vulnerability", "k8s/rollout"],
        "multi_topic",
        "en",
    ),
    _q(
        "cache TTL with observability metrics",
        ["caching/invalidation", "observability/metrics"],
        "multi_topic",
        "en",
    ),
    _q(
        "HPA scaling driven by custom metrics",
        ["k8s/scaling", "observability/metrics"],
        "multi_topic",
        "en",
    ),
    _q(
        "reducing log pipeline storage cost",
        ["observability/logging", "cost_optimization/storage"],
        "multi_topic",
        "en",
    ),
    _q(
        "blue-green deployment with mTLS enforcement",
        ["k8s/rollout", "auth/mtls"],
        "multi_topic",
        "en",
    ),
    _q(
        "partition pruning for postgres cost reduction",
        ["postgres/partitioning", "cost_optimization/database"],
        "multi_topic",
        "en",
    ),
]

_EN_NEGATION = [
    _q(
        "why eventual consistency is unsuitable for inventory writes",
        ["caching/invalidation"],
        "negation",
        "en",
    ),
    _q(
        "avoiding synchronous replication in the hot path",
        ["postgres/replication"],
        "negation",
        "en",
    ),
    _q("when not to use k8s autoscaling", ["k8s/scaling"], "negation", "en"),
    _q(
        "risks of pushing raw metrics into logs",
        ["observability/metrics", "observability/logging"],
        "negation",
        "en",
    ),
    _q(
        "why not store encryption keys in config files",
        ["security/secrets", "security/encryption"],
        "negation",
        "en",
    ),
]

_EN_GENRE_PRIMARY = [
    _q(
        "postgres configure run verify command",
        ["postgres/indexing", "postgres/connection_pool", "postgres/vacuum"],
        "genre_primary",
        "en",
        genre="runbook",
    ),
    _q(
        "observability at UTC root cause follow-up",
        [
            "observability/metrics",
            "observability/logging",
            "observability/tracing",
            "observability/alerting",
        ],
        "genre_primary",
        "en",
        genre="postmortem",
    ),
    _q(
        "k8s chose over accepted re-evaluate trade-off",
        ["k8s/scaling", "k8s/networking", "k8s/rollout"],
        "genre_primary",
        "en",
        genre="adr",
    ),
    _q(
        "security likely root cause workaround symptom",
        ["security/vulnerability", "security/secrets", "security/encryption"],
        "genre_primary",
        "en",
        genre="troubleshooting",
    ),
    _q(
        "caching configure run verify command",
        ["caching/redis", "caching/eviction", "caching/invalidation"],
        "genre_primary",
        "en",
        genre="runbook",
    ),
    _q(
        "cost chose over accepted trade-off",
        ["cost_optimization/compute", "cost_optimization/storage"],
        "genre_primary",
        "en",
        genre="adr",
    ),
    _q(
        "postgres at UTC root cause follow-up",
        ["postgres/replication", "postgres/vacuum", "postgres/connection_pool"],
        "genre_primary",
        "en",
        genre="postmortem",
    ),
    _q(
        "k8s likely root cause workaround symptom",
        ["k8s/scaling", "k8s/networking"],
        "genre_primary",
        "en",
        genre="troubleshooting",
    ),
    _q(
        "observability configure run verify command",
        ["observability/metrics", "observability/logging"],
        "genre_primary",
        "en",
        genre="runbook",
    ),
    _q(
        "security chose over accepted trade-off",
        ["security/encryption", "security/access_control", "auth/mtls"],
        "genre_primary",
        "en",
        genre="adr",
    ),
]


# ---- KO: 50 queries ----
# Discontinuity 2: use `kubernetes` instead of `k8s` in KO queries
# where the topic token is the dominant signal (kiwi drops digit-
# containing alphanumerics). KO fixture bodies include a `kubernetes`
# mention per chunk for retrieval fairness.

_KO_DIRECT = [
    _q(
        "Redis maxmemory-policy allkeys-lru eviction 정책",
        ["caching/eviction"],
        "direct",
        "ko",
    ),
    _q(
        "pgbouncer transaction 모드 커넥션 풀",
        ["postgres/connection_pool"],
        "direct",
        "ko",
    ),
    _q(
        "Prometheus 카디널리티 예산 active series",
        ["observability/metrics"],
        "direct",
        "ko",
    ),
    _q(
        "Kubernetes HPA autoscale deployment cpu-percent 설정",
        ["k8s/scaling"],
        "direct",
        "ko",
    ),
    _q(
        "cert-manager ACME 인증서 자동 갱신",
        ["networking/tls"],
        "direct",
        "ko",
    ),
    _q(
        "RBAC RoleBinding cluster-admin 바인딩",
        ["auth/rbac"],
        "direct",
        "ko",
    ),
    _q(
        "OPA rego ABAC 정책 의사결정",
        ["security/access_control"],
        "direct",
        "ko",
    ),
    _q(
        "AWS Spot 인스턴스 컴퓨트 비용 절감",
        ["cost_optimization/compute"],
        "direct",
        "ko",
    ),
    _q(
        "pg_stat_replication 복제 지연 조회",
        ["postgres/replication"],
        "direct",
        "ko",
    ),
    _q(
        "PVC allowVolumeExpansion FileSystemResizePending 용량 확장",
        ["k8s/storage"],
        "direct",
        "ko",
    ),
]

_KO_PARAPHRASE = [
    _q(
        "부하 상황에서 캐시 메모리 고갈을 막는 법",
        ["caching/eviction", "caching/stampede"],
        "paraphrase",
        "ko",
    ),
    _q(
        "쿼리 버스트 중 DB 커넥션을 유지하는 구성",
        ["postgres/connection_pool"],
        "paraphrase",
        "ko",
    ),
    _q(
        "저장 비용을 폭발시키지 않고 시스템 상태를 추적하는 지표",
        ["observability/metrics"],
        "paraphrase",
        "ko",
    ),
    _q(
        "사용량에 따라 컴퓨트 용량을 자동 조절하는 방법",
        ["k8s/scaling"],
        "paraphrase",
        "ko",
    ),
    _q(
        "전송 구간 인증서 로테이션으로 기밀성 유지",
        ["networking/tls"],
        "paraphrase",
        "ko",
    ),
    _q(
        "클라우드 청구서를 줄이기 위한 인스턴스 최적화",
        ["cost_optimization/compute"],
        "paraphrase",
        "ko",
    ),
    _q(
        "마이크로서비스 경계를 넘는 요청 흐름 추적",
        ["observability/tracing"],
        "paraphrase",
        "ko",
    ),
    _q(
        "소스 코드에 비밀번호를 심지 않고 관리하는 방법",
        ["security/secrets"],
        "paraphrase",
        "ko",
    ),
    _q(
        "무중단 컨테이너 이미지 롤아웃 절차",
        ["k8s/rollout"],
        "paraphrase",
        "ko",
    ),
    _q(
        "읽기 복제본을 일관된 상태에 가깝게 유지",
        ["postgres/replication", "caching/replication"],
        "paraphrase",
        "ko",
    ),
]

_KO_UNDERSPECIFIED = [
    _q(
        "캐시 무효화",
        ["caching/invalidation", "caching/stampede"],
        "underspecified",
        "ko",
    ),
    _q("데이터베이스 인덱스", ["postgres/indexing"], "underspecified", "ko"),
    _q("트레이싱", ["observability/tracing"], "underspecified", "ko"),
    _q("카나리 배포", ["k8s/rollout"], "underspecified", "ko"),
    _q("취약점 스캔", ["security/vulnerability"], "underspecified", "ko"),
    _q(
        "비용 최적화",
        [
            "cost_optimization/compute",
            "cost_optimization/storage",
            "cost_optimization/database",
            "cost_optimization/network",
            "cost_optimization/observability",
        ],
        "underspecified",
        "ko",
    ),
    _q("클러스터 오토스케일러", ["k8s/scaling"], "underspecified", "ko"),
    _q("알람 기준치", ["observability/alerting"], "underspecified", "ko"),
    _q(
        "백업 전략",
        ["postgres/vacuum", "postgres/partitioning", "cost_optimization/storage"],
        "underspecified",
        "ko",
    ),
    _q("이벤트 로깅", ["observability/logging"], "underspecified", "ko"),
]

_KO_MULTI_TOPIC = [
    _q(
        "Postgres 복제 지연 모니터링",
        ["postgres/replication", "observability/metrics"],
        "multi_topic",
        "ko",
    ),
    _q(
        "Kubernetes CVE 패치 자동 롤아웃",
        ["security/vulnerability", "k8s/rollout"],
        "multi_topic",
        "ko",
    ),
    _q(
        "캐시 TTL 관측 지표 관리",
        ["caching/invalidation", "observability/metrics"],
        "multi_topic",
        "ko",
    ),
    _q(
        "커스텀 지표 기반 HPA 스케일링",
        ["k8s/scaling", "observability/metrics"],
        "multi_topic",
        "ko",
    ),
    _q(
        "로깅 파이프라인 저장 비용 절감",
        ["observability/logging", "cost_optimization/storage"],
        "multi_topic",
        "ko",
    ),
    _q(
        "RBAC 기반 kubernetes 네임스페이스 접근 통제",
        ["auth/rbac", "k8s/scheduling"],
        "multi_topic",
        "ko",
    ),
    _q(
        "파티션 프루닝을 통한 postgres 비용 절감",
        ["postgres/partitioning", "cost_optimization/database"],
        "multi_topic",
        "ko",
    ),
]

_KO_NEGATION = [
    _q(
        "재고 쓰기에 이벤트 기반 최종 일관성이 부적절한 이유",
        ["caching/invalidation"],
        "negation",
        "ko",
    ),
    _q(
        "hot path 에서 동기 복제를 피해야 하는 이유",
        ["postgres/replication"],
        "negation",
        "ko",
    ),
    _q(
        "kubernetes 오토스케일링을 쓰지 말아야 할 때",
        ["k8s/scaling"],
        "negation",
        "ko",
    ),
]

_KO_GENRE_PRIMARY = [
    _q(
        "postgres 절차 접속 수행",
        ["postgres/indexing", "postgres/connection_pool", "postgres/vacuum"],
        "genre_primary",
        "ko",
        genre="runbook",
    ),
    _q(
        "observability KST 원인 후속 조치",
        ["observability/metrics", "observability/logging", "observability/tracing"],
        "genre_primary",
        "ko",
        genre="postmortem",
    ),
    _q(
        "kubernetes 대신 채택 결정 trade-off",
        ["k8s/scaling", "k8s/networking"],
        "genre_primary",
        "ko",
        genre="adr",
    ),
    _q(
        "security 증상 의심 점검 진단",
        ["security/vulnerability", "security/encryption"],
        "genre_primary",
        "ko",
        genre="troubleshooting",
    ),
    _q(
        "캐시 절차 설정 수행",
        ["caching/redis", "caching/eviction", "caching/invalidation"],
        "genre_primary",
        "ko",
        genre="runbook",
    ),
    _q(
        "cost 대신 채택 trade-off",
        ["cost_optimization/compute", "cost_optimization/storage"],
        "genre_primary",
        "ko",
        genre="adr",
    ),
    _q(
        "postgres KST 원인 후속 조치",
        ["postgres/replication", "postgres/vacuum", "postgres/connection_pool"],
        "genre_primary",
        "ko",
        genre="postmortem",
    ),
    _q(
        "kubernetes 증상 의심 점검 재현",
        ["k8s/scaling", "k8s/networking"],
        "genre_primary",
        "ko",
        genre="troubleshooting",
    ),
    _q(
        "observability 절차 설정 수행",
        ["observability/metrics", "observability/logging"],
        "genre_primary",
        "ko",
        genre="runbook",
    ),
    _q(
        "security 대신 채택 trade-off",
        ["security/encryption", "security/access_control"],
        "genre_primary",
        "ko",
        genre="adr",
    ),
]


QUERIES: list[Query] = (
    _EN_DIRECT
    + _EN_PARAPHRASE
    + _EN_UNDERSPECIFIED
    + _EN_MULTI_TOPIC
    + _EN_NEGATION
    + _EN_GENRE_PRIMARY
    + _KO_DIRECT
    + _KO_PARAPHRASE
    + _KO_UNDERSPECIFIED
    + _KO_MULTI_TOPIC
    + _KO_NEGATION
    + _KO_GENRE_PRIMARY
)


def queries_by(lang: Lang | None = None, type: QueryType | None = None) -> list[Query]:
    result = QUERIES
    if lang is not None:
        result = [q for q in result if q.lang == lang]
    if type is not None:
        result = [q for q in result if q.type == type]
    return result


EXPECTED_COUNTS: dict[tuple[Lang, QueryType], int] = {
    ("en", "direct"): 10,
    ("en", "paraphrase"): 10,
    ("en", "underspecified"): 8,
    ("en", "multi_topic"): 7,
    ("en", "negation"): 5,
    ("en", "genre_primary"): 10,
    ("ko", "direct"): 10,
    ("ko", "paraphrase"): 10,
    ("ko", "underspecified"): 10,
    ("ko", "multi_topic"): 7,
    ("ko", "negation"): 3,
    ("ko", "genre_primary"): 10,
}

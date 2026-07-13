"""Frozen bilingual holdout portfolio for retrieval benchmark methodology v2.

The 60 intent pairs below produce 120 queries: ten English/Korean pairs for
each query type.  Keep query text and identifiers immutable after publication;
future tuning must use a newly versioned holdout instead of editing this file.
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
Genre = Literal["runbook", "postmortem", "adr", "troubleshooting"]


@dataclass(frozen=True)
class HoldoutQuery:
    query_id: str
    pair_id: str
    lang: Literal["en", "ko"]
    type: QueryType
    text: str
    targets: tuple[str, ...]
    genre: Genre | None = None


def _pair(
    pair_id: str,
    query_type: QueryType,
    en: str,
    ko: str,
    targets: tuple[str, ...],
    *,
    genre: Genre | None = None,
) -> tuple[HoldoutQuery, HoldoutQuery]:
    return (
        HoldoutQuery(f"{pair_id}-en", pair_id, "en", query_type, en, targets, genre),
        HoldoutQuery(f"{pair_id}-ko", pair_id, "ko", query_type, ko, targets, genre),
    )


_PAIRS = (
    # Direct: terminology deliberately overlaps the represented technical concept.
    _pair(
        "v2-direct-01",
        "direct",
        "Redis eviction maxmemory policy",
        "Redis 메모리 퇴거 maxmemory 정책",
        ("caching/eviction",),
    ),
    _pair(
        "v2-direct-02",
        "direct",
        "Postgres connection pool saturation",
        "Postgres 커넥션 풀 포화",
        ("postgres/connection_pool",),
    ),
    _pair(
        "v2-direct-03",
        "direct",
        "Prometheus metric cardinality",
        "Prometheus 메트릭 카디널리티",
        ("observability/metrics",),
    ),
    _pair(
        "v2-direct-04",
        "direct",
        "Kubernetes deployment rollout",
        "Kubernetes 디플로이먼트 롤아웃",
        ("k8s/rollout",),
    ),
    _pair(
        "v2-direct-05",
        "direct",
        "secret manager credential rotation",
        "시크릿 매니저 자격 증명 순환",
        ("security/secrets",),
    ),
    _pair(
        "v2-direct-06",
        "direct",
        "compute rightsizing cost",
        "컴퓨트 리소스 적정 크기 비용",
        ("cost_optimization/compute",),
    ),
    _pair(
        "v2-direct-07",
        "direct",
        "Postgres autovacuum table bloat",
        "Postgres autovacuum 테이블 팽창",
        ("postgres/vacuum",),
    ),
    _pair(
        "v2-direct-08",
        "direct",
        "Kubernetes persistent volume storage",
        "Kubernetes 영구 볼륨 스토리지",
        ("k8s/storage",),
    ),
    _pair(
        "v2-direct-09",
        "direct",
        "Redis replica failover",
        "Redis 복제본 장애 조치",
        ("caching/replication",),
    ),
    _pair(
        "v2-direct-10",
        "direct",
        "alert threshold tuning",
        "알림 임계값 튜닝",
        ("observability/alerting",),
    ),
    # Paraphrase: low lexical overlap while preserving a single concept.
    _pair(
        "v2-paraphrase-01",
        "paraphrase",
        "prevent many clients rebuilding an expired value together",
        "만료된 값을 여러 클라이언트가 동시에 다시 만들지 않게 하는 방법",
        ("caching/stampede",),
    ),
    _pair(
        "v2-paraphrase-02",
        "paraphrase",
        "speed up row lookup without scanning the whole table",
        "전체 테이블을 훑지 않고 행 조회를 빠르게 하는 방법",
        ("postgres/indexing",),
    ),
    _pair(
        "v2-paraphrase-03",
        "paraphrase",
        "follow one request through several services",
        "하나의 요청이 여러 서비스를 지나는 경로 추적",
        ("observability/tracing",),
    ),
    _pair(
        "v2-paraphrase-04",
        "paraphrase",
        "add and remove pods as demand changes",
        "수요 변화에 따라 파드 수를 늘리고 줄이기",
        ("k8s/scaling",),
    ),
    _pair(
        "v2-paraphrase-05",
        "paraphrase",
        "limit which users may perform each operation",
        "사용자별로 수행 가능한 작업을 제한하는 방법",
        ("security/access_control",),
    ),
    _pair(
        "v2-paraphrase-06",
        "paraphrase",
        "reduce charges caused by traffic crossing zones",
        "가용 영역을 넘는 트래픽 요금 줄이기",
        ("cost_optimization/network",),
    ),
    _pair(
        "v2-paraphrase-07",
        "paraphrase",
        "split a very large relation into manageable pieces",
        "매우 큰 테이블을 관리 가능한 조각으로 나누기",
        ("postgres/partitioning",),
    ),
    _pair(
        "v2-paraphrase-08",
        "paraphrase",
        "choose nodes for workloads with placement constraints",
        "배치 제약에 맞춰 워크로드가 실행될 노드 선택",
        ("k8s/scheduling",),
    ),
    _pair(
        "v2-paraphrase-09",
        "paraphrase",
        "remediate a newly disclosed software weakness",
        "새로 공개된 소프트웨어 취약점 대응",
        ("security/vulnerability",),
    ),
    _pair(
        "v2-paraphrase-10",
        "paraphrase",
        "centralize application event records for investigation",
        "조사를 위해 애플리케이션 이벤트 기록을 중앙화하기",
        ("observability/logging",),
    ),
    # Underspecified: short realistic prompts with one intended concept.
    _pair(
        "v2-underspecified-01",
        "underspecified",
        "stale cache entries",
        "오래된 캐시 값",
        ("caching/invalidation",),
    ),
    _pair(
        "v2-underspecified-02",
        "underspecified",
        "database connections exhausted",
        "DB 연결 고갈",
        ("postgres/connection_pool",),
    ),
    _pair(
        "v2-underspecified-03",
        "underspecified",
        "missing spans",
        "누락된 span",
        ("observability/tracing",),
    ),
    _pair(
        "v2-underspecified-04",
        "underspecified",
        "pods stuck pending",
        "Pending 상태 파드",
        ("k8s/scheduling",),
    ),
    _pair(
        "v2-underspecified-05",
        "underspecified",
        "credential exposure",
        "자격 증명 노출",
        ("security/secrets",),
    ),
    _pair(
        "v2-underspecified-06",
        "underspecified",
        "cloud compute bill spike",
        "클라우드 컴퓨트 비용 급증",
        ("cost_optimization/compute",),
    ),
    _pair(
        "v2-underspecified-07", "underspecified", "table bloat", "테이블 팽창", ("postgres/vacuum",)
    ),
    _pair(
        "v2-underspecified-08",
        "underspecified",
        "persistent volume retention",
        "영구 볼륨 보존",
        ("k8s/storage",),
    ),
    _pair(
        "v2-underspecified-09",
        "underspecified",
        "noisy paging",
        "과도한 호출 알림",
        ("observability/alerting",),
    ),
    _pair(
        "v2-underspecified-10",
        "underspecified",
        "cache failover",
        "캐시 장애 조치",
        ("caching/replication",),
    ),
    # Multi-topic: both target concepts must be represented in top-k.
    _pair(
        "v2-multi-01",
        "multi_topic",
        "monitor cache invalidation with metrics",
        "메트릭으로 캐시 무효화 모니터링",
        ("caching/invalidation", "observability/metrics"),
    ),
    _pair(
        "v2-multi-02",
        "multi_topic",
        "secure a Postgres connection pool",
        "Postgres 커넥션 풀 보안",
        ("postgres/connection_pool", "security/access_control"),
    ),
    _pair(
        "v2-multi-03",
        "multi_topic",
        "trace a Kubernetes rollout",
        "Kubernetes 롤아웃 추적",
        ("k8s/rollout", "observability/tracing"),
    ),
    _pair(
        "v2-multi-04",
        "multi_topic",
        "reduce storage cost for database partitions",
        "데이터베이스 파티션의 저장 비용 절감",
        ("postgres/partitioning", "cost_optimization/storage"),
    ),
    _pair(
        "v2-multi-05",
        "multi_topic",
        "rotate secrets during deployment",
        "배포 중 시크릿 순환",
        ("security/secrets", "k8s/rollout"),
    ),
    _pair(
        "v2-multi-06",
        "multi_topic",
        "alert on Redis replica failures",
        "Redis 복제본 장애 알림",
        ("caching/replication", "observability/alerting"),
    ),
    _pair(
        "v2-multi-07",
        "multi_topic",
        "autoscale workloads while controlling compute cost",
        "컴퓨트 비용을 관리하며 워크로드 자동 확장",
        ("k8s/scaling", "cost_optimization/compute"),
    ),
    _pair(
        "v2-multi-08",
        "multi_topic",
        "log access-control denials",
        "접근 제어 거부 이벤트 로깅",
        ("security/access_control", "observability/logging"),
    ),
    _pair(
        "v2-multi-09",
        "multi_topic",
        "index vulnerability audit records",
        "취약점 감사 기록 인덱싱",
        ("postgres/indexing", "security/vulnerability"),
    ),
    _pair(
        "v2-multi-10",
        "multi_topic",
        "measure cache stampede impact on cloud cost",
        "캐시 스탬피드가 클라우드 비용에 미치는 영향 측정",
        ("caching/stampede", "cost_optimization/compute"),
    ),
    # Negation/contrast: relevant passages are matching ADR chunks; matching
    # non-ADR chunks become explicit hard negatives in the frozen qrels.
    _pair(
        "v2-negation-01",
        "negation",
        "why not use allkeys eviction for every Redis key",
        "모든 Redis 키에 allkeys 퇴거를 쓰지 않는 이유",
        ("caching/eviction",),
        genre="adr",
    ),
    _pair(
        "v2-negation-02",
        "negation",
        "why avoid synchronous cache invalidation across regions",
        "리전 간 동기식 캐시 무효화를 피하는 이유",
        ("caching/invalidation",),
        genre="adr",
    ),
    _pair(
        "v2-negation-03",
        "negation",
        "why not rely on manual Postgres indexes alone",
        "수동 Postgres 인덱스에만 의존하지 않는 이유",
        ("postgres/indexing",),
        genre="adr",
    ),
    _pair(
        "v2-negation-04",
        "negation",
        "when not to add more database connections",
        "DB 연결 수를 더 늘리면 안 되는 경우",
        ("postgres/connection_pool",),
        genre="adr",
    ),
    _pair(
        "v2-negation-05",
        "negation",
        "why not schedule every pod on the cheapest node",
        "모든 파드를 가장 저렴한 노드에 배치하지 않는 이유",
        ("k8s/scheduling",),
        genre="adr",
    ),
    _pair(
        "v2-negation-06",
        "negation",
        "why avoid deleting persistent volumes automatically",
        "영구 볼륨을 자동 삭제하지 않는 이유",
        ("k8s/storage",),
        genre="adr",
    ),
    _pair(
        "v2-negation-07",
        "negation",
        "why not retain every debug log",
        "모든 디버그 로그를 보관하지 않는 이유",
        ("observability/logging",),
        genre="adr",
    ),
    _pair(
        "v2-negation-08",
        "negation",
        "why not collect every metric at maximum frequency",
        "모든 메트릭을 최고 빈도로 수집하지 않는 이유",
        ("observability/metrics",),
        genre="adr",
    ),
    _pair(
        "v2-negation-09",
        "negation",
        "why not grant broad administrative access",
        "광범위한 관리자 접근 권한을 부여하지 않는 이유",
        ("security/access_control",),
        genre="adr",
    ),
    _pair(
        "v2-negation-10",
        "negation",
        "why avoid leaving secrets in application configuration",
        "애플리케이션 설정에 시크릿을 남기지 않는 이유",
        ("security/secrets",),
        genre="adr",
    ),
    # Genre-primary: relevance is topic AND expected genre, not selected tags.
    _pair(
        "v2-genre-01",
        "genre_primary",
        "Redis step-by-step verification procedure",
        "Redis 단계별 확인 절차",
        ("caching/redis",),
        genre="runbook",
    ),
    _pair(
        "v2-genre-02",
        "genre_primary",
        "Postgres incident timeline and follow-up",
        "Postgres 장애 타임라인과 후속 조치",
        ("postgres/replication",),
        genre="postmortem",
    ),
    _pair(
        "v2-genre-03",
        "genre_primary",
        "Kubernetes option decision and trade-off",
        "Kubernetes 대안 결정과 트레이드오프",
        ("k8s/networking",),
        genre="adr",
    ),
    _pair(
        "v2-genre-04",
        "genre_primary",
        "observability symptom diagnosis and workaround",
        "관측성 증상 진단과 우회 방법",
        ("observability/metrics",),
        genre="troubleshooting",
    ),
    _pair(
        "v2-genre-05",
        "genre_primary",
        "security response commands and verification",
        "보안 대응 명령과 검증 절차",
        ("security/vulnerability",),
        genre="runbook",
    ),
    _pair(
        "v2-genre-06",
        "genre_primary",
        "cloud cost incident root cause timeline",
        "클라우드 비용 장애 원인 타임라인",
        ("cost_optimization/compute",),
        genre="postmortem",
    ),
    _pair(
        "v2-genre-07",
        "genre_primary",
        "Postgres architecture choice rationale",
        "Postgres 아키텍처 선택 근거",
        ("postgres/partitioning",),
        genre="adr",
    ),
    _pair(
        "v2-genre-08",
        "genre_primary",
        "Kubernetes failure symptom and corrective check",
        "Kubernetes 실패 증상과 교정 점검",
        ("k8s/scaling",),
        genre="troubleshooting",
    ),
    _pair(
        "v2-genre-09",
        "genre_primary",
        "metrics collection operational checklist",
        "메트릭 수집 운영 체크리스트",
        ("observability/metrics",),
        genre="runbook",
    ),
    _pair(
        "v2-genre-10",
        "genre_primary",
        "cache outage impact and remediation timeline",
        "캐시 장애 영향과 복구 타임라인",
        ("caching/replication",),
        genre="postmortem",
    ),
)

QUERIES: tuple[HoldoutQuery, ...] = tuple(query for pair in _PAIRS for query in pair)

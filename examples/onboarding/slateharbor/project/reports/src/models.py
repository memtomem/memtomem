"""Slateharbor reports: synthetic local policy sample, no external services."""
from dataclasses import dataclass

@dataclass(frozen=True)
class FreshnessMinutesRequest:
    """report freshness: 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다."""
    age_minutes: int
    environment: str = 'production'

@dataclass(frozen=True)
class ExportRowsRequest:
    """CSV export rows: 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다."""
    rows: int
    environment: str = 'production'

@dataclass(frozen=True)
class WindowDaysRequest:
    """report window: 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다."""
    days: int
    environment: str = 'production'

@dataclass(frozen=True)
class CacheSecondsRequest:
    """report cache ttl: 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다."""
    age_seconds: int
    environment: str = 'production'

@dataclass(frozen=True)
class MinimumGroupRequest:
    """aggregate group privacy: 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다."""
    members: int
    environment: str = 'production'

@dataclass(frozen=True)
class QuerySecondsRequest:
    """report query timeout: 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다."""
    elapsed_seconds: int
    environment: str = 'production'


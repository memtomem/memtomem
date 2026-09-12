"""Slateharbor reports: synthetic local policy sample, no external services."""

def freshness_minutes(age_minutes, config):
    """report freshness. 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다."""
    return age_minutes <= config['freshness_minutes']['value']


def export_rows(rows, config):
    """CSV export rows. 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다."""
    return rows <= config['export_rows']['value']


def window_days(days, config):
    """report window. 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다."""
    return days <= config['window_days']['value']


def cache_seconds(age_seconds, config):
    """report cache ttl. 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다."""
    return age_seconds < config['cache_seconds']['value']


def minimum_group(members, config):
    """aggregate group privacy. 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다."""
    return members >= config['minimum_group']['value']


def query_seconds(elapsed_seconds, config):
    """report query timeout. 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다."""
    return elapsed_seconds <= config['query_seconds']['value']


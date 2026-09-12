"""Slateharbor reports: synthetic local policy sample, no external services."""
from reports.src import policy

def evaluate_freshness_minutes(age_minutes, config):
    """materialized_at과 원장 event watermark를 비교한다. 갱신 실패 시 이전 값과 지연 상태를 함께 보여준다."""
    return {'policy': 'freshness_minutes', 'allowed': policy.freshness_minutes(age_minutes, config),
            'revision': config['freshness_minutes']['revision'], 'observed': age_minutes}

def evaluate_export_rows(rows, config):
    """export 요청 ID로 기존 job을 조회하고 중복 클릭에는 같은 ID를 반환한다."""
    return {'policy': 'export_rows', 'allowed': policy.export_rows(rows, config),
            'revision': config['export_rows']['revision'], 'observed': rows}

def evaluate_window_days(days, config):
    """요청 기간을 확인하고 읽기 전용 분석 경로로 우회한다."""
    return {'policy': 'window_days', 'allowed': policy.window_days(days, config),
            'revision': config['window_days']['revision'], 'observed': days}

def evaluate_cache_seconds(age_seconds, config):
    """cache key의 tenant와 permission revision을 확인하고 영향을 받은 key만 무효화한다."""
    return {'policy': 'cache_seconds', 'allowed': policy.cache_seconds(age_seconds, config),
            'revision': config['cache_seconds']['revision'], 'observed': age_seconds}

def evaluate_minimum_group(members, config):
    """그룹 크기를 확인하고 작은 그룹은 상위 집계로 합친다."""
    return {'policy': 'minimum_group', 'allowed': policy.minimum_group(members, config),
            'revision': config['minimum_group']['revision'], 'observed': members}

def evaluate_query_seconds(elapsed_seconds, config):
    """query 상태와 오류 코드를 확인하고 마지막 정상 결과와 구분해서 표시한다."""
    return {'policy': 'query_seconds', 'allowed': policy.query_seconds(elapsed_seconds, config),
            'revision': config['query_seconds']['revision'], 'observed': elapsed_seconds}


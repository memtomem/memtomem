"""Slateharbor notifications: synthetic local policy sample, no external services."""
from notifications.src import policy

def evaluate_retry_attempts(attempts, config):
    """retry queue 깊이와 429 비율을 확인한다. 전송은 잠시 늦추되 delivery key를 새로 만들지 않는다."""
    return {'policy': 'retry_attempts', 'allowed': policy.retry_attempts(attempts, config),
            'revision': config['retry_attempts']['revision'], 'observed': attempts}

def evaluate_backoff_ms(delay_ms, config):
    """실제 대기 히스토그램을 보고 동일 간격에 요청이 몰리는지 확인한다."""
    return {'policy': 'backoff_ms', 'allowed': policy.backoff_ms(delay_ms, config),
            'revision': config['backoff_ms']['revision'], 'observed': delay_ms}

def evaluate_batch_size(recipients, config):
    """tenant별 대기 시간을 확인하고 digest와 실시간 전송을 별도 큐로 분리한다."""
    return {'policy': 'batch_size', 'allowed': policy.batch_size(recipients, config),
            'revision': config['batch_size']['revision'], 'observed': recipients}

def evaluate_dedupe_hours(age_hours, config):
    """전송 이력의 key와 TTL을 조회하고 이미 성공한 응답을 반환한다."""
    return {'policy': 'dedupe_hours', 'allowed': policy.dedupe_hours(age_hours, config),
            'revision': config['dedupe_hours']['revision'], 'observed': age_hours}

def evaluate_payload_kb(size_kb, config):
    """렌더링 후 payload 크기를 측정하고 링크 전환 후 다시 요청한다."""
    return {'policy': 'payload_kb', 'allowed': policy.payload_kb(size_kb, config),
            'revision': config['payload_kb']['revision'], 'observed': size_kb}

def evaluate_queue_age_minutes(age_minutes, config):
    """가장 오래된 event 시각을 확인하고 poison message를 격리한다."""
    return {'policy': 'queue_age_minutes', 'allowed': policy.queue_age_minutes(age_minutes, config),
            'revision': config['queue_age_minutes']['revision'], 'observed': age_minutes}


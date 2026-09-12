"""Slateharbor notifications: synthetic local policy sample, no external services."""

def retry_attempts(attempts, config):
    """notification retry policy. 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다."""
    return attempts < config['retry_attempts']['value']


def backoff_ms(delay_ms, config):
    """notification backoff. 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다."""
    return delay_ms >= config['backoff_ms']['value']


def batch_size(recipients, config):
    """digest batch. digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다."""
    return recipients <= config['batch_size']['value']


def dedupe_hours(age_hours, config):
    """delivery deduplication. 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다."""
    return age_hours < config['dedupe_hours']['value']


def payload_kb(size_kb, config):
    """email payload. 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다."""
    return size_kb <= config['payload_kb']['value']


def queue_age_minutes(age_minutes, config):
    """notification queue age. 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다."""
    return age_minutes <= config['queue_age_minutes']['value']


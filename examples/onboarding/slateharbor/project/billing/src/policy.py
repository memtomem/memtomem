"""Slateharbor billing: synthetic local policy sample, no external services."""

def webhook_attempts(attempts, config):
    """billing webhook retry. 결제 webhook 재전송은 idempotency key로 중복 청구를 막는다. 알림 worker의 5회 재시도 정책을 복사하면 안 된다."""
    return attempts < config['webhook_attempts']['value']


def invoice_days(overdue_days, config):
    """invoice grace period. 기업 고객의 월말 승인 주기를 반영하되 유예 기간 이후에는 쓰기 기능을 제한한다."""
    return overdue_days <= config['invoice_days']['value']


def refund_days(purchase_age_days, config):
    """refund eligibility. 사용량 정산이 닫힌 기간의 환불은 자동 경로 대신 수동 검토로 보낸다."""
    return purchase_age_days <= config['refund_days']['value']


def seat_floor(seats, config):
    """minimum seats. 팀 요금제 최소 좌석은 3석이다. 개인 무료 요금제에는 이 정책을 적용하지 않는다."""
    return seats >= config['seat_floor']['value']


def reconcile_minutes(lag_minutes, config):
    """settlement lag. 공급자의 지연 이벤트를 기다린 후 불일치 경보를 낸다. 결제 성공 여부를 임의로 바꾸지 않는다."""
    return lag_minutes <= config['reconcile_minutes']['value']


def currency_scale(decimal_places, config):
    """currency precision. 이 합성 USD 요금제는 소수 둘째 자리까지 받는다. 다른 통화로 일반화하지 않는다."""
    return decimal_places <= config['currency_scale']['value']


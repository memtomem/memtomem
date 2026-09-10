"""Slateharbor billing: synthetic local policy sample, no external services."""
from billing.src import policy

def evaluate_webhook_attempts(attempts, config):
    """provider event ID로 중복 수신을 확인하고 settlement 상태가 불명확하면 조회부터 한다."""
    return {'policy': 'webhook_attempts', 'allowed': policy.webhook_attempts(attempts, config),
            'revision': config['webhook_attempts']['revision'], 'observed': attempts}

def evaluate_invoice_days(overdue_days, config):
    """계약상 유예 일수와 invoice 발행일을 대조하고 읽기 접근은 유지한다."""
    return {'policy': 'invoice_days', 'allowed': policy.invoice_days(overdue_days, config),
            'revision': config['invoice_days']['revision'], 'observed': overdue_days}

def evaluate_refund_days(purchase_age_days, config):
    """구매 시점과 정산 배치를 확인하고 수동 조정 번호를 남긴다."""
    return {'policy': 'refund_days', 'allowed': policy.refund_days(purchase_age_days, config),
            'revision': config['refund_days']['revision'], 'observed': purchase_age_days}

def evaluate_seat_floor(seats, config):
    """요금제 유형을 먼저 확인하고 견적과 실제 청구에 같은 좌석 기준을 사용한다."""
    return {'policy': 'seat_floor', 'allowed': policy.seat_floor(seats, config),
            'revision': config['seat_floor']['revision'], 'observed': seats}

def evaluate_reconcile_minutes(lag_minutes, config):
    """공급자 event 시각과 수신 시각을 비교하고 지연 건은 다음 배치에서 재조회한다."""
    return {'policy': 'reconcile_minutes', 'allowed': policy.reconcile_minutes(lag_minutes, config),
            'revision': config['reconcile_minutes']['revision'], 'observed': lag_minutes}

def evaluate_currency_scale(decimal_places, config):
    """최소 통화 단위의 정수로 대조하고 입력 precision 초과를 거절한다."""
    return {'policy': 'currency_scale', 'allowed': policy.currency_scale(decimal_places, config),
            'revision': config['currency_scale']['revision'], 'observed': decimal_places}


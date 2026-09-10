"""Slateharbor billing: synthetic local policy sample, no external services."""
from billing.src import policy

def replay_webhook_attempts(fixtures, config):
    """Replay billing webhook retry input examples; no real requests are sent."""
    case = fixtures['webhook_attempts']
    return [policy.webhook_attempts(value, config) for value in case['inputs']]

def replay_invoice_days(fixtures, config):
    """Replay invoice grace period input examples; no real requests are sent."""
    case = fixtures['invoice_days']
    return [policy.invoice_days(value, config) for value in case['inputs']]

def replay_refund_days(fixtures, config):
    """Replay refund eligibility input examples; no real requests are sent."""
    case = fixtures['refund_days']
    return [policy.refund_days(value, config) for value in case['inputs']]

def replay_seat_floor(fixtures, config):
    """Replay minimum seats input examples; no real requests are sent."""
    case = fixtures['seat_floor']
    return [policy.seat_floor(value, config) for value in case['inputs']]

def replay_reconcile_minutes(fixtures, config):
    """Replay settlement lag input examples; no real requests are sent."""
    case = fixtures['reconcile_minutes']
    return [policy.reconcile_minutes(value, config) for value in case['inputs']]

def replay_currency_scale(fixtures, config):
    """Replay currency precision input examples; no real requests are sent."""
    case = fixtures['currency_scale']
    return [policy.currency_scale(value, config) for value in case['inputs']]


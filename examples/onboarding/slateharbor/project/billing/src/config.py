"""Slateharbor billing: synthetic local policy sample, no external services."""
from copy import deepcopy

def configure_webhook_attempts(config, value):
    """Validate and copy billing webhook retry; production value is 3."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid webhook_attempts')
    updated = deepcopy(config)
    updated['webhook_attempts']['value'] = value
    return updated

def configure_invoice_days(config, value):
    """Validate and copy invoice grace period; production value is 14."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid invoice_days')
    updated = deepcopy(config)
    updated['invoice_days']['value'] = value
    return updated

def configure_refund_days(config, value):
    """Validate and copy refund eligibility; production value is 30."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid refund_days')
    updated = deepcopy(config)
    updated['refund_days']['value'] = value
    return updated

def configure_seat_floor(config, value):
    """Validate and copy minimum seats; production value is 3."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid seat_floor')
    updated = deepcopy(config)
    updated['seat_floor']['value'] = value
    return updated

def configure_reconcile_minutes(config, value):
    """Validate and copy settlement lag; production value is 20."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid reconcile_minutes')
    updated = deepcopy(config)
    updated['reconcile_minutes']['value'] = value
    return updated

def configure_currency_scale(config, value):
    """Validate and copy currency precision; production value is 2."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid currency_scale')
    updated = deepcopy(config)
    updated['currency_scale']['value'] = value
    return updated


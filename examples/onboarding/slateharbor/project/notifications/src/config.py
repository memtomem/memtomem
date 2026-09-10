"""Slateharbor notifications: synthetic local policy sample, no external services."""
from copy import deepcopy

def configure_retry_attempts(config, value):
    """Validate and copy notification retry policy; production value is 5."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid retry_attempts')
    updated = deepcopy(config)
    updated['retry_attempts']['value'] = value
    return updated

def configure_backoff_ms(config, value):
    """Validate and copy notification backoff; production value is 250."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid backoff_ms')
    updated = deepcopy(config)
    updated['backoff_ms']['value'] = value
    return updated

def configure_batch_size(config, value):
    """Validate and copy digest batch; production value is 40."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid batch_size')
    updated = deepcopy(config)
    updated['batch_size']['value'] = value
    return updated

def configure_dedupe_hours(config, value):
    """Validate and copy delivery deduplication; production value is 24."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid dedupe_hours')
    updated = deepcopy(config)
    updated['dedupe_hours']['value'] = value
    return updated

def configure_payload_kb(config, value):
    """Validate and copy email payload; production value is 128."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid payload_kb')
    updated = deepcopy(config)
    updated['payload_kb']['value'] = value
    return updated

def configure_queue_age_minutes(config, value):
    """Validate and copy notification queue age; production value is 15."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid queue_age_minutes')
    updated = deepcopy(config)
    updated['queue_age_minutes']['value'] = value
    return updated


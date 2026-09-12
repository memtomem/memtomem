"""Slateharbor notifications: synthetic local policy sample, no external services."""
from notifications.src import policy

def replay_retry_attempts(fixtures, config):
    """Replay notification retry policy input examples; no real requests are sent."""
    case = fixtures['retry_attempts']
    return [policy.retry_attempts(value, config) for value in case['inputs']]

def replay_backoff_ms(fixtures, config):
    """Replay notification backoff input examples; no real requests are sent."""
    case = fixtures['backoff_ms']
    return [policy.backoff_ms(value, config) for value in case['inputs']]

def replay_batch_size(fixtures, config):
    """Replay digest batch input examples; no real requests are sent."""
    case = fixtures['batch_size']
    return [policy.batch_size(value, config) for value in case['inputs']]

def replay_dedupe_hours(fixtures, config):
    """Replay delivery deduplication input examples; no real requests are sent."""
    case = fixtures['dedupe_hours']
    return [policy.dedupe_hours(value, config) for value in case['inputs']]

def replay_payload_kb(fixtures, config):
    """Replay email payload input examples; no real requests are sent."""
    case = fixtures['payload_kb']
    return [policy.payload_kb(value, config) for value in case['inputs']]

def replay_queue_age_minutes(fixtures, config):
    """Replay notification queue age input examples; no real requests are sent."""
    case = fixtures['queue_age_minutes']
    return [policy.queue_age_minutes(value, config) for value in case['inputs']]


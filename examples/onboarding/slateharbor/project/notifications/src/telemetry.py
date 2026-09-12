"""Slateharbor notifications: synthetic local policy sample, no external services."""
from notifications.src import policy

def summarize_retry_attempts(observations, config):
    """notification retry policy: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.retry_attempts(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_backoff_ms(observations, config):
    """notification backoff: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.backoff_ms(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_batch_size(observations, config):
    """digest batch: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.batch_size(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_dedupe_hours(observations, config):
    """delivery deduplication: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.dedupe_hours(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_payload_kb(observations, config):
    """email payload: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.payload_kb(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_queue_age_minutes(observations, config):
    """notification queue age: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.queue_age_minutes(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}


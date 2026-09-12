"""Slateharbor jobs: synthetic local policy sample, no external services."""
from jobs.src import policy

def summarize_lease_seconds(observations, config):
    """worker lease recovery: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.lease_seconds(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_max_parallel(observations, config):
    """worker concurrency: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.max_parallel(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_checkpoint_seconds(observations, config):
    """checkpoint interval: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.checkpoint_seconds(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_retry_budget(observations, config):
    """job retry budget: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.retry_budget(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_shutdown_seconds(observations, config):
    """worker shutdown grace: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.shutdown_seconds(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_backlog_limit(observations, config):
    """job admission: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.backlog_limit(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}


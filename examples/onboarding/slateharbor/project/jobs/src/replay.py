"""Slateharbor jobs: synthetic local policy sample, no external services."""
from jobs.src import policy

def replay_lease_seconds(fixtures, config):
    """Replay worker lease recovery input examples; no real requests are sent."""
    case = fixtures['lease_seconds']
    return [policy.lease_seconds(value, config) for value in case['inputs']]

def replay_max_parallel(fixtures, config):
    """Replay worker concurrency input examples; no real requests are sent."""
    case = fixtures['max_parallel']
    return [policy.max_parallel(value, config) for value in case['inputs']]

def replay_checkpoint_seconds(fixtures, config):
    """Replay checkpoint interval input examples; no real requests are sent."""
    case = fixtures['checkpoint_seconds']
    return [policy.checkpoint_seconds(value, config) for value in case['inputs']]

def replay_retry_budget(fixtures, config):
    """Replay job retry budget input examples; no real requests are sent."""
    case = fixtures['retry_budget']
    return [policy.retry_budget(value, config) for value in case['inputs']]

def replay_shutdown_seconds(fixtures, config):
    """Replay worker shutdown grace input examples; no real requests are sent."""
    case = fixtures['shutdown_seconds']
    return [policy.shutdown_seconds(value, config) for value in case['inputs']]

def replay_backlog_limit(fixtures, config):
    """Replay job admission input examples; no real requests are sent."""
    case = fixtures['backlog_limit']
    return [policy.backlog_limit(value, config) for value in case['inputs']]


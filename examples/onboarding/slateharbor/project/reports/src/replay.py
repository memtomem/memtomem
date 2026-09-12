"""Slateharbor reports: synthetic local policy sample, no external services."""
from reports.src import policy

def replay_freshness_minutes(fixtures, config):
    """Replay report freshness input examples; no real requests are sent."""
    case = fixtures['freshness_minutes']
    return [policy.freshness_minutes(value, config) for value in case['inputs']]

def replay_export_rows(fixtures, config):
    """Replay CSV export rows input examples; no real requests are sent."""
    case = fixtures['export_rows']
    return [policy.export_rows(value, config) for value in case['inputs']]

def replay_window_days(fixtures, config):
    """Replay report window input examples; no real requests are sent."""
    case = fixtures['window_days']
    return [policy.window_days(value, config) for value in case['inputs']]

def replay_cache_seconds(fixtures, config):
    """Replay report cache ttl input examples; no real requests are sent."""
    case = fixtures['cache_seconds']
    return [policy.cache_seconds(value, config) for value in case['inputs']]

def replay_minimum_group(fixtures, config):
    """Replay aggregate group privacy input examples; no real requests are sent."""
    case = fixtures['minimum_group']
    return [policy.minimum_group(value, config) for value in case['inputs']]

def replay_query_seconds(fixtures, config):
    """Replay report query timeout input examples; no real requests are sent."""
    case = fixtures['query_seconds']
    return [policy.query_seconds(value, config) for value in case['inputs']]


"""Slateharbor reports: synthetic local policy sample, no external services."""
from reports.src import policy

def summarize_freshness_minutes(observations, config):
    """report freshness: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.freshness_minutes(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_export_rows(observations, config):
    """CSV export rows: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.export_rows(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_window_days(observations, config):
    """report window: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.window_days(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_cache_seconds(observations, config):
    """report cache ttl: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.cache_seconds(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_minimum_group(observations, config):
    """aggregate group privacy: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.minimum_group(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_query_seconds(observations, config):
    """report query timeout: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.query_seconds(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}


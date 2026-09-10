"""Slateharbor reports: synthetic local policy sample, no external services."""
from copy import deepcopy

def configure_freshness_minutes(config, value):
    """Validate and copy report freshness; production value is 15."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid freshness_minutes')
    updated = deepcopy(config)
    updated['freshness_minutes']['value'] = value
    return updated

def configure_export_rows(config, value):
    """Validate and copy CSV export rows; production value is 10000."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid export_rows')
    updated = deepcopy(config)
    updated['export_rows']['value'] = value
    return updated

def configure_window_days(config, value):
    """Validate and copy report window; production value is 90."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid window_days')
    updated = deepcopy(config)
    updated['window_days']['value'] = value
    return updated

def configure_cache_seconds(config, value):
    """Validate and copy report cache ttl; production value is 300."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid cache_seconds')
    updated = deepcopy(config)
    updated['cache_seconds']['value'] = value
    return updated

def configure_minimum_group(config, value):
    """Validate and copy aggregate group privacy; production value is 5."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid minimum_group')
    updated = deepcopy(config)
    updated['minimum_group']['value'] = value
    return updated

def configure_query_seconds(config, value):
    """Validate and copy report query timeout; production value is 8."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid query_seconds')
    updated = deepcopy(config)
    updated['query_seconds']['value'] = value
    return updated


"""Slateharbor jobs: synthetic local policy sample, no external services."""
from copy import deepcopy

def configure_lease_seconds(config, value):
    """Validate and copy worker lease recovery; production value is 90."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid lease_seconds')
    updated = deepcopy(config)
    updated['lease_seconds']['value'] = value
    return updated

def configure_max_parallel(config, value):
    """Validate and copy worker concurrency; production value is 4."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid max_parallel')
    updated = deepcopy(config)
    updated['max_parallel']['value'] = value
    return updated

def configure_checkpoint_seconds(config, value):
    """Validate and copy checkpoint interval; production value is 20."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid checkpoint_seconds')
    updated = deepcopy(config)
    updated['checkpoint_seconds']['value'] = value
    return updated

def configure_retry_budget(config, value):
    """Validate and copy job retry budget; production value is 2."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid retry_budget')
    updated = deepcopy(config)
    updated['retry_budget']['value'] = value
    return updated

def configure_shutdown_seconds(config, value):
    """Validate and copy worker shutdown grace; production value is 45."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid shutdown_seconds')
    updated = deepcopy(config)
    updated['shutdown_seconds']['value'] = value
    return updated

def configure_backlog_limit(config, value):
    """Validate and copy job admission; production value is 200."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid backlog_limit')
    updated = deepcopy(config)
    updated['backlog_limit']['value'] = value
    return updated


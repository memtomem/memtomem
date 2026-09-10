"""Slateharbor auth: synthetic local policy sample, no external services."""
from copy import deepcopy

def configure_legacy_callback(config, value):
    """Validate and copy legacy callback; production value is True."""
    if type(value) is not bool:
        raise ValueError('Invalid legacy_callback')
    updated = deepcopy(config)
    updated['legacy_callback']['value'] = value
    return updated

def configure_session_minutes(config, value):
    """Validate and copy session expiry; production value is 60."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid session_minutes')
    updated = deepcopy(config)
    updated['session_minutes']['value'] = value
    return updated

def configure_login_attempts(config, value):
    """Validate and copy login throttle; production value is 5."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid login_attempts')
    updated = deepcopy(config)
    updated['login_attempts']['value'] = value
    return updated

def configure_invite_hours(config, value):
    """Validate and copy invite validity; production value is 48."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid invite_hours')
    updated = deepcopy(config)
    updated['invite_hours']['value'] = value
    return updated

def configure_clock_skew_seconds(config, value):
    """Validate and copy clock skew; production value is 30."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid clock_skew_seconds')
    updated = deepcopy(config)
    updated['clock_skew_seconds']['value'] = value
    return updated

def configure_recovery_codes(config, value):
    """Validate and copy recovery inventory; production value is 8."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid recovery_codes')
    updated = deepcopy(config)
    updated['recovery_codes']['value'] = value
    return updated


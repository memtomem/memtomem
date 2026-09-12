"""Slateharbor auth: synthetic local policy sample, no external services."""
from auth.src import policy

def summarize_legacy_callback(observations, config):
    """legacy callback: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.legacy_callback(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_session_minutes(observations, config):
    """session expiry: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.session_minutes(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_login_attempts(observations, config):
    """login throttle: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.login_attempts(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_invite_hours(observations, config):
    """invite validity: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.invite_hours(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_clock_skew_seconds(observations, config):
    """clock skew: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.clock_skew_seconds(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_recovery_codes(observations, config):
    """recovery inventory: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.recovery_codes(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}


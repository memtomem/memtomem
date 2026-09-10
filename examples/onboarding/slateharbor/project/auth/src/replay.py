"""Slateharbor auth: synthetic local policy sample, no external services."""
from auth.src import policy

def replay_legacy_callback(fixtures, config):
    """Replay legacy callback input examples; no real requests are sent."""
    case = fixtures['legacy_callback']
    return [policy.legacy_callback(value, config) for value in case['inputs']]

def replay_session_minutes(fixtures, config):
    """Replay session expiry input examples; no real requests are sent."""
    case = fixtures['session_minutes']
    return [policy.session_minutes(value, config) for value in case['inputs']]

def replay_login_attempts(fixtures, config):
    """Replay login throttle input examples; no real requests are sent."""
    case = fixtures['login_attempts']
    return [policy.login_attempts(value, config) for value in case['inputs']]

def replay_invite_hours(fixtures, config):
    """Replay invite validity input examples; no real requests are sent."""
    case = fixtures['invite_hours']
    return [policy.invite_hours(value, config) for value in case['inputs']]

def replay_clock_skew_seconds(fixtures, config):
    """Replay clock skew input examples; no real requests are sent."""
    case = fixtures['clock_skew_seconds']
    return [policy.clock_skew_seconds(value, config) for value in case['inputs']]

def replay_recovery_codes(fixtures, config):
    """Replay recovery inventory input examples; no real requests are sent."""
    case = fixtures['recovery_codes']
    return [policy.recovery_codes(value, config) for value in case['inputs']]


"""Slateharbor auth: synthetic local policy sample, no external services."""
from auth.src import policy

def evaluate_legacy_callback(legacy_client, config):
    """구형 클라이언트 cohort의 callback 404를 확인하고 legacy route를 유지한다. V2 문제는 AUTH_CALLBACK_V2_ENABLED를 false로 되돌려 분리한다."""
    return {'policy': 'legacy_callback', 'allowed': policy.legacy_callback(legacy_client, config),
            'revision': config['legacy_callback']['revision'], 'observed': legacy_client}

def evaluate_session_minutes(age_minutes, config):
    """idle 시간과 전체 세션 나이를 각각 확인하고 만료 시 재인증한다."""
    return {'policy': 'session_minutes', 'allowed': policy.session_minutes(age_minutes, config),
            'revision': config['session_minutes']['revision'], 'observed': age_minutes}

def evaluate_login_attempts(attempts, config):
    """계정과 시간창을 함께 확인하고 IP 전체 차단을 피한다."""
    return {'policy': 'login_attempts', 'allowed': policy.login_attempts(attempts, config),
            'revision': config['login_attempts']['revision'], 'observed': attempts}

def evaluate_invite_hours(age_hours, config):
    """초대 생성 시각과 회수 여부를 조회한 뒤 새 초대를 발급한다."""
    return {'policy': 'invite_hours', 'allowed': policy.invite_hours(age_hours, config),
            'revision': config['invite_hours']['revision'], 'observed': age_hours}

def evaluate_clock_skew_seconds(offset_seconds, config):
    """서버 시간 편차를 측정하고 NTP 복구 후 인증 오류를 재확인한다."""
    return {'policy': 'clock_skew_seconds', 'allowed': policy.clock_skew_seconds(offset_seconds, config),
            'revision': config['clock_skew_seconds']['revision'], 'observed': offset_seconds}

def evaluate_recovery_codes(remaining, config):
    """남은 코드 수 분포와 재발급 요청 건수를 함께 확인한다. 소진이 가까운 사용자에게 재발급을 먼저 안내한다."""
    return {'policy': 'recovery_codes', 'allowed': policy.recovery_codes(remaining, config),
            'revision': config['recovery_codes']['revision'], 'observed': remaining}


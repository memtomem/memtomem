"""Slateharbor auth: synthetic local policy sample, no external services."""
from dataclasses import dataclass

@dataclass(frozen=True)
class LegacyCallbackRequest:
    """legacy callback: 데스크톱 2.8 클라이언트가 /api/auth/legacy-callback을 계속 호출한다. AUTH_CALLBACK_V2_ENABLED만으로 구형 경로를 제거하면 재로그인이 불가능하다."""
    legacy_client: bool
    environment: str = 'production'

@dataclass(frozen=True)
class SessionMinutesRequest:
    """session expiry: 공용 단말에서 열린 세션이 다음 교대조까지 남지 않게 한다. 개인 기기의 refresh token 정책과 혼동하지 않는다."""
    age_minutes: int
    environment: str = 'production'

@dataclass(frozen=True)
class LoginAttemptsRequest:
    """login throttle: 계정 단위 공격을 늦추되 회사 NAT 뒤의 다른 사용자를 함께 차단하지 않는다."""
    attempts: int
    environment: str = 'production'

@dataclass(frozen=True)
class InviteHoursRequest:
    """invite validity: 전달된 오래된 초대 링크가 조직 변경 이후 재사용되지 않게 한다."""
    age_hours: int
    environment: str = 'production'

@dataclass(frozen=True)
class ClockSkewSecondsRequest:
    """clock skew: 작은 시계 오차는 허용하지만 지나친 허용폭으로 만료 검증을 무력화하지 않는다."""
    offset_seconds: int
    environment: str = 'production'

@dataclass(frozen=True)
class RecoveryCodesRequest:
    """recovery inventory: 복구 코드는 한 번만 쓰며 재발급 시 기존 묶음을 모두 폐기한다. 한 묶음은 8개로 발급해 소진으로 인한 잠금을 줄인다."""
    remaining: int
    environment: str = 'production'


---
tags: [slateharbor, auth, current]
---
# Slateharbor 인증: 동작 계약

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## legacy callback

입력: `legacy_client`. `legacy callback`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `legacy_callback`입니다.

승인 예: `True`. 거절 예: `False`. 기준은 [운영 설정](../../config/production.json)의 `True`입니다.

업무 의미: 데스크톱 2.8 클라이언트가 /api/auth/legacy-callback을 계속 호출한다. AUTH_CALLBACK_V2_ENABLED만으로 구형 경로를 제거하면 재로그인이 불가능하다.

잘못된 적용 사례: INC-AUTH-17에서 신규 웹 로그인은 정상인데 구형 데스크톱만 callback 404가 발생했다. 웹 성공률 대시보드에는 장애가 드러나지 않았다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## session expiry

입력: `age_minutes`. `session expiry`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `session_minutes`입니다.

승인 예: `30`. 거절 예: `90`. 기준은 [운영 설정](../../config/production.json)의 `60`입니다.

업무 의미: 공용 단말에서 열린 세션이 다음 교대조까지 남지 않게 한다. 개인 기기의 refresh token 정책과 혼동하지 않는다.

잘못된 적용 사례: 교대 종료 후 같은 브라우저를 쓰던 다른 직원에게 이전 tenant 화면이 노출됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## login throttle

입력: `attempts`. `login throttle`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `login_attempts`입니다.

승인 예: `2`. 거절 예: `8`. 기준은 [운영 설정](../../config/production.json)의 `5`입니다.

업무 의미: 계정 단위 공격을 늦추되 회사 NAT 뒤의 다른 사용자를 함께 차단하지 않는다.

잘못된 적용 사례: 계정당 10회를 허용하는 동안 자동화 도구가 유효 계정 목록을 추려냈고, IP 단위로 막자 같은 사무실의 정상 로그인까지 함께 차단됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## invite validity

입력: `age_hours`. `invite validity`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `invite_hours`입니다.

승인 예: `12`. 거절 예: `72`. 기준은 [운영 설정](../../config/production.json)의 `48`입니다.

업무 의미: 전달된 오래된 초대 링크가 조직 변경 이후 재사용되지 않게 한다.

잘못된 적용 사례: 회수된 초대 메일의 링크가 일주일 뒤에도 사용됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## clock skew

입력: `offset_seconds`. `clock skew`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `clock_skew_seconds`입니다.

승인 예: `10`. 거절 예: `90`. 기준은 [운영 설정](../../config/production.json)의 `30`입니다.

업무 의미: 작은 시계 오차는 허용하지만 지나친 허용폭으로 만료 검증을 무력화하지 않는다.

잘못된 적용 사례: 시간 동기화가 끊긴 worker가 만료된 세션을 허용했다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## recovery inventory

입력: `remaining`. `recovery inventory`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `recovery_codes`입니다.

승인 예: `3`. 거절 예: `0`. 기준은 [운영 설정](../../config/production.json)의 `8`입니다.

업무 의미: 복구 코드는 한 번만 쓰며 재발급 시 기존 묶음을 모두 폐기한다. 한 묶음은 8개로 발급해 소진으로 인한 잠금을 줄인다.

잘못된 적용 사례: 복구 코드 4개를 모두 소진한 사용자가 재발급 전까지 로그인하지 못해 지원팀 수동 처리로 몰렸다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

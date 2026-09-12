---
tags: [slateharbor, auth, current]
---
# Slateharbor 인증: 복구 절차

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## legacy callback

시작 조건: INC-AUTH-17에서 신규 웹 로그인은 정상인데 구형 데스크톱만 callback 404가 발생했다. 웹 성공률 대시보드에는 장애가 드러나지 않았다.

1. 구형 클라이언트 cohort의 callback 404를 확인하고 legacy route를 유지한다. V2 문제는 AUTH_CALLBACK_V2_ENABLED를 false로 되돌려 분리한다.

2. [운영 설정](../../config/production.json)에서 `legacy callback` / `legacy_callback`의 현재 값 `True`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 데스크톱 2.8 클라이언트가 /api/auth/legacy-callback을 계속 호출한다. AUTH_CALLBACK_V2_ENABLED만으로 구형 경로를 제거하면 재로그인이 불가능하다.

## session expiry

시작 조건: 교대 종료 후 같은 브라우저를 쓰던 다른 직원에게 이전 tenant 화면이 노출됐다.

1. idle 시간과 전체 세션 나이를 각각 확인하고 만료 시 재인증한다.

2. [운영 설정](../../config/production.json)에서 `session expiry` / `session_minutes`의 현재 값 `60`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 공용 단말에서 열린 세션이 다음 교대조까지 남지 않게 한다. 개인 기기의 refresh token 정책과 혼동하지 않는다.

## login throttle

시작 조건: 계정당 10회를 허용하는 동안 자동화 도구가 유효 계정 목록을 추려냈고, IP 단위로 막자 같은 사무실의 정상 로그인까지 함께 차단됐다.

1. 계정과 시간창을 함께 확인하고 IP 전체 차단을 피한다.

2. [운영 설정](../../config/production.json)에서 `login throttle` / `login_attempts`의 현재 값 `5`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 계정 단위 공격을 늦추되 회사 NAT 뒤의 다른 사용자를 함께 차단하지 않는다.

## invite validity

시작 조건: 회수된 초대 메일의 링크가 일주일 뒤에도 사용됐다.

1. 초대 생성 시각과 회수 여부를 조회한 뒤 새 초대를 발급한다.

2. [운영 설정](../../config/production.json)에서 `invite validity` / `invite_hours`의 현재 값 `48`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 전달된 오래된 초대 링크가 조직 변경 이후 재사용되지 않게 한다.

## clock skew

시작 조건: 시간 동기화가 끊긴 worker가 만료된 세션을 허용했다.

1. 서버 시간 편차를 측정하고 NTP 복구 후 인증 오류를 재확인한다.

2. [운영 설정](../../config/production.json)에서 `clock skew` / `clock_skew_seconds`의 현재 값 `30`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 작은 시계 오차는 허용하지만 지나친 허용폭으로 만료 검증을 무력화하지 않는다.

## recovery inventory

시작 조건: 복구 코드 4개를 모두 소진한 사용자가 재발급 전까지 로그인하지 못해 지원팀 수동 처리로 몰렸다.

1. 남은 코드 수 분포와 재발급 요청 건수를 함께 확인한다. 소진이 가까운 사용자에게 재발급을 먼저 안내한다.

2. [운영 설정](../../config/production.json)에서 `recovery inventory` / `recovery_codes`의 현재 값 `8`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 복구 코드는 한 번만 쓰며 재발급 시 기존 묶음을 모두 폐기한다. 한 묶음은 8개로 발급해 소진으로 인한 잠금을 줄인다.

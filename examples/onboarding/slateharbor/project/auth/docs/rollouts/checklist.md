---
tags: [slateharbor, auth, current]
---
# Slateharbor 인증: 단계 배포 점검

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## legacy callback

변경 대상: `legacy callback`의 `legacy_callback`. 이전 `False`, 현재 `True`.

배포 전: 구형 클라이언트 cohort의 callback 404를 확인하고 legacy route를 유지한다. V2 문제는 AUTH_CALLBACK_V2_ENABLED를 false로 되돌려 분리한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: INC-AUTH-17에서 신규 웹 로그인은 정상인데 구형 데스크톱만 callback 404가 발생했다. 웹 성공률 대시보드에는 장애가 드러나지 않았다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 데스크톱 2.8 클라이언트가 /api/auth/legacy-callback을 계속 호출한다. AUTH_CALLBACK_V2_ENABLED만으로 구형 경로를 제거하면 재로그인이 불가능하다.

배포 후 담당 작업: TASK-AUTH-42: 데스크톱 cohort별 사용량 관찰을 추가한다. 삭제 PR은 아직 승인되지 않았다.

## session expiry

변경 대상: `session expiry`의 `session_minutes`. 이전 `120`, 현재 `60`.

배포 전: idle 시간과 전체 세션 나이를 각각 확인하고 만료 시 재인증한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 교대 종료 후 같은 브라우저를 쓰던 다른 직원에게 이전 tenant 화면이 노출됐다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 공용 단말에서 열린 세션이 다음 교대조까지 남지 않게 한다. 개인 기기의 refresh token 정책과 혼동하지 않는다.

배포 후 담당 작업: TASK-AUTH-43: session age 경계값을 프런트 안내 문구와 맞춘다.

## login throttle

변경 대상: `login throttle`의 `login_attempts`. 이전 `10`, 현재 `5`.

배포 전: 계정과 시간창을 함께 확인하고 IP 전체 차단을 피한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 계정당 10회를 허용하는 동안 자동화 도구가 유효 계정 목록을 추려냈고, IP 단위로 막자 같은 사무실의 정상 로그인까지 함께 차단됐다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 계정 단위 공격을 늦추되 회사 NAT 뒤의 다른 사용자를 함께 차단하지 않는다.

배포 후 담당 작업: TASK-AUTH-44: 계정별 throttle 로그에 원인 코드를 추가한다.

## invite validity

변경 대상: `invite validity`의 `invite_hours`. 이전 `168`, 현재 `48`.

배포 전: 초대 생성 시각과 회수 여부를 조회한 뒤 새 초대를 발급한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 회수된 초대 메일의 링크가 일주일 뒤에도 사용됐다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 전달된 오래된 초대 링크가 조직 변경 이후 재사용되지 않게 한다.

배포 후 담당 작업: TASK-AUTH-45: 만료와 회수 상태를 초대 화면에서 구분한다.

## clock skew

변경 대상: `clock skew`의 `clock_skew_seconds`. 이전 `120`, 현재 `30`.

배포 전: 서버 시간 편차를 측정하고 NTP 복구 후 인증 오류를 재확인한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 시간 동기화가 끊긴 worker가 만료된 세션을 허용했다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 작은 시계 오차는 허용하지만 지나친 허용폭으로 만료 검증을 무력화하지 않는다.

배포 후 담당 작업: TASK-AUTH-46: 시계 편차 경보를 readiness와 분리한다.

## recovery inventory

변경 대상: `recovery inventory`의 `recovery_codes`. 이전 `4`, 현재 `8`.

배포 전: 남은 코드 수 분포와 재발급 요청 건수를 함께 확인한다. 소진이 가까운 사용자에게 재발급을 먼저 안내한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 복구 코드 4개를 모두 소진한 사용자가 재발급 전까지 로그인하지 못해 지원팀 수동 처리로 몰렸다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 복구 코드는 한 번만 쓰며 재발급 시 기존 묶음을 모두 폐기한다. 한 묶음은 8개로 발급해 소진으로 인한 잠금을 줄인다.

배포 후 담당 작업: TASK-AUTH-47: 남은 코드 수가 임계값 아래로 내려가면 재발급을 안내하는 경고를 추가한다.

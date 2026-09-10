---
tags: [slateharbor, auth, proposed]
---
# Slateharbor 인증: 검토 중 제안

> 합성 프로젝트 기록. 상태: proposed. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## legacy callback

상태: proposed. `legacy callback` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: INC-AUTH-17에서 신규 웹 로그인은 정상인데 구형 데스크톱만 callback 404가 발생했다. 웹 성공률 대시보드에는 장애가 드러나지 않았다.

제안: TASK-AUTH-42: 데스크톱 cohort별 사용량 관찰을 추가한다. 삭제 PR은 아직 승인되지 않았다.

현재 계약: `legacy_callback` = `True`. 데스크톱 2.8 클라이언트가 /api/auth/legacy-callback을 계속 호출한다. AUTH_CALLBACK_V2_ENABLED만으로 구형 경로를 제거하면 재로그인이 불가능하다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## session expiry

상태: proposed. `session expiry` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 교대 종료 후 같은 브라우저를 쓰던 다른 직원에게 이전 tenant 화면이 노출됐다.

제안: TASK-AUTH-43: session age 경계값을 프런트 안내 문구와 맞춘다.

현재 계약: `session_minutes` = `60`. 공용 단말에서 열린 세션이 다음 교대조까지 남지 않게 한다. 개인 기기의 refresh token 정책과 혼동하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## login throttle

상태: proposed. `login throttle` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 계정당 10회를 허용하는 동안 자동화 도구가 유효 계정 목록을 추려냈고, IP 단위로 막자 같은 사무실의 정상 로그인까지 함께 차단됐다.

제안: TASK-AUTH-44: 계정별 throttle 로그에 원인 코드를 추가한다.

현재 계약: `login_attempts` = `5`. 계정 단위 공격을 늦추되 회사 NAT 뒤의 다른 사용자를 함께 차단하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## invite validity

상태: proposed. `invite validity` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 회수된 초대 메일의 링크가 일주일 뒤에도 사용됐다.

제안: TASK-AUTH-45: 만료와 회수 상태를 초대 화면에서 구분한다.

현재 계약: `invite_hours` = `48`. 전달된 오래된 초대 링크가 조직 변경 이후 재사용되지 않게 한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## clock skew

상태: proposed. `clock skew` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 시간 동기화가 끊긴 worker가 만료된 세션을 허용했다.

제안: TASK-AUTH-46: 시계 편차 경보를 readiness와 분리한다.

현재 계약: `clock_skew_seconds` = `30`. 작은 시계 오차는 허용하지만 지나친 허용폭으로 만료 검증을 무력화하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## recovery inventory

상태: proposed. `recovery inventory` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 복구 코드 4개를 모두 소진한 사용자가 재발급 전까지 로그인하지 못해 지원팀 수동 처리로 몰렸다.

제안: TASK-AUTH-47: 남은 코드 수가 임계값 아래로 내려가면 재발급을 안내하는 경고를 추가한다.

현재 계약: `recovery_codes` = `8`. 복구 코드는 한 번만 쓰며 재발급 시 기존 묶음을 모두 폐기한다. 한 묶음은 8개로 발급해 소진으로 인한 잠금을 줄인다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

---
tags: [slateharbor, auth, current]
---
# Slateharbor 인증: 작업 인계

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## legacy callback

작업: TASK-AUTH-42: 데스크톱 cohort별 사용량 관찰을 추가한다. 삭제 PR은 아직 승인되지 않았다.

이미 확정: `legacy callback` = `True`. 데스크톱 2.8 클라이언트가 /api/auth/legacy-callback을 계속 호출한다. AUTH_CALLBACK_V2_ENABLED만으로 구형 경로를 제거하면 재로그인이 불가능하다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 구형 클라이언트 cohort의 callback 404를 확인하고 legacy route를 유지한다. V2 문제는 AUTH_CALLBACK_V2_ENABLED를 false로 되돌려 분리한다.

보류 사항: 원인 확인 없이 이전 `False`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## session expiry

작업: TASK-AUTH-43: session age 경계값을 프런트 안내 문구와 맞춘다.

이미 확정: `session expiry` = `60`. 공용 단말에서 열린 세션이 다음 교대조까지 남지 않게 한다. 개인 기기의 refresh token 정책과 혼동하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: idle 시간과 전체 세션 나이를 각각 확인하고 만료 시 재인증한다.

보류 사항: 원인 확인 없이 이전 `120`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## login throttle

작업: TASK-AUTH-44: 계정별 throttle 로그에 원인 코드를 추가한다.

이미 확정: `login throttle` = `5`. 계정 단위 공격을 늦추되 회사 NAT 뒤의 다른 사용자를 함께 차단하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 계정과 시간창을 함께 확인하고 IP 전체 차단을 피한다.

보류 사항: 원인 확인 없이 이전 `10`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## invite validity

작업: TASK-AUTH-45: 만료와 회수 상태를 초대 화면에서 구분한다.

이미 확정: `invite validity` = `48`. 전달된 오래된 초대 링크가 조직 변경 이후 재사용되지 않게 한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 초대 생성 시각과 회수 여부를 조회한 뒤 새 초대를 발급한다.

보류 사항: 원인 확인 없이 이전 `168`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## clock skew

작업: TASK-AUTH-46: 시계 편차 경보를 readiness와 분리한다.

이미 확정: `clock skew` = `30`. 작은 시계 오차는 허용하지만 지나친 허용폭으로 만료 검증을 무력화하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 서버 시간 편차를 측정하고 NTP 복구 후 인증 오류를 재확인한다.

보류 사항: 원인 확인 없이 이전 `120`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## recovery inventory

작업: TASK-AUTH-47: 남은 코드 수가 임계값 아래로 내려가면 재발급을 안내하는 경고를 추가한다.

이미 확정: `recovery inventory` = `8`. 복구 코드는 한 번만 쓰며 재발급 시 기존 묶음을 모두 폐기한다. 한 묶음은 8개로 발급해 소진으로 인한 잠금을 줄인다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 남은 코드 수 분포와 재발급 요청 건수를 함께 확인한다. 소진이 가까운 사용자에게 재발급을 먼저 안내한다.

보류 사항: 원인 확인 없이 이전 `4`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

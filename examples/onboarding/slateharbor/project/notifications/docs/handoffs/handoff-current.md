---
tags: [slateharbor, notifications, current]
---
# Slateharbor 알림: 작업 인계

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

작업: TASK-NOTIFY-57: jitter 분포와 delivery key 유지 테스트를 추가한다. retries를 다시 12로 올리지 않는다.

이미 확정: `notification retry policy` = `5`. 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: retry queue 깊이와 429 비율을 확인한다. 전송은 잠시 늦추되 delivery key를 새로 만들지 않는다.

보류 사항: 원인 확인 없이 이전 `12`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## notification backoff

작업: TASK-NOTIFY-58: seed를 고정한 jitter 단위 테스트를 넣는다.

이미 확정: `notification backoff` = `250`. 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 실제 대기 히스토그램을 보고 동일 간격에 요청이 몰리는지 확인한다.

보류 사항: 원인 확인 없이 이전 `50`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## digest batch

작업: TASK-NOTIFY-59: 공정성 테스트에 대형 tenant를 추가한다.

이미 확정: `digest batch` = `40`. digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: tenant별 대기 시간을 확인하고 digest와 실시간 전송을 별도 큐로 분리한다.

보류 사항: 원인 확인 없이 이전 `100`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## delivery deduplication

작업: TASK-NOTIFY-60: TTL 경계의 이중 전송 검사를 추가한다.

이미 확정: `delivery deduplication` = `24`. 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 전송 이력의 key와 TTL을 조회하고 이미 성공한 응답을 반환한다.

보류 사항: 원인 확인 없이 이전 `6`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## email payload

작업: TASK-NOTIFY-61: 멀티바이트 본문의 byte 길이를 측정한다.

이미 확정: `email payload` = `128`. 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 렌더링 후 payload 크기를 측정하고 링크 전환 후 다시 요청한다.

보류 사항: 원인 확인 없이 이전 `256`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## notification queue age

작업: TASK-NOTIFY-62: dead-letter 이동 이유를 인계 화면에 노출한다.

이미 확정: `notification queue age` = `15`. 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 가장 오래된 event 시각을 확인하고 poison message를 격리한다.

보류 사항: 원인 확인 없이 이전 `60`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

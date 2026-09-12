---
tags: [slateharbor, notifications, proposed]
---
# Slateharbor 알림: 검토 중 제안

> 합성 프로젝트 기록. 상태: proposed. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

상태: proposed. `notification retry policy` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

제안: TASK-NOTIFY-57: jitter 분포와 delivery key 유지 테스트를 추가한다. retries를 다시 12로 올리지 않는다.

현재 계약: `retry_attempts` = `5`. 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## notification backoff

상태: proposed. `notification backoff` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

제안: TASK-NOTIFY-58: seed를 고정한 jitter 단위 테스트를 넣는다.

현재 계약: `backoff_ms` = `250`. 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## digest batch

상태: proposed. `digest batch` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

제안: TASK-NOTIFY-59: 공정성 테스트에 대형 tenant를 추가한다.

현재 계약: `batch_size` = `40`. digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## delivery deduplication

상태: proposed. `delivery deduplication` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

제안: TASK-NOTIFY-60: TTL 경계의 이중 전송 검사를 추가한다.

현재 계약: `dedupe_hours` = `24`. 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## email payload

상태: proposed. `email payload` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

제안: TASK-NOTIFY-61: 멀티바이트 본문의 byte 길이를 측정한다.

현재 계약: `payload_kb` = `128`. 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## notification queue age

상태: proposed. `notification queue age` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

제안: TASK-NOTIFY-62: dead-letter 이동 이유를 인계 화면에 노출한다.

현재 계약: `queue_age_minutes` = `15`. 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

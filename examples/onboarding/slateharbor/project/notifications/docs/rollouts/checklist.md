---
tags: [slateharbor, notifications, current]
---
# Slateharbor 알림: 단계 배포 점검

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

변경 대상: `notification retry policy`의 `retry_attempts`. 이전 `12`, 현재 `5`.

배포 전: retry queue 깊이와 429 비율을 확인한다. 전송은 잠시 늦추되 delivery key를 새로 만들지 않는다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다.

배포 후 담당 작업: TASK-NOTIFY-57: jitter 분포와 delivery key 유지 테스트를 추가한다. retries를 다시 12로 올리지 않는다.

## notification backoff

변경 대상: `notification backoff`의 `backoff_ms`. 이전 `50`, 현재 `250`.

배포 전: 실제 대기 히스토그램을 보고 동일 간격에 요청이 몰리는지 확인한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다.

배포 후 담당 작업: TASK-NOTIFY-58: seed를 고정한 jitter 단위 테스트를 넣는다.

## digest batch

변경 대상: `digest batch`의 `batch_size`. 이전 `100`, 현재 `40`.

배포 전: tenant별 대기 시간을 확인하고 digest와 실시간 전송을 별도 큐로 분리한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다.

배포 후 담당 작업: TASK-NOTIFY-59: 공정성 테스트에 대형 tenant를 추가한다.

## delivery deduplication

변경 대상: `delivery deduplication`의 `dedupe_hours`. 이전 `6`, 현재 `24`.

배포 전: 전송 이력의 key와 TTL을 조회하고 이미 성공한 응답을 반환한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다.

배포 후 담당 작업: TASK-NOTIFY-60: TTL 경계의 이중 전송 검사를 추가한다.

## email payload

변경 대상: `email payload`의 `payload_kb`. 이전 `256`, 현재 `128`.

배포 전: 렌더링 후 payload 크기를 측정하고 링크 전환 후 다시 요청한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다.

배포 후 담당 작업: TASK-NOTIFY-61: 멀티바이트 본문의 byte 길이를 측정한다.

## notification queue age

변경 대상: `notification queue age`의 `queue_age_minutes`. 이전 `60`, 현재 `15`.

배포 전: 가장 오래된 event 시각을 확인하고 poison message를 격리한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다.

배포 후 담당 작업: TASK-NOTIFY-62: dead-letter 이동 이유를 인계 화면에 노출한다.

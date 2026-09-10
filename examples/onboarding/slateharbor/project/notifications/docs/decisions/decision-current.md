---
tags: [slateharbor, notifications, current]
---
# Slateharbor 알림: 채택 결정

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

결정 NOTIFICATIONS-01: `notification retry policy`의 현재 값은 `5`입니다.

선택 이유: 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다.

대안 검토: 이전 값 `12`는 아래 장애 조건을 만족하지 못했습니다. INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

적용: [정책 구현](../../src/policy.py)의 `retry_attempts`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: retry queue 깊이와 429 비율을 확인한다. 전송은 잠시 늦추되 delivery key를 새로 만들지 않는다.

## notification backoff

결정 NOTIFICATIONS-02: `notification backoff`의 현재 값은 `250`입니다.

선택 이유: 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다.

대안 검토: 이전 값 `50`는 아래 장애 조건을 만족하지 못했습니다. 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

적용: [정책 구현](../../src/policy.py)의 `backoff_ms`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 실제 대기 히스토그램을 보고 동일 간격에 요청이 몰리는지 확인한다.

## digest batch

결정 NOTIFICATIONS-03: `digest batch`의 현재 값은 `40`입니다.

선택 이유: digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다.

대안 검토: 이전 값 `100`는 아래 장애 조건을 만족하지 못했습니다. 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

적용: [정책 구현](../../src/policy.py)의 `batch_size`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: tenant별 대기 시간을 확인하고 digest와 실시간 전송을 별도 큐로 분리한다.

## delivery deduplication

결정 NOTIFICATIONS-04: `delivery deduplication`의 현재 값은 `24`입니다.

선택 이유: 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다.

대안 검토: 이전 값 `6`는 아래 장애 조건을 만족하지 못했습니다. 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

적용: [정책 구현](../../src/policy.py)의 `dedupe_hours`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 전송 이력의 key와 TTL을 조회하고 이미 성공한 응답을 반환한다.

## email payload

결정 NOTIFICATIONS-05: `email payload`의 현재 값은 `128`입니다.

선택 이유: 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다.

대안 검토: 이전 값 `256`는 아래 장애 조건을 만족하지 못했습니다. 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

적용: [정책 구현](../../src/policy.py)의 `payload_kb`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 렌더링 후 payload 크기를 측정하고 링크 전환 후 다시 요청한다.

## notification queue age

결정 NOTIFICATIONS-06: `notification queue age`의 현재 값은 `15`입니다.

선택 이유: 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다.

대안 검토: 이전 값 `60`는 아래 장애 조건을 만족하지 못했습니다. 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

적용: [정책 구현](../../src/policy.py)의 `queue_age_minutes`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 가장 오래된 event 시각을 확인하고 poison message를 격리한다.

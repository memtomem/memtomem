---
tags: [slateharbor, notifications, current]
---
# Slateharbor 알림: 복구 절차

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

시작 조건: INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

1. retry queue 깊이와 429 비율을 확인한다. 전송은 잠시 늦추되 delivery key를 새로 만들지 않는다.

2. [운영 설정](../../config/production.json)에서 `notification retry policy` / `retry_attempts`의 현재 값 `5`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다.

## notification backoff

시작 조건: 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

1. 실제 대기 히스토그램을 보고 동일 간격에 요청이 몰리는지 확인한다.

2. [운영 설정](../../config/production.json)에서 `notification backoff` / `backoff_ms`의 현재 값 `250`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다.

## digest batch

시작 조건: 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

1. tenant별 대기 시간을 확인하고 digest와 실시간 전송을 별도 큐로 분리한다.

2. [운영 설정](../../config/production.json)에서 `digest batch` / `batch_size`의 현재 값 `40`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다.

## delivery deduplication

시작 조건: 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

1. 전송 이력의 key와 TTL을 조회하고 이미 성공한 응답을 반환한다.

2. [운영 설정](../../config/production.json)에서 `delivery deduplication` / `dedupe_hours`의 현재 값 `24`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다.

## email payload

시작 조건: 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

1. 렌더링 후 payload 크기를 측정하고 링크 전환 후 다시 요청한다.

2. [운영 설정](../../config/production.json)에서 `email payload` / `payload_kb`의 현재 값 `128`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다.

## notification queue age

시작 조건: 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

1. 가장 오래된 event 시각을 확인하고 poison message를 격리한다.

2. [운영 설정](../../config/production.json)에서 `notification queue age` / `queue_age_minutes`의 현재 값 `15`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다.

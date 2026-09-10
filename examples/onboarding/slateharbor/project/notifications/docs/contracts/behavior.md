---
tags: [slateharbor, notifications, current]
---
# Slateharbor 알림: 동작 계약

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

입력: `attempts`. `notification retry policy`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `retry_attempts`입니다.

승인 예: `4`. 거절 예: `6`. 기준은 [운영 설정](../../config/production.json)의 `5`입니다.

업무 의미: 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다.

잘못된 적용 사례: INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## notification backoff

입력: `delay_ms`. `notification backoff`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `backoff_ms`입니다.

승인 예: `300`. 거절 예: `100`. 기준은 [운영 설정](../../config/production.json)의 `250`입니다.

업무 의미: 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다.

잘못된 적용 사례: 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## digest batch

입력: `recipients`. `digest batch`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `batch_size`입니다.

승인 예: `20`. 거절 예: `70`. 기준은 [운영 설정](../../config/production.json)의 `40`입니다.

업무 의미: digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다.

잘못된 적용 사례: 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## delivery deduplication

입력: `age_hours`. `delivery deduplication`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `dedupe_hours`입니다.

승인 예: `12`. 거절 예: `30`. 기준은 [운영 설정](../../config/production.json)의 `24`입니다.

업무 의미: 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다.

잘못된 적용 사례: 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## email payload

입력: `size_kb`. `email payload`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `payload_kb`입니다.

승인 예: `64`. 거절 예: `200`. 기준은 [운영 설정](../../config/production.json)의 `128`입니다.

업무 의미: 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다.

잘못된 적용 사례: 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## notification queue age

입력: `age_minutes`. `notification queue age`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `queue_age_minutes`입니다.

승인 예: `5`. 거절 예: `25`. 기준은 [운영 설정](../../config/production.json)의 `15`입니다.

업무 의미: 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다.

잘못된 적용 사례: 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

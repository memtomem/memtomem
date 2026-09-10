---
tags: [slateharbor, notifications, current]
---
# Slateharbor 알림: 8월 변경 기록

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

8월 변경: `notification retry policy` 값이 `12`에서 `5`로 바뀌었습니다.

관련 관찰: INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

사용자에게 달라지는 점: 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다.

확인 절차: retry queue 깊이와 429 비율을 확인한다. 전송은 잠시 늦추되 delivery key를 새로 만들지 않는다.

추적 작업: TASK-NOTIFY-57: jitter 분포와 delivery key 유지 테스트를 추가한다. retries를 다시 12로 올리지 않는다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## notification backoff

8월 변경: `notification backoff` 값이 `50`에서 `250`로 바뀌었습니다.

관련 관찰: 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

사용자에게 달라지는 점: 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다.

확인 절차: 실제 대기 히스토그램을 보고 동일 간격에 요청이 몰리는지 확인한다.

추적 작업: TASK-NOTIFY-58: seed를 고정한 jitter 단위 테스트를 넣는다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## digest batch

8월 변경: `digest batch` 값이 `100`에서 `40`로 바뀌었습니다.

관련 관찰: 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

사용자에게 달라지는 점: digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다.

확인 절차: tenant별 대기 시간을 확인하고 digest와 실시간 전송을 별도 큐로 분리한다.

추적 작업: TASK-NOTIFY-59: 공정성 테스트에 대형 tenant를 추가한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## delivery deduplication

8월 변경: `delivery deduplication` 값이 `6`에서 `24`로 바뀌었습니다.

관련 관찰: 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

사용자에게 달라지는 점: 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다.

확인 절차: 전송 이력의 key와 TTL을 조회하고 이미 성공한 응답을 반환한다.

추적 작업: TASK-NOTIFY-60: TTL 경계의 이중 전송 검사를 추가한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## email payload

8월 변경: `email payload` 값이 `256`에서 `128`로 바뀌었습니다.

관련 관찰: 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

사용자에게 달라지는 점: 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다.

확인 절차: 렌더링 후 payload 크기를 측정하고 링크 전환 후 다시 요청한다.

추적 작업: TASK-NOTIFY-61: 멀티바이트 본문의 byte 길이를 측정한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## notification queue age

8월 변경: `notification queue age` 값이 `60`에서 `15`로 바뀌었습니다.

관련 관찰: 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

사용자에게 달라지는 점: 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다.

확인 절차: 가장 오래된 event 시각을 확인하고 poison message를 격리한다.

추적 작업: TASK-NOTIFY-62: dead-letter 이동 이유를 인계 화면에 노출한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

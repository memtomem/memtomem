---
tags: [slateharbor, notifications, current]
---
# Slateharbor 알림: 지원 진단

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## notification retry policy

문의 유형: `notification retry policy`가 예상과 다르게 동작합니다.

증상: INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

확인 질문: 요청이 production인지 staging인지, `attempts`의 실제 값이 얼마인지 먼저 확인합니다.

진단: retry queue 깊이와 429 비율을 확인한다. 전송은 잠시 늦추되 delivery key를 새로 만들지 않는다.

비교 기준: [현행 설정](../../config/production.json)의 `retry_attempts` = `5`.

설명할 이유: 알림 retry policy는 최대 5회, 250 ms backoff와 jitter다. 수신자에게 중복 메일을 보내지 않도록 delivery key를 유지한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## notification backoff

문의 유형: `notification backoff`가 예상과 다르게 동작합니다.

증상: 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

확인 질문: 요청이 production인지 staging인지, `delay_ms`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 실제 대기 히스토그램을 보고 동일 간격에 요청이 몰리는지 확인한다.

비교 기준: [현행 설정](../../config/production.json)의 `backoff_ms` = `250`.

설명할 이유: 재시도 최소 대기 250 ms 위에 jitter를 더해 worker들의 동시 재진입을 분산한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## digest batch

문의 유형: `digest batch`가 예상과 다르게 동작합니다.

증상: 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

확인 질문: 요청이 production인지 staging인지, `recipients`의 실제 값이 얼마인지 먼저 확인합니다.

진단: tenant별 대기 시간을 확인하고 digest와 실시간 전송을 별도 큐로 분리한다.

비교 기준: [현행 설정](../../config/production.json)의 `batch_size` = `40`.

설명할 이유: digest 묶음을 줄여 특정 tenant의 대량 전송이 나머지 알림을 막지 않게 한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## delivery deduplication

문의 유형: `delivery deduplication`가 예상과 다르게 동작합니다.

증상: 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

확인 질문: 요청이 production인지 staging인지, `age_hours`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 전송 이력의 key와 TTL을 조회하고 이미 성공한 응답을 반환한다.

비교 기준: [현행 설정](../../config/production.json)의 `dedupe_hours` = `24`.

설명할 이유: 하루 이내 같은 delivery key는 재전송 결과를 재사용한다. 새 사용자 행동은 새 key를 쓴다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## email payload

문의 유형: `email payload`가 예상과 다르게 동작합니다.

증상: 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

확인 질문: 요청이 production인지 staging인지, `size_kb`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 렌더링 후 payload 크기를 측정하고 링크 전환 후 다시 요청한다.

비교 기준: [현행 설정](../../config/production.json)의 `payload_kb` = `128`.

설명할 이유: 큰 첨부물은 본문 대신 파일 링크로 전달한다. 파일 권한은 수신 시점에 다시 검사한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## notification queue age

문의 유형: `notification queue age`가 예상과 다르게 동작합니다.

증상: 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

확인 질문: 요청이 production인지 staging인지, `age_minutes`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 가장 오래된 event 시각을 확인하고 poison message를 격리한다.

비교 기준: [현행 설정](../../config/production.json)의 `queue_age_minutes` = `15`.

설명할 이유: 초대·멘션 알림의 지연은 큐 길이보다 가장 오래된 메시지 나이로 감지한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

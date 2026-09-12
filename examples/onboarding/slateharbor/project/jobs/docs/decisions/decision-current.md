---
tags: [slateharbor, jobs, current]
---
# Slateharbor 백그라운드 작업: 채택 결정

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## worker lease recovery

결정 JOBS-01: `worker lease recovery`의 현재 값은 `90`입니다.

선택 이유: worker lease는 90초이며 완료 체크포인트를 먼저 확인한 후 재할당한다. timeout만 보고 같은 작업을 두 번 시작하지 않는다.

대안 검토: 이전 값 `30`는 아래 장애 조건을 만족하지 못했습니다. INC-JOBS-22에서 GC pause로 heartbeat가 늦자 살아 있는 worker의 작업을 다른 worker가 다시 수행했다.

적용: [정책 구현](../../src/policy.py)의 `lease_seconds`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: checkpoint와 lease owner를 함께 읽는다. 완료면 결과를 재사용하고 만료·미완료일 때만 다시 claim한다.

## worker concurrency

결정 JOBS-02: `worker concurrency`의 현재 값은 `4`입니다.

선택 이유: CPU 작업 4개를 넘기지 않아 heartbeat 스레드가 굶지 않게 한다.

대안 검토: 이전 값 `16`는 아래 장애 조건을 만족하지 못했습니다. 과도한 병렬 압축이 이벤트 루프를 막아 lease가 연달아 만료됐다.

적용: [정책 구현](../../src/policy.py)의 `max_parallel`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: CPU와 heartbeat 지연을 함께 보고 동시 작업 수를 낮춘다.

## checkpoint interval

결정 JOBS-03: `checkpoint interval`의 현재 값은 `20`입니다.

선택 이유: 20초마다 재개 가능한 offset을 기록한다. 부분 파일이 durable해진 뒤에만 offset을 전진시킨다.

대안 검토: 이전 값 `60`는 아래 장애 조건을 만족하지 못했습니다. 메모리상의 진행률만 저장해 재시작 후 누락된 출력 구간이 생겼다.

적용: [정책 구현](../../src/policy.py)의 `checkpoint_seconds`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 출력 flush와 checkpoint 순서를 확인하고 마지막 durable offset부터 재개한다.

## job retry budget

결정 JOBS-04: `job retry budget`의 현재 값은 `2`입니다.

선택 이유: 같은 입력으로 계속 실패하는 CPU 작업은 두 번 뒤 검토 큐로 보낸다. 알림 retry와 다른 예산이다.

대안 검토: 이전 값 `6`는 아래 장애 조건을 만족하지 못했습니다. 손상된 파일 하나가 worker 시간을 반복 소비하며 정상 작업을 밀어냈다.

적용: [정책 구현](../../src/policy.py)의 `retry_budget`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 입력 checksum과 실패 원인이 같은지 확인하고 재현 자료를 격리한다.

## worker shutdown grace

결정 JOBS-05: `worker shutdown grace`의 현재 값은 `45`입니다.

선택 이유: 종료 요청 뒤 새 claim은 막고 진행 중인 작업의 마지막 checkpoint에 시간을 준다.

대안 검토: 이전 값 `10`는 아래 장애 조건을 만족하지 못했습니다. 즉시 종료 때문에 결과는 썼지만 완료 표시가 없어 작업이 다시 실행됐다.

적용: [정책 구현](../../src/policy.py)의 `shutdown_seconds`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: draining 상태와 마지막 checkpoint를 확인한 뒤 프로세스를 종료한다.

## job admission

결정 JOBS-06: `job admission`의 현재 값은 `200`입니다.

선택 이유: 처리 용량을 넘는 작업은 접수 시 재시도 가능한 응답을 준다. 대기열을 무한히 늘리지 않는다.

대안 검토: 이전 값 `1000`는 아래 장애 조건을 만족하지 못했습니다. 대량 업로드가 오래된 작업의 처리 약속을 깨뜨렸다.

적용: [정책 구현](../../src/policy.py)의 `backlog_limit`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: tenant별 입장 제한을 확인하고 예약 작업의 backpressure를 켠다.

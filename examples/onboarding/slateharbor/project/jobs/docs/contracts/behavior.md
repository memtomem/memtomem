---
tags: [slateharbor, jobs, current]
---
# Slateharbor 백그라운드 작업: 동작 계약

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## worker lease recovery

입력: `heartbeat_age`. `worker lease recovery`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `lease_seconds`입니다.

승인 예: `40`. 거절 예: `120`. 기준은 [운영 설정](../../config/production.json)의 `90`입니다.

업무 의미: worker lease는 90초이며 완료 체크포인트를 먼저 확인한 후 재할당한다. timeout만 보고 같은 작업을 두 번 시작하지 않는다.

잘못된 적용 사례: INC-JOBS-22에서 GC pause로 heartbeat가 늦자 살아 있는 worker의 작업을 다른 worker가 다시 수행했다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## worker concurrency

입력: `running`. `worker concurrency`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `max_parallel`입니다.

승인 예: `2`. 거절 예: `8`. 기준은 [운영 설정](../../config/production.json)의 `4`입니다.

업무 의미: CPU 작업 4개를 넘기지 않아 heartbeat 스레드가 굶지 않게 한다.

잘못된 적용 사례: 과도한 병렬 압축이 이벤트 루프를 막아 lease가 연달아 만료됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## checkpoint interval

입력: `elapsed_seconds`. `checkpoint interval`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `checkpoint_seconds`입니다.

승인 예: `30`. 거절 예: `10`. 기준은 [운영 설정](../../config/production.json)의 `20`입니다.

업무 의미: 20초마다 재개 가능한 offset을 기록한다. 부분 파일이 durable해진 뒤에만 offset을 전진시킨다.

잘못된 적용 사례: 메모리상의 진행률만 저장해 재시작 후 누락된 출력 구간이 생겼다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## job retry budget

입력: `attempts`. `job retry budget`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `retry_budget`입니다.

승인 예: `1`. 거절 예: `4`. 기준은 [운영 설정](../../config/production.json)의 `2`입니다.

업무 의미: 같은 입력으로 계속 실패하는 CPU 작업은 두 번 뒤 검토 큐로 보낸다. 알림 retry와 다른 예산이다.

잘못된 적용 사례: 손상된 파일 하나가 worker 시간을 반복 소비하며 정상 작업을 밀어냈다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## worker shutdown grace

입력: `elapsed_seconds`. `worker shutdown grace`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `shutdown_seconds`입니다.

승인 예: `20`. 거절 예: `60`. 기준은 [운영 설정](../../config/production.json)의 `45`입니다.

업무 의미: 종료 요청 뒤 새 claim은 막고 진행 중인 작업의 마지막 checkpoint에 시간을 준다.

잘못된 적용 사례: 즉시 종료 때문에 결과는 썼지만 완료 표시가 없어 작업이 다시 실행됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## job admission

입력: `queued`. `job admission`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `backlog_limit`입니다.

승인 예: `100`. 거절 예: `300`. 기준은 [운영 설정](../../config/production.json)의 `200`입니다.

업무 의미: 처리 용량을 넘는 작업은 접수 시 재시도 가능한 응답을 준다. 대기열을 무한히 늘리지 않는다.

잘못된 적용 사례: 대량 업로드가 오래된 작업의 처리 약속을 깨뜨렸다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

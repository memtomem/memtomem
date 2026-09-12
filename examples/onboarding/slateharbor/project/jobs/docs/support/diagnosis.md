---
tags: [slateharbor, jobs, current]
---
# Slateharbor 백그라운드 작업: 지원 진단

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## worker lease recovery

문의 유형: `worker lease recovery`가 예상과 다르게 동작합니다.

증상: INC-JOBS-22에서 GC pause로 heartbeat가 늦자 살아 있는 worker의 작업을 다른 worker가 다시 수행했다.

확인 질문: 요청이 production인지 staging인지, `heartbeat_age`의 실제 값이 얼마인지 먼저 확인합니다.

진단: checkpoint와 lease owner를 함께 읽는다. 완료면 결과를 재사용하고 만료·미완료일 때만 다시 claim한다.

비교 기준: [현행 설정](../../config/production.json)의 `lease_seconds` = `90`.

설명할 이유: worker lease는 90초이며 완료 체크포인트를 먼저 확인한 후 재할당한다. timeout만 보고 같은 작업을 두 번 시작하지 않는다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## worker concurrency

문의 유형: `worker concurrency`가 예상과 다르게 동작합니다.

증상: 과도한 병렬 압축이 이벤트 루프를 막아 lease가 연달아 만료됐다.

확인 질문: 요청이 production인지 staging인지, `running`의 실제 값이 얼마인지 먼저 확인합니다.

진단: CPU와 heartbeat 지연을 함께 보고 동시 작업 수를 낮춘다.

비교 기준: [현행 설정](../../config/production.json)의 `max_parallel` = `4`.

설명할 이유: CPU 작업 4개를 넘기지 않아 heartbeat 스레드가 굶지 않게 한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## checkpoint interval

문의 유형: `checkpoint interval`가 예상과 다르게 동작합니다.

증상: 메모리상의 진행률만 저장해 재시작 후 누락된 출력 구간이 생겼다.

확인 질문: 요청이 production인지 staging인지, `elapsed_seconds`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 출력 flush와 checkpoint 순서를 확인하고 마지막 durable offset부터 재개한다.

비교 기준: [현행 설정](../../config/production.json)의 `checkpoint_seconds` = `20`.

설명할 이유: 20초마다 재개 가능한 offset을 기록한다. 부분 파일이 durable해진 뒤에만 offset을 전진시킨다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## job retry budget

문의 유형: `job retry budget`가 예상과 다르게 동작합니다.

증상: 손상된 파일 하나가 worker 시간을 반복 소비하며 정상 작업을 밀어냈다.

확인 질문: 요청이 production인지 staging인지, `attempts`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 입력 checksum과 실패 원인이 같은지 확인하고 재현 자료를 격리한다.

비교 기준: [현행 설정](../../config/production.json)의 `retry_budget` = `2`.

설명할 이유: 같은 입력으로 계속 실패하는 CPU 작업은 두 번 뒤 검토 큐로 보낸다. 알림 retry와 다른 예산이다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## worker shutdown grace

문의 유형: `worker shutdown grace`가 예상과 다르게 동작합니다.

증상: 즉시 종료 때문에 결과는 썼지만 완료 표시가 없어 작업이 다시 실행됐다.

확인 질문: 요청이 production인지 staging인지, `elapsed_seconds`의 실제 값이 얼마인지 먼저 확인합니다.

진단: draining 상태와 마지막 checkpoint를 확인한 뒤 프로세스를 종료한다.

비교 기준: [현행 설정](../../config/production.json)의 `shutdown_seconds` = `45`.

설명할 이유: 종료 요청 뒤 새 claim은 막고 진행 중인 작업의 마지막 checkpoint에 시간을 준다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## job admission

문의 유형: `job admission`가 예상과 다르게 동작합니다.

증상: 대량 업로드가 오래된 작업의 처리 약속을 깨뜨렸다.

확인 질문: 요청이 production인지 staging인지, `queued`의 실제 값이 얼마인지 먼저 확인합니다.

진단: tenant별 입장 제한을 확인하고 예약 작업의 backpressure를 켠다.

비교 기준: [현행 설정](../../config/production.json)의 `backlog_limit` = `200`.

설명할 이유: 처리 용량을 넘는 작업은 접수 시 재시도 가능한 응답을 준다. 대기열을 무한히 늘리지 않는다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

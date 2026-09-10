---
tags: [slateharbor, jobs, proposed]
---
# Slateharbor 백그라운드 작업: 검토 중 제안

> 합성 프로젝트 기록. 상태: proposed. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## worker lease recovery

상태: proposed. `worker lease recovery` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: INC-JOBS-22에서 GC pause로 heartbeat가 늦자 살아 있는 worker의 작업을 다른 worker가 다시 수행했다.

제안: TASK-JOBS-88: worker 재시작 뒤 완료 checkpoint를 읽는 경로부터 확인한다. 신규 작업 제출은 보류한다.

현재 계약: `lease_seconds` = `90`. worker lease는 90초이며 완료 체크포인트를 먼저 확인한 후 재할당한다. timeout만 보고 같은 작업을 두 번 시작하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## worker concurrency

상태: proposed. `worker concurrency` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 과도한 병렬 압축이 이벤트 루프를 막아 lease가 연달아 만료됐다.

제안: TASK-JOBS-89: 부하 테스트에 heartbeat deadline을 기록한다.

현재 계약: `max_parallel` = `4`. CPU 작업 4개를 넘기지 않아 heartbeat 스레드가 굶지 않게 한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## checkpoint interval

상태: proposed. `checkpoint interval` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 메모리상의 진행률만 저장해 재시작 후 누락된 출력 구간이 생겼다.

제안: TASK-JOBS-90: 강제 종료 지점을 바꿔 재개 테스트를 반복한다.

현재 계약: `checkpoint_seconds` = `20`. 20초마다 재개 가능한 offset을 기록한다. 부분 파일이 durable해진 뒤에만 offset을 전진시킨다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## job retry budget

상태: proposed. `job retry budget` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 손상된 파일 하나가 worker 시간을 반복 소비하며 정상 작업을 밀어냈다.

제안: TASK-JOBS-91: permanent 오류 분류를 검토한다.

현재 계약: `retry_budget` = `2`. 같은 입력으로 계속 실패하는 CPU 작업은 두 번 뒤 검토 큐로 보낸다. 알림 retry와 다른 예산이다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## worker shutdown grace

상태: proposed. `worker shutdown grace` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 즉시 종료 때문에 결과는 썼지만 완료 표시가 없어 작업이 다시 실행됐다.

제안: TASK-JOBS-92: 배포 스크립트의 grace 값과 맞춘다.

현재 계약: `shutdown_seconds` = `45`. 종료 요청 뒤 새 claim은 막고 진행 중인 작업의 마지막 checkpoint에 시간을 준다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## job admission

상태: proposed. `job admission` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 대량 업로드가 오래된 작업의 처리 약속을 깨뜨렸다.

제안: TASK-JOBS-93: 응답의 retry-after 안내를 추가한다.

현재 계약: `backlog_limit` = `200`. 처리 용량을 넘는 작업은 접수 시 재시도 가능한 응답을 준다. 대기열을 무한히 늘리지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

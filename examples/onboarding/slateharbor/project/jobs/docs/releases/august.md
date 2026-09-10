---
tags: [slateharbor, jobs, current]
---
# Slateharbor 백그라운드 작업: 8월 변경 기록

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## worker lease recovery

8월 변경: `worker lease recovery` 값이 `30`에서 `90`로 바뀌었습니다.

관련 관찰: INC-JOBS-22에서 GC pause로 heartbeat가 늦자 살아 있는 worker의 작업을 다른 worker가 다시 수행했다.

사용자에게 달라지는 점: worker lease는 90초이며 완료 체크포인트를 먼저 확인한 후 재할당한다. timeout만 보고 같은 작업을 두 번 시작하지 않는다.

확인 절차: checkpoint와 lease owner를 함께 읽는다. 완료면 결과를 재사용하고 만료·미완료일 때만 다시 claim한다.

추적 작업: TASK-JOBS-88: worker 재시작 뒤 완료 checkpoint를 읽는 경로부터 확인한다. 신규 작업 제출은 보류한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## worker concurrency

8월 변경: `worker concurrency` 값이 `16`에서 `4`로 바뀌었습니다.

관련 관찰: 과도한 병렬 압축이 이벤트 루프를 막아 lease가 연달아 만료됐다.

사용자에게 달라지는 점: CPU 작업 4개를 넘기지 않아 heartbeat 스레드가 굶지 않게 한다.

확인 절차: CPU와 heartbeat 지연을 함께 보고 동시 작업 수를 낮춘다.

추적 작업: TASK-JOBS-89: 부하 테스트에 heartbeat deadline을 기록한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## checkpoint interval

8월 변경: `checkpoint interval` 값이 `60`에서 `20`로 바뀌었습니다.

관련 관찰: 메모리상의 진행률만 저장해 재시작 후 누락된 출력 구간이 생겼다.

사용자에게 달라지는 점: 20초마다 재개 가능한 offset을 기록한다. 부분 파일이 durable해진 뒤에만 offset을 전진시킨다.

확인 절차: 출력 flush와 checkpoint 순서를 확인하고 마지막 durable offset부터 재개한다.

추적 작업: TASK-JOBS-90: 강제 종료 지점을 바꿔 재개 테스트를 반복한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## job retry budget

8월 변경: `job retry budget` 값이 `6`에서 `2`로 바뀌었습니다.

관련 관찰: 손상된 파일 하나가 worker 시간을 반복 소비하며 정상 작업을 밀어냈다.

사용자에게 달라지는 점: 같은 입력으로 계속 실패하는 CPU 작업은 두 번 뒤 검토 큐로 보낸다. 알림 retry와 다른 예산이다.

확인 절차: 입력 checksum과 실패 원인이 같은지 확인하고 재현 자료를 격리한다.

추적 작업: TASK-JOBS-91: permanent 오류 분류를 검토한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## worker shutdown grace

8월 변경: `worker shutdown grace` 값이 `10`에서 `45`로 바뀌었습니다.

관련 관찰: 즉시 종료 때문에 결과는 썼지만 완료 표시가 없어 작업이 다시 실행됐다.

사용자에게 달라지는 점: 종료 요청 뒤 새 claim은 막고 진행 중인 작업의 마지막 checkpoint에 시간을 준다.

확인 절차: draining 상태와 마지막 checkpoint를 확인한 뒤 프로세스를 종료한다.

추적 작업: TASK-JOBS-92: 배포 스크립트의 grace 값과 맞춘다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## job admission

8월 변경: `job admission` 값이 `1000`에서 `200`로 바뀌었습니다.

관련 관찰: 대량 업로드가 오래된 작업의 처리 약속을 깨뜨렸다.

사용자에게 달라지는 점: 처리 용량을 넘는 작업은 접수 시 재시도 가능한 응답을 준다. 대기열을 무한히 늘리지 않는다.

확인 절차: tenant별 입장 제한을 확인하고 예약 작업의 backpressure를 켠다.

추적 작업: TASK-JOBS-93: 응답의 retry-after 안내를 추가한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

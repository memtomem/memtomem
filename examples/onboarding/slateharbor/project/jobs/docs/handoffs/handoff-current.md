---
tags: [slateharbor, jobs, current]
---
# Slateharbor 백그라운드 작업: 작업 인계

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## worker lease recovery

작업: TASK-JOBS-88: worker 재시작 뒤 완료 checkpoint를 읽는 경로부터 확인한다. 신규 작업 제출은 보류한다.

이미 확정: `worker lease recovery` = `90`. worker lease는 90초이며 완료 체크포인트를 먼저 확인한 후 재할당한다. timeout만 보고 같은 작업을 두 번 시작하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: checkpoint와 lease owner를 함께 읽는다. 완료면 결과를 재사용하고 만료·미완료일 때만 다시 claim한다.

보류 사항: 원인 확인 없이 이전 `30`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## worker concurrency

작업: TASK-JOBS-89: 부하 테스트에 heartbeat deadline을 기록한다.

이미 확정: `worker concurrency` = `4`. CPU 작업 4개를 넘기지 않아 heartbeat 스레드가 굶지 않게 한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: CPU와 heartbeat 지연을 함께 보고 동시 작업 수를 낮춘다.

보류 사항: 원인 확인 없이 이전 `16`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## checkpoint interval

작업: TASK-JOBS-90: 강제 종료 지점을 바꿔 재개 테스트를 반복한다.

이미 확정: `checkpoint interval` = `20`. 20초마다 재개 가능한 offset을 기록한다. 부분 파일이 durable해진 뒤에만 offset을 전진시킨다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 출력 flush와 checkpoint 순서를 확인하고 마지막 durable offset부터 재개한다.

보류 사항: 원인 확인 없이 이전 `60`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## job retry budget

작업: TASK-JOBS-91: permanent 오류 분류를 검토한다.

이미 확정: `job retry budget` = `2`. 같은 입력으로 계속 실패하는 CPU 작업은 두 번 뒤 검토 큐로 보낸다. 알림 retry와 다른 예산이다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 입력 checksum과 실패 원인이 같은지 확인하고 재현 자료를 격리한다.

보류 사항: 원인 확인 없이 이전 `6`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## worker shutdown grace

작업: TASK-JOBS-92: 배포 스크립트의 grace 값과 맞춘다.

이미 확정: `worker shutdown grace` = `45`. 종료 요청 뒤 새 claim은 막고 진행 중인 작업의 마지막 checkpoint에 시간을 준다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: draining 상태와 마지막 checkpoint를 확인한 뒤 프로세스를 종료한다.

보류 사항: 원인 확인 없이 이전 `10`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## job admission

작업: TASK-JOBS-93: 응답의 retry-after 안내를 추가한다.

이미 확정: `job admission` = `200`. 처리 용량을 넘는 작업은 접수 시 재시도 가능한 응답을 준다. 대기열을 무한히 늘리지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: tenant별 입장 제한을 확인하고 예약 작업의 backpressure를 켠다.

보류 사항: 원인 확인 없이 이전 `1000`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

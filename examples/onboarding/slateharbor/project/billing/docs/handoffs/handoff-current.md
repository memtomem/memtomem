---
tags: [slateharbor, billing, current]
---
# Slateharbor 결제: 작업 인계

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## billing webhook retry

작업: TASK-BILL-21: 중복 수신 계수와 실제 청구 계수를 분리한다.

이미 확정: `billing webhook retry` = `3`. 결제 webhook 재전송은 idempotency key로 중복 청구를 막는다. 알림 worker의 5회 재시도 정책을 복사하면 안 된다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: provider event ID로 중복 수신을 확인하고 settlement 상태가 불명확하면 조회부터 한다.

보류 사항: 원인 확인 없이 이전 `7`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## invoice grace period

작업: TASK-BILL-22: 유예 종료 예고 메일에 실제 청구서 링크를 넣는다.

이미 확정: `invoice grace period` = `14`. 기업 고객의 월말 승인 주기를 반영하되 유예 기간 이후에는 쓰기 기능을 제한한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 계약상 유예 일수와 invoice 발행일을 대조하고 읽기 접근은 유지한다.

보류 사항: 원인 확인 없이 이전 `7`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## refund eligibility

작업: TASK-BILL-23: 경계일 테스트에 월말 구매를 추가한다.

이미 확정: `refund eligibility` = `30`. 사용량 정산이 닫힌 기간의 환불은 자동 경로 대신 수동 검토로 보낸다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 구매 시점과 정산 배치를 확인하고 수동 조정 번호를 남긴다.

보류 사항: 원인 확인 없이 이전 `60`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## minimum seats

작업: TASK-BILL-24: downgrade 경로의 견적 검사를 연결한다.

이미 확정: `minimum seats` = `3`. 팀 요금제 최소 좌석은 3석이다. 개인 무료 요금제에는 이 정책을 적용하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 요금제 유형을 먼저 확인하고 견적과 실제 청구에 같은 좌석 기준을 사용한다.

보류 사항: 원인 확인 없이 이전 `1`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## settlement lag

작업: TASK-BILL-25: 지연과 영구 누락을 다른 큐로 보낸다.

이미 확정: `settlement lag` = `20`. 공급자의 지연 이벤트를 기다린 후 불일치 경보를 낸다. 결제 성공 여부를 임의로 바꾸지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 공급자 event 시각과 수신 시각을 비교하고 지연 건은 다음 배치에서 재조회한다.

보류 사항: 원인 확인 없이 이전 `5`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## currency precision

작업: TASK-BILL-26: Decimal 변환 경로를 견적 UI와 공유한다.

이미 확정: `currency precision` = `2`. 이 합성 USD 요금제는 소수 둘째 자리까지 받는다. 다른 통화로 일반화하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 최소 통화 단위의 정수로 대조하고 입력 precision 초과를 거절한다.

보류 사항: 원인 확인 없이 이전 `4`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

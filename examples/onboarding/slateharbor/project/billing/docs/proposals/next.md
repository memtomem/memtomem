---
tags: [slateharbor, billing, proposed]
---
# Slateharbor 결제: 검토 중 제안

> 합성 프로젝트 기록. 상태: proposed. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## billing webhook retry

상태: proposed. `billing webhook retry` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: INC-BILL-09에서 공급자 timeout 뒤 재전송을 신규 청구로 처리해 중복 원장이 생겼다.

제안: TASK-BILL-21: 중복 수신 계수와 실제 청구 계수를 분리한다.

현재 계약: `webhook_attempts` = `3`. 결제 webhook 재전송은 idempotency key로 중복 청구를 막는다. 알림 worker의 5회 재시도 정책을 복사하면 안 된다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## invoice grace period

상태: proposed. `invoice grace period` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 휴일에 청구서가 승인되지 않아 정상 조직이 읽기조차 못 했다.

제안: TASK-BILL-22: 유예 종료 예고 메일에 실제 청구서 링크를 넣는다.

현재 계약: `invoice_days` = `14`. 기업 고객의 월말 승인 주기를 반영하되 유예 기간 이후에는 쓰기 기능을 제한한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## refund eligibility

상태: proposed. `refund eligibility` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 정산 종료 뒤 자동 환불이 실행돼 보고서 잔액과 공급자 잔액이 달라졌다.

제안: TASK-BILL-23: 경계일 테스트에 월말 구매를 추가한다.

현재 계약: `refund_days` = `30`. 사용량 정산이 닫힌 기간의 환불은 자동 경로 대신 수동 검토로 보낸다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## minimum seats

상태: proposed. `minimum seats` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 팀 요금제 계정이 좌석 1석까지 내려간 채 팀 기능을 계속 쓰면서 개인 요금제 기준 견적을 받았다.

제안: TASK-BILL-24: downgrade 경로의 견적 검사를 연결한다.

현재 계약: `seat_floor` = `3`. 팀 요금제 최소 좌석은 3석이다. 개인 무료 요금제에는 이 정책을 적용하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## settlement lag

상태: proposed. `settlement lag` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 짧은 경보 창으로 정상 지연 건마다 재정산 요청이 생성됐다.

제안: TASK-BILL-25: 지연과 영구 누락을 다른 큐로 보낸다.

현재 계약: `reconcile_minutes` = `20`. 공급자의 지연 이벤트를 기다린 후 불일치 경보를 낸다. 결제 성공 여부를 임의로 바꾸지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## currency precision

상태: proposed. `currency precision` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 부동소수점 변환 후 공급자 요청과 로컬 원장 합계가 달라졌다.

제안: TASK-BILL-26: Decimal 변환 경로를 견적 UI와 공유한다.

현재 계약: `currency_scale` = `2`. 이 합성 USD 요금제는 소수 둘째 자리까지 받는다. 다른 통화로 일반화하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

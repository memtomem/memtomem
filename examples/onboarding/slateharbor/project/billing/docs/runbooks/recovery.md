---
tags: [slateharbor, billing, current]
---
# Slateharbor 결제: 복구 절차

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## billing webhook retry

시작 조건: INC-BILL-09에서 공급자 timeout 뒤 재전송을 신규 청구로 처리해 중복 원장이 생겼다.

1. provider event ID로 중복 수신을 확인하고 settlement 상태가 불명확하면 조회부터 한다.

2. [운영 설정](../../config/production.json)에서 `billing webhook retry` / `webhook_attempts`의 현재 값 `3`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 결제 webhook 재전송은 idempotency key로 중복 청구를 막는다. 알림 worker의 5회 재시도 정책을 복사하면 안 된다.

## invoice grace period

시작 조건: 휴일에 청구서가 승인되지 않아 정상 조직이 읽기조차 못 했다.

1. 계약상 유예 일수와 invoice 발행일을 대조하고 읽기 접근은 유지한다.

2. [운영 설정](../../config/production.json)에서 `invoice grace period` / `invoice_days`의 현재 값 `14`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 기업 고객의 월말 승인 주기를 반영하되 유예 기간 이후에는 쓰기 기능을 제한한다.

## refund eligibility

시작 조건: 정산 종료 뒤 자동 환불이 실행돼 보고서 잔액과 공급자 잔액이 달라졌다.

1. 구매 시점과 정산 배치를 확인하고 수동 조정 번호를 남긴다.

2. [운영 설정](../../config/production.json)에서 `refund eligibility` / `refund_days`의 현재 값 `30`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 사용량 정산이 닫힌 기간의 환불은 자동 경로 대신 수동 검토로 보낸다.

## minimum seats

시작 조건: 팀 요금제 계정이 좌석 1석까지 내려간 채 팀 기능을 계속 쓰면서 개인 요금제 기준 견적을 받았다.

1. 요금제 유형을 먼저 확인하고 견적과 실제 청구에 같은 좌석 기준을 사용한다.

2. [운영 설정](../../config/production.json)에서 `minimum seats` / `seat_floor`의 현재 값 `3`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 팀 요금제 최소 좌석은 3석이다. 개인 무료 요금제에는 이 정책을 적용하지 않는다.

## settlement lag

시작 조건: 짧은 경보 창으로 정상 지연 건마다 재정산 요청이 생성됐다.

1. 공급자 event 시각과 수신 시각을 비교하고 지연 건은 다음 배치에서 재조회한다.

2. [운영 설정](../../config/production.json)에서 `settlement lag` / `reconcile_minutes`의 현재 값 `20`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 공급자의 지연 이벤트를 기다린 후 불일치 경보를 낸다. 결제 성공 여부를 임의로 바꾸지 않는다.

## currency precision

시작 조건: 부동소수점 변환 후 공급자 요청과 로컬 원장 합계가 달라졌다.

1. 최소 통화 단위의 정수로 대조하고 입력 precision 초과를 거절한다.

2. [운영 설정](../../config/production.json)에서 `currency precision` / `currency_scale`의 현재 값 `2`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 이 합성 USD 요금제는 소수 둘째 자리까지 받는다. 다른 통화로 일반화하지 않는다.

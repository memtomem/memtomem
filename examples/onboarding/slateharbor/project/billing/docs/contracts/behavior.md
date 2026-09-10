---
tags: [slateharbor, billing, current]
---
# Slateharbor 결제: 동작 계약

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## billing webhook retry

입력: `attempts`. `billing webhook retry`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `webhook_attempts`입니다.

승인 예: `1`. 거절 예: `5`. 기준은 [운영 설정](../../config/production.json)의 `3`입니다.

업무 의미: 결제 webhook 재전송은 idempotency key로 중복 청구를 막는다. 알림 worker의 5회 재시도 정책을 복사하면 안 된다.

잘못된 적용 사례: INC-BILL-09에서 공급자 timeout 뒤 재전송을 신규 청구로 처리해 중복 원장이 생겼다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## invoice grace period

입력: `overdue_days`. `invoice grace period`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `invoice_days`입니다.

승인 예: `10`. 거절 예: `20`. 기준은 [운영 설정](../../config/production.json)의 `14`입니다.

업무 의미: 기업 고객의 월말 승인 주기를 반영하되 유예 기간 이후에는 쓰기 기능을 제한한다.

잘못된 적용 사례: 휴일에 청구서가 승인되지 않아 정상 조직이 읽기조차 못 했다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## refund eligibility

입력: `purchase_age_days`. `refund eligibility`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `refund_days`입니다.

승인 예: `15`. 거절 예: `45`. 기준은 [운영 설정](../../config/production.json)의 `30`입니다.

업무 의미: 사용량 정산이 닫힌 기간의 환불은 자동 경로 대신 수동 검토로 보낸다.

잘못된 적용 사례: 정산 종료 뒤 자동 환불이 실행돼 보고서 잔액과 공급자 잔액이 달라졌다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## minimum seats

입력: `seats`. `minimum seats`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `seat_floor`입니다.

승인 예: `5`. 거절 예: `1`. 기준은 [운영 설정](../../config/production.json)의 `3`입니다.

업무 의미: 팀 요금제 최소 좌석은 3석이다. 개인 무료 요금제에는 이 정책을 적용하지 않는다.

잘못된 적용 사례: 팀 요금제 계정이 좌석 1석까지 내려간 채 팀 기능을 계속 쓰면서 개인 요금제 기준 견적을 받았다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## settlement lag

입력: `lag_minutes`. `settlement lag`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `reconcile_minutes`입니다.

승인 예: `10`. 거절 예: `40`. 기준은 [운영 설정](../../config/production.json)의 `20`입니다.

업무 의미: 공급자의 지연 이벤트를 기다린 후 불일치 경보를 낸다. 결제 성공 여부를 임의로 바꾸지 않는다.

잘못된 적용 사례: 짧은 경보 창으로 정상 지연 건마다 재정산 요청이 생성됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## currency precision

입력: `decimal_places`. `currency precision`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `currency_scale`입니다.

승인 예: `2`. 거절 예: `3`. 기준은 [운영 설정](../../config/production.json)의 `2`입니다.

업무 의미: 이 합성 USD 요금제는 소수 둘째 자리까지 받는다. 다른 통화로 일반화하지 않는다.

잘못된 적용 사례: 부동소수점 변환 후 공급자 요청과 로컬 원장 합계가 달라졌다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

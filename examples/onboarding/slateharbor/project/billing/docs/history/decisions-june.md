---
tags: [slateharbor, billing, superseded]
---
# Slateharbor 결제: 대체된 6월 결정

> 합성 프로젝트 기록. 상태: superseded. 기준일: 2026-06-01. 실제 운영 지침이 아닙니다.

## billing webhook retry

상태: superseded. 6월의 `billing webhook retry` 값은 `7`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: INC-BILL-09에서 공급자 timeout 뒤 재전송을 신규 청구로 처리해 중복 원장이 생겼다.

8월 결정으로 대체: `3`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## invoice grace period

상태: superseded. 6월의 `invoice grace period` 값은 `7`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 휴일에 청구서가 승인되지 않아 정상 조직이 읽기조차 못 했다.

8월 결정으로 대체: `14`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## refund eligibility

상태: superseded. 6월의 `refund eligibility` 값은 `60`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 정산 종료 뒤 자동 환불이 실행돼 보고서 잔액과 공급자 잔액이 달라졌다.

8월 결정으로 대체: `30`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## minimum seats

상태: superseded. 6월의 `minimum seats` 값은 `1`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 팀 요금제 계정이 좌석 1석까지 내려간 채 팀 기능을 계속 쓰면서 개인 요금제 기준 견적을 받았다.

8월 결정으로 대체: `3`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## settlement lag

상태: superseded. 6월의 `settlement lag` 값은 `5`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 짧은 경보 창으로 정상 지연 건마다 재정산 요청이 생성됐다.

8월 결정으로 대체: `20`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## currency precision

상태: superseded. 6월의 `currency precision` 값은 `4`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 부동소수점 변환 후 공급자 요청과 로컬 원장 합계가 달라졌다.

8월 결정으로 대체: `2`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.
